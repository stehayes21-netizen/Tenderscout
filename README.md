# TenderScout

A small dashboard that finds UK public-sector tenders for **commercial cleaning, facilities management, property maintenance and construction** in **Liverpool, Knowsley, Sefton, St Helens and Halton**. Wirral and the wider North West are excluded from the initial service area.

## How it works
- `tenderscout.py` collects notices from the official Find a Tender and Contracts Finder feeds plus two free discovery sources: Bearings and the Standstill UK North West RSS feed. Discovery feeds are attributed and never treated as proof of a delivery location.
- `app.py` is the Flask dashboard. **Refresh tenders** runs the collector and saves matches to the database.
- Confirmed local tenders appear on the main list. Notices that mention a target-area place in their title/description or buyer name can also appear, clearly labelled **POTENTIAL LOCAL — verify delivery site**. This surfaces candidates without treating buyer HQ or text mentions as proof of delivery. The full location-review queue remains available separately.
- Each tender card includes a plain-English explanation of what the buyer wants, where the work is, the published estimated value, contract dates/duration when available, who may qualify, the deadline and how to apply. Missing facts are labelled as not stated rather than guessed.
- **Pipeline / upcoming** notices are early warnings: they are not necessarily open for bids yet. Open the official notice to confirm its status and next steps.

## Opportunity types and source directory

- `/sources` lists official public procurement portals and community/business discovery channels, including Liverpool City Council's ProContract route and The Chest.
- `/leads` is a separate private-lead board with trade/status filters, counts, exact location, requested work, contact, original source and one-tap WhatsApp/email sharing. Private leads are stored in their own table and are not mixed into public-sector tenders. This page currently stores captured enquiries; it is not yet connected to a live authorised private-job feed.
- Community sites such as Facebook and Nextdoor are **not scraped automatically**. HomeGuild reported no open jobs on its public jobs board when checked on 27 October 2026; BuildPair loads requests dynamically and the reviewed pages do not document a public feed/API. Add only genuine enquiries from sources where access and reuse are permitted; supplier portals may require registration and may not offer a public feed.
- Council portals are listed as sources, not falsely described as connected live feeds. The Standstill UK North West RSS feed is connected as a discovery source; it is broad regional data, may use buyer location when delivery location is missing, and must be verified on the official notice. Automated ingestion from other portals should be added only where an official feed/API or written permission is available.

## Trades
Each tender is labelled with the first trade that matches: Cleaning, Facilities management, Property maintenance (repairs, void works, decorating, roofing, plumbing, gas and electrical servicing) or Construction (refurbishment, new build, demolition, groundworks, retrofit). The keyword lists are at the top of `tenderscout.py` and the dashboard can filter by trade.

## How the area filter works
The filter confirms geography from structured delivery/site evidence: postcodes, government region codes and place names. Mentions in a title or general description are sent to review, not treated as confirmed delivery locations.

| Result | Meaning |
| --- | --- |
| Confirmed | A delivery/site postcode, region code or place is in the target area, with nothing contradicting it |
| Needs a location check | No location, a vague one (e.g. "Merseyside", "Greater Manchester"), a borderline postcode (CH4), a name shared with other places (Halton, Leigh, Kirkby, Garston), or locations both inside and outside the area |
| Dropped | Every location is outside the area, e.g. Scotland, Manchester, Preston, Cheshire East |

**Region codes.** The government feeds often give the area as a code. `UKD61` (Warrington), `UKD63` (Cheshire West and Chester), `UKD71` (East Merseyside: Knowsley, St Helens, Halton), `UKD72` (Liverpool), `UKD73` (Sefton) and `UKD74` (Wirral, including Wallasey) count as confirmed. Codes that cover the area plus places outside it (Greater Manchester, Lancashire, the broad North West code) need a postcode or place name to confirm. Any other UK code is outside the area.

**Longer names win.** "Preston Brook" is Halton, not Preston; "Chester-le-Street" is Durham, not Chester; "Cheshire East" is outside the area.

**Title and description.** Place names and postcodes mentioned only in the title or general description are not proof of delivery location. They stay in the review queue until the official delivery/site fields can confirm the area.

Target postcode districts are listed at the top of `tenderscout.py`. Royal Mail postcode districts don't line up exactly with council boundaries, so check them against your own knowledge and edit them if needed.

## Local run
```bash
pip install -r requirements.txt
python app.py
```
Open `http://localhost:5000`. Without `DATABASE_URL` it uses a local SQLite file.

## Settings (environment variables)
| Name | Purpose |
| --- | --- |
| `DASHBOARD_USER`, `DASHBOARD_PASSWORD` | Required for dashboard access. If either is missing, the dashboard returns HTTP 503 and stays locked; if credentials are wrong, it returns HTTP 401. `/health` stays open for the host. |
| `DATABASE_URL` | PostgreSQL connection string. Without it the app uses SQLite, which is wiped on every redeploy on Render's free tier. |
| `TENDERSCOUT_DB` | SQLite file path (local use only) |
| `TENDERSCOUT_DAYS`, `TENDERSCOUT_PAGES`, `TENDERSCOUT_SECONDS_PER_SOURCE`, `TENDERSCOUT_FTS_SECONDS` | Look-back window (default 60 days), pagination cap, general per-source time limit (default 60 seconds), and Find a Tender time limit (default 150 seconds) |

## Hosting on Render
- Start command: `gunicorn app:app` (`gunicorn.conf.py` raises the timeout to 120 seconds so a refresh isn't cut off).
- Health check: `/health`.
- Render deploys automatically when `main` changes.
- The free web service sleeps when idle, so the first visit can be slow.

## Scheduled refresh
`.github/workflows/main.yml` runs the tests on every push. On a schedule (every 6 hours) and on manual runs it also saves a CSV of matches and can refresh the live site. To enable the refresh, set the repository variable `TENDERSCOUT_APP_URL` and the secrets `TENDERSCOUT_APP_USER` and `TENDERSCOUT_APP_PASSWORD`.

## Pages
- `/` confirmed tenders plus clearly labelled target-area candidates · `/?view=review` all tenders needing a location check · `/?view=pipeline` planning/upcoming notices · add `trade=...` or `service=...` to filter
- Each tender shows how long is left to bid (closed tenders sink to the bottom) and has a **Stage** dropdown: Reviewing, Interested, Bidding, Submitted, Won, Lost, Not for us
- `POST /refresh` fetch new tenders and return to the dashboard with a summary; `POST /api/refresh` does the same but replies in plain text for scheduled jobs
- `POST /add` add a tender by hand (`title`, `buyer`, `notice_url`, `pipeline_status`)
- `/export.csv` download public-sector tender records
- `/sources` council procurement and community source directory
- `/leads` manually capture and view private customer enquiries separately
- `/health` readiness check

## Tests
```bash
python -m unittest discover -v
```
The tests are offline and use mocked feeds. A refresh that gets no records back from its sources is reported as a failure, never as "no tenders".

Always open the official notice and check location, deadline, value, eligibility and documents before acting.
