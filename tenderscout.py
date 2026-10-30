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
- Geography matching uses explicit delivery/site evidence. Target area: Liverpool, Sefton,
  St Helens, Halton, Knowsley, Wirral and Warrington. Cheshire West and Chester is excluded.
- This script runs when you launch it. It is not a hosted service or scheduled job.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

FTS_URL = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
CF_URL = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"

# Detailed selectable trade taxonomy shared by public tenders and private leads.
TRADE_CATEGORIES = [
    "Commercial and office cleaning", "School and college cleaning",
    "Domestic and communal-area cleaning", "Deep cleaning", "Industrial cleaning",
    "Window cleaning", "Gutter cleaning", "Carpet and upholstery cleaning",
    "End-of-tenancy cleaning", "Waste removal and clearance", "Pest control",
    "Grounds maintenance and gardening", "Landscaping", "Tree surgery",
    "Facilities management", "Caretaking and concierge", "Security and guarding",
    "CCTV and access control", "Fire alarms and fire safety", "Emergency lighting",
    "Electrical installation and repairs", "Electrical testing and inspection",
    "Plumbing", "Heating and boilers", "Gas safety and servicing",
    "Air conditioning and ventilation", "Renewables and solar", "Roofing and roof repairs",
    "Roofline, fascias and soffits", "Building and general maintenance",
    "Handyman and odd jobs", "Joinery and carpentry", "Doors and windows",
    "Glazing", "Bricklaying and masonry", "Plastering and rendering",
    "Painting and decorating", "Tiling", "Flooring", "Kitchens and bathrooms",
    "Damp proofing and waterproofing", "Insulation", "Scaffolding",
    "Groundworks and excavation", "Drainage", "Roads and paving",
    "Driveways and patios", "Fencing and gates", "Building refurbishment",
    "Extensions and conversions", "Demolition", "Asbestos services",
    "Lift and escalator maintenance", "Water hygiene and legionella",
    "Building surveying", "Architecture and design", "Structural engineering",
    "Quantity surveying", "Construction project management", "Waste management",
    "Laundry and linen", "Catering and food services", "Furniture and equipment",
    "IT and telecoms", "Transport and vehicle maintenance", "Water and fire damage restoration",
    "Interior design", "Signage and sign making", "Rope access and specialist access",
    "Environmental services", "Other / unclassified",
]
assert len(TRADE_CATEGORIES) == 71

TRADE_KEYWORDS = {
    "Commercial and office cleaning": ["commercial cleaning", "office cleaning", "contract cleaning", "building cleaning"],
    "School and college cleaning": ["school cleaning", "college cleaning", "education cleaning"],
    "Domestic and communal-area cleaning": ["communal cleaning", "domestic cleaning", "housing cleaning"],
    "Deep cleaning": ["deep clean", "deep cleaning", "specialist cleaning"],
    "Industrial cleaning": ["industrial cleaning", "factory cleaning", "industrial hygiene"],
    "Window cleaning": ["window cleaning", "glass cleaning"],
    "Gutter cleaning": ["gutter cleaning", "gutter clearance"],
    "Carpet and upholstery cleaning": ["carpet cleaning", "upholstery cleaning"],
    "End-of-tenancy cleaning": ["end of tenancy", "end-of-tenancy", "void cleaning"],
    "Waste removal and clearance": ["waste removal", "rubbish clearance", "house clearance"],
    "Pest control": ["pest control", "vermin control", "rodent control"],
    "Grounds maintenance and gardening": ["grounds maintenance", "garden maintenance", "grass cutting"],
    "Landscaping": ["landscaping", "landscape works"],
    "Tree surgery": ["tree surgery", "arboriculture", "arboricultural", "tree works", "tree maintenance", "tree planting", "tree pruning", "tree felling"],
    "Facilities management": ["facilities management", "facility management", "hard fm", "soft fm"],
    "Caretaking and concierge": ["caretaking", "caretaker", "concierge"],
    "Security and guarding": ["security guarding", "manned guarding", "security services"],
    "CCTV and access control": ["cctv", "access control", "intruder alarm"],
    "Fire alarms and fire safety": ["fire alarm", "fire safety", "fire detection"],
    "Emergency lighting": ["emergency lighting"],
    "Electrical installation and repairs": ["electrical installation", "electrical repairs", "electrician", "mechanical and electrical", "mechanical & electrical", "m&e services", "m and e services"],
    "Electrical testing and inspection": ["electrical testing", "fixed wire testing", "eicr"],
    "Plumbing": ["plumbing", "plumber", "drain tap"],
    "Heating and boilers": ["boiler servicing", "boiler inspection", "boiler maintenance", "boiler replacement", "heating maintenance", "heating system", "biomass boiler"],
    "Gas safety and servicing": ["gas safety", "gas servicing", "gas certificate"],
    "Air conditioning and ventilation": ["air conditioning", "ventilation", "hvac"],
    "Renewables and solar": ["solar pv", "solar panel", "renewable energy", "heat pump"],
    "Roofing and roof repairs": ["roofing", "roof repair", "roof replacement"],
    "Roofline, fascias and soffits": ["fascias", "soffits", "roofline"],
    "Building and general maintenance": ["building maintenance", "property maintenance", "responsive repairs", "facility maintenance", "facilities maintenance", "estate maintenance", "tenancy repairs"],
    "Handyman and odd jobs": ["handyman", "minor repairs", "odd jobs"],
    "Joinery and carpentry": ["joinery", "carpentry", "carpenter"],
    "Doors and windows": ["door repairs", "window repairs", "window replacement"],
    "Glazing": ["glazing", "double glazing", "glass replacement"],
    "Bricklaying and masonry": ["bricklaying", "masonry", "repointing"],
    "Plastering and rendering": ["plastering", "rendering", "plasterer"],
    "Painting and decorating": ["painting and decorating", "decorating", "external decoration"],
    "Tiling": ["tiling", "tiler", "wall and floor tiles"],
    "Flooring": ["flooring", "floor coverings", "vinyl flooring"],
    "Kitchens and bathrooms": ["kitchen installation", "bathroom installation", "kitchen refurbishment"],
    "Damp proofing and waterproofing": ["damp proofing", "waterproofing", "rising damp"],
    "Insulation": ["insulation", "loft insulation", "cavity wall insulation"],
    "Scaffolding": ["scaffolding", "scaffold hire"],
    "Groundworks and excavation": ["groundworks", "excavation", "earthworks"],
    "Drainage": ["drainage", "drain clearance", "sewer repair"],
    "Roads and paving": ["road resurfacing", "road repairs", "highway maintenance", "unadopted highways", "highway repairs", "hard surface repairs", "pavement repairs"],
    "Driveways and patios": ["driveway", "patio installation", "block paving"],
    "Fencing and gates": ["fencing", "gates installation", "boundary fencing"],
    "Building refurbishment": ["refurbishment", "building refurbishment", "property refurbishment"],
    "Extensions and conversions": ["building extension", "property conversion", "loft conversion"],
    "Demolition": ["demolition", "dismantling works"],
    "Asbestos services": ["asbestos removal", "asbestos survey", "asbestos management"],
    "Lift and escalator maintenance": ["lift maintenance", "elevator maintenance", "escalator"],
    "Water hygiene and legionella": ["legionella", "water hygiene", "water testing"],
    "Building surveying": ["building survey", "building surveying", "condition survey"],
    "Architecture and design": ["architectural services", "architectural design"],
    "Structural engineering": ["structural engineering", "structural survey"],
    "Quantity surveying": ["quantity surveying", "cost consultancy"],
    "Construction project management": ["construction project management", "project management construction", "project management and cost consultant", "project management & cost consultant"],
    "Waste management": ["waste management", "recycling services", "waste collection"],
    "Laundry and linen": ["laundry services", "linen services", "laundry and linen"],
    "Catering and food services": ["catering services", "food services", "school meals"],
    "Furniture and equipment": ["furniture supply", "furniture installation", "equipment supply"],
    "IT and telecoms": ["information technology", "telecommunications", "network infrastructure", "it and telecoms", "it support", "telecoms support"],
    "Transport and vehicle maintenance": ["vehicle maintenance", "fleet maintenance", "vehicle servicing", "fleet servicing", "vehicle repairs", "fleet repairs", "commercial vehicle maintenance"],
    "Water and fire damage restoration": ["water damage restoration", "fire damage restoration", "flood restoration"],
    "Interior design": ["interior design", "interior fit out design"],
    "Signage and sign making": ["signage", "sign making", "wayfinding signs"],
    "Rope access and specialist access": ["rope access", "industrial abseiling", "specialist access"],
    "Environmental services": ["environmental services", "environmental remediation", "contaminated land"],
    "Other / unclassified": [],
}

