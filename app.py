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
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from flask import Flask, Response, redirect, request

import tenderscout as collector
import planning
import digest

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
    opportunity_stage TEXT DEFAULT 'Other notice',
    ocid TEXT,
    related_ocids TEXT,
    notice_status TEXT DEFAULT '',
    cancelled_by_notice_url TEXT
"""
# Columns added after the first release; added to older databases on start-up.
# Older rows default to REVIEW because their location was never confirmed.
ADDED_COLUMNS = [
    ("pipeline_status", "TEXT DEFAULT 'Reviewing'"),
    ("region_match", "TEXT DEFAULT 'REVIEW'"),
    ("opportunity_stage", "TEXT DEFAULT 'Other notice'"),
    ("ocid", "TEXT"),
    ("related_ocids", "TEXT"),
    ("notice_status", "TEXT DEFAULT ''"),
    ("cancelled_by_notice_url", "TEXT"),
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
    db.execute(f"""CREATE TABLE IF NOT EXISTS planning_leads (
        id {id_column},
        name TEXT NOT NULL UNIQUE,
        area TEXT,
        address TEXT,
        postcode TEXT,
        description TEXT,
        app_type TEXT,
        app_state TEXT,
        app_size TEXT,
        start_date TEXT,
        decided_date TEXT,
        trades TEXT,
        household INTEGER DEFAULT 0,
        council_url TEXT,
        planit_url TEXT,
        fetched_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS planning_runs (
        id INTEGER PRIMARY KEY,
        ran_at TEXT,
        infos_json TEXT,
        status TEXT,
        started_at TEXT,
        error TEXT
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS subscribers (
        id {id_column},
        name TEXT NOT NULL,
        business TEXT,
        email TEXT NOT NULL,
        planning_trades TEXT DEFAULT '',
        tender_trades TEXT DEFAULT '',
        areas TEXT DEFAULT '',
        monthly_fee TEXT,
        active INTEGER DEFAULT 1,
        notes TEXT,
        last_sent_at TEXT,
        created_at TEXT NOT NULL
    )""")
    for table, column, ddl in (("planning_leads", "job_size", "TEXT"), ("subscribers", "team_size", "INTEGER"),
                               ("planning_runs", "heartbeat_at", "TEXT")):
        if USE_POSTGRES:
            db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}")
        else:
            cols = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in cols:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
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
    db.execute("""CREATE TABLE IF NOT EXISTS fts_checkpoints (
        fingerprint TEXT NOT NULL,
        window_key TEXT NOT NULL,
        state_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (fingerprint, window_key)
    )""")
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


def _fts_checkpoint_can_clear(stats):
    """Only discard saved pages when pagination ended without truncation."""
    return collector._coverage_status(stats) == "FETCH COMPLETE (pagination ended or source exposes no cursor)"


class FTSCheckpointStore:
    """Durable per-window Find a Tender cursor and accumulated-page store."""

    def __init__(self, fingerprint):
        self.fingerprint = fingerprint

    def load(self, window_key):
        db = connect_db()
        try:
            row = db.execute(
                "SELECT state_json FROM fts_checkpoints WHERE fingerprint = ? AND window_key = ?",
                (self.fingerprint, window_key),
            ).fetchone()
            return json.loads(row["state_json"]) if row else None
        finally:
            db.close()

    def save(self, window_key, state):
        db = connect_db()
        try:
            db.execute(
                """INSERT INTO fts_checkpoints (fingerprint, window_key, state_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(fingerprint, window_key) DO UPDATE SET
                       state_json=excluded.state_json, updated_at=excluded.updated_at""",
                (self.fingerprint, window_key, json.dumps(state, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat()),
            )
            db.commit()
        finally:
            db.close()

    def clear(self):
        db = connect_db()
        try:
            db.execute("DELETE FROM fts_checkpoints WHERE fingerprint = ?", (self.fingerprint,))
            db.commit()
        finally:
            db.close()


# ---------------------------------------------------------------- security --

@app.before_request
def require_login():
    """Fail closed: dashboard and private customer leads are never public."""
    if request.path in ("/health", "/health/db", "/join", "/join/thanks"):
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
    """Liveness check for the host. Answers instantly and never touches the database.

    Render calls this while the free instance is waking up. If it waited on a slow
    first database connection, Render would treat the app as broken and restart it
    mid-load, which showed visitors a long blank screen. Use /health/db to check
    the database itself.
    """
    return {"status": "ok", "database": "postgres" if USE_POSTGRES else "sqlite"}


@app.route("/health/db")
def health_db():
    """Readiness check that also proves the database answers."""
    try:
        db = connect_db()
        db.execute("SELECT 1")
        db.close()
        return {"status": "ok", "database": "postgres" if USE_POSTGRES else "sqlite"}
    except Exception:
        app.logger.exception("Database health check failed")
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


def display_time(value):
    """UK clock time for an ISO timestamp, e.g. '7:05pm'."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone(planning.LONDON)
    return local.strftime("%I:%M%p").lstrip("0").lower()


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
    # A cancellation suppresses the affected notice in every dashboard view,
    # while preserving it and the cancellation event in the audit/export data.
    condition += " AND COALESCE(notice_status, '') != 'cancelled'"
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
    refresh_summary = (request.args.get("refresh_summary") or "").strip()
    if refresh_summary:
        banner = '<div class="banner"><b>Refresh result.</b> ' + safe(refresh_summary) + '</div>'
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
        window.location.replace('/?refresh_summary=' + encodeURIComponent(state.summary || 'Search completed.'));
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
    <p><a style="color:white" href="/">Public tenders</a> · <a style="color:white" href="/planning">Planning leads</a> · <a style="color:white" href="/subscribers">Subscribers</a> · <a style="color:white" href="/leads">Private trade leads</a> · <a style="color:white" href="/sources">Council & community sources</a></p>
    <p>Coverage: Liverpool, Sefton, St Helens, Halton, Knowsley and Wirral. Warrington and Cheshire West and Chester are excluded.</p>
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


def reclassify_saved_geography(db):
    """Reapply current delivery-location rules to previously saved tenders.

    Older records may not be returned by a later feed refresh, so their
    geography can otherwise remain stale indefinitely. Preserve manually
    entered records and every non-geography field, including pipeline status.
    """
    rows = db.execute(
        "SELECT id, title, description, location_text, region_match, geography_reason "
        "FROM tenders WHERE region_match != 'MANUAL'"
    ).fetchall()
    changed = 0
    for row in rows:
        record = dict(row)
        classification, reason = collector.classify_geography(
            record, collector.TARGET_AREA_LABEL
        )
        if classification != record.get("region_match") or reason != record.get("geography_reason"):
            db.execute(
                "UPDATE tenders SET region_match = ?, geography_reason = ? WHERE id = ?",
                (classification, reason, record["id"]),
            )
            changed += 1
    return changed

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
    fts_checkpoint = FTSCheckpointStore(f"fts-v1-days-{days}")
    # Run independent free sources concurrently. Bearings and Standstill are
    # discovery sources, not delivery-site proof; uncertain records stay REVIEW.
    with ThreadPoolExecutor(max_workers=4) as pool:
        fts_job = pool.submit(collector.fetch_fts, days, pages, fts_limit, fts_stats, fts_checkpoint)
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
    db = connect_db()
    try:
        stats["reclassified"] = reclassify_saved_geography(db)
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
                 geography_reason, region_match, trade_category, saved_at, pipeline_status, opportunity_stage,
                 ocid, related_ocids, notice_status, cancelled_by_notice_url)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    ocid=excluded.ocid,
                    related_ocids=excluded.related_ocids,
                    notice_status=CASE WHEN tenders.notice_status = 'cancelled' THEN tenders.notice_status ELSE excluded.notice_status END,
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
                    match.get("ocid", ""),
                    json.dumps(match.get("related_ocids") or [], ensure_ascii=False),
                    match.get("notice_status", ""),
                    "",
                ),
            )
            stats["local" if region_match == "LOCAL" else "review"] += 1
        # A UK12/tenderCancellation release terminates the procurement identified
        # by its own OCID. Do not apply that cancellation to related-process OCIDs:
        # those may identify an earlier or continuing procurement.
        for cancellation in records:
            if cancellation.get("notice_status") != "cancelled" or not cancellation.get("ocid"):
                continue
            cancellation_url = cancellation.get("notice_url") or ""
            db.execute(
                "UPDATE tenders SET notice_status = 'cancelled', cancelled_by_notice_url = ? "
                "WHERE source = 'Find a Tender' AND ocid = ? AND notice_url != ?",
                (cancellation_url, cancellation.get("ocid"), cancellation_url),
            )
        db.commit()
    finally:
        db.close()
    # Clear only after every fetched record has been committed and Find a
    # Tender reached the end of its cursor chain. Partial/time-limited runs must
    # retain their accumulated records and cursor for the next refresh.
    if _fts_checkpoint_can_clear(fts_stats):
        fts_checkpoint.clear()
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


