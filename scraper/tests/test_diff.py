import gzip
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scrape  # noqa: E402

NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


def urlset(*entries):
    body = "".join(
        f"<url><loc>{loc}</loc>{f'<lastmod>{lm}</lastmod>' if lm else ''}</url>" for loc, lm in entries
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset {NS}>{body}</urlset>'.encode()


def index(*locs):
    body = "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
    return f'<?xml version="1.0"?><sitemapindex {NS}>{body}</sitemapindex>'.encode()


class FakeResp:
    def __init__(self, content, status=200, url="", headers=None):
        self.content = content
        self.text = content.decode("utf-8", "replace") if isinstance(content, bytes) else content
        self.status_code = status
        self.url = url
        self.headers = headers or {"Content-Type": "text/html"}
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, max_bytes=None, allow_status=()):
        self.calls.append(url)
        if url not in self.pages:
            raise scrape.FetchError(url, 404)
        v = self.pages[url]
        if isinstance(v, int):
            if v in allow_status:
                return FakeResp(b"", v, url)
            raise scrape.FetchError(url, v)
        return FakeResp(v, 200, url)


def comp(domain="www.example.com", sitemaps=None):
    scheme, host, prefix = scrape.parse_domain(domain)
    return {"name": "Ex", "slug": "ex", "domain": domain, "scheme": scheme, "host": host, "prefix": prefix, "sitemaps": sitemaps or []}


class NormalizeTests(unittest.TestCase):
    def test_strips_tracking_fragment_trailing_slash(self):
        self.assertEqual(
            scrape.normalize_url("HTTPS://WWW.Example.com/Shoes/?utm_source=x&b=2&a=1#top"),
            "https://www.example.com/Shoes?a=1&b=2",
        )

    def test_root_keeps_slash(self):
        self.assertEqual(scrape.normalize_url("https://example.com"), "https://example.com/")

    def test_rejects_non_http(self):
        self.assertIsNone(scrape.normalize_url("mailto:a@b.com"))

    def test_scope_prefix(self):
        self.assertTrue(scrape.in_scope("https://www.nike.com/il/running", "www.nike.com", "/il"))
        self.assertTrue(scrape.in_scope("https://nike.com/il", "www.nike.com", "/il"))
        self.assertFalse(scrape.in_scope("https://www.nike.com/ilx/running", "www.nike.com", "/il"))
        self.assertFalse(scrape.in_scope("https://www.nike.com/us/running", "www.nike.com", "/il"))

    def test_section(self):
        self.assertEqual(scrape.section_of("https://x.com/il/running/shoe-1", "/il"), "/running")
        self.assertEqual(scrape.section_of("https://x.com/il", "/il"), "/")

    def test_sectioner_buckets_top_level_products_and_one_offs(self):
        b = "https://x.com/us"
        urls = [f"{b}/blog/post-{i}" for i in range(5)] + [
            f"{b}/gel-nimbus/p/1011B794-001.html",
            f"{b}/2-pack-socks/Z600278.html",
            f"{b}/about/team",
            f"{b}/help-center.html",
        ]
        sec = scrape.make_sectioner(urls, "/us")
        self.assertEqual(sec(f"{b}/blog/post-1"), "/blog")
        self.assertEqual(sec(f"{b}/gel-nimbus/p/1011B794-001.html"), scrape.PRODUCT_SECTION)
        self.assertEqual(sec(f"{b}/2-pack-socks/Z600278.html"), scrape.PRODUCT_SECTION)
        self.assertEqual(sec(f"{b}/about/team"), scrape.OTHER_SECTION)
        self.assertEqual(sec(f"{b}/help-center.html"), scrape.OTHER_SECTION)
        self.assertEqual(sec(b), "/")

    def test_parse_domain(self):
        self.assertEqual(scrape.parse_domain("www.nike.com/il/"), ("https", "www.nike.com", "/il"))
        with self.assertRaises(ValueError):
            scrape.parse_domain("localhost")


