"""Planning-application leads for Merseyside trades.

Every extension, loft conversion, new home, shop refit and protected-tree job needs a
planning application first, and councils publish them. UK PlanIt (planit.org.uk) collects
them into one free API. This module reads recent applications for the six target councils,
drops paperwork-only applications (discharge of conditions, minor amendments, adverts),
and labels each one with the trades likely to be needed.

These are leads, not confirmed jobs: an application shows work that is planned, not that
the owner has asked for quotes. Addresses are public planning records; trades should contact
homeowners only by letter or leaflet, never by cold call or unsolicited email.

PlanIt acceptable use (planit.org.uk/faq): run between 18:00 and 06:00, leave at least
60 seconds between requests and honour Retry-After, stay under 300 requests a day, and put
a contact email in the User-Agent. This module follows all four; set PLANIT_CONTACT_EMAIL.
"""

import json
import os
import re
import time
from datetime import date, datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

PLANIT_API = "https://www.planit.org.uk/api/applics/json"
REQUEST_GAP_SECONDS = 60
MAX_RETRY_AFTER_SECONDS = 300
LONDON = ZoneInfo("Europe/London")


class PlanningConfigError(RuntimeError):
    """Raised when the app is not set up to call PlanIt politely."""


def user_agent():
    contact = (os.environ.get("PLANIT_CONTACT_EMAIL") or "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", contact):
        raise PlanningConfigError(
            "Set PLANIT_CONTACT_EMAIL in Render (Environment) to your email. PlanIt requires a contact address on every request."
        )
    return f"TenderScout/1.0 (Merseyside trade leads; contact {contact})"


def in_quiet_hours(now=None):
    """PlanIt asks automated users to run between 18:00 and 06:00 UK time."""
    local = (now or datetime.now(timezone.utc)).astimezone(LONDON)
    return local.hour >= 18 or local.hour < 6

# PlanIt area names for the six target councils.
AREAS = ["Liverpool", "Sefton", "Knowsley", "St Helens", "Halton", "Wirral"]

# If an area's newest application is older than this, PlanIt has probably stopped collecting it.
STALE_AFTER_DAYS = 21

FIELDS = [
    "name", "uid", "area_name", "start_date", "decided_date", "app_state", "app_size",
    "app_type", "address", "postcode", "description", "link", "url",
]

# Application types that are paperwork, not new work.
SKIP_TYPES = {"Conditions", "Amendment", "Advertising", "Telecoms"}
SKIP_STATES = {"Rejected", "Refused", "Withdrawn"}
SKIP_PHRASES = [
    "discharge of condition", "discharge condition", "non-material amendment", "non material amendment",
    "variation of condition", "removal of condition", "neighbouring authority consultation",
    "certificate of lawfulness for the existing", "certificate of lawfulness for existing",
    "consent to display", "prior notification of agricultural", "telecommunications",
]

# Trade labels, in the order they are shown. Keywords are matched on whole words in the description.
LEAD_TRADES = {
    "Extensions and building work": [
        "extension", "extensions", "dormer", "loft conversion", "garage conversion", "outbuilding",
        "porch", "conservatory", "two storey", "single storey", "first floor", "orangery",
        "annex", "annexe", "games room", "garden room", "alterations",
    ],
    "New homes and conversions": [
        "dwelling", "dwellings", "bungalow", "apartments", "flats", "house in multiple occupation",
        "hmo", "residential units", "self contained", "self-contained",
    ],
    "Roofing": ["roof", "roofs", "dormer", "dormers", "roof lantern", "flat roof", "pitched roof"],
    "Trees and landscaping": [
        "tree", "trees", "tpo", "tree preservation order", "pollard", "pollarding", "fell", "felling",
        "crown reduction", "hedge", "hedgerow", "landscaping", "community garden", "garden",
    ],
    "Shopfronts and commercial fit-out": [
        "shopfront", "shop front", "change of use", "fit out", "fit-out", "internal alterations",
        "cafe", "café", "restaurant", "takeaway", "office", "offices", "retail", "unit", "units",
    ],
    "Windows and doors": ["window", "windows", "door", "doors", "bi-fold", "bifold", "upvc", "glazing", "glazed"],
    "Fencing, driveways and groundworks": [
        "fence", "fencing", "boundary", "driveway", "dropped kerb", "parking", "car park",
        "hardstanding", "patio", "decking", "decked", "retaining wall", "gates",
    ],
    "Demolition and clearance": ["demolition", "demolish"],
    "Solar, EV and electrical": ["solar", "photovoltaic", "pv panels", "ev charging", "lighting", "heat pump"],
}
# Builders' cleans: new homes, conversions and commercial fit-outs need a final clean before handover.
CLEANING_TRADE = "Builders' cleans (new builds and fit-outs)"
TRADE_ORDER = list(LEAD_TRADES) + [CLEANING_TRADE]


def _words(text):
    return " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip() + " "


def _has(text_words, phrase):
    return (" " + re.sub(r"[^a-z0-9]+", " ", phrase.lower()).strip() + " ") in text_words


def classify(description, app_size=None):
    """Return the trades a planning application is likely to need, most specific first."""
    words = _words(description)
    trades = [trade for trade, terms in LEAD_TRADES.items() if any(_has(words, t) for t in terms)]
    # "unit"/"units" and "alterations" are weak on their own; drop the label when that is all we matched.
    if trades == ["Shopfronts and commercial fit-out"] and not any(
        _has(words, t) for t in ["shopfront", "shop front", "change of use", "fit out", "internal alterations", "cafe", "restaurant", "takeaway", "office", "offices", "retail"]
    ):
        trades = []
    if "New homes and conversions" in trades or "Shopfronts and commercial fit-out" in trades or (app_size or "") in ("Medium", "Large"):
        if trades:
            trades.append(CLEANING_TRADE)
    return trades


