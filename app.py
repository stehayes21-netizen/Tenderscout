import csv
import hmac
import html
import io
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from flask import Flask, Response, redirect, request

import tenderscout as collector

app = Flask(__name__)

# Storage: PostgreSQL when DATABASE_URL is set, otherwise a local SQLite file.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))
DB_PATH = os.environ.get("TENDERSCOUT_DB", "tenderscout.db")

MIN_SCORE = 3
SERVICES = ["cleaning", "fm"]

_schema_ready = False


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


@app.route("/", methods=["GET"])
def dashboard():
    review_view = request.args.get("view") == "review"
    db = connect_db()
    condition = (
        "region_match = 'REVIEW'" if review_view
        else "region_match IN ('LOCAL', 'MANUAL')"
    )
    rows = db.execute(
        f"SELECT * FROM tenders WHERE {condition} "
        "ORDER BY score DESC, published_date DESC"
    ).fetchall()
    counts = count_by_region(db)
    db.close()

    cards = ""
    for row in rows:
        link = safe(row["notice_url"])
        notice = (
            f'<a href="{link}" target="_blank" rel="noopener">View official notice</a>'
            if link.startswith("https://") else ""
        )
        cards += f"""
        <article class="card">
          <h2>{safe(row["title"]) or "Untitled tender"}</h2>
          <p><b>Buyer:</b> {safe(row["buyer"]) or "Not listed"}</p>
          <p><b>Service:</b> {safe(row["service_match"])} · <b>Score:</b> {safe(row["score"])}</p>
          <p><b>Location:</b> {safe(row["location_text"]) or "Needs checking"}</p>
          <p><b>Deadline:</b> {safe(row["deadline"]) or "Check official notice"}</p>
          <p><b>Source:</b> {safe(row["source"])}</p>
          <p><b>Pipeline:</b> {safe(row["pipeline_status"] or "Reviewing")}</p>
          <p>{safe(row["geography_reason"])}</p>
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
        switch = f'<a href="/">Back to confirmed tenders ({counts["main"]})</a>'
    else:
        heading = f"{len(rows)} saved opportunities"
        switch = f'<a href="/?view=review">Needs a location check ({counts["review"]})</a>'

    page = f"""<!doctype html>
    <html><head><meta name="viewport" content="width=device-width">
    <title>Tender Scout</title>
    <style>
    body{{font:16px Arial;background:#f3f6f4;color:#18251e;
    margin:0;padding:20px}}
    header{{background:#143d2a;color:white;padding:22px;border-radius:12px}}
    button{{background:#168447;color:white;border:0;padding:13px 18px;
    border-radius:8px;font-size:16px}}
    .card{{background:white;margin:16px 0;padding:18px;
    border-radius:10px;overflow-wrap:anywhere}}
    a{{color:#08783c}}.note{{font-size:13px}}
    </style></head><body>
    <header><h1>Tender Scout</h1>
    <p>Liverpool, Sefton, Wirral, St Helens and Halton</p>
    <form method="post" action="/refresh">
    <button type="submit">Refresh tenders</button></form></header>
    <p>{heading} · {switch} · <a href="/export.csv">Export CSV</a></p>
    <p class="note">Always verify eligibility, location, deadline and contract details
    on the official notice.</p>
    {cards}</body></html>"""
    return Response(page, mimetype="text/html")


def run_refresh():
    """Fetch official feeds and save matching tenders. Returns (fetched, local, review)."""
    days = env_int("TENDERSCOUT_DAYS", 14)
    pages = env_int("TENDERSCOUT_PAGES", 3)
    records = collector.dedupe(
        collector.fetch_fts(days, pages) + collector.fetch_contracts_finder(days, pages)
    )
    local = review = 0
    if not records:
        return 0, local, review

    db = connect_db()
    try:
        for record in records:
            match = collector.score_record(
                record, SERVICES, collector.TARGET_AREA_LABEL, None, None
            )
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
                 geography_reason, region_match, saved_at, pipeline_status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(notice_url) DO UPDATE SET
                    title=excluded.title, buyer=excluded.buyer,
                    score=excluded.score, deadline=excluded.deadline,
                    location_text=excluded.location_text,
                    geography_reason=excluded.geography_reason,
                    region_match=excluded.region_match,
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
                    datetime.now(timezone.utc).isoformat(),
                    "Reviewing",
                ),
            )
            if region_match == "LOCAL":
                local += 1
            else:
                review += 1
        db.commit()
    finally:
        db.close()
    return len(records), local, review


@app.route("/refresh", methods=["POST"])
@app.route("/api/refresh", methods=["POST"])
def refresh():
    try:
        fetched, local, review = run_refresh()
    except Exception:
        app.logger.exception("Tender refresh failed")
        return Response(
            "Refresh failed. Check the service logs."
            '<p><a href="/">Return to dashboard</a></p>',
            status=500,
            mimetype="text/html",
        )

    if fetched == 0:
        # A failed or empty feed must never be mistaken for "no tenders".
        return Response(
            "No records came back from the official feeds. A source may be down, "
            "so this does not mean there are no tenders."
            '<p><a href="/">Return to dashboard</a></p>',
            status=502,
            mimetype="text/html",
        )

    return Response(
        f"Refresh complete. {fetched} notices checked: {local} confirmed in the "
        f"target area, {review} need a location check."
        '<p><a href="/">Return to dashboard</a></p>',
        mimetype="text/html",
    )


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
    "title", "buyer", "location_text", "deadline", "notice_url",
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
