import csv
import html
import io
import os
import sqlite3
from datetime import datetime, timezone

from flask import Flask, Response, request, redirect
import tenderscout as collector

app = Flask(__name__)
DB_PATH = os.environ.get("TENDERSCOUT_DB", "tenderscout.db")


def connect_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("""
        CREATE TABLE IF NOT EXISTS tenders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT, title TEXT, buyer TEXT,
            published_date TEXT, location_text TEXT,
            service_match TEXT, score INTEGER,
            deadline TEXT, notice_url TEXT UNIQUE,
            geography_reason TEXT, saved_at TEXT,
            pipeline_status TEXT DEFAULT 'Reviewing'
        )
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(tenders)").fetchall()}
    if "pipeline_status" not in columns:
        db.execute("ALTER TABLE tenders ADD COLUMN pipeline_status TEXT DEFAULT 'Reviewing'")
    return db


def safe(value):
    return html.escape(str(value or ""))


def get_tenders():
    db = connect_db()
    rows = db.execute(
        "SELECT * FROM tenders ORDER BY score DESC, published_date DESC"
    ).fetchall()
    db.close()
    return rows


@app.route("/health")
def health():
    try:
        db = connect_db()
        db.execute("SELECT 1")
        db.close()
        return {"status": "ok", "database": "ok"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}, 500


@app.route("/", methods=["GET"])
def dashboard():
    rows = get_tenders()
    cards = ""
    for row in rows:
        link = safe(row["notice_url"])
        title = safe(row["title"])
        buyer = safe(row["buyer"])
        location = safe(row["location_text"])
        deadline = safe(row["deadline"])
        source = safe(row["source"])
        service = safe(row["service_match"])
        reason = safe(row["geography_reason"])
        score = safe(row["score"])
        pipeline = safe(row["pipeline_status"] or "Reviewing")
        cards += f"""
        <article class="card">
          <h2>{title or "Untitled tender"}</h2>
          <p><b>Buyer:</b> {buyer or "Not listed"}</p>
          <p><b>Service:</b> {service} · <b>Score:</b> {score}</p>
          <p><b>Location:</b> {location or "Needs checking"}</p>
          <p><b>Deadline:</b> {deadline or "Check official notice"}</p>
          <p><b>Source:</b> {source}</p>
          <p><b>Pipeline:</b> {pipeline}</p>
          <p>{reason}</p>
          {f'<a href="{link}" target="_blank" rel="noopener">View official notice</a>' if link.startswith("https://") else ""}
        </article>
        """

    if not cards:
        cards = "<p>No tenders saved yet. Press Refresh tenders to search.</p>"

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
    <p>UK public tender opportunities</p>
    <form method="post" action="/refresh">
    <button type="submit">Refresh tenders</button></form></header>
    <p>{len(rows)} saved opportunities</p>
    <p class="note">Always verify eligibility, location, deadline and contract details
    on the official notice.</p>
    {cards}</body></html>"""
    return Response(page, mimetype="text/html")


@app.route("/refresh", methods=["POST"])
def refresh():
    try:
        records = (
            collector.fetch_fts(14, 3)
            + collector.fetch_contracts_finder(14, 3)
        )
        records = collector.dedupe(records)
        db = connect_db()
        saved = 0

        for record in records:
            match = collector.score_record(
                record, ["cleaning", "fm"], "North West", None, None
            )
            if not match or match.get("score", 0) < 3:
                continue

            url = match.get("notice_url") or ""
            if not url:
                url = (
                    "no-url:"
                    + str(match.get("source", ""))
                    + ":"
                    + str(match.get("ocid") or match.get("title", ""))
                    + ":"
                    + str(match.get("published_date", ""))
                )

            db.execute(
                """
                INSERT INTO tenders
                (source,title,buyer,published_date,location_text,
                 service_match,score,deadline,notice_url,
                 geography_reason,saved_at,pipeline_status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(notice_url) DO UPDATE SET
                title=excluded.title,buyer=excluded.buyer,
                score=excluded.score,deadline=excluded.deadline,
                location_text=excluded.location_text,
                geography_reason=excluded.geography_reason,
                saved_at=excluded.saved_at,
                pipeline_status=excluded.pipeline_status
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
                    datetime.now(timezone.utc).isoformat(),
                    "Reviewing",
                ),
            )
            saved += 1

        db.commit()
        db.close()
        return Response(
            f"Refresh complete. {saved} matching records processed. "
            '<p><a href="/">Return to dashboard</a></p>',
            mimetype="text/html",
        )
    except Exception as exc:
        app.logger.exception("Tender refresh failed")
        return Response(
            "Refresh failed: " + safe(exc)
            + '<p><a href="/">Return to dashboard</a></p>',
            status=500,
            mimetype="text/html",
        )


@app.route("/add", methods=["POST"])
def add_tender():
    title = (request.form.get("title") or "").strip()
    buyer = request.form.get("buyer") or ""
    notice_url = request.form.get("notice_url") or "https://example.com/notice"
    pipeline_status = request.form.get("pipeline_status") or "Reviewing"

    if not title:
        title = "Untitled tender"

    db = connect_db()
    db.execute(
        """
        INSERT INTO tenders
        (source, title, buyer, published_date, location_text,
         service_match, score, deadline, notice_url,
         geography_reason, saved_at, pipeline_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(notice_url) DO UPDATE SET
            title=excluded.title,
            buyer=excluded.buyer,
            geography_reason=excluded.geography_reason,
            pipeline_status=excluded.pipeline_status,
            saved_at=excluded.saved_at
        """,
        (
            "Manual",
            title,
            buyer,
            datetime.now(timezone.utc).isoformat(),
            "",
            "",
            0,
            "",
            notice_url,
            "Manual entry",
            datetime.now(timezone.utc).isoformat(),
            pipeline_status,
        ),
    )
    db.commit()
    db.close()
    return redirect("/")


@app.route("/export.csv")
def export_csv():
    db = connect_db()
    rows = db.execute(
        "SELECT title, buyer, location_text, deadline, notice_url, source, pipeline_status FROM tenders ORDER BY saved_at DESC"
    ).fetchall()
    db.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["title", "buyer", "location_text", "deadline", "notice_url", "source", "pipeline_status"])
    for row in rows:
        writer.writerow([
            row["title"],
            row["buyer"],
            row["location_text"],
            row["deadline"],
            row["notice_url"],
            row["source"],
            row["pipeline_status"],
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=tenderscout_export.csv"},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
