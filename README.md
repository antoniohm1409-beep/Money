# Web studio on autopilot

A small, real business run mostly by agents: **fixed-price website rebuilds for local
businesses whose current site is visibly out of date.**

Why this one: the buyer has money and an obvious problem, the product can be built in a
day with AI, the price ($300–$800) is an easy yes, and every step before the sale can be
automated without lying to anyone.

## How it works

```
 GitHub Actions (weekdays)            Claude agents (daily)                 You
┌──────────────────────────┐   ┌───────────────────────────────────┐   ┌──────────────────┐
│ find businesses (OSM)    │   │ outreach-writer → personal email  │   │ review + send    │
│ crawl + audit each site  │ → │ inbox-triager  → sort replies     │ → │ drafts in Gmail  │
│ keep weak sites w/ email │   │ site-builder   → homepage mock-up │   │ take the payment │
└──────────────────────────┘   └───────────────────────────────────┘   └──────────────────┘
        data/leads/*.json            Gmail drafts + Google Sheet CRM
```

1. **Find** — `pipeline/` pulls dentists, plumbers, lawyers, etc. for your cities from
   OpenStreetMap, visits each website and records concrete problems: not mobile-friendly,
   "Not secure" warning, copyright notice from 2016, 4-second load, and so on. Only sites
   that score badly *and* publish a contact email become leads.
2. **Write** — for each lead an agent writes a 60–110 word email that names the business,
   mentions one or two problems that were actually measured, and offers a free homepage
   mock-up. A validator rejects anything with invented claims, hype words, fake "Re:"
   subjects or links, and adds your name, address and an opt-out line.
3. **Reply** — replies are classified. "Yes, show me" triggers the site-builder agent,
   which builds a real one-page site from the business's own content and attaches phone
   and desktop previews to a reply draft. "No thanks" closes the lead permanently.
4. **Close** — you send the mock-up, then your payment link. Deliver the site (the
   mock-up is the starting point), offer hosting/edits as a monthly add-on.

Everything lands as **drafts in your Gmail** and rows in a **Google Sheet**; you stay the
person who presses send, until you choose to flip `auto_send`.

## Go live (about 15 minutes)

1. **Make this repository private** (GitHub → Settings → General → Danger Zone → Change
   visibility). Until then the lead finder only does dry runs, because lead data and your
   address should not be public.
2. **Fill in `config.yaml → sender`**: your name, a business name, a postal address.
   Adjust `offer.price`, `market.cities` and `market.categories` if you like.
3. **Run "Find leads"** once by hand (GitHub → Actions → Find leads → Run workflow). It
   then runs every weekday on its own.
4. **Start the daily agent**: in Claude Code, say `run the daily-run skill` — or ask
   Claude to schedule it every weekday. First drafts appear in Gmail within minutes.
5. Optional but worth it: a Stripe Payment Link in `offer.payment_link`, and two or three
   finished examples at `sender.portfolio_url`.

## What to expect — honestly

- Nothing here prints money. It produces **sales conversations**; you still have to send
  the emails, answer people like a human, and deliver good work.
- Most cold emails get no answer. A few replies per hundred sent is normal, and only some
  of those buy. At 15 emails a weekday, plan on weeks, not days, to the first sale — and
  expect the offer, price and city list to need tuning.
- The fastest money is usually closer than email: run the audit on businesses you can
  walk into or call (`python -m pipeline.run --url <site>`), and show them the result.
- Running costs: GitHub Actions is free at this volume; the agents run on your Claude
  plan; Gmail and Sheets are free.

## Legal — read before sending

Unsolicited business email is regulated differently per country. This is a summary, not
legal advice; check the rules for where your **recipients** are.

- **United States** (the default market here): allowed under CAN-SPAM if the sender and
  subject are truthful, it is clear the message is a commercial offer, it carries a real
  postal address, and opt-outs are honoured. The validator enforces these.
- **United Kingdom**: allowed to companies (Ltd, LLP), not to sole traders or
  partnerships without consent. Set `allow_freemail_recipients: false`.
- **Spain, Germany, Italy, Netherlands and several other EU countries**: prior consent is
  required even between businesses. Do **not** use the email step there — use the audits
  for phone calls or visits, and email only people who asked for it.
- **Canada, Australia**: have their own consent rules; check before adding cities.

Also: free Gmail accounts are not built for volume. Keep `max_new_per_day` at 15–20 or
your mail will start landing in spam. A mailbox on your own domain is the next step once
this earns anything.

## Commands

```bash
python -m unittest discover -s tests          # tests, no network needed
python -m pipeline.run --url https://site.tld # audit one website
python -m pipeline.run --dry-run --max 20     # try a prospecting run, save nothing
python -m pipeline.run --summary              # leads collected so far
python -m pipeline.compose preflight          # is the config ready to send?
node tools/screenshot.js demos/<lead-id>      # render + check a mock-up
```

`data/suppression.txt` — addresses and domains that must never be contacted.
