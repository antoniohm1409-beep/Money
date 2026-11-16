"""CLI: find businesses, audit their sites, write qualified leads to data/leads/.

    python -m pipeline.run                 # normal run (used by GitHub Actions)
    python -m pipeline.run --url https://example.com   # audit one site and print the result
    python -m pipeline.run --summary       # table of the leads collected so far
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from . import audit, config, prospect


def load_seen() -> dict:
    if config.SEEN_PATH.exists():
        return json.loads(config.SEEN_PATH.read_text(encoding="utf-8"))
    return {}


def save_seen(seen: dict) -> None:
    config.SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.SEEN_PATH.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def qualifies(lead: dict, cfg: dict, suppression: set[str]) -> tuple[bool, str]:
    if lead["status"] != "ok":
        return False, lead["status"]
    if lead["score"] < cfg["prospecting"]["min_score"]:
        return False, "site_good_enough"
    emails = [e for e in lead["emails"] if not config.is_suppressed(e, suppression)]
    if not cfg["outreach"].get("allow_freemail_recipients", True):
        emails = [e for e in emails if e.rsplit("@", 1)[-1] not in audit.FREEMAIL]
    if not emails:
        return False, "no_email"
    lead["emails"] = emails
    return True, "lead"


def run(cfg: dict, max_audits: int | None = None, only_city: str | None = None, dry_run: bool = False) -> int:
    """Audit new businesses. With dry_run nothing is written and only counts are printed
    (used while the repository is public, so no business is named in public logs or commits)."""
    pros = cfg["prospecting"]
    user_agent = pros["user_agent"]
    budget = max_audits or pros["max_audits_per_run"]
    seen = load_seen()
    suppression = config.load_suppression()
    config.LEADS_DIR.mkdir(parents=True, exist_ok=True)
    new_leads = 0
    outcomes: Counter = Counter()
    issue_counts: Counter = Counter()
    all_issue_counts: Counter = Counter()
    score_buckets: Counter = Counter()
    readable = with_email = 0

    for city in ([only_city] if only_city else cfg["market"]["cities"]):
        if budget <= 0:
            break
        try:
            businesses = prospect.find_businesses(city, cfg["market"]["categories"], user_agent)
        except Exception as exc:  # one city failing must not kill the run
            print(f"[{city}] prospecting failed: {exc}", file=sys.stderr)
            continue
        fresh = [
            b for b in businesses
            if b["domain"] not in seen and not config.is_suppressed(b["domain"], suppression)
        ]
        print(f"[{city}] {len(businesses)} businesses with a website, {len(fresh)} not audited yet")
        batch = fresh[:budget]
        budget -= len(batch)
        if not batch:
            continue

        with ThreadPoolExecutor(max_workers=8) as pool:
            audited = list(pool.map(
                lambda b: audit.audit_site(b, user_agent, pros["request_timeout"]), batch
            ))

        for lead in audited:
            if lead["status"] == "ok":
                readable += 1
                with_email += bool(lead["emails"])
                score_buckets[f"{lead['score'] // 10 * 10:02d}+"] += 1
                all_issue_counts.update(i["id"] for i in lead["issues"])
            ok, reason = qualifies(lead, cfg, suppression)
            outcomes[reason.split(":")[0]] += 1
            seen[lead["domain"]] = {"date": dt.date.today().isoformat(), "result": reason, "score": lead["score"]}
            if ok:
                new_leads += 1
                issue_counts.update(i["id"] for i in lead["issues"])
                if not dry_run:
                    lead.pop("osm_emails", None)
                    (config.LEADS_DIR / f"{lead['id']}.json").write_text(audit.to_json(lead), encoding="utf-8")
                    print(f"  LEAD {lead['score']:>3}  {lead['name']}")

    if not dry_run:
        save_seen(seen)
    print(f"\nNew leads: {new_leads}. Outcomes: {dict(outcomes)}")
    if issue_counts:
        print(f"Issues across new leads: {dict(issue_counts.most_common())}")
    if readable:
        print(f"Readable sites: {readable}, of which {with_email} publish an email.")
        print(f"Score distribution: {dict(sorted(score_buckets.items()))}")
        print(f"Issues across all readable sites: {dict(all_issue_counts.most_common())}")
    if dry_run:
        print("DRY RUN: nothing was saved.")
    if new_leads == 0 and not outcomes:
        print("Nothing new to audit — every business in the configured cities has been seen. "
              "Add more cities or categories in config.yaml.")
    return new_leads


def summary() -> None:
    leads = sorted(
        (json.loads(p.read_text(encoding="utf-8")) for p in config.LEADS_DIR.glob("*.json")),
        key=lambda l: -l["score"],
    )
    for lead in leads:
        top = ", ".join(i["id"] for i in lead["issues"][:3])
        print(f"{lead['score']:>3}  {lead['id']:<34} {lead['emails'][0]:<38} {top}")
    print(f"\n{len(leads)} leads")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", help="audit a single website and print the result as JSON")
    parser.add_argument("--city", help="prospect only this city")
    parser.add_argument("--max", type=int, help="override max_audits_per_run")
    parser.add_argument("--summary", action="store_true", help="list collected leads")
    parser.add_argument("--dry-run", action="store_true", help="audit but save nothing and print only counts")
    args = parser.parse_args()
    cfg = config.load_config()

    if args.summary:
        summary()
    elif args.url:
        site = prospect.normalize_website(args.url) or args.url
        business = {
            "id": prospect.domain_of(site).replace(".", "-"), "name": prospect.domain_of(site),
            "website": site, "domain": prospect.domain_of(site), "osm_emails": [],
        }
        lead = audit.audit_site(business, cfg["prospecting"]["user_agent"], cfg["prospecting"]["request_timeout"])
        print(audit.to_json(lead))
    else:
        run(cfg, args.max, args.city, args.dry_run)


if __name__ == "__main__":
    main()
