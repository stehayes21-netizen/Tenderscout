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
  tenderscout_review.csv   - tenders with uncertain or missing locations requiring manual review
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
    "merseyside", "liverpool", "knowsley",
    "prescot", "huyton", "kirkby",
    "sefton", "southport", "formby", "crosby", "bootle",
    "waterloo", "litherland", "maghull", "netherton",
    "wirral", "birkenhead", "wallasey", "moreton", "heswall",
    "bromborough", "eastham", "spital", "new brighton",
    "st helens", "saint helens", "rainhill", "haydock", "eccleston",
    "thatto hector", "whiston",
]

TARGET_POSTCODE_DISTRICTS = {
    # Liverpool (verified city-core districts, not broad boundary-crossing ranges)
    "L1": "Liverpool",
    "L2": "Liverpool",
    "L3": "Liverpool",
    "L4": "Liverpool",
    "L5": "Liverpool",
    "L6": "Liverpool",
    "L7": "Liverpool",
    "L8": "Liverpool",
    "L9": "Liverpool",
    "L15": "Liverpool",
    "L16": "Liverpool",
    "L17": "Liverpool",
    "L18": "Liverpool",
    "L19": "Liverpool",
    # Knowsley (districts verified as local to Knowsley)
    "L10": "Knowsley",
    "L11": "Knowsley",
    "L12": "Knowsley",
    "L13": "Knowsley",
    "L14": "Knowsley",
    "L25": "Knowsley",
    "L26": "Knowsley",
    "L27": "Knowsley",
    "L36": "Knowsley",
    # Sefton (districts verified as local to Sefton)
    "L20": "Sefton",
    "L21": "Sefton",
    "L22": "Sefton",
    "L23": "Sefton",
    "L29": "Sefton",
    "L30": "Sefton",
    "L31": "Sefton",
    "L37": "Sefton",
    "L38": "Sefton",
    "L39": "Sefton",
    # Halton
    "WA7": "Halton",
    "WA8": "Halton",
    # St Helens
    "WA9": "St Helens",
    "WA10": "St Helens",
    "WA11": "St Helens",
    "WA12": "St Helens",
    "WA13": "St Helens",
    "WA14": "St Helens",
    # Wirral
    "CH41": "Wirral",
    "CH42": "Wirral",
    "CH43": "Wirral",
    "CH44": "Wirral",
    "CH45": "Wirral",
    "CH46": "Wirral",
    "CH47": "Wirral",
    "CH48": "Wirral",
    "CH49": "Wirral",
    "CH60": "Wirral",
    "CH61": "Wirral",
    "CH62": "Wirral",
    "CH63": "Wirral",
    "CH64": "Wirral",
    "CH65": "Wirral",
    "CH66": "Wirral",
}

# Districts explicitly known to be outside the target area, used to reject confirmed
# delivery sites outside the preferred geography without over-classifying uncertain
# districts as out-of-area.
OUT_OF_AREA_POSTCODE_DISTRICTS = {
    "LS1",
    "M1",
    "M2",
    "M3",
    "M4",
    "M5",
    "M6",
    "M7",
    "M8",
    "M9",
    "B1",
    "NE1",
    "BD1",
    "PR1",
    "BL1",
    "OL1",
    "HX1",
    "WA1",
    "WA2",
    "WA3",
    "WA4",
    "WA5",
    "WA6",
}

# Postcode districts that are not reliably assignable without boundary-crossing or
# incomplete evidence; these must remain REVIEW rather than LOCAL or OUT_OF_AREA.
AMBIGUOUS_POSTCODE_DISTRICTS = {
    "L24",
    "L28",
    "L32",
    "L33",
    "L34",
    "L35",
    "CH25",
    "CH26",
    "CH27",
    "CH28",
    "CH29",
    "CH30",
    "CH31",
    "CH32",
    "CH33",
    "CH34",
}

FIELDS = [
    "source", "published_date", "title", "buyer", "description", "location_text",
    "service_match", "region_match", "geography_reason", "estimated_value_gbp",
    "deadline", "status", "score", "score_reasons", "notice_url", "ocid",
]

EXPLICIT_LOCATION_KEYS = (
    "deliveryAddress",
    "deliveryAddresses",
    "delivery_address",
    "deliveryLocation",
    "deliveryLocations",
    "delivery_location",
    "siteAddress",
    "site_address",
    "site",
    "sites",
    "location",
    "locations",
)
ADDRESS_TEXT_KEYS = (
    "streetAddress",
    "street_address",
    "addressLine",
    "address_line",
    "locality",
    "region",
    "postalCode",
    "postal_code",
    "countryName",
    "country_name",
    "town",
    "city",
    "county",
    "district",
    "suburb",
)


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


