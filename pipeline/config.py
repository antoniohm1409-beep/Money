from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
DATA_DIR = ROOT / "data"
LEADS_DIR = DATA_DIR / "leads"
SEEN_PATH = DATA_DIR / "seen.json"
SUPPRESSION_PATH = DATA_DIR / "suppression.txt"

SENDER_REQUIRED = ("name", "business_name", "postal_address")


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def missing_sender_fields(cfg: dict) -> list[str]:
    """Sender fields that still hold a placeholder. Outreach must not start while any remain."""
    sender = cfg.get("sender") or {}
    return [
        key
        for key in SENDER_REQUIRED
        if not str(sender.get(key) or "").strip() or str(sender.get(key)).strip().upper() == "TODO"
    ]


def load_suppression(path: Path = SUPPRESSION_PATH) -> set[str]:
    """Emails and domains that must never be contacted (opt-outs, complaints, own clients)."""
    if not path.exists():
        return set()
    entries = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line:
            entries.add(line)
    return entries


def is_suppressed(email_or_domain: str, suppression: set[str]) -> bool:
    value = email_or_domain.strip().lower()
    if value in suppression:
        return True
    domain = value.rsplit("@", 1)[-1]
    return domain in suppression
