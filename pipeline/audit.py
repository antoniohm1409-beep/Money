"""Crawl a business website (home page + contact page) and record verifiable problems.

Every issue carries the evidence it was derived from, because the outreach agents are
only allowed to mention things that were actually measured.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, UnicodeDammit

from .prospect import domain_of

EMAIL_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._%+-]{0,63}@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,24}")
COPYRIGHT_RE = re.compile(r"©|copyright", re.I)
YEAR_RE = re.compile(r"(?<![0-9])(?:19|20)[0-9]{2}(?![0-9])")
JQUERY_RE = re.compile(
    r"(?:jquery[-.]v?(\d+)\.(\d+)(?:\.\d+)?(?:\.min)?\.js|jquery/(\d+)\.(\d+)(?:\.\d+)?/jquery)", re.I
)
WP_VERSION_RE = re.compile(r"wordpress\s+(\d+)\.(\d+)", re.I)
CONTACT_LINK_RE = re.compile(r"contact|kontakt|contacto|contatt|get-in-touch|about|impressum|sobre", re.I)

GENERIC_TITLES = {"home", "home page", "homepage", "welcome", "index", "untitled", "my site", "new page", "default"}
ASSET_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js", ".ico", ".woff", ".woff2")
JUNK_EMAIL_DOMAINS = (
    "sentry.io", "wixpress.com", "example.com", "example.org", "domain.com", "email.com", "yourdomain.com",
    "godaddy.com", "squarespace.com", "wordpress.org", "wordpress.com", "w3.org", "schema.org", "wix.com",
    "sentry-next.wixpress.com", "mysite.com", "website.com", "company.com", "google.com", "gstatic.com",
)
JUNK_LOCAL_PARTS = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "abuse", "postmaster", "mailer-daemon",
    "your", "you", "name", "email", "user", "username", "example", "test", "webmaster", "privacy",
}
FREEMAIL = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "live.com",
    "msn.com", "me.com", "ymail.com", "protonmail.com", "proton.me", "gmx.com", "att.net",
    "sbcglobal.net", "comcast.net", "verizon.net", "bellsouth.net",
}
LOCAL_BUSINESS_HINTS = (
    "localbusiness", "dentist", "plumber", "electrician", "restaurant", "attorney", "legalservice",
    "autorepair", "hairsalon", "veterinarycare", "roofingcontractor", "hvacbusiness", "accountingservice",
    "medicalbusiness", "homeandconstructionbusiness", "professionalservice", "organization",
)

WEIGHTS = {
    "no_viewport": 30,
    "no_https": 25,
    "legacy_markup": 20,
    "outdated_copyright": 12,
    "very_outdated_copyright": 18,
    "heavy_images": 15,
    "slow_response": 12,
    "outdated_cms": 12,
    "no_https_redirect": 8,
    "mixed_content": 8,
    "weak_title": 8,
    "no_meta_description": 8,
    "no_click_to_call": 4,
    "no_h1": 5,
    "old_jquery": 5,
    "images_missing_alt": 4,
    "no_structured_data": 4,
    "no_open_graph": 3,
}


# Problems a customer notices without knowing anything about websites. A lead needs at least one:
# an email about missing meta tags alone is not worth an owner's time.
VISIBLE = {
    "no_viewport", "no_https", "no_https_redirect", "legacy_markup", "outdated_copyright",
    "very_outdated_copyright", "slow_response", "heavy_images", "mixed_content", "outdated_cms",
}


def issue(issue_id: str, plain: str, evidence: str) -> dict:
    return {"id": issue_id, "weight": WEIGHTS[issue_id], "plain": plain, "evidence": evidence}


# ── email discovery ──────────────────────────────────────────────────────────

def decode_cfemail(encoded: str) -> str | None:
    """Decode Cloudflare's email obfuscation (data-cfemail attribute)."""
    try:
        key = int(encoded[:2], 16)
        return "".join(chr(int(encoded[i:i + 2], 16) ^ key) for i in range(2, len(encoded), 2))
    except ValueError:
        return None


