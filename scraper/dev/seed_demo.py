#!/usr/bin/env python3
"""Writes realistic fake data for local UI testing. Never commit its output.

    python3 scraper/dev/seed_demo.py            # writes scraper/dev/demo/data
    python3 -m http.server -d .                 # from the repo root
    open http://localhost:8000/scraper/?data=dev/demo/data
"""
import datetime as dt
import json
import random
import shutil
from pathlib import Path

OUT = Path(__file__).resolve().parent / "demo" / "data"
random.seed(7)
now = dt.datetime.now(dt.timezone.utc).replace(hour=5, minute=4, second=0, microsecond=0)
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")

COMPETITORS = [
    ("nike", "Nike", "www.nike.com/il", ["/w", "/t", "/a", "/launch", "/retail", "/running"], 14118, "ok"),
    ("adidas", "Adidas", "www.adidas.co.il", ["/he/men", "/he/women", "/he/blog", "/he/running", "/he/outlet"], 8203, "ok"),
    ("puma", "Puma", "il.puma.com", ["/shoes", "/sale", "/collections", "/stories"], 3120, "partial"),
    ("new-balance", "New Balance", "www.newbalance.co.il", ["/products", "/pages"], 1876, "blocked"),
]
SLUGS = ["pegasus-41", "air-max-dn", "vomero-18", "invincible-3", "journey-run", "structure-26",
         "trail-ultrafly", "zoom-fly-6", "alphafly-3", "cortez-leather", "dunk-low-retro", "tech-fleece-hoodie"]
TITLES = ["Running Shoes", "Men's Road Running Shoes", "Trail Shoes", "Winter Collection", "Fleece Hoodies",
          "Marathon Training Guide", "Sale up to 40%", "Black Friday Deals"]


def url(domain, section):
    host, _, prefix = domain.partition("/")
    prefix = "/" + prefix if prefix else ""
    return f"https://{host}{prefix}{section}/{random.choice(SLUGS)}-{random.randint(100, 999)}"


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    index_comps = []
    all_events = {}
    for slug, name, domain, sections, total, status in COMPETITORS:
        events, runs, total_now = [], [], total
        for back in range(45, -1, -1):
            day = now - dt.timedelta(days=back)
            counts = {}
            burst = 1 if random.random() > 0.15 else 0
            if slug == "adidas" and 8 <= back <= 10:
                burst = 6  # a content push
            plan = {
                "added": random.randint(0, 6) * burst,
                "removed": random.randint(0, 3) * burst,
                "updated": random.randint(0, 9) * burst,
                "seo_changed": random.randint(0, 2) * burst,
                "status_changed": 1 if random.random() < 0.12 else 0,
            }
            if back == 45:
                plan = {}
            for t, n in plan.items():
                for _ in range(n):
                    sec = "/he/blog" if slug == "adidas" and burst == 6 and t == "added" else random.choice(sections)
                    u = url(domain, sec)
                    ev = {"date": iso(day), "type": t, "url": u, "section": "/" + sec.strip("/").split("/")[-1] if slug == "adidas" else sec}
                    if t == "updated":
                        ev.update(before=(day - dt.timedelta(days=30)).date().isoformat(), after=day.date().isoformat())
                    elif t == "seo_changed":
                        old = random.choice(TITLES)
                        ev.update(fields=["title", "description"],
                                  before={"title": f"{old} | {name}", "description": f"Shop the latest {old.lower()} from {name}."},
                                  after={"title": f"{old} 2026 | Free Delivery | {name}", "description": f"Discover new {old.lower()} with free delivery and returns at {name}."})
                    elif t == "status_changed":
                        ev.update(before={"status": 200, "redirect": None}, after={"status": 301, "redirect": u.rsplit("-", 1)[0]} if random.random() < .6 else {"status": 404, "redirect": None})
                    elif t in ("added", "removed"):
                        ev["lastmod"] = day.date().isoformat()
                    events.append(ev)
                    counts[t] = counts.get(t, 0) + 1
            total_now += counts.get("added", 0) - counts.get("removed", 0)
            runs.insert(0, {"date": iso(day), "status": "baseline" if back == 45 else ("ok" if status != "blocked" or back > 3 else "blocked"), "total_urls": total_now, "counts": counts})
        events.sort(key=lambda e: e["date"], reverse=True)
        write(OUT / "changes" / "client-a" / f"{slug}.json", {"competitor": {"name": name, "slug": slug, "domain": domain}, "events": events, "runs": runs})
        all_events[slug] = events
        notes = {
            "partial": ["Some sitemaps failed or were truncated, so removals were skipped this run"],
            "blocked": ["The site refused our requests (HTTP 403). It may block automated traffic from GitHub's servers."],
        }.get(status, [])
        index_comps.append({"name": name, "slug": slug, "domain": domain, "status": status, "notes": notes, "total_urls": total_now,
                            "last_checked": iso(now), "baseline_at": iso(now - dt.timedelta(days=45)), "last_counts": runs[0]["counts"]})

    # Second client with a fresh baseline and no changes yet.
    write(OUT / "changes" / "client-b" / "zara.json", {"events": [], "runs": [{"date": iso(now), "status": "baseline", "total_urls": 5120, "counts": {}}]})
    write(OUT / "index.json", {
        "generated_at": iso(now),
        "repo": "michaelbreslow-hub/michaelbreslow-hub.github.io",
        "clients": [
            {"id": "client-a", "name": "Client A", "competitors": index_comps},
            {"id": "client-b", "name": "Client B", "competitors": [{"name": "Zara", "slug": "zara", "domain": "www.zara.com/il", "status": "baseline", "notes": [], "total_urls": 5120, "last_checked": iso(now), "baseline_at": iso(now)}]},
        ],
    })
    write(OUT / "summaries" / "client-a.json", {
        "headline": "Adidas launched a 40-page running content hub while Nike quietly rewrote product titles.",
        "overall": "Adidas was the most active competitor this week. Its /blog section grew by roughly 40 pages in three days, almost all running-training guides, which points to a deliberate organic push ahead of marathon season. Nike's sitemap stayed steady, but it rewrote titles on several product pages to add \"Free Delivery\". Puma's data is incomplete because some sitemaps failed, and New Balance blocked the scanner.",
        "competitors": {
            "nike": {"signal": "active", "bullets": ["Rewrote titles on 6 product pages to add \"2026 | Free Delivery\" (e.g. /t/pegasus-41-412).", "Added 18 pages, mostly under /w and /launch.", "Retired 5 pages under /retail, which fits store-page consolidation."]},
            "adidas": {"signal": "major", "bullets": ["Published about 40 new pages under /blog in a 3-day burst, all running and training guides.", "That pattern suggests a coordinated content push targeting running queries.", "Minor product churn under /men and /women."]},
            "puma": {"signal": "quiet", "bullets": ["Limited activity. Some sitemaps failed this week, so removals may be missing."]},
            "new-balance": {"signal": "quiet", "bullets": ["No data. The site is blocking the scanner (HTTP 403)."]},
        },
        "watch_next": "Check whether Adidas's new /blog guides start ranking for the running terms we target, and consider a counter-piece.",
        "generated_at": iso(now + dt.timedelta(minutes=3)),
        "model": "claude-opus-5",
        "window": {"from": iso(now - dt.timedelta(days=7)), "to": iso(now), "days": 7},
        "event_count": sum(len([e for e in ev if e["date"] >= iso(now - dt.timedelta(days=7))]) for ev in all_events.values()),
        "source_signature": "demo",
    })
    print("Demo data written to", OUT)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
