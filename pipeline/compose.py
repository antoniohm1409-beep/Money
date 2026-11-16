"""Guardrails between what an agent writes and what reaches a real inbox.

    python -m pipeline.compose preflight
    python -m pipeline.compose pending --handled handled.txt --limit 15
    python -m pipeline.compose render outbox/<lead_id>.json [--followup]

`render` validates an agent-written draft against the lead's audit and the config, appends the
legally required footer, and prints the final email as JSON. A non-zero exit means: do not send.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import audit, config

OPT_OUT = {
    "en": 'Not relevant? Just reply "no thanks" and I won\'t email you again.',
    "es": 'Si no le interesa, responda "no gracias" y no volveré a escribirle.',
    "fr": 'Pas intéressé ? Répondez « non merci » et je ne vous écrirai plus.',
    "de": 'Kein Interesse? Antworten Sie einfach mit „nein danke“ und ich schreibe Ihnen nicht mehr.',
    "it": 'Non le interessa? Risponda "no grazie" e non le scriverò più.',
    "pt": 'Não tem interesse? Responda "não, obrigado" e não volto a escrever.',
}
# Phrases that are deceptive, unverifiable or spam-filter bait. Matched case-insensitively.
BANNED = (
    "guarantee", "100%", "act now", "limited time", "risk-free", "risk free", "double your", "triple your",
    "#1", "number one on google", "first page of google", "top of google", "dear sir", "to whom it may concern",
    "i am a customer", "as a customer", "hope this email finds you", "garantiz", "garantía",
)
PLACEHOLDER_RE = re.compile(r"\{\{|\}\}|\[[A-Z][A-Za-z _]*\]|<[A-Z_ ]+>|\bTODO\b|\bXXX\b|lorem ipsum", re.I)
URL_RE = re.compile(r"(?:https?://|www\.)[^\s)>\]]+", re.I)
WORD_LIMITS = {"first": (45, 140), "followup": (20, 80)}


def footer(cfg: dict) -> str:
    sender = cfg["sender"]
    lines = ["--", f"{sender['name']} · {sender['business_name']}", sender["postal_address"]]
    contact = " · ".join(v for v in (sender.get("phone"), sender.get("portfolio_url")) if v)
    if contact:
        lines.append(contact)
    lines.append(OPT_OUT.get(cfg["market"].get("language", "en"), OPT_OUT["en"]))
    return "\n".join(lines)


def load_lead(lead_id: str) -> dict:
    path = config.LEADS_DIR / f"{lead_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"No lead file for {lead_id!r}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate(draft: dict, lead: dict, cfg: dict, suppression: set[str], kind: str = "first") -> list[str]:
    """Return the list of reasons this draft must not be sent (empty = OK)."""
    errors: list[str] = []
    missing = config.missing_sender_fields(cfg)
    if missing:
        errors.append(f"config.yaml sender fields not filled in: {', '.join(missing)}")

    to = (draft.get("to") or "").strip().lower()
    if to not in lead.get("emails", []):
        errors.append(f"recipient {to!r} is not one of the addresses found for this lead")
    if to and config.is_suppressed(to, suppression):
        errors.append(f"recipient {to!r} is on the suppression list")
    if to and not cfg["outreach"].get("allow_freemail_recipients", True) and to.rsplit("@", 1)[-1] in audit.FREEMAIL:
        errors.append("free-mail recipients are disabled in config")

    subject = (draft.get("subject") or "").strip()
    body = (draft.get("body") or "").strip()
    if kind == "first":
        if not 3 <= len(subject) <= 60:
            errors.append("subject must be 3-60 characters")
        if re.match(r"\s*(re|fwd?|aw|rv)\s*:", subject, re.I):
            errors.append("subject fakes a reply/forward")
        if subject.isupper() or "!" in subject:
            errors.append("subject is shouting")

    low, high = WORD_LIMITS[kind]
    words = len(body.split())
    if not low <= words <= high:
        errors.append(f"body has {words} words, expected {low}-{high}")
    text = f"{subject}\n{body}"
    for phrase in BANNED:
        if phrase in text.lower():
            errors.append(f"banned phrase: {phrase!r}")
    if PLACEHOLDER_RE.search(text):
        errors.append(f"unfilled placeholder: {PLACEHOLDER_RE.search(text).group(0)!r}")
    if re.search(r"[*_#`]{2}|^\s*[-*] ", body, re.M):
        errors.append("body contains Markdown formatting; write plain text")
    allowed_hosts = {lead.get("domain", "")} | {
        audit.domain_of(u) for u in (cfg["sender"].get("portfolio_url"), cfg["offer"].get("payment_link")) if u
    }
    for url in URL_RE.findall(body):
        host = audit.domain_of(url if "://" in url else "http://" + url)
        if host not in allowed_hosts:
            errors.append(f"link to {host} is not allowed in outreach")

    if kind == "first":
        known = {i["id"] for i in lead.get("issues", [])}
        used = draft.get("issues_used") or []
        if not used:
            errors.append("issues_used is empty: the email must cite at least one measured issue")
        for issue_id in used:
            if issue_id not in known:
                errors.append(f"issue {issue_id!r} was not measured on this site")
        name_tokens = [t for t in re.findall(r"[A-Za-zÀ-ÿ]{4,}", lead.get("name", ""))]
        if name_tokens and not any(t.lower() in text.lower() for t in name_tokens):
            errors.append("email never mentions the business by name")
    return errors


def render(draft: dict, cfg: dict, kind: str = "first") -> dict:
    lead = load_lead(draft["lead_id"])
    errors = validate(draft, lead, cfg, config.load_suppression(), kind)
    if errors:
        return {"ok": False, "lead_id": draft["lead_id"], "errors": errors}
    return {
        "ok": True,
        "lead_id": draft["lead_id"],
        "to": draft["to"].strip().lower(),
        "subject": draft.get("subject", "").strip(),
        "body": draft["body"].strip() + "\n\n" + footer(cfg),
        "issues_used": draft.get("issues_used", []),
    }


def pending(cfg: dict, handled: set[str], limit: int) -> list[dict]:
    """Leads nobody has contacted yet, best opportunities first."""
    suppression = config.load_suppression()
    rows = []
    for path in sorted(config.LEADS_DIR.glob("*.json")):
        lead = json.loads(path.read_text(encoding="utf-8"))
        keys = {lead["id"].lower(), lead["domain"].lower(), *(e.lower() for e in lead["emails"])}
        if keys & handled:
            continue
        emails = [e for e in lead["emails"] if not config.is_suppressed(e, suppression)]
        if not emails or config.is_suppressed(lead["domain"], suppression):
            continue
        rows.append({
            "lead_id": lead["id"], "name": lead["name"], "category": lead.get("category", ""),
            "city": lead.get("city", ""), "website": lead["website"], "email": emails[0], "score": lead["score"],
            "file": str(path.relative_to(config.ROOT)),
        })
    rows.sort(key=lambda r: -r["score"])
    return rows[:limit]


def preflight(cfg: dict) -> dict:
    leads = list(config.LEADS_DIR.glob("*.json"))
    return {
        "sender_missing": config.missing_sender_fields(cfg),
        "auto_send": bool(cfg["outreach"].get("auto_send")),
        "max_new_per_day": cfg["outreach"]["max_new_per_day"],
        "followups": cfg["outreach"]["followups"],
        "followup_after_days": cfg["outreach"]["followup_after_days"],
        "language": cfg["market"]["language"],
        "crm_sheet_id": cfg["crm"].get("sheet_id") or "",
        "crm_sheet_tab": cfg["crm"].get("sheet_tab") or "Leads",
        "leads_on_disk": len(leads),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    p_pending = sub.add_parser("pending")
    p_pending.add_argument("--handled", help="file with one lead id / email / domain per line already in the CRM")
    p_pending.add_argument("--limit", type=int, default=15)
    p_render = sub.add_parser("render")
    p_render.add_argument("draft", help="path to an agent-written draft JSON")
    p_render.add_argument("--followup", action="store_true")
    args = parser.parse_args()
    cfg = config.load_config()

    if args.command == "preflight":
        print(json.dumps(preflight(cfg), indent=2))
    elif args.command == "pending":
        handled: set[str] = set()
        if args.handled and Path(args.handled).exists():
            handled = {l.strip().lower() for l in Path(args.handled).read_text(encoding="utf-8").splitlines() if l.strip()}
        print(json.dumps(pending(cfg, handled, args.limit), indent=2, ensure_ascii=False))
    else:
        draft = json.loads(Path(args.draft).read_text(encoding="utf-8"))
        result = render(draft, cfg, "followup" if args.followup else "first")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if not result["ok"]:
            sys.exit(1)


if __name__ == "__main__":
    main()
