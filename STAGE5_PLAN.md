# TenderScout Stage 5 — Opportunity discovery, company matching and workbook

## Purpose

Turn TenderScout from a tender collector into a private opportunity desk: collect notices automatically, explain each opportunity clearly, match it to suitable companies using verified job and company information, and maintain an automatically refreshed Excel workbook.

This work is developed on `stage5/opportunity-matching-exports`, based on draft PR #66. It must not be merged or deployed to the live Render service without the owner's explicit approval.

## Non-negotiable product rules

- The user works from TenderScout. They must not have to visit The Chest, Atamis, Find a Tender, Contracts Finder or council portals to discover jobs.
- Every source is collected automatically through a documented, permitted feed/API or another permitted machine-readable route. A source directory link alone does not count as integration.
- Keep the existing nine target areas: Liverpool, Sefton, St Helens, Warrington, Halton, West Cheshire, Wirral, Wallasey and Knowsley.
- A job is LOCAL only when supported by delivery/site evidence. Buyer headquarters, buyer postcode, portal region and a place-name clue in prose do not prove delivery location. Ambiguous and mixed-area records stay REVIEW under the existing geography rules.
- Never invent job value, delivery postcode, buyer contact, qualification, company capacity or match certainty. Display "Not stated" / "Needs checking" where evidence is absent.
- Preserve source attribution, original notice URL, publication date, closing date and collection errors. Clearly flag discovery listings whose official notice still needs verification.
- £0 upfront spend. Use free public data and open-source libraries. No paid APIs or advertising.
- Keep private leads out of the public tender board unless separately approved.
- Do not merge or deploy without explicit approval.

## Stage 5 work order

### 5A — Source coverage and permitted collection
1. Audit the current official feed collectors and per-source coverage/error statistics.
2. Map each of the nine areas to its council procurement route and determine whether The Chest / ProContract and any council-specific portals expose a permitted feed, API, export or public machine-readable endpoint.
3. Investigate NHS Atamis access and terms. Do not automate authenticated/private access or scrape against terms; seek an authorised feed/integration route.
4. Prioritise sources by real incremental local opportunity coverage, data quality, permission and reliability.
5. Add one source at a time with pagination, rate-limit handling, clear incomplete-coverage reporting, deduplication and tests.
6. Never describe a source as integrated until it is actually being collected and validated.

### 5B — Opportunity detail and contact completeness
For each opportunity, show and export:
- unique TenderScout ID and official notice/reference ID;
- title, buyer/contracting authority and trade;
- plain-English summary of requested work;
- delivery address/postcode and geography evidence/status;
- stated value and contract start/end dates;
- published date, deadline and current notice stage;
- buyer contact name, email, phone or official portal messaging route, when published;
- source name, original notice URL, and whether the link is an aggregator/discovery listing or official notice;
- why the job matched and what information is missing.

Extract contact fields only from source data / official notice content. Do not fabricate a contact or imply that a generic portal link is a direct named contact.

### 5C — Company directory and fit rules
Create a company directory that can be maintained by the user. A company profile should support:
- company name and active/inactive status;
- trades/services offered;
- postcode districts and/or explicit travel radius/areas covered;
- company size / workforce capacity;
- minimum and maximum job value the company is willing and able to take;
- domestic/commercial/public-sector work capability;
- relevant accreditations, insurance and mandatory qualifications;
- notes and contact details.

Matching must be explainable and evidence-based. Separate hard disqualifiers (wrong trade, outside coverage, mandatory qualification absent, value above stated capacity) from soft fit signals (nearby area, relevant experience, team size). Missing company or job data produces "Needs review", not a confident match. Do not infer company size or financial capacity from its name or website.

### 5D — Automatic Excel workbook
Generate an `.xlsx` workbook automatically after each successful collection/update, using a free open-source library. The workbook should contain:
1. **All Opportunities** — all collected records with source, dates, status and location evidence.
2. **Company Matches** — one row per job/company candidate, match status, score/reasons and missing requirements.
3. **Company Directory** — saved company profiles and coverage/capacity data.
4. **Needs Review** — uncertain geography, missing postcodes/contacts, incomplete notice data and uncertain matches.
5. **Allocated Jobs** — user-selected company, allocation date, follow-up/status and outcome.

Workbook requirements:
- Excel filters, frozen header row, sensible column widths, readable dates and currency.
- Stable IDs and upsert/update behaviour; refreshes must not create duplicate jobs or erase user-entered company profiles, allocations or notes.
- Keep source URL clickable.
- Missing values stay blank or explicitly "Not stated"; never fill them with guesses.
- A downloadable latest workbook from the private TenderScout dashboard. A CSV audit export can remain as a fallback.
- Workbook generation failure must not fail or corrupt the tender refresh; report the export error clearly and retain the previous valid workbook.

## Acceptance tests before this stage is considered complete

- Source-by-source collection status makes incomplete coverage visible.
- At least one genuinely new permitted source is integrated and validated, or documented as blocked with the reason and next permitted route.
- Manual validation of real opportunities across different trades and target areas.
- Each displayed contact is traceable to a source field or official notice.
- Geography tests retain the conservative LOCAL / REVIEW rules.
- Company matching tests cover postcode coverage, trade, value limits, workforce/capacity and missing information.
- Repeated refreshes update existing spreadsheet rows without duplicate jobs or loss of user-maintained data.
- Workbook opens in Excel/LibreOffice and all five tabs contain the expected columns.
- Full test suite passes.
- Live Render app remains unchanged until explicit approval.

## Implementation notes from the current code audit

- Current collectors: Find a Tender, Contracts Finder, Bearings and Standstill UK.
- The Chest is currently listed in the source directory but is not an automatic collector.
- Atamis is not currently an automatic collector; access/permission must be established first.
- Tender rows already have contact columns (`contact_name`, `contact_email`, `contact_phone`) and an `eligibility_text` column. The next code audit must establish which collectors populate these fields and where the dashboard displays them before adding duplicate schema.
- Current `requirements.txt` has Flask, Gunicorn and psycopg; no Excel library is installed. Select and test a free workbook library before implementation.
- This document is a work plan, not evidence that any new source or workbook functionality is already live.