def feed_coverage_lines():
    """Return per-source coverage labels using the collector's authoritative rules."""
    lines = []
    for name, stats in LAST_FEEDS.items():
        if not isinstance(stats, dict):
            continue
        status = collector._coverage_status(stats)
        line = (
            f"{name}: {status}; {stats.get('records', 0)} notices "
            f"in {stats.get('pages', 0)} page(s)"
        )
        if stats.get("first_date") and stats.get("last_date"):
            line += f", published {stats['first_date']} to {stats['last_date']}"
        if stats.get("windows"):
            line += f" across {stats['windows']} search window(s)"
        if stats.get("feed_counts"):
            detail = ", ".join(
                f"{label}: {count}" for label, count in stats["feed_counts"].items()
            )
            line += f"; feed breakdown: {detail}"
        if stats.get("error"):
            line += f"; error: {stats['error']}"
        if stats.get("partial_errors"):
            line += f"; partial errors: {stats['partial_errors']}"
        if stats.get("stopped"):
            line += f"; stopped: {stats['stopped']}"
        if stats.get("more"):
            line += "; additional cursor pages were not read"
        if stats.get("truncated_windows"):
            line += f"; truncated windows: {stats['truncated_windows']}"
        if status == "POSSIBLE TRUNCATION":
            line += "; source cap may have hidden more records (not confirmed)"
        lines.append(line)
    return lines


def feed_notes():
    """Render source-by-source diagnostics for the synchronous refresh result."""
    lines = [safe(line) for line in feed_coverage_lines()]
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
    """Return only proven collector conditions that make coverage incomplete."""
    issues = []
    for name, feed in LAST_FEEDS.items():
        if not isinstance(feed, dict):
            continue
        if collector._coverage_status(feed) != "INCOMPLETE":
            continue
        reasons = []
        if feed.get("error"):
            reasons.append("feed error")
        if feed.get("partial_errors"):
            reasons.append("partial feed errors")
        if feed.get("stopped"):
            reasons.append("stopped early")
        if feed.get("more"):
            reasons.append("additional cursor pages not read")
        if feed.get("truncated_windows"):
            reasons.append("one or more search windows truncated")
        issues.append(f"{name}: {', '.join(reasons) or 'collector marked coverage incomplete'}")
    return issues


def refresh_feed_warnings():
    """Return possible source-cap truncation separately from incomplete collection."""
    warnings = []
    for name, feed in LAST_FEEDS.items():
        if not isinstance(feed, dict):
            continue
        if collector._coverage_status(feed) == "POSSIBLE TRUNCATION":
            warnings.append(
                f"{name}: source cap may have hidden additional records; "
                "this is a warning, not confirmed pagination failure"
            )
    return warnings


