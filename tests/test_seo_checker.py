import csv
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import seo_checker  # noqa: E402


def _make_html(
    *,
    title: str | None = "Sample Page Title For SEO Audit",
    description: str | None = (
        "A clear, well-structured meta description exceeding fifty characters for clean SEO audits."
    ),
    canonical: str | None = "https://example.com/",
    viewport: str | None = "width=device-width, initial-scale=1.0",
    robots: str | None = None,
    base_href: str | None = None,
    h1: list[str] | None = None,
    head_extra: str = "",
    body: str = "<p>Hello world.</p>",
) -> str:
    head_parts: list[str] = ['<meta charset="utf-8">']
    if base_href is not None:
        head_parts.append(f'<base href="{base_href}">')
    if title is not None:
        head_parts.append(f"<title>{title}</title>")
    if description is not None:
        head_parts.append(f'<meta name="description" content="{description}">')
    if viewport is not None:
        head_parts.append(f'<meta name="viewport" content="{viewport}">')
    if robots is not None:
        head_parts.append(f'<meta name="robots" content="{robots}">')
    if canonical is not None:
        head_parts.append(f'<link rel="canonical" href="{canonical}">')
    if head_extra:
        head_parts.append(head_extra)

    h1_list = ["Main Heading"] if h1 is None else h1
    h1_html = "\n".join(f"<h1>{text}</h1>" for text in h1_list)

    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        + "\n".join(head_parts)
        + "\n</head>\n"
        "<body>\n"
        + h1_html
        + "\n"
        + body
        + "\n</body>\n</html>\n"
    )


def _write_files(root: Path, mapping: dict[str, str | bytes]) -> None:
    for rel, content in mapping.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")


