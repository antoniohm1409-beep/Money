import datetime as dt
import functools
import http.server
import shutil
import tempfile
import threading
import unittest

import requests
from pathlib import Path

from pipeline import audit, config, prospect, run

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = dt.date(2026, 11, 16)


def ids(result):
    return {i["id"] for i in result["issues"]}


class AnalyzeHtmlTest(unittest.TestCase):
    def setUp(self):
        self.dated = audit.analyze_html(
            "http://smithfamilydentistry.example/", (FIXTURES / "dated.html").read_text(), 4.2, TODAY
        )
        self.modern = audit.analyze_html(
            "https://lakeside.example/", (FIXTURES / "modern.html").read_text(), 0.4, TODAY
        )

    def test_dated_site_issues(self):
        self.assertEqual(
            ids(self.dated),
            {
                "no_viewport", "legacy_markup", "very_outdated_copyright", "slow_response", "outdated_cms",
                "old_jquery", "weak_title", "no_meta_description", "no_h1", "no_click_to_call",
                "images_missing_alt", "no_structured_data", "no_open_graph",
            },
        )
        self.assertEqual(audit.score_issues(self.dated["issues"]), 100)

    def test_evidence_is_specific(self):
        by_id = {i["id"]: i for i in self.dated["issues"]}
        self.assertIn("2016", by_id["very_outdated_copyright"]["plain"])
        self.assertIn("WordPress 4.9.8", by_id["outdated_cms"]["evidence"])
        self.assertIn("jQuery 1.7", by_id["old_jquery"]["plain"])
        self.assertIn("3 of 4 images", by_id["images_missing_alt"]["evidence"])
        self.assertIn("took 4.2 seconds", by_id["slow_response"]["plain"])

    def test_modern_site_is_clean(self):
        self.assertEqual(ids(self.modern), set())
        self.assertEqual(audit.score_issues(self.modern["issues"]), 0)

    def test_copyright_range_uses_latest_year(self):
        html = "<html><head><title>X co</title></head><body>© 2009-2025 X</body></html>"
        self.assertNotIn("outdated_copyright", ids(audit.analyze_html("https://x.example", html, 0.1, TODAY)))
        html = html.replace("2009-2025", "2009 &ndash; 2023 X, est. 1987")
        self.assertIn("outdated_copyright", ids(audit.analyze_html("https://x.example", html, 0.1, TODAY)))

    def test_script_licence_years_are_not_the_footer(self):
        html = ("<html><head><title>X co</title><script>/*! lib | Copyright 2011 Lib Foundation */</script></head>"
                "<body><!-- copyright 2010 --><p>&copy; 2026 X</p></body></html>")
        found = ids(audit.analyze_html("https://x.example", html, 0.1, TODAY))
        self.assertFalse(found & {"outdated_copyright", "very_outdated_copyright"})

    def test_jquery_version_detection(self):
        def found(src):
            html = f'<html><head><title>X co</title><script src="{src}"></script></head><body></body></html>'
            return "old_jquery" in ids(audit.analyze_html("https://x.example", html, 0.1, TODAY))

        self.assertTrue(found("https://ajax.googleapis.com/ajax/libs/jquery/1.12.4/jquery.min.js"))
        self.assertTrue(found("/js/jquery-1.4.2.js"))
        self.assertFalse(found("https://code.jquery.com/jquery-3.6.0.min.js"))
        self.assertFalse(found("/js/jquery-ui-1.8.2.min.js"))
        self.assertFalse(found("/js/jquery.fancybox-1.3.4.js"))
        self.assertFalse(found("/js/jquery-migrate-1.4.1.min.js"))

    def test_only_certainly_downloaded_images_are_weighed(self):
        html = ('<html><head><title>X co</title></head><body><img src="/hero.jpg" alt="a">'
                '<img src="/lazy.jpg" loading="lazy" alt="b"><img src="/set.jpg" srcset="/s.jpg 480w" alt="c">'
                '<img src="data:image/gif;base64,AAAA" alt="d"></body></html>')
        result = audit.analyze_html("https://x.example/", html, 0.1, TODAY)
        self.assertEqual(result["eager_images"], ["https://x.example/hero.jpg"])
        self.assertEqual(len(result["content"]["images"]), 3)

    def test_mixed_content_only_on_https(self):
        html = '<html><head><title>X co</title></head><body><img src="http://x.example/a.jpg" alt="a"></body></html>'
        self.assertIn("mixed_content", ids(audit.analyze_html("https://x.example", html, 0.1, TODAY)))
        self.assertNotIn("mixed_content", ids(audit.analyze_html("http://x.example", html, 0.1, TODAY)))

    def test_emails_extracted_and_junk_dropped(self):
        self.assertIn("office@smithfamilydentistry.example", self.dated["emails"])
        for junk in ("banner@2x.png", "user@example.com"):
            self.assertNotIn(junk, self.dated["emails"])
        self.assertFalse(any("sentry.io" in e for e in self.dated["emails"]))
        self.assertEqual(
            self.dated["emails"],
            ["office@smithfamilydentistry.example", "billing@smithfamilydentistry.example"],
        )

    def test_cloudflare_email_decoding(self):
        self.assertEqual(audit.decode_cfemail("54363d38383d3a331427393d203c3235393d382d30313a203d2720262d7a312c3539243831"), "billing@smithfamilydentistry.example")
        self.assertIsNone(audit.decode_cfemail("zz"))

    def test_contact_links_same_domain_contact_first(self):
        self.assertEqual(
            self.dated["contact_links"],
            ["http://smithfamilydentistry.example/contact-us.html", "http://smithfamilydentistry.example/about.html"],
        )

    def test_content_for_writers(self):
        content = self.modern["content"]
        self.assertEqual(content["h1"], ["Austin's same-day plumbers"])
        self.assertEqual(content["phones"], ["+15125550199"])
        self.assertEqual(content["logo"], "https://lakeside.example/logo.svg")
        self.assertIn("Smith Family Dentistry", self.dated["content"]["text"])
        self.assertNotIn("jquery", self.dated["content"]["text"].lower())