def clean_email(raw: str) -> str | None:
    email = raw.strip().strip(".,;:<>()[]\"'").lower()
    if email.startswith("mailto:"):
        email = email[7:]
    email = email.split("?")[0]
    if not EMAIL_RE.fullmatch(email) or email.endswith(ASSET_SUFFIXES):
        return None
    local, _, domain = email.partition("@")
    if local in JUNK_LOCAL_PARTS or any(domain == d or domain.endswith("." + d) for d in JUNK_EMAIL_DOMAINS):
        return None
    if re.fullmatch(r"[0-9a-f]{20,}", local):  # tracking hashes
        return None
    return email


def extract_emails(html: str, soup: BeautifulSoup) -> list[str]:
    found: list[str] = []
    for link in soup.select('a[href^="mailto:" i]'):
        found.append(link.get("href", ""))
    for node in soup.select("[data-cfemail]"):
        decoded = decode_cfemail(node.get("data-cfemail", ""))
        if decoded:
            found.append(decoded)
    found.extend(EMAIL_RE.findall(html))
    cleaned: list[str] = []
    for raw in found:
        email = clean_email(raw)
        if email and email not in cleaned:
            cleaned.append(email)
    return cleaned


def rank_emails(emails: list[str], site_domain: str) -> list[str]:
    """Own-domain addresses first, then free-mail, then anything else (often the web agency)."""

    def rank(email: str) -> int:
        domain = email.rsplit("@", 1)[-1]
        if domain == site_domain or domain.endswith("." + site_domain) or site_domain.endswith("." + domain):
            return 0
        return 1 if domain in FREEMAIL else 2

    return sorted(dict.fromkeys(emails), key=rank)


# ── page analysis (pure, unit-tested) ────────────────────────────────────────