class SitemapTests(unittest.TestCase):
    def test_index_recursion_gzip_and_prefix_filter(self):
        base = "https://www.example.com"
        pages = {
            f"{base}/robots.txt": f"User-agent: *\nSitemap: {base}/sitemap_index.xml\n".encode(),
            f"{base}/sitemap_index.xml": index(f"{base}/sitemap-il.xml.gz", f"{base}/sitemap-us.xml"),
            f"{base}/sitemap-il.xml.gz": gzip.compress(urlset((f"{base}/il/a", "2026-01-01"), (f"{base}/il/b/", None), (f"{base}/us/c", None))),
            f"{base}/sitemap-us.xml": urlset((f"{base}/us/d", None)),
        }
        f = FakeFetcher(pages)
        urls, meta, _ = scrape.collect_urls(f, comp("www.example.com/il"))
        self.assertEqual(urls, {f"{base}/il/a": "2026-01-01", f"{base}/il/b": None})
        self.assertTrue(meta["complete"])
        # The locale heuristic skips the US child sitemap entirely.
        self.assertNotIn(f"{base}/sitemap-us.xml", f.calls)

    def test_robots_roots_are_not_narrowed_by_locale(self):
        base = "https://www.example.com"
        pages = {
            f"{base}/robots.txt": f"Sitemap: {base}/sitemap_index_all.xml\nSitemap: {base}/extra-il.xml\n".encode(),
            f"{base}/sitemap_index_all.xml": urlset((f"{base}/il/main", None)),
            f"{base}/extra-il.xml": urlset((f"{base}/il/extra", None)),
        }
        urls, _, _ = scrape.collect_urls(FakeFetcher(pages), comp("www.example.com/il"))
        self.assertEqual(set(urls), {f"{base}/il/main", f"{base}/il/extra"})

    def test_fallback_and_partial(self):
        base = "https://www.example.com"
        pages = {
            f"{base}/sitemap.xml": index(f"{base}/s1.xml", f"{base}/s2.xml"),
            f"{base}/s1.xml": urlset((f"{base}/a", None)),
            f"{base}/s2.xml": 500,
        }
        urls, meta, _ = scrape.collect_urls(FakeFetcher(pages), comp())
        self.assertEqual(list(urls), [f"{base}/a"])
        self.assertFalse(meta["complete"])
        self.assertTrue(meta["any_ok"])


class DiffTests(unittest.TestCase):
    ok = {"any_ok": True, "complete": True}

    def test_added_removed_updated(self):
        prev = {"u1": {"lastmod": "1"}, "u2": {"lastmod": "1"}, "u3": {"lastmod": None}}
        new = {"u1": "1", "u2": "2", "u4": None}
        events, merged, status, _, _ = scrape.diff_snapshot(prev, new, {}, self.ok, "2026-09-24T00:00:00Z")
        types = sorted((e["type"], e["url"]) for e in events)
        self.assertEqual(types, [("added", "u4"), ("removed", "u3"), ("updated", "u2")])
        self.assertEqual(set(merged), {"u1", "u2", "u4"})
        self.assertEqual(status, "ok")

    def test_partial_fetch_skips_removals(self):
        prev = {"u1": {}, "u2": {}}
        events, merged, status, _, _ = scrape.diff_snapshot(prev, {"u1": None, "u9": None}, {}, {"any_ok": True, "complete": False}, "t")
        self.assertEqual([e["type"] for e in events], ["added"])
        self.assertIn("u2", merged)
        self.assertEqual(status, "partial")

    def test_mass_drop_guard_then_confirm(self):
        prev = {f"u{i}": {} for i in range(100)}
        new = {f"u{i}": None for i in range(30)}
        events, merged, status, _, pending = scrape.diff_snapshot(prev, new, {}, self.ok, "t")
        self.assertEqual(events, [])
        self.assertEqual(len(merged), 100)
        self.assertEqual(status, "partial")
        self.assertEqual(pending, 30)
        # Same drop on the next run is accepted.
        events, merged, status, _, pending = scrape.diff_snapshot(prev, new, {"pending_drop": 30}, self.ok, "t")
        self.assertEqual(sum(e["type"] == "removed" for e in events), 70)
        self.assertEqual(status, "ok")

    def test_no_sitemap_is_error(self):
        _, merged, status, _, _ = scrape.diff_snapshot({"u": {}}, {}, {}, {"any_ok": False}, "t")
        self.assertEqual(status, "error")
        self.assertEqual(merged, {"u": {}})