def classify_trade_category(fulltext):
    """Return the best detailed category; leave unmatched opportunities unclassified."""
    text = (fulltext or "").lower()
    matches = []
    for category, terms in TRADE_KEYWORDS.items():
        for term in terms:
            if term in text:
                matches.append((len(term), category))
    if matches:
        matches.sort(reverse=True)
        return matches[0][1]
    return "Other / unclassified"


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
# Approved service area: Liverpool, Sefton, St Helens, Halton, Knowsley, Wirral and Warrington.
# Cheshire West and Chester, and all other Cheshire areas, are excluded.
# Postcode districts are the main evidence. Any uncertain or mixed location goes
# to REVIEW; never loosen these rules merely to increase the number of matches.
TARGET_AREA_LABEL = "Target area"

LOCAL_POSTCODE_DISTRICTS = {
    # Liverpool
    *(f"L{n}" for n in range(1, 20)), "L24", "L25", "L27",
    # Sefton
    "L20", "L21", "L22", "L23", "L29", "L30", "L31", "L37", "L38", "PR8", "PR9",
    # St Helens
    "WA9", "WA10", "WA11", "WA12",
    # Halton
    "WA7", "WA8",
    # Warrington (WA6 is Cheshire West and Chester, so excluded)
    "WA1", "WA2", "WA3", "WA4", "WA5", "WA13",
    # Knowsley
    "L26", "L28", "L32", "L33", "L34", "L35", "L36",
    # Wirral
    *(f"CH{n}" for n in range(41, 50)), "CH60", "CH61", "CH62", "CH63",
}
# Cheshire West postcode districts are excluded; Wirral districts remain in-scope.
REVIEW_POSTCODE_DISTRICTS = set()
EXCLUDED_POSTCODE_DISTRICTS = {
    *(f"CH{n}" for n in range(1, 41)),
    "CH64", "CH65", "CH66", "WA6",
    *(f"CW{n}" for n in range(1, 10)),
    "WN1", "WN2", "WN3", "WN4", "WN5", "WN6", "WN7", "WN8",
    "L39", "L40",
}

LOCAL_PLACE_TERMS = [
    # Liverpool
    "liverpool", "wavertree", "aigburth", "childwall", "croxteth", "fazakerley",
    "norris green", "tuebrook", "old swan", "gateacre", "speke", "toxteth",
    "anfield", "west derby", "mossley hill", "sefton park", "cressington",
    # Sefton
    "sefton", "southport", "formby", "crosby", "great crosby", "blundellsands",
    "bootle", "litherland", "seaforth", "maghull", "lydiate", "melling",
    "netherton", "ainsdale", "birkdale", "aintree", "hightown", "churchtown",
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
    # Wirral
    "wirral", "birkenhead", "wallasey", "heswall", "bromborough", "bebington",
    "hoylake", "west kirby", "new brighton", "oxton", "prenton", "rock ferry",
    "tranmere", "moreton", "upton",
    # Warrington
    "warrington", "culcheth", "lymm", "padgate", "stockton heath", "latchford",
    "grappenhall", "burtonwood", "winwick", "great sankey", "penketh", "thelwall",
]
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
    "liverpool street", "chester-le-street", "cheshire west", "west cheshire",
    "chester", "ellesmere port", "neston", "northwich", "winsford", "frodsham",
    "helsby", "tarporley", "crewe", "cheshire east",
    "wigan", "hindley", "standish", "golborne", "tyldesley", "atherton",
    "ashton-in-makerfield", "ince-in-makerfield", "shevington", "aspull", "pemberton",
    "west lancashire", "ormskirk", "skelmersdale", "burscough", "aughton", "up holland",
    "macclesfield", "knutsford", "altrincham",
    "manchester", "salford", "bolton", "bury", "rochdale", "oldham",
    "stockport", "tameside", "trafford", "blackburn", "blackpool", "preston",
    "lancaster", "cumbria", "barrow-in-furness", "northumberland", "chorley",
    "leeds", "sheffield", "nottingham", "london", "birmingham", "bristol",
    "cardiff", "newcastle", "oxford", "cambridge", "wales", "northern ireland",
    # Regional labels in procurement feeds are useful when no postcode is supplied.
    # These are outside the configured target area and must not sit in its review queue.
    "east midlands", "west midlands", "east of england", "south east",
    "south west", "yorkshire and the humber", "north east",
    "north east england", "south east england", "south west england",
    "belfast", "flintshire", "wrexham",
    "stoke-on-trent", "samlesbury", "darent valley hospital",
    "lusaka", "singapore",
    "scotland", "scottish", "glasgow", "edinburgh", "aberdeen", "dundee",
    "inverness", "perth", "stirling", "falkirk", "paisley", "ayr",
    "livingston", "kirkcaldy", "lanarkshire", "ayrshire", "fife", "highland",
    "highlands", "dumfries", "argyll", "lothian", "renfrewshire",
    "dunbartonshire", "clackmannanshire", "moray", "angus",
]

FULL_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b")
DISTRICT_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\b")

