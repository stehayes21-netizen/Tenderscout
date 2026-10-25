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
    "commercial cleaning", "office cleaning", "contract cleaning",
    "building cleaning", "school cleaning", "communal cleaning",
    "cleansing services",
]
FM_TERMS = [
    "facilities management", "facility management", "hard fm", "soft fm",
    "building maintenance", "estates maintenance", "caretaking",
    "caretaker", "grounds maintenance", "waste management", "pest control",
    "planned preventative maintenance", "ppm", "property maintenance",
]
MAINTENANCE_TERMS = [
    "property maintenance", "building maintenance", "estates maintenance",
    "repairs and maintenance", "repair and maintenance", "maintenance and repair",
    "responsive repairs", "reactive repairs", "reactive maintenance",
    "planned maintenance", "cyclical maintenance", "void repairs", "void works",
    "void property", "housing repairs", "property repairs", "minor works",
    "painting and decorating", "decoration services", "external decoration",
    "gas servicing", "gas safety", "boiler servicing", "heating maintenance",
    "electrical maintenance", "electrical testing", "fixed wire testing",
    "fire alarm", "emergency lighting", "roofing", "roof repair", "plumbing",
    "joinery", "handyman", "damp proofing", "window repairs", "door repairs",
    "lift maintenance", "legionella", "fire safety works",
]
CONSTRUCTION_TERMS = [
    "construction", "refurbishment", "refurbish", "building works",
    "building contractor", "new build", "demolition", "groundworks",
    "fit out", "fit-out", "renovation", "design and build", "retrofit",
    "extension works", "conversion works", "building alterations",
    "civil engineering", "structural works", "remodelling", "redevelopment",
]
EXCLUDE_TERMS = [
    "software", "information technology", "it equipment", "laptop",
    "cyber security", "telecommunications", "road construction",
    "architectural services",
]

# Order matters: a tender that matches several trades gets the first label.
SERVICE_ORDER = ["cleaning", "fm", "maintenance", "construction"]
SERVICE_LABELS = {
    "cleaning": "Cleaning",
    "fm": "Facilities management",
    "maintenance": "Property maintenance",
    "construction": "Construction",
}

# --- Target area ------------------------------------------------------------
# Core areas: Liverpool, Sefton, Wirral, St Helens and Halton.
# Wider areas: Knowsley, Warrington, Wigan, West Lancashire and Cheshire West and
# Chester (Chester, Ellesmere Port, Neston, Northwich, Winsford, Frodsham).
# Postcodes are the main evidence because place names are ambiguous. Royal Mail
# postcode districts do not line up exactly with council boundaries, so borderline
# districts go to the review queue instead of the shortlist.
# Edit these sets to change the area.
TARGET_AREA_LABEL = "Target area"