class HeavyImagesTest(unittest.TestCase):
    def test_thresholds(self):
        base = {"count": 5, "largest_url": "https://x.example/a.jpg"}
        self.assertIsNone(audit.heavy_images_issue(None))
        self.assertIsNone(audit.heavy_images_issue({**base, "total_mb": 2.4, "largest_mb": 0.9}))
        self.assertIsNotNone(audit.heavy_images_issue({**base, "total_mb": 2.6, "largest_mb": 0.6}))
        self.assertIsNotNone(audit.heavy_images_issue({**base, "total_mb": 1.2, "largest_mb": 1.1}))


class EmailRankTest(unittest.TestCase):
    def test_own_domain_first_then_freemail_then_others(self):
        ranked = audit.rank_emails(
            ["webmaster2@agency.example", "doc@gmail.com", "info@smith.example", "doc@gmail.com"], "smith.example"
        )
        self.assertEqual(ranked, ["info@smith.example", "doc@gmail.com", "webmaster2@agency.example"])


class ProspectTest(unittest.TestCase):
    CATS = ["amenity=dentist", "craft=plumber"]

    def test_query_covers_both_website_keys(self):
        query = prospect.build_overpass_query(self.CATS, (30.1, -97.9, 30.5, -97.5))
        self.assertIn('nwr["amenity"="dentist"]["website"](30.1,-97.9,30.5,-97.5);', query)
        self.assertIn('nwr["craft"="plumber"]["contact:website"]', query)

    def test_normalize_website(self):
        self.assertEqual(prospect.normalize_website("www.smith.example"), "http://www.smith.example")
        self.assertIsNone(prospect.normalize_website("https://www.facebook.com/smithdental"))
        self.assertIsNone(prospect.normalize_website(""))
        self.assertEqual(prospect.domain_of("https://www.Smith.example/path"), "smith.example")

    def test_chains_and_brands_are_dropped(self):
        def el(i, name, site, **extra):
            return {"type": "node", "id": i, "tags": {"name": name, "amenity": "dentist", "website": site, **extra}}

        elements = [
            el(1, "Smith Dental", "https://smith.example", email="a@smith.example", **{"addr:city": "Austin"}),
            el(2, "Chain A", "https://chain.example/austin-1"),
            el(3, "Chain B", "https://chain.example/austin-2"),
            el(4, "Chain C", "https://chain.example/austin-3"),
            el(5, "Big Brand", "https://brand.example", brand="Big Brand"),
            el(6, "No site", "https://facebook.com/nosite"),
            {"type": "way", "id": 7, "tags": {"craft": "plumber", "contact:website": "pipes.example", "name": "Pipes"}},
        ]
        result = prospect.dedupe_independents(elements, self.CATS, "Austin")
        self.assertEqual([b["domain"] for b in result], ["smith.example", "pipes.example"])
        self.assertEqual(result[0]["osm_emails"], ["a@smith.example"])
        self.assertEqual(result[0]["id"], "smith-example")
        self.assertEqual(result[1]["category"], "plumber")