# Find a Tender and Contracts Finder often give the region as a NUTS code instead
# of a postcode. UKD61 is Warrington; UKD71 is East Merseyside (Knowsley, St Helens,
# Halton); UKD72 Liverpool; UKD73 Sefton; and UKD74 Wirral. UKD63 (Cheshire West
# and Chester) is deliberately excluded. Broad codes require further evidence.
NUTS_RE = re.compile(r"\bUK([C-N])(\d{0,2})\b")
LOCAL_NUTS = {"UKD61", "UKD71", "UKD72", "UKD73", "UKD74"}
REVIEW_NUTS = {
    "UKD", "UKD3", "UKD31", "UKD32", "UKD34", "UKD35", "UKD36", "UKD37",
    "UKD4", "UKD43", "UKD47", "UKD6", "UKD7",
}


def nuts_codes(text):
    return {"UK" + m.group(1) + m.group(2) for m in NUTS_RE.finditer(text.upper())}

FIELDS = [
    "source", "published_date", "title", "buyer", "description", "location_text",
    "service_match", "region_match", "geography_reason", "estimated_value_gbp",
    "contract_start_date", "contract_end_date",
    "deadline", "status", "opportunity_stage", "score", "score_reasons", "notice_url", "ocid",
]

EXPLICIT_LOCATION_KEYS = (
    "deliveryAddress",
    "deliveryAddresses",
    "delivery_address",
    "deliveryLocation",
    "deliveryLocations",
    "delivery_location",
    "placeOfPerformance",
    "place_of_performance",
    "performanceLocation",
    "performance_location",
    "serviceLocation",
    "service_location",
    "siteLocation",
    "site_location",
    "workLocation",
    "work_location",
    "deliveryPoint",
    "delivery_point",
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


def source_name_for_url(url):
    """Return a short source label without exposing a cursor URL."""
    host = urllib.parse.urlparse(url).hostname or ""
    if "find-tender.service.gov.uk" in host:
        return "Find a Tender"
    if "contractsfinder.service.gov.uk" in host:
        return "Contracts Finder"
    return host or "Procurement feed"


def get_json(url: str, timeout: int = 25):
    """Fetch JSON and honour the source's Retry-After on one rate-limit response.

    Find a Tender has returned HTTP 429 with a 120-second Retry-After. Retrying
    immediately or shortening the normal request delay causes more throttling.
    Retry only once; if the feed is still limiting us, let the caller record a
    clear incomplete-coverage error instead of looping or hammering the source.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "TenderScout/1.0 (public procurement research)",
            "Accept": "application/json",
        },
    )
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            # Never print the full cursor URL: it can be enormous and makes the
            # mobile results page unreadable. Preserve Retry-After for diagnostics.
            try:
                body = " ".join(exc.read().decode("utf-8", errors="replace").split())[:180]
            except Exception:
                body = ""
            retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
            if exc.code == 429 and attempt == 0:
                # Use the server-provided delay. If absent/malformed, use the
                # known conservative 120-second minimum; never retry early.
                try:
                    delay = max(120, int(retry_after))
                except (TypeError, ValueError):
                    delay = 120
                print(f"[WARN] {source_name_for_url(url)} rate limited (HTTP 429); waiting {delay}s before one retry", file=sys.stderr)
                time.sleep(delay)
                continue
            retry_note = f" Retry after {retry_after} seconds." if retry_after.isdigit() else ""
            raise RuntimeError(
                f"HTTP {exc.code} {exc.reason}.{retry_note} Feed said: {body or 'no details returned'}"
            ) from None
    raise RuntimeError("Rate-limit retry exhausted.")


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
        raw = str(value).strip()
        # Procurement feeds sometimes publish a deadline as a date with no time.
        # Treat that date as inclusive through the end of the day; interpreting
        # YYYY-MM-DD as midnight would wrongly expire it at the start of that day.
        date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw))
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if date_only:
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
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


def add_location_values(obj, location_parts, in_location=False):
    """Collect delivery/site evidence without mistaking buyer metadata for a site.

    Location descriptions are common in OCDS feeds even when no structured
    address is supplied. Read a description only after entering an explicit
    delivery/site field; never read the tender's general description as a location.
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
            add_location_values(item, location_parts, in_location=in_location)
        return
    if not isinstance(obj, dict):
        return

    if in_location:
        description = obj.get("description")
        if isinstance(description, str) and description.strip():
            location_parts.append(description.strip())

    for key in EXPLICIT_LOCATION_KEYS:
        if key in obj:
            # A location becomes trusted context only through a known delivery,
            # site, or address field. Generic "description" is never a trigger.
            child_is_location = in_location or key in {
                "deliveryAddress", "deliveryAddresses", "delivery_address",
                "deliveryLocation", "deliveryLocations", "delivery_location",
                "placeOfPerformance", "place_of_performance",
                "performanceLocation", "performance_location",
                "serviceLocation", "service_location", "siteLocation",
                "site_location", "workLocation", "work_location",
                "deliveryPoint", "delivery_point", "location", "locations", "address", "addresses", "siteAddress",
                "site_address", "site",
            }
            add_location_values(obj[key], location_parts, in_location=child_is_location)

    # Some publishers wrap the actual site address one level deeper.
    for nested_key in ("address", "location", "deliveryAddress", "deliveryLocation"):
        if nested_key in obj and nested_key not in EXPLICIT_LOCATION_KEYS:
            add_location_values(obj[nested_key], location_parts, in_location=True)


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
            contact_point = buyer.get("contactPoint") or buyer.get("contact_point") or {}
            contact_name = as_text(first(contact_point, "name", "contactName"))
            contact_email = as_text(first(contact_point, "email", "emailAddress"))
            contact_phone = as_text(first(contact_point, "telephone", "phone"))
            eligibility = first(tender, "eligibility", "selectionCriteria", "selection_criteria")
            eligibility_text = as_text(eligibility)
            planning = rel.get("planning") or {}
            value = (
                first(tender, "value", "budget")
                or first(planning, "budget", "value")
                or first(rel, "value", "budget")
            )
            published = first(rel, "date", "publishedDate", "publicationDate")
            deadline = ""
            period = tender.get("tenderPeriod") or tender.get("tender_period") or {}
            if isinstance(period, dict):
                deadline = as_text(first(period, "endDate", "end_date", "end"))
            contract_period = (
                tender.get("contractPeriod") or tender.get("contract_period")
                or (rel.get("contract") or {}).get("period")
                or rel.get("contractPeriod") or rel.get("contract_period") or {}
            )
            if not isinstance(contract_period, dict):
                contract_period = {}
            contract_start_date = as_text(first(contract_period, "startDate", "start_date", "start"))
            contract_end_date = as_text(first(contract_period, "endDate", "end_date", "end"))
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
            # OCDS location extensions can place delivery evidence on tender
            # items, awards, or contracts. Inspect only those explicit
            # procurement structures; never infer a site from buyer metadata.
            location_candidates = [
                tender,
                rel,
                rel.get("contract") or {},
                *(tender.get("items") or []),
            ]
            for group_key in ("awards", "contracts"):
                groups = rel.get(group_key) or []
                if isinstance(groups, dict):
                    groups = [groups]
                if not isinstance(groups, list):
                    continue
                for group in groups:
                    if not isinstance(group, dict):
                        continue
                    location_candidates.append(group)
                    items = group.get("items") or []
                    if isinstance(items, list):
                        location_candidates.extend(items)
            for candidate in location_candidates:
                add_location_values(candidate, location_parts)

            raw_tags = rel.get("tag") or rel.get("tags") or []
            if isinstance(raw_tags, str):
                raw_tags = [raw_tags]
            tag_values = [as_text(tag).strip().lower() for tag in raw_tags if as_text(tag).strip()]
            tag_text = " ".join(tag_values)

            # Cancellation and award notices can retain the original tenderPeriod.
            # Do not mistake "tenderCancellation" for a live tender merely because
            # its tag contains the word "tender".
            notice_statuses = [
                as_text(first(tender, "status")),
                as_text(first(tender, "awardStatus")),
                as_text(first(rel, "status")),
            ]
            raw_awards = rel.get("awards") or []
            if isinstance(raw_awards, dict):
                raw_awards = [raw_awards]
            if isinstance(raw_awards, list):
                notice_statuses.extend(
                    as_text(first(award, "status"))
                    for award in raw_awards if isinstance(award, dict)
                )
            status_values = {value.strip().lower() for value in notice_statuses if value.strip()}
            status_text = " ".join(status_values)
            is_cancelled = (
                any("cancel" in value or "termination" in value for value in tag_values)
                or "cancel" in status_text
                or "terminated" in status_text
            )
            is_closed = bool(status_values.intersection({
                "unsuccessful", "withdrawn", "complete", "completed"
            }))
            is_planning = any(
                value in {"planning", "planningupdate"}
                or "pipeline" in value or "early market" in value
                for value in tag_values
            )
            # OCDS defines tenderUpdate and tenderAmendment as releases about
            # an existing tender, not awards or contract lifecycle notices.
            is_tender_notice = any(
                value in {"tender", "tenderupdate", "tenderamendment"}
                for value in tag_values
            )

            if is_cancelled or is_closed:
                opportunity_stage = "Other notice"
            elif is_planning:
                opportunity_stage = "Planning / upcoming"
            elif is_tender_notice or (not tag_values and (
                tender.get("tenderPeriod") or tender.get("tender_period")
            )):
                opportunity_stage = "Open for bids"
            else:
                opportunity_stage = "Other notice"

            # Trade/category evidence is often published on tender line items rather
            # than in the notice summary. Preserve readable item text for matching.
            item_parts = []
            tender_items = tender.get("items") or []
            if isinstance(tender_items, list):
                for item in tender_items:
                    if not isinstance(item, dict):
                        continue
                    for key in ("description", "name", "classification", "additionalClassifications"):
                        if key not in item:
                            continue
                        value_part = item[key]
                        if isinstance(value_part, dict):
                            value_part = first(value_part, "description", "name", "id")
                        elif isinstance(value_part, list):
                            value_part = " ".join(
                                as_text(first(v, "description", "name", "id") if isinstance(v, dict) else v)
                                for v in value_part
                            )
                        text_part = as_text(value_part).strip()
                        if text_part:
                            item_parts.append(text_part)

            output.append({
                "source": source,
                "opportunity_stage": opportunity_stage,
                "location_text": ", ".join(dict.fromkeys(location_parts)),
                "published_date": as_text(published),
                "title": title.strip(),
                "buyer": buyer_name.strip(),
                "description": desc.strip(),
                "trade_context": " ".join(dict.fromkeys(item_parts)),
                "contact_name": contact_name.strip(),
                "contact_email": contact_email.strip(),
                "contact_phone": contact_phone.strip(),
                "eligibility_text": eligibility_text.strip(),
                "estimated_value_gbp": money_value(value),
                "contract_start_date": contract_start_date,
                "contract_end_date": contract_end_date,
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
                  "error": "", "stopped": "", "windows": 0, "full_end": False,
                  "truncated_windows": 0, "last_page_had_cursor": False})
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


def _fts_windows(days, recent_days=10):
    """Search recent Find a Tender updates in daily slices, then older history weekly.

    Daily slices avoid a busy week's first 400 releases hiding a recent notice.
    Older history stays weekly so the hosted collector can cover more time within
    Find a Tender's rate limit.
    """
    now = datetime.now(timezone.utc)
    earliest = now - timedelta(days=days)
    recent_cutoff = max(earliest, now - timedelta(days=min(days, recent_days)))
    windows = []
    end = now
    while end > recent_cutoff:
        start = max(recent_cutoff, end - timedelta(days=1))
        windows.append((start, end))
        end = start
    end = recent_cutoff
    while end > earliest:
        start = max(earliest, end - timedelta(days=7))
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
        stats["last_page_had_cursor"] = bool(cursor)
        if not cursor:
            if len(batch) >= 100:
                stats["full_end"] = True
            return results
        # Find a Tender rate-limits bursty clients. Keep requests below a
        # conservative 10/minute cadence rather than hammering each page/window.
        time.sleep(10.5 if source == "Find a Tender" else 0.2)
    stats["more"] = True  # used every allowed page and the feed still had more
    stats["truncated_windows"] = stats.get("truncated_windows", 0) + 1
    print(
        f"[WARN] {source} pagination incomplete: reached the {max_pages}-page limit "
        "while the feed still supplied a next-page cursor; results for this window are partial",
        file=sys.stderr,
    )
    return results


def fetch_fts_notice(notice_id):
    """Fetch one known Find a Tender notice directly by its official notice ID."""
    notice_id = str(notice_id or "").strip()
    if not re.fullmatch(r"[0-9]{6}-[0-9]{4}", notice_id):
        raise ValueError("Find a Tender notice ID must look like 098300-2026")
    payload = get_json(f"{FTS_URL}/{urllib.parse.quote(notice_id, safe='')}")
    records = extract_records(payload, "Find a Tender")
    if not records:
        raise RuntimeError(f"Find a Tender returned no extractable record for {notice_id}")
    return records


def fetch_fts(days, max_pages, max_seconds=None, stats=None):
    stats = _start_stats(stats)
    results = []
    started = time.monotonic()
    # Daily windows for recent updates reduce the chance that a busy week hides
    # new notices behind the first few cursor pages; older history remains weekly.
    # Follow the official cursor for every window.
    for start, end in _fts_windows(days):
        if stats["stopped"]:
            break
        if stats["windows"] and not stats.get("last_page_had_cursor"):
            # If the previous window ended without a cursor, no page-level delay
            # was applied. Otherwise _page_through already waited before the next
            # request, so sleeping here too would halve coverage unnecessarily.
            time.sleep(10.5)
        stats["windows"] += 1
        # Do not send the API stages filter here. Find a Tender's documented
        # filter has historically omitted many Procurement Act 2023 notices,
        # including tender/tenderUpdate releases, when stages=tender is supplied.
        # Retrieve the complete release window and classify lifecycle locally.
        params = {
            "limit": "100",
            "updatedFrom": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "updatedTo": end.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        # Allow up to three pages per hosted window. This preserves the newer
        # daily slices and gives busy weekly windows more room before the source
        # time budget becomes the hard stop.
        # The source time budget remains the hard stop; CLI audits use their
        # requested page limit for every window.
        if max_seconds:
            window_page_limit = min(max_pages, 6)
        else:
            window_page_limit = max_pages
        results.extend(_page_through(FTS_URL, params, "Find a Tender", window_page_limit,
                                     started, max_seconds, stats))
        if stats["error"]:
            break
    coverage = "INCOMPLETE" if stats["more"] or stats["stopped"] or stats["error"] else "page-walk complete"
    print(
        "Find a Tender coverage: "
        f"{coverage}; windows={stats['windows']}; pages={stats['pages']}; "
        f"records={stats['records']}; truncated_windows={stats['truncated_windows']}; "
        f"stopped={stats['stopped'] or 'no'}; error={stats['error'] or 'none'}"
    )
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


def fetch_bearings(max_seconds=None, stats=None):
    """Fetch free Bearings tender listings as discovery leads, never as proof of delivery location.

    Bearings explicitly permits commercial use with attribution and links to the
    provider's own page. Its area filter means business eligibility rather than
    contract delivery location, so imported records deliberately have blank
    location_text and must pass through TenderScout's normal REVIEW safeguards.
    """
    stats = _start_stats(stats)
    started = time.monotonic()
    results = []
    params = {"area": "lcr,cheshire", "kind": "tender", "limit": "200", "sort": "newest"}
    url = "https://getbearings.co.uk/api/v1/opportunities?" + urllib.parse.urlencode(params)
    try:
        payload = get_json(url)
    except Exception as exc:
        stats["error"] = str(exc)[:320]
        print(f"[WARN] Bearings fetch failed: {exc}", file=sys.stderr)
        return results
    data = payload.get("data", []) if isinstance(payload, dict) else []
    if not isinstance(data, list):
        stats["error"] = "Unexpected Bearings API response shape"
        return results
    for item in data:
        if max_seconds and time.monotonic() - started > max_seconds:
            stats["stopped"] = "time limit"
            break
        if not isinstance(item, dict) or str(item.get("kind", "")).lower() != "tender":
            continue
        title = as_text(item.get("title")).strip()
        provider_url = as_text(item.get("url")).strip()
        if not title or not provider_url.startswith("https://"):
            continue
        bearings_url = as_text(item.get("bearings_url")).strip()
        description_parts = [as_text(item.get("summary")).strip(), as_text(item.get("conditions")).strip()]
        where = as_text(item.get("where")).strip()
        if where:
            description_parts.append("Eligibility/location wording from Bearings (not verified delivery site): " + where)
        if bearings_url.startswith("https://"):
            description_parts.append("Source credit: Bearings — " + bearings_url)
        results.append({
            "source": "Bearings (discovery; verify provider notice)",
            "opportunity_stage": "Other notice",
            "location_text": "",
            "published_date": as_text(item.get("first_seen") or ""),
            "title": title,
            "buyer": as_text(item.get("provider") or ""),
            "description": " ".join(part for part in description_parts if part),
            "contact_name": "", "contact_email": "", "contact_phone": "",
            "eligibility_text": as_text(item.get("conditions") or ""),
            "estimated_value_gbp": "",
            "contract_start_date": "", "contract_end_date": "",
            "deadline": as_text(item.get("closes") or ""),
            "status": "Bearings discovery listing — verify provider notice",
            "notice_url": provider_url,
            "ocid": as_text(item.get("id") or provider_url),
            "_raw": item,
        })
    stats.update({"pages": 1 if data else 0, "records": len(results), "last_batch": len(results),
                  "windows": 1, "more": False, "full_end": len(data) >= 200})
    return results


def fetch_standstill_northwest(max_seconds=None, stats=None):
    """Fetch free Standstill RSS discovery feeds, retaining source attribution.

    The regional feed and CPV-specific feeds widen discovery beyond the two
    official feeds. Aggregator geography is not delivery-site evidence, so all
    records still pass through normal geography classification and REVIEW.
    """
    stats = _start_stats(stats)
    feeds = [
        ("North West region", "https://standstill.uk/region/north-west/feed.xml"),
        ("Cleaning CPV 90910000", "https://standstill.uk/cpv/90910000/feed.xml"),
        ("Construction CPV 45000000", "https://standstill.uk/cpv/45000000/feed.xml"),
        ("Building installation maintenance CPV 50710000", "https://standstill.uk/cpv/50710000/feed.xml"),
        ("Refurbishment CPV 45453100", "https://standstill.uk/cpv/45453100/feed.xml"),
        ("Grounds maintenance CPV 77314000", "https://standstill.uk/cpv/77314000/feed.xml"),
        ("Waste services CPV 90500000", "https://standstill.uk/cpv/90500000/feed.xml"),
    ]
    started = time.monotonic()
    output = []
    errors = []
    feed_counts = {}

    def local_name(tag):
        return tag.rsplit("}", 1)[-1].lower()

    def child_text(node, *names):
        for child in list(node):
            if local_name(child.tag) in names:
                if child.text and child.text.strip():
                    return child.text.strip()
                href = child.attrib.get("href", "").strip()
                if href:
                    return href
        return ""

    def labelled_deadline(*texts):
        # RSS discovery feeds often put the closing date in prose instead of a
        # structured field. Parse only explicitly labelled closing/deadline text.
        combined = " ".join(text for text in texts if text)
        match = re.search(
            r"\b(?:closing(?: date)?|deadline|closes)\s*[:\-]?\s*"
            r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})"
            r"(?:[, ]+\s*(\d{1,2}:\d{2}))?"
            r"(?:\s*(UK time|GMT|UTC|BST))?",
            combined,
            re.IGNORECASE,
        )
        if not match:
            return ""
        date_text, time_text, zone_text = match.groups()
        parsed = None
        for date_format in ("%d %b %Y", "%d %B %Y"):
            try:
                parsed = datetime.strptime(date_text, date_format)
                break
            except ValueError:
                continue
        if parsed is None:
            return ""
        if time_text:
            hour, minute = (int(part) for part in time_text.split(":"))
            parsed = parsed.replace(hour=hour, minute=minute)
            zone = (zone_text or "").upper()
            if zone == "BST":
                parsed = parsed.replace(tzinfo=timezone(timedelta(hours=1)))
            elif zone in {"GMT", "UTC"}:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.replace(tzinfo=ZoneInfo("Europe/London"))
            return parsed.astimezone(timezone.utc).isoformat()
        return parsed.date().isoformat()

    for feed_label, url in feeds:
        elapsed = time.monotonic() - started
        if max_seconds and elapsed >= max_seconds:
            stats["stopped"] = "time limit"
            break
        timeout = min(20, max(1, int(max_seconds - elapsed))) if max_seconds else 20
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "TenderScout/1.0 (public procurement research; source attribution retained)",
                    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                payload = response.read()
            root = ET.fromstring(payload)
        except Exception as exc:
            errors.append(f"{feed_label}: {str(exc)[:180]}")
            print(f"[WARN] Standstill {feed_label} RSS fetch failed: {exc}", file=sys.stderr)
            continue

        items = [node for node in root.iter() if local_name(node.tag) in {"item", "entry"}]
        before = len(output)
        for item in items:
            if max_seconds and time.monotonic() - started >= max_seconds:
                stats["stopped"] = "time limit"
                break
            title = html.unescape(child_text(item, "title"))
            feed_link = child_text(item, "link")
            raw_description = child_text(item, "description", "summary", "content")
            description = html.unescape(re.sub(r"<[^>]+>", " ", raw_description))
            description = " ".join(description.split())
            if not title or not feed_link.startswith("https://"):
                continue
            official_match = re.search(
                r"https?://(?:www\.)?(?:find-tender\.service\.gov\.uk/Notice/[^\s\"'<>]+|contractsfinder\.service\.gov\.uk/Notice/[^\s\"'<>]+)",
                html.unescape(raw_description),
                re.IGNORECASE,
            )
            official_url = html.unescape(official_match.group(0)).rstrip(".,);") if official_match else feed_link
            published = child_text(item, "pubdate", "published", "updated")
            deadline = labelled_deadline(title, description)
            guid = child_text(item, "guid", "id")
            buyer_match = re.search(r"(?:Buyer|Organisation|Organization)\s*:\s*([^|;]+)", description, re.IGNORECASE)
            buyer = buyer_match.group(1).strip()[:180] if buyer_match else ""
            output.append({
                "source": "Standstill UK (RSS discovery; verify official notice)",
                "opportunity_stage": "Other notice",
                "location_text": "North West England (aggregator region only; delivery site not confirmed)",
                "published_date": published,
                "title": title,
                "buyer": buyer,
                "description": description or "Open the source page for the summary and official notice.",
                "trade_context": description,
                "contact_name": "", "contact_email": "", "contact_phone": "",
                "eligibility_text": "",
                "estimated_value_gbp": "",
                "contract_start_date": "", "contract_end_date": "",
                "deadline": deadline,
                "status": "Standstill UK discovery listing — verify official notice",
                "notice_url": official_url,
                "ocid": guid or official_url,
                "_raw": {"feed_url": url, "feed_label": feed_label, "item_title": title, "feed_link": feed_link},
            })
        feed_counts[feed_label] = len(output) - before

    raw_count = len(output)
    output = dedupe(output)
    stats.update({
        "pages": sum(1 for value in feed_counts.values() if value >= 0),
        "records": len(output),
        "raw_records": raw_count,
        "last_batch": len(output),
        "windows": len(feed_counts),
        "more": False,
        "full_end": any(value >= 50 for value in feed_counts.values()),
        "feed_counts": feed_counts,
        "partial_errors": "; ".join(errors)[:320] if feed_counts else "",
        "error": ("; ".join(errors)[:320] if errors and not feed_counts else ""),
    })
    return output


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