LOCAL_POSTCODE_DISTRICTS = {
    # Liverpool
    *(f"L{n}" for n in range(1, 20)), "L24", "L25", "L27",
    # Sefton
    "L20", "L21", "L22", "L23", "L29", "L30", "L31", "L37", "L38", "PR8", "PR9",
    # Wirral
    *(f"CH{n}" for n in range(41, 50)), "CH60", "CH61", "CH62", "CH63",
    # St Helens
    "WA9", "WA10", "WA11", "WA12",
    # Halton
    "WA7", "WA8",
    # Knowsley
    "L26", "L28", "L32", "L33", "L34", "L35", "L36",
    # Warrington
    "WA1", "WA2", "WA3", "WA4", "WA5", "WA13",
    # Wigan
    "WN1", "WN2", "WN3", "WN4", "WN5", "WN6", "WN7",
    # West Lancashire
    "L39", "L40", "WN8",
    # Cheshire West and Chester
    "CH1", "CH2", "CH3", "CH64", "CH65", "CH66", "WA6", "CW6", "CW7", "CW8", "CW9",
}
# Districts that straddle the target area and a neighbour (CH4 includes parts of
# Wales).
REVIEW_POSTCODE_DISTRICTS = {"CH4"}

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
    # Knowsley
    "knowsley", "huyton", "prescot", "halewood", "stockbridge village",
    "tarbock", "roby", "cronton",
    # Warrington
    "warrington", "culcheth", "lymm", "padgate", "stockton heath", "latchford",
    "grappenhall", "burtonwood", "winwick", "great sankey", "penketh", "thelwall",
    # Wigan
    "wigan", "hindley", "standish", "golborne", "tyldesley", "atherton",
    "ashton-in-makerfield", "ince-in-makerfield", "shevington", "aspull",
    "pemberton",
    # West Lancashire
    "west lancashire", "ormskirk", "skelmersdale", "burscough", "aughton",
    "up holland",
    # Cheshire West and Chester
    "cheshire west", "west cheshire", "chester", "ellesmere port", "neston",
    "northwich", "winsford", "frodsham", "helsby", "tarporley",
]
# Names that also exist in other parts of the UK. On their own they only send a
# tender to the review list; a postcode or a clear place name must confirm them.
AMBIGUOUS_PLACE_TERMS = [
    "allerton", "woolton", "garston", "walton", "kirkdale", "upton", "moreton",
    "eccleston", "halton", "orrell", "thornton", "sutton", "kirkby", "whiston",
    "birchwood", "leigh", "astley",
]
# Too broad to confirm the target area on their own.
WEAK_AREA_TERMS = ["merseyside", "north west", "cheshire", "lancashire",
                   "greater manchester"]
# Places that clearly are not in the target area, including everything in Scotland.
OUT_OF_AREA_TERMS = [
    "liverpool street", "chester-le-street", "crewe", "cheshire east",
    "macclesfield", "knutsford", "altrincham",
    "manchester", "salford", "bolton", "bury", "rochdale", "oldham",
    "stockport", "tameside", "trafford", "blackburn", "blackpool", "preston",
    "lancaster", "cumbria", "barrow-in-furness", "northumberland", "chorley",
    "leeds", "sheffield", "nottingham", "london", "birmingham", "bristol",
    "cardiff", "newcastle", "oxford", "cambridge", "wales", "northern ireland",
    "belfast", "flintshire", "wrexham",
    "scotland", "scottish", "glasgow", "edinburgh", "aberdeen", "dundee",
    "inverness", "perth", "stirling", "falkirk", "paisley", "ayr",
    "livingston", "kirkcaldy", "lanarkshire", "ayrshire", "fife", "highland",
    "highlands", "dumfries", "argyll", "lothian", "renfrewshire",
    "dunbartonshire", "clackmannanshire", "moray", "angus",
]

FULL_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b")
DISTRICT_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\b")

# Find a Tender and Contracts Finder often give the region as a NUTS code instead
# of a postcode. These codes are confirmed against the published code lists:
# UKD61 Warrington, UKD63 Cheshire West and Chester, UKD71 East Merseyside
# (Knowsley, St Helens, Halton), UKD72 Liverpool, UKD73 Sefton, UKD74 Wirral.
# Codes that cover the target area plus places outside it (Greater Manchester,
# Lancashire, the broad Cheshire and North West codes) need a postcode or place
# name to confirm. Every other UK code is outside.
NUTS_RE = re.compile(r"\bUK([C-N])(\d{0,2})\b")
LOCAL_NUTS = {"UKD61", "UKD63", "UKD71", "UKD72", "UKD73", "UKD74"}
REVIEW_NUTS = {
    "UKD", "UKD3", "UKD31", "UKD32", "UKD34", "UKD35", "UKD36", "UKD37",
    "UKD4", "UKD43", "UKD47", "UKD6", "UKD7",
}


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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        # Keep the feed's own explanation, so a rejected request can be understood.
        try:
            body = " ".join(exc.read().decode("utf-8", errors="replace").split())[:200]
        except Exception:
            body = ""
        query = urllib.parse.urlparse(url).query
        raise RuntimeError(
            f"HTTP {exc.code} {exc.reason}. Feed said: {body or 'nothing'}. Request: ?{query}"
        ) from None


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


