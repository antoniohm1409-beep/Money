---
name: build-demo
description: Build the homepage mock-up for one lead (demos/<lead-id>/index.html plus phone and desktop preview images). Use when a lead asks to see the mock-up, or when asked to "build the demo for <business>".
---

# Build a demo

Input: a lead id (the file name in `data/leads/` without `.json`). If you were given a
business name instead, find its file with Grep.

1. Read `data/leads/<id>.json`. If `content.text` is thin (under ~300 characters), tell
   the owner the mock-up will be sparse and continue.
2. Launch the **site-builder** agent with `lead_file: data/leads/<id>.json` and
   `out_dir: demos/<id>/`.
3. Verify independently — do not take the builder's word for it:
   ```bash
   node tools/screenshot.js demos/<id>
   ```
   It must exit 0. Then open `demos/<id>/preview-mobile.png` and `preview-desktop.png`
   with Read and check: the business name and phone are right (compare with the lead
   file), nothing on the page is invented (no reviews, prices or claims that are not in
   the lead file), and it looks finished at both sizes. Send it back to the builder with
   specific notes if not.
4. Report the three file paths. They are attached to the reply draft by the caller
   (Gmail `create_draft` → `attachments`, as file references).

How the owner shares it with the client: the two PNG previews go in the email. For a live
link, the `index.html` can be dropped onto any static host; when the client buys, the
same file is the starting point of the real site (remove the "Preview" ribbon first).
