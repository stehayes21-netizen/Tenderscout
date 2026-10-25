#!/usr/bin/env python3
"""
TenderScout v1 — UK tender collector and filter
Uses official public OCDS feeds; Python 3.10+; standard library only.

Run:
  python tenderscout.py
  python tenderscout.py --days 14 --region "Target area" --min-score 3
  python tenderscout.py --services cleaning fm --out my_tenders.csv

Outputs:
  tenderscout_results.csv  - filtered shortlist (opens in Excel/Google Sheets)
  tenderscout_review.csv   - tenders with uncertain or missing locations requiring manual review
  tenderscout_all.csv      - all fetched tender-stage notices, for audit

Important:
- Public feeds may be delayed or omit fields; always open the official notice.
- Geography matching uses postcode districts first, then place names. It is a heuristic,
  not a definitive location check. Target area: Liverpool, Sefton, Wirral, St Helens, Halton.
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

# --- Target area ------------------------------------------------------------
# Only tenders delivered in these five council areas are shortlisted:
# Liverpool, Sefton, Wirral, St Helens and Halton.
# Postcodes are the main evidence because place names are ambiguous. Royal Mail
# postcode districts do not line up exactly with council boundaries, so
# borderline districts go to the review queue instead of the shortlist.
# Edit these sets to change the area.
TARGET_AREA_LABEL = "Target area"

LOCAL_POSTCODE_DISTRICTS = {
    # Liverpool (L24 also covers Hale in Halton)
    *(f"L{n}" for n in range(1, 20)), "L24", "L25", "L27",
    # Sefton
    "L20", "L21", "L22", "L23", "L29", "L30", "L31", "L37", "L38", "PR8", "PR9",
    # Wirral
    *(f"CH{n}" for n in range(41, 50)), "CH60", "CH61", "CH62", "CH63",
    # St Helens
    "WA9", "WA10", "WA11", "WA12",
    # Halton
    "WA7", "WA8",
}
# Districts that straddle the target area and its neighbours.
REVIEW_POSTCODE_DISTRICTS = {"L35", "CH64"}

LOCAL_PLACE_TERMS = [
    # Liverpool
    "liverpool", "wavertree", "aigburth", "childwall", "croxteth", "fazakerley",
    "norris green", "tuebrook", "old swan", "gateacre", "speke", "toxteth",
    "anfield", "west derby", "mossley hill", "sefton park", "cressington",
    # Sefton
    "sefton", "southport", "formby", "crosby", "great crosby", "blundellsands",
    "bootle", "litherland", "seaforth", "maghull", "lydiate", "melling",
    "netherton", "ainsdale", "birkdale", "aintree", "hightown", "churchtown",
    # Wirral
    "wirral", "birkenhead", "wallasey", "heswall", "bromborough", "bebington",
    "hoylake", "west kirby", "new brighton", "oxton", "prenton", "rock ferry",
    "tranmere", "seacombe", "egremont", "leasowe", "meols", "bidston",
    "port sunlight", "eastham", "clatterbridge", "pensby", "greasby", "irby",
    "thingwall", "noctorum", "claughton",
    # St Helens
    "st helens", "saint helens", "rainhill", "haydock", "newton-le-willows",
    "earlestown", "billinge", "rainford", "thatto hector", "clock face",
    "garswood", "windle",
    # Halton
    "halton borough", "borough of halton", "halton council", "runcorn", "widnes",
    "daresbury", "preston brook", "murdishaw", "castlefields", "palacefields",
    "weston point", "halton brook",
]
# Names that also exist in other parts of the UK. On their own they only send a
# tender to the review list; a postcode or a clear place name must confirm them.
AMBIGUOUS_PLACE_TERMS = [
    "allerton", "woolton", "garston", "walton", "kirkdale", "upton", "moreton",
    "eccleston", "halton", "orrell", "thornton", "sutton",
]
# Too broad to confirm the target area on their own.
WEAK_AREA_TERMS = ["merseyside", "north west", "cheshire", "lancashire", "neston"]
# Places that clearly are not in the target area (including Knowsley, which is
# deliberately excluded, and everything in Scotland).
OUT_OF_AREA_TERMS = [
    "knowsley", "kirkby", "huyton", "prescot", "whiston", "halewood",
    "liverpool street",
    "ormskirk", "skelmersdale", "ellesmere port", "warrington", "wigan",
    "chester", "northwich", "crewe",
    "manchester", "salford", "bolton", "bury", "rochdale", "oldham",
    "stockport", "tameside", "trafford", "blackburn", "blackpool", "preston",
    "lancaster", "cumbria", "barrow-in-furness", "northumberland",
    "leeds", "sheffield", "nottingham", "london", "birmingham", "bristol",
    "cardiff", "newcastle", "oxford", "cambridge", "wales", "northern ireland",
    "belfast",
    "scotland", "scottish", "glasgow", "edinburgh", "aberdeen", "dundee",
    "inverness", "perth", "stirling", "falkirk", "paisley", "ayr",
    "livingston", "kirkcaldy", "lanarkshire", "ayrshire", "fife", "highland",
    "highlands", "dumfries", "argyll", "lothian", "renfrewshire",
    "dunbartonshire", "clackmannanshire", "moray", "angus",
]

FULL_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b")
DISTRICT_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\b")

# Find a Tender and Contracts Finder often give the region as a NUTS code instead
# of a postcode. UKD72 Liverpool, UKD73 Sefton and UKD74 Wirral are in the target
# area. UKD71 (East Merseyside) also covers Knowsley, which is excluded, so it
# needs a postcode or place name to confirm. Every other UK code is outside.
NUTS_RE = re.compile(r"\bUK([C-N])(\d{0,2})\b")
LOCAL_NUTS = {"UKD72", "UKD73", "UKD74"}
REVIEW_NUTS = {"UKD", "UKD7", "UKD71"}


def nuts_codes(text):
    return {"UK" + m.group(1) + m.group(2) for m in NUTS_RE.finditer(text.upper())}


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
    "location",
    "locations",
    "address",
    "addresses",
    "siteAddress",
    "site_address",
    "site",
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


def add_location_values(obj, location_parts):
    """Collect values only from explicit delivery/site location fields.

    We intentionally ignore buyer addresses and other generic organisation details
    unless they are nested inside a contract/site delivery structure.
    """
    if obj is None:
        return
    if isinstance(obj, str):
        text = obj.strip()
        if text:
            location_parts.append(text)
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

    # If the address/location object itself contains a nested address block, capture it.
    for nested_key in ("address", "location", "deliveryAddress", "deliveryLocation"):
        if nested_key in obj:
            add_location_values(obj[nested_key], location_parts)


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
            for candidate in (
                tender,
                rel,
                rel.get("contract") or {},
                *(tender.get("items") or []),
            ):
                add_location_values(candidate, location_parts)

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


def normalise_region(region):
    """'North West' is kept as an alias so older commands keep working."""
    return "UK wide" if region == "UK wide" else TARGET_AREA_LABEL


def place_hits(text, terms):
    """Whole-word matches, so 'bury' does not match 'Canterbury'."""
    return [
        term for term in terms
        if re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", text)
    ]


def postcode_districts(text, include_bare=False):
    """Return (full-postcode districts, bare target/review districts) found in text."""
    upper = text.upper()
    full = {m.group(1) for m in FULL_POSTCODE_RE.finditer(upper)}
    bare = set()
    if include_bare:
        known = LOCAL_POSTCODE_DISTRICTS | REVIEW_POSTCODE_DISTRICTS
        bare = {m.group(1) for m in DISTRICT_RE.finditer(upper) if m.group(1) in known}
    return full, bare


def classify_geography(record, region):
    """Classify a tender as LOCAL, OUT_OF_AREA or REVIEW from delivery/site evidence.

    Postcode districts are checked first, then whole-word place names.
    Anything uncertain goes to REVIEW rather than the shortlist.
    """
    if normalise_region(region) == "UK wide":
        return "UK-wide search", "UK-wide search"

    location_text = (record.get("location_text") or "").lower()
    prose = " ".join([
        record.get("title", ""),
        record.get("description", ""),
    ]).lower()

    full, bare = postcode_districts(location_text, include_bare=True)
    districts = full | bare
    pc_local = sorted(d for d in districts if d in LOCAL_POSTCODE_DISTRICTS)
    pc_review = sorted(d for d in districts if d in REVIEW_POSTCODE_DISTRICTS)
    pc_other = sorted(d for d in full if d not in LOCAL_POSTCODE_DISTRICTS
                      and d not in REVIEW_POSTCODE_DISTRICTS)

    nuts = nuts_codes(location_text)
    nuts_local = sorted(c for c in nuts if c in LOCAL_NUTS)
    nuts_weak = sorted(c for c in nuts if c in REVIEW_NUTS)
    nuts_out = sorted(c for c in nuts if c not in LOCAL_NUTS | REVIEW_NUTS)

    strong_local = pc_local + nuts_local + place_hits(location_text, LOCAL_PLACE_TERMS)
    strong_out = pc_other + nuts_out + place_hits(location_text, OUT_OF_AREA_TERMS)
    weak = pc_review + nuts_weak + place_hits(
        location_text, WEAK_AREA_TERMS + AMBIGUOUS_PLACE_TERMS)

    if strong_local and strong_out:
        return "REVIEW", (
            "Multiple delivery/site locations across target and non-target areas: "
            + ", ".join((strong_local + strong_out)[:5])
        )
    if strong_local:
        return "LOCAL", (
            "Structured delivery/site location: " + ", ".join(strong_local[:3])
        )
    if strong_out and weak:
        return "REVIEW", (
            "Delivery/site location is mixed or borderline: "
            + ", ".join((weak + strong_out)[:5])
        )
    if strong_out:
        return "OUT_OF_AREA", (
            "Structured delivery/site location outside preferred area: "
            + ", ".join(strong_out[:3])
        )
    if weak:
        return "REVIEW", (
            "Borderline area, cannot confirm it is in the target area: "
            + ", ".join(weak[:3])
        )

    prose_full, _ = postcode_districts(prose)
    # A full postcode written in the notice text is specific enough to trust when
    # nothing in the text points anywhere else.
    prose_local = sorted(d for d in prose_full if d in LOCAL_POSTCODE_DISTRICTS)
    prose_elsewhere = [d for d in prose_full if d not in LOCAL_POSTCODE_DISTRICTS]
    if prose_local and not prose_elsewhere and not place_hits(prose, OUT_OF_AREA_TERMS):
        return "LOCAL", (
            "Postcode found in the notice text, not a structured location "
            "(verify on the notice): " + ", ".join(prose_local[:3])
        )
    prose_hits = (
        sorted(d for d in prose_full if d in LOCAL_POSTCODE_DISTRICTS | REVIEW_POSTCODE_DISTRICTS)
        + place_hits(prose, LOCAL_PLACE_TERMS)
    )
    if prose_hits:
        return "REVIEW", (
            "Nearby place appears only in title/description: "
            + ", ".join(prose_hits[:3])
        )
    if location_text.strip():
        return "REVIEW", "Structured location is too vague to confirm preferred area"

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
    region = normalise_region(region)
    geo_class, geo_reason = classify_geography(record, region)
    record["region_match"] = geo_class
    record["geography_reason"] = geo_reason
    if region == TARGET_AREA_LABEL:
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
    parser.add_argument(
        "--region", choices=[TARGET_AREA_LABEL, "North West", "UK wide"], default=TARGET_AREA_LABEL,
        help="'Target area' = Liverpool, Sefton, Wirral, St Helens, Halton ('North West' is an alias)")
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
        if normalise_region(args.region) == TARGET_AREA_LABEL and scored.get("region_match") == "REVIEW":
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