class TestSeoChecker(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.site_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _build_healthy_site(self) -> None:
        _write_files(
            self.site_dir,
            {
                "assets/app.css": "body { color: #111; }",
                "assets/app.js": "console.log('ok');",
                "assets/logo.png": b"\x89PNG\r\n\x1a\n",
                "index.html": _make_html(
                    title="Home Page — Static SEO Checker Demo",
                    description="Primary landing page with valid links, images, metadata, and structured sitemap entries.",
                    canonical="https://example.com/index.html",
                    head_extra='<link rel="stylesheet" href="assets/app.css?v=1">',
                    body=(
                        '<nav><a href="about.html#team">Team</a> | '
                        '<a href="docs/guide.html">Guide</a> | '
                        '<a href="#features">Features</a></nav>\n'
                        '<section id="features"><img src="assets/logo.png" alt="Brand Logo"></section>\n'
                        '<script src="assets/app.js"></script>'
                    ),
                ),
                "about.html": _make_html(
                    title="About Us — Static SEO Checker Demo",
                    description="About page describing the engineering team, mission, and verified internal navigation links.",
                    canonical="https://example.com/about.html",
                    head_extra='<link rel="stylesheet" href="assets/app.css">',
                    body='<a href="index.html">Back</a><h2 id="team">Our Team</h2>',
                ),
                "docs/guide.html": _make_html(
                    title="User Guide — Static SEO Checker Demo",
                    description="Documentation guide inside a nested directory verifying relative asset and parent navigation.",
                    canonical="https://example.com/docs/guide.html",
                    head_extra='<link rel="stylesheet" href="../assets/app.css">',
                    body='<a href="../index.html">Home</a><a href="../about.html">About</a>',
                ),
                "sitemap.xml": (
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                    "  <url><loc>https://example.com/</loc></url>\n"
                    "  <url><loc>https://example.com/about.html</loc></url>\n"
                    "  <url><loc>https://example.com/docs/guide.html</loc></url>\n"
                    "</urlset>\n"
                ),
            },
        )

    def test_01_healthy_site_scores_100(self) -> None:
        self._build_healthy_site()
        res = seo_checker.audit_site(self.site_dir)
        self.assertEqual(res.summary.pages_scanned, 3)
        self.assertEqual(res.summary.total_issues, 0)
        self.assertEqual(res.summary.health_score, 100)
        self.assertTrue(res.summary.sitemap_found)
        self.assertEqual(res.summary.sitemap_urls, 3)
        self.assertTrue(res.summary.policy_passed)

    def test_02_broken_relative_internal_links(self) -> None:
        _write_files(
            self.site_dir,
            {
                "index.html": _make_html(
                    body='<a href="missing.html">Missing</a>\n<a href="sub/nope.html">Sub Nope</a>'
                ),
                "sitemap.xml": "<urlset><url><loc>/index.html</loc></url></urlset>",
            },
        )
        res = seo_checker.audit_site(self.site_dir)
        broken = [i for i in res.issues if i.code == "BROKEN_LINK"]
        self.assertEqual(len(broken), 2)
        self.assertTrue(all(i.severity == "critical" for i in broken))
        self.assertEqual({i.target for i in broken}, {"missing.html", "sub/nope.html"})
        self.assertTrue(all(i.line is not None for i in broken))

    def test_03_path_escaping_site_root_is_flagged(self) -> None:
        _write_files(
            self.site_dir,
            {
                "index.html": _make_html(body='<a href="../../secret.html">Escape</a>'),
            },
        )
        res = seo_checker.audit_site(self.site_dir, check_sitemap=False)
        broken = [i for i in res.issues if i.code == "BROKEN_LINK"]
        self.assertEqual(len(broken), 1)
        self.assertIn("escapes", broken[0].message)

    def test_04_same_page_and_cross_page_anchor_validation(self) -> None:
        _write_files(
            self.site_dir,
            {
                "index.html": _make_html(
                    body=(
                        '<a href="#valid-sec">OK</a>'
                        '<a href="#legacy-anchor">Legacy OK</a>'
                        '<a href="#sec%20encoded">Encoded OK</a>'
                        '<a href="#missing-id">Bad Local</a>'
                        '<a href="about.html#bad-remote">Bad Remote</a>'
                        '<a href="about.html#team">Good Remote</a>'
                        '<div id="valid-sec"></div>'
                        '<a name="legacy-anchor"></a>'
                        '<div id="sec encoded"></div>'
                    )
                ),
                "about.html": _make_html(
                    title="About Page — Anchor Verification Demo",
                    description="Secondary page with a valid #team section ID and home link for anchor testing.",
                    body='<a href="index.html">Home</a><section id="team">Team</section>',
                ),
            },
        )
        res = seo_checker.audit_site(self.site_dir, check_sitemap=False)
        broken_anchors = [i for i in res.issues if i.code == "BROKEN_ANCHOR"]
        self.assertEqual(len(broken_anchors), 2)
        self.assertEqual({i.target for i in broken_anchors}, {"#missing-id", "about.html#bad-remote"})
        self.assertTrue(all(i.severity == "critical" for i in broken_anchors))

    def test_05_clean_urls_and_no_clean_urls_mode(self) -> None:
        _write_files(
            self.site_dir,
            {
                "index.html": _make_html(
                    body='<a href="/about">About</a><a href="/docs/">Docs</a><a href="/docs">Docs2</a>'
                ),
                "about.html": _make_html(
                    title="About Clean URLs Page Title",
                    description="Target page resolved via extensionless /about clean URL rule in static builds.",
                    body='<a href="/">Home</a>',
                ),
                "docs/index.html": _make_html(
                    title="Docs Index Clean URLs Page Title",
                    description="Nested docs/index.html resolved via /docs and /docs/ clean URL routing.",
                    body='<a href="/">Home</a>',
                ),
            },
        )
        res_clean = seo_checker.audit_site(self.site_dir, check_sitemap=False, clean_urls=True)
        self.assertEqual([i for i in res_clean.issues if i.code == "BROKEN_LINK"], [])

        res_strict = seo_checker.audit_site(self.site_dir, check_sitemap=False, clean_urls=False)
        broken_strict = [i for i in res_strict.issues if i.code == "BROKEN_LINK"]
        self.assertEqual([i.target for i in broken_strict], ["/about"])

    def test_06_case_sensitive_filename_mismatch_detected(self) -> None:
        _write_files(
            self.site_dir,
            {
                "index.html": _make_html(body='<a href="About.html">Case Mismatch</a>'),
                "about.html": _make_html(
                    title="Lowercase About Filename On Disk",
                    description="Target file exists strictly as lowercase about.html, catching Linux deploy mismatches.",
                    body='<a href="index.html">Home</a>',
                ),
            },
        )
        res = seo_checker.audit_site(self.site_dir, check_sitemap=False)
        broken = [i for i in res.issues if i.code == "BROKEN_LINK"]
        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0].target, "About.html")

    def test_07_broken_assets_and_cache_busting_queries(self) -> None:
        _write_files(
            self.site_dir,
            {
                "css/site.css": "h1 {}",
                "img/sprite.svg": "<svg></svg>",
                "index.html": _make_html(
                    head_extra=(
                        '<link rel="stylesheet" href="css/site.css?v=123">'
                        '<link rel="stylesheet" href="css/missing.css">'
                    ),
                    body=(
                        '<img src="img/sprite.svg#icon" alt="Icon">'
                        '<img src="img/missing.png" alt="Missing">'
                        '<script src="js/missing.js"></script>'
                    ),
                ),
            },
        )
        res = seo_checker.audit_site(self.site_dir, check_sitemap=False)
        broken_assets = [i for i in res.issues if i.code == "BROKEN_ASSET"]
        self.assertEqual(len(broken_assets), 3)
        self.assertEqual(
            {i.target for i in broken_assets},
            {"css/missing.css", "img/missing.png", "js/missing.js"},
        )

    def test_08_srcset_and_video_poster_checks(self) -> None:
        _write_files(
            self.site_dir,
            {
                "img/hero-1x.jpg": b"jpg",
                "index.html": _make_html(
                    body=(
                        '<img src="img/hero-1x.jpg" srcset="img/hero-1x.jpg 1x, img/hero-2x.jpg 2x" alt="Hero">'
                        '<video poster="img/poster-missing.jpg"></video>'
                    )
                ),
            },
        )
        res = seo_checker.audit_site(self.site_dir, check_sitemap=False)
        broken_assets = [i for i in res.issues if i.code == "BROKEN_ASSET"]
        self.assertEqual(
            {i.target for i in broken_assets},
            {"img/hero-2x.jpg", "img/poster-missing.jpg"},
        )

    def test_09_image_missing_alt_vs_empty_alt(self) -> None:
        _write_files(
            self.site_dir,
            {
                "a.png": b"png",
                "b.png": b"png",
                "c.png": b"png",
                "index.html": _make_html(
                    body=(
                        '<img src="a.png">'
                        '<img src="b.png" alt="">'
                        '<img src="c.png" alt="Valid diagram">'
                    )
                ),
            },
        )
        res = seo_checker.audit_site(self.site_dir, check_sitemap=False)
        missing_alt = [i for i in res.issues if i.code == "MISSING_IMAGE_ALT"]
        empty_alt = [i for i in res.issues if i.code == "EMPTY_IMAGE_ALT"]
        self.assertEqual(len(missing_alt), 1)
        self.assertEqual(missing_alt[0].severity, "warning")
        self.assertEqual(missing_alt[0].target, "a.png")
        self.assertEqual(len(empty_alt), 1)
        self.assertEqual(empty_alt[0].severity, "info")
        self.assertEqual(empty_alt[0].target, "b.png")

    def test_10_missing_seo_meta_and_multiple_h1(self) -> None:
        _write_files(
            self.site_dir,
            {
                "index.html": (
                    "<!DOCTYPE html><html><head></head><body>"
                    "<h1>First</h1><h1>Second</h1>"
                    "</body></html>"
                ),
            },
        )
        res = seo_checker.audit_site(self.site_dir, check_sitemap=False)
        codes = {i.code for i in res.issues}
        self.assertIn("MISSING_TITLE", codes)
        self.assertIn("MISSING_META_DESCRIPTION", codes)
        self.assertIn("MISSING_CANONICAL", codes)
        self.assertIn("MISSING_VIEWPORT", codes)
        self.assertIn("MULTIPLE_H1", codes)

        missing_h1_dir = self.site_dir / "no_h1"
        missing_h1_dir.mkdir()
        _write_files(
            missing_h1_dir,
            {"index.html": _make_html(h1=[])},
        )
        res2 = seo_checker.audit_site(missing_h1_dir, check_sitemap=False)
        self.assertIn("MISSING_H1", {i.code for i in res2.issues})

    def test_11_duplicate_titles_and_meta_descriptions(self) -> None:
        _write_files(
            self.site_dir,
            {
                "index.html": _make_html(
                    title="Identical Site Title",
                    description="Identical description shared across two distinct static pages for testing.",
                    body='<a href="page2.html">Next</a>',
                ),
                "page2.html": _make_html(
                    title="  identical   site title ",
                    description="Identical description shared across two distinct static pages for testing.",
                    body='<a href="index.html">Back</a>',
                ),
            },
        )
        res = seo_checker.audit_site(self.site_dir, check_sitemap=False)
        dup_titles = [i for i in res.issues if i.code == "DUPLICATE_TITLE"]
        dup_descs = [i for i in res.issues if i.code == "DUPLICATE_META_DESCRIPTION"]
        self.assertEqual(len(dup_titles), 1)
        self.assertEqual(dup_titles[0].severity, "warning")
        self.assertEqual(dup_titles[0].page, "index.html, page2.html")
        self.assertEqual(len(dup_descs), 1)
        self.assertEqual(dup_descs[0].severity, "warning")

    def test_12_sitemap_orphans_broken_urls_and_noindex_handling(self) -> None:
        _write_files(
            self.site_dir,
            {
                "index.html": _make_html(
                    body='<a href="orphan.html">Orphan</a><a href="secret.html">Secret</a>'
                ),
                "orphan.html": _make_html(
                    title="Orphan Not In Sitemap Page",
                    description="Valid indexable HTML page omitted from sitemap.xml to trigger SITEMAP_ORPHAN.",
                    body='<a href="index.html">Home</a>',
                ),
                "secret.html": _make_html(
                    title="Noindex Secret Page Title",
                    description="Page marked noindex that should not be flagged as a sitemap orphan.",
                    robots="noindex, nofollow",
                    body='<a href="index.html">Home</a>',
                ),
                "draft-noindex.html": _make_html(
                    title="Noindex Page Included In Sitemap",
                    description="Noindex page mistakenly included inside sitemap.xml to trigger NOINDEX_IN_SITEMAP.",
                    robots="noindex",
                    body='<a href="index.html">Home</a>',
                ),
                "404.html": _make_html(
                    title="404 Page Not Found Error",
                    description="Standard 404 page should never be flagged as a sitemap orphan or link orphan.",
                    body='<a href="index.html">Home</a>',
                ),
                "sitemap.xml": (
                    "<urlset>"
                    "<url><loc>https://example.com/</loc></url>"
                    "<url><loc>https://example.com/draft-noindex.html</loc></url>"
                    "<url><loc>https://example.com/dead-page.html</loc></url>"
                    "<url><loc>https://example.com/</loc></url>"
                    "</urlset>"
                ),
            },
        )
        res = seo_checker.audit_site(self.site_dir)
        codes_by_page = {(i.code, i.page) for i in res.issues}
        self.assertIn(("SITEMAP_ORPHAN", "orphan.html"), codes_by_page)
        self.assertNotIn(("SITEMAP_ORPHAN", "secret.html"), codes_by_page)
        self.assertNotIn(("SITEMAP_ORPHAN", "404.html"), codes_by_page)
        self.assertIn(("NOINDEX_IN_SITEMAP", "draft-noindex.html"), codes_by_page)
        self.assertIn(("BROKEN_SITEMAP_URL", "sitemap.xml"), codes_by_page)
        self.assertIn(("DUPLICATE_SITEMAP_URL", "sitemap.xml"), codes_by_page)

    def test_13_missing_and_malformed_sitemap(self) -> None:
        _write_files(self.site_dir, {"index.html": _make_html()})
        res_missing = seo_checker.audit_site(self.site_dir)
        self.assertIn("MISSING_SITEMAP", {i.code for i in res_missing.issues})

        res_skip = seo_checker.audit_site(self.site_dir, check_sitemap=False)
        self.assertNotIn("MISSING_SITEMAP", {i.code for i in res_skip.issues})

        _write_files(self.site_dir, {"sitemap.xml": "<urlset><url><loc>broken"})
        res_bad_xml = seo_checker.audit_site(self.site_dir)
        invalid = [i for i in res_bad_xml.issues if i.code == "INVALID_SITEMAP"]
        self.assertEqual(len(invalid), 1)
        self.assertEqual(invalid[0].severity, "critical")

    def test_14_sitemap_index_and_base_url_subpath(self) -> None:
        _write_files(
            self.site_dir,
            {
                "index.html": _make_html(
                    canonical="https://user.github.io/my-docs/",
                    body='<a href="https://user.github.io/my-docs/intro.html">Intro</a>',
                ),
                "intro.html": _make_html(
                    title="Introduction — Base Subpath Docs",
                    description="Subpath documentation page checked using --base-url subpath stripping.",
                    canonical="https://user.github.io/my-docs/intro.html",
                    body='<a href="index.html">Home</a>',
                ),
                "sitemap.xml": (
                    "<sitemapindex>"
                    "<sitemap><loc>https://user.github.io/my-docs/sitemap-pages.xml</loc></sitemap>"
                    "</sitemapindex>"
                ),
                "sitemap-pages.xml": (
                    "<urlset>"
                    "<url><loc>https://user.github.io/my-docs/</loc></url>"
                    "<url><loc>https://user.github.io/my-docs/intro.html</loc></url>"
                    "</urlset>"
                ),
            },
        )
        # Note: sitemapindex loc points to /my-docs/sitemap-pages.xml, while file on disk is sitemap-pages.xml
        # Let's also verify root-relative sitemapindex child resolution
        _write_files(
            self.site_dir,
            {
                "sitemap.xml": (
                    "<sitemapindex>"
                    "<sitemap><loc>/sitemap-pages.xml</loc></sitemap>"
                    "</sitemapindex>"
                ),
            },
        )
        res = seo_checker.audit_site(
            self.site_dir,
            base_url="https://user.github.io/my-docs",
        )
        self.assertEqual(res.summary.total_issues, 0)
        self.assertEqual(res.summary.sitemap_urls, 2)
        self.assertEqual(res.summary.health_score, 100)

    def test_15_ignored_schemes_and_external_links_offline(self) -> None:
        _write_files(
            self.site_dir,
            {
                "index.html": _make_html(
                    head_extra='<link rel="stylesheet" href="https://cdn.example.com/app.css">',
                    body=(
                        '<a href="mailto:hello@example.com">Email</a>'
                        '<a href="tel:+15551234567">Phone</a>'
                        '<a href="javascript:void(0)">JS</a>'
                        '<a href="https://github.com/Kreqwaii">GitHub</a>'
                        '<a href="//external.example.org/lib">ProtoRel</a>'
                        '<img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==" alt="Pixel">'
                    ),
                ),
            },
        )
        res = seo_checker.audit_site(self.site_dir, check_sitemap=False)
        self.assertEqual(res.summary.total_issues, 0)
        self.assertEqual(res.summary.health_score, 100)

    def test_16_base_href_resolution(self) -> None:
        _write_files(
            self.site_dir,
            {
                "index.html": _make_html(
                    base_href="/docs/",
                    body='<a href="guide.html">Guide via base</a>',
                ),
                "docs/guide.html": _make_html(
                    title="Guide Page Reached Via Base Href",
                    description="Documentation guide reached using HTML <base href='/docs/'> relative resolution.",
                    body='<a href="../index.html">Home</a>',
                ),
            },
        )
        res = seo_checker.audit_site(self.site_dir, check_sitemap=False)
        self.assertEqual([i for i in res.issues if i.code == "BROKEN_LINK"], [])

    def test_17_orphan_page_detection_in_link_graph(self) -> None:
        _write_files(
            self.site_dir,
            {
                "index.html": _make_html(body='<a href="linked.html">Linked</a>'),
                "linked.html": _make_html(
                    title="Linked Page — Internal Link Graph",
                    description="Page that receives an incoming internal link from index.html.",
                    body='<a href="index.html">Home</a>',
                ),
                "unlinked.html": _make_html(
                    title="Unlinked Orphan Page Title",
                    description="Page with zero incoming internal links triggering ORPHAN_PAGE info finding.",
                    body='<a href="index.html">Home</a>',
                ),
            },
        )
        res = seo_checker.audit_site(self.site_dir, check_sitemap=False)
        orphans = [i for i in res.issues if i.code == "ORPHAN_PAGE"]
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0].page, "unlinked.html")
        self.assertEqual(orphans[0].severity, "info")

    def test_18_exclude_glob_pattern(self) -> None:
        _write_files(
            self.site_dir,
            {
                "index.html": _make_html(),
                "drafts/broken-draft.html": "<html><body><a href='missing.html'>x</a></body></html>",
            },
        )
        res = seo_checker.audit_site(
            self.site_dir,
            check_sitemap=False,
            excludes=["drafts"],
        )
        self.assertEqual(res.summary.pages_scanned, 1)
        self.assertEqual(res.summary.total_issues, 0)

    def test_19_health_score_formula_and_bounds(self) -> None:
        self.assertEqual(seo_checker.compute_health_score(0, 0, 0), 100)
        self.assertEqual(seo_checker.compute_health_score(1, 0, 0), 90)
        self.assertEqual(seo_checker.compute_health_score(0, 2, 3), 91)
        self.assertEqual(seo_checker.compute_health_score(0, 0, 50), 85)  # info capped at 15
        self.assertEqual(seo_checker.compute_health_score(15, 10, 5), 0)

    def test_20_cli_formats_text_json_csv_and_output_file(self) -> None:
        _write_files(
            self.site_dir,
            {
                "index.html": _make_html(body='<a href="broken.html">Broken</a>'),
            },
        )

        # JSON output
        out_json = io.StringIO()
        with redirect_stdout(out_json):
            rc = seo_checker.main([str(self.site_dir), "--format", "json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out_json.getvalue())
        self.assertEqual(payload["summary"]["pages_scanned"], 1)
        self.assertEqual(payload["summary"]["severity_breakdown"]["critical"], 1)
        self.assertEqual(payload["summary"]["severity_breakdown"]["warning"], 1)

        # CSV output with severity filter
        out_csv = io.StringIO()
        with redirect_stdout(out_csv):
            rc2 = seo_checker.main(
                [str(self.site_dir), "--format", "csv", "--severity", "critical"]
            )
        self.assertEqual(rc2, 0)
        rows = list(csv.DictReader(io.StringIO(out_csv.getvalue())))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code"], "BROKEN_LINK")

        # Text output written to file
        report_file = self.site_dir / "reports" / "audit.txt"
        rc3 = seo_checker.main(
            [str(self.site_dir), "--format", "text", "-o", str(report_file)]
        )
        self.assertEqual(rc3, 0)
        report_text = report_file.read_text(encoding="utf-8")
        self.assertIn("Static Site SEO & Link Audit", report_text)
        self.assertIn("BROKEN_LINK", report_text)

    def test_21_cli_exit_codes_policy_and_error_handling(self) -> None:
        _write_files(
            self.site_dir,
            {
                "index.html": _make_html(description=None),  # 1 warning (MISSING_META_DESCRIPTION), 0 critical
            },
        )

        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                seo_checker.main([str(self.site_dir), "--no-sitemap", "--fail-on", "critical"]),
                0,
            )
            self.assertEqual(
                seo_checker.main([str(self.site_dir), "--no-sitemap", "--fail-on", "warning"]),
                1,
            )
            self.assertEqual(
                seo_checker.main([str(self.site_dir), "--no-sitemap", "--min-score", "99"]),
                1,
            )
            self.assertEqual(
                seo_checker.main([str(self.site_dir), "--no-sitemap", "--min-score", "90"]),
                0,
            )

        err_buf = io.StringIO()
        with redirect_stderr(err_buf):
            self.assertEqual(seo_checker.main([str(self.site_dir / "missing_dir")]), 2)
            self.assertEqual(seo_checker.main([str(self.site_dir), "--min-score", "150"]), 2)
            self.assertEqual(seo_checker.main([str(self.site_dir), "--base-url", "not-a-url"]), 2)

    def test_22_subprocess_cli_execution(self) -> None:
        self._build_healthy_site()
        proc = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "seo_checker.py"),
                str(self.site_dir),
                "--fail-on",
                "warning",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["health_score"], 100)
        self.assertTrue(data["summary"]["policy_passed"])


if __name__ == "__main__":
    unittest.main()