def explicit_out_of_area_delivery_sites(text, terms):
    """Find named out-of-area places only when nearby wording describes a site or delivery scope.

    General mentions of a place (for example, a buyer's address or a previous
    project) are not enough. This is a one-way exclusion safeguard: prose can
    never promote a record to LOCAL.
    """
    hits = []
    for term in place_hits(text, terms):
        pattern = r"(?<![a-z])" + re.escape(term) + r"(?![a-z])"
        # A place can appear first in the buyer/title and later in the actual
        # scope. Check every occurrence rather than letting the first mention
        # hide stronger delivery-site wording later in the notice.
        for match in re.finditer(pattern, text):
            before = text[max(0, match.start() - 120):match.start()]
            after = text[match.end():match.end() + 100]
            before_cue = re.search(
                r"\\b(?:at|in|within|across|throughout|to|delivery in|services in|works in|"
                r"properties in|site in|project in|located in|based at)\\s+"
                r"(?:(?:the\\s+)?(?:boundaries of|area of|city of|town of|borough of|county of)\\s+)*$",
                before,
            )
            after_cue = re.match(
                r"\\s*(?:enterprise zone|council housing properties|housing properties|"
                r"properties|premises|estate|campus|hospital|schools|facilities|site|"
                r"project|works|residential compounds)\\b",
                after,
            )
            if before_cue or after_cue:
                hits.append(term)
                break
    return hits


