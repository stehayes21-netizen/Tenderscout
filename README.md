# TenderScout

A small dashboard that finds UK public-sector tenders for **commercial cleaning, facilities management, property maintenance and construction** in **Liverpool, Sefton, St Helens, Halton, Knowsley and Wirral**. Warrington and Cheshire West and Chester are outside the service area.

## How it works
- `tenderscout.py` collects notices from the official Find a Tender and Contracts Finder feeds plus free discovery sources: Bearings and seven Standstill RSS feeds (North West region, cleaning, construction, building-installation maintenance, refurbishment, grounds maintenance and waste services). Discovery feeds are attributed and never treated as proof of a delivery location.
- `app.py` is the Flask dashboard. **Refresh tenders** runs the collector and saves matches to the database.
- Confirmed local tenders appear on the main list. Notices that mention a target-area place in their title/description or buyer name can also appear, clearly labelled **POTENTIAL LOCAL — verify delivery site**. This surfaces candidates without treating buyer HQ or text mentions as proof of delivery. The full location-review queue remains available separately.
- Each tender card starts with a **10-second check** in three scan lines: **JOB** (work required), **WHERE** (delivery location and whether it is verified), and **VALUE / DEADLINE / CONTRACT**. Full eligibility, buyer contact, application instructions and the official notice remain underneath. Missing facts are labelled as not stated rather than guessed.
- **Pipeline / upcoming** notices are early warnings: they are not necessarily open for bids yet. Open the official notice to confirm its status and next steps.

## Opportunity types and source directory

- `/sources` lists official public procurement portals and community/business discovery channels, including Liverpool City Council's ProContract route and The Chest.
- `/leads` is a separate private-lead board with trade/status filters, counts, exact location, requested work, contact, original source and one-tap WhatsApp/email sharing. Private leads are stored in their own table and are not mixed into public-sector tenders. This page currently stores captured enquiries; it is not yet connected to a live authorised private-job feed.
- Community sites such as Facebook and Nextdoor are **not scraped automatically**. HomeGuild reported no open jobs on its public jobs board when checked on 27 October 2026; BuildPair loads requests dynamically and the reviewed pages do not document a public feed/API. Add only genuine enquiries from sources where access and reuse are permitted; supplier portals may require registration and may not offer a public feed.
- Council portals are listed as sources, not falsely described as connected live feeds. The Standstill UK North West RSS feed is connected as a discovery source; it is broad regional data, may use buyer location when delivery location is missing, and must be verified on the official notice. Automated ingestion from other portals should be added only where an official feed/API or written permission is available.

## Planning leads
`planning.py` reads recent applications for the six councils from UK PlanIt (planit.org.uk), a free aggregator of council planning registers. It drops paperwork-only applications (discharge of conditions, minor amendments, adverts, refusals) and labels each lead with likely trades: extensions and building work, new homes and conversions, roofing, trees and landscaping, shopfronts and fit-out, windows and doors, fencing and groundworks, demolition, solar/EV/electrical, and builders' cleans.

- Coverage is shown on the page. When this was written, PlanIt was current for Sefton, Knowsley, St Helens and Halton, but had stopped updating Liverpool (July 2026) and Wirral (June 2026). Stale councils are flagged rather than shown as "no jobs".
- A planning application is planned work, not a request for quotes. Addresses are public records; homeowners should be contacted by letter or leaflet only, never by cold call or unsolicited email.
- PlanIt's acceptable-use rules are built in: checks run only between 18:00 and 06:00 UK time (automatically at most once a day, after the scheduled refresh), with 60 seconds between requests, Retry-After honoured, and well under 300 requests a day. **Set `PLANIT_CONTACT_EMAIL`** in Render: PlanIt requires a contact email in every request, and planning checks stay off until it is set. The check button runs in the background (about 6 minutes).
- PlanIt is a free hobby service run by one person, with no service guarantee. `TENDERSCOUT_PLANNING_DAYS` sets the look-back window (default 30).

## Weekly subscriber emails
`/subscribers` lists tradespeople who pay for a weekly email of matching jobs. Each subscriber picks planning-lead types, tender trades and areas; **Preview email** shows exactly what they'd get and **Send now** sends it.

Every Monday from 6am UK time, active subscribers get one email with new planning leads (submitted in the last 14 days or approved in the last 7) and open local tenders that match. The scheduled GitHub job calls `POST /api/digest/send`; the app sends at most once a week per subscriber, so repeated calls are safe.

Email is sent through SMTP; a free Gmail account works (up to about 500 emails a day). Set in Render:

| Name | Purpose |
| --- | --- |
| `SMTP_USER` | The Gmail address that sends the emails |
| `SMTP_PASSWORD` | A Gmail **app password** (Google Account > Security > 2-Step Verification > App passwords), not your normal password |
| `DIGEST_SENDER_NAME` | Optional name shown as the sender, e.g. "Merseyside Trade Leads" |
| `DIGEST_REPLY_TO` | Optional reply-to address |
| `SMTP_HOST`, `SMTP_PORT` | Optional, default `smtp.gmail.com` and `587` |

## Trades
Each tender is labelled with the first trade that matches: Cleaning, Facilities management, Property maintenance (repairs, void works, decorating, roofing, plumbing, gas and electrical servicing) or Construction (refurbishment, new build, demolition, groundworks, retrofit). The keyword lists are at the top of `tenderscout.py` and the dashboard can filter by trade.

## How the area filter works
The filter confirms geography from structured delivery/site evidence: postcodes, government region codes and place names. Mentions in a title or general description are sent to review, not treated as confirmed delivery locations.

| Result | Meaning |
| --- | --- |
| Confirmed | A delivery/site postcode, region code or place is in the target area, with nothing contradicting it |
| Needs a location check | No location, a vague one (e.g. "Merseyside", "Greater Manchester"), a borderline postcode (CH4), a name shared with other places (Halton, Leigh, Kirkby, Garston), or locations both inside and outside the area |
| Dropped | Every location is outside the area, e.g. Scotland, Manchester, Preston, Cheshire East |

**Region codes.** The government feeds often give the area as a code. `UKD71` (East Merseyside: Knowsley, St Helens and Halton), `UKD72` (Liverpool), `UKD73` (Sefton) and `UKD74` (Wirral, including Wallasey) count as confirmed. `UKD61` (Warrington) and `UKD63` (Cheshire West and Chester) are outside the current service area. Codes that cover the area plus places outside it (Greater Manchester, Lancashire, the broad North West code) need a postcode or place name to confirm. Any other UK code is outside the area. CH4 remains a borderline district and stays in review pending site-level confirmation.

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
- `/planning` planning-application leads (extensions, lofts, new homes, shop refits, tree works) sorted by trade, area and stage; **Check for new planning applications** refreshes them, and the scheduled refresh does too
- `/subscribers` weekly job emails for paying tradespeople: add, preview, send now, pause
- `/leads` manually capture and view private customer enquiries separately
- `/health` instant liveness check for the host (never touches the database, so a slow first connection after waking can't make Render restart the app)
- `/health/db` database check

## Tests
```bash
python -m unittest discover -v
```
The tests are offline and use mocked feeds. A refresh that gets no records back from its sources is reported as a failure, never as "no tenders".

Always open the official notice and check location, deadline, value, eligibility and documents before acting.
