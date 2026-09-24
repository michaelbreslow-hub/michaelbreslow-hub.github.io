#!/usr/bin/env python3
"""Competitor sitemap tracker.

Reads clients.yaml, fetches each competitor's sitemap(s), diffs against the
previous snapshot and writes JSON under data/ for the static dashboard.

Usage:
    python3 scraper/scrape.py                 # all clients
    python3 scraper/scrape.py --client client-a
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.robotparser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "clients.yaml"
DATA_DIR = Path(os.environ.get("SCRAPER_DATA_DIR", ROOT / "data"))

USER_AGENT = (
    "WadiSitemapTracker/1.0 (+https://michaelbreslow-hub.github.io/scraper/; "
    "competitive sitemap monitoring)"
)
MAX_COMPETITORS = 5
MAX_URLS = 50_000
MAX_SITEMAP_FILES = 300
PAGE_BUDGET_CHANGED = 30   # new/updated pages fetched per competitor per run
PAGE_BUDGET_RECHECK = 10   # oldest-checked pages re-verified per run
PAGE_DELAY_S = 1.0
REQUEST_TIMEOUT_S = 20
RETRIES = 2
LOG_RETENTION_DAYS = 90
MAX_EVENTS_PER_TYPE_PER_RUN = 2000
MAX_RUNS_KEPT = 120
DROP_GUARD_RATIO = 0.40
MAX_HTML_BYTES = 1_500_000

TRACKING_PARAMS = re.compile(
    r"^(utm_.*|gclid|fbclid|msclkid|mc_cid|mc_eid|_ga|_gl|ref|srsltid)$", re.I
)
BLOCK_STATUSES = {401, 403, 429, 503}

_print_lock = threading.Lock()


def log(*parts):
    with _print_lock:
        print(*parts, flush=True)


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_ts(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


# --------------------------------------------------------------------------- config

def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "site"


def parse_domain(domain: str):
    """'www.nike.com/il' -> ('https', 'www.nike.com', '/il')."""
    raw = domain.strip()
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if not parts.hostname or "." not in parts.hostname:
        raise ValueError(f"Invalid domain: {domain!r}")
    prefix = parts.path.rstrip("/")
    return parts.scheme or "https", parts.hostname.lower(), prefix


def load_config(path: Path = CONFIG_PATH) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    clients = cfg.get("clients") or []
    seen_clients = set()
    out = []
    for c in clients:
        cid = c.get("id")
        if not cid or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", cid):
            raise ValueError(f"Client id must be lowercase letters, digits and dashes: {cid!r}")
        if cid in seen_clients:
            raise ValueError(f"Duplicate client id: {cid}")
        seen_clients.add(cid)
        comps = c.get("competitors") or []
        if len(comps) > MAX_COMPETITORS:
            raise ValueError(f"{cid}: at most {MAX_COMPETITORS} competitors allowed, found {len(comps)}")
        seen_slugs = set()
        parsed = []
        for comp in comps:
            name = comp.get("name") or comp.get("domain")
            scheme, host, prefix = parse_domain(comp["domain"])
            slug = comp.get("slug") or slugify(name)
            if slug in seen_slugs:
                raise ValueError(f"{cid}: duplicate competitor slug {slug!r}")
            seen_slugs.add(slug)
            parsed.append({
                "name": name,
                "slug": slug,
                "domain": comp["domain"],
                "scheme": scheme,
                "host": host,
                "prefix": prefix,
                "sitemaps": comp.get("sitemaps") or [],
            })
        out.append({"id": cid, "name": c.get("name") or cid, "competitors": parsed})
    return out


# --------------------------------------------------------------------------- urls

def normalize_url(url: str) -> str | None:
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    host = p.hostname.lower()
    if p.port and p.port not in (80, 443):
        host = f"{host}:{p.port}"
    path = p.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)]
    return urlunsplit((p.scheme.lower(), host, path, urlencode(sorted(query)), ""))


def _bare_host(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def in_scope(url: str, host: str, prefix: str) -> bool:
    p = urlsplit(url)
    if _bare_host((p.hostname or "").lower()) != _bare_host(host):
        return False
    if not prefix:
        return True
    path = p.path or "/"
    return path == prefix or path.startswith(prefix + "/")


def section_of(url: str, prefix: str) -> str:
    path = urlsplit(url).path or "/"
    if prefix and path.startswith(prefix):
        path = path[len(prefix):] or "/"
    seg = [s for s in path.split("/") if s]
    return "/" + seg[0] if seg else "/"


# --------------------------------------------------------------------------- http

class FetchError(Exception):
    def __init__(self, url, status=None, reason=""):
        self.url, self.status, self.reason = url, status, reason
        super().__init__(f"{url}: {status or ''} {reason}".strip())


class Fetcher:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xml,text/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        })

    def get(self, url: str, max_bytes: int | None = None, allow_status=()) -> requests.Response:
        last = None
        for attempt in range(RETRIES + 1):
            try:
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT_S, allow_redirects=True, stream=max_bytes is not None)
                if max_bytes is not None:
                    chunks, size = [], 0
                    for chunk in resp.iter_content(65536):
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= max_bytes:
                            break
                    resp._content = b"".join(chunks)
                    resp.close()
                if resp.status_code == 200 or resp.status_code in allow_status:
                    return resp
                last = FetchError(url, resp.status_code)
                if resp.status_code not in (429, 500, 502, 503, 504):
                    break
            except requests.RequestException as exc:
                last = FetchError(url, None, type(exc).__name__)
            if attempt < RETRIES:
                time.sleep(2 ** attempt)
        raise last


# --------------------------------------------------------------------------- sitemaps

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(content: bytes):
    """Returns (kind, [(loc, lastmod)]) where kind is 'index' or 'urlset'."""
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    root = ET.fromstring(content.lstrip())
    kind = "index" if _local(root.tag) == "sitemapindex" else "urlset"
    entries = []
    for node in root:
        loc = lastmod = None
        for child in node:
            name = _local(child.tag)
            if name == "loc" and child.text:
                loc = child.text.strip()
            elif name == "lastmod" and child.text:
                lastmod = child.text.strip()
        if loc:
            entries.append((loc, lastmod))
    return kind, entries


def _locale_token(prefix: str) -> str | None:
    last = [s for s in prefix.split("/") if s]
    return last[-1].lower() if last else None


def prioritize_children(children: list[str], prefix: str) -> list[str]:
    """For a path-scoped competitor (e.g. /il), prefer child sitemaps whose URL
    names that segment, so we don't download every country's sitemap."""
    token = _locale_token(prefix)
    if not token:
        return children
    pat = re.compile(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])")
    matching = [c for c in children if pat.search(urlsplit(c).path.lower())]
    return matching or children


def discover_sitemaps(fetcher: Fetcher, comp: dict) -> tuple[list[str], dict]:
    base = f"{comp['scheme']}://{comp['host']}"
    info = {"robots_status": None, "guesses": set()}
    if comp["sitemaps"]:
        return [urljoin(base + "/", s) for s in comp["sitemaps"]], info
    found = []
    try:
        resp = fetcher.get(base + "/robots.txt")
        info["robots_status"] = 200
        info["robots_txt"] = resp.text
        for line in resp.text.splitlines():
            if line.lower().startswith("sitemap:"):
                found.append(line.split(":", 1)[1].strip())
    except FetchError as exc:
        info["robots_status"] = exc.status
    if not found:
        candidates = []
        if comp["prefix"]:
            candidates.append(f"{base}{comp['prefix']}/sitemap.xml")
        candidates += [f"{base}/sitemap.xml", f"{base}/sitemap_index.xml"]
        found = candidates
        info["guesses"] = set(candidates)  # a missing guess isn't an error
    found = [u for u in dict.fromkeys(found) if _bare_host((urlsplit(u).hostname or "").lower()) == _bare_host(comp["host"])]
    return prioritize_children(found, comp["prefix"]), info


def collect_urls(fetcher: Fetcher, comp: dict):
    """Crawl sitemaps. Returns (urls {url: lastmod}, meta)."""
    roots, info = discover_sitemaps(fetcher, comp)
    urls: dict[str, str | None] = {}
    queue = list(roots)
    seen = set()
    errors = []
    fetched = 0
    ok_any = False
    truncated = False
    while queue:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        if fetched >= MAX_SITEMAP_FILES:
            truncated = True
            break
        fetched += 1
        try:
            resp = fetcher.get(sm)
            kind, entries = parse_sitemap(resp.content)
            ok_any = True
        except FetchError as exc:
            if sm in info["guesses"] and exc.status in (404, 410):
                continue
            errors.append({"sitemap": sm, "status": exc.status, "reason": exc.reason})
            continue
        except (ET.ParseError, OSError, EOFError) as exc:
            errors.append({"sitemap": sm, "status": None, "reason": f"parse error: {exc}"[:200]})
            continue
        if kind == "index":
            queue.extend(prioritize_children([loc for loc, _ in entries], comp["prefix"]))
            continue
        for loc, lastmod in entries:
            norm = normalize_url(loc)
            if norm and in_scope(norm, comp["host"], comp["prefix"]):
                urls[norm] = lastmod
                if len(urls) >= MAX_URLS:
                    truncated = True
                    break
        if truncated:
            break
    meta = {
        "sitemaps_fetched": fetched,
        "sitemap_errors": errors,
        "robots_status": info.get("robots_status"),
        "complete": ok_any and not errors and not truncated,
        "truncated": truncated,
        "any_ok": ok_any,
    }
    return urls, meta, info.get("robots_txt")


# --------------------------------------------------------------------------- page seo

class SEOParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = self.description = self.h1 = self.canonical = None
        self._in = None
        self._buf = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title" and self.title is None:
            self._in, self._buf = "title", []
        elif tag == "h1" and self.h1 is None:
            self._in, self._buf = "h1", []
        elif tag == "meta" and a.get("name", "").lower() == "description" and self.description is None:
            self.description = a.get("content", "")
        elif tag == "link" and "canonical" in a.get("rel", "").lower().split() and self.canonical is None:
            self.canonical = a.get("href", "")

    def handle_endtag(self, tag):
        if self._in and tag == self._in:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            setattr(self, self._in, text[:300])
            self._in = None

    def handle_data(self, data):
        if self._in:
            self._buf.append(data)


def clean(value):
    if value is None:
        return None
    value = re.sub(r"\s+", " ", value).strip()
    return value[:300] or None


def extract_seo(html: str, base_url: str) -> dict:
    p = SEOParser()
    try:
        p.feed(html)
    except Exception:  # malformed markup: keep whatever was parsed
        pass
    canonical = clean(p.canonical)
    if canonical:
        canonical = normalize_url(urljoin(base_url, canonical)) or canonical
    return {"title": clean(p.title), "description": clean(p.description), "h1": clean(p.h1), "canonical": canonical}


def seo_hash(seo: dict) -> str:
    return hashlib.sha1(json.dumps(seo, sort_keys=True).encode()).hexdigest()[:12]


def fetch_page(fetcher: Fetcher, url: str):
    """Returns (http_info, seo | None). http_info is None when blocked/unknown."""
    try:
        resp = fetcher.get(url, max_bytes=MAX_HTML_BYTES, allow_status=(301, 302, 404, 410))
    except FetchError as exc:
        if exc.status in (404, 410):
            return {"status": exc.status, "redirect": None}, None
        return None, None
    final = normalize_url(resp.url) or resp.url
    http = {"status": resp.status_code, "redirect": final if final != url else None}
    if resp.status_code != 200 or "html" not in resp.headers.get("Content-Type", "html"):
        return http, None
    resp.encoding = resp.encoding or resp.apparent_encoding
    return http, extract_seo(resp.text, final)


# --------------------------------------------------------------------------- diffing

def diff_snapshot(prev_urls: dict, new_urls: dict, prev_meta: dict, fetch_meta: dict, today: str):
    """Pure diff. Returns (events, merged_urls, status, notes, pending_drop)."""
    events = []
    notes = []
    status = "ok"
    pending_drop = None

    if not fetch_meta.get("any_ok"):
        return [], prev_urls, "error", ["No sitemap could be fetched"], prev_meta.get("pending_drop")

    prev_count = len(prev_urls)
    new_count = len(new_urls)
    removal_allowed = fetch_meta.get("complete", False)
    if not removal_allowed:
        status = "partial"
        notes.append("Some sitemaps failed or were truncated, so removals were skipped this run")
    elif prev_count and new_count < prev_count * (1 - DROP_GUARD_RATIO):
        if prev_meta.get("pending_drop") and abs(prev_meta["pending_drop"] - new_count) <= max(5, new_count * 0.05):
            notes.append(f"URL count dropped {prev_count} → {new_count} two runs in a row, so the drop was accepted")
        else:
            removal_allowed = False
            status = "partial"
            pending_drop = new_count
            notes.append(f"URL count dropped sharply ({prev_count} → {new_count}). Removals are held until the next run confirms it")

    merged = {}
    for url, lastmod in new_urls.items():
        rec = dict(prev_urls.get(url) or {})
        if url not in prev_urls:
            rec = {"first_seen": today}
            events.append({"type": "added", "url": url, "lastmod": lastmod})
        elif lastmod and rec.get("lastmod") and lastmod != rec.get("lastmod"):
            events.append({"type": "updated", "url": url, "before": rec.get("lastmod"), "after": lastmod})
        rec["lastmod"] = lastmod
        merged[url] = rec

    for url, rec in prev_urls.items():
        if url in merged:
            continue
        if removal_allowed:
            events.append({"type": "removed", "url": url, "lastmod": rec.get("lastmod")})
        else:
            merged[url] = rec
    return events, merged, status, notes, pending_drop


def seo_event(url: str, before: dict, after: dict):
    changed = [k for k in ("title", "description", "h1", "canonical") if before.get(k) != after.get(k)]
    if not changed:
        return None
    return {
        "type": "seo_changed",
        "url": url,
        "fields": changed,
        "before": {k: before.get(k) for k in changed},
        "after": {k: after.get(k) for k in changed},
    }


def http_event(url: str, before: dict | None, after: dict):
    if not before or before == after:
        return None
    return {"type": "status_changed", "url": url, "before": before, "after": after}


def pick_pages(events: list, urls: dict) -> tuple[list[str], list[str]]:
    changed = [e["url"] for e in events if e["type"] in ("added", "updated") and e["url"] in urls]
    changed = list(dict.fromkeys(changed))[:PAGE_BUDGET_CHANGED]
    skip = set(changed)
    rest = [u for u in urls if u not in skip]
    rest.sort(key=lambda u: (urls[u].get("seo_checked") or "", u))
    return changed, rest[:PAGE_BUDGET_RECHECK]


# --------------------------------------------------------------------------- io

def read_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(path)


def trim_events(events: list, now: dt.datetime) -> list:
    cutoff = now - dt.timedelta(days=LOG_RETENTION_DAYS)
    return [e for e in events if parse_ts(e["date"]) >= cutoff]


# --------------------------------------------------------------------------- run

def process_competitor(client: dict, comp: dict, run_at: str) -> dict:
    snap_path = DATA_DIR / "snapshots" / client["id"] / f"{comp['slug']}.json"
    log_path = DATA_DIR / "changes" / client["id"] / f"{comp['slug']}.json"
    snap = read_json(snap_path, None)
    changes = read_json(log_path, {"events": [], "runs": []})
    prev_urls = (snap or {}).get("urls", {})
    prev_meta = (snap or {}).get("meta", {})
    fetcher = Fetcher()
    label = f"[{client['id']}/{comp['slug']}]"
    log(label, "fetching sitemaps…")

    result = {"status": "ok", "notes": [], "counts": {}}
    try:
        new_urls, fmeta, robots_txt = collect_urls(fetcher, comp)
    except Exception as exc:  # defensive: never let one site break the run
        new_urls, fmeta, robots_txt = {}, {"any_ok": False, "sitemap_errors": [{"reason": str(exc)[:200]}]}, None

    blocked = not fmeta.get("any_ok") and (
        fmeta.get("robots_status") in BLOCK_STATUSES
        or any(e.get("status") in BLOCK_STATUSES for e in fmeta.get("sitemap_errors", []))
    )

    if snap is None:
        events, merged, status, notes, pending = [], {u: {"lastmod": lm, "first_seen": run_at} for u, lm in new_urls.items()}, "baseline", [], None
        if not fmeta.get("any_ok"):
            status, notes = "error", ["No sitemap could be fetched"]
    else:
        events, merged, status, notes, pending = diff_snapshot(prev_urls, new_urls, prev_meta, fmeta, run_at)
    if blocked:
        status = "blocked"
        codes = sorted({e.get("status") for e in fmeta.get("sitemap_errors", []) if e.get("status")} | ({fmeta.get("robots_status")} - {None, 200}))
        notes = [f"The site refused our requests (HTTP {', '.join(map(str, codes)) or '403'}). It may block automated traffic from GitHub's servers."]

    # SEO basics for new/changed pages, plus a rotating re-check.
    if merged and status != "error" and status != "blocked":
        robots = urllib.robotparser.RobotFileParser()
        robots.parse((robots_txt or "").splitlines())
        changed, recheck = pick_pages(events, merged)
        pages_blocked = 0
        for url in changed + recheck:
            if robots_txt and not robots.can_fetch(USER_AGENT, url):
                merged[url]["seo_checked"] = run_at
                continue
            http, seo = fetch_page(fetcher, url)
            time.sleep(PAGE_DELAY_S)
            rec = merged[url]
            if http is None:
                pages_blocked += 1
                continue
            if snap is not None and rec.get("http"):
                ev = http_event(url, rec.get("http"), http)
                if ev:
                    events.append(ev)
            rec["http"] = http
            if seo is not None:
                h = seo_hash(seo)
                if rec.get("seo") and rec.get("seo_hash") != h and snap is not None:
                    ev = seo_event(url, rec["seo"], seo)
                    if ev:
                        events.append(ev)
                rec["seo"], rec["seo_hash"] = seo, h
            rec["seo_checked"] = run_at
        if pages_blocked and pages_blocked == len(changed + recheck):
            notes.append("Page fetches were blocked, so the SEO fields weren't updated")

    # Stamp, section, cap and persist.
    counts: dict[str, int] = {}
    kept, per_type = [], {}
    for ev in events:
        counts[ev["type"]] = counts.get(ev["type"], 0) + 1
        per_type[ev["type"]] = per_type.get(ev["type"], 0) + 1
        if per_type[ev["type"]] > MAX_EVENTS_PER_TYPE_PER_RUN:
            continue
        ev["date"] = run_at
        ev["section"] = section_of(ev["url"], comp["prefix"])
        kept.append(ev)
    if len(kept) < len(events):
        notes.append(f"Only the first {MAX_EVENTS_PER_TYPE_PER_RUN} events of each type were logged. The totals are still exact")

    now = parse_ts(run_at)
    changes["events"] = trim_events(kept + changes.get("events", []), now)
    runs = changes.get("runs", [])
    runs.insert(0, {"date": run_at, "status": status, "total_urls": len(merged), "counts": counts})
    changes["runs"] = runs[:MAX_RUNS_KEPT]
    changes["competitor"] = {"name": comp["name"], "slug": comp["slug"], "domain": comp["domain"]}

    # A failed run leaves the snapshot untouched, so a first run that fails
    # doesn't create an empty baseline that would later flag everything as new.
    if status not in ("error", "blocked"):
        write_json(snap_path, {
            "meta": {
                "baseline_at": prev_meta.get("baseline_at") or run_at,
                "last_run": run_at,
                "pending_drop": pending,
                "sitemaps_fetched": fmeta.get("sitemaps_fetched", 0),
            },
            "urls": merged,
        })
    write_json(log_path, changes)

    baseline_at = prev_meta.get("baseline_at") or (run_at if status == "baseline" else None)
    log(label, status, f"{len(merged)} urls", counts or "")
    return {
        "name": comp["name"],
        "slug": comp["slug"],
        "domain": comp["domain"],
        "status": status,
        "notes": notes,
        "total_urls": len(merged) if merged else len(prev_urls),
        "last_checked": run_at,
        "baseline_at": baseline_at,
        "sitemaps_fetched": fmeta.get("sitemaps_fetched", 0),
        "sitemap_errors": fmeta.get("sitemap_errors", [])[:10],
        "last_counts": counts,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--client", help="Only run this client id")
    args = ap.parse_args(argv)

    clients = load_config()
    run_at = now_utc()
    index_path = DATA_DIR / "index.json"
    prev_index = read_json(index_path, {})
    prev_by_client = {c["id"]: c for c in prev_index.get("clients", [])}

    jobs = []
    for client in clients:
        if args.client and client["id"] != args.client:
            continue
        for comp in client["competitors"]:
            jobs.append((client, comp))

    results: dict[tuple, dict] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(process_competitor, c, comp, run_at): (c["id"], comp["slug"]) for c, comp in jobs}
        for fut, key in futures.items():
            try:
                results[key] = fut.result()
            except Exception as exc:
                log(f"[{key[0]}/{key[1]}] crashed: {exc}")
                results[key] = {"status": "error", "notes": [f"Unexpected error: {str(exc)[:200]}"], "last_checked": run_at}

    index_clients = []
    for client in clients:
        prev_comps = {c["slug"]: c for c in prev_by_client.get(client["id"], {}).get("competitors", [])}
        comps = []
        for comp in client["competitors"]:
            key = (client["id"], comp["slug"])
            base = {"name": comp["name"], "slug": comp["slug"], "domain": comp["domain"]}
            if key in results:
                comps.append({**prev_comps.get(comp["slug"], {}), **base, **results[key]})
            elif comp["slug"] in prev_comps:
                comps.append({**prev_comps[comp["slug"]], **base})
            else:
                comps.append({**base, "status": "pending", "notes": [], "total_urls": 0})
        index_clients.append({"id": client["id"], "name": client["name"], "competitors": comps})

    write_json(index_path, {
        "generated_at": run_at,
        "repo": os.environ.get("GITHUB_REPOSITORY", "michaelbreslow-hub/michaelbreslow-hub.github.io"),
        "clients": index_clients,
    })
    log("done:", len(results), "competitors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
