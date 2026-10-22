# Tender Scout — web MVP package

A small dashboard for UK public procurement opportunities in cleaning, facilities management, caretaking, grounds and property/building maintenance.

## Included
- `tenderscout.py`: collector for official Find a Tender and Contracts Finder OCDS feeds.
- `app.py`: web dashboard with manual opportunity entry, pipeline stages, filters, deduplication and CSV export.
- SQLite by default for local development; managed PostgreSQL via `DATABASE_URL` for hosted persistence.
- `test_feeds.py`, `test_geography.py`: offline tests using mocked feed responses.
- `test_live_feeds.py`: live smoke test for an internet-enabled environment.
- `test_app.py`: dashboard/pipeline smoke tests.
- `.github/workflows/live-feed-check.yml`: scheduled hourly verification on GitHub Actions, plus a manual run option.
- `railway.json`, `Procfile`, `Dockerfile`, `requirements.txt`: Railway-compatible deployment configuration with a health check.

## Local run
Requires Python 3.10+ and dependencies in `requirements.txt`.

```bash
pip install -r requirements.txt
python app.py
```
Then open `http://localhost:5000`.

## Deploy configuration
The included `render.yaml` is a starting point for Render. Review its settings and current pricing before deploying. Configure the generated `DASHBOARD_PASSWORD`, `SECRET_KEY`, and a managed PostgreSQL `DATABASE_URL` in the host dashboard. Basic authentication is enabled whenever both `DASHBOARD_USER` and `DASHBOARD_PASSWORD` are set. The Render blueprint prompts for `DATABASE_URL`; use a managed PostgreSQL connection (for example, a free-tier Neon project) to retain opportunities and pipeline stages across service restarts.

**Important storage limitation:** on a hosted service, do not use SQLite as the production database unless the host provides persistent storage. The app uses PostgreSQL when `DATABASE_URL` is supplied; otherwise it falls back to local SQLite for development. Render Free web services have an ephemeral filesystem, so an on-disk SQLite database can be lost on restarts/redeploys. Render's free Postgres database is also time-limited, so use a suitable persistent managed database tier and check current terms before relying on it. The project has not yet been deployed.

## Live refresh
Press **Find new tenders now** in the dashboard. The refresh retrieves up to five pages per official source for a 90-day lookback, applies the current cleaning/FM and North West filters, and upserts matching records without changing the pipeline stage already assigned by the user. A failed source is surfaced as a warning; it must not be interpreted as zero tenders.

The dashboard supports manual refresh. The included GitHub Actions workflow is configured to test both official APIs hourly and can call the deployed app's authenticated `/api/refresh` endpoint after those tests pass. To enable the hosted refresh, set the repository variable `TENDERSCOUT_APP_URL` to the app's base URL and repository secrets `TENDERSCOUT_APP_USER` and `TENDERSCOUT_APP_PASSWORD` to the dashboard Basic Auth credentials. Do not place credentials in the workflow file.

## Tests
```bash
python test_feeds.py
python test_geography.py
python test_live_feeds.py
python -m unittest test_app -v
```
The live smoke test needs outbound internet. A DNS/network error means verification is incomplete, not that the feed is empty. `test_app.py` needs Flask installed.

## Current verified status
- Offline pagination and mocked-feed checks: pass.
- Offline geography checks: pass.
- Python syntax compilation: pass.
- Live API retrieval from this workspace: blocked by DNS/network failure for both official sources.
- Web app tests: not run successfully in this workspace because Flask is not installed and package installation is blocked by the same DNS/network restriction.
- Hosting, scheduled hourly refresh, and durable hosted storage: not deployed/configured yet.
- Deployment package: Gunicorn and Psycopg are included. The app supports a durable managed PostgreSQL database through `DATABASE_URL`.
- The hosted refresh step stays skipped until `TENDERSCOUT_APP_URL` is configured in the GitHub repository variables and the two dashboard credentials are stored as repository secrets.

Always open the official notice and verify location, deadline, value, eligibility and procurement documents before acting.

## Hosted live-feed verification (prepared, not yet active)

`.github/workflows/live-feed-check.yml` runs on pushes to `main`/`master`, hourly, or manually. It installs dependencies, runs offline collector/geography/dashboard tests, checks both official APIs, and — if the hosted app URL variable is configured — calls the authenticated refresh endpoint to save results to the database. GitHub Actions can only run after the project is pushed to a repository with Actions enabled. The connected account currently returns no accessible repositories, so this automation is prepared but not active yet. The live test reports API/network errors distinctly and must not interpret a failed request as zero opportunities.


## Railway deployment notes (prepared, not deployed)

- `railway.json` defines the start command, `/health` readiness check, and restart policy.
- The app can use SQLite at `TENDERSCOUT_DB=/data/tenderscout.db` if a Railway volume is attached at `/data`. This avoids requiring a separate Postgres service for the first small MVP. Railway volumes are billed by actual storage used (currently documented at $0.15/GB-month), and compute may also incur usage charges; check the current plan and billing page before deployment.
- Alternatively, set `DATABASE_URL` to a managed PostgreSQL connection. Do not set both storage modes expecting data to be shared; `DATABASE_URL` takes precedence.
- Set strong `SECRET_KEY`, `DASHBOARD_USER`, and `DASHBOARD_PASSWORD` environment variables before making the dashboard public. `/health` deliberately exposes only a minimal readiness signal.
- Deployment is not yet live. Railway's project creation returned a temporary “Deploys have been paused temporarily” error in this session, so no project/service/database/volume was created and no costs were intentionally initiated.