def is_useful(record):
    """True when the application describes real building, tree or fit-out work."""
    if (record.get("app_type") or "") in SKIP_TYPES:
        return False
    if (record.get("app_state") or "") in SKIP_STATES:
        return False
    words = _words(record.get("description"))
    return not any(_has(words, p) for p in SKIP_PHRASES)


def status_label(app_state):
    return {
        "Permitted": "Approved: work likely soon",
        "Conditions": "Approved with conditions: work likely soon",
        "Undecided": "Awaiting decision: plan ahead",
        "Appealed": "Under appeal",
        "Referred": "Referred",
    }.get(app_state or "", app_state or "Status not stated")


def is_household(record):
    uid = (record.get("uid") or record.get("name") or "").upper()
    words = _words(record.get("description"))
    return "HH" in uid or any(_has(words, t) for t in ["single storey", "two storey", "dormer", "porch", "conservatory", "garage conversion", "loft conversion", "rear extension", "side extension"])


def normalise(record, fetched_at=None):
    """Turn a PlanIt record into a planning lead, or None if it isn't useful."""
    if not record.get("name") or not record.get("description") or not is_useful(record):
        return None
    trades = classify(record.get("description"), record.get("app_size"))
    if not trades:
        return None
    council_url = record.get("url") or ""
    planit_url = record.get("link") or ""
    return {
        "name": str(record["name"])[:200],
        "area": str(record.get("area_name") or "")[:60],
        "address": str(record.get("address") or "")[:300],
        "postcode": str(record.get("postcode") or "")[:12],
        "description": " ".join(str(record.get("description")).split())[:2000],
        "app_type": str(record.get("app_type") or "")[:40],
        "app_state": str(record.get("app_state") or "")[:40],
        "app_size": str(record.get("app_size") or "")[:20],
        "start_date": str(record.get("start_date") or "")[:10],
        "decided_date": str(record.get("decided_date") or "")[:10],
        "trades": ", ".join(trades),
        "household": 1 if is_household(record) else 0,
        "council_url": council_url if council_url.startswith(("https://", "http://")) else "",
        "planit_url": planit_url if planit_url.startswith("https://") else "",
        "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(),
    }


def _get(params, timeout, sleep=time.sleep):
    url = PLANIT_API + "?" + urlencode(params)
    req = Request(url, headers={"User-Agent": user_agent(), "Accept": "application/json"})
    for attempt in range(2):
        try:
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read(3_000_000).decode("utf-8"))
        except HTTPError as exc:
            if exc.code != 429 or attempt:
                raise
            try:
                wait = int(exc.headers.get("Retry-After") or REQUEST_GAP_SECONDS)
            except (TypeError, ValueError):
                wait = REQUEST_GAP_SECONDS
            sleep(min(max(wait, REQUEST_GAP_SECONDS), MAX_RETRY_AFTER_SECONDS))


def fetch_area(area, days=30, page_size=200, max_pages=3, timeout=30, getter=None, pace=None):
    """Read recent applications for one council. Returns (records, info).

    `pace` is called before every request so callers can keep the 60-second gap PlanIt asks for.
    """
    getter = getter or _get
    params = {"auth": area, "recent": days, "pg_sz": page_size, "compress": "on", "select": ",".join(FIELDS)}
    records, info = [], {"area": area, "total": 0, "newest": "", "error": ""}
    try:
        for page in range(1, max_pages + 1):
            if pace:
                pace()
            data = getter(dict(params, page=page), timeout)
            if data.get("error"):
                raise RuntimeError(str(data["error"])[:200])
            batch = data.get("records") or []
            records.extend(batch)
            info["total"] = data.get("total") or len(records)
            to = data.get("to")
            if not batch or to is None or to + 1 >= (data.get("total") or 0):
                break
    except Exception as exc:  # keep the other councils going
        info["error"] = str(exc)[:200]
    info["newest"] = max((r.get("start_date") or "" for r in records), default="")
    return records, info


def area_is_stale(info, today=None):
    """PlanIt stops updating some councils when their website changes; flag that instead of showing 'no jobs'."""
    today = today or date.today()
    if info.get("error"):
        return False
    if not info.get("newest"):
        return True
    try:
        newest = date.fromisoformat(info["newest"][:10])
    except ValueError:
        return True
    return (today - newest).days > STALE_AFTER_DAYS


def fetch_all(days=30, areas=None, getter=None, sleep=time.sleep):
    """Fetch and normalise leads for every target council. Returns (leads, infos).

    Waits REQUEST_GAP_SECONDS between requests (not before the first).
    """
    if getter is None:
        user_agent()  # fail fast, before any request, if the contact email is missing
    fetched_at = datetime.now(timezone.utc).isoformat()
    leads, infos, seen = [], [], set()
    made = {"n": 0}

    def pace():
        if made["n"]:
            sleep(REQUEST_GAP_SECONDS)
        made["n"] += 1

    for area in areas or AREAS:
        records, info = fetch_area(area, days=days, getter=getter, pace=pace)
        kept = 0
        for record in records:
            lead = normalise(record, fetched_at)
            if lead and lead["name"] not in seen:
                seen.add(lead["name"])
                leads.append(lead)
                kept += 1
        info["kept"] = kept
        infos.append(info)
    return leads, infos
