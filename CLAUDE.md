# Web studio outreach pipeline

A one-person business: sell fixed-price website rebuilds to local businesses whose current
site is measurably weak. Code finds and audits prospects; agents write the outreach,
handle replies and build the mock-ups; the owner reviews, sends and closes.

## Map
- `config.yaml` — the owner's identity, offer, market, limits. Single source of truth.
- `pipeline/prospect.py` — businesses from OpenStreetMap (Nominatim + Overpass).
- `pipeline/audit.py` — crawls a site, records issues **with evidence**, finds emails.
- `pipeline/run.py` — CLI used by `.github/workflows/prospect.yml`; writes `data/leads/`.
- `pipeline/compose.py` — guardrails: `preflight`, `pending`, `render` (validates an
  agent-written email and appends the legal footer).
- `.claude/agents/` — `outreach-writer` (sonnet), `inbox-triager` (haiku),
  `site-builder` (opus).
- `.claude/skills/daily-run` — the operating procedure for one full cycle.
- `.claude/skills/build-demo` — mock-up for a lead who said yes.
- `tools/screenshot.js` — renders a demo at phone/desktop width and fails on defects.
- CRM = Google Sheet (`crm.sheet_id`); mail = the owner's Gmail (connectors).

## Rules that are not negotiable
- Outreach only states what `audit.py` measured. No invented problems, statistics,
  testimonials, results or urgency. If a check is not reliable enough to say to an
  owner's face, fix the check or drop it.
- Nothing is sent unless `outreach.auto_send` is `true`; otherwise drafts only. Replies
  to a human are always drafts.
- Opt-outs are permanent. `data/suppression.txt` and CRM statuses `unsubscribed`,
  `not_interested`, `bounced` are checked before every message.
- Volume stays low (`max_new_per_day`). This is personal outreach from a real mailbox,
  not bulk mail.
- Lead data (business emails, audits) and the owner's details must not be published:
  the repository must be private before `data/leads/` is committed. The workflow
  enforces this with a dry run.

## Working here
- Tests: `python -m unittest discover -s tests` (no network needed).
- One site: `python -m pipeline.run --url https://example.com` (needs open network;
  Claude cloud sessions usually cannot reach arbitrary sites — that is why crawling runs
  on GitHub Actions).
- New audit checks need a fixture-based test and an `evidence` string.