def _set_refresh_state(status, summary="", error="", refresh_token=None):
    """Update refresh state only while this refresh still owns the database lease."""
    db = connect_db()
    now = datetime.now(timezone.utc).isoformat()
    try:
        cursor = db.execute(
            """UPDATE refresh_state
               SET status = ?, finished_at = ?, summary = ?, error = ?, heartbeat_at = NULL
               WHERE id = 1
                 AND (refresh_token = ? OR (refresh_token IS NULL AND CAST(? AS TEXT) IS NULL))""",
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
        coverage_lines = feed_coverage_lines()
        if coverage_lines:
            summary += " Source coverage: " + " | ".join(coverage_lines)
        issues = refresh_feed_issues()
        warnings = refresh_feed_warnings()
        if issues:
            detail = "; ".join(issues)
            _set_refresh_state("incomplete", summary=summary + " Some source results may be missing.",
                               error="Partial collection: " + detail[:900], refresh_token=refresh_token)
            app.logger.warning("Refresh saved partial results: %s", detail)
        else:
            if warnings:
                warning_text = "; ".join(warnings)
                summary += " Coverage warning: " + warning_text
                app.logger.warning("Refresh completed with possible source-cap warnings: %s", warning_text)
            _set_refresh_state("completed", summary=summary, refresh_token=refresh_token)
        refresh_planning_leads_safely()
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
                 AND (started_at = ? OR (started_at IS NULL AND CAST(? AS TEXT) IS NULL))
                 AND (refresh_token = ? OR (refresh_token IS NULL AND CAST(? AS TEXT) IS NULL))
                 AND (heartbeat_at = ? OR (heartbeat_at IS NULL AND CAST(? AS TEXT) IS NULL))""",
            (started, started, token, current.get("status"), current.get("started_at"),
             current.get("started_at"), current.get("refresh_token"), current.get("refresh_token"),
             current.get("heartbeat_at"), current.get("heartbeat_at")),
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
    "opportunity_stage", "ocid", "related_ocids", "notice_status", "cancelled_by_notice_url",
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
        '<header><nav><a href="/">Public tenders</a> · <a href="/planning">Planning leads</a> · <a href="/subscribers">Subscribers</a> · <a href="/leads">Private leads</a> · <a href="/sources">Sources</a></nav>'
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
        '<header><nav><a href="/">Public tenders</a> · <a href="/planning">Planning leads</a> · <a href="/subscribers">Subscribers</a> · <a href="/leads">Private leads</a> · <a href="/sources">Sources</a></nav>'
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


# --------------------------------------------------------- planning leads --

PLANNING_KEEP_DAYS = 120
PLANNING_AUTO_EVERY_HOURS = 20      # automatic check at most once a day
PLANNING_RUNNING_STALE_MINUTES = 3   # no heartbeat for this long = the check was killed (e.g. the free instance slept)
KEEPALIVE_EVERY_SECONDS = 30


def _planning_run(db):
    return db.execute("SELECT * FROM planning_runs WHERE id = 1").fetchone()


def _set_planning_run(db, **fields):
    db.execute("INSERT INTO planning_runs (id) VALUES (1) ON CONFLICT(id) DO NOTHING")
    for key, value in fields.items():
        db.execute(f"UPDATE planning_runs SET {key} = ? WHERE id = 1", (value,))
    db.commit()


def _hours_since(iso, now):
    try:
        then = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (now - then).total_seconds() / 3600


def planning_running(row, now=None):
    """True while a check is alive: it writes a heartbeat at least every 30 seconds."""
    now = now or datetime.now(timezone.utc)
    if not row or row["status"] != "running":
        return False
    try:
        beat = row["heartbeat_at"] or row["started_at"]
    except (KeyError, IndexError):
        beat = row["started_at"]
    hours = _hours_since(beat, now)
    return hours is not None and hours * 60 < PLANNING_RUNNING_STALE_MINUTES


def _planning_heartbeat():
    db = connect_db()
    try:
        _set_planning_run(db, heartbeat_at=datetime.now(timezone.utc).isoformat())
    finally:
        db.close()


def _ping_self():
    """Visit our own public address so Render's free plan sees traffic and doesn't put the app to sleep mid-check."""
    base = (os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
    if not base.startswith("https://"):
        return
    try:
        with urlopen(Request(base + "/health", headers={"User-Agent": "TenderScout keep-awake"}), timeout=10) as r:
            r.read(200)
    except Exception:
        pass


def _keepalive_sleep(seconds):
    """Wait between PlanIt requests while keeping the check visibly alive and the instance awake."""
    import time as _time
    remaining = float(seconds)
    while remaining > 0:
        chunk = min(KEEPALIVE_EVERY_SECONDS, remaining)
        try:
            _planning_heartbeat()
        except Exception:
            app.logger.exception("Planning heartbeat failed")
        _ping_self()
        _time.sleep(chunk)
        remaining -= chunk


def refresh_planning_leads(getter=None, now=None, sleep=None):
    """Read recent planning applications for the target councils and save the useful ones."""
    leads, infos = planning.fetch_all(days=env_int("TENDERSCOUT_PLANNING_DAYS", 30), getter=getter,
                                      sleep=sleep or _keepalive_sleep)
    db = connect_db()
    try:
        for lead in leads:
            db.execute(
                "INSERT INTO planning_leads (name,area,address,postcode,description,app_type,app_state,app_size,"
                "start_date,decided_date,trades,job_size,household,council_url,planit_url,fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "app_state=excluded.app_state, decided_date=excluded.decided_date, trades=excluded.trades, "
                "job_size=excluded.job_size, description=excluded.description, fetched_at=excluded.fetched_at",
                tuple(lead[k] for k in ("name", "area", "address", "postcode", "description", "app_type", "app_state",
                                        "app_size", "start_date", "decided_date", "trades", "job_size", "household",
                                        "council_url", "planit_url", "fetched_at")),
            )
        cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=PLANNING_KEEP_DAYS)).date().isoformat()
        db.execute("DELETE FROM planning_leads WHERE start_date < ?", (cutoff,))
        db.commit()
        _set_planning_run(db, ran_at=datetime.now(timezone.utc).isoformat(), infos_json=json.dumps(infos),
                          status="done", error="")
    finally:
        db.close()
    return leads, infos


def _planning_worker():
    # Logged as warnings so the lines show in Render's logs (info is hidden there).
    app.logger.warning("PLANNING: check started")
    try:
        leads, infos = refresh_planning_leads()
        app.logger.warning("PLANNING: check finished, %s useful leads saved; %s", len(leads),
                           "; ".join(f"{i.get('area')}={i.get('kept', 0)}/{i.get('total', 0)}"
                                     + (f" ERROR {i['error']}" if i.get("error") else "") for i in infos))
    except Exception as exc:
        app.logger.exception("PLANNING: check failed")
        db = connect_db()
        try:
            _set_planning_run(db, status="failed", error=str(exc)[:300])
        finally:
            db.close()


def start_planning_refresh(now=None, automatic=False):
    """Start a background PlanIt check if allowed. Returns 'started' or the reason it didn't start."""
    now = now or datetime.now(timezone.utc)
    try:
        planning.user_agent()
    except planning.PlanningConfigError:
        return "config"
    if not planning.in_quiet_hours(now):
        return "daytime"
    db = connect_db()
    try:
        row = _planning_run(db)
        if planning_running(row, now):
            return "running"
        if automatic and row and row["ran_at"]:
            hours = _hours_since(row["ran_at"], now)
            if hours is not None and hours < PLANNING_AUTO_EVERY_HOURS:
                return "recent"
        _set_planning_run(db, status="running", started_at=now.isoformat(), heartbeat_at=now.isoformat(), error="")
    finally:
        db.close()
    threading.Thread(target=_planning_worker, daemon=True, name="tenderscout-planning").start()
    return "started"


def refresh_planning_leads_safely():
    """Called after the scheduled tender refresh. Never affects the tender refresh status."""
    try:
        result = start_planning_refresh(automatic=True)
        app.logger.warning("PLANNING: automatic check after tender refresh: %s", result)
    except Exception:
        app.logger.exception("Planning leads refresh could not start")


@app.route("/planning/refresh", methods=["POST"])
def planning_refresh():
    return redirect("/planning?check=" + start_planning_refresh())


@app.route("/planning")
def planning_page():
    trade = request.args.get("trade", "")
    trade = trade if trade in planning.TRADE_ORDER else ""
    area = request.args.get("area", "")
    area = area if area in planning.AREAS else ""
    status = request.args.get("status", "")
    status = status if status in ("approved", "pending") else ""
    db = connect_db()
    try:
        where, params = [], []
        if trade:
            where.append("trades LIKE ?")
            params.append("%" + trade + "%")
        if area:
            where.append("area = ?")
            params.append(area)
        if status == "approved":
            where.append("app_state IN ('Permitted', 'Conditions')")
        elif status == "pending":
            where.append("app_state = 'Undecided'")
        sql = "SELECT * FROM planning_leads" + (" WHERE " + " AND ".join(where) if where else "")
        rows = db.execute(sql + " ORDER BY start_date DESC LIMIT 200", tuple(params)).fetchall()
        total = db.execute("SELECT COUNT(*) AS n FROM planning_leads").fetchone()["n"]
        trade_counts = {t: db.execute("SELECT COUNT(*) AS n FROM planning_leads WHERE trades LIKE ?", ("%" + t + "%",)).fetchone()["n"]
                        for t in planning.TRADE_ORDER}
        run = _planning_run(db)
    finally:
        db.close()

    infos = []
    if run and run["infos_json"]:
        try:
            infos = json.loads(run["infos_json"])
        except ValueError:
            infos = []
    coverage = ""
    for info in infos:
        if info.get("error"):
            note, cls = "could not be read this time: " + safe(info["error"]), "warn"
        elif planning.area_is_stale(info):
            note, cls = ("not updating on PlanIt (newest application " + safe(display_date(info.get("newest")) or "none")
                         + "). Check the council's own planning search."), "warn"
        else:
            note, cls = str(int(info.get("kept") or 0)) + " useful of " + str(int(info.get("total") or 0)) + " recent applications", "ok"
        coverage += f'<li class="{cls}"><b>{safe(info.get("area"))}</b>: {note}</li>'
    last_run = display_date(run["ran_at"]) if run and run["ran_at"] else ""

    def chip(label, **query):
        merged = {"trade": trade, "area": area, "status": status}
        merged.update(query)
        href = "/planning?" + urlencode({k: v for k, v in merged.items() if v})
        current = all(merged[k] == {"trade": trade, "area": area, "status": status}[k] for k in merged)
        return f'<a class="chip{" on" if current else ""}" href="{safe(href)}">{label}</a>'

    trade_chips = chip("All trades", trade="") + "".join(
        chip(f"{safe(t)} <span>{trade_counts[t]}</span>", trade=t) for t in planning.TRADE_ORDER if trade_counts[t])
    area_chips = chip("All areas", area="") + "".join(chip(safe(a), area=a) for a in planning.AREAS)
    status_chips = chip("Any stage", status="") + chip("Approved", status="approved") + chip("Awaiting decision", status="pending")

    cards = ""
    for row in rows:
        approved = row["app_state"] in ("Permitted", "Conditions")
        share = "\n".join([
            "PLANNING LEAD: " + str(row["description"] or ""),
            "WHERE: " + str(row["address"] or ""),
            "STAGE: " + planning.status_label(row["app_state"]) + (" (" + display_date(row["decided_date"]) + ")" if row["decided_date"] else ""),
            "LIKELY TRADES: " + str(row["trades"] or ""),
            "COUNCIL RECORD: " + str(row["council_url"] or row["planit_url"] or ""),
            "Contact the owner by letter or leaflet only. No cold calls or emails.",
        ])
        wa = "https://wa.me/?" + urlencode({"text": share})
        em = "mailto:?" + urlencode({"subject": "Planning lead: " + str(row["address"] or "")[:80], "body": share})
        link = row["council_url"] or row["planit_url"]
        link_html = f'<a href="{safe(link)}" target="_blank" rel="noopener">Council record</a>' if link else ""
        trades_html = "".join(f'<span class="tag">{safe(t.strip())}</span>' for t in str(row["trades"] or "").split(",") if t.strip())
        cards += (
            f'<article class="plan{" hot" if approved else ""}">'
            f'<p class="kicker">{safe(row["area"])} · {"Household" if row["household"] else "Commercial / development"} · {safe(row["job_size"] or planning.job_size(row["description"], row["app_size"]))} job</p>'
            f'<h3>{safe(row["description"])}</h3>'
            f'<p class="where"><b>WHERE:</b> {safe(row["address"])}</p>'
            f'<p class="stage {"ok" if approved else ""}">{safe(planning.status_label(row["app_state"]))}'
            f'{" · decided " + safe(display_date(row["decided_date"])) if row["decided_date"] else ""} · submitted {safe(display_date(row["start_date"]) or "date not stated")}</p>'
            f'<p class="tags">{trades_html}</p>'
            f'<p class="actions">{link_html}'
            f'<a href="{safe(wa)}" target="_blank" rel="noopener">Send by WhatsApp</a><a href="{safe(em)}">Send by email</a></p></article>'
        )
    if not cards:
        cards = ('<p class="empty">No planning leads saved yet. Tap <b>Check for new planning applications</b> above.</p>'
                 if not total else '<p class="empty">Nothing matches these filters. Try All trades or All areas.</p>')

    check = request.args.get("check", "")
    notice = ""
    if planning_running(run):
        notice = ('<p class="notice ok"><b>Checking PlanIt now</b> (started ' + safe(display_time(run["started_at"]))
                  + '). It waits a minute between councils, so it takes about 6 minutes. Reload this page later.</p>')
    elif check == "daytime":
        notice = ('<p class="notice warn">PlanIt asks for checks between 6pm and 6am, so it isn\'t checked during the day. '
                  'It checks automatically overnight, or tap the button after 6pm.</p>')
    elif check == "config":
        notice = ('<p class="notice warn">Planning checks are switched off until <b>PLANIT_CONTACT_EMAIL</b> is set in Render '
                  '(your service, Environment). PlanIt needs a contact email on every request.</p>')
    elif infos and all(i.get("error") for i in infos):
        notice = ('<p class="notice warn"><b>PlanIt turned down every request last time</b> '
                  '(' + safe(infos[0].get("error", "")[:80]) + '). PlanIt itself is usually fine, so this is most likely a limit on '
                  'the shared internet address this app uses. Try again later; if it keeps happening, PlanIt\'s owner needs to allow it.</p>')
    elif run and run["status"] == "failed":
        notice = '<p class="notice warn">The last check failed: ' + safe(run["error"] or "unknown error") + '. It will try again tonight.</p>'

    page = (
        '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Tender Scout — planning leads</title><style>'
        'body{font:16px Arial;max-width:960px;margin:0 auto;padding:16px;color:#18251e;background:#f3f6f4;line-height:1.45}'
        'header{background:#143d2a;color:white;border-radius:14px;padding:20px;margin-bottom:16px}header a{color:white}nav{font-size:14px}'
        'main{background:white;border-radius:14px;padding:18px}a{color:#08783c}'
        'button{padding:13px 18px;background:#168447;color:white;border:0;border-radius:9px;font-weight:bold;font-size:16px;width:100%}'
        '.push{display:flex;align-items:center;justify-content:center;gap:10px;min-height:64px;font-size:19px;border-radius:14px;'
        'background:linear-gradient(#1fa35a,#147a40);box-shadow:0 6px 0 #0d5129,0 10px 18px rgba(0,0,0,.18);'
        'transform:translateY(0);transition:transform .06s,box-shadow .06s;margin:4px 0 14px;cursor:pointer;-webkit-tap-highlight-color:transparent}'
        '.push:active,.push.pressed{transform:translateY(6px);box-shadow:0 0 0 #0d5129,0 4px 8px rgba(0,0,0,.18)}'
        '.push[disabled]{background:#6b8f7a;box-shadow:0 2px 0 #4d6a5a;transform:translateY(4px);cursor:default}'
        '.push .ico{font-size:22px;line-height:1}'
        '@media(prefers-reduced-motion:reduce){.push{transition:none}}'
        '.chips{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}.chip{padding:8px 12px;border:1px solid #b8c6bd;border-radius:99px;text-decoration:none;color:#18251e;font-size:14px;background:#fff}'
        '.chip span{color:#53645a}.chip.on{background:#143d2a;color:#fff;border-color:#143d2a}.chip.on span{color:#cfe3d6}'
        'article.plan{background:#fff;border:1px solid #d5e1d8;border-left:5px solid #9bb5a5;border-radius:10px;padding:14px;margin:12px 0;overflow-wrap:anywhere}'
        'article.plan.hot{border-left-color:#168447}.kicker{font-size:13px;text-transform:uppercase;letter-spacing:.03em;color:#53645a;margin:0}'
        'article h3{font-size:18px;margin:8px 0}.where{background:#e8f2eb;padding:10px;border-radius:8px}.stage{font-size:14px;color:#53645a}.stage.ok{color:#08783c;font-weight:bold}'
        '.tags{display:flex;flex-wrap:wrap;gap:6px}.tag{font-size:13px;background:#f3f6f4;border:1px solid #dce5de;border-radius:99px;padding:3px 9px}'
        '.actions{display:flex;flex-wrap:wrap;gap:8px}.actions a{padding:10px 12px;background:#e8f2eb;border-radius:8px;text-decoration:none;font-weight:bold;min-height:40px}'
        '.notice{padding:12px;border-radius:9px}.notice.ok{background:#e8f2eb}.notice.warn,.privacy{background:#fff9e8;border:1px solid #d8c28b;padding:12px;border-radius:9px}'
        '.coverage{margin:6px 0 0;padding-left:18px;font-size:14px}.coverage .warn{color:#8a5a00}.empty{padding:20px;text-align:center;color:#53645a}'
        'details summary{cursor:pointer;font-weight:bold;color:#145c35;padding:6px 0}'
        '@media(max-width:520px){body{padding:10px}header,main{padding:14px}}</style></head><body>'
        '<header><nav><a href="/">Public tenders</a> · <a href="/planning">Planning leads</a> · <a href="/subscribers">Subscribers</a> · <a href="/leads">Private leads</a> · <a href="/sources">Sources</a></nav>'
        '<h1>Planning leads</h1><p>New extensions, loft conversions, new homes, shop refits and tree works in Merseyside, from public planning applications.</p></header><main>'
        + notice +
        '<form method="post" action="/planning/refresh" id="checkform">'
        + ('<button type="submit" class="push" disabled><span class="ico">⏳</span> Checking now… reload in a few minutes</button>'
           if planning_running(run) else
           '<button type="submit" class="push" id="checkbtn"><span class="ico">🔍</span> Check for new planning applications</button>')
        + '</form>'
        '<script>(function(){var f=document.getElementById("checkform"),b=document.getElementById("checkbtn");'
        'if(!f||!b)return;f.addEventListener("submit",function(){b.classList.add("pressed");b.disabled=true;'
        'b.innerHTML="<span class=\\"ico\\">⏳</span> Starting…";});})();</script>'
        f'<p class="stage">{str(total)} leads saved{" · last checked " + safe(last_run) if last_run else ""}. Green cards are approved, so work is likely soon.</p>'
        + (f'<details><summary>Council coverage</summary><ul class="coverage">{coverage}</ul></details>' if coverage else "")
        + '<div class="chips">' + trade_chips + '</div><div class="chips">' + area_chips + '</div><div class="chips">' + status_chips + '</div>'
        '<p class="privacy"><b>How to use these leads.</b> A planning application shows planned work, not a request for quotes. '
        'Addresses are public planning records. Tradespeople should contact homeowners by letter or leaflet only, never by cold call or unsolicited email. '
        'Businesses and developers can be approached directly.</p>'
        + cards +
        '<p class="stage">Source: UK PlanIt (planit.org.uk), which collects council planning registers. Always check the council record before acting.</p>'
        '</main></body></html>'
    )
    return Response(page, mimetype="text/html")


# ------------------------------------------------------ subscriber emails --

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def digest_candidates(db, now):
    """New planning leads and open local tenders from the last fortnight."""
    since = (now - timedelta(days=14)).date().isoformat()
    week = (now - timedelta(days=7)).date().isoformat()
    today = now.date().isoformat()
    leads = db.execute(
        "SELECT * FROM planning_leads WHERE start_date >= ? OR decided_date >= ? ORDER BY start_date DESC LIMIT 500",
        (since, week),
    ).fetchall()
    tenders = db.execute(
        "SELECT * FROM tenders WHERE region_match = 'LOCAL' AND opportunity_stage = 'Open for bids' "
        "AND COALESCE(notice_status, '') != 'cancelled' AND (deadline IS NULL OR deadline = '' OR deadline >= ?) "
        "AND (published_date IS NULL OR published_date = '' OR published_date >= ?) ORDER BY deadline LIMIT 500",
        (today, since),
    ).fetchall()
    return leads, tenders


def build_digest(db, subscriber, now=None):
    now = now or datetime.now(timezone.utc)
    leads, tenders = digest_candidates(db, now)
    return digest.compose(subscriber, digest.match_planning(subscriber, leads),
                          digest.match_tenders(subscriber, tenders), planning.status_label, now)


def send_digest(db, subscriber, now=None, sender=None):
    """Send one subscriber's email. Returns 'sent', 'empty' or raises."""
    now = now or datetime.now(timezone.utc)
    built = build_digest(db, subscriber, now)
    if not built:
        db.execute("UPDATE subscribers SET last_sent_at = ? WHERE id = ?", (now.isoformat(), subscriber["id"]))
        db.commit()
        return "empty"
    subject, text, body = built
    (sender or digest.send)(subscriber["email"], subject, text, body)
    db.execute("UPDATE subscribers SET last_sent_at = ? WHERE id = ?", (now.isoformat(), subscriber["id"]))
    db.commit()
    return "sent"


def send_weekly_digests(now=None, sender=None):
    """Monday-morning run: email every active subscriber who hasn't had this week's email."""
    now = now or datetime.now(timezone.utc)
    if not digest.is_send_window(now):
        return {"skipped": "Weekly emails go out on Mondays from 6am UK time."}
    if sender is None:
        digest.smtp_settings()  # fail early with a clear message
    db = connect_db()
    result = {"sent": 0, "empty": 0, "failed": 0}
    try:
        for sub in db.execute("SELECT * FROM subscribers WHERE active = 1").fetchall():
            if not digest.due(sub, now):
                continue
            try:
                result[send_digest(db, sub, now, sender)] += 1
            except Exception:
                app.logger.exception("Weekly email failed for subscriber %s", sub["id"])
                result["failed"] += 1
    finally:
        db.close()
    return result


@app.route("/api/digest/send", methods=["POST"])
def api_digest_send():
    try:
        result = send_weekly_digests()
    except digest.DigestConfigError as exc:
        # Not an error: until email settings are added, emails are sent by hand from the preview page.
        return Response(json.dumps({"skipped": str(exc)}), mimetype="application/json")
    return Response(json.dumps(result), mimetype="application/json")


def _subscriber_form_values(form):
    return {
        "name": (form.get("name") or "").strip()[:120],
        "business": (form.get("business") or "").strip()[:160],
        "email": (form.get("email") or "").strip()[:200],
        "planning_trades": ", ".join(t for t in form.getlist("planning_trades") if t in planning.TRADE_ORDER),
        "tender_trades": ", ".join(t for t in form.getlist("tender_trades") if t in TRADE_CATEGORIES),
        "areas": ", ".join(a for a in form.getlist("areas") if a in planning.AREAS),
        "monthly_fee": (form.get("monthly_fee") or "").strip()[:20],
        "notes": (form.get("notes") or "").strip()[:2000],
        "team_size": _int_or_none(form.get("team_size")),
    }


def _int_or_none(value):
    try:
        n = int(str(value or "").strip())
    except ValueError:
        return None
    return n if 0 < n < 10000 else None


PLACES_PER_TRADE_AREA = env_int("TENDERSCOUT_PLACES_PER_AREA", 5)


def _sub_areas(value):
    areas = [a for a in digest.split_list(value) if a in planning.AREAS]
    return areas or list(planning.AREAS)   # none ticked = all six


def places_taken(db, exclude_id=None):
    """{(trade, area): number of active subscribers} for planning trades."""
    taken = {}
    for sub in db.execute("SELECT id, planning_trades, areas FROM subscribers WHERE active = 1").fetchall():
        if exclude_id is not None and sub["id"] == exclude_id:
            continue
        for trade in digest.split_list(sub["planning_trades"]):
            for area in _sub_areas(sub["areas"]):
                taken[(trade, area)] = taken.get((trade, area), 0) + 1
    return taken


def full_places(db, planning_trades, areas, exclude_id=None):
    """Trade-and-area combinations this subscriber wants that already have the maximum number of firms."""
    taken = places_taken(db, exclude_id)
    return [f"{t} in {a}" for t in digest.split_list(planning_trades) for a in _sub_areas(areas)
            if taken.get((t, a), 0) >= PLACES_PER_TRADE_AREA]


@app.route("/subscribers/<int:sub_id>/toggle", methods=["POST"])
def subscriber_toggle(sub_id):
    db = connect_db()
    try:
        sub = db.execute("SELECT * FROM subscribers WHERE id = ?", (sub_id,)).fetchone()
        if not sub:
            return Response("Subscriber not found", 404)
        if not sub["active"] and full_places(db, sub["planning_trades"], sub["areas"], exclude_id=sub_id):
            return redirect("/subscribers?sent=full")
        db.execute("UPDATE subscribers SET active = CASE WHEN active = 1 THEN 0 ELSE 1 END WHERE id = ?", (sub_id,))
        db.commit()
    finally:
        db.close()
    return redirect("/subscribers")


@app.route("/subscribers/<int:sub_id>/preview")
def subscriber_preview(sub_id):
    db = connect_db()
    try:
        sub = db.execute("SELECT * FROM subscribers WHERE id = ?", (sub_id,)).fetchone()
        if not sub:
            return Response("Subscriber not found", 404)
        built = build_digest(db, sub)
        saved_leads = db.execute("SELECT COUNT(*) AS n FROM planning_leads").fetchone()["n"]
    finally:
        db.close()
    actions = ""
    if not built:
        if not saved_leads:
            body = ('<p><b>No planning leads loaded yet.</b> After 6pm, open <a href="/planning">Planning leads</a> and tap '
                    '<b>Check for new planning applications</b>. It takes about 6 minutes. Then come back here.</p>')
        else:
            body = ('<p>No matching leads this week for these trades and areas. Try more trades or areas '
                    '(Sefton has the most), or check for new planning applications first.</p>')
        subject = "Nothing to send yet"
    else:
        subject, text, body = built
        mailto = "mailto:" + quote(sub["email"]) + "?" + urlencode({"subject": subject, "body": text}, quote_via=quote)
        actions = (
            '<div style="display:grid;gap:10px;margin:0 0 14px">'
            f'<a href="{safe(mailto)}" style="display:block;text-align:center;padding:14px;background:#168447;color:#fff;border-radius:10px;font:bold 17px Arial;text-decoration:none">Open in my email app to send</a>'
            '<button type="button" id="copy" style="padding:12px;border:0;border-radius:10px;background:#e8f2eb;color:#145c35;font:bold 16px Arial">Copy email text</button>'
            f'<form method="post" action="/subscribers/{int(sub_id)}/mark-sent" style="margin:0">'
            '<button style="width:100%;padding:12px;border:1px solid #b8c6bd;border-radius:10px;background:#fff;color:#18251e;font:bold 16px Arial">I\'ve sent it</button></form>'
            '<p style="font:13px Arial;color:#53645a;margin:0">Opens your email app with everything filled in: just press send, then tap "I\'ve sent it". '
            'If the email app shows it cut short, use Copy email text and paste it instead.</p></div>'
            f'<textarea id="plain" readonly style="position:absolute;left:-9999px">{safe(text)}</textarea>'
            '<script>document.getElementById("copy").onclick=function(){var t=document.getElementById("plain");var b=this;'
            'function sel(){t.style.position="static";t.style.width="100%";t.style.height="200px";t.select();b.textContent="Selected: copy it now";}'
            'try{navigator.clipboard.writeText(t.value).then(function(){b.textContent="Copied";},sel);}catch(e){sel();}};</script>'
        )
    page = ('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Email preview</title></head><body style="margin:0;padding:12px;background:#f3f6f4">'
            '<p style="font:14px Arial"><a href="/subscribers">Back to subscribers</a></p>'
            f'<p style="font:14px Arial;color:#53645a">To: {safe(sub["email"])}<br>Subject: <b>{safe(subject)}</b></p>'
            + actions +
            f'<div style="background:#fff;border-radius:12px;padding:14px">{body}</div></body></html>')
    return Response(page, mimetype="text/html")


@app.route("/subscribers/<int:sub_id>/mark-sent", methods=["POST"])
def subscriber_mark_sent(sub_id):
    db = connect_db()
    try:
        db.execute("UPDATE subscribers SET last_sent_at = ? WHERE id = ?", (datetime.now(timezone.utc).isoformat(), sub_id))
        db.commit()
    finally:
        db.close()
    return redirect("/subscribers?sent=manual")


@app.route("/subscribers/<int:sub_id>/send", methods=["POST"])
def subscriber_send(sub_id):
    db = connect_db()
    try:
        sub = db.execute("SELECT * FROM subscribers WHERE id = ?", (sub_id,)).fetchone()
        if not sub:
            return Response("Subscriber not found", 404)
        try:
            outcome = send_digest(db, sub)
        except digest.DigestConfigError:
            outcome = "config"
        except Exception:
            app.logger.exception("Email send failed")
            outcome = "failed"
    finally:
        db.close()
    return redirect("/subscribers?sent=" + outcome)


@app.route("/subscribers", methods=["GET", "POST"])
def subscribers_page():
    db = connect_db()
    message = ""
    try:
        if request.method == "POST":
            values = _subscriber_form_values(request.form)
            if not values["name"] or not EMAIL_RE.match(values["email"]):
                message = '<p class="notice warn">Please enter a name and a valid email address.</p>'
            elif not values["planning_trades"] and not values["tender_trades"]:
                message = '<p class="notice warn">Pick at least one planning lead type or tender trade.</p>'
            else:
                full = full_places(db, values["planning_trades"], values["areas"])
                notes = values["notes"]
                if full:
                    notes = ("WAITING LIST: full for " + "; ".join(full) + ". " + notes).strip()
                db.execute(
                    "INSERT INTO subscribers (name,business,email,planning_trades,tender_trades,areas,monthly_fee,active,notes,team_size,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (values["name"], values["business"], values["email"], values["planning_trades"], values["tender_trades"],
                     values["areas"], values["monthly_fee"], 0 if full else 1, notes, values["team_size"],
                     datetime.now(timezone.utc).isoformat()),
                )
                db.commit()
                if full:
                    message = ('<p class="notice warn"><b>Added to the waiting list.</b> These already have '
                               f'{PLACES_PER_TRADE_AREA} firms: ' + safe("; ".join(full)) +
                               '. Untick those areas or trades to start them now, or wait for a place.</p>')
                else:
                    message = '<p class="notice ok">Subscriber added. Tap Preview to see their email.</p>'
        subs = db.execute("SELECT * FROM subscribers ORDER BY active DESC, name").fetchall()
        taken = places_taken(db)
    finally:
        db.close()

    sent = request.args.get("sent", "")
    message += {
        "sent": '<p class="notice ok">Email sent.</p>',
        "empty": '<p class="notice ok">Nothing matched this week, so no email was sent.</p>',
        "config": ('<p class="notice warn">Automatic sending isn\'t set up, so nothing was sent. No problem: tap '
                   '<b>Preview email</b>, then <b>Open in my email app to send</b>, and send it yourself.</p>'),
        "manual": '<p class="notice ok">Marked as sent this week.</p>',
        "full": ('<p class="notice warn">Can\'t resume: that trade and area already has the maximum number of firms. '
                 'It stays on the waiting list.</p>'),
        "failed": '<p class="notice warn">Sending failed. Check the email settings in Render and the service logs.</p>',
    }.get(sent, "")

    active = [s for s in subs if s["active"]]
    new_signups = sum(1 for s_ in subs if not s_["active"] and "NEW SIGN-UP" in (s_["notes"] or ""))
    income = 0.0
    for s_ in active:
        try:
            income += float(str(s_["monthly_fee"] or "0").replace("£", ""))
        except ValueError:
            pass
    cards = ""
    for s_ in subs:
        cards += (
            f'<article class="sub{"" if s_["active"] else " off"}"><h3>{safe(s_["name"])}'
            f'{" · " + safe(s_["business"]) if s_["business"] else ""}</h3>'
            f'<p class="meta">{safe(s_["email"])}{" · £" + safe(str(s_["monthly_fee"]).replace("£", "")) + "/month" if s_["monthly_fee"] else ""}'
            f' · {"Active" if s_["active"] else "Paused"}'
            f'{" · last email " + safe(display_date(s_["last_sent_at"])) if s_["last_sent_at"] else ""}</p>'
            f'<p><b>Planning:</b> {safe(s_["planning_trades"] or "none")}<br><b>Tenders:</b> {safe(s_["tender_trades"] or "none")}'
            f'<br><b>Areas:</b> {safe(s_["areas"] or "All six")}'
            f'<br><b>Team:</b> {safe(str(s_["team_size"]) + " tradespeople" if s_["team_size"] else "not given")}'
            f' · gets {safe(", ".join(sorted(planning.sizes_for_team(s_["team_size"]), key=planning.JOB_SIZES.index)).lower())} jobs</p>'
            + (f'<p class="meta"><b>{safe(s_["notes"][:200])}</b></p>' if ((s_["notes"] or "").startswith("WAITING LIST") or "NEW SIGN-UP" in (s_["notes"] or "")) else "")
            + ''
            f'<div class="row"><a class="btn" href="/subscribers/{int(s_["id"])}/preview">Preview email</a>'
            f'<form method="post" action="/subscribers/{int(s_["id"])}/send"><button>Send automatically</button></form>'
            f'<form method="post" action="/subscribers/{int(s_["id"])}/toggle"><button class="ghost">{"Pause" if s_["active"] else "Resume"}</button></form></div></article>'
        )
    if not cards:
        cards = '<p class="empty">No subscribers yet. Add your first tradesperson below.</p>'

    live_areas = [a for a in planning.AREAS if a not in ("Liverpool", "Wirral")]
    used_trades = [t for t in planning.TRADE_ORDER if any(taken.get((t, a)) for a in planning.AREAS)]
    places_html = ""
    if used_trades:
        rows_html = "".join(
            f"<li><b>{safe(t)}</b>: " + ", ".join(
                f"{safe(a)} {taken.get((t, a), 0)}/{PLACES_PER_TRADE_AREA}" for a in live_areas) + "</li>"
            for t in used_trades)
        places_html = (f'<details class="places"><summary>Places taken (max {PLACES_PER_TRADE_AREA} firms per trade per council)</summary>'
                       f'<ul>{rows_html}</ul></details>')

    def boxes(name, options, cols=""):
        return f'<div class="boxes {cols}">' + "".join(
            f'<label class="box"><input type="checkbox" name="{name}" value="{safe(o)}"> {safe(o)}</label>' for o in options) + "</div>"

    page = (
        '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Tender Scout — subscribers</title><style>'
        'body{font:16px Arial;max-width:960px;margin:0 auto;padding:16px;color:#18251e;background:#f3f6f4;line-height:1.45}'
        'header{background:#143d2a;color:white;border-radius:14px;padding:20px;margin-bottom:16px}header a{color:white}nav{font-size:14px}'
        'main{background:white;border-radius:14px;padding:18px}a{color:#08783c}'
        '.facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.facts p{margin:0;padding:12px;background:#f3f6f4;border-radius:10px}'
        '.facts b{display:block;font-size:24px}.sub{border:1px solid #d5e1d8;border-left:5px solid #168447;border-radius:10px;padding:14px;margin:12px 0;overflow-wrap:anywhere}'
        '.sub.off{border-left-color:#b8c6bd;opacity:.75}.sub h3{margin:0 0 4px}.meta{color:#53645a;font-size:14px;margin:0}'
        '.row{display:flex;flex-wrap:wrap;gap:8px}.row form{margin:0}button,.btn{display:inline-block;padding:11px 14px;background:#168447;color:#fff;border:0;border-radius:9px;font-weight:bold;font-size:15px;text-decoration:none;min-height:44px;box-sizing:border-box}'
        'button.ghost{background:#e8f2eb;color:#145c35}.btn{background:#e8f2eb;color:#145c35}'
        'label{display:block;margin:12px 0 4px;font-weight:600}input[type=text],input[type=email],textarea{box-sizing:border-box;width:100%;padding:12px;border:1px solid #b8c6bd;border-radius:8px;font:inherit}'
        '.boxes{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}.box{font-weight:normal;margin:0;padding:8px;background:#f7faf8;border:1px solid #dce5de;border-radius:8px}'
        '.notice{padding:12px;border-radius:9px}.notice.ok{background:#e8f2eb}.notice.warn{background:#fff9e8;border:1px solid #d8c28b}'
        'details summary{cursor:pointer;font-weight:bold;color:#145c35;padding:8px 0}.empty{color:#53645a;text-align:center;padding:16px}'
        '.add button{width:100%;margin-top:14px}@media(max-width:520px){body{padding:10px}header,main{padding:14px}.boxes{grid-template-columns:1fr}}'
        '</style></head><body>'
        '<header><nav><a href="/">Public tenders</a> · <a href="/planning">Planning leads</a> · <a href="/subscribers">Subscribers</a> · <a href="/leads">Private leads</a> · <a href="/sources">Sources</a></nav>'
        '<h1>Subscribers</h1><p>Tradespeople who get a weekly email of matching jobs, every Monday from 6am.</p></header><main>'
        + message
        + (f'<p class="notice warn"><b>{new_signups} new sign-up{"s" if new_signups != 1 else ""}</b> from your sign-up page. '
           'Check them below (marked NEW SIGN-UP), email them, then tap Resume to start their trial.</p>' if new_signups else "")
        + '<p class="meta">Your public sign-up page to share: <a href="/join">' + safe(request.host_url.rstrip("/")) + '/join</a></p>'
        f'<div class="facts"><p>Active subscribers<b>{len(active)}</b></p><p>Monthly income<b>£{income:,.0f}</b></p></div>'
        + places_html
        + cards +
        '<details class="add"' + ("" if subs else " open") + '><summary>+ Add a subscriber</summary><form method="post">'
        '<label for="s-name">Name</label><input type="text" id="s-name" name="name" required maxlength="120" placeholder="e.g. Dave Jones">'
        '<label for="s-biz">Business</label><input type="text" id="s-biz" name="business" maxlength="160" placeholder="e.g. DJ Building Services">'
        '<label for="s-email">Email</label><input type="email" id="s-email" name="email" required maxlength="200">'
        '<label>Planning leads they want</label>' + boxes("planning_trades", planning.TRADE_ORDER) +
        '<details><summary>Tender trades they want</summary>' + boxes("tender_trades", TRADE_CATEGORIES) + '</details>'
        '<label>Areas <span style="font-weight:normal;color:#53645a">(none ticked = all six)</span></label>' + boxes("areas", planning.AREAS) +
        '<label for="s-team">How many tradespeople in their team?</label><input type="text" id="s-team" name="team_size" inputmode="numeric" maxlength="5" placeholder="e.g. 3">'
        '<p class="meta">1-3 get small jobs only; 4-10 get small and medium; 11 or more get everything. Leave blank for small and medium.</p>'
        '<label for="s-fee">Monthly fee £</label><input type="text" id="s-fee" name="monthly_fee" inputmode="decimal" maxlength="20" placeholder="e.g. 20">'
        '<label for="s-notes">Notes</label><textarea id="s-notes" name="notes" rows="3"></textarea>'
        '<button type="submit">Add subscriber</button></form></details>'
        '<p class="meta">Easiest way: tap Preview email, then Open in my email app to send. Automatic Monday sending only works once SMTP_USER and SMTP_PASSWORD are set in Render.</p>'
        '</main></body></html>'
    )
    return Response(page, mimetype="text/html")


