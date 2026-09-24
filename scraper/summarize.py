#!/usr/bin/env python3
"""Writes an AI brief per client from the last 7 days of competitor changes.

This step is optional. Without an ANTHROPIC_API_KEY it prints a message and
exits 0. A client's summary is only regenerated when there are new events.

Usage:
    python3 scraper/summarize.py [--client client-a] [--force]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict

from scrape import DATA_DIR, load_config, now_utc, parse_ts, read_json, write_json

MODEL = os.environ.get("SUMMARY_MODEL", "claude-opus-5")
WINDOW_DAYS = 7
MAX_SAMPLES = {"added": 15, "removed": 10, "updated": 8, "seo_changed": 12, "status_changed": 8}
MAX_SECTIONS = 12

SYSTEM_PROMPT = """You are a competitive-intelligence analyst at an SEO agency. \
You get structured data about changes detected on competitors' websites over the past week, \
based on their XML sitemaps and on-page SEO fields (title, meta description, H1, canonical).

Write a brief for the account team. Guidelines:
- Base every statement only on the data provided. Do not speculate about anything the data doesn't show.
- Look for strategy, not just activity: new product lines or categories, content pushes \
(for example many new /blog/ pages), site sections being pruned or consolidated, \
redirects that suggest migrations, and SEO rewrites such as title patterns changing across many pages.
- Quantify ("34 new pages under /running") and cite one or two example URL paths for each claim.
- If a competitor had little or no activity, set signal to "quiet" and give a single short bullet saying so.
- A scrape status of "blocked", "error" or "partial" means the data is incomplete. Say that briefly rather than reading meaning into the missing data.
- Use plain, direct language. No marketing fluff."""

SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "One sentence, at most 16 words, capturing the most important move this week."},
        "overall": {"type": "string", "description": "2-4 sentences synthesizing activity across all competitors."},
        "competitors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "signal": {"type": "string", "enum": ["quiet", "active", "major"]},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["slug", "signal", "bullets"],
                "additionalProperties": False,
            },
        },
        "watch_next": {"type": "string", "description": "One suggestion of what the account team should watch or act on."},
    },
    "required": ["headline", "overall", "competitors", "watch_next"],
    "additionalProperties": False,
}


def path_of(url: str) -> str:
    from urllib.parse import urlsplit
    p = urlsplit(url)
    return p.path + (("?" + p.query) if p.query else "")


def build_payload(client: dict, index_client: dict, since: dt.datetime):
    status_by_slug = {c["slug"]: c for c in (index_client or {}).get("competitors", [])}
    payload = {"client": client["name"], "window_days": WINDOW_DAYS, "competitors": []}
    all_events = []
    for comp in client["competitors"]:
        log = read_json(DATA_DIR / "changes" / client["id"] / f"{comp['slug']}.json", {"events": [], "runs": []})
        events = [e for e in log.get("events", []) if parse_ts(e["date"]) >= since]
        all_events.extend(events)
        # Exact counts come from run records, since the event log is capped per run.
        counts = Counter()
        for run in log.get("runs", []):
            if parse_ts(run["date"]) >= since:
                counts.update(run.get("counts", {}))
        sections = defaultdict(Counter)
        for e in events:
            sections[e.get("section", "/")][e["type"]] += 1
        top_sections = sorted(sections.items(), key=lambda kv: -sum(kv[1].values()))[:MAX_SECTIONS]
        samples = defaultdict(list)
        for e in events:
            t = e["type"]
            if len(samples[t]) >= MAX_SAMPLES.get(t, 5):
                continue
            item = {"path": path_of(e["url"])}
            if t in ("seo_changed", "status_changed"):
                item["before"], item["after"] = e.get("before"), e.get("after")
            samples[t].append(item)
        st = status_by_slug.get(comp["slug"], {})
        payload["competitors"].append({
            "slug": comp["slug"],
            "name": comp["name"],
            "domain": comp["domain"],
            "scrape_status": st.get("status", "unknown"),
            "total_urls": st.get("total_urls"),
            "counts": dict(counts),
            "sections": [{"section": s, **dict(c)} for s, c in top_sections],
            "samples": dict(samples),
        })
    return payload, all_events


def signature(events: list) -> str:
    keys = sorted(f"{e['date']}|{e['type']}|{e['url']}" for e in events)
    return hashlib.sha1("\n".join(keys).encode()).hexdigest()[:16]


def call_claude(payload: dict) -> tuple[dict, str]:
    import anthropic

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{
            "role": "user",
            "content": "Here is this week's competitor change data as JSON:\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=1),
        }],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("model declined the request")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise RuntimeError(f"no text in response (stop_reason={response.stop_reason})")
    return json.loads(text), response.model


def validate(summary: dict, slugs: list[str]) -> dict:
    out = {
        "headline": str(summary.get("headline", "")).strip(),
        "overall": str(summary.get("overall", "")).strip(),
        "watch_next": str(summary.get("watch_next", "")).strip(),
        "competitors": {},
    }
    for c in summary.get("competitors", []):
        slug = c.get("slug")
        if slug not in slugs:
            continue
        signal = c.get("signal") if c.get("signal") in ("quiet", "active", "major") else "quiet"
        bullets = [str(b).strip() for b in c.get("bullets", []) if str(b).strip()][:6]
        out["competitors"][slug] = {"signal": signal, "bullets": bullets}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--client")
    ap.add_argument("--force", action="store_true", help="Regenerate even if nothing changed")
    args = ap.parse_args(argv)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set, so the AI summaries are skipped. Add the key as a repo secret to enable them.")
        return 0

    import anthropic

    index = read_json(DATA_DIR / "index.json", {})
    index_by_id = {c["id"]: c for c in index.get("clients", [])}
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=WINDOW_DAYS)
    failures = 0

    for client in load_config():
        if args.client and client["id"] != args.client:
            continue
        out_path = DATA_DIR / "summaries" / f"{client['id']}.json"
        existing = read_json(out_path, {})
        payload, events = build_payload(client, index_by_id.get(client["id"]), since)
        sig = signature(events)
        if not events and not existing:
            print(f"[{client['id']}] no events yet, so there's nothing to summarize")
            continue
        if not args.force and existing.get("source_signature") == sig:
            print(f"[{client['id']}] unchanged since last summary, so it was skipped")
            continue
        try:
            raw, served_by = call_claude(payload)
            summary = validate(raw, [c["slug"] for c in client["competitors"]])
        except (anthropic.APIError, RuntimeError, json.JSONDecodeError) as exc:
            failures += 1
            print(f"[{client['id']}] summary failed: {exc}", file=sys.stderr)
            continue
        write_json(out_path, {
            **summary,
            "generated_at": now_utc(),
            "model": served_by,
            "window": {"from": since.replace(microsecond=0).isoformat().replace("+00:00", "Z"), "to": now_utc(), "days": WINDOW_DAYS},
            "event_count": len(events),
            "source_signature": sig,
        })
        print(f"[{client['id']}] summary written ({len(events)} events)")

    # Summary failures shouldn't fail the workflow, since the scrape data is still worth committing.
    if failures:
        print(f"{failures} summaries failed. See the errors above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