def _append_location_piece(value, location_parts):
    if value is None:
        return
    if isinstance(value, str):
        text = value.strip()
        if text:
            location_parts.append(text)
        return
    if isinstance(value, (int, float)):
        text = str(value).strip()
        if text:
            location_parts.append(text)
        return
    if isinstance(value, list):
        for item in value:
            _append_location_piece(item, location_parts)
        return
    if isinstance(value, dict):
        for key in ADDRESS_TEXT_KEYS:
            if key in value:
                _append_location_piece(value.get(key), location_parts)
        for nested_key in ("address", "location", "deliveryAddress", "deliveryLocation"):
            if nested_key in value:
                add_location_values(value[nested_key], location_parts)


def add_location_values(obj, location_parts):
    """Collect only verified delivery/site fields from actual OCDS items and contracts.

    We intentionally ignore buyer addresses, parties, document metadata, and generic
    organisation addresses. The actual delivery/site evidence lives in item/contract
    delivery/site structures and their nested address objects.
    """
    if obj is None:
        return
    if isinstance(obj, str):
        _append_location_piece(obj, location_parts)
        return
    if isinstance(obj, list):
        for item in obj:
            add_location_values(item, location_parts)
        return
    if not isinstance(obj, dict):
        return

    for key in EXPLICIT_LOCATION_KEYS:
        if key in obj:
            add_location_values(obj[key], location_parts)

    for key in ADDRESS_TEXT_KEYS:
        if key in obj:
            _append_location_piece(obj.get(key), location_parts)

    for nested_key in ("address", "location", "deliveryAddress", "deliveryLocation"):
        if nested_key in obj:
            add_location_values(obj[nested_key], location_parts)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _iter_delivery_roots(rel):
    """Yield only tender/contract/award item structures that can plausibly contain delivery sites."""
    if not isinstance(rel, dict):
        return

    tender = rel.get("tender") or {}
    if isinstance(tender, dict):
        yield tender
        for key in ("items", "lots"):
            for item in _as_list(tender.get(key)):
                if isinstance(item, (dict, list)):
                    yield item

    for key in ("contract", "contracts", "award", "awards"):
        for node in _as_list(rel.get(key)):
            if isinstance(node, (dict, list)):
                yield node
                if isinstance(node, dict):
                    for child_key in ("items", "lots"):
                        for item in _as_list(node.get(child_key)):
                            if isinstance(item, (dict, list)):
                                yield item

    for key in ("items", "lots"):
        for item in _as_list(rel.get(key)):
            if isinstance(item, (dict, list)):
                yield item


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

            location_parts = []
            for candidate in _iter_delivery_roots(rel):
                add_location_values(candidate, location_parts)

            unique_locations = []
            seen = set()
            for part in location_parts:
                key = str(part).strip()
                if not key or key.lower() in seen:
                    continue
                seen.add(key.lower())
                unique_locations.append(key)

            output.append({
                "source": source,
                "location_text": ", ".join(unique_locations),
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


def _postcode_district_from_string(value):
    if not value:
        return None
    compact = re.sub(r"[^A-Z0-9]", "", value.upper())
    if len(compact) < 5:
        return None
    match = re.match(r"([A-Z]{1,2}\d[A-Z\d]?)", compact)
    if not match:
        return None
    return match.group(1)


def _full_postcodes_in_text(value):
    if not value:
        return []
    matches = re.findall(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", value.upper())
    cleaned = []
    for match in matches:
        compact = re.sub(r"[^A-Z0-9]", "", match)
        if len(compact) >= 5:
            cleaned.append(compact)
    return cleaned


def _postcode_classification(location_text):
    if not location_text:
        return [], [], []
    local_postcodes = []
    out_postcodes = []
    recognised = []
    for postcode in _full_postcodes_in_text(str(location_text)):
        recognised.append(postcode)
        district = _postcode_district_from_string(postcode)
        if district in TARGET_POSTCODE_DISTRICTS:
            local_postcodes.append(postcode)
        elif district in AMBIGUOUS_POSTCODE_DISTRICTS:
            continue
        elif district in OUT_OF_AREA_POSTCODE_DISTRICTS:
            out_postcodes.append(postcode)
        elif district and district.startswith(("L", "WA", "CH")):
            continue
        elif district:
            out_postcodes.append(postcode)
    return local_postcodes, out_postcodes, recognised


def classify_geography(record, region):
    """Classify tenders using structured location evidence."""
    if region == "UK wide":
        return "UK-wide search", "UK-wide search"

    location_text = (record.get("location_text") or "")
    prose = " ".join([
        record.get("title", ""),
        record.get("description", "")
    ]).lower()

    local_terms = [
        term for term in NORTH_WEST_TERMS
        if term not in {"north west"}
    ]
    local_hit_terms = keyword_hits(location_text.lower(), local_terms)
    out_of_area_terms = [
        "manchester", "salford", "bolton", "bury",
        "rochdale", "oldham", "stockport", "tameside",
        "trafford", "blackburn", "blackpool", "preston",
        "lancaster", "cumbria", "barrow-in-furness",
        "leeds", "sheffield", "nottingham", "london",
        "birmingham", "bristol", "cardiff", "edinburgh",
        "glasgow", "newcastle", "oxford", "cambridge", "scotland", "aberdeen", "dundee",
        "inverness", "perth", "stirling", "falkirk", "paisley", "ayr", "livingston",
        "kirkcaldy", "warrington", "runcorn", "widnes", "chester",
        "cheshire", "lancashire", "northumberland",
    ]
    out_term_hits = keyword_hits(location_text.lower(), out_of_area_terms)
    local_postcodes, out_postcodes, all_postcodes = _postcode_classification(location_text)

    if local_postcodes and out_postcodes:
        return "REVIEW", (
            "Multiple delivery site postcodes across target and non-target areas: "
            + ", ".join(local_postcodes + out_postcodes)[:5]
        )

    if local_postcodes:
        return "LOCAL", (
            "Structured delivery/site postcode in target area: "
            + ", ".join(local_postcodes[:3])
        )

    if out_postcodes:
        return "OUT_OF_AREA", (
            "Structured delivery/site postcode outside preferred area: "
            + ", ".join(out_postcodes[:3])
        )

    if local_hit_terms and out_term_hits:
        return "REVIEW", (
            "Multiple delivery/site locations across target and non-target areas: "
            + ", ".join((local_hit_terms + out_term_hits)[:5])
        )

    if local_hit_terms:
        return "LOCAL", (
            "Structured delivery/site location: "
            + ", ".join(local_hit_terms[:3])
        )

    if out_term_hits:
        return "OUT_OF_AREA", (
            "Structured delivery/site location outside preferred area: "
            + ", ".join(out_term_hits[:3])
        )

    prose_hits = keyword_hits(prose, local_terms)
    if prose_hits:
        return "REVIEW", (
            "Nearby place appears only in title/description: "
            + ", ".join(prose_hits[:3])
        )
    if location_text.strip():
        if all_postcodes:
            return "REVIEW", "Malformed or unusable postcode evidence"
        return "REVIEW", (
            "Structured location is too vague to confirm preferred area"
        )

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
    review_rows = []
    for row in all_rows:
        scored = score_record(row, args.services, args.region, args.min_value, args.max_value)
        if not scored:
            continue
        if args.region == "North West" and scored.get("region_match") == "REVIEW":
            review_rows.append(scored)
            continue
        if scored.get("region_match") == "LOCAL" and scored["score"] >= args.min_score:
            filtered.append(scored)
    filtered.sort(key=lambda r: (r.get("score", 0), r.get("published_date", "")), reverse=True)
    review_rows.sort(key=lambda r: (r.get("score", 0), r.get("published_date", "")), reverse=True)

    all_path = Path("tenderscout_all.csv")
    write_csv(all_path, all_rows)
    out_path = Path(args.out)
    write_csv(out_path, filtered)
    review_path = Path("tenderscout_review.csv")
    write_csv(review_path, review_rows)

    print(f"Fetched/deduplicated notices: {len(all_rows)}")
    print(f"Matches after filters: {len(filtered)}")
    print(f"Review queue: {len(review_rows)}")
    print(f"All fetched records: {all_path.resolve()}")
    print(f"Filtered shortlist: {out_path.resolve()}")
    print(f"Location review file: {review_path.resolve()}")
    print("\nTop matches:")
    for row in filtered[:10]:
        print(f"- [{row['score']}] {row['title']} | {row['buyer']} | {row['deadline'] or 'deadline not found'}")
        print(f"  {row['notice_url'] or 'No official URL extracted — search title on source site'}")
    if review_rows:
        print("\nReview queue:")
        for row in review_rows[:10]:
            print(f"- [{row['score']}] {row['title']} | {row['geography_reason']}")
    print("\nAlways verify scope, geography, deadline, eligibility and documents on the official notice.")


if __name__ == "__main__":
    main()
