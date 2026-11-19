# static-site-seo-broken-link-checker-cli-lite

Zero-dependency Python 3.9+ CLI to audit static HTML site builds for broken relative/anchor links, missing images/alt text, duplicate titles/meta descriptions, and sitemap orphans.

---

## Features

- **Zero external dependencies**: Pure Python 3.9+ standard library (`html.parser`, `xml.etree.ElementTree`, `argparse`, `csv`, `json`, `pathlib`, `urllib.parse`). No `pip install` or virtual environment required.
- **Internal Link & `#anchor` Verification**:
  - Walks static HTML output folders (`dist/`, `_site/`, `public/`, `build/`, etc.) and resolves relative, root-relative (`/docs/`), `<base href>`, clean URL (`/about` -> `about.html` or `about/index.html`), and subpath `--base-url` links.
  - Verifies same-page (`#section`) and cross-page (`about.html#team`) fragment IDs against target `id` and legacy `<a name="...">` anchors.
  - Enforces **case-sensitive filename matching** even on Windows/macOS so casing bugs are caught before deploying to Linux hosts.
  - Flags directory-traversal links escaping the site root.
- **Local Asset & Image Auditing**:
  - Checks `<img>` (`src` & `srcset`), `<link rel="stylesheet|icon|manifest|preload">`, `<script src>`, `<source>`, and `<video poster>` against files on disk (ignoring cache-busting `?query` and SVG `#fragment` suffixes).
  - Detects missing `alt` attributes (`warning`) vs empty `alt=""` (`info`).
- **On-Page & Cross-Page SEO Checks**:
  - Validates `<title>`, `<meta name="description">`, `<h1>`, `<link rel="canonical">`, and `<meta name="viewport">`.
  - Detects site-wide **duplicate `<title>`** and **duplicate `<meta name="description">`** groups plus orphan HTML pages with zero incoming internal links.
- **`sitemap.xml` Cross-Check**:
  - Supports standard `<urlset>` and nested local `<sitemapindex>` XML files.
  - Flags broken `<loc>` URLs, duplicate `<loc>` entries, `noindex` pages listed in the sitemap, and indexable HTML pages omitted from `sitemap.xml` (`SITEMAP_ORPHAN`).
- **Scoring & Automation Outputs**:
  - Computes a deterministic **0–100 Health Score** and severity breakdown (`critical` / `warning` / `info`).
  - Supports `--format text|json|csv`, `-o <file>`, `--severity`, `--fail-on none|critical|warning`, and `--min-score`.

---

## Quick Start

```bash
# Audit a static site directory (e.g., dist/ or _site/)
python seo_checker.py ./dist

# Fail CI on any critical issue (broken link, broken anchor, missing asset, missing title)
python seo_checker.py ./dist --fail-on critical

# Strict CI audit with GitHub Pages subpath and minimum health score
python seo_checker.py ./public --base-url https://example.github.io/docs --fail-on warning --min-score 90

# Export machine-readable JSON or CSV reports
python seo_checker.py ./dist --format json -o seo-report.json
python seo_checker.py ./dist --format csv -o seo-issues.csv
```

Example terminal output:

```text
Static Site SEO & Link Audit | Score: 74/100 (FAIL) | Pages: 4 | Links: 12 | Assets: 6 | Critical: 2 | Warning: 2 | Info: 0
Site: C:/projects/my-site/dist
Sitemap: found (3 URLs)

Severity | Code                | Page        | Line | Target           | Message
---------+---------------------+-------------+------+------------------+---------------------------------------------------------------------------
CRITICAL | BROKEN_LINK         | index.html  | 18   | pricing.html     | Broken internal link 'pricing.html': Target 'pricing.html' not found in site directory.
CRITICAL | BROKEN_ANCHOR       | docs.html   | 42   | #nonexistent     | Broken same-page anchor '#nonexistent' (no matching id or name found).
WARNING  | MISSING_IMAGE_ALT   | index.html  | 24   | img/hero.png     | Image 'img/hero.png' is missing an alt attribute.
WARNING  | SITEMAP_ORPHAN      | landing.html| -    | sitemap.xml      | HTML page 'landing.html' is missing from sitemap.xml.
```

---

## CI/CD Integration Examples

### GitHub Actions

```yaml
name: Static Site SEO & Link Audit

on:
  push:
    branches: [main]
  pull_request:

jobs:
  seo-audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build static site
        run: npm run build
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Run SEO & Broken Link Audit
        run: |
          python seo_checker.py ./dist --fail-on critical --min-score 85 --format text
          python seo_checker.py ./dist --format json -o seo-report.json
      - name: Upload SEO Report Artifact
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: seo-audit-report
          path: seo-report.json
```

### GitLab CI

