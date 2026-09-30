"""Tests for excluded paths, content types and keyword alerts."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scrape  # noqa: E402
from test_diff import FakeFetcher, comp, urlset  # noqa: E402


class ExcludeTests(unittest.TestCase):
    def test_prefix_pattern(self):
        rx = [scrape.compile_path_pattern("/careers")]
        self.assertTrue(scrape.is_excluded("https://x.com/careers", "", rx))
        self.assertTrue(scrape.is_excluded("https://x.com/careers/dev-1", "", rx))
        self.assertFalse(scrape.is_excluded("https://x.com/careers-blog", "", rx))
        self.assertFalse(scrape.is_excluded("https://x.com/blog/careers", "", rx))

    def test_wildcard_and_domain_path(self):
        rx = [scrape.compile_path_pattern("*/jobs/*"), scrape.compile_path_pattern("retail")]
        self.assertTrue(scrape.is_excluded("https://x.com/en/jobs/a", "", rx))
        self.assertTrue(scrape.is_excluded("https://www.nike.com/il/retail/s/tlv", "/il", rx))
        self.assertFalse(scrape.is_excluded("https://www.nike.com/il/t/shoe", "/il", rx))

    def test_collect_urls_skips_excluded(self):
        b = "https://www.example.com"
        c = comp()
        c["exclude_rx"] = [scrape.compile_path_pattern("/careers")]
        pages = {f"{b}/sitemap.xml": urlset((f"{b}/a", None), (f"{b}/careers/x", None))}
        urls, meta, _ = scrape.collect_urls(FakeFetcher(pages), c)
        self.assertEqual(list(urls), [f"{b}/a"])
        self.assertEqual(meta["excluded"], 1)


class AssetTypeTests(unittest.TestCase):
    def test_defaults(self):
        cases = {
            "https://x.com/blog/why-ai": "blog",
            "https://x.com/resources/webinars/ai-2026": "webinar",
            "https://x.com/whitepaper-cloud-security": "whitepaper",
            "https://x.com/ebooks/guide": "whitepaper",
            "https://x.com/case-studies/acme": "case_study",
            "https://x.com/news/launch": "news",
            "https://x.com/products/shoe-1": "product",
            "https://x.com/shoe/p/ABC.html": "product",
            "https://x.com/collections/summer": "category",
            "https://x.com/about": "other",
        }
        for url, want in cases.items():
            self.assertEqual(scrape.classify_asset(url, ""), want, url)

    def test_title_fallback_and_overrides(self):
        self.assertEqual(scrape.classify_asset("https://x.com/lp/cloud", "", page={"title": "Free Webinar: Cloud"}), "webinar")
        over = [(scrape.compile_path_pattern("/a"), "blog")]
        self.assertEqual(scrape.classify_asset("https://www.nike.com/il/a/cadence", "/il", over), "blog")


class KeywordTests(unittest.TestCase):
    def test_matches_url_title_and_phrases(self):
        kws = scrape.compile_keywords(["running", "Gore-Tex", "air max"])
        self.assertEqual(scrape.match_keywords(kws, "https://x.com/t/trail-running-shoe", ""), ["running"])
        self.assertEqual(scrape.match_keywords(kws, "https://x.com/t/1", "", ["New GORE TEX jacket"]), ["Gore-Tex"])
        self.assertEqual(scrape.match_keywords(kws, "https://x.com/air-max-90", ""), ["air max"])
        self.assertEqual(scrape.match_keywords(kws, "https://x.com/runningman", ""), [])


class EndToEndAdditionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = scrape.DATA_DIR, scrape.Fetcher
        scrape.DATA_DIR = Path(self.tmp.name)
        scrape.PAGE_DELAY_S = 0

    def tearDown(self):
        scrape.DATA_DIR, scrape.Fetcher = self._old
        self.tmp.cleanup()

    def run_with(self, pages, when, c, keywords=()):
        scrape.Fetcher = lambda: FakeFetcher(pages)
        return scrape.process_competitor({"id": "c", "keywords": list(keywords)}, c, when)

    def test_new_exclusion_is_not_logged_as_removed_and_events_are_tagged(self):
        b = "https://www.example.com"
        page = lambda t: f"<title>{t}</title><h1>{t}</h1>".encode()
        pages = {
            f"{b}/sitemap.xml": urlset((f"{b}/blog/a", "1"), (f"{b}/careers/dev", "1")),
            f"{b}/blog/a": page("A"), f"{b}/careers/dev": page("Dev"),
        }
        c = comp()
        self.run_with(pages, "2026-09-23T05:00:00Z", c)

        # Day 2: careers gets excluded and a new webinar page appears.
        c["exclude_rx"] = [scrape.compile_path_pattern("/careers")]
        pages[f"{b}/sitemap.xml"] = urlset((f"{b}/blog/a", "1"), (f"{b}/careers/dev", "1"), (f"{b}/webinars/ai-running", "1"))
        pages[f"{b}/webinars/ai-running"] = page("AI for runners")
        r = self.run_with(pages, "2026-09-24T05:00:00Z", c, keywords=["running"])
        self.assertEqual(r["last_counts"], {"added": 1, "keyword_hits": 1})
        self.assertEqual(r["excluded_urls"], 1)
        log = scrape.read_json(scrape.DATA_DIR / "changes" / "c" / "ex.json", {})
        ev = log["events"][0]
        self.assertEqual(ev["asset_type"], "webinar")
        self.assertEqual(ev["keywords"], ["running"])
        self.assertEqual(ev["page"]["title"], "AI for runners")


class ConfigAdditionsTests(unittest.TestCase):
    def test_repo_config_has_the_new_settings(self):
        c = scrape.load_config()[0]
        self.assertTrue(c["keywords"])
        nike = c["competitors"][0]
        self.assertIn("/retail", nike["exclude"])
        self.assertIn("/careers", nike["exclude"])  # client-level exclude is inherited

    def test_unknown_asset_type_is_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write("clients:\n  - id: a\n    competitors:\n      - name: X\n        domain: x.com\n        asset_types:\n          /a: blogg\n")
        with self.assertRaises(ValueError):
            scrape.load_config(Path(fh.name))


if __name__ == "__main__":
    unittest.main()