# ------------------------------------------------------- public sign-up --
# The only pages anyone can open without the dashboard login. Sign-ups are saved
# as PAUSED subscribers with a note, and nothing is emailed to them until you
# check them and tap Resume on the Subscribers page.

JOIN_TRADES = [t for t in planning.TRADE_ORDER]
JOIN_COMPANY_TYPES = ["Limited company", "Sole trader", "Partnership", "Other"]
_JOIN_HITS = {}
JOIN_LIMIT_PER_HOUR = 5


def _join_rate_ok(ip, now=None):
    import time as _t
    now = now or _t.time()
    hits = [t for t in _JOIN_HITS.get(ip, []) if now - t < 3600]
    if len(hits) >= JOIN_LIMIT_PER_HOUR:
        _JOIN_HITS[ip] = hits
        return False
    hits.append(now)
    _JOIN_HITS[ip] = hits
    return True


JOIN_CSS = (
    'body{font:17px Arial,sans-serif;max-width:640px;margin:0 auto;padding:16px;color:#18251e;background:#f3f6f4;line-height:1.5}'
    'header{background:#143d2a;color:#fff;border-radius:16px;padding:22px 20px;margin-bottom:16px}'
    'header h1{margin:6px 0 8px;font-size:28px;line-height:1.15}header p{margin:0;opacity:.92}'
    '.kick{font-size:13px;letter-spacing:.06em;text-transform:uppercase;opacity:.8}'
    'main{background:#fff;border-radius:16px;padding:20px;display:grid;gap:16px}'
    '.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}'
    '.stat{background:#e8f2eb;border-radius:12px;padding:12px}.stat b{display:block;font-size:26px;color:#145c35}'
    '.stat span{font-size:14px;color:#3d5246}'
    'ul.how{margin:0;padding-left:20px;display:grid;gap:6px}'
    'label{display:block;font-weight:bold;margin:0 0 6px}'
    'input[type=text],input[type=email],select{box-sizing:border-box;width:100%;padding:13px;border:1px solid #b8c6bd;border-radius:9px;font:inherit;background:#fff}'
    '.boxes{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}'
    '.box{font-weight:normal;margin:0;padding:10px;background:#f7faf8;border:1px solid #dce5de;border-radius:9px}'
    '.field{display:grid;gap:4px}.hint{font-size:14px;color:#53645a;margin:0}'
    'button{width:100%;padding:15px;background:#168447;color:#fff;border:0;border-radius:10px;font:bold 18px Arial,sans-serif;min-height:52px}'
    '.err{background:#fff0f0;color:#a11;padding:12px;border-radius:9px}.hp{position:absolute;left:-9999px}'
    '.small{font-size:13px;color:#53645a}'
    '@media(max-width:480px){.boxes{grid-template-columns:1fr}body{padding:10px}}'
)