def postcode_districts(text, include_bare=False):
    """Return (full-postcode districts, bare target/review districts) found in text."""
    upper = text.upper()
    full = {m.group(1) for m in FULL_POSTCODE_RE.finditer(upper)}
    bare = set()
    if include_bare:
        known = LOCAL_POSTCODE_DISTRICTS | REVIEW_POSTCODE_DISTRICTS | EXCLUDED_POSTCODE_DISTRICTS
        bare = {m.group(1) for m in DISTRICT_RE.finditer(upper) if m.group(1) in known}
    return full, bare


def classify_geography(record, region):
    """Classify a tender as LOCAL, OUT_OF_AREA or REVIEW from delivery/site evidence.

    Postcode districts are checked first, then whole-word place names.
    Anything uncertain goes to REVIEW rather than the shortlist.
    """
    if normalise_region(region) == "UK wide":
        return "UK-wide search", "UK-wide search"

    raw_location_text = str(record.get("location_text") or "")
    location_text = raw_location_text.lower()
    prose = " ".join([
        record.get("title", ""),
        record.get("description", ""),
    ]).lower()

    full, bare = postcode_districts(location_text, include_bare=True)
    districts = full | bare
    pc_local = sorted(d for d in districts if d in LOCAL_POSTCODE_DISTRICTS)
    pc_review = sorted(d for d in districts if d in REVIEW_POSTCODE_DISTRICTS)
    pc_other = sorted(d for d in (full | bare) if d not in LOCAL_POSTCODE_DISTRICTS
                      and d not in REVIEW_POSTCODE_DISTRICTS)
    pc_other = sorted(set(pc_other) | (districts & EXCLUDED_POSTCODE_DISTRICTS))

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

    # Do not clutter the local review queue with contracts whose stated delivery
    # scope is explicitly worldwide or a long country list. This rule only applies
    # when no specific target-area site/postcode has been found; a real local site
    # mixed with wider coverage must remain in REVIEW for human assessment.
    global_scope_markers = (
        "rest of the world", "rest of world", "worldwide", "world wide",
        "global works", "global framework", "global services",
        "british overseas territories", "channel islands",
    )
    global_scope = any(marker in location_text for marker in global_scope_markers)
    # Some feeds enumerate dozens of country codes/names instead of saying
    # "worldwide". A long list of two-letter country codes is a strong signal.
    country_code_count = len(re.findall(r"(?<![A-Z])(?:AI|BM|FK|GI|KY|PN|MS|SH|TC|VG|AF|AL|DZ|AG|AR|AM|AU|AT|AZ|BH|BD|BB|BY|BE|BZ|BO|BA|BR|BN|BG|KH|CA|CL|CN|CO|CD|CR|HR|CU|CY|CZ|DK|DJ|DO|EC|EG|SV|ER|EE|SZ|ET|FJ|FI|FR|GE|DE|GH|GR|GD|GT|GN|GY|HT|HU|IS|IN|ID|IR|IQ|IE|IL|IT|JM|JP|JO|KZ|KE|KI|KW|KG|LA|LV|LB|LS|LR|LY|LT|MG|MW|MY|MX|MA|NP|NL|NZ|NG|NO|PK|PA|PE|PH|PL|PT|QA|RO|RU|SA|SN|RS|SG|SK|SI|ZA|KR|ES|LK|SE|CH|TW|TH|TR|UA|AE|GB|US|UY|UZ|VE|VN|YE|ZM|ZW)(?![A-Z])", raw_location_text.upper()))
    enumerated_global_scope = country_code_count >= 12
    # A multi-country or out-of-area structured field can coexist with a real
    # target-area delivery site listed in the notice description (for example,
    # a multi-school cleaning contract spanning Trafford, Knowsley and Wigan).
    # Description text must never promote a notice to LOCAL, but it must stop us
    # incorrectly discarding a mixed-area opportunity as OUT_OF_AREA.
    prose_local_sites = place_hits(prose, LOCAL_PLACE_TERMS)
    prose_full_postcodes, _ = postcode_districts(prose)
    # A postcode appearing only in title/description is deliberately not
    # authoritative. Preserve REVIEW for those records, including mixed sites.
    prose_out_sites = (
        [] if prose_full_postcodes
        else explicit_out_of_area_delivery_sites(prose, OUT_OF_AREA_TERMS)
    )
    if prose_out_sites and not prose_local_sites:
        return "OUT_OF_AREA", (
            "Explicit out-of-area delivery/site wording in notice: "
            + ", ".join(dict.fromkeys(prose_out_sites[:5]))
        )
    if strong_out and prose_local_sites:
        return "REVIEW", (
            "Structured delivery location includes non-target areas, while the "
            "title/description also names possible target-area sites: "
            + ", ".join(dict.fromkeys(prose_local_sites[:5]))
            + "; verify the official site list"
        )

    if not strong_local and (global_scope or enumerated_global_scope):
        return "OUT_OF_AREA", "Explicit worldwide / multi-country delivery scope; no specific target-area site identified"

    # A postcode district that is outside the configured service area is
    # stronger evidence than an ambiguous place name such as Moreton or Leigh.
    # Keep mixed explicit target/non-target sites in REVIEW.
    if pc_other and not strong_local:
        return "OUT_OF_AREA", (
            "Delivery/site postcode outside preferred area: " + ", ".join(pc_other[:3])
        )

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
    # A title or general description is not authoritative delivery-site evidence,
    # even if it contains a full local postcode. Structured delivery fields above
    # are the only route to a confirmed LOCAL classification.
    prose_hits = (
        sorted(prose_full)
        + place_hits(prose, LOCAL_PLACE_TERMS)
        + place_hits(prose, OUT_OF_AREA_TERMS)
    )
    if prose_hits:
        return "REVIEW", (
            "Location appears only in title/description; verify the official "
            "delivery/site field: " + ", ".join(dict.fromkeys(prose_hits[:5]))
        )
    if location_text.strip():
        return "REVIEW", "Structured location is too vague to confirm preferred area"

    return "REVIEW", "No usable delivery/site location evidence"