def visible_text(soup: BeautifulSoup, limit: int = 3000) -> str:
    clone = BeautifulSoup(str(soup), "html.parser")
    for tag in clone(["script", "style", "noscript", "svg", "template", "iframe"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", clone.get_text(" ")).strip()
    return text[:limit]


def analyze_html(final_url: str, html: str, elapsed: float | None = None, today: dt.date | None = None) -> dict:
    """Derive issues + reusable content from one fetched home page."""
    today = today or dt.date.today()
    soup = BeautifulSoup(html, "html.parser")
    lower = html.lower()
    issues: list[dict] = []
    is_https = final_url.lower().startswith("https://")

    # Mobile
    viewport = soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)})
    if not viewport:
        issues.append(issue(
            "no_viewport",
            "The site has no mobile viewport setting, so phones show the desktop layout shrunk down "
            "and visitors have to pinch and zoom.",
            'No <meta name="viewport"> tag in the home page HTML.',
        ))

    # Legacy markup
    legacy = []
    for tag_name in ("font", "center", "marquee", "frameset", "frame", "blink"):
        count = len(soup.find_all(tag_name))
        if count:
            legacy.append(f"{count}× <{tag_name}>")
    nested_tables = sum(1 for table in soup.find_all("table") if table.find("table"))
    if nested_tables >= 2:
        legacy.append(f"{nested_tables} nested layout tables")
    if ".swf" in lower:
        legacy.append("Flash (.swf) content")
    if legacy:
        issues.append(issue(
            "legacy_markup",
            "The page is built with techniques from the 2000s that modern browsers and phones handle poorly.",
            "Found " + ", ".join(legacy) + ".",
        ))

    # Copyright year
    # Visible text only: licence headers inside scripts ("(c) 2011 jQuery Foundation") are not the footer.
    page_text = visible_text(soup, limit=200_000)
    years = []
    for match in COPYRIGHT_RE.finditer(page_text):
        # every year in the short stretch after the marker, so "© 2009–2025 Name" reads as 2025
        tail = page_text[match.end():match.end() + 60]
        years.extend(int(y) for y in YEAR_RE.findall(tail) if int(y) <= today.year + 1)
    if years:
        newest = max(years)
        age = today.year - newest
        if age >= 5:
            issues.append(issue(
                "very_outdated_copyright",
                f"The copyright notice still says {newest}, which tells visitors the site has not been touched in years.",
                f"Newest copyright year on the page: {newest}.",
            ))
        elif age >= 2:
            issues.append(issue(
                "outdated_copyright",
                f"The copyright notice still says {newest}, which makes the business look less active than it is.",
                f"Newest copyright year on the page: {newest}.",
            ))

    # Speed
    if elapsed is not None and elapsed >= 3.0:
        issues.append(issue(
            "slow_response",
            f"The home page took {elapsed:.1f} seconds just to start loading in our test; "
            "most visitors leave after about 3.",
            f"Best of two HTML fetches: {elapsed:.2f}s.",
        ))

    # CMS / libraries
    generator_tag = soup.find("meta", attrs={"name": re.compile(r"^generator$", re.I)})
    generator = (generator_tag.get("content") or "").strip() if generator_tag else ""
    wp = WP_VERSION_RE.search(generator)
    if wp and int(wp.group(1)) < 6:
        issues.append(issue(
            "outdated_cms",
            f"The site runs {generator}, a version that is years old and no longer gets every security fix.",
            f'Generator meta tag: "{generator}".',
        ))
    jquery_versions = []
    for script in soup.find_all("script", src=True):
        match = JQUERY_RE.search(script["src"])
        if match:
            major, minor = (g for g in match.groups() if g is not None)
            jquery_versions.append((int(major), int(minor)))
    if jquery_versions and min(jquery_versions) < (2, 0):
        major, minor = min(jquery_versions)
        issues.append(issue(
            "old_jquery",
            f"It loads jQuery {major}.{minor}, a script library version from about a decade ago.",
            f"Script reference to jQuery {major}.{minor}.",
        ))

    # Mixed content
    if is_https:
        insecure = [
            node.get("src") or node.get("href")
            for node in soup.find_all(["script", "img", "link", "iframe"])
            if (node.get("src") or "").lower().startswith("http://")
            or (node.name == "link" and "stylesheet" in " ".join(node.get("rel") or []).lower()
                and (node.get("href") or "").lower().startswith("http://"))
        ]
        if insecure:
            issues.append(issue(
                "mixed_content",
                "Some files on the secure page are loaded insecurely, which browsers block or flag.",
                f"{len(insecure)} resource(s) loaded over http://, e.g. {insecure[0][:120]}",
            ))

    # SEO basics
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    if not title or title.lower() in GENERIC_TITLES:
        issues.append(issue(
            "weak_title",
            "The page title Google shows in search results is " + (f'just "{title}"' if title else "empty")
            + ", so it says nothing about what the business does or where.",
            f'<title> is "{title}".' if title else "No <title> text.",
        ))
    description_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    description = (description_tag.get("content") or "").strip() if description_tag else ""
    if not description:
        issues.append(issue(
            "no_meta_description",
            "There is no search description, so Google makes up its own snippet for the listing.",
            "No meta description on the home page.",
        ))
    h1s = [re.sub(r"\s+", " ", h.get_text(" ")).strip() for h in soup.find_all("h1")]
    h1s = [h for h in h1s if h]
    if not h1s:
        issues.append(issue(
            "no_h1",
            "The page has no main headline for search engines to read.",
            "No <h1> element on the home page.",
        ))

    # Mobile conversion
    tel_links = [a.get("href", "") for a in soup.select('a[href^="tel:" i]')]
    if not tel_links:
        issues.append(issue(
            "no_click_to_call",
            "There is no tap-to-call button, so mobile visitors have to hunt for the number in the text.",
            "No tel: link on the home page (phones may still auto-detect a number written as text).",
        ))

    images = soup.find_all("img")
    without_alt = [img for img in images if not (img.get("alt") or "").strip()]
    if len(images) >= 4 and len(without_alt) / len(images) > 0.5:
        issues.append(issue(
            "images_missing_alt",
            "Most images have no description, which hurts image search and accessibility.",
            f"{len(without_alt)} of {len(images)} images have no alt text.",
        ))

    structured = " ".join(s.get_text() for s in soup.find_all("script", type=re.compile("ld\\+json", re.I))).lower()
    itemtypes = " ".join(n.get("itemtype", "") for n in soup.find_all(attrs={"itemtype": True})).lower()
    if not any(hint in structured or hint in itemtypes for hint in LOCAL_BUSINESS_HINTS):
        issues.append(issue(
            "no_structured_data",
            "The site does not give Google its business details (hours, address, phone) in machine-readable form.",
            "No LocalBusiness-type schema.org markup found.",
        ))
    if not soup.find("meta", attrs={"property": re.compile(r"^og:image$", re.I)}):
        issues.append(issue(
            "no_open_graph",
            "Links shared on Facebook or WhatsApp show no preview image.",
            "No og:image meta tag.",
        ))

    # Reusable content for the writers / site builder
    logo = next(
        (
            urljoin(final_url, img.get("src"))
            for img in images
            if img.get("src") and "logo" in " ".join(
                [img.get("src", ""), img.get("alt", ""), " ".join(img.get("class") or []), img.get("id", "")]
            ).lower()
        ),
        "",
    )
    content = {
        "title": title,
        "meta_description": description,
        "h1": h1s[:3],
        "h2": [t for t in (re.sub(r"\s+", " ", h.get_text(" ")).strip() for h in soup.find_all("h2")) if t][:10],
        "text": visible_text(soup),
        "phones": list(dict.fromkeys(t[4:].strip() for t in tel_links))[:3],
        "logo": logo,
        "images": list(dict.fromkeys(
            urljoin(final_url, img.get("src")) for img in images
            if img.get("src") and not img.get("src", "").startswith("data:")
        ))[:12],
        "generator": generator,
        "lang": (soup.html.get("lang") or "") if soup.html else "",
    }
    contact_links = []
    for link in soup.find_all("a", href=True):
        label = f"{link.get('href')} {link.get_text(' ')}"
        if CONTACT_LINK_RE.search(label) and not link["href"].lower().startswith(("mailto:", "tel:", "#", "javascript:")):
            absolute = urljoin(final_url, link["href"]).split("#")[0]
            if domain_of(absolute) == domain_of(final_url) and absolute not in contact_links:
                contact_links.append(absolute)
    # "contact" pages before "about" pages
    contact_links.sort(key=lambda u: 0 if re.search(r"contact|kontakt|contatt", u, re.I) else 1)
    # Only images the browser certainly downloads on load: not lazy-loaded, and no srcset
    # (with srcset the browser may pick a smaller file than the one named in src).
    eager_images = list(dict.fromkeys(
        urljoin(final_url, img.get("src")) for img in images
        if img.get("src") and not img.get("src", "").startswith("data:")
        and (img.get("loading") or "").lower() != "lazy" and not img.get("srcset")
    ))
    return {
        "issues": issues,
        "content": content,
        "eager_images": eager_images,
        "emails": extract_emails(html, soup),
        "contact_links": contact_links[:2],
    }