API_HOSTS = {"www.find-tender.service.gov.uk", "www.contractsfinder.service.gov.uk"}


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
            # Follow the link the feed gives us exactly as given, but only if it
            # points at one of the official feeds.
            if parsed.hostname in API_HOSTS:
                return urllib.parse.urlunparse(parsed._replace(scheme="https"))
            values = urllib.parse.parse_qs(parsed.query).get("cursor")
            if values:
                return values[0]
    return None


def _page_url(base_url, params, cursor):
    """Build the next request URL, supporting opaque cursors and next links."""
    if cursor and cursor.startswith("https://"):
        return cursor
    page_params = dict(params)
    if cursor:
        page_params["cursor"] = cursor
    return base_url + "?" + urllib.parse.urlencode(page_params)


def _start_stats(stats):
    stats = stats if stats is not None else {}
    stats.update({"pages": 0, "records": 0, "last_batch": 0, "more": False,
                  "error": "", "stopped": "", "windows": 0, "full_end": False})
    return stats


def _note_page(stats, batch):
    """Record what a feed page returned, so a paging problem can be seen."""
    stats["pages"] += 1
    stats["records"] += len(batch)
    stats["last_batch"] = len(batch)
    dates = sorted(str(r.get("published_date", ""))[:10] for r in batch if r.get("published_date"))
    if dates:
        stats["first_date"] = min(stats.get("first_date") or dates[0], dates[0])
        stats["last_date"] = max(stats.get("last_date") or dates[-1], dates[-1])


def _windows(days, size=7):
    """Split the look-back period into week-long windows, newest first.

    The feeds seem to cap how much one search returns, so smaller windows reach
    more notices, and the newest weeks are always fetched before any time limit.
    """
    now = datetime.now(timezone.utc)
    earliest = now - timedelta(days=days)
    windows = []
    end = now
    while end > earliest:
        start = max(earliest, end - timedelta(days=size))
        windows.append((start, end))
        end = start
    return windows


def _page_through(base_url, params, source, max_pages, started, max_seconds, stats):
    """Read one search page by page, following the feed's own next-page links."""
    results = []
    cursor = None
    for _ in range(max_pages):
        if max_seconds and time.monotonic() - started > max_seconds:
            print("[WARN] Stopped early: the time limit for this source was reached", file=sys.stderr)
            stats["stopped"] = "time limit"
            return results
        url = _page_url(base_url, params, cursor)
        try:
            payload = get_json(url)
        except Exception as exc:
            print(f"[WARN] {source} fetch failed: {exc}", file=sys.stderr)
            stats["error"] = str(exc)[:320]
            return results
        batch = extract_records(payload, source)
        if not batch:
            return results
        _note_page(stats, batch)
        results.extend(batch)
        cursor = _next_cursor(payload)
        if not cursor:
            if len(batch) >= 100:
                stats["full_end"] = True
            return results
        time.sleep(0.2)
    stats["more"] = True  # used every allowed page and the feed still had more
    return results