def _join_stats(db):
    since = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    live = [a for a in planning.AREAS if a not in ("Liverpool", "Wirral")]
    marks = ",".join("?" for _ in live)
    rows = db.execute(f"SELECT trades FROM planning_leads WHERE start_date >= ? AND area IN ({marks})",
                      (since, *live)).fetchall()
    counts = {}
    for row in rows:
        for t in digest.split_list(row["trades"]):
            counts[t] = counts.get(t, 0) + 1
    return counts


@app.route("/join", methods=["GET", "POST"])
def join_page():
    error = ""
    form = request.form if request.method == "POST" else {}
    if request.method == "POST":
        if (form.get("website") or "").strip():           # hidden field only robots fill in
            return redirect("/join/thanks")
        ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?")
        name = (form.get("name") or "").strip()[:120]
        business = (form.get("business") or "").strip()[:160]
        email = (form.get("email") or "").strip()[:200]
        trade = form.get("trade") or ""
        areas = [a for a in request.form.getlist("areas") if a in planning.AREAS]
        company_type = form.get("company_type") or ""
        team = _int_or_none(form.get("team_size"))
        if not name or not business or not EMAIL_RE.match(email):
            error = "Please fill in your name, business name and a valid email address."
        elif trade not in JOIN_TRADES:
            error = "Please choose your trade."
        elif not areas:
            error = "Please tick at least one area you cover."
        elif company_type not in JOIN_COMPANY_TYPES:
            error = "Please choose your business type."
        elif form.get("consent") != "yes":
            error = "Please tick the box to say you're happy to receive the weekly email."
        elif not _join_rate_ok(ip):
            error = "Too many sign-ups from this connection. Please try again later."
        else:
            db = connect_db()
            try:
                full = full_places(db, trade, ", ".join(areas))
                note = (f"NEW SIGN-UP {datetime.now(timezone.utc).date().isoformat()}: check and tap Resume to start their trial. "
                        f"Business type: {company_type}.")
                if full:
                    note = "WAITING LIST: full for " + "; ".join(full) + ". " + note
                db.execute(
                    "INSERT INTO subscribers (name,business,email,planning_trades,tender_trades,areas,monthly_fee,active,notes,team_size,created_at) "
                    "VALUES (?,?,?,?,?,?,?,0,?,?,?)",
                    (name, business, email, trade, "", ", ".join(areas), "", note, team, datetime.now(timezone.utc).isoformat()),
                )
                db.commit()
            finally:
                db.close()
            return redirect("/join/thanks")

    db = connect_db()
    try:
        counts = _join_stats(db)
    finally:
        db.close()
    shown = [(t, counts.get(t, 0)) for t in ("Extensions and building work", "Trees and landscaping", "Roofing",
                                              "Roofline, gutters and exterior") if counts.get(t)]
    stats = ""
    if shown:
        stats = ('<section><p class="hint">New jobs in planning across Sefton, Knowsley, St Helens and Halton in the last 30 days:</p>'
                 '<div class="stats">' + "".join(f'<div class="stat"><b>{n}</b><span>{safe(t)}</span></div>' for t, n in shown)
                 + '</div></section>')

    def val(key):
        return safe(form.get(key) or "") if form else ""

    trade_opts = '<option value="">Choose your trade</option>' + "".join(
        f'<option value="{safe(t)}"{" selected" if form and form.get("trade") == t else ""}>{safe(t)}</option>' for t in JOIN_TRADES)
    type_opts = '<option value="">Choose one</option>' + "".join(
        f'<option{" selected" if form and form.get("company_type") == t else ""}>{safe(t)}</option>' for t in JOIN_COMPANY_TYPES)
    ticked = set(request.form.getlist("areas")) if form else set()
    area_boxes = "".join(
        f'<label class="box"><input type="checkbox" name="areas" value="{safe(a)}"{" checked" if a in ticked else ""}> {safe(a)}'
        + (' <span class="small">(waiting list)</span>' if a in ("Liverpool", "Wirral") else "") + '</label>'
        for a in planning.AREAS)

    page = (
        '<!doctype html><html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Merseyside Trade Leads: free 4-week trial</title>'
        '<meta name="description" content="Every Monday, the planned building and tree work near you, straight from council planning applications.">'
        f'<style>{JOIN_CSS}</style></head><body>'
        '<header><div class="kick">Merseyside Trade Leads</div>'
        '<h1>Every Monday, the planned jobs near you, before they go out to quote.</h1>'
        '<p>Extensions, lofts, roofs, new homes and tree work, straight from council planning applications, plus council contracts for your trade.</p></header>'
        '<main>' + stats +
        '<section><ul class="how">'
        '<li><b>One email a week</b>, sorted for your trade and area.</li>'
        '<li><b>Only jobs your team can take on</b>: small firms get household jobs, bigger firms get bigger sites.</li>'
        '<li><b>Limited places</b>: no more than 5 firms per trade in each council area.</li>'
        '<li><b>Free for 4 weeks.</b> No card needed. Stop any time.</li>'
        '</ul></section>'
        + (f'<p class="err" role="alert">{safe(error)}</p>' if error else "") +
        '<form method="post" action="/join" style="display:grid;gap:14px">'
        f'<div class="field"><label for="j-name">Your name</label><input type="text" id="j-name" name="name" required maxlength="120" value="{val("name")}" autocomplete="name"></div>'
        f'<div class="field"><label for="j-biz">Business name</label><input type="text" id="j-biz" name="business" required maxlength="160" value="{val("business")}" autocomplete="organization"></div>'
        f'<div class="field"><label for="j-email">Email</label><input type="email" id="j-email" name="email" required maxlength="200" value="{val("email")}" autocomplete="email"></div>'
        f'<div class="field"><label for="j-trade">Your trade</label><select id="j-trade" name="trade" required>{trade_opts}</select></div>'
        f'<div class="field"><label>Areas you cover</label><div class="boxes">{area_boxes}</div></div>'
        f'<div class="field"><label for="j-team">How many tradespeople in your team?</label><input type="text" id="j-team" name="team_size" inputmode="numeric" maxlength="5" value="{val("team_size")}" placeholder="e.g. 3"></div>'
        f'<div class="field"><label for="j-type">Business type</label><select id="j-type" name="company_type" required>{type_opts}</select></div>'
        '<div class="hp" aria-hidden="true"><label for="j-web">Leave this empty</label><input type="text" id="j-web" name="website" tabindex="-1" autocomplete="off"></div>'
        '<label class="box"><input type="checkbox" name="consent" value="yes" required> I\'m happy to receive a weekly email of job leads. I can stop any time by replying "stop".</label>'
        '<button type="submit">Start my free 4-week trial</button>'
        '</form>'
        '<p class="small">We use your details only to send your weekly leads and to contact you about your trial. '
        'Planning addresses are public records: contact homeowners by letter or leaflet only, never by cold call or email.</p>'
        '</main></body></html>'
    )
    return Response(page, mimetype="text/html")


@app.route("/join/thanks")
def join_thanks():
    page = (
        '<!doctype html><html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>Thanks: Merseyside Trade Leads</title><style>{JOIN_CSS}</style></head><body>'
        '<header><div class="kick">Merseyside Trade Leads</div><h1>Thanks, you\'re on the list.</h1>'
        '<p>We\'ll email you within 2 working days to start your free 4-week trial.</p></header>'
        '<main><p>Your first email will list the planned jobs for your trade in your area. If your area is full, '
        'we\'ll tell you and keep your place on the waiting list.</p>'
        '<p class="small">Questions? Just reply to any of our emails.</p></main></body></html>'
    )
    return Response(page, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
