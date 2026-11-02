# TenderScout

TenderScout identifies public procurement contracts relevant to cleaning, facilities management, property maintenance and construction businesses in **Liverpool, Sefton, St Helens, Halton, Knowsley, Wirral and Warrington**. **Cheshire, including Cheshire West and Chester, is excluded.**

## Current collection

- `tenderscout.py` collects from the official Find a Tender and Contracts Finder feeds, plus Bearings and Standstill UK as secondary discovery sources.
- Bearings and Standstill regional/business-eligibility labels are not proof of a contract delivery site; records still pass through the normal geography and opportunity checks.
- The `/sources` page is currently a directory. Listing a council or NHS portal there does **not** mean TenderScout is automatically collecting that portal.

## Approved source coverage

All qualifying procurement opportunities must appear in the **same main opportunity board**, using the same card layout, trade classification, location assessment, deadline/value fields, saved status and exports. Each record must retain its originating portal and contracting authority.

Source groups to support:

1. **National procurement:** Find a Tender and Contracts Finder.
2. **Local authorities and councils:** The Chest and relevant council-specific portals such as ProContract/Proactis, plus other official council procurement systems serving the target area.
3. **NHS and healthcare procurement:** Atamis/Health Family eCommercial System and other official NHS procurement portals where the opportunity is a contract.
4. **Other public and institutional procurement:** relevant universities, colleges, housing providers, fire and rescue services, ambulance services, police, transport bodies and similar organisations where they publish genuine procurement contracts.

The board should have filters by source group and, where useful, by named source or buyer. Source provenance and delivery geography are separate fields: a Liverpool Council buyer does not prove that the delivery site is in Liverpool.

**NHS employment vacancies are excluded.** This is procurement-only.

**Private domestic leads are parked.** Formby Bubble, homeowner jobs, Facebook/Nextdoor enquiries and similar consumer lead channels are not part of the active TenderScout collection scope. Do not mix them into the procurement board.

Only use documented public feeds/APIs, publicly accessible procurement listings where permitted, or sources for which access has been authorised. Do not bypass supplier logins or access controls. Where a portal has no safe supported ingestion route, keep it clearly labelled as a manual source for now rather than pretending it is connected.

## How opportunities are assessed

- Confirmed local delivery/site evidence is classified **LOCAL**.
- Incomplete, ambiguous or potentially mixed delivery geography is classified **REVIEW**.
- Confirmed delivery outside the target area is classified **OUT OF AREA**.
- Buyer headquarters, buyer name, title or a general description mention must not by itself promote a contract to LOCAL.
- A confirmed outside-area delivery site must not remain in the actionable local review queue.
- A mixed local and outside delivery scope remains in REVIEW.

| Classification | Meaning |
| --- | --- |
| LOCAL | Delivery/site evidence confirms a target-area location |
| REVIEW | Potentially relevant, but delivery location or multi-site scope needs checking |
| OUT OF AREA | Delivery/site evidence confirms the contract is outside the target area |

The current target geography is Liverpool, Sefton, St Helens, Halton, Knowsley, Wirral (including Wallasey) and Warrington. Cheshire is not part of the service area.

## How it works

- `tenderscout.py` collects notices and discovery listings.
- `app.py` is the Flask dashboard. **Refresh tenders** runs the collector and saves matches to the database.
- Confirmed local opportunities appear on the main list. Actionable records with a plausible target-area clue but unverified delivery location can appear as **POTENTIAL LOCAL — verify delivery site**.
- Each card provides a quick view of the work, buyer, location confidence, value, deadline and contract period. Missing facts are shown as not stated rather than guessed.
- Planning/upcoming notices stay separate from live invitations to bid.
- Duplicates are suppressed in the working list; the full audit remains available.
- Always verify eligibility, location, deadline, value and documents on the original procurement notice.

## Trades

TenderScout classifies relevant work across cleaning, facilities management, property maintenance and construction. The detailed trade categories and keyword rules are in `tenderscout.py`; the dashboard can filter by trade.

## Local run

```bash
pip install -r requirements.txt
python app.py
```

Open `http://localhost:5000`. Without `DATABASE_URL` it uses a local SQLite file.

## Settings

| Name | Purpose |
| --- | --- |
| `DASHBOARD_USER`, `DASHBOARD_PASSWORD` | Required for dashboard access. If either is missing, the dashboard stays locked. |
| `DATABASE_URL` | PostgreSQL connection string. Without it the app uses SQLite. |
| `TENDERSCOUT_DB` | SQLite file path for local use. |
| `TENDERSCOUT_DAYS`, `TENDERSCOUT_PAGES`, `TENDERSCOUT_SECONDS_PER_SOURCE`, `TENDERSCOUT_FTS_SECONDS` | Collection look-back, pagination and source time limits. |

## Hosting and scheduled refresh

- Render start command: `gunicorn app:app`; health check: `/health`.
- Render deploys automatically when `main` changes.
- The GitHub Actions workflow runs tests and scheduled/manual collection. Hosted refresh requires the repository variable `TENDERSCOUT_APP_URL` and secrets `TENDERSCOUT_APP_USER` / `TENDERSCOUT_APP_PASSWORD`.

## Pages and exports

- `/` main opportunity board; add `trade=...` or `service=...` to filter.
- `/?view=review` location-review queue.
- `/?view=pipeline` planning/upcoming notices.
- `/export.csv` actionable export.
- `/export.csv?view=all` full audit export.
- `/sources` procurement source directory.
- `/health` readiness check.
- The legacy private-lead area remains parked and is not part of active collection or the procurement roadmap.

## Tests

```bash
python -m unittest discover -v
```

Tests are offline and use mocked feeds. A refresh that gets no records back from its sources is reported as a failure, never as "no tenders".
