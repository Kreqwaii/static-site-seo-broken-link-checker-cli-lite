#!/usr/bin/env python3
"""Zero-dependency Python CLI to audit static HTML site builds for broken links,
anchor IDs, missing assets/alt text, SEO meta tags, and sitemap.xml orphans.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import io
import json
import os
import posixpath
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import unquote, urlsplit

VERSION = "1.0.0"

HTML_EXTENSIONS = {".html", ".htm"}
DEFAULT_EXCLUDES = {".git", "__pycache__", ".venv", "venv", "node_modules", ".DS_Store"}
IGNORED_SCHEMES = (
    "mailto:",
    "tel:",
    "sms:",
    "javascript:",
    "data:",
    "blob:",
    "about:",
)
ASSET_LINK_RELS = {
    "stylesheet",
    "icon",
    "shortcut",
    "apple-touch-icon",
    "manifest",
    "preload",
    "modulepreload",
    "mask-icon",
}
VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}

TITLE_MIN_LEN = 15
TITLE_MAX_LEN = 65
META_DESC_MIN_LEN = 50
META_DESC_MAX_LEN = 160

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


@dataclass
class ElementRef:
    tag: str
    attr: str
    raw_url: str
    line: int
    rel: tuple[str, ...] = ()
    has_alt: bool = False
    alt_text: Optional[str] = None


@dataclass
class PageData:
    rel_path: str
    base_href: Optional[str] = None
    titles: list[str] = field(default_factory=list)
    meta_descriptions: list[str] = field(default_factory=list)
    canonicals: list[tuple[str, int]] = field(default_factory=list)
    has_viewport: bool = False
    viewport_content: str = ""
    noindex: bool = False
    h1s: list[str] = field(default_factory=list)
    ids: set[str] = field(default_factory=set)
    links: list[ElementRef] = field(default_factory=list)
    assets: list[ElementRef] = field(default_factory=list)
    images: list[ElementRef] = field(default_factory=list)

    @property
    def primary_title(self) -> str:
        return self.titles[0] if self.titles else ""

    @property
    def primary_meta_description(self) -> str:
        return self.meta_descriptions[0] if self.meta_descriptions else ""

    @property
    def primary_canonical(self) -> str:
        return self.canonicals[0][0] if self.canonicals else ""


@dataclass
class Issue:
    severity: str  # "critical" | "warning" | "info"
    code: str
    page: str
    line: Optional[int]
    target: str
    message: str

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "code": self.code,
            "page": self.page,
            "line": self.line,
            "target": self.target,
            "message": self.message,
        }


@dataclass
class AuditSummary:
    pages_scanned: int
    links_checked: int
    assets_checked: int
    sitemap_found: bool
    sitemap_urls: int
    total_issues: int
    critical: int
    warning: int
    info: int
    health_score: int
    policy_fail_on: str
    min_score: Optional[int]
    policy_passed: bool

    def to_dict(self) -> dict:
        return {
            "pages_scanned": self.pages_scanned,
            "links_checked": self.links_checked,
            "assets_checked": self.assets_checked,
            "sitemap_found": self.sitemap_found,
            "sitemap_urls": self.sitemap_urls,
            "total_issues": self.total_issues,
            "severity_breakdown": {
                "critical": self.critical,
                "warning": self.warning,
                "info": self.info,
            },
            "health_score": self.health_score,
            "policy_fail_on": self.policy_fail_on,
            "min_score": self.min_score,
            "policy_passed": self.policy_passed,
        }


@dataclass
class AuditResult:
    site_dir: str
    base_url: Optional[str]
    summary: AuditSummary
    pages: list[dict]
    issues: list[Issue]

    def filtered_issues(self, severity_filter: str = "all") -> list[Issue]:
        if severity_filter == "all":
            return list(self.issues)
        return [item for item in self.issues if item.severity == severity_filter]

    def to_dict(self, severity_filter: str = "all") -> dict:
        return {
            "tool": "static-site-seo-broken-link-checker-cli-lite",
            "version": VERSION,
            "site_dir": self.site_dir,
            "base_url": self.base_url,
            "health_score": self.summary.health_score,
            "summary": self.summary.to_dict(),
            "pages": self.pages,
            "issues": [issue.to_dict() for issue in self.filtered_issues(severity_filter)],
        }


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())


class SiteHTMLParser(HTMLParser):
    """Stdlib HTMLParser extracting SEO tags, IDs, links, and local asset references."""

    def __init__(self, rel_path: str) -> None:
        super().__init__(convert_charrefs=True)
        self.page = PageData(rel_path=rel_path)
        self._in_title = False
        self._title_buf: list[str] = []
        self._in_h1 = False
        self._h1_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        self._process_tag(tag.lower(), attrs, self_closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        self._process_tag(tag.lower(), attrs, self_closing=True)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title" and self._in_title:
            self._in_title = False
            self.page.titles.append(_collapse_ws("".join(self._title_buf)))
            self._title_buf.clear()
        elif tag == "h1" and self._in_h1:
            self._in_h1 = False
            self.page.h1s.append(_collapse_ws("".join(self._h1_buf)))
            self._h1_buf.clear()

    def close(self) -> None:
        super().close()
        if self._in_title:
            self._in_title = False
            self.page.titles.append(_collapse_ws("".join(self._title_buf)))
            self._title_buf.clear()
        if self._in_h1:
            self._in_h1 = False
            self.page.h1s.append(_collapse_ws("".join(self._h1_buf)))
            self._h1_buf.clear()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_buf.append(data)
        if self._in_h1:
            self._h1_buf.append(data)

    def _process_tag(
        self,
        tag: str,
        attrs: list[tuple[str, Optional[str]]],
        self_closing: bool,
    ) -> None:
        attr_map: dict[str, Optional[str]] = {}
        for key, value in attrs:
            k = key.lower()
            if k not in attr_map:
                attr_map[k] = value

        line = self.getpos()[0]

        # Track element IDs and legacy <a name="..."> anchors
        elem_id = attr_map.get("id")
        if elem_id is not None and elem_id.strip():
            self.page.ids.add(elem_id.strip())
        if tag == "a":
            anchor_name = attr_map.get("name")
            if anchor_name is not None and anchor_name.strip():
                self.page.ids.add(anchor_name.strip())

        if tag == "base" and self.page.base_href is None:
            href = attr_map.get("href")
            if href is not None and href.strip():
                self.page.base_href = href.strip()

        elif tag == "title" and not self_closing:
            self._in_title = True
            self._title_buf.clear()

        elif tag == "h1" and not self_closing:
            self._in_h1 = True
            self._h1_buf.clear()

        elif tag == "meta":
            name = (attr_map.get("name") or "").strip().lower()
            content = (attr_map.get("content") or "").strip()
            if name == "description":
                self.page.meta_descriptions.append(_collapse_ws(content))
            elif name == "viewport":
                self.page.has_viewport = True
                self.page.viewport_content = content
            elif name == "robots":
                tokens = {tok.strip() for tok in content.lower().split(",") if tok.strip()}
                if "noindex" in tokens:
                    self.page.noindex = True

        elif tag in ("a", "area"):
            if "href" in attr_map:
                raw_href = (attr_map.get("href") or "").strip()
                self.page.links.append(
                    ElementRef(tag=tag, attr="href", raw_url=raw_href, line=line)
                )

        elif tag == "img":
            has_alt = "alt" in attr_map
            alt_text = attr_map.get("alt")
            if "src" in attr_map:
                raw_src = (attr_map.get("src") or "").strip()
                ref = ElementRef(
                    tag="img",
                    attr="src",
                    raw_url=raw_src,
                    line=line,
                    has_alt=has_alt,
                    alt_text=alt_text,
                )
                self.page.images.append(ref)
                self.page.assets.append(ref)
            else:
                # <img> without src attribute at all
                ref = ElementRef(
                    tag="img",
                    attr="src",
                    raw_url="",
                    line=line,
                    has_alt=has_alt,
                    alt_text=alt_text,
                )
                self.page.images.append(ref)
                self.page.assets.append(ref)

            raw_srcset = attr_map.get("srcset")
            if raw_srcset:
                for candidate in _parse_srcset(raw_srcset):
                    self.page.assets.append(
                        ElementRef(tag="img", attr="srcset", raw_url=candidate, line=line)
                    )

        elif tag == "link":
            rel_raw = (attr_map.get("rel") or "").strip().lower()
            rel_tokens = tuple(tok for tok in rel_raw.split() if tok)
            href = (attr_map.get("href") or "").strip() if "href" in attr_map else ""
            if "canonical" in rel_tokens:
                self.page.canonicals.append((href, line))
            if any(token in ASSET_LINK_RELS for token in rel_tokens) and "href" in attr_map:
                self.page.assets.append(
                    ElementRef(
                        tag="link",
                        attr="href",
                        raw_url=href,
                        line=line,
                        rel=rel_tokens,
                    )
                )

        elif tag == "script":
            if "src" in attr_map:
                raw_src = (attr_map.get("src") or "").strip()
                self.page.assets.append(
                    ElementRef(tag="script", attr="src", raw_url=raw_src, line=line)
                )

        elif tag == "source":
            if "src" in attr_map:
                raw_src = (attr_map.get("src") or "").strip()
                self.page.assets.append(
                    ElementRef(tag="source", attr="src", raw_url=raw_src, line=line)
                )
            raw_srcset = attr_map.get("srcset")
            if raw_srcset:
                for candidate in _parse_srcset(raw_srcset):
                    self.page.assets.append(
                        ElementRef(tag="source", attr="srcset", raw_url=candidate, line=line)
                    )

        elif tag == "video":
            if "poster" in attr_map:
                raw_poster = (attr_map.get("poster") or "").strip()
                self.page.assets.append(
                    ElementRef(tag="video", attr="poster", raw_url=raw_poster, line=line)
                )


def _parse_srcset(raw_srcset: str) -> list[str]:
    stripped = raw_srcset.strip()
    if not stripped or stripped.lower().startswith("data:"):
        return []
    urls: list[str] = []
    for part in stripped.split(","):
        token = part.strip()
        if not token:
            continue
        candidate = token.split()[0]
        if candidate:
            urls.append(candidate)
    return urls


def _is_excluded(rel_posix: str, excludes: set[str]) -> bool:
    parts = rel_posix.split("/")
    for pattern in excludes:
        for part in parts:
            if fnmatch.fnmatch(part, pattern):
                return True
        if fnmatch.fnmatch(rel_posix, pattern):
            return True
    return False


def collect_site_files(
    site_dir: Path,
    excludes: Optional[Iterable[str]] = None,
) -> tuple[set[str], set[str], list[str]]:
    """Walk site_dir and return exact case-sensitive relative POSIX files, dirs, and HTML files."""
    exclude_set = set(DEFAULT_EXCLUDES)
    if excludes:
        exclude_set.update(excludes)

    existing_files: set[str] = set()
    existing_dirs: set[str] = {""}
    html_files: list[str] = []

    for root, dirs, files in os.walk(site_dir):
        rel_root = Path(root).relative_to(site_dir).as_posix()
        if rel_root == ".":
            rel_root = ""

        # Prune excluded directories in-place
        kept_dirs: list[str] = []
        for d in sorted(dirs):
            rel_dir = f"{rel_root}/{d}" if rel_root else d
            if not _is_excluded(rel_dir, exclude_set):
                kept_dirs.append(d)
                existing_dirs.add(rel_dir)
        dirs[:] = kept_dirs

        for filename in sorted(files):
            rel_file = f"{rel_root}/{filename}" if rel_root else filename
            if _is_excluded(rel_file, exclude_set):
                continue
            existing_files.add(rel_file)
            if Path(filename).suffix.lower() in HTML_EXTENSIONS:
                html_files.append(rel_file)

    html_files.sort()
    return existing_files, existing_dirs, html_files


def parse_html_page(site_dir: Path, rel_path: str) -> PageData:
    full_path = site_dir / rel_path
    raw_text = full_path.read_text(encoding="utf-8-sig", errors="replace")
    parser = SiteHTMLParser(rel_path=rel_path)
    parser.feed(raw_text)
    parser.close()
    return parser.page


def _normalize_base_url(base_url: Optional[str]) -> Optional[tuple[str, str, str]]:
    if not base_url:
        return None
    parts = urlsplit(base_url.strip())
    if not parts.scheme or not parts.netloc:
        return None
    base_path = parts.path.rstrip("/")
    return parts.scheme.lower(), parts.netloc.lower(), base_path


def _strip_base_url(raw_url: str, norm_base: Optional[tuple[str, str, str]]) -> Optional[str]:
    """If raw_url is an HTTP(S) URL matching norm_base, return a site-root-relative path."""
    if norm_base is None:
        return None
    parts = urlsplit(raw_url)
    if parts.scheme.lower() != norm_base[0] or parts.netloc.lower() != norm_base[1]:
        return None
    base_path = norm_base[2]
    url_path = parts.path or "/"
    if base_path:
        if url_path == base_path or url_path == f"{base_path}/":
            rel = "/"
        elif url_path.startswith(f"{base_path}/"):
            rel = url_path[len(base_path) :]
        else:
            return None
    else:
        rel = url_path
    if parts.fragment:
        return f"{rel}#{parts.fragment}"
    return rel


def _resolve_disk_target(
    rel_candidate: str,
    existing_files: set[str],
    existing_dirs: set[str],
    is_asset: bool,
    clean_urls: bool,
    trailing_slash: bool,
) -> Optional[str]:
    cleaned = rel_candidate.strip("/")
    if is_asset:
        if not cleaned or trailing_slash:
            return None
        return cleaned if cleaned in existing_files else None

    # HTML/page link resolution
    if not cleaned:
        for idx in ("index.html", "index.htm"):
            if idx in existing_files:
                return idx
        return None

    if trailing_slash:
        for idx in (f"{cleaned}/index.html", f"{cleaned}/index.htm"):
            if idx in existing_files:
                return idx
        return None

    if cleaned in existing_files:
        return cleaned

    if cleaned in existing_dirs:
        for idx in (f"{cleaned}/index.html", f"{cleaned}/index.htm"):
            if idx in existing_files:
                return idx

    if clean_urls and "." not in posixpath.basename(cleaned):
        for candidate in (
            f"{cleaned}.html",
            f"{cleaned}.htm",
            f"{cleaned}/index.html",
            f"{cleaned}/index.htm",
        ):
            if candidate in existing_files:
                return candidate

    return None


def resolve_local_url(
    raw_url: str,
    source_rel_path: str,
    base_href: Optional[str],
    norm_base_url: Optional[tuple[str, str, str]],
    existing_files: set[str],
    existing_dirs: set[str],
    is_asset: bool,
    clean_urls: bool,
) -> dict:
    """Resolve a URL found in an HTML file to a local file/anchor inside site_dir."""
    stripped = raw_url.strip()
    if not stripped:
        return {"status": "empty"}

    lower = stripped.lower()
    if any(lower.startswith(scheme) for scheme in IGNORED_SCHEMES):
        return {"status": "ignored"}

    if stripped.startswith("//"):
        return {"status": "external"}

    parts = urlsplit(stripped)
    if parts.scheme:
        if parts.scheme.lower() in ("http", "https"):
            mapped = _strip_base_url(stripped, norm_base_url)
            if mapped is None:
                return {"status": "external"}
            stripped = mapped
            parts = urlsplit(stripped)
        else:
            return {"status": "ignored"}

    fragment = unquote(parts.fragment) if parts.fragment else ""
    url_path = unquote(parts.path)

    # Pure fragment or query-only link (e.g. "#", "#section", or "?tab=1")
    if not url_path:
        if not parts.query:
            if not fragment:
                return {"status": "bare_hash"}
            return {
                "status": "same_page_anchor",
                "target_file": source_rel_path,
                "fragment": fragment,
            }
        return {
            "status": "resolved",
            "target_file": source_rel_path,
            "fragment": fragment,
        }

    # Determine resolution base directory relative to site_dir
    source_dir = posixpath.dirname(source_rel_path)
    effective_dir = source_dir
    if base_href and not url_path.startswith("/"):
        b_parts = urlsplit(base_href)
        if b_parts.scheme in ("", "http", "https"):
            b_path = b_parts.path
            if b_parts.scheme:
                mapped_base = _strip_base_url(base_href, norm_base_url)
                b_path = urlsplit(mapped_base).path if mapped_base else ""
            if b_path:
                if b_path.startswith("/"):
                    norm_b = posixpath.normpath(b_path.lstrip("/"))
                    effective_dir = "" if norm_b == "." else norm_b
                else:
                    joined_base = posixpath.join(source_dir, b_path) if source_dir else b_path
                    norm_b = posixpath.normpath(joined_base)
                    if not (norm_b == ".." or norm_b.startswith("../")):
                        effective_dir = "" if norm_b == "." else norm_b

    trailing_slash = url_path.endswith("/")
    if url_path.startswith("/"):
        rel_path_raw = url_path.lstrip("/")
        if norm_base_url and norm_base_url[2]:
            prefix = norm_base_url[2].lstrip("/")
            if rel_path_raw == prefix:
                rel_path_raw = ""
            elif rel_path_raw.startswith(f"{prefix}/"):
                rel_path_raw = rel_path_raw[len(prefix) + 1 :]
        normed = posixpath.normpath(rel_path_raw) if rel_path_raw else ""
    else:
        combined = posixpath.join(effective_dir, url_path) if effective_dir else url_path
        normed = posixpath.normpath(combined)

    if normed == ".":
        normed = ""

    if normed == ".." or normed.startswith("../"):
        return {
            "status": "broken",
            "reason": f"Path '{raw_url}' escapes the site root directory.",
            "target": normed,
        }

    target_file = _resolve_disk_target(
        rel_candidate=normed,
        existing_files=existing_files,
        existing_dirs=existing_dirs,
        is_asset=is_asset,
        clean_urls=clean_urls,
        trailing_slash=trailing_slash,
    )
    if target_file is None:
        return {
            "status": "broken",
            "reason": f"Target '{normed or '/'}' not found in site directory.",
            "target": normed or "/",
        }

    return {
        "status": "resolved",
        "target_file": target_file,
        "fragment": fragment,
    }


def _infer_base_url_from_locs(locs: list[str]) -> Optional[tuple[str, str, str]]:
    for loc in locs:
        parts = urlsplit(loc.strip())
        if parts.scheme.lower() in ("http", "https") and parts.netloc:
            return parts.scheme.lower(), parts.netloc.lower(), ""
    return None


def _parse_sitemap_xml(
    sitemap_path: Path,
    site_dir: Path,
    seen_sitemaps: set[Path],
) -> tuple[list[str], list[Issue]]:
    """Extract <loc> URLs from a <urlset> or nested local <sitemapindex>."""
    resolved = sitemap_path.resolve()
    if resolved in seen_sitemaps:
        return [], []
    seen_sitemaps.add(resolved)

    rel_display = (
        sitemap_path.relative_to(site_dir).as_posix()
        if sitemap_path.is_relative_to(site_dir)
        else sitemap_path.name
    )
    issues: list[Issue] = []

    try:
        content = sitemap_path.read_text(encoding="utf-8-sig", errors="replace")
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        issues.append(
            Issue(
                severity="critical",
                code="INVALID_SITEMAP",
                page=rel_display,
                line=exc.position[0] if getattr(exc, "position", None) else None,
                target=rel_display,
                message=f"Malformed XML in sitemap '{rel_display}': {exc}",
            )
        )
        return [], issues

    root_tag = root.tag.split("}")[-1].lower()
    locs: list[str] = []

    if root_tag == "urlset":
        for child in root:
            if child.tag.split("}")[-1].lower() == "url":
                for sub in child:
                    if sub.tag.split("}")[-1].lower() == "loc" and sub.text:
                        locs.append(sub.text.strip())
    elif root_tag == "sitemapindex":
        for child in root:
            if child.tag.split("}")[-1].lower() == "sitemap":
                for sub in child:
                    if sub.tag.split("}")[-1].lower() == "loc" and sub.text:
                        child_loc = sub.text.strip()
                        c_parts = urlsplit(child_loc)
                        child_rel = unquote(c_parts.path).lstrip("/")
                        child_path = site_dir / child_rel
                        if child_path.is_file():
                            sub_locs, sub_issues = _parse_sitemap_xml(
                                child_path, site_dir, seen_sitemaps
                            )
                            locs.extend(sub_locs)
                            issues.extend(sub_issues)
    else:
        issues.append(
            Issue(
                severity="warning",
                code="INVALID_SITEMAP",
                page=rel_display,
                line=1,
                target=root_tag,
                message=f"Unexpected sitemap root element <{root_tag}> in '{rel_display}'.",
            )
        )

    return locs, issues


def compute_health_score(critical: int, warning: int, info: int) -> int:
    penalty = (critical * 10) + (warning * 3) + min(15, info)
    return max(0, min(100, 100 - penalty))


def audit_site(
    site_dir: Path,
    base_url: Optional[str] = None,
    sitemap_path: Optional[Path] = None,
    check_sitemap: bool = True,
    excludes: Optional[Iterable[str]] = None,
    clean_urls: bool = True,
    fail_on: str = "none",
    min_score: Optional[int] = None,
) -> AuditResult:
    """Run full SEO & broken-link audit on a local static website directory."""
    site_dir = site_dir.resolve()
    existing_files, existing_dirs, html_files = collect_site_files(site_dir, excludes=excludes)
    norm_base = _normalize_base_url(base_url)

    parsed_pages: dict[str, PageData] = {}
    for rel_path in html_files:
        parsed_pages[rel_path] = parse_html_page(site_dir, rel_path)

    issues: list[Issue] = []
    links_checked = 0
    assets_checked = 0
    inbound_links: dict[str, set[str]] = {rel: set() for rel in html_files}

    # 1. Per-page link, asset, image, and on-page SEO checks
    for rel_path in html_files:
        page = parsed_pages[rel_path]

        # <title>
        if not page.titles:
            issues.append(
                Issue(
                    severity="critical",
                    code="MISSING_TITLE",
                    page=rel_path,
                    line=None,
                    target="<title>",
                    message="Page is missing a <title> tag.",
                )
            )
        else:
            if len(page.titles) > 1:
                issues.append(
                    Issue(
                        severity="warning",
                        code="MULTIPLE_TITLES",
                        page=rel_path,
                        line=None,
                        target="<title>",
                        message=f"Page defines {len(page.titles)} <title> tags; keep exactly 1.",
                    )
                )
            title_text = page.primary_title
            if not title_text:
                issues.append(
                    Issue(
                        severity="critical",
                        code="EMPTY_TITLE",
                        page=rel_path,
                        line=None,
                        target="<title>",
                        message="Page has an empty <title> tag.",
                    )
                )
            elif len(title_text) < TITLE_MIN_LEN:
                issues.append(
                    Issue(
                        severity="info",
                        code="SHORT_TITLE",
                        page=rel_path,
                        line=None,
                        target=title_text,
                        message=f"<title> is short ({len(title_text)} chars; recommended {TITLE_MIN_LEN}-{TITLE_MAX_LEN}).",
                    )
                )
            elif len(title_text) > TITLE_MAX_LEN:
                issues.append(
                    Issue(
                        severity="info",
                        code="LONG_TITLE",
                        page=rel_path,
                        line=None,
                        target=title_text,
                        message=f"<title> is long ({len(title_text)} chars; recommended {TITLE_MIN_LEN}-{TITLE_MAX_LEN}).",
                    )
                )

        # <meta name="description">
        if not page.meta_descriptions:
            issues.append(
                Issue(
                    severity="warning",
                    code="MISSING_META_DESCRIPTION",
                    page=rel_path,
                    line=None,
                    target='meta[name="description"]',
                    message='Page is missing <meta name="description">.',
                )
            )
        else:
            if len(page.meta_descriptions) > 1:
                issues.append(
                    Issue(
                        severity="warning",
                        code="MULTIPLE_META_DESCRIPTIONS",
                        page=rel_path,
                        line=None,
                        target='meta[name="description"]',
                        message=f"Page defines {len(page.meta_descriptions)} meta description tags.",
                    )
                )
            desc = page.primary_meta_description
            if not desc:
                issues.append(
                    Issue(
                        severity="warning",
                        code="EMPTY_META_DESCRIPTION",
                        page=rel_path,
                        line=None,
                        target='meta[name="description"]',
                        message='<meta name="description"> content attribute is empty.',
                    )
                )
            elif len(desc) < META_DESC_MIN_LEN:
                issues.append(
                    Issue(
                        severity="info",
                        code="SHORT_META_DESCRIPTION",
                        page=rel_path,
                        line=None,
                        target='meta[name="description"]',
                        message=f"Meta description is short ({len(desc)} chars; recommended {META_DESC_MIN_LEN}-{META_DESC_MAX_LEN}).",
                    )
                )
            elif len(desc) > META_DESC_MAX_LEN:
                issues.append(
                    Issue(
                        severity="info",
                        code="LONG_META_DESCRIPTION",
                        page=rel_path,
                        line=None,
                        target='meta[name="description"]',
                        message=f"Meta description is long ({len(desc)} chars; recommended {META_DESC_MIN_LEN}-{META_DESC_MAX_LEN}).",
                    )
                )

        # <h1>
        if not page.h1s:
            issues.append(
                Issue(
                    severity="warning",
                    code="MISSING_H1",
                    page=rel_path,
                    line=None,
                    target="<h1>",
                    message="Page is missing an <h1> heading.",
                )
            )
        else:
            if any(not h for h in page.h1s):
                issues.append(
                    Issue(
                        severity="warning",
                        code="EMPTY_H1",
                        page=rel_path,
                        line=None,
                        target="<h1>",
                        message="Page contains an empty <h1> heading.",
                    )
                )
            if len(page.h1s) > 1:
                issues.append(
                    Issue(
                        severity="warning",
                        code="MULTIPLE_H1",
                        page=rel_path,
                        line=None,
                        target="<h1>",
                        message=f"Page defines {len(page.h1s)} <h1> headings; recommend a single primary <h1>.",
                    )
                )

        # <link rel="canonical">
        if not page.canonicals:
            issues.append(
                Issue(
                    severity="warning",
                    code="MISSING_CANONICAL",
                    page=rel_path,
                    line=None,
                    target='link[rel="canonical"]',
                    message='Page is missing <link rel="canonical">.',
                )
            )
        else:
            if len(page.canonicals) > 1:
                issues.append(
                    Issue(
                        severity="warning",
                        code="MULTIPLE_CANONICALS",
                        page=rel_path,
                        line=page.canonicals[1][1],
                        target='link[rel="canonical"]',
                        message=f"Page defines {len(page.canonicals)} canonical link tags.",
                    )
                )
            canon_href, canon_line = page.canonicals[0]
            if not canon_href:
                issues.append(
                    Issue(
                        severity="warning",
                        code="EMPTY_CANONICAL",
                        page=rel_path,
                        line=canon_line,
                        target='link[rel="canonical"]',
                        message="Canonical link tag has an empty href.",
                    )
                )
            else:
                canon_res = resolve_local_url(
                    raw_url=canon_href,
                    source_rel_path=rel_path,
                    base_href=page.base_href,
                    norm_base_url=norm_base,
                    existing_files=existing_files,
                    existing_dirs=existing_dirs,
                    is_asset=False,
                    clean_urls=clean_urls,
                )
                if canon_res["status"] == "broken":
                    issues.append(
                        Issue(
                            severity="warning",
                            code="BROKEN_CANONICAL",
                            page=rel_path,
                            line=canon_line,
                            target=canon_href,
                            message=f"Canonical URL '{canon_href}' does not resolve to an existing local page.",
                        )
                    )

        # <meta name="viewport">
        if not page.has_viewport or not page.viewport_content:
            issues.append(
                Issue(
                    severity="warning",
                    code="MISSING_VIEWPORT",
                    page=rel_path,
                    line=None,
                    target='meta[name="viewport"]',
                    message='Page is missing a non-empty <meta name="viewport"> tag.',
                )
            )

        # Internal links & #anchor verification
        for link in page.links:
            links_checked += 1
            res = resolve_local_url(
                raw_url=link.raw_url,
                source_rel_path=rel_path,
                base_href=page.base_href,
                norm_base_url=norm_base,
                existing_files=existing_files,
                existing_dirs=existing_dirs,
                is_asset=False,
                clean_urls=clean_urls,
            )
            status = res["status"]
            if status == "empty":
                issues.append(
                    Issue(
                        severity="warning",
                        code="EMPTY_LINK",
                        page=rel_path,
                        line=link.line,
                        target=f"<{link.tag}>",
                        message=f"<{link.tag}> has an empty href attribute.",
                    )
                )
            elif status == "bare_hash":
                issues.append(
                    Issue(
                        severity="info",
                        code="PLACEHOLDER_ANCHOR",
                        page=rel_path,
                        line=link.line,
                        target="#",
                        message="Link uses placeholder fragment '#'.",
                    )
                )
            elif status == "same_page_anchor":
                frag = res["fragment"]
                if frag not in page.ids:
                    issues.append(
                        Issue(
                            severity="critical",
                            code="BROKEN_ANCHOR",
                            page=rel_path,
                            line=link.line,
                            target=link.raw_url,
                            message=f"Broken same-page anchor '#{frag}' (no matching id or name found).",
                        )
                    )
            elif status == "broken":
                issues.append(
                    Issue(
                        severity="critical",
                        code="BROKEN_LINK",
                        page=rel_path,
                        line=link.line,
                        target=link.raw_url,
                        message=f"Broken internal link '{link.raw_url}': {res['reason']}",
                    )
                )
            elif status == "resolved":
                target_file = res["target_file"]
                if target_file in inbound_links and target_file != rel_path:
                    inbound_links[target_file].add(rel_path)
                frag = res["fragment"]
                if frag and target_file in parsed_pages:
                    if frag not in parsed_pages[target_file].ids:
                        issues.append(
                            Issue(
                                severity="critical",
                                code="BROKEN_ANCHOR",
                                page=rel_path,
                                line=link.line,
                                target=link.raw_url,
                                message=f"Broken anchor '#{frag}' on target page '{target_file}'.",
                            )
                        )

        # Images alt attribute check
        for img in page.images:
            if not img.has_alt:
                issues.append(
                    Issue(
                        severity="warning",
                        code="MISSING_IMAGE_ALT",
                        page=rel_path,
                        line=img.line,
                        target=img.raw_url or "<img>",
                        message=f"Image '{img.raw_url or '<img>'}' is missing an alt attribute.",
                    )
                )
            elif img.alt_text is not None and img.alt_text.strip() == "":
                issues.append(
                    Issue(
                        severity="info",
                        code="EMPTY_IMAGE_ALT",
                        page=rel_path,
                        line=img.line,
                        target=img.raw_url or "<img>",
                        message=f"Image '{img.raw_url or '<img>'}' has an empty alt=\"\" attribute.",
                    )
                )

        # Local CSS, JS, image, source, poster assets
        for asset in page.assets:
            assets_checked += 1
            res = resolve_local_url(
                raw_url=asset.raw_url,
                source_rel_path=rel_path,
                base_href=page.base_href,
                norm_base_url=norm_base,
                existing_files=existing_files,
                existing_dirs=existing_dirs,
                is_asset=True,
                clean_urls=clean_urls,
            )
            if res["status"] in ("empty", "broken"):
                reason = res.get("reason") or f"Empty {asset.attr} attribute on <{asset.tag}>."
                issues.append(
                    Issue(
                        severity="critical",
                        code="BROKEN_ASSET",
                        page=rel_path,
                        line=asset.line,
                        target=asset.raw_url or f"<{asset.tag} {asset.attr}>",
                        message=f"Broken local asset <{asset.tag} {asset.attr}=\"{asset.raw_url}\">: {reason}",
                    )
                )

    # 2. Duplicate <title> and <meta description> across site
    title_groups: dict[str, list[str]] = {}
    desc_groups: dict[str, list[str]] = {}
    for rel_path in html_files:
        page = parsed_pages[rel_path]
        if page.primary_title:
            key = page.primary_title.lower()
            title_groups.setdefault(key, []).append(rel_path)
        if page.primary_meta_description:
            key = page.primary_meta_description.lower()
            desc_groups.setdefault(key, []).append(rel_path)

    for group_pages in title_groups.values():
        if len(group_pages) > 1:
            sample_title = parsed_pages[group_pages[0]].primary_title
            issues.append(
                Issue(
                    severity="warning",
                    code="DUPLICATE_TITLE",
                    page=", ".join(group_pages),
                    line=None,
                    target=sample_title,
                    message=f"Duplicate <title> '{sample_title}' shared by {len(group_pages)} pages: {', '.join(group_pages)}.",
                )
            )

    for group_pages in desc_groups.values():
        if len(group_pages) > 1:
            sample_desc = parsed_pages[group_pages[0]].primary_meta_description
            preview = sample_desc[:60] + ("..." if len(sample_desc) > 60 else "")
            issues.append(
                Issue(
                    severity="warning",
                    code="DUPLICATE_META_DESCRIPTION",
                    page=", ".join(group_pages),
                    line=None,
                    target=preview,
                    message=f"Duplicate meta description shared by {len(group_pages)} pages: {', '.join(group_pages)}.",
                )
            )

    # 3. Orphan pages in internal link graph
    if len(html_files) > 1:
        for rel_path in html_files:
            filename_lower = Path(rel_path).name.lower()
            if rel_path in ("index.html", "index.htm") or filename_lower in ("404.html", "404.htm"):
                continue
            if not inbound_links.get(rel_path):
                issues.append(
                    Issue(
                        severity="info",
                        code="ORPHAN_PAGE",
                        page=rel_path,
                        line=None,
                        target=rel_path,
                        message=f"Page '{rel_path}' has no incoming internal links from other pages.",
                    )
                )

    # 4. Sitemap.xml cross-check
    sitemap_found = False
    sitemap_urls_count = 0
    sitemap_covered_pages: set[str] = set()

    if check_sitemap:
        chosen_sitemap = sitemap_path if sitemap_path is not None else (site_dir / "sitemap.xml")
        if chosen_sitemap.is_file():
            sitemap_found = True
            locs, sm_issues = _parse_sitemap_xml(chosen_sitemap, site_dir, set())
            issues.extend(sm_issues)
            sitemap_urls_count = len(locs)

            effective_base = norm_base if norm_base is not None else _infer_base_url_from_locs(locs)
            sm_display = (
                chosen_sitemap.relative_to(site_dir).as_posix()
                if chosen_sitemap.is_relative_to(site_dir)
                else chosen_sitemap.name
            )
            seen_locs: set[str] = set()

            for loc in locs:
                if loc in seen_locs:
                    issues.append(
                        Issue(
                            severity="warning",
                            code="DUPLICATE_SITEMAP_URL",
                            page=sm_display,
                            line=None,
                            target=loc,
                            message=f"Duplicate <loc> URL '{loc}' in {sm_display}.",
                        )
                    )
                seen_locs.add(loc)

                res = resolve_local_url(
                    raw_url=loc,
                    source_rel_path=sm_display,
                    base_href=None,
                    norm_base_url=effective_base,
                    existing_files=existing_files,
                    existing_dirs=existing_dirs,
                    is_asset=False,
                    clean_urls=clean_urls,
                )
                if res["status"] == "external":
                    issues.append(
                        Issue(
                            severity="warning",
                            code="SITEMAP_ORIGIN_MISMATCH",
                            page=sm_display,
                            line=None,
                            target=loc,
                            message=f"Sitemap URL '{loc}' does not match configured base URL.",
                        )
                    )
                elif res["status"] == "broken":
                    issues.append(
                        Issue(
                            severity="critical",
                            code="BROKEN_SITEMAP_URL",
                            page=sm_display,
                            line=None,
                            target=loc,
                            message=f"Sitemap URL '{loc}' does not map to any local file: {res['reason']}",
                        )
                    )
                elif res["status"] == "resolved":
                    target_file = res["target_file"]
                    sitemap_covered_pages.add(target_file)
                    if target_file in parsed_pages and parsed_pages[target_file].noindex:
                        issues.append(
                            Issue(
                                severity="warning",
                                code="NOINDEX_IN_SITEMAP",
                                page=target_file,
                                line=None,
                                target=loc,
                                message=f"Page '{target_file}' has robots=noindex yet appears in {sm_display}.",
                            )
                        )

            for rel_path in html_files:
                page = parsed_pages[rel_path]
                filename_lower = Path(rel_path).name.lower()
                if page.noindex or filename_lower in ("404.html", "404.htm"):
                    continue
                if rel_path not in sitemap_covered_pages:
                    issues.append(
                        Issue(
                            severity="warning",
                            code="SITEMAP_ORPHAN",
                            page=rel_path,
                            line=None,
                            target=sm_display,
                            message=f"HTML page '{rel_path}' is missing from {sm_display}.",
                        )
                    )
        else:
            issues.append(
                Issue(
                    severity="warning",
                    code="MISSING_SITEMAP",
                    page="sitemap.xml",
                    line=None,
                    target="sitemap.xml",
                    message="No sitemap.xml found in site root directory.",
                )
            )

    # Sort issues deterministically: severity, page, line, code
    issues.sort(
        key=lambda item: (
            SEVERITY_ORDER.get(item.severity, 99),
            item.page,
            item.line if item.line is not None else 0,
            item.code,
            item.target,
        )
    )

    critical_count = sum(1 for i in issues if i.severity == "critical")
    warning_count = sum(1 for i in issues if i.severity == "warning")
    info_count = sum(1 for i in issues if i.severity == "info")
    health_score = compute_health_score(critical_count, warning_count, info_count)

    policy_passed = True
    if fail_on == "critical" and critical_count > 0:
        policy_passed = False
    elif fail_on == "warning" and (critical_count > 0 or warning_count > 0):
        policy_passed = False

    if min_score is not None and health_score < min_score:
        policy_passed = False

    summary = AuditSummary(
        pages_scanned=len(html_files),
        links_checked=links_checked,
        assets_checked=assets_checked,
        sitemap_found=sitemap_found,
        sitemap_urls=sitemap_urls_count,
        total_issues=len(issues),
        critical=critical_count,
        warning=warning_count,
        info=info_count,
        health_score=health_score,
        policy_fail_on=fail_on,
        min_score=min_score,
        policy_passed=policy_passed,
    )

    pages_summary = [
        {
            "path": rel_path,
            "title": parsed_pages[rel_path].primary_title,
            "meta_description": parsed_pages[rel_path].primary_meta_description,
            "h1": parsed_pages[rel_path].h1s,
            "canonical": parsed_pages[rel_path].primary_canonical,
            "has_viewport": parsed_pages[rel_path].has_viewport,
            "noindex": parsed_pages[rel_path].noindex,
            "links": len(parsed_pages[rel_path].links),
            "images": len(parsed_pages[rel_path].images),
            "assets": len(parsed_pages[rel_path].assets),
            "in_sitemap": rel_path in sitemap_covered_pages,
        }
        for rel_path in html_files
    ]

    return AuditResult(
        site_dir=site_dir.as_posix(),
        base_url=base_url,
        summary=summary,
        pages=pages_summary,
        issues=issues,
    )


def format_text(result: AuditResult, severity_filter: str = "all") -> str:
    s = result.summary
    status_label = "PASS" if s.policy_passed else "FAIL"
    lines = [
        (
            f"Static Site SEO & Link Audit | Score: {s.health_score}/100 ({status_label}) | "
            f"Pages: {s.pages_scanned} | Links: {s.links_checked} | Assets: {s.assets_checked} | "
            f"Critical: {s.critical} | Warning: {s.warning} | Info: {s.info}"
        ),
        f"Site: {result.site_dir}",
    ]
    if s.sitemap_found:
        lines.append(f"Sitemap: found ({s.sitemap_urls} URLs)")
    else:
        lines.append("Sitemap: not found or skipped")

    filtered = result.filtered_issues(severity_filter)
    if not filtered:
        lines.append("")
        lines.append("No issues matched the active filter.")
        return "\n".join(lines)

    headers = ["Severity", "Code", "Page", "Line", "Target", "Message"]
    rows = [
        [
            item.severity.upper(),
            item.code,
            item.page,
            str(item.line) if item.line is not None else "-",
            item.target,
            item.message,
        ]
        for item in filtered
    ]

    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            # Cap Target column display width slightly only if needed; keep full readable columns
            widths[idx] = max(widths[idx], len(cell))

    def _fmt_row(cells: list[str]) -> str:
        return " | ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(cells))

    lines.append("")
    lines.append(_fmt_row(headers))
    lines.append("-+-".join("-" * w for w in widths))
    for row in rows:
        lines.append(_fmt_row(row))

    return "\n".join(lines)


def format_json(result: AuditResult, severity_filter: str = "all") -> str:
    return json.dumps(result.to_dict(severity_filter=severity_filter), indent=2)


def format_csv(result: AuditResult, severity_filter: str = "all") -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["severity", "code", "page", "line", "target", "message"])
    for item in result.filtered_issues(severity_filter):
        writer.writerow(
            [
                item.severity,
                item.code,
                item.page,
                "" if item.line is None else str(item.line),
                item.target,
                item.message,
            ]
        )
    return buf.getvalue().rstrip("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seo_checker.py",
        description=(
            "Zero-dependency Python CLI to audit static HTML site builds for broken "
            "relative/anchor links, missing images/alt text, SEO meta issues, and sitemap orphans."
        ),
    )
    parser.add_argument(
        "site_dir",
        nargs="?",
        default=".",
        help="Path to local static site build directory (default: current directory).",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Canonical/sitemap base URL (e.g. https://example.com or https://example.com/docs).",
    )
    parser.add_argument(
        "--sitemap",
        default=None,
        help="Custom path to sitemap.xml (defaults to <site_dir>/sitemap.xml).",
    )
    parser.add_argument(
        "--no-sitemap",
        action="store_true",
        help="Disable sitemap.xml validation and orphan page checks.",
    )
    parser.add_argument(
        "--no-clean-urls",
        action="store_true",
        help="Require exact .html filenames instead of resolving extensionless clean URLs.",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Directory or filename glob pattern to exclude (repeatable).",
    )
    parser.add_argument(
        "--severity",
        choices=("all", "critical", "warning", "info"),
        default="all",
        help="Filter displayed issues by severity (does not alter score or CI exit code).",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json", "csv"),
        default="text",
        help="Output format: text (default), json, or csv.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Optional output file path instead of stdout.",
    )
    parser.add_argument(
        "--fail-on",
        choices=("none", "critical", "warning"),
        default="none",
        help="Exit with code 1 when issues at or above threshold exist (default: none).",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=None,
        help="Exit with code 1 if health score (0..100) is below this minimum.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    site_dir = Path(args.site_dir)
    if not site_dir.exists() or not site_dir.is_dir():
        print(f"Error: Site directory does not exist or is not a directory: {site_dir}", file=sys.stderr)
        return 2

    if args.min_score is not None and not (0 <= args.min_score <= 100):
        print("Error: --min-score must be between 0 and 100.", file=sys.stderr)
        return 2

    if args.base_url is not None and _normalize_base_url(args.base_url) is None:
        print(
            f"Error: Invalid --base-url '{args.base_url}'. Expected full URL like https://example.com",
            file=sys.stderr,
        )
        return 2

    sitemap_path: Optional[Path] = None
    if args.sitemap is not None:
        candidate = Path(args.sitemap)
        if not candidate.is_absolute() and not candidate.exists():
            candidate = site_dir / args.sitemap
        if not candidate.is_file():
            print(f"Error: Sitemap file not found: {args.sitemap}", file=sys.stderr)
            return 2
        sitemap_path = candidate

    result = audit_site(
        site_dir=site_dir,
        base_url=args.base_url,
        sitemap_path=sitemap_path,
        check_sitemap=not args.no_sitemap,
        excludes=args.exclude,
        clean_urls=not args.no_clean_urls,
        fail_on=args.fail_on,
        min_score=args.min_score,
    )

    if args.format == "json":
        rendered = format_json(result, severity_filter=args.severity)
    elif args.format == "csv":
        rendered = format_csv(result, severity_filter=args.severity)
    else:
        rendered = format_text(result, severity_filter=args.severity)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)

    return 0 if result.summary.policy_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
