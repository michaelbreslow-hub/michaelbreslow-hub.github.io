# Competitor Watch

Tracks up to 5 competitors' sitemaps per client and shows new, removed and changed pages on a dashboard, plus an optional AI brief.

**Dashboard:** https://michaelbreslow-hub.github.io/scraper/

## How it works

GitHub Pages only serves static files, so the work happens in a GitHub Action:

1. `.github/workflows/scrape.yml` runs every day at 05:00 UTC. You can also run it by hand from the Actions tab.
2. `scrape.py` reads `clients.yaml`, finds each competitor's sitemaps (from `robots.txt`, falling back to `/sitemap.xml`) and compares the result with the last snapshot. It records:
   - **added / removed** URLs
   - **updated** URLs, where the sitemap `lastmod` changed
   - **SEO changes**: title, meta description, H1 and canonical, for new and changed pages plus a small rotating re-check
   - **HTTP status changes**, such as pages that start redirecting or return 404
3. `summarize.py` asks Claude for a weekly brief per client. It only does this when an API key is set and there are new changes.
4. The results are committed to `scraper/data/`, and `index.html` reads them.

The first scan of a competitor only captures a baseline. Changes start showing up from the second daily scan.

## Adding competitors

Edit `clients.yaml`:

```yaml
clients:
  - id: client-a          # lowercase, used in URLs
    name: Client A        # this repo is public, so use codenames
    competitors:
      - name: Nike
        domain: www.nike.com/il      # a path limits tracking to that section
      - name: Example
        domain: example.com
        sitemaps:                    # optional: skip discovery
          - https://example.com/sitemap-products.xml
```

You can track up to 5 competitors per client. Commit the change, then run the workflow or wait for the next daily scan.

**Tip:** many global sites (for example Asics or Hoka) publish every country in one sitemap. Add the country or language path to the domain, such as `www.asics.com/us/en-us` or `www.newbalance.co.il/en`. Otherwise the changes mix every locale, and bilingual sites log each change twice.

## Enabling the AI brief

Add the repo secret under **Settings → Secrets and variables → Actions → New repository secret**, named `ANTHROPIC_API_KEY`. It uses `claude-opus-5` by default. You can override that with a `SUMMARY_MODEL` env var in the workflow.

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r scraper/requirements.txt
.venv/bin/python -m unittest discover scraper/tests
.venv/bin/python scraper/scrape.py
python3 scraper/dev/seed_demo.py            # fake data for UI work (gitignored)
python3 -m http.server 8765                 # from the repo root
# open http://localhost:8765/scraper/?data=dev/demo/data
```

## Good to know

- **Public data.** The repo is public, so everything under `scraper/data/` and the AI briefs can be seen by anyone.
- **Blocked sites.** Some large sites block traffic from GitHub's servers. The dashboard shows these as "Blocked". The scanner uses an honest User-Agent, respects `robots.txt` for page fetches and doesn't try to get around blocks.
- **Safety guards.** If a sitemap fails or the URL count suddenly drops by more than 40%, removals are held back rather than logged, which avoids floods of fake "removed" events. A drop is only accepted once a second run confirms it.
- **Scheduled runs pausing.** GitHub pauses scheduled workflows after 60 days with no repo activity. The daily data commits normally prevent this.