class ConfigTest(unittest.TestCase):
    def test_placeholder_sender_blocks_outreach(self):
        self.assertEqual(config.missing_sender_fields({"sender": {"name": "TODO", "business_name": ""}}),
                         ["name", "business_name", "postal_address"])
        self.assertEqual(
            config.missing_sender_fields({"sender": {"name": "A", "business_name": "B", "postal_address": "C"}}), []
        )

    def test_suppression_matches_email_and_domain(self):
        suppression = {"blocked.example", "one@mixed.example"}
        self.assertTrue(config.is_suppressed("anyone@blocked.example", suppression))
        self.assertTrue(config.is_suppressed("Blocked.example", suppression))
        self.assertTrue(config.is_suppressed("one@mixed.example", suppression))
        self.assertFalse(config.is_suppressed("two@mixed.example", suppression))

    def test_repo_config_loads(self):
        cfg = config.load_config()
        self.assertFalse(cfg["outreach"]["auto_send"])
        self.assertTrue(cfg["market"]["cities"])


class AuditSiteOverHttpTest(unittest.TestCase):
    """End-to-end audit against a local web server serving the fixtures."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        site = Path(cls.tmp.name)
        shutil.copytree(FIXTURES, site, dirs_exist_ok=True)
        (site / "images").mkdir()
        (site / "images" / "office1.jpg").write_bytes(b"\0" * 2_200_000)
        (site / "images" / "office2.jpg").write_bytes(b"\0" * 900_000)
        handler = functools.partial(QuietHandler, directory=str(site))
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def business(self, page):
        return {"id": "t", "name": "T", "website": f"{self.base}/{page}", "domain": "127.0.0.1", "osm_emails": []}

    def test_full_audit_follows_contact_page(self):
        lead = audit.audit_site(self.business("dated.html"), "test-agent", timeout=5)
        self.assertEqual(lead["status"], "ok")
        self.assertEqual(lead["score"], 100)
        self.assertIn("no_https", {i["id"] for i in lead["issues"]})
        self.assertIn("frontdesk@smithfamilydentistry.example", lead["emails"])
        self.assertIn("drsmith1998@gmail.com", lead["emails"])
        self.assertEqual(lead["issues"][0]["id"], "no_viewport")  # sorted by weight
        heavy = next(i for i in lead["issues"] if i["id"] == "heavy_images")
        self.assertIn("about 3.1 MB of images (the largest alone is 2.2 MB)", heavy["plain"])
        self.assertIn("2 home page images", heavy["evidence"])  # logo.gif and team.jpg do not exist

    def test_endless_response_is_cut_off(self):
        (Path(self.tmp.name) / "huge.html").write_bytes(b"<html><body>" + b"x" * 5_000_000)
        resp, elapsed = audit._fetch(requests.Session(), f"{self.base}/huge.html", timeout=5)
        self.assertLessEqual(len(resp.content), audit.MAX_BODY_BYTES)
        self.assertGreater(len(resp.content), 1_000_000)
        self.assertLess(elapsed, 5)

    def test_missing_page_is_not_a_lead(self):
        lead = audit.audit_site(self.business("nope.html"), "test-agent", timeout=5)
        self.assertEqual(lead["status"], "bad_response: HTTP 404")
        cfg = config.load_config()
        self.assertEqual(run.qualifies(lead, cfg, set()), (False, "bad_response: HTTP 404"))

    def test_qualification_rules(self):
        cfg = config.load_config()
        lead = audit.audit_site(self.business("dated.html"), "test-agent", timeout=5)
        self.assertEqual(run.qualifies(dict(lead), cfg, set()), (True, "lead"))
        everything = {e.rsplit("@", 1)[-1] for e in lead["emails"]} | set(lead["emails"])
        self.assertEqual(run.qualifies(dict(lead), cfg, everything), (False, "no_email"))
        # plenty of points but nothing a customer would notice -> not worth an email
        seo_only = dict(lead, score=60, issues=[{"id": "no_meta_description"}, {"id": "no_h1"}])
        self.assertEqual(run.qualifies(seo_only, cfg, set()), (False, "site_good_enough"))
        good = audit.audit_site(self.business("modern.html"), "test-agent", timeout=5)
        # local server is plain http and sends no charset header: the only finding must be the
        # missing https, and the UTF-8 "© 2019–2026" footer must still be read as 2026
        self.assertEqual([i["id"] for i in good["issues"]], ["no_https"])
        self.assertEqual(run.qualifies(dict(good), cfg, set()), (True, "lead"))  # "Not secure" alone is a lead
        clean = dict(good, issues=[], score=0)  # the same site once it is served over https
        self.assertEqual(run.qualifies(clean, cfg, set()), (False, "site_good_enough"))


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


if __name__ == "__main__":
    unittest.main()


class ComposeTest(unittest.TestCase):
    LEAD = {
        "id": "smith-example", "name": "Smith Family Dentistry", "domain": "smith.example",
        "emails": ["office@smith.example", "drsmith@gmail.com"],
        "issues": [{"id": "no_viewport"}, {"id": "very_outdated_copyright"}],
    }
    BODY = (
        "Hi, I was looking at dentists in Austin and opened the Smith Family Dentistry site on my phone. "
        "It shows the desktop layout shrunk down, so the phone number and hours are hard to read without zooming, "
        "and the copyright notice still says 2016. I rebuild sites like this for local practices. "
        "I'd be glad to put together a free mock-up of a new homepage using your existing content, no commitment. "
        "If you like it, the full rebuild is a flat $490. Want me to send it over?"
    )

    def cfg(self, **sender):
        cfg = config.load_config()
        cfg["sender"].update({"name": "Alex Doe", "business_name": "Doe Web", "postal_address": "1 Main St, Austin TX"})
        cfg["sender"].update(sender)
        return cfg

    def draft(self, **over):
        draft = {"lead_id": "smith-example", "to": "office@smith.example", "subject": "smith family dentistry on mobile",
                 "body": self.BODY, "issues_used": ["no_viewport", "very_outdated_copyright"]}
        draft.update(over)
        return draft

    def errors(self, draft=None, cfg=None, suppression=frozenset(), kind="first"):
        from pipeline import compose
        return compose.validate(draft or self.draft(), self.LEAD, cfg or self.cfg(), set(suppression), kind)

    def test_good_draft_passes_and_gets_footer(self):
        from pipeline import compose
        self.assertEqual(self.errors(), [])
        foot = compose.footer(self.cfg(phone="+1 512 555 0100"))
        self.assertIn("Alex Doe · Doe Web", foot)
        self.assertIn("1 Main St, Austin TX", foot)
        self.assertIn("no thanks", foot)

    def test_unfilled_sender_blocks_everything(self):
        errors = self.errors(cfg=config.load_config())
        self.assertTrue(any("sender fields not filled in" in e for e in errors))

    def test_recipient_must_come_from_the_audit_and_not_be_suppressed(self):
        self.assertTrue(any("not one of the addresses" in e for e in self.errors(self.draft(to="ceo@other.example"))))
        self.assertTrue(any("suppression" in e for e in self.errors(suppression={"smith.example"})))

    def test_only_measured_issues_may_be_cited(self):
        self.assertTrue(any("was not measured" in e for e in self.errors(self.draft(issues_used=["slow_response"]))))
        self.assertTrue(any("issues_used is empty" in e for e in self.errors(self.draft(issues_used=[]))))

    def test_deceptive_or_sloppy_copy_is_rejected(self):
        cases = {
            "fakes a reply": self.draft(subject="Re: your website"),
            "shouting": self.draft(subject="Fix your site now!"),
            "banned phrase": self.draft(body=self.BODY + " I guarantee more patients."),
            "placeholder": self.draft(body=self.BODY.replace("Hi,", "Hi [Name],")),
            "not allowed": self.draft(body=self.BODY + " See https://bit.ly/abc"),
            "expected 45-140": self.draft(body="Smith Family Dentistry, too short."),
            "never mentions": self.draft(subject="your site on mobile", body=self.BODY.replace("Smith Family Dentistry ", "your ")),
            "Markdown": self.draft(body=self.BODY.replace("flat $490", "**flat $490**")),
        }
        for expected, draft in cases.items():
            with self.subTest(expected):
                self.assertTrue(any(expected in e for e in self.errors(draft)), self.errors(draft))

    def test_own_site_link_is_allowed(self):
        self.assertEqual(self.errors(self.draft(body=self.BODY + " (www.smith.example)")), [])

    def test_followup_rules(self):
        body = ("Just floating this back up in case it got buried: happy to send that free homepage mock-up for "
                "Smith Family Dentistry. If now is not the time, no problem, this is my last note.")
        self.assertEqual(self.errors(self.draft(subject="", body=body, issues_used=[]), kind="followup"), [])