```yaml
seo_link_audit:
  stage: test
  image: python:3.11-slim
  script:
    - python seo_checker.py ./public --fail-on warning --format csv -o seo-issues.csv
  artifacts:
    when: always
    paths:
      - seo-issues.csv
```

---

## Health Score & Audit Rules

Health Score starts at `100` and deducts deterministic penalties clamped to `0..100`:

```text
health_score = max(0, min(100, 100 - (critical * 10 + warning * 3 + min(15, info))))
```

| Severity | Issue Codes |
| :--- | :--- |
| `critical` | `BROKEN_LINK`, `BROKEN_ANCHOR`, `BROKEN_ASSET`, `BROKEN_SITEMAP_URL`, `INVALID_SITEMAP`, `MISSING_TITLE`, `EMPTY_TITLE` |
| `warning` | `MISSING_META_DESCRIPTION`, `EMPTY_META_DESCRIPTION`, `DUPLICATE_TITLE`, `DUPLICATE_META_DESCRIPTION`, `MISSING_H1`, `EMPTY_H1`, `MULTIPLE_H1`, `MISSING_CANONICAL`, `EMPTY_CANONICAL`, `BROKEN_CANONICAL`, `MULTIPLE_CANONICALS`, `MISSING_VIEWPORT`, `MULTIPLE_TITLES`, `MULTIPLE_META_DESCRIPTIONS`, `MISSING_IMAGE_ALT`, `EMPTY_LINK`, `MISSING_SITEMAP`, `SITEMAP_ORPHAN`, `NOINDEX_IN_SITEMAP`, `DUPLICATE_SITEMAP_URL`, `SITEMAP_ORIGIN_MISMATCH` |
| `info` | `EMPTY_IMAGE_ALT`, `SHORT_TITLE`, `LONG_TITLE`, `SHORT_META_DESCRIPTION`, `LONG_META_DESCRIPTION`, `ORPHAN_PAGE`, `PLACEHOLDER_ANCHOR` |

---

## CLI Options & Exit Codes

| Flag | Default | Description |
| :--- | :--- | :--- |
| `site_dir` | `.` | Path to local static HTML build folder |
| `--base-url` | `—` | Canonical/sitemap base URL (e.g. `https://example.com/docs`) |
| `--sitemap` | `<site_dir>/sitemap.xml` | Custom path to sitemap XML file |
| `--no-sitemap` | `false` | Skip `sitemap.xml` validation and orphan checks |
| `--no-clean-urls` | `false` | Require exact `.html` extensions instead of clean URL fallback |
| `--exclude` | `[]` | Repeatable directory or filename glob pattern to exclude |
| `--severity` | `all` | Display filter (`all`, `critical`, `warning`, `info`) |
| `--format` | `text` | Report output format (`text`, `json`, `csv`) |
| `-o`, `--output` | stdout | Write report to file path |
| `--fail-on` | `none` | Fail CI (`exit 1`) on `none`, `critical`, or `warning` |
| `--min-score` | `—` | Fail CI (`exit 1`) if health score is below threshold (`0..100`) |
| `--version` | — | Show CLI version |

**Exit Codes**: `0` = audit completed within policy, `1` = policy failed (`--fail-on` or `--min-score`), `2` = invalid arguments or unreadable site/sitemap path.

---

## Lite CLI vs Full Local SEO Audit Studio Pro

This open-source Lite CLI is ideal for terminal checks and CI pipelines. If you run client SEO audits, manage large static/live websites, or deliver branded audit PDFs, compare both editions below:

| Capability | Lite CLI (Free MIT) | Full Local SEO Sitemap, Meta & Broken-Link Audit Studio + Web UI & A4 Client Reports Pro |
| :--- | :--- | :--- |
| Offline static folder HTML, anchor & asset check | Yes | Yes (static builds + live HTTP/HTTPS crawler) |
| Title, meta description, H1, canonical & viewport check | Yes | Yes + OpenGraph, Twitter Cards, robots.txt & hreflang |
| `sitemap.xml` validation & orphan detection | Yes | Yes + automated clean `sitemap.xml` generator |
| Health score & CLI `text` / `json` / `csv` exports | Yes | Yes + interactive Web UI dashboard & project history |
| Printable A4 branded client audit reports (HTML/PDF) | — | Yes (white-label executive summary & fix checklist) |
| External link status & redirect chain checker | — | Yes (concurrent HTTP status, 301/302 loops, SSL expiry) |
| Visual link graph & issue diffing across snapshots | — | Yes |

- **Get the Pro Edition on FunPay:** https://funpay.com/lots/offer?id=82603939

---

## Running Tests

```bash
py -m unittest discover -s tests -v
```

---

## License

MIT License — see [LICENSE](LICENSE).