class SEOTests(unittest.TestCase):
    def test_extract(self):
        html = textwrap.dedent("""
            <html><head><title> Running  Shoes | Nike </title>
            <meta name="Description" content="Shop running shoes.">
            <link rel="canonical" href="/il/running"></head>
            <body><h1>Running <span>Shoes</span></h1><h1>Second</h1></body></html>""")
        seo = scrape.extract_seo(html, "https://www.nike.com/il/running?x=1")
        self.assertEqual(seo, {"title": "Running Shoes | Nike", "description": "Shop running shoes.", "h1": "Running Shoes", "canonical": "https://www.nike.com/il/running"})

    def test_seo_event_only_changed_fields(self):
        before = {"title": "A", "description": "d", "h1": "H", "canonical": None}
        after = {"title": "B", "description": "d", "h1": "H", "canonical": None}
        ev = scrape.seo_event("u", before, after)
        self.assertEqual(ev["fields"], ["title"])
        self.assertEqual(ev["before"], {"title": "A"})
        self.assertIsNone(scrape.seo_event("u", before, dict(before)))


class EndToEndTests(unittest.TestCase):
    """Runs process_competitor twice against a fake site."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = scrape.DATA_DIR
        scrape.DATA_DIR = Path(self.tmp.name)
        scrape.PAGE_DELAY_S = 0
        self._old_fetcher = scrape.Fetcher

    def tearDown(self):
        scrape.DATA_DIR = self._old
        scrape.Fetcher = self._old_fetcher
        self.tmp.cleanup()

    def run_with(self, pages, when):
        scrape.Fetcher = lambda: FakeFetcher(pages)
        return scrape.process_competitor({"id": "c"}, comp(), when)

    def test_baseline_then_changes(self):
        b = "https://www.example.com"
        page = lambda t: f"<title>{t}</title><h1>{t}</h1>".encode()
        pages = {
            f"{b}/sitemap.xml": urlset((f"{b}/a", "1"), (f"{b}/b", "1")),
            f"{b}/a": page("A"), f"{b}/b": page("B"),
        }
        r1 = self.run_with(pages, "2026-09-23T05:00:00Z")
        self.assertEqual(r1["status"], "baseline")
        self.assertEqual(r1["last_counts"], {})

        pages[f"{b}/sitemap.xml"] = urlset((f"{b}/a", "2"), (f"{b}/c", "1"))
        pages[f"{b}/a"] = page("A new")
        pages[f"{b}/c"] = page("C")
        r2 = self.run_with(pages, "2026-09-24T05:00:00Z")
        self.assertEqual(r2["status"], "ok")
        self.assertEqual(r2["last_counts"], {"added": 1, "removed": 1, "updated": 1, "seo_changed": 1})
        log = scrape.read_json(scrape.DATA_DIR / "changes" / "c" / "ex.json", {})
        self.assertEqual(len(log["runs"]), 2)
        seo = [e for e in log["events"] if e["type"] == "seo_changed"][0]
        self.assertEqual(seo["after"]["title"], "A new")

    def test_blocked_first_run_writes_no_snapshot(self):
        r = self.run_with({"https://www.example.com/robots.txt": 403, "https://www.example.com/sitemap.xml": 403}, "2026-09-24T05:00:00Z")
        self.assertEqual(r["status"], "blocked")
        self.assertFalse((scrape.DATA_DIR / "snapshots" / "c" / "ex.json").exists())


class ConfigTests(unittest.TestCase):
    def write(self, text):
        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        self.addCleanup(os.remove, path)
        return Path(path)

    def test_repo_config_is_valid(self):
        self.assertTrue(scrape.load_config())

    def test_too_many_competitors(self):
        comps = "".join(f"      - name: C{i}\n        domain: c{i}.com\n" for i in range(6))
        with self.assertRaises(ValueError):
            scrape.load_config(self.write(f"clients:\n  - id: a\n    competitors:\n{comps}"))


if __name__ == "__main__":
    unittest.main()
