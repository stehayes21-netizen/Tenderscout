# TenderScout backup and recovery runbook

## Current backup format

The authenticated `/backup.json` route downloads a snapshot with format `tenderscout-database-backup`, version 1. It includes three separate tables: `tenders`, `private_leads`, and `external_projects`. This is a data export, not a copy of Render's PostgreSQL schema, indexes, roles, or service configuration.

Keep the downloaded JSON and the three CSV exports somewhere outside Render. Treat the JSON as sensitive: private leads may contain customer contact details. Do not commit backups to GitHub or share them publicly.

## Restore to a local SQLite database (no paid service required)

Requirements: Python 3.10+ and the downloaded JSON backup. The restore tool uses only Python's standard library.

1. Download or clone the TenderScout repository.
2. Keep the original JSON backup unchanged.
3. From the repository directory, run:

   ```bash
   python restore_backup.py /path/to/tenderscout_database_backup.json ./recovery/tenderscout-recovered.db
   ```

4. The destination database must not already exist. The tool refuses to overwrite it.
5. Read the output. It reports row counts for all three tables and checks SQLite integrity.
6. Keep the JSON and the restored database as separate copies.

On Windows, replace the paths with Windows paths, for example:

```powershell
python restore_backup.py "$HOME\Downloads\tenderscout_database_backup.json" ".\recovery\tenderscout-recovered.db"
```

On Android, the simplest route is to preserve the JSON in Downloads and copy it to Drive or another storage location. The script is designed for a computer with Python installed; it is not an Android app.

## Running the app from a local SQLite recovery copy

Install the project requirements, then point the app at the recovered database file. Do not set `DATABASE_URL` in this local shell, because that selects PostgreSQL instead.

Linux/macOS:

```bash
python -m pip install -r requirements.txt
export TENDERSCOUT_DB="$PWD/recovery/tenderscout-recovered.db"
unset DATABASE_URL
python app.py
```

Windows PowerShell:

```powershell
python -m pip install -r requirements.txt
$env:TENDERSCOUT_DB = "$PWD\recovery\tenderscout-recovered.db"
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
python app.py
```

Configure `DASHBOARD_USER` and `DASHBOARD_PASSWORD` locally before opening the app. Keep the dashboard private.

## Limits and operational safety

- The tool restores data to a local SQLite database. It does **not** write to or restore Render PostgreSQL.
- This avoids risking the live database while recovery is being validated.
- A restore should be performed on a copy, never by replacing the only backup.
- After any backup, compare the reported row counts with the expected export counts and keep a dated copy outside Render.
- Before relying on this as a production disaster-recovery plan, run the restore against the actual downloaded JSON and verify the restored row counts. Automated tests use representative fixtures; they do not replace that real-file check.
- Render's free database has a stated expiry of 23 November 2026. Do not assume it will remain available after that date.