def score_issues(issues: list[dict]) -> int:
    return min(100, sum(i["weight"] for i in issues))


# ── network ──────────────────────────────────────────────────────────────────

BLOCKED_STATUSES = {401, 403, 406, 429, 503}


def decode_body(resp: requests.Response) -> str:
    """Decode HTML honouring the header charset, then <meta charset>, then detection.

    requests alone assumes ISO-8859-1 when the header has no charset, which garbles UTF-8 pages.
    """
    declared = requests.utils.get_encoding_from_headers(resp.headers)
    hints = [declared] if declared and "charset" in resp.headers.get("content-type", "").lower() else []
    return UnicodeDammit(resp.content, hints, is_html=True).unicode_markup or resp.text


def _fetch(session: requests.Session, url: str, timeout: int) -> tuple[requests.Response, float]:
    started = time.monotonic()
    resp = session.get(url, timeout=timeout, allow_redirects=True)
    return resp, time.monotonic() - started


def _robots_allows(session: requests.Session, base_url: str, user_agent: str, timeout: int) -> bool:
    try:
        resp = session.get(urljoin(base_url, "/robots.txt"), timeout=timeout)
    except requests.RequestException:
        return True
    if resp.status_code != 200 or "<html" in resp.text[:500].lower():
        return True
    parser = robotparser.RobotFileParser()
    parser.parse(resp.text.splitlines())
    return parser.can_fetch(user_agent, base_url)


def measure_images(session: requests.Session, urls: list[str], timeout: int = 8) -> dict | None:
    """Total download size of the home page's images, from Content-Length headers."""
    sizes: list[tuple[int, str]] = []
    for url in urls[:15]:
        try:
            resp = session.head(url, timeout=timeout, allow_redirects=True)
            size = int(resp.headers.get("content-length") or 0)
        except (requests.RequestException, ValueError):
            continue
        if resp.status_code == 200 and size:
            sizes.append((size, url))
    if not sizes:
        return None
    largest = max(sizes)
    return {"count": len(sizes), "total_mb": sum(s for s, _ in sizes) / 1e6, "largest_mb": largest[0] / 1e6,
            "largest_url": largest[1]}