def score_record(record, services, region, min_value, max_value):
    fulltext = " ".join([
        record.get("title", ""), record.get("description", ""),
        record.get("trade_context", ""), record.get("buyer", ""),
    ]).lower()
    trade_hits = {
        "cleaning": keyword_hits(fulltext, CLEANING_TERMS),
        "fm": keyword_hits(fulltext, FM_TERMS),
        "maintenance": keyword_hits(fulltext, MAINTENANCE_TERMS),
        "construction": keyword_hits(fulltext, CONSTRUCTION_TERMS),
    }
    exclude_hits = keyword_hits(fulltext, EXCLUDE_TERMS)
    matched = [t for t in SERVICE_ORDER if t in services and trade_hits[t]]
    detailed_category = classify_trade_category(fulltext)
    # The default scope now covers the full detailed trade taxonomy. Keep
    # backwards compatibility for callers explicitly selecting a subset of the
    # legacy service groups: those switches must still exclude disabled groups.
    all_legacy_groups_enabled = all(group in services for group in SERVICE_ORDER)
    if not matched and (not all_legacy_groups_enabled or detailed_category == "Other / unclassified"):
        return None
    service = SERVICE_LABELS[matched[0]] if matched else detailed_category
    service_hits = [term for t in matched for term in trade_hits[t]]
    if not service_hits and detailed_category != "Other / unclassified":
        service_hits = [detailed_category]

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
        "trade_category": detailed_category,
        "score": score,
        "score_reasons": "; ".join(reasons),
        "status": deadline_status,
    })
    return record


