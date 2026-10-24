# TenderScout

A small dashboard that finds UK public-sector tenders for cleaning and facilities management (FM) in **Liverpool, Sefton, Wirral, St Helens and Halton**.

## How it works
- `tenderscout.py` collects notices from the official Find a Tender and Contracts Finder feeds, scores them, and decides whether each one is in the target area.
- `app.py` is the Flask dashboard. **Refresh tenders** runs the collector and saves matches to the database.
- Tenders confirmed in the target area appear on the main list. Anything whose location can't be confirmed goes to **Needs a location check** instead, so unclear tenders never mix into the main list.

## How the area filter works
Postcodes are checked first, then whole-word place names.

| Result | Meaning |
| --- | --- |
| Confirmed | A delivery/site postcode or place is in the target area, with nothing contradicting it |
| Needs a location check | No location, a vague one (e.g. "Merseyside", "Cheshire"), a borderline postcode (L35, CH64), or locations both inside and outside the area |
| Dropped | Every location is outside the area, e.g. Scotland, Knowsley, Warrington, Manchester |

Target postcode districts: `L1`–`L19`, `L24`, `L25`, `L27` (Liverpool); `L20`–`L23`, `L29`–`L31`, `L37`, `L38`, `PR8`, `PR9` (Sefton); `CH41`–`CH49`, `CH60`–`CH63` (Wirral); `WA9`–`WA12` (St Helens); `WA7`, `WA8` (Halton).

Royal Mail postcode districts don't line up exactly with council boundaries, so check the lists at the top of `tenderscout.py` against your own knowledge and edit them if needed. Knowsley is deliberately excluded.

## Local run
```bash
pip install -r requirements.txt
python app.py
```
Open `http://localhost:5000`. Without `DATABASE_URL` it uses a local SQLite file.

## Settings (environment variables)
| Name | Purpose |
| --- | --- |
| `DASHBOARD_USER`, `DASHBOARD_PASSWORD` | Turn on password protection. **Set both before sharing the site.** If either is missing the site is open to anyone with the link. `/health` stays open for the host. |
| `DATABASE_URL` | PostgreSQL connection string. Without it the app uses SQLite, which is wiped on every redeploy on Render's free tier. |
| `TENDERSCOUT_DB` | SQLite file path (local use only) |
| `TENDERSCOUT_DAYS`, `TENDERSCOUT_PAGES` | Look-back window (default 14 days) and pages per source (default 3) |

## Hosting on Render
- Start command: `gunicorn app:app` (`gunicorn.conf.py` raises the timeout to 120 seconds so a refresh isn't cut off).
- Health check: `/health`.
- Render deploys automatically when `main` changes.
- The free web service sleeps when idle, so the first visit can be slow.

## Scheduled refresh
`.github/workflows/main.yml` runs the tests on every push. On a schedule (every 6 hours) and on manual runs it also saves a CSV of matches and can refresh the live site. To enable the refresh, set the repository variable `TENDERSCOUT_APP_URL` and the secrets `TENDERSCOUT_APP_USER` and `TENDERSCOUT_APP_PASSWORD`.

## Pages
- `/` confirmed tenders · `/?view=review` tenders needing a location check
- `POST /refresh` (or `/api/refresh`) fetch new tenders
- `POST /add` add a tender by hand (`title`, `buyer`, `notice_url`, `pipeline_status`)
- `/export.csv` download everything
- `/health` readiness check

## Tests
```bash
python -m unittest discover -v
```
The tests are offline and use mocked feeds. A refresh that gets nothing back from both feeds is reported as a failure, never as "no tenders".

Always open the official notice and check location, deadline, value, eligibility and documents before acting.
