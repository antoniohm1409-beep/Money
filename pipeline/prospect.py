"""Find local businesses that publish a website, using OpenStreetMap (free, no API key)."""
from __future__ import annotations

import time
from urllib.parse import urlparse

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

# Sites that are a profile on someone else's platform, not the business's own website.
NOT_OWN_SITE = (
    "facebook.com", "instagram.com", "yelp.com", "linktr.ee", "twitter.com", "x.com",
    "google.com", "goo.gl", "tripadvisor.", "yellowpages.", "linkedin.com", "tiktok.com",
    "youtube.com", "opentable.", "doordash.com", "ubereats.com", "grubhub.com",
)


def geocode_city(city: str, user_agent: str, timeout: int = 20) -> tuple[float, float, float, float]:
    """Return (south, west, north, east) for a city name."""
    resp = requests.get(
        NOMINATIM_URL,
        params={"q": city, "format": "jsonv2", "limit": 1},
        headers={"User-Agent": user_agent},
        timeout=timeout,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise ValueError(f"City not found: {city!r}")
    south, north, west, east = (float(v) for v in results[0]["boundingbox"])
    return south, west, north, east


def build_overpass_query(categories: list[str], bbox: tuple[float, float, float, float]) -> str:
    south, west, north, east = bbox
    box = f"({south},{west},{north},{east})"
    clauses = []
    for category in categories:
        key, _, value = category.partition("=")
        for site_key in ("website", "contact:website"):
            clauses.append(f'  nwr["{key.strip()}"="{value.strip()}"]["{site_key}"]{box};')
    return "[out:json][timeout:90];\n(\n" + "\n".join(clauses) + "\n);\nout tags center;"


def run_overpass(query: str, user_agent: str, timeout: int = 120) -> list[dict]:
    last_error: Exception | None = None
    for attempt, url in enumerate(OVERPASS_URLS * 2):
        try:
            resp = requests.post(url, data={"data": query}, headers={"User-Agent": user_agent}, timeout=timeout)
            if resp.status_code == 200:
                return resp.json().get("elements", [])
            last_error = RuntimeError(f"{url} -> HTTP {resp.status_code}")
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
        time.sleep(3 + attempt * 3)
    raise RuntimeError(f"All Overpass mirrors failed: {last_error}")


def normalize_website(raw: str) -> str | None:
    raw = (raw or "").strip().split(";")[0].strip()
    if not raw:
        return None
    if not raw.lower().startswith(("http://", "https://")):
        raw = "http://" + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if "." not in host or any(marker in host for marker in NOT_OWN_SITE):
        return None
    return raw


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def element_to_business(element: dict, categories: list[str], city: str) -> dict | None:
    tags = element.get("tags") or {}
    name = (tags.get("name") or "").strip()
    website = normalize_website(tags.get("website") or tags.get("contact:website") or "")
    if not name or not website:
        return None
    category = next(
        (c for c in categories if tags.get(c.partition("=")[0]) == c.partition("=")[2]), categories[0]
    )
    street = " ".join(p for p in (tags.get("addr:housenumber"), tags.get("addr:street")) if p)
    address = ", ".join(
        p for p in (street, tags.get("addr:city"), tags.get("addr:state"), tags.get("addr:postcode")) if p
    )
    emails = [
        e.strip().lower()
        for e in (tags.get("email") or tags.get("contact:email") or "").split(";")
        if "@" in e
    ]
    return {
        "id": domain_of(website).replace(".", "-"),
        "name": name,
        "category": category.partition("=")[2].replace("_", " "),
        "city": city,
        "website": website,
        "domain": domain_of(website),
        "address": address,
        "phone": tags.get("phone") or tags.get("contact:phone") or "",
        "opening_hours": tags.get("opening_hours") or "",
        "osm_emails": emails,
        "osm": f"{element.get('type')}/{element.get('id')}",
    }


def find_businesses(city: str, categories: list[str], user_agent: str) -> list[dict]:
    bbox = geocode_city(city, user_agent)
    elements = run_overpass(build_overpass_query(categories, bbox), user_agent)
    return dedupe_independents(elements, categories, city)


def dedupe_independents(elements: list[dict], categories: list[str], city: str, max_locations: int = 2) -> list[dict]:
    """One entry per domain, dropping chains and franchises (head office decides, not the branch)."""
    businesses: dict[str, dict] = {}
    locations: dict[str, int] = {}
    branded: set[str] = set()
    for element in elements:
        business = element_to_business(element, categories, city)
        if not business:
            continue
        domain = business["domain"]
        locations[domain] = locations.get(domain, 0) + 1
        tags = element.get("tags") or {}
        if tags.get("brand") or tags.get("brand:wikidata"):
            branded.add(domain)
        businesses.setdefault(domain, business)
    return [
        b for domain, b in businesses.items()
        if domain not in branded and locations[domain] <= max_locations
    ]