def fetch_fts(days, max_pages, max_seconds=None, stats=None):
    stats = _start_stats(stats)
    results = []
    started = time.monotonic()
    for start, end in _windows(days):
        if stats["stopped"]:
            break
        stats["windows"] += 1
        params = {
            "stages": "tender", "limit": "100",
            "updatedFrom": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "updatedTo": end.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        results.extend(_page_through(FTS_URL, params, "Find a Tender", max_pages,
                                     started, max_seconds, stats))
        if stats["error"]:
            break
    return results


def fetch_contracts_finder(days, max_pages, max_seconds=None, stats=None):
    stats = _start_stats(stats)
    results = []
    started = time.monotonic()
    for start, end in _windows(days):
        if stats["stopped"]:
            break
        stats["windows"] += 1
        params = {
            "publishedFrom": start.date().isoformat(),
            "publishedTo": end.date().isoformat(),
            "stages": "planning,tender",
            "limit": "100",
        }
        results.extend(_page_through(CF_URL, params, "Contracts Finder", max_pages,
                                     started, max_seconds, stats))
        if stats["error"]:
            break
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


def drop_contained(hits, longer_hits):
    """Drop a hit that is just part of a longer matched name.

    "preston" inside "preston brook", or "cheshire" inside "cheshire east", should
    not count on its own: the longer, more specific name decides.
    """
    kept = []
    for hit in hits:
        pattern = r"(?<![a-z])" + re.escape(hit) + r"(?![a-z])"
        if any(len(other) > len(hit) and re.search(pattern, other) for other in longer_hits):
            continue
        kept.append(hit)
    return kept


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

    local_raw = place_hits(location_text, LOCAL_PLACE_TERMS)
    out_raw = place_hits(location_text, OUT_OF_AREA_TERMS)
    # The longer, more specific name wins: "preston" is not Preston Brook, and
    # "chester" is not Chester-le-Street.
    broad_raw = place_hits(location_text, WEAK_AREA_TERMS)
    local_places = drop_contained(local_raw, out_raw)
    out_places = drop_contained(out_raw, local_raw + broad_raw)
    named = local_places + out_places
    ambiguous = drop_contained(place_hits(location_text, AMBIGUOUS_PLACE_TERMS), named)
    broad = drop_contained(broad_raw, named)

    strong_local = pc_local + nuts_local + local_places
    strong_out = pc_other + nuts_out + out_places
    # Place-level doubt (a shared name, a borderline postcode) means a person
    # should look. Broad region words and codes only describe the wider region, so
    # on their own they never rescue a tender whose named place is outside.
    weak_place = pc_review + ambiguous
    weak = weak_place + nuts_weak + broad

    if strong_local and strong_out:
        return "REVIEW", (
            "Multiple delivery/site locations across target and non-target areas: "
            + ", ".join((strong_local + strong_out)[:5])
        )
    if strong_local:
        return "LOCAL", (
            "Structured delivery/site location: " + ", ".join(strong_local[:3])
        )
    if strong_out and weak_place:
        return "REVIEW", (
            "Delivery/site location is mixed or borderline: "
            + ", ".join((weak_place + strong_out)[:5])
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
    trade_hits = {
        "cleaning": keyword_hits(fulltext, CLEANING_TERMS),
        "fm": keyword_hits(fulltext, FM_TERMS),
        "maintenance": keyword_hits(fulltext, MAINTENANCE_TERMS),
        "construction": keyword_hits(fulltext, CONSTRUCTION_TERMS),
    }
    exclude_hits = keyword_hits(fulltext, EXCLUDE_TERMS)
    matched = [t for t in SERVICE_ORDER if t in services and trade_hits[t]]
    if not matched:
        return None
    service = SERVICE_LABELS[matched[0]]
    service_hits = [term for t in matched for term in trade_hits[t]]

    score, reasons = 3, []
    reasons.append("service keywords: " + ", ".join(dict.fromkeys(service_hits))[:80])
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

    if exclude_hits:
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
    parser.add_argument("--days", type=int, default=60, help="Look-back window for publication/update date (default 60)")
    parser.add_argument(
        "--region", choices=[TARGET_AREA_LABEL, "North West", "UK wide"], default=TARGET_AREA_LABEL,
        help="'Target area' = Liverpool, Sefton, Wirral, St Helens, Halton ('North West' is an alias)")
    parser.add_argument("--services", nargs="+", choices=SERVICE_ORDER, default=SERVICE_ORDER)
    parser.add_argument("--min-value", type=float, default=None, help="Minimum estimated GBP value")
    parser.add_argument("--max-value", type=float, default=None, help="Maximum estimated GBP value")
    parser.add_argument("--min-score", type=int, default=3, help="Minimum relevance score (default 3)")
    parser.add_argument("--pages", type=int, default=10, help="Maximum pages to fetch from each source")
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
