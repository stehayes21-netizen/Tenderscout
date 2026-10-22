#!/usr/bin/env python3
"""
TenderScout v1 — UK tender collector and filter
Uses official public OCDS feeds; Python 3.10+; standard library only.

Run:
  python tenderscout.py
  python tenderscout.py --days 14 --region "North West" --min-score 3
  python tenderscout.py --services cleaning fm --out my_tenders.csv

Outputs:
  tenderscout_results.csv  - filtered shortlist (opens in Excel/Google Sheets)
  tenderscout_all.csv      - all fetched tender-stage notices, for audit

Important:
- Public feeds may be delayed or omit fields; always open the official notice.
- Geography matching is a keyword heuristic, not a definitive location check.
- This script runs when you launch it. It is not a hosted service or scheduled job.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

FTS_URL = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
CF_URL = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"

CLEANING_TERMS = [
    "cleaning", "deep clean", "window cleaning", "carpet cleaning",
    "washroom", "janitorial", "hygiene services", "sanitary",
    "graffiti removal", "industrial cleaning", "specialised cleaning",
    "specialized cleaning", "biohazard cleaning", "kitchen extraction",
]
FM_TERMS = [
    "facilities management", "facility management", "hard fm", "soft fm",
    "building maintenance", "estates maintenance", "caretaking",
    "caretaker", "grounds maintenance", "waste management", "pest control",
    "planned preventative maintenance", "ppm", "property maintenance",
]
EXCLUDE_TERMS = [
    "software", "information technology", "it equipment", "laptop",
    "cyber security", "telecommunications", "road construction",
    "new build", "building construction", "architectural services",
]

NORTH_WEST_TERMS = [
    "north west", "merseyside", "liverpool",
    "birkenhead", "wirral", "wallasey",
    "sefton", "southport", "formby",
    "st helens", "knowsley", "halton",
    "warrington", "runcorn", "chester",
    "cheshire", "widnes", "prescot",
    "huyton", "bootle", "crosby",
]

FIELDS = [
    "source", "published_date", "title", "buyer", "description", "location_text",
    "service_match", "region_match", "geography_reason", "estimated_value_gbp",
    "deadline", "status", "score", "score_reasons", "notice_url", "ocid",
]


def get_json(url: str, timeout: int = 25):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "TenderScout/1.0 (public procurement research)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def as_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return " ".join(as_text(v) for v in value)
    if isinstance(value, dict):
        return " ".join(as_text(v) for v in value.values())
    return ""


def first(dct, *keys):
    for key in keys:
        val = dct.get(key) if isinstance(dct, dict) else None
        if val not in (None, ""):
            return val
    return None


def parse_date(value):
    if not value:
        return None
    try:
        value = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def money_value(value):
    if isinstance(value, dict):
        value = value.get("amount")
    try:
        return round(float(value), 2) if value is not None else ""
    except (TypeError, ValueError):
        return ""


def extract_records(payload, source):
    """Tolerate the common OCDS package and a few API response shapes."""
    if isinstance(payload, list):
        roots = payload
    elif isinstance(payload, dict):
        roots = []
        for key in ("releases", "results", "data", "items", "records"):
            if isinstance(payload.get(key), list):
                roots.extend(payload[key])
        if not roots and isinstance(payload.get("packages"), list):
            roots.extend(payload["packages"])
        if not roots and any(k in payload for k in ("ocid", "id", "tender")):
            roots = [payload]
    else:
        roots = []

    output = []
    for root in roots:
        if not isinstance(root, dict):
            continue
        nested = root.get("releases")
        releases = nested if isinstance(nested, list) else [root]
        for rel in releases:
            if not isinstance(rel, dict):
                continue
            tender = rel.get("tender") or {}
            buyer = rel.get("buyer") or {}
            parties = rel.get("parties") or []
            if not buyer and isinstance(parties, list):
                buyer = next((p for p in parties if "buyer" in (p.get("roles") or [])), {})
            title = as_text(first(tender, "title", "name") or first(rel, "title", "description"))
            desc = as_text(first(tender, "description", "summary") or first(rel, "description"))
            buyer_name = as_text(first(buyer, "name", "legalName"))
            value = first(tender, "value", "budget")
            published = first(rel, "date", "publishedDate", "publicationDate")
            deadline = ""
            period = tender.get("tenderPeriod") or tender.get("tender_period") or {}
            if isinstance(period, dict):
                deadline = as_text(first(period, "endDate", "end_date", "end"))
            notice_url = ""
            for doc in (tender.get("documents") or []) + (rel.get("documents") or []):
                if isinstance(doc, dict):
                    url = first(doc, "url", "uri")
                    if url and ("find-tender.service.gov.uk/Notice/" in str(url)
                                or "contractsfinder.service.gov.uk" in str(url)):
                        notice_url = str(url)
                        break
            if not notice_url:
                release_id = as_text(first(rel, "id"))
                if source == "Find a Tender" and re.fullmatch(r"\d{6}-\d{4}", release_id):
                    notice_url = f"https://www.find-tender.service.gov.uk/Notice/{release_id}"
            # Prefer structured delivery/site addresses for geography. A place name
            # appearing only in a title or description is weak evidence and must be
            # reviewed rather than treated as proof of contract location.
            location_parts = []
            def add_location(obj):
                if isinstance(obj, dict):
                    for key in ("streetAddress", "locality", "region", "postalCode", "countryName", "country"):
                        val = obj.get(key)
                        if val:
                            location_parts.append(as_text(val))
                elif isinstance(obj, list):
                    for item in obj:
                        add_location(item)
            add_location(tender.get("deliveryAddress"))
            add_location(tender.get("deliveryAddresses"))
            add_location(tender.get("delivery_address"))
            for item in (tender.get("items") or []):
                if isinstance(item, dict):
                    add_location(item.get("deliveryAddress") or item.get("delivery_address"))
            contract = rel.get("contract") or {}
            add_location(contract.get("deliveryAddress"))
            output.append({
                "source": source,
                "location_text": ", ".join(dict.fromkeys(location_parts)),
                "published_date": as_text(published),
                "title": title.strip(),
                "buyer": buyer_name.strip(),
                "description": desc.strip(),
                "estimated_value_gbp": money_value(value),
                "deadline": deadline,
                "status": as_text(first(tender, "status") or first(rel, "tag")),
                "notice_url": notice_url,
                "ocid": as_text(first(rel, "ocid")),
                "_raw": rel,
            })
    return output


def _next_cursor(payload):
    """Read common OCDS pagination cursor shapes without assuming page numbers."""
    if not isinstance(payload, dict):
        return None
    for key in ("nextCursor", "next_cursor", "cursor"):
        value = payload.get(key)
        if value and isinstance(value, str):
            return value
    links = payload.get("links") or {}
    if isinstance(links, dict):
        nxt = links.get("next")
        if isinstance(nxt, dict):
            nxt = nxt.get("href") or nxt.get("uri")
        if isinstance(nxt, str) and nxt:
            parsed = urllib.parse.urlparse(nxt)
            query = urllib.parse.parse_qs(parsed.query)
            values = query.get("cursor")
            if values:
                return values[0]
            # Some OCDS feeds provide a full next URL instead of a cursor.
            return nxt
    return None


def _page_url(base_url, params, cursor):
    """Build the next request URL, supporting opaque cursors and next links."""
    if cursor and cursor.startswith(("https://", "http://")):
        return cursor
    page_params = dict(params)
    if cursor:
        page_params["cursor"] = cursor
    return base_url + "?" + urllib.parse.urlencode(page_params)


def fetch_fts(days, max_pages):
    results = []
    updated_from = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    cursor = None
    for _ in range(max_pages):
        params = {"stages": "tender", "limit": "100", "updatedFrom": updated_from}
        url = _page_url(FTS_URL, params, cursor)
        try:
            payload = get_json(url)
        except Exception as exc:
            print(f"[WARN] Find a Tender fetch failed: {exc}", file=sys.stderr)
            break
        batch = extract_records(payload, "Find a Tender")
        results.extend(batch)
        cursor = _next_cursor(payload)
        if not cursor or not batch:
            break
        time.sleep(0.2)
    return results


def fetch_contracts_finder(days, max_pages):
    results = []
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)
    cursor = None
    for _ in range(max_pages):
        params = {
            "publishedFrom": start.isoformat(),
            "publishedTo": today.isoformat(),
            "stages": "planning,tender",
            "limit": "100",
        }
        url = _page_url(CF_URL, params, cursor)
        try:
            payload = get_json(url)
        except Exception as exc:
            print(f"[WARN] Contracts Finder fetch failed: {exc}", file=sys.stderr)
            break
        batch = extract_records(payload, "Contracts Finder")
        if not batch:
            break
        results.extend(batch)
        cursor = _next_cursor(payload)
        if not cursor:
            break
        time.sleep(0.2)
    return results


def keyword_hits(text, terms):
    lower = text.lower()
    return [term for term in terms if term in lower]


def classify_geography(record, region):
    """Classify location conservatively; never infer a confirmed site from prose alone."""
    if region == "UK wide":
        return "UK-wide search", "UK-wide search"

    location_text = (record.get("location_text") or "").lower()
    prose = " ".join([record.get("title", ""), record.get("description", "")]).lower()
    nw_hits = keyword_hits(location_text, NORTH_WEST_TERMS)
    if nw_hits:
        return "LOCAL", "Structured delivery/site address: " + ", ".join(nw_hits[:3])

    # A known delivery address that has no North West evidence is outside the target.
    # Avoid interpreting a bare country name as a specific location.
    has_specific_location = bool(re.search(
        r"\b(?:street|road|avenue|lane|drive|way|close|court|postcode|\w+shire|london|birmingham|bristol|leeds|sheffield|nottingham|cardiff|edinburgh|glasgow|newcastle|oxford|cambridge)\b", location_text
    ))
    if location_text.strip() and has_specific_location:
        return "OUT_OF_AREA", "Structured address contains no North West location"

    prose_hits = keyword_hits(prose, NORTH_WEST_TERMS)
    if prose_hits:
        return "REVIEW", "North West term appears only in title/description: " + ", ".join(prose_hits[:3])
    return "REVIEW", "No usable delivery/site location evidence"


def score_record(record, services, region, min_value, max_value):
    fulltext = " ".join([
        record.get("title", ""), record.get("description", ""),
        record.get("buyer", ""),
    ]).lower()
    cleaning_hits = keyword_hits(fulltext, CLEANING_TERMS)
    fm_hits = keyword_hits(fulltext, FM_TERMS)
    exclude_hits = keyword_hits(fulltext, EXCLUDE_TERMS)
    if "cleaning" in services and cleaning_hits:
        service = "Cleaning"
    elif "fm" in services and fm_hits:
        service = "Facilities management"
    else:
        return None
    if exclude_hits and not (cleaning_hits or fm_hits):
        return None

    score, reasons = 3, []
    reasons.append("service keywords: " + ", ".join((cleaning_hits + fm_hits)[:4]))
    geo_class, geo_reason = classify_geography(record, region)
    record["region_match"] = geo_class
    record["geography_reason"] = geo_reason
    if region == "North West":
        if geo_class == "OUT_OF_AREA":
            return None
        if geo_class == "LOCAL":
            score += 3
        else:
            score -= 1
            reasons.append("location requires manual verification")
    else:
        score += 1
        reasons.append("UK-wide search")

    value = record.get("estimated_value_gbp", "")
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        numeric_value = None
    if numeric_value is not None:
        if min_value is not None and numeric_value < min_value:
            return None
        if max_value is not None and numeric_value > max_value:
            return None
        score += 1
        reasons.append("estimated value published")
    else:
        reasons.append("value not published")

    deadline_dt = parse_date(record.get("deadline"))
    if deadline_dt:
        if deadline_dt < datetime.now(timezone.utc):
            return None
        score += 2
        reasons.append("deadline appears to be in the future")
        deadline_status = "Future date in feed — verify official notice"
    else:
        deadline_status = "Deadline missing — manual check required"
        reasons.append("deadline missing or unparsed")

    if exclude_hits and (cleaning_hits or fm_hits):
        score -= 1
        reasons.append("mixed keywords; inspect scope")

    record.update({
        "service_match": service,
        "score": score,
        "score_reasons": "; ".join(reasons),
        "status": deadline_status,
    })
    return record


def dedupe(records):
    seen = set()
    result = []
    for row in records:
        key = row.get("ocid") or (row.get("title", "").lower(), row.get("buyer", "").lower())
        if not key or key == "":
            key = (row.get("title", "").lower(), row.get("buyer", "").lower(), row.get("published_date", ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in FIELDS})


def main():
    parser = argparse.ArgumentParser(description="TenderScout official-feed collector and filter")
    parser.add_argument("--days", type=int, default=14, help="Look-back window for publication/update date (default 14)")
    parser.add_argument("--region", choices=["North West", "UK wide"], default="North West")
    parser.add_argument("--services", nargs="+", choices=["cleaning", "fm"], default=["cleaning", "fm"])
    parser.add_argument("--min-value", type=float, default=None, help="Minimum estimated GBP value")
    parser.add_argument("--max-value", type=float, default=None, help="Maximum estimated GBP value")
    parser.add_argument("--min-score", type=int, default=3, help="Minimum relevance score (default 3)")
    parser.add_argument("--pages", type=int, default=5, help="Maximum pages to fetch from each source")
    parser.add_argument("--out", default="tenderscout_results.csv", help="Filtered output CSV path")
    args = parser.parse_args()

    print("TenderScout v1 — fetching official public procurement feeds...")
    all_rows = fetch_fts(args.days, args.pages) + fetch_contracts_finder(args.days, args.pages)
    all_rows = dedupe(all_rows)
    if not all_rows:
        print("No feed records retrieved. Check your internet connection/API availability and try again.")
        print("No CSV was written because no source records were received.")
        sys.exit(2)

    filtered = []
    for row in all_rows:
        scored = score_record(row, args.services, args.region, args.min_value, args.max_value)
        if scored and scored["score"] >= args.min_score:
            filtered.append(scored)
    filtered.sort(key=lambda r: (r.get("score", 0), r.get("published_date", "")), reverse=True)

    all_path = Path("tenderscout_all.csv")
    write_csv(all_path, all_rows)
    out_path = Path(args.out)
    write_csv(out_path, filtered)

    print(f"Fetched/deduplicated notices: {len(all_rows)}")
    print(f"Matches after filters: {len(filtered)}")
    print(f"All fetched records: {all_path.resolve()}")
    print(f"Filtered shortlist: {out_path.resolve()}")
    print("\nTop matches:")
    for row in filtered[:10]:
        print(f"- [{row['score']}] {row['title']} | {row['buyer']} | {row['deadline'] or 'deadline not found'}")
        print(f"  {row['notice_url'] or 'No official URL extracted — search title on source site'}")
    print("\nAlways verify scope, geography, deadline, eligibility and documents on the official notice.")


if __name__ == "__main__":
    main()