def shortlist_eligible_stage(record):
    """Only open tenders and explicitly planned opportunities belong in the shortlist."""
    stage = record.get("opportunity_stage") or "Other notice"
    return stage in {"Open for bids", "Planning / upcoming"}


def dedupe(records):
    """Deduplicate procurement processes and cross-source copies of the same notice."""
    priority = {"Open for bids": 3, "Planning / upcoming": 2, "Other notice": 1}
    by_key = {}
    notice_key = {}
    for row in records:
        notice_url = str(row.get("notice_url") or "").strip()
        if notice_url and notice_url in notice_key:
            key = notice_key[notice_url]
        else:
            ocid = row.get("ocid")
            key = ("ocid", ocid) if ocid else (
                "title", row.get("title", "").lower(), row.get("buyer", "").lower(),
                row.get("published_date", ""),
            )
        if notice_url:
            notice_key[notice_url] = key
        current = by_key.get(key)
        if current is None or priority.get(row.get("opportunity_stage"), 0) > priority.get(current.get("opportunity_stage"), 0):
            by_key[key] = row
    return list(by_key.values())


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
        help="'Target area' = Liverpool, Sefton, St Helens, Halton, Wirral (including Wallasey), Knowsley and agreed wider North West districts; 'North West' is an alias")
    parser.add_argument("--services", nargs="+", choices=SERVICE_ORDER, default=SERVICE_ORDER)
    parser.add_argument("--min-value", type=float, default=None, help="Minimum estimated GBP value")
    parser.add_argument("--max-value", type=float, default=None, help="Maximum estimated GBP value")
    parser.add_argument("--min-score", type=int, default=3, help="Minimum relevance score (default 3)")
    parser.add_argument("--pages", type=int, default=10, help="Maximum pages to fetch from each source")
    parser.add_argument("--fts-only", action="store_true", help="Audit Find a Tender only; skip other feeds")
    parser.add_argument("--notice-id", help="Fetch one known Find a Tender notice directly by ID for audit/backfill")
    parser.add_argument("--out", default="tenderscout_results.csv", help="Filtered output CSV path")
    args = parser.parse_args()

    print("TenderScout v1 — fetching official public procurement feeds...")
    # Keep the scheduled/CLI collector aligned with the hosted dashboard's three free feeds.
    # Bearings records have no trusted delivery location and therefore remain in REVIEW
    # until a structured site location is verified; they are never auto-labelled LOCAL.
    direct_notice_rows = fetch_fts_notice(args.notice_id) if args.notice_id else []
    all_rows = fetch_fts(args.days, args.pages) if args.fts_only else (
        fetch_fts(args.days, args.pages)
        + fetch_contracts_finder(args.days, args.pages)
        + fetch_bearings()
        + fetch_standstill_northwest()
    )
    if direct_notice_rows:
        target_id = str(args.notice_id)
        target_title = str(direct_notice_rows[0].get("title", "")).strip().casefold()
        fts_found = any(
            row.get("source") == "Find a Tender"
            and (target_id in str(row.get("notice_url", "")) or
                 (target_title and str(row.get("title", "")).strip().casefold() == target_title))
            for row in all_rows
        )
        alternate_sources = sorted({
            str(row.get("source", "unknown"))
            for row in all_rows
            if row.get("source") != "Find a Tender"
            and (target_id in str(row.get("notice_url", "")) or
                 (target_title and str(row.get("title", "")).strip().casefold() == target_title))
        })
        print(
            f"Direct notice lookup {target_id}: confirmed; "
            f"Find a Tender rolling feed: {'FOUND' if fts_found else 'NOT FOUND'}; "
            f"alternate discovery sources: {', '.join(alternate_sources) if alternate_sources else 'NONE'}"
        )
        all_rows.extend(direct_notice_rows)
    all_rows = dedupe(all_rows)
    if not all_rows:
        print("No feed records retrieved. Check your internet connection/API availability and try again.")
        print("No CSV was written because no source records were received.")
        sys.exit(2)

    filtered = []
    review_rows = []
    rejected_by_scoring = 0
    excluded_by_stage = 0
    excluded_review_by_stage = 0
    scored_geography_counts = {}
    scored_trade_counts = {}
    for row in all_rows:
        scored = score_record(row, args.services, args.region, args.min_value, args.max_value)
        if not scored:
            rejected_by_scoring += 1
            continue
        geo = scored.get("region_match", "NOT SCORED")
        scored_geography_counts[geo] = scored_geography_counts.get(geo, 0) + 1
        trade = scored.get("trade_category") or scored.get("service_match") or "NO TRADE MATCH"
        scored_trade_counts[trade] = scored_trade_counts.get(trade, 0) + 1
        if normalise_region(args.region) == TARGET_AREA_LABEL and geo == "REVIEW":
            if shortlist_eligible_stage(scored):
                review_rows.append(scored)
            else:
                excluded_review_by_stage += 1
            continue
        if geo == "LOCAL" and scored["score"] >= args.min_score:
            if shortlist_eligible_stage(scored):
                filtered.append(scored)
            else:
                excluded_by_stage += 1
    filtered.sort(key=lambda r: (r.get("score", 0), r.get("published_date", "")), reverse=True)
    review_rows.sort(key=lambda r: (r.get("score", 0), r.get("published_date", "")), reverse=True)

    all_path = Path("tenderscout_all.csv")
    write_csv(all_path, all_rows)
    out_path = Path(args.out)
    write_csv(out_path, filtered)
    review_path = Path("tenderscout_review.csv")
    write_csv(review_path, review_rows)

    print(f"Fetched/deduplicated notices: {len(all_rows)}")
    stage_counts = {}
    for row in all_rows:
        stage = row.get("opportunity_stage", "Other notice")
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
    print("Opportunity stages: " + ", ".join(f"{stage}={count}" for stage, count in sorted(stage_counts.items())))
    geography_counts = {}
    trade_counts = {}
    for row in all_rows:
        # Score only matched trades for the main list, but independently audit
        # the geography and taxonomy of every raw notice. This separates a
        # thin local feed from trade-keyword gaps and avoids reporting every
        # unscored notice as "NOT SCORED".
        geo, _ = classify_geography(row, args.region)
        geography_counts[geo] = geography_counts.get(geo, 0) + 1
        fulltext = " ".join((row.get(field, "") or "") for field in ("title", "description", "buyer"))
        trade = classify_trade_category(fulltext)
        trade_counts[trade] = trade_counts.get(trade, 0) + 1
    print("Raw geography classification (all notices): " + ", ".join(f"{key}={count}" for key, count in sorted(geography_counts.items())))
    print("Raw trade taxonomy (all notices): " + ", ".join(f"{key}={count}" for key, count in sorted(trade_counts.items(), key=lambda item: (-item[1], item[0]))[:15]))
    local_trade_counts = {}
    review_trade_counts = {}
    for row in all_rows:
        geo, _ = classify_geography(row, args.region)
        fulltext = " ".join((row.get(field, "") or "") for field in ("title", "description", "buyer"))
        trade = classify_trade_category(fulltext)
        if geo == "LOCAL":
            local_trade_counts[trade] = local_trade_counts.get(trade, 0) + 1
        elif geo == "REVIEW":
            review_trade_counts[trade] = review_trade_counts.get(trade, 0) + 1
    print("Trade taxonomy in confirmed local notices: " + (", ".join(f"{key}={count}" for key, count in sorted(local_trade_counts.items(), key=lambda item: (-item[1], item[0]))) or "none"))
    print("Trade taxonomy in location-review notices: " + ", ".join(f"{key}={count}" for key, count in sorted(review_trade_counts.items(), key=lambda item: (-item[1], item[0]))[:15]))
    deadline_counts = {"future": 0, "expired": 0, "missing_or_unparsed": 0}
    for row in all_rows:
        deadline = parse_date(row.get("deadline"))
        if deadline is None:
            deadline_counts["missing_or_unparsed"] += 1
        elif deadline < datetime.now(timezone.utc):
            deadline_counts["expired"] += 1
        else:
            deadline_counts["future"] += 1
    print("Raw deadline health (all notices): " + ", ".join(f"{key}={count}" for key, count in deadline_counts.items()))
    print("Scored geography decisions: " + ", ".join(f"{key}={count}" for key, count in sorted(scored_geography_counts.items())))
    print("Scored trade categories: " + ", ".join(f"{key}={count}" for key, count in sorted(scored_trade_counts.items(), key=lambda item: (-item[1], item[0]))[:12]))
    print(f"Rejected by scoring/value/deadline/trade rules: {rejected_by_scoring}")
    print(f"Excluded from active shortlist by notice stage: {excluded_by_stage}")
    print(f"Excluded from location review by notice stage: {excluded_review_by_stage}")
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
