import csv
import hmac
import html
import io
import json
import os
import re
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from flask import Flask, Response, redirect, request

import tenderscout as collector

app = Flask(__name__)

# Storage: PostgreSQL when DATABASE_URL is set, otherwise a local SQLite file.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))
DB_PATH = os.environ.get("TENDERSCOUT_DB", "tenderscout.db")

MIN_SCORE = 3
SERVICES = list(collector.SERVICE_ORDER)
STAGES = ["Reviewing", "Interested", "Bidding", "Submitted", "Won", "Lost", "Not for us"]
SERVICE_FILTERS = dict(collector.SERVICE_LABELS)
TRADE_CATEGORIES = list(collector.TRADE_CATEGORIES)

_schema_ready = False
LAST_FEEDS = {}  # what each government feed returned on the latest refresh
_EMPTY_BOARD_REFRESH_ATTEMPTED = False
# Refresh ownership is coordinated by an atomic database lease, not process memory.
# A refresh without a heartbeat for 15 minutes is presumed interrupted.
# Active collectors renew the lease every 30 seconds.
REFRESH_STALE_AFTER_SECONDS = 15 * 60


def env_int(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- database --

class Database:
    """Thin wrapper so the same SQL runs on SQLite and PostgreSQL."""

    def __init__(self):
        if USE_POSTGRES:
            import psycopg
            from psycopg.rows import dict_row

            self.conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        else:
            self.conn = sqlite3.connect(DB_PATH)
            self.conn.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        if USE_POSTGRES:
            sql = sql.replace("?", "%s")
        return self.conn.execute(sql, params)

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


COLUMNS = """
    source TEXT, title TEXT, buyer TEXT,
    published_date TEXT, location_text TEXT, description TEXT,
    estimated_value_gbp TEXT, contract_start_date TEXT, contract_end_date TEXT,
    contact_name TEXT, contact_email TEXT, contact_phone TEXT, eligibility_text TEXT,
    service_match TEXT, score INTEGER,
    deadline TEXT, notice_url TEXT UNIQUE,
    geography_reason TEXT,
    region_match TEXT DEFAULT 'REVIEW',
    trade_category TEXT DEFAULT 'Other / unclassified',
    saved_at TEXT,
    pipeline_status TEXT DEFAULT 'Reviewing',
    opportunity_stage TEXT DEFAULT 'Other notice'
"""
# Columns added after the first release; added to older databases on start-up.
# Older rows default to REVIEW because their location was never confirmed.
ADDED_COLUMNS = [
    ("pipeline_status", "TEXT DEFAULT 'Reviewing'"),
    ("region_match", "TEXT DEFAULT 'REVIEW'"),
    ("opportunity_stage", "TEXT DEFAULT 'Other notice'"),
    ("description", "TEXT"),
    ("estimated_value_gbp", "TEXT"),
    ("contract_start_date", "TEXT"),
    ("contract_end_date", "TEXT"),
    ("contact_name", "TEXT"),
    ("contact_email", "TEXT"),
    ("contact_phone", "TEXT"),
    ("eligibility_text", "TEXT"),
]


def ensure_schema(db):
    global _schema_ready
    if _schema_ready:
        return
    id_column = "SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
    db.execute(f"CREATE TABLE IF NOT EXISTS tenders (id {id_column}, {COLUMNS})")
    if USE_POSTGRES:
        db.execute("ALTER TABLE tenders ADD COLUMN IF NOT EXISTS trade_category TEXT DEFAULT 'Other / unclassified'")
        db.execute("ALTER TABLE tenders ADD COLUMN IF NOT EXISTS opportunity_stage TEXT DEFAULT 'Other notice'")
    else:
        existing_columns = {row[1] for row in db.execute("PRAGMA table_info(tenders)").fetchall()}
        if "trade_category" not in existing_columns:
            db.execute("ALTER TABLE tenders ADD COLUMN trade_category TEXT DEFAULT 'Other / unclassified'")
        if "opportunity_stage" not in existing_columns:
            db.execute("ALTER TABLE tenders ADD COLUMN opportunity_stage TEXT DEFAULT 'Other notice'")
    db.execute(f"""CREATE TABLE IF NOT EXISTS private_leads (
        id {id_column},
        title TEXT NOT NULL,
        trade_category TEXT NOT NULL,
        location_text TEXT NOT NULL,
        source_name TEXT NOT NULL,
        source_url TEXT,
        contact_details TEXT,
        details TEXT,
        urgency TEXT DEFAULT 'Normal',
        status TEXT DEFAULT 'New',
        created_at TEXT NOT NULL
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS external_projects (
        id {id_column},
        title TEXT NOT NULL,
        trade_category TEXT NOT NULL,
        location_text TEXT NOT NULL,
        details TEXT,
        source_name TEXT NOT NULL,
        source_url TEXT NOT NULL UNIQUE,
        published_date TEXT,
        location_status TEXT NOT NULL DEFAULT 'Potential local — verify',
        imported_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS refresh_state (
        id INTEGER PRIMARY KEY,
        status TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        summary TEXT,
        error TEXT,
        refresh_token TEXT,
        heartbeat_at TEXT
    )""")
    if USE_POSTGRES:
        db.execute("ALTER TABLE refresh_state ADD COLUMN IF NOT EXISTS refresh_token TEXT")
        db.execute("ALTER TABLE refresh_state ADD COLUMN IF NOT EXISTS heartbeat_at TEXT")
    else:
        refresh_columns = {row[1] for row in db.execute("PRAGMA table_info(refresh_state)").fetchall()}
        if "refresh_token" not in refresh_columns:
            db.execute("ALTER TABLE refresh_state ADD COLUMN refresh_token TEXT")
        if "heartbeat_at" not in refresh_columns:
            db.execute("ALTER TABLE refresh_state ADD COLUMN heartbeat_at TEXT")
    db.execute("INSERT INTO refresh_state (id, status) VALUES (1, 'idle') ON CONFLICT(id) DO NOTHING")
    if USE_POSTGRES:
        db.execute("ALTER TABLE private_leads ADD COLUMN IF NOT EXISTS urgency TEXT DEFAULT 'Normal'")
    else:
        lead_columns = {row[1] for row in db.execute("PRAGMA table_info(private_leads)").fetchall()}
        if "urgency" not in lead_columns:
            db.execute("ALTER TABLE private_leads ADD COLUMN urgency TEXT DEFAULT 'Normal'")
    if USE_POSTGRES:
        for name, ddl in ADDED_COLUMNS:
            db.execute(f"ALTER TABLE tenders ADD COLUMN IF NOT EXISTS {name} {ddl}")
    else:
        existing = {row[1] for row in db.execute("PRAGMA table_info(tenders)").fetchall()}
        for name, ddl in ADDED_COLUMNS:
            if name not in existing:
                db.execute(f"ALTER TABLE tenders ADD COLUMN {name} {ddl}")
    db.commit()
    _schema_ready = True


def connect_db():
    db = Database()
    ensure_schema(db)
    return db


# ---------------------------------------------------------------- security --

@app.before_request
def require_login():
    """Fail closed: dashboard and private customer leads are never public."""
    if request.path == "/health":
        return None
    user = os.environ.get("DASHBOARD_USER", "")
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not (user and password):
        return Response("TenderScout is locked until DASHBOARD_USER and DASHBOARD_PASSWORD are configured.", 503, {"Cache-Control": "no-store"})
    auth = request.authorization
    if auth and hmac.compare_digest((auth.username or "").encode(), user.encode()) and hmac.compare_digest((auth.password or "").encode(), password.encode()):
        return None
    return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="TenderScout"', "Cache-Control": "no-store"})


@app.before_request
def reject_cross_site_posts():
    """Block forms submitted from other websites while you are logged in."""
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            return Response("Cross-site request blocked", 403)
    return None


def safe(value):
    return html.escape(str(value or ""))


def csv_safe(value):
    """Stop spreadsheet apps treating feed text as a formula."""
    text = "" if value is None else str(value)
    if text and text[0] in "=+-@\t\r":
        return "'" + text
    return text


# ------------------------------------------------------------------ routes --

@app.route("/health")
def health():
    try:
        db = connect_db()
        db.execute("SELECT 1")
        db.close()
        return {"status": "ok", "database": "postgres" if USE_POSTGRES else "sqlite"}
    except Exception:
        app.logger.exception("Health check failed")
        return {"status": "error"}, 503


def count_by_region(db):
    """Count stored opportunities without treating unverified locations as confirmed."""
    counts = {"main": 0, "review": 0, "confirmed_local": 0, "manual": 0}
    for row in db.execute(
        "SELECT region_match, COUNT(*) AS n FROM tenders GROUP BY region_match"
    ).fetchall():
        region = row["region_match"]
        if region == "REVIEW":
            counts["review"] += row["n"]
        else:
            counts["main"] += row["n"]
        if region == "LOCAL":
            counts["confirmed_local"] += row["n"]
        elif region == "MANUAL":
            counts["manual"] += row["n"]
    return counts


def has_target_area_clue(row):
    """True only when a review record names a target-area clue worth surfacing.

    This does not promote a record to LOCAL. It lets the main board surface
    potentially relevant notices while preserving the explicit location-check
    badge. Records with no target-area evidence remain in the full review queue.
    """
    # Buyer names can justify a manual look, but never establish delivery geography.
    text = " ".join(str(row[field] or "") for field in ("location_text", "title", "description", "buyer")).lower()
    local_hits = collector.place_hits(text, collector.LOCAL_PLACE_TERMS)
    out_hits = collector.place_hits(text, collector.OUT_OF_AREA_TERMS)
    local_hits = collector.drop_contained(local_hits, out_hits)
    full, bare = collector.postcode_districts(text, include_bare=True)
    return bool(local_hits or ((full | bare) & collector.LOCAL_POSTCODE_DISTRICTS))


def deadline_info(deadline):
    """Return (label, css class, closed) for a tender deadline string."""
    when = collector.parse_date(deadline)
    if not when:
        return "Check official notice", "", False
    days = (when.date() - datetime.now(timezone.utc).date()).days
    date_text = when.strftime("%d %b %Y").lstrip("0")
    if days < 0:
        return f"Closed {date_text}", "closed", True
    if days == 0:
        return f"Closes today ({date_text})", "soon", False
    if days <= 7:
        return f"Closes in {days} day{'s' if days != 1 else ''} ({date_text})", "soon", False
    return f"Closes {date_text} ({days} days)", "", False


def format_money(value):
    """Format a published contract value without inventing a missing amount."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "Not stated in the feed"
    return f"£{amount:,.0f}" if amount.is_integer() else f"£{amount:,.2f}"


def display_date(value):
    parsed = collector.parse_date(value)
    return parsed.strftime("%d %B %Y").lstrip("0") if parsed else ""


def contract_length(start_date, end_date):
    start = display_date(start_date)
    end = display_date(end_date)
    if start and end:
        return f"From {start} to {end}"
    if start:
        return f"Starts {start}; end date not stated"
    if end:
        return f"End date {end}; start date not stated"
    return "Not stated in the feed — check the official notice"


def dashboard_url(view="", service=""):
    params = {k: v for k, v in (("view", view), ("service", service)) if v}
    return "/?" + urlencode(params) if params else "/"


@app.route("/", methods=["GET"])
def dashboard():
    review_view = request.args.get("view") == "review"
    pipeline_view = request.args.get("view") == "pipeline"
    trade = request.args.get("trade", "")
    trade = trade if trade in TRADE_CATEGORIES else ""
    service = request.args.get("service", "")
    service = service if service in SERVICE_FILTERS else ""
    view = "review" if review_view else ("pipeline" if pipeline_view else "")

    db = connect_db()
    # Never collect feeds during a dashboard GET. External sources can be slow or
    # rate-limit requests; render the dashboard immediately and let the explicit
    # /api/refresh action run collection in the background.
    # The default board includes confirmed local jobs plus *potentially relevant*
    # location-review notices that mention one of the target areas. They are
    # labelled as unverified below; the full review view remains available.
    condition = (
        "region_match = 'REVIEW'" if review_view
        else "region_match IN ('LOCAL', 'REVIEW', 'MANUAL')"
    )
    params = []
    if pipeline_view:
        condition += " AND opportunity_stage = ?"
        params.append("Planning / upcoming")
    if trade:
        condition += " AND trade_category = ?"
        params.append(trade)
    elif service:
        condition += " AND service_match = ?"
        params.append(SERVICE_FILTERS[service])
    params = tuple(params)
    rows = db.execute(
        f"SELECT * FROM tenders WHERE {condition} "
        "ORDER BY score DESC, published_date DESC",
        params,
    ).fetchall()
    counts = count_by_region(db)
    if not review_view:
        rows = [
            row for row in rows
            if row["region_match"] != "REVIEW" or has_target_area_clue(row)
        ]
    db.close()

    decorated = [(row, *deadline_info(row["deadline"])) for row in rows]
    if not review_view and not pipeline_view:
        # The main jobs board is for actionable bids, not expired tenders or
        # award/contract-update notices. Keep the underlying records for review
        # and CSV export; planning notices have their own pipeline view.
        decorated = [
            item for item in decorated
            if (item[0]["opportunity_stage"] or "Other notice") == "Open for bids"
            and not item[3]
        ]
    # Open tenders first; soon-to-close opportunities remain visible at the top.
    decorated.sort(key=lambda item: item[3])

    cards = ""
    for row, deadline_label, deadline_class, _closed in decorated:
        link = safe(row["notice_url"])
        title_text = row["title"] or "Untitled opportunity"
        buyer_text = row["buyer"] or "the organisation named in the notice"
        trade_text = row["trade_category"] or ""
        if not trade_text or trade_text == "Other / unclassified":
            trade_text = row["service_match"] or "this type of work"
        location_text = row["location_text"] or "Delivery location not supplied — open the official notice";
        location_display = location_text;
        if location_text.strip().upper() in {"UK, UNITED KINGDOM", "UNITED KINGDOM", "UKD63"}:
            location_display = ("Cheshire West and Chester region (UKD63); full site postcode not supplied" if location_text.strip().upper() == "UKD63" else "UK-wide location only; exact site postcode not supplied")
        stage_text = row["opportunity_stage"] or "Other notice"
        region_badge = (
            "POTENTIAL LOCAL — verify delivery site" if row["region_match"] == "REVIEW"
            else "Confirmed local" if row["region_match"] == "LOCAL"
            else "Manual entry"
        )
        if stage_text == "Planning / upcoming":
            simple_explanation = (
                f"This is an early warning that {buyer_text} may need {trade_text.lower()} work. "
                "It is not an open invitation to bid yet. Use it to prepare and check back for the full tender notice."
            )
            how_to_apply = (
                "You cannot submit a bid from this pipeline notice alone. Open the official notice, "
                "note any planned dates, and look for the later tender notice with the application instructions."
            )
        elif stage_text == "Open for bids":
            simple_explanation = (
                f"{buyer_text} is looking for a business to provide {trade_text.lower()} work. "
                "If your business can do this work, check the rules and submit an offer before the deadline."
            )
            how_to_apply = (
                "Open the official notice and follow its submission instructions. The buyer may require "
                "you to register on a procurement website and upload forms or supporting documents."
            )
        else:
            simple_explanation = (
                f"This is a procurement notice about {title_text}. It may be a planning update or another "
                "notice type rather than a live invitation to bid. Check the official notice before taking action."
            )
            how_to_apply = "Open the official notice to confirm whether bids are being accepted and how to respond."
        eligibility = row["eligibility_text"] or (
            f"The notice feed does not say exactly who qualifies. Businesses that provide {trade_text.lower()} "
            "may be relevant, but check the official notice for required experience, insurance, qualifications, "
            "accreditations, financial checks and any limits on who can apply."
        )
        value_label = format_money(row["estimated_value_gbp"])
        duration_label = contract_length(row["contract_start_date"], row["contract_end_date"])
        notice_label = (
            "Open source listing — verify official notice"
            if "Standstill UK" in str(row["source"] or "")
            else "View official notice"
        )
        notice = (
            f'<a href="{link}" target="_blank" rel="noopener">{notice_label}</a>'
            if link.startswith("https://") else ""
        )
        share_actions = ""
        if link.startswith("https://"):
            share_title = row["title"] or "Tender opportunity"
            share_message = "\n".join([
                share_title,
                "Buyer: " + str(row["buyer"] or "Not listed"),
                "Trade: " + str(row["trade_category"] or row["service_match"] or "Unclassified"),
                "Location: " + str(row["location_text"] or "Check official notice"),
                "Source: " + str(row["source"] or "Public procurement notice"),
                "Official notice: " + str(row["notice_url"]),
            ])
            whatsapp_url = "https://wa.me/?text=" + urlencode({"text": share_message})[5:]
            email_url = "mailto:?" + urlencode({
                "subject": "Tender opportunity: " + share_title,
                "body": share_message,
            })
            share_actions = (
                '<p class="share-actions">'
                f'<a href="{safe(whatsapp_url)}" target="_blank" rel="noopener">Share by WhatsApp</a>'
                ' · '
                f'<a href="{safe(email_url)}">Share by Email</a>'
                '</p>'
            )
        current = row["pipeline_status"] or "Reviewing"
        options = "".join(
            f'<option{" selected" if stage == current else ""}>{safe(stage)}</option>'
            for stage in STAGES
        )
        cards += f"""
        <article class="card">
          <p class="job-kicker">{safe(row["trade_category"] or row["service_match"] or "Trade opportunity")} · {safe(region_badge)}</p>
          <h2>{safe(row["title"]) or "Untitled opportunity"}</h2>
          <p class="board-buyer">{safe(buyer_text)}</p>
          <div class="board-facts">
            <p><b>Location / postcode</b><span>{safe(location_display)}</span></p>
            <p><b>Estimated value</b><span>{safe(value_label)}</span></p>
            <p><b>Deadline</b><span class="deadline {deadline_class}">{safe(deadline_label)}</span></p>
            <p><b>Opportunity type</b><span>{safe(stage_text)}</span></p>
          </div>
          <section class="board-summary">
            <h3>10-second check</h3>
            <p><b>JOB:</b> {safe(title_text)} — {safe((row["description"] or simple_explanation or "Open the notice for the job specification.")[:240])}</p>
            <p><b>WHERE:</b> {safe(location_display)} · {safe(region_badge)}</p>
            <p><b>VALUE / DEADLINE / CONTRACT:</b> {safe(value_label)} · {safe(deadline_label)} · {safe(duration_label)}</p>
          </section>
          <details class="full-details"><summary>More details, eligibility and buyer contact</summary>
          <section class="plain-english">
            <h3>In simple terms</h3>
            <p>{safe(simple_explanation)}</p>
            <div class="job-facts">
              <p><b>Where is the work?</b><span>{safe(location_text)}</span></p>
              <p><b>How much is it worth?</b><span>{safe(value_label)}{(" (published estimated value; check whether VAT, options and all contract years are included)" if row["estimated_value_gbp"] not in (None, "") else "")}</span></p>
              <p><b>How long is the contract?</b><span>{safe(duration_label)}</span></p>
              <p><b>Who can bid?</b><span>{safe(eligibility)}</span></p>
              <p><b>How do you apply?</b><span>{safe(how_to_apply)}</span></p>
              <p><b>Deadline</b><span>{safe(deadline_label)}</span></p>
            </div>
          </section>
          {('<p class="job-description"><b>Official description</b><br>' + safe(row["description"]) + '</p>') if row["description"] else '<p class="job-description"><b>Official description</b><br>The feed does not include a description. Open the official notice for the full specification.</p>'}
          {('<div class="contact-box"><b>Buyer contact</b>' +
             ('<p>Contact: ' + safe(row["contact_name"]) + '</p>' if row["contact_name"] else '') +
             ('<p>Email: <a href="mailto:' + safe(row["contact_email"]) + '">' + safe(row["contact_email"]) + '</a></p>' if row["contact_email"] else '') +
             ('<p>Telephone: <a href="tel:' + safe(row["contact_phone"]) + '">' + safe(row["contact_phone"]) + '</a></p>' if row["contact_phone"] else '') +
             ('<p>Contact details not supplied in the feed; use the official notice.</p>' if not (row["contact_name"] or row["contact_email"] or row["contact_phone"]) else '') +
             '</div>')}
          <p class="notice-meta"><b>Opportunity type:</b> {safe(row["opportunity_stage"] or "Open for bids")} · <b>Relevance score:</b> {safe(row["score"])}</p>
          <p class="location-reason">{safe(row["geography_reason"])}</p>
          <form method="post" action="/tender/{int(row["id"])}/stage" class="stage">
            <input type="hidden" name="view" value="{view}">
            <input type="hidden" name="service" value="{service}">
            <label><b>Bid progress:</b>
              <select name="stage" onchange="this.form.submit()">{options}</select>
            </label>
            <noscript><button type="submit">Save</button></noscript>
          </form>
          {notice}
          {share_actions}
          </details>
        </article>
        """

    if not cards:
        if review_view:
            cards = "<p>Nothing to check right now.</p>"
        elif trade:
            cards = (
                f'<p>No saved opportunities match the trade “{safe(trade)}”. '
                f'<a href="{safe(dashboard_url(view, service))}">Show all trades</a>, '
                "or refresh to check for new notices.</p>"
            )
        elif service:
            cards = (
                f'<p>No saved opportunities match the {safe(service)} filter. '
                f'<a href="{safe(dashboard_url(view))}">Show all trades</a>, '
                "or refresh to check for new notices.</p>"
            )
        elif counts["review"] and not pipeline_view:
            cards = (
                f'<p>No confirmed local opportunities or target-area candidates are currently visible. '
                f'{counts["review"]} notices are in the full location-review queue. '
                f'<a href="{safe(dashboard_url("review", service))}">Review location-uncertain tenders</a>, '
                "or refresh to check for new notices.</p>"
            )
        else:
            cards = "<p>No tenders saved yet. Press Refresh tenders to search.</p>"

    if review_view:
        heading = f"{len(rows)} tenders need a location check"
        switch = f'<a href="{dashboard_url("", service)}">Back to jobs board ({counts["main"] + counts["review"]})</a>'
    elif pipeline_view:
        heading = f"{len(rows)} pipeline / upcoming opportunities"
        switch = f'<a href="{dashboard_url("", service)}">Back to jobs board ({counts["main"] + counts["review"]})</a>'
    else:
        if review_view:
            heading = f"{len(rows)} notices in the full location-review queue"
        elif pipeline_view:
            heading = f"{len(rows)} pipeline / upcoming opportunities"
        else:
            potential_count = sum(1 for row in rows if row["region_match"] == "REVIEW")
            heading = (
                f"{len(rows)} open opportunities · {counts['confirmed_local']} confirmed local overall · "
                f"{potential_count} possible local match(es) shown with location unverified"
            )
        switch = f'<a href="{dashboard_url("pipeline", service)}">Pipeline / upcoming</a> · <a href="{dashboard_url("review", service)}">Full location review ({counts["review"]})</a>'

    selected_trade = trade
    filters = "".join(
        f'<option value="{safe(category)}"{" selected" if category == selected_trade else ""}>{safe(category)}</option>'
        for category in TRADE_CATEGORIES
    )
    # Quick trade sections put the common categories one tap away on mobile.
    trade_groups = {
        "Cleaning": ["Commercial and office cleaning", "Domestic and communal-area cleaning", "Deep cleaning", "Window cleaning", "Gutter cleaning", "Carpet and upholstery cleaning", "End-of-tenancy cleaning", "Waste removal and clearance"],
        "Property maintenance": ["Building and general maintenance", "Handyman and odd jobs", "Grounds maintenance and gardening", "Roofing and roof repairs", "Roofline, fascias and soffits", "Drainage", "Pest control"],
        "Plumbing & electrical": ["Plumbing", "Heating and boilers", "Gas safety and servicing", "Electrical installation and repairs", "Electrical testing and inspection", "Fire alarms and fire safety"],
        "Building trades": ["Painting and decorating", "Plastering and rendering", "Tiling", "Joinery and carpentry", "Bricklaying and masonry", "Kitchens and bathrooms", "Flooring", "Driveways and patios"],
    }
    quick_sections = []
    for group_name, categories in trade_groups.items():
        chips = "".join(
            f'<a class="trade-chip" href="/?trade={urlencode({"trade": category})[6:]}' +
            (f'&amp;view={safe(view)}' if view else '') +
            f'">{safe(category)}</a>'
            for category in categories
        )
        quick_sections.append(f'<section class="trade-group"><h3>{safe(group_name)}</h3><div class="trade-chips">{chips}</div></section>')
    filter_form = (
        '<section class="trade-browser"><h2>Find tenders by trade</h2>'
        '<p class="note">Choose a trade to narrow the list. The refresh checks official public-sector feeds.</p>'
        + "".join(quick_sections)
        + '<details><summary>Browse all 71 trade categories</summary>'
        + '<form method="get" action="/" style="margin:12px 0">'
        + (f'<input type="hidden" name="view" value="{view}">' if view else '')
        + '<label for="trade-filter"><b>All trade categories</b></label> '
        + '<select id="trade-filter" name="trade" onchange="this.form.submit()" style="max-width:100%;padding:10px;font-size:16px">'
        + '<option value="">All trades</option>' + filters + '</select>'
        + '<noscript><button type="submit">Apply filter</button></noscript></form></details></section>'
    )

    banner = ""
    if request.args.get("refresh_started") == "1":
        banner = '<div class="banner"><b>Refresh already running.</b> The existing search is still in progress. Reload this page shortly to see its results.</div>'
    if request.args.get("done") == "1":
        try:
            counts = {k: max(0, min(int(request.args.get(k, "")), 1000000))
                      for k in ("n", "s", "o", "l", "r")}
        except ValueError:
            counts = None
        if counts:
            banner = (
                '<div class="banner"><b>Refresh complete.</b> '
                + summary_text({"fetched": counts["n"], "service": counts["s"],
                                "outside": counts["o"], "local": counts["l"],
                                "review": counts["r"]})
                + feed_notes()
                + "</div>"
            )

    refresh_script = r"""<script>
(function () {
  var form = document.getElementById('refresh-form');
  var status = document.getElementById('refresh-status');
  if (!form || !status) return;
  var button = form.querySelector('button');
  var running = false;
  function show(message) {
    status.hidden = false;
    status.textContent = message;
  }
  async function poll() {
    try {
      var response = await fetch('/api/refresh/status', {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) throw new Error('Status check returned HTTP ' + response.status);
      var state = await response.json();
      if (state.status === 'completed') {
        show(state.summary || 'Search finished. Reloading opportunities…');
        window.location.replace('/?done=0');
        return true;
      }
      if (state.status === 'incomplete') {
        show((state.summary || 'Some results were saved.') + ' Collection INCOMPLETE: ' + (state.error || 'one or more sources may be missing results') + '. You can use the saved results, but do not treat this as a complete search.');
        button.disabled = false;
        button.textContent = '↻ Refresh tenders';
        running = false;
        return true;
      }
      if (state.status === 'failed') {
        show(state.error || 'Refresh failed. Try again; if it repeats, check the service logs.');
        button.disabled = false;
        button.textContent = '↻ Refresh tenders';
        running = false;
        return true;
      }
      show('Searching for tenders… this can take several minutes. Keep this page open.');
      return false;
    } catch (error) {
      show('Could not check refresh status (' + error.message + '). Refresh may still be running; reload this page to check.');
      button.disabled = false;
      button.textContent = '↻ Refresh tenders';
      running = false;
      return true;
    }
  }
  form.addEventListener('submit', async function (event) {
    event.preventDefault();
    if (running) return;
    running = true;
    button.disabled = true;
    button.textContent = 'Searching…';
    show('Starting refresh…');
    try {
      var response = await fetch('/api/refresh', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {'Accept': 'application/json'}
      });
      if (!response.ok && response.status !== 202) {
        throw new Error('Refresh request returned HTTP ' + response.status);
      }
      var result = await response.json();
      if (result.status === 'failed') throw new Error(result.error || 'Could not start refresh');
      show('Searching for tenders… this can take several minutes.');
      var finished = false;
      while (!finished && running) {
        await new Promise(function (resolve) { window.setTimeout(resolve, 3000); });
        finished = await poll();
      }
    } catch (error) {
      show('Could not start refresh (' + error.message + '). Reload the page and try again.');
      button.disabled = false;
      button.textContent = '↻ Refresh tenders';
      running = false;
    }
  });
})();
</script>"""
    page = f"""<!doctype html>
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>Tender Scout</title>
    <style>
.job-kicker{{margin:0 0 .35rem;color:#08783c;font-weight:700;text-transform:uppercase;letter-spacing:.04em;font-size:.82rem}}.board-buyer{{margin:.25rem 0 .7rem;color:#4b5b53;font-weight:600}}.board-facts{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.55rem;margin:.7rem 0}}.board-facts p{{margin:0;padding:.65rem .7rem;background:#f0f6f2;border-radius:.55rem;min-width:0}}.board-facts b{{display:block;font-size:.78rem;color:#40534a}}.board-facts span{{display:block;margin-top:.18rem;font-size:.94rem}}.board-summary{{padding:.8rem .9rem;margin:.75rem 0;background:#f7f8f7;border:1px solid #d7e4dc;border-radius:.65rem;line-height:1.45}}.board-summary h3{{margin:0 0 .4rem;font-size:1rem}}.board-summary p{{margin:.35rem 0}}.full-details{{margin-top:.8rem;border-top:1px solid #d7e4dc;padding-top:.65rem}}.full-details summary{{color:#08783c;font-weight:700;cursor:pointer;padding:.35rem 0}}.plain-english{{padding:.9rem 1rem;background:#f7f8f7;border:1px solid #d7e4dc;border-radius:.65rem;margin:1rem 0}}.plain-english h3{{margin:.1rem 0 .4rem}}.job-facts{{display:grid;gap:.5rem;margin:1rem 0}}.job-facts p{{margin:0;padding:.65rem .75rem;background:#f0f6f2;border-radius:.55rem}}.job-facts b{{display:block;font-size:.88rem;color:#40534a}}.job-facts span{{display:block;margin-top:.18rem}}.job-description{{line-height:1.5}}.contact-box{{padding:.8rem 1rem;background:#f7f8f7;border:1px solid #d7e4dc;border-radius:.65rem;margin:1rem 0}}.contact-box p{{margin:.4rem 0}}.notice-meta,.location-reason{{font-size:.9rem;color:#4b5b53}}.card h2{{line-height:1.18}}.share-actions{{display:flex;flex-wrap:wrap;gap:.5rem}}.share-actions a{{display:inline-block}}

    *{{box-sizing:border-box}}
    html{{width:100%;max-width:100%;overflow-x:hidden}}
    body{{font:16px Arial;background:#f3f6f4;color:#18251e;
    margin:0;padding:20px;width:100%;max-width:900px;overflow-wrap:anywhere}}
    header{{background:#143d2a;color:white;padding:22px;border-radius:12px}}
    .btn{{display:block;width:100%;max-width:340px;margin-top:16px;
    padding:15px 22px;background:linear-gradient(#2fc36f,#168447);color:#fff;
    border:0;border-radius:12px;font:bold 17px Arial;cursor:pointer;
    box-shadow:0 5px 0 #0d5c30,0 9px 14px rgba(0,0,0,.3);
    transition:transform .08s,box-shadow .08s}}
    .btn:hover{{filter:brightness(1.08)}}
    .btn:active{{transform:translateY(4px);
    box-shadow:0 1px 0 #0d5c30,0 2px 6px rgba(0,0,0,.3)}}
    .btn[disabled]{{opacity:.75;cursor:wait}}
    .card{{background:white;margin:16px 0;padding:18px;
    border-radius:10px;overflow-wrap:anywhere}}
    .banner{{background:#e3f4e8;border:1px solid #168447;border-radius:10px;
    padding:12px 14px;margin:14px 0;overflow-wrap:anywhere;word-break:break-word}}
    .deadline{{font-weight:bold;color:#18251e}}
    .deadline.soon{{color:#b45309}}
    .deadline.closed{{color:#8a8f8c}}
    .stage select{{font-size:16px;padding:8px;margin-left:6px}}
    .share-actions{{margin-top:12px;font-weight:bold}}
    .share-actions a{{display:inline-block;padding:9px 11px;background:#e8f2eb;border:1px solid #c8ddce;border-radius:8px;text-decoration:none}}
    a{{color:#08783c}}.note{{font-size:13px;line-height:1.45}}
    .trade-browser{{background:#e8f2eb;border:1px solid #d0e2d5;border-radius:14px;padding:16px;margin:18px 0}}
    .trade-browser h2{{margin:0 0 6px}}
    .trade-group{{background:#fff;border-radius:10px;padding:12px;margin:12px 0}}
    .trade-group h3{{font-size:17px;margin:0 0 10px}}
    .trade-chips{{display:flex;flex-wrap:wrap;gap:8px}}
    .trade-chip{{display:inline-block;background:#f3f8f4;border:1px solid #c8ddce;border-radius:20px;padding:8px 11px;text-decoration:none;font-size:14px}}
    details{{margin-top:12px}}summary{{cursor:pointer;font-weight:bold;padding:8px 0}}
    @media(max-width:520px){{body{{padding:12px}}header{{padding:17px}}.card{{padding:14px}}.trade-browser{{padding:12px}}.board-facts{{grid-template-columns:1fr 1fr;gap:.4rem}}.board-facts p{{padding:.55rem}}.board-facts span{{font-size:.88rem}}}}
    </style></head><body>
    <header><h1>Tender Scout</h1>
    <p><a style="color:white" href="/">Public tenders</a> · <a style="color:white" href="/leads">Private trade leads</a> · <a style="color:white" href="/sources">Council & community sources</a></p>
    <p>Coverage: Liverpool, Sefton, St Helens, Halton, Knowsley, Wirral, Warrington and Cheshire West and Chester. Other North West areas are excluded.</p>
    <form id="refresh-form" method="post" action="/refresh">
    <button type="submit" class="btn">&#8635; Refresh tenders</button></form>
    <p id="refresh-status" class="note" role="status" aria-live="polite" hidden></p></header>
    {banner}
    <p>{heading} · {switch} · <a href="/export.csv">Export CSV</a></p>
    {filter_form}
    <p class="note">Always verify eligibility, location, deadline and contract details
    on the official notice.</p>
    {cards}{refresh_script}</body></html>"""
    return Response(page, mimetype="text/html")


@app.route("/tender/<int:tender_id>/stage", methods=["POST"])
def set_stage(tender_id):
    stage = request.form.get("stage", "")
    if stage not in STAGES:
        return Response("Unknown stage", 400)
    db = connect_db()
    try:
        db.execute("UPDATE tenders SET pipeline_status = ? WHERE id = ?", (stage, tender_id))
        db.commit()
    finally:
        db.close()
    view = "review" if request.form.get("view") == "review" else ""
    service = request.form.get("service", "")
    return redirect(dashboard_url(view, service if service in SERVICE_FILTERS else ""))


def sample_line(record):
    """A one-line description of a tender, for reading in the service logs."""
    def clip(value, size):
        return " ".join(str(value or "").split())[:size]
    return " | ".join([
        clip(record.get("title"), 70),
        "buyer: " + clip(record.get("buyer"), 40),
        "location: " + clip(record.get("location_text"), 140),
        "why: " + clip(record.get("geography_reason"), 90),
    ])


def run_refresh():
    """Fetch official feeds and save matching tenders. Returns a dict of counts."""
    days = env_int("TENDERSCOUT_DAYS", 60)
    pages = env_int("TENDERSCOUT_PAGES", 25)
    # Each source stops after this many seconds, so a slow feed cannot make the
    # request run past the web server's timeout.
    limit = env_int("TENDERSCOUT_SECONDS_PER_SOURCE", 60)
    # Find a Tender rate-limits pagination. A 60-day lookback has ten daily
    # windows plus weekly history; allow enough time to traverse them without
    # falsely reporting incomplete coverage before the source can finish.
    fts_limit = env_int("TENDERSCOUT_FTS_SECONDS", 720)
    fts_stats, cf_stats, bearings_stats, standstill_stats = {}, {}, {}, {}
    # Run independent free sources concurrently. Bearings and Standstill are
    # discovery sources, not delivery-site proof; uncertain records stay REVIEW.
    with ThreadPoolExecutor(max_workers=4) as pool:
        fts_job = pool.submit(collector.fetch_fts, days, pages, fts_limit, fts_stats)
        cf_job = pool.submit(collector.fetch_contracts_finder, days, pages, limit, cf_stats)
        bearings_job = pool.submit(collector.fetch_bearings, limit, bearings_stats)
        standstill_job = pool.submit(collector.fetch_standstill_northwest, limit, standstill_stats)
        records = collector.dedupe(
            fts_job.result() + cf_job.result() + bearings_job.result() + standstill_job.result()
        )
    LAST_FEEDS["Find a Tender"] = fts_stats
    LAST_FEEDS["Contracts Finder"] = cf_stats
    LAST_FEEDS["Bearings (discovery; verify provider notice)"] = bearings_stats
    LAST_FEEDS["Standstill UK RSS discovery"] = standstill_stats
    app.logger.warning("REFRESH feeds: %s", LAST_FEEDS)
    samples = {"review": [], "outside": []}
    stats = {"fetched": len(records), "service": 0, "outside": 0, "local": 0, "review": 0}
    if not records:
        return stats

    db = connect_db()
    try:
        for record in records:
            match = collector.score_record(
                record, SERVICES, collector.TARGET_AREA_LABEL, None, None
            )
            # score_record sets region_match only once a tender has passed the
            # cleaning/FM check, so this tells the two kinds of drop apart.
            if "region_match" in record:
                stats["service"] += 1
                if record["region_match"] == "OUT_OF_AREA":
                    stats["outside"] += 1
                    if len(samples["outside"]) < 20:
                        samples["outside"].append(sample_line(record))
                elif record["region_match"] == "REVIEW" and match and len(samples["review"]) < 40:
                    samples["review"].append(sample_line(record))
            # Keep relevant but incomplete notices in REVIEW even when a missing deadline
            # lowers their score. The score threshold applies to confirmed LOCAL matches;
            # otherwise useful leads with uncertain locations/deadlines disappear silently.
            if not match:
                continue
            if match.get("region_match") == "LOCAL" and match.get("score", 0) < MIN_SCORE:
                continue
            region_match = match.get("region_match")
            if region_match not in ("LOCAL", "REVIEW"):
                continue

            url = match.get("notice_url") or (
                "no-url:"
                + str(match.get("source", ""))
                + ":"
                + str(match.get("ocid") or match.get("title", ""))
                + ":"
                + str(match.get("published_date", ""))
            )

            # pipeline_status is deliberately not updated on conflict, so a stage
            # you set is never reset by a refresh.
            db.execute(
                """
                INSERT INTO tenders
                (source, title, buyer, published_date, location_text, description,
                 estimated_value_gbp, contract_start_date, contract_end_date,
                 contact_name, contact_email, contact_phone, eligibility_text,
                 service_match, score, deadline, notice_url,
                 geography_reason, region_match, trade_category, saved_at, pipeline_status, opportunity_stage)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(notice_url) DO UPDATE SET
                    source=excluded.source, title=excluded.title, buyer=excluded.buyer,
                    published_date=excluded.published_date,
                    service_match=excluded.service_match,
                    score=excluded.score, deadline=excluded.deadline,
                    location_text=excluded.location_text, description=excluded.description,
                    estimated_value_gbp=excluded.estimated_value_gbp,
                    contract_start_date=excluded.contract_start_date,
                    contract_end_date=excluded.contract_end_date,
                    contact_name=excluded.contact_name, contact_email=excluded.contact_email,
                    contact_phone=excluded.contact_phone, eligibility_text=excluded.eligibility_text,
                    geography_reason=excluded.geography_reason,
                    region_match=excluded.region_match,
                    trade_category=excluded.trade_category,
                    opportunity_stage=excluded.opportunity_stage,
                    saved_at=excluded.saved_at
                """,
                (
                    match.get("source", ""),
                    match.get("title", ""),
                    match.get("buyer", ""),
                    match.get("published_date", ""),
                    match.get("location_text", ""),
                    match.get("description", ""),
                    match.get("estimated_value_gbp", ""),
                    match.get("contract_start_date", ""),
                    match.get("contract_end_date", ""),
                    match.get("contact_name", ""),
                    match.get("contact_email", ""),
                    match.get("contact_phone", ""),
                    match.get("eligibility_text", ""),
                    match.get("service_match", ""),
                    match.get("score", 0),
                    match.get("deadline", ""),
                    url,
                    match.get("geography_reason", ""),
                    region_match,
                    match.get("trade_category", "Other / unclassified"),
                    datetime.now(timezone.utc).isoformat(),
                    "Reviewing",
                    match.get("opportunity_stage", "Open for bids"),
                ),
            )
            stats["local" if region_match == "LOCAL" else "review"] += 1
        db.commit()
    finally:
        db.close()
    for kind, lines in samples.items():
        for line in lines:
            app.logger.warning("REFRESH %s: %s", kind, line)
    return stats


def refresh_or_error():
    """Run a refresh. Returns (stats, None) or (None, error_response)."""
    try:
        stats = run_refresh()
    except Exception:
        app.logger.exception("Tender refresh failed")
        return None, Response(
            "Refresh failed. Check the service logs."
            '<p><a href="/">Return to dashboard</a></p>',
            status=500,
            mimetype="text/html",
        )
    if stats["fetched"] == 0:
        # A failed or empty feed must never be mistaken for "no tenders".
        return None, Response(
            "No records came back from the official feeds. A source may be down, "
            "so this does not mean there are no tenders."
            '<p><a href="/">Return to dashboard</a></p>',
            status=502,
            mimetype="text/html",
        )
    return stats, None


def feed_notes():
    """One short line per feed: what came back, and any sign it was cut short."""
    lines = []
    for name, st in LAST_FEEDS.items():
        if not st:
            continue
        text = f"{name}: {st.get('records', 0)} notices in {st.get('pages', 0)} page(s)"
        if st.get("windows"):
            text += f" over {st['windows']} week(s) searched"
        if st.get("first_date"):
            text += f", published {st['first_date']} to {st['last_date']}"
        if st.get("feed_counts"):
            feed_detail = ", ".join(f"{label}: {count}" for label, count in st["feed_counts"].items())
            text += f". Feed breakdown — {feed_detail}"
        if st.get("error"):
            text += f". Problem: {st['error']}"
        elif st.get("partial_errors"):
            text += f". Some feeds failed: {st['partial_errors']}"
        elif st.get("stopped"):
            text += ". Stopped early (time limit), so there may be more"
        elif st.get("more"):
            text += ". More pages were available but not read"
        elif st.get("full_end") or st.get("last_batch", 0) >= 100:
            text += ". The last page was full but no next-page link came back, so results may be cut off"
        lines.append(safe(text))
    if not lines:
        return ""
    return '<div class="note">' + "<br>".join(lines) + "</div>"


def summary_text(stats):
    return (
        f"{stats['fetched']} notices checked. {stats['service']} matched your trades; "
        f"{stats['outside']} of those were outside your area. "
        f"{stats['local']} confirmed in your area, {stats['review']} need a location check."
    )


@app.route("/refresh", methods=["POST"])
def refresh():
    # The non-JavaScript form path must acquire the same cross-process lease.
    token, result, status_code = _claim_refresh_lease()
    if token is None:
        if status_code >= 500:
            return Response(result.get("error", "Could not start refresh."), status=status_code)
        return redirect("/?refresh_started=1")
    stats, error = _run_background_refresh(token)
    if error:
        return error
    query = urlencode({"done": 1, "n": stats["fetched"], "s": stats["service"],
                       "o": stats["outside"], "l": stats["local"], "r": stats["review"]})
    return redirect("/?" + query)


def refresh_feed_issues():
    """Return feeds with explicit signs that collection may be incomplete."""
    issues = []
    for name, feed in LAST_FEEDS.items():
        if not isinstance(feed, dict):
            continue
        reasons = []
        if feed.get("error"):
            reasons.append("feed error")
        if feed.get("partial_errors"):
            reasons.append("partial feed errors")
        if feed.get("stopped"):
            reasons.append("stopped early")
        if feed.get("more"):
            reasons.append("more pages available")
        if feed.get("truncated_windows"):
            reasons.append("one or more search windows truncated")
        if feed.get("full_end") or feed.get("last_batch", 0) >= 100:
            reasons.append("possible page truncation")
        if reasons:
            issues.append(f"{name}: {', '.join(reasons)}")
    return issues


def _set_refresh_state(status, summary="", error="", refresh_token=None):
    """Update refresh state only while this refresh still owns the database lease."""
    db = connect_db()
    now = datetime.now(timezone.utc).isoformat()
    try:
        cursor = db.execute(
            """UPDATE refresh_state
               SET status = ?, finished_at = ?, summary = ?, error = ?, heartbeat_at = NULL
               WHERE id = 1
                 AND (refresh_token = ? OR (refresh_token IS NULL AND ? IS NULL))""",
            (status, now if status in ("completed", "incomplete", "failed") else None,
             summary, error, refresh_token, refresh_token),
        )
        db.commit()
        return cursor.rowcount == 1
    finally:
        db.close()


def _refresh_heartbeat(refresh_token, stop_event):
    """Renew the lease every 30 seconds while the owning collector is active."""
    while not stop_event.wait(30):
        db = None
        try:
            db = connect_db()
            now = datetime.now(timezone.utc).isoformat()
            db.execute(
                """UPDATE refresh_state SET heartbeat_at = ?
                   WHERE id = 1 AND status = 'running' AND refresh_token = ?""",
                (now, refresh_token),
            )
            db.commit()
        except Exception:
            app.logger.exception("Could not renew tender refresh lease")
        finally:
            if db is not None:
                db.close()


def _run_background_refresh(refresh_token=None):
    stop_event = threading.Event()
    heartbeat = None
    if refresh_token is not None:
        heartbeat = threading.Thread(target=_refresh_heartbeat, args=(refresh_token, stop_event),
                                     daemon=True, name="tenderscout-refresh-heartbeat")
        heartbeat.start()
    try:
        stats, error = refresh_or_error()
        if error:
            _set_refresh_state("failed", error=error.get_data(as_text=True)[:1000], refresh_token=refresh_token)
            return None, error
        summary = summary_text(stats)
        issues = refresh_feed_issues()
        if issues:
            detail = "; ".join(issues)
            _set_refresh_state("incomplete", summary=summary + " Some source results may be missing.",
                               error="Partial collection: " + detail[:900], refresh_token=refresh_token)
            app.logger.warning("Refresh saved partial results: %s", detail)
        else:
            _set_refresh_state("completed", summary=summary, refresh_token=refresh_token)
        return stats, None
    except Exception:
        app.logger.exception("Background tender refresh failed")
        try:
            _set_refresh_state("failed", error="Refresh failed. Check the service logs.", refresh_token=refresh_token)
        except Exception:
            app.logger.exception("Could not save failed refresh status")
        return None, Response("Refresh failed. Check the service logs.", status=500)
    finally:
        stop_event.set()
        if heartbeat is not None:
            heartbeat.join(timeout=2)


def _start_refresh_thread(refresh_token):
    threading.Thread(
        target=_run_background_refresh,
        args=(refresh_token,),
        daemon=True,
        name="tenderscout-refresh",
    ).start()


def _claim_refresh_lease():
    """Atomically claim the shared database lease for any refresh entry point."""
    started = datetime.now(timezone.utc).isoformat()
    token = uuid.uuid4().hex
    db = None
    try:
        db = connect_db()
        row = db.execute(
            "SELECT status, started_at, refresh_token, heartbeat_at FROM refresh_state WHERE id = 1"
        ).fetchone()
        current = dict(row) if row else {}
        if current.get("status") == "running" and not _refresh_state_is_stale(current):
            return None, {"status": "running", "message": "A refresh is already running."}, 202
        cursor = db.execute(
            """UPDATE refresh_state SET status = 'running', started_at = ?, heartbeat_at = ?,
                   finished_at = NULL, summary = '', error = '', refresh_token = ?
               WHERE id = 1 AND status = ?
                 AND (started_at = ? OR (started_at IS NULL AND ? IS NULL))
                 AND (refresh_token = ? OR (refresh_token IS NULL AND ? IS NULL))""",
            (started, started, token, current.get("status"), current.get("started_at"),
             current.get("started_at"), current.get("refresh_token"), current.get("refresh_token")),
        )
        db.commit()
        if cursor.rowcount != 1:
            return None, {"status": "running", "message": "A refresh is already running."}, 202
        return token, {"status": "running", "started_at": started}, 202
    except Exception:
        app.logger.exception("Could not claim tender refresh lease")
        return None, {"status": "failed", "error": "Could not claim the refresh lease."}, 500
    finally:
        if db is not None:
            db.close()


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Claim the shared database lease, then start one background refresh."""
    token, result, status_code = _claim_refresh_lease()
    if token is None:
        return result, status_code
    try:
        _start_refresh_thread(token)
    except Exception:
        _set_refresh_state("failed", error="Could not start the refresh thread.", refresh_token=token)
        app.logger.exception("Could not start background tender refresh")
        return {"status": "failed", "error": "Could not start the refresh thread."}, 500
    return result, status_code


def _refresh_state_is_stale(row, now=None):
    """Return True when a persisted running refresh is old enough to be presumed interrupted."""
    if not row or row["status"] != "running":
        return False
    heartbeat_at = row.get("heartbeat_at") if hasattr(row, "get") else None
    started_at = heartbeat_at or row["started_at"]
    if not started_at:
        return True
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    now = now or datetime.now(timezone.utc)
    return (now - started).total_seconds() >= REFRESH_STALE_AFTER_SECONDS


@app.route("/api/refresh/status", methods=["GET"])
def api_refresh_status():
    """Read refresh state and recover stale 'running' status after a process restart."""
    db = connect_db()
    try:
        row = db.execute(
            "SELECT status, started_at, finished_at, summary, error, refresh_token, heartbeat_at FROM refresh_state WHERE id = 1"
        ).fetchone()
        state = dict(row) if row else {}
    finally:
        db.close()

    if _refresh_state_is_stale(state):
        _set_refresh_state(
            "failed",
            error="Refresh appears to have been interrupted or exceeded the 15-minute safety limit. Start a new refresh.",
            refresh_token=state.get("refresh_token"),
        )
        db = connect_db()
        try:
            row = db.execute(
                "SELECT status, started_at, finished_at, summary, error, refresh_token FROM refresh_state WHERE id = 1"
            ).fetchone()
            state = dict(row) if row else {}
        finally:
            db.close()
    state.pop("refresh_token", None)
    return state


@app.route("/add", methods=["POST"])
def add_tender():
    title = (request.form.get("title") or "").strip() or "Untitled tender"
    buyer = (request.form.get("buyer") or "").strip()
    pipeline_status = (request.form.get("pipeline_status") or "").strip() or "Reviewing"
    opportunity_stage = (request.form.get("opportunity_stage") or "").strip() or "Open for bids"
    if opportunity_stage not in ("Planning / upcoming", "Open for bids", "Other notice"):
        opportunity_stage = "Open for bids"
    # Manual entries without a link get their own unique key, so they never
    # overwrite each other.
    notice_url = (request.form.get("notice_url") or "").strip() or "manual:" + uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()

    db = connect_db()
    try:
        db.execute(
            """
            INSERT INTO tenders
            (source, title, buyer, published_date, location_text,
             service_match, score, deadline, notice_url,
             geography_reason, region_match, saved_at, pipeline_status, opportunity_stage)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(notice_url) DO UPDATE SET
                title=excluded.title,
                buyer=excluded.buyer,
                geography_reason=excluded.geography_reason,
                pipeline_status=excluded.pipeline_status,
                opportunity_stage=excluded.opportunity_stage,
                saved_at=excluded.saved_at
            """,
            (
                "Manual", title, buyer, now, "", "", 0, "", notice_url,
                "Manual entry", "MANUAL", now, pipeline_status, opportunity_stage,
            ),
        )
        db.commit()
    finally:
        db.close()
    return redirect("/")


# Include every tender column in the recovery export, not just the card summary.
# Keep the column order explicit so CSVs are stable and easy to inspect.
EXPORT_FIELDS = [
    "id", "source", "title", "buyer", "published_date", "location_text",
    "description", "estimated_value_gbp", "contract_start_date", "contract_end_date",
    "contact_name", "contact_email", "contact_phone", "eligibility_text",
    "service_match", "score", "deadline", "notice_url", "geography_reason",
    "region_match", "trade_category", "saved_at", "pipeline_status",
    "opportunity_stage",
]


@app.route("/backup.json")
def export_database_backup():
    """Download a portable snapshot of every TenderScout data table."""
    tables = ("tenders", "private_leads", "external_projects")
    db = connect_db()
    try:
        payload = {
            "format": "tenderscout-database-backup",
            "format_version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "tables": {},
        }
        for table in tables:
            rows = db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            payload["tables"][table] = [dict(row) for row in rows]
    finally:
        db.close()

    return Response(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        mimetype="application/json",
        headers={"Content-Disposition": 'attachment; filename="tenderscout_database_backup.json"',
                 "Cache-Control": "no-store"},
    )


@app.route("/export.csv")
def export_csv():
    db = connect_db()
    rows = db.execute(
        f"SELECT {', '.join(EXPORT_FIELDS)} FROM tenders ORDER BY saved_at DESC"
    ).fetchall()
    db.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(EXPORT_FIELDS)
    for row in rows:
        writer.writerow([csv_safe(row[field]) for field in EXPORT_FIELDS])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=tenderscout_export.csv"},
    )


@app.route("/leads/export.csv")
def export_private_leads_csv():
    """Export the private lead pipeline so records can be backed up or reused."""
    # Include the database ID as well as all private-lead fields for recovery.
    fields = [
        "id", "title", "trade_category", "location_text", "source_name", "source_url",
        "contact_details", "details", "urgency", "status", "created_at",
    ]
    db = connect_db()
    try:
        rows = db.execute(
            f"SELECT {', '.join(fields)} FROM private_leads ORDER BY created_at DESC"
        ).fetchall()
    finally:
        db.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(fields)
    for row in rows:
        writer.writerow([csv_safe(row[field]) for field in fields])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=tenderscout_private_leads.csv",
                 "Cache-Control": "no-store"},
    )


@app.route("/projects/export.csv")
def export_external_projects_csv():
    """Export public external-project summaries for backup; these are not private leads."""
    fields = [
        "id", "title", "trade_category", "location_text", "details", "source_name",
        "source_url", "published_date", "location_status", "imported_at",
    ]
    db = connect_db()
    try:
        rows = db.execute(
            f"SELECT {', '.join(fields)} FROM external_projects ORDER BY imported_at DESC"
        ).fetchall()
    finally:
        db.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(fields)
    for row in rows:
        writer.writerow([csv_safe(row[field]) for field in fields])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=tenderscout_external_projects.csv",
                 "Cache-Control": "no-store"},
    )



TRADE_CATEGORIES = list(collector.TRADE_CATEGORIES)
LEAD_STATUSES = ["New", "Contacted", "Qualified", "Passed to trade", "Won", "Closed"]
LEAD_URGENCIES = ["Urgent — today", "Urgent — within 48 hours", "This week", "Flexible / no rush"]
LEAD_SOURCES = [
    "Facebook community group", "Nextdoor / neighbourhood group", "Local business or letting agent",
    "Direct customer enquiry", "Council / procurement portal", "Trade directory", "Other"
]


@app.route("/leads/<int:lead_id>/status", methods=["POST"])
def update_private_lead_status(lead_id):
    status = (request.form.get("status") or "").strip()
    if status not in LEAD_STATUSES:
        return Response("Unknown lead status", 400)
    db = connect_db()
    try:
        db.execute("UPDATE private_leads SET status = ? WHERE id = ?", (status, lead_id))
        db.commit()
    finally:
        db.close()
    return redirect("/leads")


@app.route("/sources")
def sources():
    """Directory of official procurement portals and public community channels."""
    items = [
        ("Public-sector tenders", [
            ("Find a Tender", "https://www.find-tender.service.gov.uk/Search", "UK government procurement notices"),
            ("Contracts Finder", "https://www.contractsfinder.service.gov.uk/Search", "UK public-sector opportunities"),
            ("Liverpool City Council — ProContract", "https://procontract.due-north.com/", "Liverpool Council says most of its opportunities are advertised through ProContract; some routes use other approved portals"),
            ("The Chest", "https://www.the-chest.org.uk/", "North West local-authority procurement portal"),
            ("Bearings — Liverpool City Region tenders", "https://getbearings.co.uk/rss", "Free North West opportunity aggregator with RSS feeds and an open API; use as a discovery source and verify every tender on the buyer's official notice"),
            ("Standstill UK — North West tenders", "https://standstill.uk/region/north-west/", "Free daily RSS discovery feed now included in TenderScout; regional label is not proof of the delivery site, and every item must be verified on the official notice"),
            ("Merseyside Fire & Rescue procurement", "https://www.merseyfire.gov.uk/about/supplying-goods-and-services-to-mfra/", "Confirms openly advertised opportunities are on The Chest and national notices on Find a Tender / Contracts Finder"),
            ("Sefton Council tenders and contracts", "https://www.sefton.gov.uk/business/tenders-contracts", "Confirms Sefton opportunities use The Chest and points suppliers to national notice sources"),
        ]),
        ("Community and private-lead channels", [
            ("HomeGuild — Liverpool homeowner jobs", "https://www.homeguild.co.uk/jobs/", "Public job-board page; current jobs may be empty and contact details are reserved for registered tradespeople"),
            ("BuildPair — latest job requests", "https://www.buildpair.co.uk/jobs", "Public job-board page; review the source's terms before any automated reuse"),
            ("Formby Bubble Approved Trader List", "https://www.formbybubble.com/bubbleapprovedtraderlist", "Local trader directory; use as a contact/discovery source, not proof of a live job"),
            ("Grafters Network Liverpool", "https://www.grafterssupergroups.co.uk/events/liverpoolflagship/", "Local business networking"),
            ("Liverpool Business Forum", "https://liverpoolbusinessforum.co.uk/about-lbf/", "Local business contacts and networking"),
            ("Facebook — Liverpool community groups", "https://www.facebook.com/search/groups/?q=Liverpool%20community", "Open group search; check each group's rules and use permitted/manual submissions"),
            ("Facebook — Formby community groups", "https://www.facebook.com/search/groups/?q=Formby%20community", "Open group search; check each group's rules and use permitted/manual submissions"),
            ("Nextdoor", "https://nextdoor.co.uk/", "Neighbourhood recommendations and requests; use authorised access only"),
        ]),
    ]
    sections = ""
    for heading, entries in items:
        cards = "".join(
            f'<li><a href="{safe(url)}" target="_blank" rel="noopener">{safe(name)}</a> — {safe(note)}</li>'
            for name, url, note in entries
        )
        sections += f"<h2>{safe(heading)}</h2><ul>{cards}</ul>"
    return Response(
        '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Tender Scout sources</title><style>'
        'body{font:16px Arial;max-width:960px;margin:0 auto;padding:16px;color:#18251e;background:#f3f6f4;line-height:1.5}'
        'header{background:#143d2a;color:white;border-radius:14px;padding:20px;margin-bottom:16px}'
        'header a{color:white}main{background:white;border-radius:14px;padding:18px}'
        'h2{font-size:21px;margin-top:22px}ul{list-style:none;padding:0}'
        'li{margin:10px 0;padding:14px;background:#f5f8f6;border:1px solid #e0e8e2;border-radius:10px}'
        'a{color:#08783c;font-weight:600}p{line-height:1.5}.note{font-size:14px;color:#53645a}'
        '@media(max-width:520px){body{padding:10px}header,main{padding:14px}}</style></head><body>'
        '<header><nav><a href="/">Public tenders</a> · <a href="/leads">Private leads</a> · <a href="/sources">Sources</a></nav>'
        '<h1>Council & community sources</h1><p>Use this directory to find official tender notices and permitted customer enquiry sources.</p></header><main>'
        '<p class="note">Public contracts and private customer leads are different opportunity types. Council portals may require supplier registration and may not expose an open feed.</p>'
        + sections +
        '<p><b>Important:</b> Tender Scout does not automatically scrape Facebook, Nextdoor or gated supplier portals. '
        'Add a private lead only when you have permission to use the information and a legitimate source link.</p>'
        '<p><a href="/">Back to public tenders</a> · <a href="/leads">Open private leads</a></p></main></body></html>',
        mimetype="text/html",
    )


# BuildConnect publishes public project summaries via RSS. Contact details remain private.
BUILDCONNECT_FEED_URL = "https://www.buildconnect-uk.co.uk/feed.xml"
BUILDCONNECT_CACHE_SECONDS = 1800
_LAST_BUILDCONNECT_CHECK = 0.0
_LAST_BUILDCONNECT_RESULT = {"checked": False, "imported": 0, "error": ""}


def _plain_feed_text(value):
    """Turn an RSS title/summary into safe plain text before saving or displaying it."""
    value = html.unescape(str(value or ""))
    value = re.sub(r"(?is)<(script|style)\b.*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return " ".join(value.split())[:12000]


def _project_location_check(text):
    """Only accept clear target-area mentions; never treat a broad region as proof."""
    lower = " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip() + " "
    local_hits = [term for term in collector.LOCAL_PLACE_TERMS if (" " + term + " ") in lower]
    # Exact postcode district checks can identify target locations, including Wirral, even if no town is named.
    try:
        full, bare = collector.postcode_districts(text or "", include_bare=True)
        postcode_hits = sorted((full | bare) & collector.LOCAL_POSTCODE_DISTRICTS)
    except Exception:
        postcode_hits = []
    if not local_hits and not postcode_hits:
        return False, "Location not confirmed in the six target areas"
    excluded_terms = [
        "warrington", "wigan", "west lancashire",
        "ormskirk", "skelmersdale", "chester", "ellesmere port", "manchester",
        "salford", "bolton", "preston", "blackpool", "north west", "cheshire west",
    ]
    out_hits = [term for term in excluded_terms if (" " + term + " ") in lower]
    if out_hits:
        return False, "Mixed or outside-area location needs checking: " + ", ".join(out_hits[:3])
    return True, "Potential local — verify exact project location"


def refresh_buildconnect_projects(force=False):
    """Import public BuildConnect project summaries only; never infer private contacts."""
    global _LAST_BUILDCONNECT_CHECK, _LAST_BUILDCONNECT_RESULT
    import time
    now = time.time()
    if not force and now - _LAST_BUILDCONNECT_CHECK < BUILDCONNECT_CACHE_SECONDS:
        return dict(_LAST_BUILDCONNECT_RESULT)
    result = {"checked": False, "imported": 0, "feed_items": 0, "target_matches": 0, "error": ""}
    try:
        request_obj = Request(
            BUILDCONNECT_FEED_URL,
            headers={"User-Agent": "TenderScout/1.0 (public RSS reader; contact: site administrator)",
                     "Accept": "application/rss+xml, application/xml, text/xml"},
        )
        with urlopen(request_obj, timeout=8) as response:
            raw = response.read(2_000_000)
        root = ET.fromstring(raw)
        items = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() in ("item", "entry")]
        result["feed_items"] = len(items)
        db = connect_db()
        imported = 0
        target_matches = 0
        try:
            for item in items[:100]:
                fields = {}
                for child in list(item):
                    key = child.tag.rsplit("}", 1)[-1].lower()
                    # Atom feeds store the URL on <link href="...">, not as text.
                    child_value = child.text or (child.attrib.get("href", "") if key == "link" else "")
                    fields.setdefault(key, child_value)
                title = _plain_feed_text(fields.get("title"))
                link = _plain_feed_text(fields.get("link"))
                if not link and fields.get("guid", "").startswith("http"):
                    link = fields.get("guid", "")
                parsed = urlparse(link)
                if not title or parsed.scheme != "https" or parsed.hostname not in ("buildconnect-uk.co.uk", "www.buildconnect-uk.co.uk"):
                    continue
                description = _plain_feed_text(fields.get("description") or fields.get("summary") or fields.get("content"))
                combined = title + " " + description
                is_target, location_status = _project_location_check(combined)
                if not is_target:
                    continue
                target_matches += 1
                trade = collector.classify_trade_category(combined)
                cursor = db.execute(
                    "INSERT INTO external_projects (title,trade_category,location_text,details,source_name,source_url,published_date,location_status,imported_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(source_url) DO NOTHING",
                    (title[:250], trade, "Target-area clue found in public feed — verify exact site",
                     description[:4000], "BuildConnect UK (public RSS)", link,
                     _plain_feed_text(fields.get("pubdate") or fields.get("published") or "")[:100],
                     location_status, datetime.now(timezone.utc).isoformat()),
                )
                # SQLite/PostgreSQL both expose rowcount for INSERT ... DO NOTHING.
                if getattr(cursor, "rowcount", 0) == 1:
                    imported += 1
            db.commit()
        finally:
            db.close()
        result = {"checked": True, "imported": imported, "feed_items": len(items), "target_matches": target_matches, "error": ""}
    except Exception as exc:
        # Keep the page usable if the public feed is temporarily unavailable.
        result = {"checked": False, "imported": 0, "feed_items": result.get("feed_items", 0), "target_matches": result.get("target_matches", 0), "error": str(exc)[:220]}
    _LAST_BUILDCONNECT_CHECK = now
    _LAST_BUILDCONNECT_RESULT = dict(result)
    return result


@app.route("/leads/import-buildconnect", methods=["POST"])
def import_buildconnect_projects():
    result = refresh_buildconnect_projects(force=True)
    return redirect("/leads?projects_checked=1&projects_imported=" + str(result.get("imported", 0))
                    + "&projects_items=" + str(result.get("feed_items", 0))
                    + "&projects_matches=" + str(result.get("target_matches", 0))
                    + ("&projects_error=" + urlencode({"detail": result.get("error", "Feed unavailable")})[7:] if result.get("error") else ""))


@app.route("/leads", methods=["GET", "POST"])
def private_leads():
    """Separate, manually captured private customer enquiries; never mixed with tenders."""
    db = connect_db()
    message = ""
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        trade = (request.form.get("trade_category") or "").strip()
        location = (request.form.get("location_text") or "").strip()
        source_name = (request.form.get("source_name") or "").strip()
        source_url = (request.form.get("source_url") or "").strip()
        contact = (request.form.get("contact_details") or "").strip()
        details = (request.form.get("details") or "").strip()
        urgency = (request.form.get("urgency") or "Flexible / no rush").strip()
        if urgency not in LEAD_URGENCIES:
            urgency = "Flexible / no rush"
        if trade not in TRADE_CATEGORIES:
            message = '<p class="error">Please choose a valid trade category.</p>'
        elif not title or not location or not details:
            message = '<p class="error">Please enter the job title, exact location and what the customer actually wants. Source type is optional.</p>'
        elif source_name and source_name not in LEAD_SOURCES:
            message = '<p class="error">Please choose a valid source type or leave it blank.</p>'
        elif source_url and not source_url.startswith(("https://", "http://")):
            message = '<p style="color:#a11">Source link must start with http:// or https://.</p>'
        else:
            db.execute(
                "INSERT INTO private_leads (title,trade_category,location_text,source_name,source_url,contact_details,details,urgency,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (title, trade, location, source_name, source_url, contact, details, urgency, "New",
                 datetime.now(timezone.utc).isoformat()),
            )
            db.commit()
            message = '<p style="color:#08783c"><b>Private lead saved.</b></p>'
    lead_trade = request.args.get("trade", "")
    lead_trade = lead_trade if lead_trade in TRADE_CATEGORIES else ""
    lead_status = request.args.get("status", "")
    lead_status = lead_status if lead_status in LEAD_STATUSES else ""
    where = []
    query_params = []
    if lead_trade:
        where.append("trade_category = ?")
        query_params.append(lead_trade)
    if lead_status:
        where.append("status = ?")
        query_params.append(lead_status)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    rows = db.execute(
        "SELECT * FROM private_leads" + where_sql + " ORDER BY created_at DESC LIMIT 100",
        tuple(query_params),
    ).fetchall()
    lead_counts = {
        "total": db.execute("SELECT COUNT(*) AS n FROM private_leads").fetchone()["n"],
        "new": db.execute("SELECT COUNT(*) AS n FROM private_leads WHERE status = 'New'").fetchone()["n"],
        "passed": db.execute("SELECT COUNT(*) AS n FROM private_leads WHERE status = 'Passed to trade'").fetchone()["n"],
    }
    project_rows = db.execute("SELECT * FROM external_projects ORDER BY imported_at DESC LIMIT 100").fetchall()
    db.close()
    options = "".join(f'<option value="{safe(t)}">{safe(t)}</option>' for t in TRADE_CATEGORIES)
    source_options = "".join(f'<option value="{safe(s)}">{safe(s)}</option>' for s in LEAD_SOURCES)
    cards = ""
    for row in rows:
        link = (f'<a href="{safe(row["source_url"])}" target="_blank" rel="noopener">Open original source</a>'
                if row["source_url"] and row["source_url"].startswith(("https://", "http://")) else "")
        share_message = "\n".join([
            "JOB: " + str(row["title"] or ""),
            "TRADE: " + str(row["trade_category"] or ""),
            "LOCATION: " + str(row["location_text"] or ""),
            "WHAT THEY WANT: " + str(row["details"] or "Confirm job scope with the customer"),
            "CONTACT: " + str(row["contact_details"] or "Not supplied — check original source"),
            "SOURCE: " + str(row["source_url"] or row["source_name"] or "Not recorded"),
        ])
        whatsapp_url = "https://wa.me/?" + urlencode({"text": share_message})
        email_url = "mailto:?" + urlencode({"subject": "Trade lead: " + str(row["title"] or ""), "body": share_message})
        status_options = "".join(
            f'<option value="{safe(st)}"{" selected" if st == row["status"] else ""}>{safe(st)}</option>'
            for st in LEAD_STATUSES
        )
        created_label = display_date(row["created_at"])
        urgency_class = "urgent" if str(row["urgency"] or "").startswith("Urgent") else ""
        cards += (
            f'<article class="lead-card {urgency_class}"><p class="lead-kicker">{safe(row["trade_category"])} · {safe(row["urgency"] or "Flexible / no rush")}</p>'
            f'<h3>{safe(row["title"])}</h3><p class="lead-location"><b>WHERE:</b> {safe(row["location_text"])}</p>'
            f'<div class="lead-facts"><p><b>Status</b><span>{safe(row["status"] or "New")}</span></p><p><b>Added</b><span>{safe(created_label or "Date not available")}</span></p></div>'
            f'<p><b>Original source:</b> {safe(row["source_name"] or "Not recorded")} {link}</p>'
            f'<p><b>Customer / business contact:</b> {safe(row["contact_details"]) or "Not supplied — check the original post or contact the source"}</p>'
            f'<section class="lead-request"><h4>WHAT THE CUSTOMER NEEDS</h4><p>{safe(row["details"]) or "Not recorded yet — check the original post before passing this lead on."}</p></section>'
            f'<form method="post" action="/leads/{int(row["id"])}/status" class="status-form"><label>Lead progress<select name="status">{status_options}</select></label><button type="submit">Save status</button></form>'
            f'<p class="share-actions"><a href="{safe(whatsapp_url)}" target="_blank" rel="noopener">Send lead by WhatsApp</a> <a href="{safe(email_url)}">Send lead by email</a></p></article>'
        )
    if not cards:
        cards = "<p>No private leads have been entered yet. This list is separate from public-sector tenders.</p>"
    project_cards = ""
    for project in project_rows:
        share_text = "\n".join([
            "PROJECT OPPORTUNITY (location must be verified)",
            "JOB: " + str(project["title"] or ""),
            "TRADE: " + str(project["trade_category"] or ""),
            "LOCATION: " + str(project["location_text"] or ""),
            "DETAILS: " + str(project["details"] or "Open the source link for details"),
            "CONTACT: Not provided by source; request an approved introduction",
            "SOURCE: " + str(project["source_url"] or ""),
        ])
        wa = "https://wa.me/?" + urlencode({"text": share_text})
        em = "mailto:?" + urlencode({"subject": "Construction opportunity: " + str(project["title"] or ""), "body": share_text})
        project_cards += (
            f'<article class="lead-card project-card"><p class="lead-kicker">PUBLIC PROJECT · {safe(project["trade_category"])} · LOCATION TO VERIFY</p>'
            f'<h3>{safe(project["title"])}</h3><p class="lead-location"><b>WHERE:</b> {safe(project["location_text"])}</p>'
            f'<section class="lead-request"><h4>PROJECT DETAILS</h4><p>{safe(project["details"] or "Open the original source for project details.")}</p></section>'
            f'<p><b>Source:</b> BuildConnect UK · <a href="{safe(project["source_url"])}" target="_blank" rel="noopener">Open original project</a></p>'
            '<p><b>Contact:</b> Not supplied by the source. The project owner controls introductions.</p>'
            f'<p class="share-actions"><a href="{safe(wa)}" target="_blank" rel="noopener">Send by WhatsApp</a> <a href="{safe(em)}">Send by email</a></p></article>'
        )
    project_status = ""
    if request.args.get("projects_checked") == "1":
        if request.args.get("projects_error"):
            project_status = '<p class="source-warning">BuildConnect check failed: ' + safe(request.args.get("projects_error")) + '. Feed entries read: ' + safe(request.args.get("projects_items", "0")) + '. Check the source/feed response before treating this as zero available jobs.</p>'
        else:
            project_status = '<p class="source-warning">BuildConnect check completed. Feed entries read: ' + safe(request.args.get("projects_items", "0")) + '; entries matching target-area clues: ' + safe(request.args.get("projects_matches", "0")) + '; newly saved: ' + safe(request.args.get("projects_imported", "0")) + '. Zero new saves can mean no matches or that matching projects were already saved. Verify every location on the original page.</p>'
    page = (
        '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Tender Scout — private trade leads</title><style>'
        'body{font:16px Arial;max-width:960px;margin:0 auto;padding:16px;color:#18251e;background:#f3f6f4;line-height:1.45}'
        'header{background:#143d2a;color:white;border-radius:14px;padding:20px;margin-bottom:16px}'
        'header a{color:white}nav{font-size:14px}main{background:white;border-radius:14px;padding:18px}'
        'label{display:block;margin:14px 0;font-weight:600}input,select,textarea{box-sizing:border-box;width:100%;padding:12px;border:1px solid #b8c6bd;border-radius:8px;font:inherit;margin-top:6px;background:white}'
        'button{padding:13px 18px;background:#168447;color:white;border:0;border-radius:9px;font-weight:bold;font-size:16px;width:100%;margin-top:8px}'
        'article{background:#f3f6f4;border:1px solid #e0e8e2;border-radius:10px;padding:14px;margin:12px 0;overflow-wrap:anywhere}'
        '.lead-kicker{font-weight:bold;color:#08783c}.lead-location{font-size:18px;background:#e8f2eb;padding:10px;border-radius:8px}.lead-request{background:#fff;border:1px solid #cbd9cf;border-radius:8px;padding:12px;margin:12px 0}.lead-request h4{margin:0 0 6px;color:#145c35;font-size:14px;letter-spacing:.03em}.urgent{border:2px solid #b45309;background:#fffaf0}.status-form label{display:block;font-weight:600}.status-form select{margin-bottom:8px}.share-actions{display:flex;gap:8px;flex-wrap:wrap}.share-actions a{display:inline-block;padding:10px;background:#e8f2eb;border-radius:8px;text-decoration:none;font-weight:bold}'
        'a{color:#08783c}.error{color:#a11;background:#fff0f0;padding:10px;border-radius:8px}'
        '.helper{font-size:13px;color:#53645a;font-weight:normal}.project-board{background:#eef6f0;border:1px solid #c8dccd;border-radius:12px;padding:14px;margin:18px 0}.project-board h2{margin-top:0}.project-card{border-left-color:#2563eb}.lead-entry{background:#f7faf8;border:1px solid #dce5de;border-radius:10px;padding:12px}.lead-entry summary{font-weight:bold;color:#145c35;padding:8px;cursor:pointer}.lead-card{background:#fff;border:1px solid #d5e1d8;border-left:5px solid #168447;box-shadow:0 2px 6px rgba(0,0,0,.04)}.lead-card h3{font-size:20px;margin:8px 0}.lead-kicker{font-size:13px;text-transform:uppercase;letter-spacing:.03em}.lead-request p{margin-bottom:0}.lead-location{font-weight:600}.lead-facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:14px 0}.lead-facts p{margin:0;padding:10px;background:#f3f6f4;border-radius:8px}.lead-facts b,.lead-facts span{display:block}.lead-facts b{font-size:12px;color:#53645a;text-transform:uppercase}.status-form{display:flex;align-items:end;gap:10px;flex-wrap:wrap;margin:12px 0}.status-form label{flex:1;min-width:180px;margin:0}.status-form button{width:auto;margin:0;white-space:nowrap}.share-actions a{min-height:40px}.source-warning{padding:12px;border:1px solid #d8c28b;background:#fff9e8;border-radius:9px}.lead-filters{display:grid;grid-template-columns:2fr 1fr auto auto;gap:10px;align-items:end;background:#f7faf8;border:1px solid #dce5de;border-radius:10px;padding:12px}.lead-filters label{margin:0}.lead-filters button{width:auto;margin:0}.lead-filters a{align-self:center}@media(max-width:620px){.lead-filters{grid-template-columns:1fr}.lead-facts{grid-template-columns:1fr 1fr}}h1{margin-top:10px}hr{border:0;border-top:1px solid #dce5de;margin:22px 0}'
        '@media(max-width:520px){body{padding:10px}header,main{padding:14px}}</style></head><body>'
        '<header><nav><a href="/">Public tenders</a> · <a href="/leads">Private leads</a> · <a href="/sources">Sources</a></nav>'
        '<h1>Private trade leads</h1><p>Save genuine customer enquiries separately from council tenders.</p></header><main>'
        '<p><b>Private leads.</b> Each card shows the job, trade, location, requested work and contact so you can pass it to a suitable tradesperson. They stay separate from public tenders.</p>'
        '<div class="lead-facts"><p><b>Total captured leads</b><span>' + str(lead_counts["total"]) + '</span></p><p><b>New</b><span>' + str(lead_counts["new"]) + '</span></p><p><b>Passed to trade</b><span>' + str(lead_counts["passed"]) + '</span></p></div>'
        '<p><a href="/leads/export.csv">Download private leads CSV (backup)</a></p>'
        '<section class="project-board"><h2>Public construction project opportunities</h2>'
        '<p>These are public project summaries from BuildConnect, not private customer enquiries. Contact details are not included. Location is only a clue from the feed and must be checked before sharing.</p>'
        '<form method="post" action="/leads/import-buildconnect"><button type="submit">Check BuildConnect for new projects</button></form>'
        + project_status + (project_cards or '<p>No matching target-area projects have been imported yet. Use “Check BuildConnect for new projects” to search the public feed.</p>')
        + '</section>'
        '<p class="source-warning"><b>Source status:</b> private leads here are enquiries captured in TenderScout. This list is not yet connected to an authorised live feed of domestic customer requests. Facebook and Nextdoor are not scraped, and customer contact details are never invented.</p>'
        '<form class="lead-filters" method="get" action="/leads"><label>Filter by trade<select name="trade"><option value="">All trades</option>' + "".join(f'<option value="{safe(t)}"{" selected" if t == lead_trade else ""}>{safe(t)}</option>' for t in TRADE_CATEGORIES) + '</select></label><label>Filter by status<select name="status"><option value="">All statuses</option>' + "".join(f'<option value="{safe(st)}"{" selected" if st == lead_status else ""}>{safe(st)}</option>' for st in LEAD_STATUSES) + '</select></label><button type="submit">Apply filters</button><a href="/leads">Clear filters</a></form>'
        '<p>Record the most precise location and only save customer contact details when they were provided for this enquiry and can legitimately be reused. A direct source URL is optional.</p>'
        + message +
        '<details class="lead-entry"><summary>+ Add a private job manually</summary><form method="post"><label>Job / enquiry title<input name="title" required maxlength="250" placeholder="e.g. Urgent gutter cleaning needed"></label>'
        '<label>Trade category<select name="trade_category" required><option value="">Choose the right trade</option>' + options + '</select></label>'
        '<label>Exact job location<input name="location_text" required maxlength="300" placeholder="e.g. street/area and postcode, Allerton L18"></label>'
        '<label>Urgency (optional)<select name="urgency">' + "".join(f'<option value="{safe(u)}"{" selected" if u == "Flexible / no rush" else ""}>{safe(u)}</option>' for u in LEAD_URGENCIES) + '</select></label>'
        '<label>Where did you find it? (optional)<select name="source_name"><option value="">Choose a source</option>' + source_options + '</select><span class="helper">Select a source type instead of typing its name.</span></label>'
        '<label>Original post / source link (optional)<input name="source_url" type="url" maxlength="1000" placeholder="Paste a link only if you have one"></label>'
        '<label>Customer / business contact <span class="helper">(only where provided for this enquiry and appropriate to reuse)</span><input name="contact_details" maxlength="500" placeholder="e.g. name and phone number"></label>'
        '<label>What exactly does the customer want? <span class="helper">(required — copy the actual request, not just the trade)</span><textarea name="details" required rows="5" maxlength="4000" placeholder="Example: wants rear patio pressure-washed, approx. 20 m², at the address above; asks for a quote this week. Only include details stated in the original enquiry."></textarea></label>'
        '<button type="submit">Save private lead</button></form></details><hr><h2>Latest private job leads</h2><p class="helper">Job-board view · newest first · open the original source before contacting anyone.</p>' + cards +
        '<p><a href="/">Public tenders</a> · <a href="/sources">Council & community sources</a></p></main></body></html>'
    )
    return Response(page, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