def heavy_images_issue(measured: dict | None) -> dict | None:
    if not measured or (measured["total_mb"] < 2.5 and measured["largest_mb"] < 1.0):
        return None
    return issue(
        "heavy_images",
        f"The home page loads about {measured['total_mb']:.1f} MB of images (the largest alone is "
        f"{measured['largest_mb']:.1f} MB), which is a long wait on a phone connection.",
        f"Content-Length of {measured['count']} home page images sums to {measured['total_mb']:.2f} MB; "
        f"largest: {measured['largest_url'][:140]} ({measured['largest_mb']:.2f} MB).",
    )


def audit_site(business: dict, user_agent: str, timeout: int = 15) -> dict:
    """Audit one business website. Returns the business dict extended with audit results.

    `status` is "ok" when the home page could be analysed; anything else means we could not
    measure the site and must not make claims about it.
    """
    result = dict(business)
    result.update({"status": "ok", "issues": [], "score": 0, "emails": list(business.get("osm_emails", []))})
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"})
    url = business["website"]

    try:
        if not _robots_allows(session, url, user_agent, timeout):
            result["status"] = "robots_disallow"
            return result
        resp, elapsed = _fetch(session, url, timeout)
    except requests.exceptions.SSLError:
        # Broken certificate: retry over plain http so the rest of the audit can run.
        try:
            resp, elapsed = _fetch(session, "http://" + domain_of(url), timeout)
        except requests.RequestException as exc:
            result["status"] = f"unreachable: {type(exc).__name__}"
            return result
    except requests.RequestException as exc:
        result["status"] = f"unreachable: {type(exc).__name__}"
        return result

    if resp.status_code in BLOCKED_STATUSES:
        result["status"] = f"blocked: HTTP {resp.status_code}"
        return result
    if resp.status_code != 200 or "html" not in resp.headers.get("content-type", "html").lower():
        result["status"] = f"bad_response: HTTP {resp.status_code}"
        return result
    html = decode_body(resp)
    if len(html) < 400:
        result["status"] = "empty_page"
        return result

    # Second sample so one slow hiccup is not reported as a slow site.
    try:
        _, elapsed_again = _fetch(session, resp.url, timeout)
        elapsed = min(elapsed, elapsed_again)
    except requests.RequestException:
        pass

    analysis = analyze_html(resp.url, html, elapsed)
    issues = analysis["issues"]

    # HTTPS
    if not resp.url.lower().startswith("https://"):
        https_ok = False
        try:
            https_resp = session.get("https://" + urlparse(resp.url).netloc, timeout=timeout)
            https_ok = https_resp.status_code == 200
        except requests.RequestException:
            https_ok = False
        if https_ok:
            issues.append(issue(
                "no_https_redirect",
                "Visitors who type the address land on the insecure version of the site, "
                'which browsers label "Not secure".',
                f"{resp.url} does not redirect to https although https works.",
            ))
        else:
            issues.append(issue(
                "no_https",
                'The site has no working secure connection, so Chrome and Safari label it "Not secure" '
                "next to the address.",
                f"Final URL {resp.url}; https:// version failed to load.",
            ))

    heavy = heavy_images_issue(measure_images(session, analysis["eager_images"]))
    if heavy:
        issues.append(heavy)

    emails = result["emails"] + analysis["emails"]
    for contact_url in analysis["contact_links"]:
        try:
            contact_resp = session.get(contact_url, timeout=timeout)
        except requests.RequestException:
            continue
        if contact_resp.status_code == 200:
            contact_html = decode_body(contact_resp)
            emails += extract_emails(contact_html, BeautifulSoup(contact_html, "html.parser"))

    issues.sort(key=lambda i: -i["weight"])
    result.update({
        "final_url": resp.url,
        "response_seconds": round(elapsed, 2),
        "html_kb": round(len(resp.content) / 1024),
        "issues": issues,
        "score": score_issues(issues),
        "emails": rank_emails(emails, business["domain"]),
        "content": analysis["content"],
        "audited_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    })
    return result


def to_json(lead: dict) -> str:
    return json.dumps(lead, indent=2, ensure_ascii=False) + "\n"
