import csv
import hmac
import html
import io
import os
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse

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
    published_date TEXT, location_text TEXT,
    service_match TEXT, score INTEGER,
    deadline TEXT, notice_url TEXT UNIQUE,
    geography_reason TEXT,
    region_match TEXT DEFAULT 'REVIEW',
    trade_category TEXT DEFAULT 'Other / unclassified',
    saved_at TEXT,
    pipeline_status TEXT DEFAULT 'Reviewing'
"""
# Columns added after the first release; added to older databases on start-up.
# Older rows default to REVIEW because their location was never confirmed.
ADDED_COLUMNS = [
    ("pipeline_status", "TEXT DEFAULT 'Reviewing'"),
    ("region_match", "TEXT DEFAULT 'REVIEW'"),
]


def ensure_schema(db):
    global _schema_ready
    if _schema_ready:
        return
    id_column = "SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
    db.execute(f"CREATE TABLE IF NOT EXISTS tenders (id {id_column}, {COLUMNS})")
    if USE_POSTGRES:
        db.execute("ALTER TABLE tenders ADD COLUMN IF NOT EXISTS trade_category TEXT DEFAULT 'Other / unclassified'")
    else:
        existing_columns = {row[1] for row in db.execute("PRAGMA table_info(tenders)").fetchall()}
        if "trade_category" not in existing_columns:
            db.execute("ALTER TABLE tenders ADD COLUMN trade_category TEXT DEFAULT 'Other / unclassified'")
    db.execute(f"""CREATE TABLE IF NOT EXISTS private_leads (
        id {id_column},
        title TEXT NOT NULL,
        trade_category TEXT NOT NULL,
        location_text TEXT NOT NULL,
        source_name TEXT NOT NULL,
        source_url TEXT,
        contact_details TEXT,
        details TEXT,
        status TEXT DEFAULT 'New',
        created_at TEXT NOT NULL
    )""")
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
    """Basic auth for everything except /health, once both variables are set."""
    if request.path == "/health":
        return None
    user = os.environ.get("DASHBOARD_USER", "")
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not (user and password):
        return None
    auth = request.authorization
    if (
        auth
        and hmac.compare_digest((auth.username or "").encode(), user.encode())
        and hmac.compare_digest((auth.password or "").encode(), password.encode())
    ):
        return None
    return Response(
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="TenderScout"'},
    )


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
    counts = {"main": 0, "review": 0}
    for row in db.execute(
        "SELECT region_match, COUNT(*) AS n FROM tenders GROUP BY region_match"
    ).fetchall():
        key = "review" if row["region_match"] == "REVIEW" else "main"
        counts[key] += row["n"]
    return counts


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


def dashboard_url(view="", service=""):
    params = {k: v for k, v in (("view", view), ("service", service)) if v}
    return "/?" + urlencode(params) if params else "/"


@app.route("/", methods=["GET"])
def dashboard():
    review_view = request.args.get("view") == "review"
    trade = request.args.get("trade", "")
    trade = trade if trade in TRADE_CATEGORIES else ""
    service = request.args.get("service", "")
    service = service if service in SERVICE_FILTERS else ""
    view = "review" if review_view else ""

    db = connect_db()
    condition = (
        "region_match = 'REVIEW'" if review_view
        else "region_match IN ('LOCAL', 'MANUAL')"
    )
    params = ()
    if trade:
        condition += " AND trade_category = ?"
        params = (trade,)
    elif service:
        condition += " AND service_match = ?"
        params = (SERVICE_FILTERS[service],)
    rows = db.execute(
        f"SELECT * FROM tenders WHERE {condition} "
        "ORDER BY score DESC, published_date DESC",
        params,
    ).fetchall()
    counts = count_by_region(db)
    db.close()

    # Open tenders first; closed ones sink to the bottom (order is otherwise kept).
    decorated = [(row, *deadline_info(row["deadline"])) for row in rows]
    decorated.sort(key=lambda item: item[3])

    cards = ""
    for row, deadline_label, deadline_class, _closed in decorated:
        link = safe(row["notice_url"])
        notice = (
            f'<a href="{link}" target="_blank" rel="noopener">View official notice</a>'
            if link.startswith("https://") else ""
        )
        current = row["pipeline_status"] or "Reviewing"
        options = "".join(
            f'<option{" selected" if stage == current else ""}>{safe(stage)}</option>'
            for stage in STAGES
        )
        cards += f"""
        <article class="card">
          <h2>{safe(row["title"]) or "Untitled tender"}</h2>
          <p class="deadline {deadline_class}">{safe(deadline_label)}</p>
          <p><b>Buyer:</b> {safe(row["buyer"]) or "Not listed"}</p>
          <p><b>Trade:</b> {safe(row["trade_category"] or row["service_match"])} · <b>Score:</b> {safe(row["score"])}</p>
          <p><b>Location:</b> {safe(row["location_text"]) or "Needs checking"}</p>
          <p><b>Source:</b> {safe(row["source"])}</p>
          <p>{safe(row["geography_reason"])}</p>
          <form method="post" action="/tender/{int(row["id"])}/stage" class="stage">
            <input type="hidden" name="view" value="{view}">
            <input type="hidden" name="service" value="{service}">
            <label><b>Stage:</b>
              <select name="stage" onchange="this.form.submit()">{options}</select>
            </label>
            <noscript><button type="submit">Save</button></noscript>
          </form>
          {notice}
        </article>
        """

    if not cards:
        cards = (
            "<p>Nothing to check right now.</p>" if review_view
            else "<p>No tenders saved yet. Press Refresh tenders to search.</p>"
        )

    if review_view:
        heading = f"{len(rows)} tenders need a location check"
        switch = f'<a href="{dashboard_url("", service)}">Back to confirmed tenders ({counts["main"]})</a>'
    else:
        heading = f"{len(rows)} saved opportunities"
        switch = f'<a href="{dashboard_url("review", service)}">Needs a location check ({counts["review"]})</a>'

    selected_trade = trade
    filters = "".join(
        f'<option value="{safe(category)}"{" selected" if category == selected_trade else ""}>{safe(category)}</option>'
        for category in TRADE_CATEGORIES
    )
    filter_form = (
        '<form method="get" action="/" style="margin:12px 0">'
        + (f'<input type="hidden" name="view" value="{view}">' if view else '')
        + '<label for="trade-filter"><b>Filter by trade (71 categories):</b></label> '
        + '<select id="trade-filter" name="trade" onchange="this.form.submit()" style="max-width:100%;padding:10px;font-size:16px">'
        + '<option value="">All trades</option>' + filters + '</select>'
        + '<noscript><button type="submit">Apply filter</button></noscript></form>'
    )

    banner = ""
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

    page = f"""<!doctype html>
    <html><head><meta name="viewport" content="width=device-width">
    <title>Tender Scout</title>
    <style>
    body{{font:16px Arial;background:#f3f6f4;color:#18251e;
    margin:0;padding:20px}}
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
    padding:12px 14px;margin:14px 0}}
    .deadline{{font-weight:bold;color:#18251e}}
    .deadline.soon{{color:#b45309}}
    .deadline.closed{{color:#8a8f8c}}
    .stage select{{font-size:16px;padding:8px;margin-left:6px}}
    a{{color:#08783c}}.note{{font-size:13px}}
    </style></head><body>
    <header><h1>Tender Scout</h1>
    <p><a style="color:white" href="/">Public tenders</a> · <a style="color:white" href="/leads">Private trade leads</a> · <a style="color:white" href="/sources">Council & community sources</a></p>
    <p>Liverpool, Sefton, Wirral, St Helens, Halton, Knowsley, Warrington, Wigan, West Lancashire and Cheshire West</p>
    <form method="post" action="/refresh"
      onsubmit="var b=this.querySelector('button');b.disabled=true;b.textContent='Searching... this can take a minute';">
    <button type="submit" class="btn">&#8635; Refresh tenders</button></form></header>
    {banner}
    <p>{heading} · {switch} · <a href="/export.csv">Export CSV</a></p>
    {filter_form}
    <p class="note">Always verify eligibility, location, deadline and contract details
    on the official notice.</p>
    {cards}</body></html>"""
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
    fts_stats, cf_stats = {}, {}
    # Both feeds are read at the same time, so a refresh takes about as long as
    # the slower one.
    with ThreadPoolExecutor(max_workers=2) as pool:
        fts_job = pool.submit(collector.fetch_fts, days, pages, limit, fts_stats)
        cf_job = pool.submit(collector.fetch_contracts_finder, days, pages, limit, cf_stats)
        records = collector.dedupe(fts_job.result() + cf_job.result())
    LAST_FEEDS["Find a Tender"] = fts_stats
    LAST_FEEDS["Contracts Finder"] = cf_stats
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
            if not match or match.get("score", 0) < MIN_SCORE:
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
                (source, title, buyer, published_date, location_text,
                 service_match, score, deadline, notice_url,
                 geography_reason, region_match, trade_category, saved_at, pipeline_status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(notice_url) DO UPDATE SET
                    title=excluded.title, buyer=excluded.buyer,
                    score=excluded.score, deadline=excluded.deadline,
                    location_text=excluded.location_text,
                    geography_reason=excluded.geography_reason,
                    region_match=excluded.region_match,
                    trade_category=excluded.trade_category,
                    saved_at=excluded.saved_at
                """,
                (
                    match.get("source", ""),
                    match.get("title", ""),
                    match.get("buyer", ""),
                    match.get("published_date", ""),
                    match.get("location_text", ""),
                    match.get("service_match", ""),
                    match.get("score", 0),
                    match.get("deadline", ""),
                    url,
                    match.get("geography_reason", ""),
                    region_match,
                    match.get("trade_category", "Other / unclassified"),
                    datetime.now(timezone.utc).isoformat(),
                    "Reviewing",
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
        if st.get("error"):
            text += f". Problem: {st['error']}"
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
    stats, error = refresh_or_error()
    if error:
        return error
    query = urlencode({
        "done": 1, "n": stats["fetched"], "s": stats["service"],
        "o": stats["outside"], "l": stats["local"], "r": stats["review"],
    })
    return redirect("/?" + query)


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Plain-text version for scheduled jobs."""
    stats, error = refresh_or_error()
    if error:
        return error
    return Response("Refresh complete. " + summary_text(stats), mimetype="text/plain")


@app.route("/add", methods=["POST"])
def add_tender():
    title = (request.form.get("title") or "").strip() or "Untitled tender"
    buyer = (request.form.get("buyer") or "").strip()
    pipeline_status = (request.form.get("pipeline_status") or "").strip() or "Reviewing"
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
             geography_reason, region_match, saved_at, pipeline_status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(notice_url) DO UPDATE SET
                title=excluded.title,
                buyer=excluded.buyer,
                geography_reason=excluded.geography_reason,
                pipeline_status=excluded.pipeline_status,
                saved_at=excluded.saved_at
            """,
            (
                "Manual", title, buyer, now, "", "", 0, "", notice_url,
                "Manual entry", "MANUAL", now, pipeline_status,
            ),
        )
        db.commit()
    finally:
        db.close()
    return redirect("/")


EXPORT_FIELDS = [
    "title", "buyer", "location_text", "trade_category", "deadline", "notice_url",
    "source", "region_match", "pipeline_status",
]


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



TRADE_CATEGORIES = list(collector.TRADE_CATEGORIES)
LEAD_STATUSES = ["New", "Contacted", "Qualified", "Passed to trade", "Won", "Closed"]


@app.route("/sources")
def sources():
    """Directory of official procurement portals and public community channels."""
    items = [
        ("Public-sector tenders", [
            ("Find a Tender", "https://www.find-tender.service.gov.uk/Search", "UK government procurement notices"),
            ("Contracts Finder", "https://www.contractsfinder.service.gov.uk/Search", "UK public-sector opportunities"),
            ("Liverpool City Council — ProContract", "https://procontract.due-north.com/", "Liverpool Council says most of its opportunities are advertised through ProContract; some routes use other approved portals"),
            ("The Chest", "https://www.the-chest.org.uk/", "North West local-authority procurement portal"),
            ("Wirral Council tender guidance", "https://www.wirral.gov.uk/business/tenders-and-contracts", "Wirral directs suppliers to The Chest"),
        ]),
        ("Community and private-lead channels", [
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
        '<title>Tender Scout sources</title><style>body{font:16px Arial;max-width:900px;margin:20px auto;padding:0 16px;color:#18251e}'
        'li{margin:12px 0}a{color:#08783c}p{line-height:1.5}</style></head><body>'
        '<h1>Council & community sources</h1><p>Public contracts and private customer leads are different opportunity types. '
        'Council portals may require supplier registration and may not expose an open feed.</p>'
        + sections +
        '<p><b>Important:</b> Tender Scout does not automatically scrape Facebook, Nextdoor or gated supplier portals. '
        'Add a private lead only when you have permission to use the information and a legitimate source link.</p>'
        '<p><a href="/">Back to public tenders</a> · <a href="/leads">Open private leads</a></p></body></html>',
        mimetype="text/html",
    )


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
        if not title or trade not in TRADE_CATEGORIES or not location or not source_name:
            message = '<p style="color:#a11">Please enter a title, valid trade, location and source.</p>'
        elif source_url and not source_url.startswith(("https://", "http://")):
            message = '<p style="color:#a11">Source link must start with http:// or https://.</p>'
        else:
            db.execute(
                "INSERT INTO private_leads (title,trade_category,location_text,source_name,source_url,contact_details,details,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (title, trade, location, source_name, source_url, contact, details, "New",
                 datetime.now(timezone.utc).isoformat()),
            )
            db.commit()
            message = '<p style="color:#08783c"><b>Private lead saved.</b></p>'
    rows = db.execute("SELECT * FROM private_leads ORDER BY created_at DESC LIMIT 100").fetchall()
    db.close()
    options = "".join(f'<option value="{safe(t)}">{safe(t)}</option>' for t in TRADE_CATEGORIES)
    cards = ""
    for row in rows:
        link = (f'<a href="{safe(row["source_url"])}" target="_blank" rel="noopener">Original source</a>'
                if row["source_url"] and row["source_url"].startswith(("https://", "http://")) else "")
        cards += (
            f'<article><h3>{safe(row["title"])}</h3><p><b>Trade:</b> {safe(row["trade_category"])} · '
            f'<b>Location:</b> {safe(row["location_text"])}</p><p><b>Source:</b> {safe(row["source_name"])} {link}</p>'
            f'<p><b>Status:</b> {safe(row["status"])} · <b>Added:</b> {safe(row["created_at"][:10])}</p>'
            f'<p>{safe(row["details"])}</p></article>'
        )
    if not cards:
        cards = "<p>No private leads have been entered yet. This list is separate from public-sector tenders.</p>"
    page = (
        '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Tender Scout — private trade leads</title><style>body{font:16px Arial;max-width:900px;margin:20px auto;padding:0 16px;color:#18251e}'
        'label{display:block;margin:10px 0}input,select,textarea{box-sizing:border-box;width:100%;padding:10px;font:inherit}'
        'button{padding:12px 18px;background:#168447;color:white;border:0;border-radius:8px;font-weight:bold}'
        'article{background:#f3f6f4;border-radius:8px;padding:12px;margin:12px 0}a{color:#08783c}</style></head><body>'
        '<h1>Private trade leads</h1><p>Manually capture genuine customer enquiries from permitted public sources. '
        'Do not enter private or sensitive personal information unless you have a lawful basis to do so.</p>'
        + message +
        '<form method="post"><label>Job / enquiry title<input name="title" required maxlength="250"></label>'
        '<label>Trade category<select name="trade_category" required><option value="">Choose a trade</option>' + options + '</select></label>'
        '<label>Location<input name="location_text" required maxlength="200" placeholder="e.g. Liverpool L15"></label>'
        '<label>Source name<input name="source_name" required maxlength="200" placeholder="e.g. local community group"></label>'
        '<label>Original source URL<input name="source_url" type="url" maxlength="1000"></label>'
        '<label>Business contact details (only if publicly provided for this enquiry)<input name="contact_details" maxlength="500"></label>'
        '<label>Job details<textarea name="details" rows="3" maxlength="4000"></textarea></label>'
        '<button type="submit">Save private lead</button></form><hr><h2>Recent private leads</h2>' + cards +
        '<p><a href="/">Public tenders</a> · <a href="/sources">Council & community sources</a></p></body></html>'
    )
    return Response(page, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
