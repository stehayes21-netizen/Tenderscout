#!/usr/bin/env python3
"""Restore a TenderScout JSON export to a NEW local SQLite database.

This utility never overwrites an existing destination and never connects to a
network service. Keep the original backup unchanged.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

FORMAT = "tenderscout-database-backup"
TABLES = {
    "tenders": [
        ("id", "INTEGER PRIMARY KEY"),
        ("source", "TEXT"), ("title", "TEXT"), ("buyer", "TEXT"),
        ("published_date", "TEXT"), ("location_text", "TEXT"), ("description", "TEXT"),
        ("estimated_value_gbp", "TEXT"), ("contract_start_date", "TEXT"),
        ("contract_end_date", "TEXT"), ("contact_name", "TEXT"), ("contact_email", "TEXT"),
        ("contact_phone", "TEXT"), ("eligibility_text", "TEXT"), ("service_match", "TEXT"),
        ("score", "INTEGER"), ("deadline", "TEXT"), ("notice_url", "TEXT UNIQUE"),
        ("geography_reason", "TEXT"), ("region_match", "TEXT DEFAULT 'REVIEW'"),
        ("trade_category", "TEXT DEFAULT 'Other / unclassified'"), ("saved_at", "TEXT"),
        ("pipeline_status", "TEXT DEFAULT 'Reviewing'"),
        ("opportunity_stage", "TEXT DEFAULT 'Other notice'"),
    ],
    "private_leads": [
        ("id", "INTEGER PRIMARY KEY"), ("title", "TEXT NOT NULL"),
        ("trade_category", "TEXT NOT NULL"), ("location_text", "TEXT NOT NULL"),
        ("source_name", "TEXT NOT NULL"), ("source_url", "TEXT"),
        ("contact_details", "TEXT"), ("details", "TEXT"),
        ("urgency", "TEXT DEFAULT 'Normal'"), ("status", "TEXT DEFAULT 'New'"),
        ("created_at", "TEXT NOT NULL"),
    ],
    "external_projects": [
        ("id", "INTEGER PRIMARY KEY"), ("title", "TEXT NOT NULL"),
        ("trade_category", "TEXT NOT NULL"), ("location_text", "TEXT NOT NULL"),
        ("details", "TEXT"), ("source_name", "TEXT NOT NULL"),
        ("source_url", "TEXT NOT NULL UNIQUE"), ("published_date", "TEXT"),
        ("location_status", "TEXT NOT NULL DEFAULT 'Potential local — verify'"),
        ("imported_at", "TEXT NOT NULL"),
    ],
}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", help="path to tenderscout_database_backup.json")
    parser.add_argument("destination", help="new SQLite database file path; must not already exist")
    args = parser.parse_args()
    backup_path, destination = Path(args.backup), Path(args.destination)

    if destination.exists():
        parser.error(f"destination already exists; refusing to overwrite: {destination}")
    try:
        payload = json.loads(backup_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        parser.error(f"cannot read valid JSON backup: {exc}")
    if not isinstance(payload, dict) or payload.get("format") != FORMAT or payload.get("format_version") != 1:
        parser.error("unsupported backup format or version")
    data = payload.get("tables")
    if not isinstance(data, dict) or set(data) != set(TABLES):
        parser.error("backup must contain exactly tenders, private_leads, and external_projects")
    for table, rows in data.items():
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            parser.error(f"invalid row list for table {table}")
        allowed = {name for name, _ in TABLES[table]}
        for row in rows:
            if not set(row).issubset(allowed):
                parser.error(f"unexpected field in {table}: {sorted(set(row) - allowed)}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create prevents accidental overwrite if another process creates
    # the destination between the check and connect.
    try:
        with destination.open("xb"):
            pass
    except FileExistsError:
        parser.error(f"destination already exists; refusing to overwrite: {destination}")

    try:
        conn = sqlite3.connect(destination)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN")
            for table, columns in TABLES.items():
                declaration = ", ".join(f'"{name}" {ddl}' for name, ddl in columns)
                conn.execute(f'CREATE TABLE "{table}" ({declaration})')
                names = [name for name, _ in columns]
                for row in data[table]:
                    present = [name for name in names if name in row]
                    if not present:
                        raise ValueError(f"row without recognised fields in {table}")
                    placeholders = ", ".join("?" for _ in present)
                    fields = ", ".join(f'"{name}"' for name in present)
                    conn.execute(
                        f'INSERT INTO "{table}" ({fields}) VALUES ({placeholders})',
                        [row[name] for name in present],
                    )
            conn.commit()
            counts = {
                table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in TABLES
            }
            for table in TABLES:
                if counts[table] != len(data[table]):
                    raise ValueError(f"row-count mismatch for {table}")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise ValueError(f"SQLite integrity check failed: {integrity}")
        finally:
            conn.close()
    except Exception as exc:
        try:
            destination.unlink()
        except OSError:
            pass
        print(f"Restore failed; destination removed where possible: {exc}", file=sys.stderr)
        return 1

    print(f"Restore complete: {destination}")
    print(f"Backup exported at: {payload.get('exported_at', 'unknown')}")
    for table, count in counts.items():
        print(f"{table}: {count} rows")
    print("SQLite integrity check: ok")
    print("This is a local SQLite recovery copy; it does not restore Render or PostgreSQL.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
