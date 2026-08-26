#!/usr/bin/env python3
#
# wp-static-export -- SEO-aware static site exporter for WordPress (or any CMS).
# Copyright (C) 2026 huthanslhd@gmail.com
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
"""
wp-static-export -- SEO-aware static site exporter for WordPress (or any CMS).

What it does
------------
* Discovers ALL pages via robots.txt + XML sitemaps (incl. nested sitemap
  indexes from Yoast / Rank Math / WP core), not just what happens to be
  linked -- so orphan pages make it into the export too.
* Optionally also follows internal links found while crawling (on by
  default) to pick up pages that are linked but missing from the sitemap
  (paginated archives etc.).
* Downloads every same-site asset: CSS, JS, images (src / srcset /
  lazy-load attributes), fonts and images referenced via url() inside CSS
  files and inline styles, linked PDFs/uploads, favicons, og:images.
* Preserves the URL structure 1:1: /ueber-uns/ becomes /ueber-uns/index.html,
  so every public URL keeps working unchanged after deployment (no
  .html-suffix rewriting -> no SEO impact).

SEO behaviour
-------------
* robots.txt, every sitemap XML and their XSL stylesheets are part of the
  export, so the static site still exposes its sitemap to crawlers.
* By default, same-site URLs are rewritten to root-relative -- in ALL the
  forms page builders emit them: plain absolute, protocol-relative
  (//host/...), JSON-escaped (backslash-escaped slashes) in inline scripts,
  and base64-encoded lazy-load attributes (Slider Revolution data-dbsrc).
  The export is self-contained and renders 1:1 from any hostname.
  canonical / rel=alternate links, og:*/twitter:* meta tags and JSON-LD
  are intentionally left absolute (SEO data, never fetched by browsers).
* --no-rewrite instead keeps HTML pages BYTE-IDENTICAL to the server
  response (nothing re-serialized); the export then only renders correctly
  when served under the original hostname.
* Inline <script> bodies and same-site .js files are additionally scanned
  for internal URLs, so slider configs & co. get their assets exported too.
* After the export a verification pass checks that every local reference
  resolves to a file on disk and that no unexpected absolute self-reference
  remains (results in the report).
* The themed 404 page is captured as 404.html (and a soft-404 check warns
  if the site answers 200 for nonsense URLs).
* A report (report.txt / report.json) lists: fetch errors, redirects
  (incl. ready-made nginx rewrite rules), noindex'ed pages that are in the
  sitemap, canonical mismatches, and pages without <title>/meta description.

Mobile behaviour
----------------
Responsive sites (one HTML + CSS media queries) are covered automatically:
the export contains the exact same HTML/CSS a browser uses on any screen
size, srcset image variants included. To verify that assumption instead of
silently relying on it, every page is fetched a second time with an iPhone
user agent and compared (nonces/whitespace normalized; a desktop control
fetch rules out per-request randomness):

  * identical  -> responsive, the export covers mobile as-is
  * different  -> the origin does UA-based dynamic serving; the mobile HTML
                  is saved under mobile-variants/ for inspection and the
                  report explains the deployment options
  * additionally, 'Vary: User-Agent' response headers and pages without a
    <meta name="viewport"> tag (mobile usability) are reported

AMP variants (<link rel="amphtml">) are discovered and exported like normal
pages. Disable the comparison with --no-mobile-check.

Deployment helpers
------------------
nginx.conf and a Dockerfile are generated next to the export, so
`docker build` in the output directory gives you a ready-to-run container.

Usage
-----
    python3 wp-static-export.py https://www.example.at -o ./export
    python3 wp-static-export.py https://staging.example.at -o ./export \
        --clean --concurrency 8 --delay 0.2
    # server reachable only by IP (no DNS entry): connect to the IP and
    # send the site's hostname as Host header (like curl --resolve);
    # with https the certificate is verified against the --host name (SNI)
    python3 wp-static-export.py http://10.0.0.5:8080 --host example.at \
        -o ./export --clean

Dependencies: requests, beautifulsoup4
    pip install requests beautifulsoup4
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import posixpath
import queue
import random
import re
import shutil
import string
import sys
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote, quote

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TOOL_NAME = "wp-static-export"
VERSION = "1.0.1"

# --------------------------------------------------------------------------
# Classification helpers
# --------------------------------------------------------------------------

ASSET_EXTENSIONS = {
    "css", "js", "mjs", "map", "json", "xml", "xsl", "txt",
    "png", "jpg", "jpeg", "gif", "webp", "avif", "svg", "ico", "bmp",
    "woff", "woff2", "ttf", "otf", "eot",
    "mp4", "webm", "ogv", "mp3", "ogg", "wav", "m4a",
    "pdf", "zip", "gz", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "webmanifest", "wasm",
}

PAGE_SKIP_PATTERNS = re.compile(
    r"(/wp-admin(/|$)|/wp-login\.php|/wp-json(/|$)|/xmlrpc\.php"
    r"|/wp-signup\.php|/wp-activate\.php|/wp-trackback\.php"
    r"|/feed/?$|/comments/feed|/wp-cron\.php|/cart/?$|/checkout/?$)",
    re.IGNORECASE,
)

# attributes that may hold a single URL
URL_ATTRS = ("src", "href", "poster", "data-src", "data-lazy-src", "data-bg",
             "data-placeholder-image", "data-cmplz-src", "data-src-cmplz")
# attributes that hold srcset-style comma separated candidate lists
SRCSET_ATTRS = ("srcset", "data-srcset", "data-lazy-srcset")
# attributes that hold internal *page* links (theme-specific, e.g. The7's
# clickable images)
PAGE_URL_ATTRS = ("data-dt-location",)
# attributes that hold base64-encoded URLs (e.g. Slider Revolution lazy src)
B64_URL_ATTRS = ("data-dbsrc",)

CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.IGNORECASE)
CSS_IMPORT_RE = re.compile(r"@import\s+['\"]([^'\"]+)['\"]", re.IGNORECASE)
XML_STYLESHEET_RE = re.compile(r"<\?xml-stylesheet[^>]*href=[\"']([^\"']+)[\"']")
SITEMAP_LINE_RE = re.compile(r"(?im)^\s*sitemap:\s*(\S+)")

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
             "Mobile/15E148 Safari/604.1")

# strip nonce/token-like values + collapse whitespace before comparing two
# HTML responses, so WordPress CSRF nonces don't cause false "different"s
NONCE_RE = re.compile(
    rb"""(_wpnonce|nonce|csrf[\w-]*|token)(["']?\s*[:=]\s*["'])"""
    rb"""[A-Za-z0-9+/=._-]{6,}(["'])""", re.IGNORECASE)
WS_RE = re.compile(rb"\s+")


def normalize_html(data: bytes) -> bytes:
    data = NONCE_RE.sub(rb"\1\2\3", data)
    return WS_RE.sub(b" ", data).strip()


def norm_host(host: str) -> str:
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def path_extension(url_path: str) -> str:
    name = posixpath.basename(url_path)
    if "." in name:
        return name.rsplit(".", 1)[-1].lower()
    return ""


def canon_path(path: str) -> str:
    """Canonical URL path: percent-decoded, Unicode-NFC, re-quoted.

    Collapses spelling variants of the same path (%C3%A4 vs literal ä vs
    NFD a+%CC%88) into one form so crawl keys, rewritten links and on-disk
    paths all agree. RFC 3986 path characters plus {} and * stay literal:
    plugins substitute {placeholder} templates and match /path/* globs at
    runtime, which percent-encoding would break."""
    return quote(unicodedata.normalize("NFC", unquote(path)),
                 safe="/:@!$&'()*+,;={}")


def canon_ref(ref: str) -> str:
    """canon_path for a reference that may carry ?query / #fragment."""
    for sep in ("?", "#"):
        if sep in ref:
            head, tail = ref.split(sep, 1)
            return canon_path(head or "/") + sep + tail
    return canon_path(ref or "/")


def decode_cfemail(enc: str) -> str | None:
    """Decode a Cloudflare email-obfuscation hex string (XOR with first byte)."""
    try:
        data = bytes.fromhex(enc)
    except ValueError:
        return None
    if len(data) < 2:
        return None
    try:
        return bytes(b ^ data[0] for b in data[1:]).decode("utf-8")
    except UnicodeDecodeError:
        return None


def try_b64_url(val: str) -> str | None:
    """Decode a base64 attribute value if (and only if) it holds a URL."""
    if not val or not re.fullmatch(r"[A-Za-z0-9+/]{8,}={0,2}", val):
        return None
    try:
        decoded = base64.b64decode(val, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if decoded.startswith(("//", "http://", "https://", "/")):
        return decoded
    return None


class HostResolveAdapter(HTTPAdapter):
    """HTTPAdapter that pins TLS SNI / certificate verification to a fixed
    hostname while the URL targets an IP address -- the requests-level
    equivalent of `curl --resolve` (used with --host)."""

    def __init__(self, server_hostname: str | None = None, **kwargs):
        self._server_hostname = server_hostname
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        if self._server_hostname:
            kwargs["server_hostname"] = self._server_hostname
        super().init_poolmanager(connections, maxsize, block=block, **kwargs)

    def proxy_manager_for(self, proxy, **kwargs):
        if self._server_hostname:
            kwargs["server_hostname"] = self._server_hostname
        return super().proxy_manager_for(proxy, **kwargs)


# --------------------------------------------------------------------------
# Config / state
# --------------------------------------------------------------------------

@dataclass
class Config:
    base_url: str
    out_dir: Path
    rewrite: bool = True
    clean: bool = False
    follow_links: bool = True
    concurrency: int = 5
    delay: float = 0.0
    timeout: float = 25.0
    max_pages: int = 5000
    user_agent: str = f"{TOOL_NAME}/{VERSION} (+static site exporter)"
    insecure: bool = False
    host_header: str | None = None
    extra_headers: dict = field(default_factory=dict)
    port: int = 8080
    extra_sitemaps: list = field(default_factory=list)
    mobile_check: bool = True
    mobile_user_agent: str = MOBILE_UA


@dataclass
class PageRecord:
    url: str
    status: int = 0
    final_url: str = ""
    error: str = ""
    title: str = ""
    has_title: bool = False
    has_description: bool = False
    has_viewport: bool = False
    noindex: bool = False
    canonical: str = ""
    content_type: str = ""
    source: str = "sitemap"          # sitemap | link | manual
    mobile: str = ""                 # same | different | dynamic | check-failed


class Exporter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        split = urlsplit(cfg.base_url)
        self.scheme = split.scheme
        # connect target: where TCP connections actually go (may be a bare
        # IP when --host is used). Logical host: the site's hostname, used
        # for URL classification, crawl keys and rewriting.
        self.connect_scheme = split.scheme
        self.connect_netloc = split.netloc
        self.connect_norm = norm_host(split.netloc)
        self.host = cfg.host_header or split.netloc
        self.base_norm = norm_host(self.host)
        self.internal_norms = {self.base_norm}
        if cfg.host_header and self.connect_norm != self.base_norm:
            self.internal_norms.add(self.connect_norm)
        self.public_dir = cfg.out_dir / "public"

        # host-matching regexes for URL localization; cover the www. variant
        # and every textual form page builders emit: plain absolute,
        # protocol-relative (//host/...) and JSON-escaped (https:\/\/host\/...)
        hp = rf"(?:www\.)?{re.escape(self.base_norm)}"
        if len(self.internal_norms) > 1:
            # with --host, origin markup may also embed connect-address URLs
            # (e.g. staging-IP leaks in serialized slider configs)
            hp = rf"(?:{hp}|{re.escape(self.connect_norm)})"
        self.text_url_re = re.compile(
            rf"(?:https?:)?//{hp}(/[^\s'\"<>()\\]*)?", re.IGNORECASE)
        self.esc_url_re = re.compile(
            rf"(?:https?:)?\\/\\/{hp}((?:\\/[^\s'\"<>()\\]*)*)", re.IGNORECASE)
        self.host_probe_re = re.compile(
            rf"(?:https?:)?(?:\\?/){{2}}{hp}", re.IGNORECASE)

        self.session = requests.Session()
        retry = Retry(total=2, backoff_factor=0.4,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=("GET", "HEAD"))
        sni = None
        if cfg.host_header:
            sni = urlsplit("//" + cfg.host_header).hostname
            try:
                ipaddress.ip_address(sni or "")
                sni = None  # an IP is no use as SNI / certificate name
            except ValueError:
                pass
        adapter = HostResolveAdapter(server_hostname=sni,
                                     max_retries=retry,
                                     pool_connections=cfg.concurrency,
                                     pool_maxsize=cfg.concurrency * 2)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers["User-Agent"] = cfg.user_agent
        if cfg.host_header:
            # vhost routing for servers reachable only by IP (no DNS entry);
            # per-request headers (e.g. the mobile UA) merge on top
            self.session.headers["Host"] = cfg.host_header
        if cfg.extra_headers:
            # e.g. X-Forwarded-Proto: https so a WordPress origin behind an
            # HTTPS-terminating proxy serves pages instead of 301-ing to https
            self.session.headers.update(cfg.extra_headers)
        self.verify = not cfg.insecure

        # crawl state (mutated from the main thread only, between BFS rounds)
        self.page_urls_seen: set[str] = set()
        self.asset_urls_seen: set[str] = set()
        self.written_paths: set[Path] = set()
        self.write_lock = threading.Lock()

        # report data
        self.pages: list[PageRecord] = []
        self.asset_errors: list[dict] = []
        self.redirects: list[dict] = []
        self.skipped_query_urls: set[str] = set()
        self.external_hosts: set[str] = set()
        self.sitemap_files: list[str] = []
        self.warnings: list[str] = []
        self.soft_404 = False
        self.asset_count = 0
        self.mobile_diff: list[dict] = []
        self.mobile_dynamic: list[str] = []
        self.vary_ua_pages: list[str] = []
        self.amp_links: set[str] = set()
        self.verify_missing: list[dict] = []
        self.verify_unexpected: list[dict] = []
        # URLs derived from runtime URL-construction patterns (e.g. Slider
        # Revolution module lists): fetched if the origin has them, silently
        # skipped if not -- a 404 here is not an export error
        self.speculative_urls: set[str] = set()

    # -- URL helpers -------------------------------------------------------

    def is_internal(self, url: str) -> bool:
        s = urlsplit(url)
        return (s.scheme in ("http", "https")
                and norm_host(s.netloc) in self.internal_norms)

    def to_connect_url(self, url: str) -> str:
        """Map a logical-host URL to the actual connect address (--host)."""
        if not self.cfg.host_header:
            return url
        s = urlsplit(url)
        return urlunsplit((self.connect_scheme, self.connect_netloc,
                           s.path, s.query, s.fragment))

    def to_base_host(self, url: str) -> str:
        """Force the configured host/scheme so www./non-www variants collapse."""
        s = urlsplit(url)
        return urlunsplit((self.scheme, self.host, s.path or "/", s.query, ""))

    def normalize_page_url(self, url: str) -> str | None:
        """Canonical form of a page URL, or None if it should be skipped."""
        url, _ = self._defrag(url)
        s = urlsplit(url)
        if s.query:
            self.skipped_query_urls.add(url)
            return None
        path = s.path or "/"
        if PAGE_SKIP_PATTERNS.search(path):
            return None
        # never pages: Cloudflare runtime endpoints, form AJAX routes, and
        # extensionless plugin/theme directories referenced from JS configs
        if (path.startswith(("/cdn-cgi/", "/wpforms-ajax"))
                or (path.startswith(("/wp-content/", "/wp-includes/"))
                    and not path_extension(path))):
            return None
        # extensionless page URLs get a trailing slash (WordPress canonical form)
        if not path.endswith("/") and not path_extension(path):
            path += "/"
        return urlunsplit((self.scheme, self.host, canon_path(path), "", ""))

    @staticmethod
    def _defrag(url: str) -> tuple[str, str]:
        if "#" in url:
            url, frag = url.split("#", 1)
            return url, frag
        return url, ""

    def looks_like_asset(self, url: str) -> bool:
        ext = path_extension(urlsplit(url).path)
        return ext in ASSET_EXTENSIONS and ext not in ("xml",)  # sitemaps handled separately

    # -- filesystem mapping ------------------------------------------------

    def local_path_for(self, url: str, is_page: bool) -> Path | None:
        s = urlsplit(url)
        path = unicodedata.normalize("NFC", unquote(s.path)) or "/"
        if is_page:
            if path.endswith("/"):
                path += "index.html"
            elif not path_extension(path):
                path += "/index.html"
        # normalize + jail-check against directory traversal
        norm = posixpath.normpath(path).lstrip("/")
        if norm.startswith(".."):
            return None
        target = (self.public_dir / norm).resolve()
        try:
            target.relative_to(self.public_dir.resolve())
        except ValueError:
            return None
        return target

    def write_bytes(self, target: Path, data: bytes) -> None:
        """Collision-safe write. First write to a path wins (a URL can reach
        us both as a page and as an 'asset'); a target that already exists
        as a directory falls back to its index.html; conflicting ancestors
        (an extensionless file where a directory is needed, or vice versa)
        are skipped with a warning instead of crashing the crawl."""
        with self.write_lock:
            if target in self.written_paths:
                return
            if target.is_dir():
                target = target / "index.html"
                if target in self.written_paths:
                    return
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            except (FileExistsError, NotADirectoryError, IsADirectoryError) as exc:
                self.warnings.append(
                    f"path conflict, not written: {target} ({exc.__class__.__name__})")
                return
            self.written_paths.add(target)

    # -- fetching ----------------------------------------------------------

    def fetch(self, url: str, headers: dict | None = None) -> requests.Response:
        """GET with manual redirect handling: redirects are followed only
        while they stay on the target site, so the exporter NEVER sends a
        request to a foreign host. A refused (off-site) or unresolved
        redirect is returned as the final 30x response -- callers detect it
        via off_site_redirect()."""
        current = url
        hops: list[requests.Response] = []
        resp: requests.Response
        for hop in range(6):
            if self.cfg.delay:
                time.sleep(self.cfg.delay)
            resp = self.session.get(self.to_connect_url(current),
                                    timeout=self.cfg.timeout,
                                    headers=headers, verify=self.verify,
                                    allow_redirects=False)
            if self.cfg.host_header:
                resp.url = current  # history/report/callers see logical URLs
            if not (resp.is_redirect or resp.is_permanent_redirect) or hop == 5:
                break
            nxt = urljoin(current, resp.headers.get("Location") or "")
            if not nxt or not self.is_internal(nxt):
                break  # off-site or malformed redirect: stop, never follow
            hops.append(resp)
            current = nxt
        resp.history = hops
        return resp

    @staticmethod
    def off_site_redirect(resp: requests.Response) -> str | None:
        """Redirect target if the final response is a redirect that fetch()
        refused to follow (off-site or too many hops), else None."""
        if resp.is_redirect or resp.is_permanent_redirect:
            return urljoin(resp.url, resp.headers.get("Location") or "") or "?"
        return None

    # ----------------------------------------------------------------------
    # Phase 1: sitemap discovery
    # ----------------------------------------------------------------------

    def discover(self) -> list[str]:
        page_urls: list[str] = []
        sitemap_candidates: list[str] = []

        # robots.txt
        robots_url = f"{self.scheme}://{self.host}/robots.txt"
        try:
            resp = self.fetch(robots_url)
            if resp.ok and resp.content:
                target = self.local_path_for(robots_url, is_page=False)
                if target:
                    self.write_bytes(target, resp.content)
                for m in SITEMAP_LINE_RE.finditer(resp.text):
                    sitemap_candidates.append(m.group(1).strip())
                if re.search(r"(?im)^\s*disallow:\s*/\s*$", resp.text):
                    self.warnings.append(
                        "robots.txt contains 'Disallow: /' -- the site is blocked "
                        "for crawlers. Intentional?")
                print(f"[discover] robots.txt found "
                      f"({len(sitemap_candidates)} sitemap entries)")
            else:
                self.warnings.append(f"robots.txt not available (HTTP {resp.status_code})")
        except requests.RequestException as exc:
            self.warnings.append(f"robots.txt fetch failed: {exc}")

        # well-known fallbacks (Yoast/RankMath, generic, WP core)
        for cand in ("/sitemap_index.xml", "/sitemap.xml", "/wp-sitemap.xml"):
            sitemap_candidates.append(f"{self.scheme}://{self.host}{cand}")
        sitemap_candidates += self.cfg.extra_sitemaps

        seen_sitemaps: set[str] = set()
        found_any = False
        for sm in sitemap_candidates:
            if self.is_internal(sm):
                sm = self.to_base_host(sm)
            elif sm not in self.cfg.extra_sitemaps:
                # robots.txt may declare sitemaps on foreign hosts -- the
                # exporter only ever talks to the target site (explicit
                # --extra-sitemap entries are the user's own decision)
                self.warnings.append(
                    f"foreign-host sitemap declared in robots.txt, "
                    f"skipped: {sm}")
                continue
            if sm in seen_sitemaps:
                continue
            urls = self.walk_sitemap(sm, seen_sitemaps, depth=0)
            if urls:
                found_any = True
                page_urls.extend(urls)
            if found_any and sm.endswith(("/sitemap_index.xml", "/sitemap.xml",
                                          "/wp-sitemap.xml")):
                # one working well-known entry point is enough; robots-declared
                # and --extra-sitemap entries are always processed
                remaining = [c for c in sitemap_candidates
                             if c in self.cfg.extra_sitemaps and c not in seen_sitemaps]
                for extra in remaining:
                    page_urls.extend(self.walk_sitemap(extra, seen_sitemaps, depth=0))
                break

        if not found_any:
            self.warnings.append(
                "No usable XML sitemap found -- falling back to link crawling "
                "from the homepage. Orphan pages will be missing.")
            page_urls.append(f"{self.scheme}://{self.host}/")
        return page_urls

    def walk_sitemap(self, url: str, seen: set[str], depth: int) -> list[str]:
        if url in seen or depth > 5 or len(seen) > 200:
            return []
        seen.add(url)
        try:
            resp = self.fetch(url)
        except requests.RequestException as exc:
            self.warnings.append(f"sitemap fetch failed: {url} ({exc})")
            return []
        if resp.status_code != 200 or not resp.content.strip():
            return []
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            return []
        if local_name(root.tag) not in ("sitemapindex", "urlset"):
            return []

        # keep the sitemap file itself in the export
        data = resp.content
        if self.cfg.rewrite:
            # localize the xml-stylesheet PI so the sitemap renders without
            # the origin; <loc> entries deliberately stay absolute (SEO)
            pi = XML_STYLESHEET_RE.search(data[:2048].decode("utf-8", "replace"))
            if pi:
                xsl_abs = urljoin(url, pi.group(1))
                if self.is_internal(xsl_abs):
                    local = self.localize_url(self.to_base_host(xsl_abs))
                    data = data.replace(pi.group(1).encode("utf-8"),
                                        local.encode("utf-8"), 1)
        target = self.local_path_for(url, is_page=False)
        if target:
            self.write_bytes(target, data)
            self.sitemap_files.append(urlsplit(url).path)

        # XSL stylesheet referenced via processing instruction
        head = resp.content[:2048].decode("utf-8", "replace")
        for m in XML_STYLESHEET_RE.finditer(head):
            xsl = urljoin(url, m.group(1))
            if self.is_internal(xsl):
                self.asset_urls_seen.add(self.to_base_host(xsl))

        urls: list[str] = []
        if local_name(root.tag) == "sitemapindex":
            for sm in root:
                loc = next((c.text for c in sm if local_name(c.tag) == "loc"), None)
                if not loc:
                    continue
                loc = loc.strip()
                if self.is_internal(loc):
                    urls.extend(self.walk_sitemap(loc, seen, depth + 1))
                else:
                    self.warnings.append(
                        f"sitemap index lists foreign-host sitemap, "
                        f"skipped: {loc}")
        else:  # urlset
            count = 0
            for u in root:
                loc = next((c.text for c in u if local_name(c.tag) == "loc"), None)
                if not loc:
                    continue
                loc = loc.strip()
                if self.is_internal(loc):
                    urls.append(loc)
                    count += 1
                else:
                    self.warnings.append(
                        f"sitemap {urlsplit(url).path} lists foreign URL, "
                        f"skipped: {loc}")
            print(f"[discover] {urlsplit(url).path}: {count} URLs")
        return urls

    # ----------------------------------------------------------------------
    # Phase 2: crawl
    # ----------------------------------------------------------------------

    def crawl(self, seed_pages: list[str]) -> None:
        pending_pages: list[tuple[str, str]] = []
        for u in seed_pages:
            n = self.normalize_page_url(u)
            if n and n not in self.page_urls_seen:
                self.page_urls_seen.add(n)
                pending_pages.append((n, "sitemap"))

        pending_assets: list[str] = list(self.asset_urls_seen)
        round_no = 0

        def safe_page(t: tuple[str, str]) -> tuple[list[str], list[str]]:
            try:
                return self.process_page(*t)
            except Exception as exc:            # noqa: BLE001 -- keep crawling
                self.warnings.append(f"unexpected error on page {t[0]}: {exc!r}")
                return [], []

        def safe_asset(u: str) -> tuple[list[str], list[str]]:
            try:
                return self.process_asset(u)
            except Exception as exc:            # noqa: BLE001 -- keep crawling
                self.asset_errors.append({"url": u, "error": f"unexpected: {exc!r}"})
                return [], []

        with ThreadPoolExecutor(max_workers=self.cfg.concurrency) as pool:
            while pending_pages or pending_assets:
                round_no += 1
                print(f"[crawl] round {round_no}: {len(pending_pages)} pages, "
                      f"{len(pending_assets)} assets")
                page_results = list(pool.map(safe_page, pending_pages))
                asset_results = list(pool.map(safe_asset, pending_assets))
                pending_pages, pending_assets = [], []

                for new_pages, new_assets in page_results:
                    for u in new_assets:
                        if u not in self.asset_urls_seen:
                            self.asset_urls_seen.add(u)
                            pending_assets.append(u)
                    if not self.cfg.follow_links:
                        continue
                    for u in new_pages:
                        if len(self.page_urls_seen) >= self.cfg.max_pages:
                            break
                        if u not in self.page_urls_seen:
                            self.page_urls_seen.add(u)
                            pending_pages.append((u, "link"))
                for new_pages, new_assets in asset_results:
                    for u in new_assets:
                        if u not in self.asset_urls_seen:
                            self.asset_urls_seen.add(u)
                            pending_assets.append(u)
                    if not self.cfg.follow_links:
                        continue
                    for u in new_pages:
                        if len(self.page_urls_seen) >= self.cfg.max_pages:
                            break
                        if u not in self.page_urls_seen:
                            self.page_urls_seen.add(u)
                            pending_pages.append((u, "link"))

        if len(self.page_urls_seen) >= self.cfg.max_pages:
            self.warnings.append(
                f"--max-pages ({self.cfg.max_pages}) reached, crawl truncated.")

    # -- page processing ---------------------------------------------------

    def process_page(self, url: str, source: str) -> tuple[list[str], list[str]]:
        rec = PageRecord(url=url, source=source)
        self.pages.append(rec)
        try:
            resp = self.fetch(url)
        except requests.RequestException as exc:
            rec.error = str(exc)
            return [], []

        rec.status = resp.status_code
        rec.final_url = resp.url
        rec.content_type = (resp.headers.get("Content-Type") or "").split(";")[0]

        if resp.history:
            self.redirects.append({
                "from": url, "to": resp.url,
                "status": resp.history[0].status_code})
        off_site = self.off_site_redirect(resp)
        if off_site:
            if self.is_internal(off_site):
                rec.error = (f"redirect loop / too many hops (last target "
                             f"{off_site}); if the origin forces HTTPS, pass "
                             f"--header 'X-Forwarded-Proto: https'")
            else:
                rec.error = f"redirect not followed off-site (to {off_site})"
                host = urlsplit(off_site).netloc
                if host:
                    self.external_hosts.add(host)
            self.redirects.append({"from": url, "to": off_site,
                                   "status": resp.status_code})
            return [], []
        if resp.status_code == 404:
            # Unicode edge case: our canonical (NFC) spelling can 404 when the
            # CMS stores the slug decomposed (NFD) -- retry once with NFD, but
            # keep saving under the NFC path
            s = urlsplit(url)
            nfd_path = quote(unicodedata.normalize("NFD", unquote(s.path)), safe="/")
            if nfd_path != s.path:
                try:
                    retry = self.fetch(urlunsplit((s.scheme, s.netloc, nfd_path, "", "")))
                except requests.RequestException:
                    retry = None
                if retry is not None and retry.ok:
                    resp = retry
                    rec.status = resp.status_code
                    rec.final_url = resp.url
                    rec.content_type = (
                        resp.headers.get("Content-Type") or "").split(";")[0]
        if not resp.ok:
            rec.error = f"HTTP {resp.status_code}"
            return [], []
        if not self.is_internal(resp.url):
            rec.error = f"redirected off-site to {resp.url}"
            return [], []

        # non-HTML target (e.g. a PDF listed in the sitemap)
        if "html" not in rec.content_type:
            target = self.local_path_for(self.to_base_host(resp.url), is_page=False)
            if target:
                self.write_bytes(target, resp.content)
            return [], []

        if "user-agent" in (resp.headers.get("Vary") or "").lower():
            self.vary_ua_pages.append(url)

        save_url = self.normalize_page_url(self.to_base_host(resp.url)) or url
        if resp.history and self.cfg.rewrite and save_url != url:
            # keep redirect sources working in the static mirror: a
            # meta-refresh stub at the original path points to the target
            src_target = self.local_path_for(url, is_page=True)
            if src_target:
                dest = self.localize_url(save_url)
                stub = ('<!doctype html><html><head><meta charset="utf-8">'
                        f'<meta http-equiv="refresh" content="0;url={dest}">'
                        f'<link rel="canonical" href="{save_url}">'
                        '<title>Redirect</title></head>'
                        f'<body><a href="{dest}">{dest}</a></body></html>')
                self.write_bytes(src_target, stub.encode("utf-8"))
        new_pages, new_assets = self.parse_and_save_html(
            save_url, resp.content, rec)
        if self.cfg.mobile_check:
            self.check_mobile_variant(save_url, resp.content, rec)
        return new_pages, new_assets

    def check_mobile_variant(self, url: str, desktop_raw: bytes,
                             rec: PageRecord) -> None:
        """Fetch the page with a mobile UA and compare against the desktop
        response. A second desktop fetch distinguishes UA-based dynamic
        serving from per-request randomness."""
        try:
            mresp = self.fetch(url, headers={"User-Agent": self.cfg.mobile_user_agent})
        except requests.RequestException as exc:
            rec.mobile = "check-failed"
            self.warnings.append(f"mobile check failed for {url}: {exc}")
            return
        if not mresp.ok or "html" not in (mresp.headers.get("Content-Type") or ""):
            rec.mobile = "check-failed"
            return
        if normalize_html(mresp.content) == normalize_html(desktop_raw):
            rec.mobile = "same"
            return
        # control fetch: does a second *desktop* request differ as well?
        try:
            dresp = self.fetch(url)
        except requests.RequestException:
            rec.mobile = "check-failed"
            return
        if dresp.ok and normalize_html(dresp.content) != normalize_html(desktop_raw):
            rec.mobile = "dynamic"
            self.mobile_dynamic.append(url)
            return
        rec.mobile = "different"
        target = self.local_path_for(url, is_page=True)
        variant_rel = ""
        if target:
            rel = target.relative_to(self.public_dir.resolve())
            variant = self.cfg.out_dir / "mobile-variants" / rel
            variant.parent.mkdir(parents=True, exist_ok=True)
            variant.write_bytes(mresp.content)
            variant_rel = str(Path("mobile-variants") / rel)
        self.mobile_diff.append({"url": url, "variant": variant_rel})

    def parse_and_save_html(self, page_url: str, raw: bytes,
                            rec: PageRecord | None,
                            save_as: Path | None = None) -> tuple[list[str], list[str]]:
        soup = BeautifulSoup(raw, "html.parser")
        new_pages: list[str] = []
        new_assets: list[str] = []

        def note_external(u: str) -> None:
            host = urlsplit(u).netloc
            if host:
                self.external_hosts.add(host)

        def handle_candidate(u: str, tag_name: str, rel: set[str]) -> None:
            u = u.strip()
            if not u or u.startswith(("data:", "mailto:", "tel:", "javascript:", "#")):
                return
            absolute = urljoin(page_url, u)
            absolute, _ = self._defrag(absolute)
            if not self.is_internal(absolute):
                note_external(absolute)
                return
            absolute = self.to_base_host(absolute)
            if tag_name == "a" and not self.looks_like_asset(absolute):
                n = self.normalize_page_url(absolute)
                if n:
                    new_pages.append(n)
            elif tag_name == "link" and "amphtml" in rel:
                n = self.normalize_page_url(absolute)
                if n:
                    new_pages.append(n)
                    self.amp_links.add(n)
                elif "?" in u:
                    self.warnings.append(
                        f"AMP variant uses a query-string URL and was skipped: {absolute}")
            elif tag_name == "link" and rel & {"canonical", "alternate", "next",
                                               "prev", "shortlink", "pingback"}:
                return  # SEO/meta links, never fetched as assets
            else:
                if PAGE_SKIP_PATTERNS.search(urlsplit(absolute).path):
                    return  # dynamic endpoints (wp-json, xmlrpc, ...) --
                            # useless statically, and saving them as files
                            # blocks same-named directories
                new_assets.append(absolute)

        handled_attrs = (set(URL_ATTRS) | set(SRCSET_ATTRS) | set(PAGE_URL_ATTRS)
                         | set(B64_URL_ATTRS) | {"style", "rel", "class", "id"})

        for tag in soup.find_all(True):
            rel = set()
            rel_attr = tag.get("rel")
            if rel_attr:
                rel = {r.lower() for r in
                       (rel_attr if isinstance(rel_attr, list) else rel_attr.split())}
            for attr in URL_ATTRS:
                val = tag.get(attr)
                if not val:
                    continue
                if attr == "data-bg" and "url(" in val:
                    # some themes store a full CSS declaration in data-bg
                    for m in CSS_URL_RE.finditer(val):
                        handle_candidate(m.group(1), "style", set())
                else:
                    handle_candidate(val, tag.name, rel)
            for attr in SRCSET_ATTRS:
                val = tag.get(attr)
                if val:
                    for cand in val.split(","):
                        cand = cand.strip().split(" ")[0]
                        if cand:
                            handle_candidate(cand, "img", rel)
            for attr in PAGE_URL_ATTRS:
                val = tag.get(attr)
                if val:
                    handle_candidate(val, "a", rel)
            for attr in B64_URL_ATTRS:
                decoded = try_b64_url(tag.get(attr))
                if decoded:
                    handle_candidate(decoded, "img", rel)
            style_attr = tag.get("style")
            if style_attr:
                for m in CSS_URL_RE.finditer(style_attr):
                    handle_candidate(m.group(1), "style", set())
            # safety net: any other attribute mentioning the site host
            # (theme/plugin-specific data-* attrs)
            if tag.name != "meta":
                for attr, val in tag.attrs.items():
                    if attr in handled_attrs or not isinstance(val, str):
                        continue
                    pages, assets = self.scan_text_for_urls(val)
                    new_pages.extend(pages)
                    new_assets.extend(assets)

        # og:image / twitter:image -> download, tag stays untouched
        for meta in soup.find_all("meta"):
            key = (meta.get("property") or meta.get("name") or "").lower()
            if key in ("og:image", "og:image:url", "twitter:image"):
                val = meta.get("content")
                if val:
                    handle_candidate(val, "meta-img", set())

        # inline <style> blocks
        for style in soup.find_all("style"):
            css = style.string or ""
            for m in CSS_URL_RE.finditer(css):
                handle_candidate(m.group(1), "style", set())
            for m in CSS_IMPORT_RE.finditer(css):
                handle_candidate(m.group(1), "style", set())

        # inline <script> bodies: slider configs & co. reference images, CSS
        # and even pages that appear nowhere else in the markup
        for script in soup.find_all("script"):
            if script.string:
                pages, assets = self.scan_text_for_urls(script.string)
                new_pages.extend(pages)
                new_assets.extend(assets)

        # SEO signals for the report
        if rec is not None:
            title = soup.find("title")
            rec.has_title = bool(title and title.get_text(strip=True))
            rec.title = title.get_text(strip=True) if rec.has_title else ""
            desc = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
            rec.has_description = bool(desc and (desc.get("content") or "").strip())
            robots = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
            if robots and "noindex" in (robots.get("content") or "").lower():
                rec.noindex = True
            canonical = soup.find("link", rel=lambda v: v and "canonical" in v)
            if canonical and canonical.get("href"):
                rec.canonical = canonical["href"].strip()
            viewport = soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)})
            rec.has_viewport = bool(viewport and (viewport.get("content") or "").strip())

        # write file
        target = save_as or self.local_path_for(page_url, is_page=True)
        if target:
            if self.cfg.rewrite:
                self.rewrite_soup_relative(soup)
                self.write_bytes(target, str(soup).encode("utf-8"))
            else:
                self.write_bytes(target, raw)  # byte-identical to the origin
        return new_pages, new_assets

    # -- URL discovery in JS/JSON text -------------------------------------

    def scan_text_for_urls(self, text: str) -> tuple[list[str], list[str]]:
        """Conservative sweep of JS/JSON/attribute text for internal URLs
        (plain, protocol-relative and JSON-escaped forms).

        Returns (page_urls, asset_urls) ready for the crawl queues."""
        pages: list[str] = []
        assets: list[str] = []
        if not text or not self.host_probe_re.search(text):
            return pages, assets
        work = text.replace("\\/", "/")
        # Complianz builds its banner CSS URL from a template at runtime;
        # resolve the placeholders from the config values in the same text
        if "{banner_id}" in work:
            m_id = re.search(r'"user_banner_id"\s*:\s*"?(\d+)', work)
            m_type = re.search(r'"consenttype"\s*:\s*"?(\w+)', work)
            if m_id and m_type:
                work = (work.replace("{banner_id}", m_id.group(1))
                            .replace("{type}", m_type.group(1)))
        # Slider Revolution 7 constructs its runtime resource URLs from
        # SR7.E.plugin_url plus module/lib/css name lists (sr7.js:
        # plugin_url+"public/js/"+name+".js" etc.). The CSS ones are loaded
        # lazily by every slider; without them checkResources() never
        # resolves and the slider stays blank. Queue all candidates
        # speculatively -- names satisfied inline by the sr7.js bundle 404
        # on the origin too and are skipped silently.
        m_pu = re.search(r"SR7\.E\.plugin_url\s*=\s*['\"]([^'\"]+)['\"]", work)
        if m_pu and "SR7.E.modules" in work:
            base = urlsplit(urljoin(f"{self.scheme}://{self.host}/",
                                    m_pu.group(1))).path
            candidates: list[str] = []
            def names(var: str) -> list[str]:
                m = re.search(rf"SR7\.E\.{var}\s*=\s*\[([^\]]*)\]", work)
                return re.findall(r"['\"]([\w-]+)['\"]", m.group(1)) if m else []
            candidates += [f"public/js/{n}.js" for n in names("modules")]
            candidates += [f"public/js/libs/{n.lower()}.js" for n in names("libs")]
            candidates += [f"public/css/{n.replace('css', 'sr7.')}.css"
                           for n in names("css")]
            for flag, css in (
                    ("FontAwesome", "public/css/fonts/font-awesome/css/font-awesome.css"),
                    ("Materialicons", "public/css/fonts/material/material-icons.css"),
                    ("PeIcon", "public/css/fonts/pe-icon-7-stroke/css/pe-icon-7-stroke.css"),
                    ("RevIcon", "public/css/fonts/revicons/css/revicons.css")):
                if f'"{flag}":true' in work:
                    candidates.append(css)
            for cand in candidates:
                absolute = (f"{self.scheme}://{self.host}"
                            f"{posixpath.join(base, cand)}")
                self.speculative_urls.add(absolute)
                assets.append(absolute)
        for m in self.text_url_re.finditer(work):
            ref = (m.group(1) or "/").rstrip(".,;:")
            if "{" in ref or "}" in ref or "*" in ref:
                continue  # template placeholder / glob pattern, not a real URL
            absolute = f"{self.scheme}://{self.host}{ref}"
            s = urlsplit(absolute)
            if PAGE_SKIP_PATTERNS.search(s.path):
                continue
            if self.looks_like_asset(absolute):
                assets.append(absolute)
            elif not s.query:
                n = self.normalize_page_url(absolute)
                if n:
                    pages.append(n)
        return pages, assets

    # -- URL localization (rewrite mode) -----------------------------------

    def localize_url(self, u: str) -> str:
        """Same-site absolute or protocol-relative URL -> root-relative,
        percent-encoding and Unicode form normalized (NFC)."""
        cand = ("https:" + u) if u.startswith("//") else u
        if not self.is_internal(cand):
            return u
        s = urlsplit(cand)
        rel = canon_path(s.path or "/")
        if s.query:
            rel += "?" + s.query
        if s.fragment:
            rel += "#" + s.fragment
        return rel

    def localize_text(self, text: str) -> str:
        """Host-strip every textual URL form (plain, protocol-relative,
        JSON-escaped) inside JS/CSS/JSON text."""
        def plain(m: re.Match) -> str:
            return canon_ref(m.group(1) or "/")

        def escaped(m: re.Match) -> str:
            ref = (m.group(1) or "").replace("\\/", "/")
            return canon_ref(ref or "/").replace("/", "\\/")

        return self.esc_url_re.sub(escaped, self.text_url_re.sub(plain, text))

    def localize_b64(self, val: str) -> str:
        decoded = try_b64_url(val)
        if decoded is None:
            return val
        localized = self.localize_url(decoded)
        if localized == decoded:
            return val
        return base64.b64encode(localized.encode("utf-8")).decode("ascii")

    def rewrite_soup_relative(self, soup: BeautifulSoup) -> None:
        """Rewrite same-site URLs to root-relative -- in every form page
        builders emit them: plain absolute, protocol-relative, JSON-escaped
        inside inline scripts/attributes, base64-encoded lazy-load attrs.

        canonical / rel=alternate / wp-json <link>s, <meta> tags (og:*,
        twitter:*) and JSON-LD stay absolute on purpose: SEO data, never
        fetched by browsers."""
        for tag in soup.find_all(True):
            if tag.name == "meta":
                continue
            rel_attr = tag.get("rel")
            rel = {r.lower() for r in (rel_attr if isinstance(rel_attr, list)
                                       else (rel_attr or "").split())}
            if tag.name == "link" and (
                    rel & {"canonical", "alternate"}
                    or PAGE_SKIP_PATTERNS.search(
                        urlsplit(tag.get("href") or "").path)):
                continue  # SEO links and dynamic-endpoint metadata (wp-json,
                          # xmlrpc EditURI/pingback) stay absolute: browsers
                          # never fetch them, and localizing them would just
                          # create dead same-origin links
            for attr, val in list(tag.attrs.items()):
                if not isinstance(val, str) or not val:
                    continue
                if attr in SRCSET_ATTRS:
                    parts = []
                    for cand in val.split(","):
                        cand = cand.strip()
                        if not cand:
                            continue
                        bits = cand.split(" ", 1)
                        bits[0] = self.localize_url(bits[0])
                        parts.append(" ".join(bits))
                    tag[attr] = ", ".join(parts)
                elif attr == "style" or (attr == "data-bg" and "url(" in val):
                    tag[attr] = CSS_URL_RE.sub(
                        lambda m: f"url('{self.localize_url(m.group(1))}')", val)
                elif attr in B64_URL_ATTRS:
                    tag[attr] = self.localize_b64(val)
                elif attr in URL_ATTRS or attr in PAGE_URL_ATTRS:
                    tag[attr] = self.localize_url(val)
                elif self.host_probe_re.search(val):
                    tag[attr] = self.localize_text(val)
        # Cloudflare email obfuscation: pre-decode addresses so email links
        # work on the static site even without the runtime decode script
        # (whose /cdn-cgi/l/email-protection backend does not exist here)
        for tag in soup.find_all(attrs={"data-cfemail": True}):
            email = decode_cfemail(tag["data-cfemail"])
            if not email:
                continue
            tag.string = email
            del tag["data-cfemail"]
            classes = [c for c in (tag.get("class") or []) if c != "__cf_email__"]
            if classes:
                tag["class"] = classes
            elif tag.get("class") is not None:
                del tag["class"]
            if tag.name == "a" and "email-protection" in (tag.get("href") or ""):
                tag["href"] = "mailto:" + email
        for a in soup.find_all("a", href=True):
            if "/cdn-cgi/l/email-protection#" in a["href"]:
                email = decode_cfemail(a["href"].split("#", 1)[1])
                if email:
                    a["href"] = "mailto:" + email
        for style in soup.find_all("style"):
            if style.string and self.host_probe_re.search(style.string):
                style.string = self.localize_text(style.string)
        for script in soup.find_all("script"):
            stype = (script.get("type") or "").lower()
            if "ld+json" in stype:
                continue  # SEO structured data stays absolute
            if script.string and self.host_probe_re.search(script.string):
                script.string = self.localize_text(script.string)

    # -- asset processing --------------------------------------------------

    def process_asset(self, url: str) -> tuple[list[str], list[str]]:
        if PAGE_SKIP_PATTERNS.search(urlsplit(url).path):
            return [], []  # dynamic endpoint, useless statically
        try:
            resp = self.fetch(url)
        except requests.RequestException as exc:
            self.asset_errors.append({"url": url, "error": str(exc)})
            return [], []
        off_site = self.off_site_redirect(resp)
        if off_site:
            if url not in self.speculative_urls:
                self.asset_errors.append(
                    {"url": url,
                     "error": f"redirect not followed (to {off_site})"})
            if not self.is_internal(off_site):
                host = urlsplit(off_site).netloc
                if host:
                    self.external_hosts.add(host)
            return [], []
        if not resp.ok:
            if url not in self.speculative_urls:
                self.asset_errors.append(
                    {"url": url, "error": f"HTTP {resp.status_code}"})
            return [], []
        if not self.is_internal(resp.url):
            self.asset_errors.append({"url": url,
                                      "error": f"redirected off-site to {resp.url}"})
            return [], []

        discovered_pages: list[str] = []
        discovered: list[str] = []
        ext = path_extension(urlsplit(url).path)
        ctype = (resp.headers.get("Content-Type") or "").lower()
        data = resp.content

        if "html" in ctype and ext and ext not in ("html", "htm"):
            # the origin answered an asset URL (.png, .css, ...) with an HTML
            # page -- the real file doesn't exist there (broken on the origin
            # too); never save HTML bytes under an asset filename
            self.asset_errors.append({
                "url": url,
                "error": f"origin returned HTML for .{ext} asset URL "
                         f"(resource broken on the origin site as well)"})
            return [], []

        if "html" in ctype and not ext:
            # extensionless URL answering with HTML: that's a page (typically
            # a WordPress *attachment page* referenced in an asset context,
            # e.g. <img src="/home/bg-boxanschrift">). Store it under its
            # page path (.../index.html) instead of colliding with the
            # directory the page crawl creates for the same URL.
            target = self.local_path_for(self.to_base_host(resp.url), is_page=True)
            if target:
                self.write_bytes(target, data)
            return [], []

        if ext == "css" or "text/css" in ctype:
            text = resp.text
            for m in list(CSS_URL_RE.finditer(text)) + list(CSS_IMPORT_RE.finditer(text)):
                ref = m.group(1).strip()
                if ref.startswith(("data:", "#")):
                    continue
                absolute = urljoin(url, ref)
                absolute, _ = self._defrag(absolute)
                if self.is_internal(absolute):
                    discovered.append(self.to_base_host(absolute))
                else:
                    self.external_hosts.add(urlsplit(absolute).netloc)
            if self.cfg.rewrite:
                def rel(m: re.Match) -> str:
                    u = m.group(1).strip()
                    if u.startswith(("data:", "#")):
                        return m.group(0)
                    absolute = urljoin(url, u)
                    if self.is_internal(absolute):
                        return f"url('{self.localize_url(absolute)}')"
                    return m.group(0)

                def rel_import(m: re.Match) -> str:
                    absolute = urljoin(url, m.group(1).strip())
                    if self.is_internal(absolute):
                        return f"@import '{self.localize_url(absolute)}'"
                    return m.group(0)

                data = CSS_IMPORT_RE.sub(
                    rel_import, CSS_URL_RE.sub(rel, text)).encode("utf-8")
        elif ext in ("js", "mjs") or "javascript" in ctype:
            text = resp.text
            page_refs, asset_refs = self.scan_text_for_urls(text)
            discovered_pages.extend(page_refs)
            discovered.extend(asset_refs)
            if self.cfg.rewrite and self.host_probe_re.search(text):
                data = self.localize_text(text).encode("utf-8")

        target = self.local_path_for(url, is_page=False)
        if target:
            self.write_bytes(target, data)
            self.asset_count += 1
        return discovered_pages, discovered

    # ----------------------------------------------------------------------
    # Phase 3: extras -- 404 page, favicon
    # ----------------------------------------------------------------------

    def capture_404(self) -> None:
        token = "".join(random.choices(string.ascii_lowercase + string.digits, k=18))
        probe = f"{self.scheme}://{self.host}/{TOOL_NAME}-404-probe-{token}/"
        try:
            resp = self.fetch(probe)
        except requests.RequestException as exc:
            self.warnings.append(f"404 probe failed: {exc}")
            return
        if resp.status_code == 404:
            if "html" in (resp.headers.get("Content-Type") or ""):
                # parse it too, so the 404 page's own assets are exported
                self.parse_and_save_html(
                    f"{self.scheme}://{self.host}/404.html", resp.content,
                    rec=None, save_as=self.public_dir / "404.html")
                print("[extras] 404 page saved as 404.html")
        elif resp.ok:
            self.soft_404 = True
            self.warnings.append(
                "Site answers HTTP 200 for a nonsense URL (soft 404) -- "
                "no 404.html captured; check the theme/SEO setup.")

    def capture_favicon(self) -> None:
        url = f"{self.scheme}://{self.host}/favicon.ico"
        if url in self.asset_urls_seen:
            return
        try:
            resp = self.fetch(url)
            if resp.ok and resp.content and not self.off_site_redirect(resp):
                target = self.local_path_for(url, is_page=False)
                if target and target not in self.written_paths:
                    self.write_bytes(target, resp.content)
        except requests.RequestException:
            pass

    # ----------------------------------------------------------------------
    # Phase 3b: post-export verification
    # ----------------------------------------------------------------------

    def verify_export(self) -> None:
        """Machine-check the '1:1 and self-contained' claim: every local
        reference in the final HTML/CSS must resolve to a file on disk, and
        no unexpected absolute self-reference may remain (canonical /
        rel=alternate / wp-json <link>s, <meta> tags and JSON-LD are
        intentional). Results land in the report."""
        # the homepage MUST land at the web root, else the served site 404s /
        # nginx reports "directory index forbidden" for GET /
        if not (self.public_dir / "index.html").is_file():
            self.verify_unexpected.append(
                {"file": "index.html",
                 "ref": "MISSING homepage at web root -- GET / will not work"})
        # policy check: no dynamic-endpoint artifacts may exist in the export
        for pat in ("wp-admin*", "wp-login*", "wp-json*", "xmlrpc*"):
            for hit in self.public_dir.glob(pat):
                self.verify_unexpected.append(
                    {"file": hit.relative_to(self.public_dir).as_posix(),
                     "ref": "dynamic endpoint artifact in export"})
        if not self.cfg.rewrite:
            return
        origin_404 = {urlsplit(a["url"]).path for a in self.asset_errors}
        missing_seen: set[tuple[str, str]] = set()
        unexpected_seen: set[tuple[str, str]] = set()

        def note_unexpected(rel_file: str, ref: str) -> None:
            key = (rel_file, ref)
            if key not in unexpected_seen:
                unexpected_seen.add(key)
                self.verify_unexpected.append({"file": rel_file, "ref": ref})

        def exists_local(ref: str) -> bool:
            path = ref.split("#", 1)[0].split("?", 1)[0]
            path = unicodedata.normalize("NFC", unquote(path))
            target = self.public_dir / path.lstrip("/")
            if target.is_file():
                return True
            if path.endswith("/") or not path_extension(path):
                # page URL, or a directory base path from a JS config var
                # (plugin_url & co.) that scripts join file names onto
                return (target / "index.html").is_file() or target.is_dir()
            return False

        def check_ref(rel_file: str, ref: str) -> None:
            ref = ref.strip()
            if not ref.startswith("/") or ref.startswith("//"):
                return  # relative or external -- not checked here
            if ("{" in ref or "*" in ref
                    or PAGE_SKIP_PATTERNS.search(ref.split("?")[0])):
                return  # runtime template / glob pattern / dynamic endpoint
            if ref.startswith("/cdn-cgi/l/email-protection"):
                return  # decoded to mailto: at runtime, never fetched
            if exists_local(ref):
                return
            key = (rel_file, ref)
            if key in missing_seen:
                return
            missing_seen.add(key)
            self.verify_missing.append({
                "file": rel_file, "ref": ref,
                "origin_error": ref.split("?")[0] in origin_404})

        for html_file in sorted(self.public_dir.rglob("*.html")):
            rel_file = html_file.relative_to(self.public_dir).as_posix()
            soup = BeautifulSoup(html_file.read_bytes(), "html.parser")
            for tag in soup.find_all(True):
                if tag.name == "meta":
                    continue
                rel_attr = tag.get("rel")
                rel = {r.lower() for r in (rel_attr if isinstance(rel_attr, list)
                                           else (rel_attr or "").split())}
                allowed_abs = tag.name == "link" and (
                    rel & {"canonical", "alternate"}
                    or PAGE_SKIP_PATTERNS.search(
                        urlsplit(tag.get("href") or "").path))
                for attr, val in tag.attrs.items():
                    if not isinstance(val, str) or not val:
                        continue
                    if not allowed_abs and self.host_probe_re.search(val):
                        note_unexpected(rel_file, f"<{tag.name} {attr}=...>")
                    if attr in SRCSET_ATTRS:
                        for cand in val.split(","):
                            cand = cand.strip().split(" ")[0]
                            if cand:
                                check_ref(rel_file, cand)
                    elif attr == "style" or (attr == "data-bg" and "url(" in val):
                        for m in CSS_URL_RE.finditer(val):
                            check_ref(rel_file, m.group(1))
                    elif attr in URL_ATTRS or attr in PAGE_URL_ATTRS:
                        check_ref(rel_file, val)
            for style in soup.find_all("style"):
                if not style.string:
                    continue
                if self.host_probe_re.search(style.string):
                    note_unexpected(rel_file, "<style> block")
                for m in CSS_URL_RE.finditer(style.string):
                    check_ref(rel_file, m.group(1))
            for script in soup.find_all("script"):
                stype = (script.get("type") or "").lower()
                if "ld+json" in stype or not script.string:
                    continue
                if self.host_probe_re.search(script.string):
                    note_unexpected(rel_file, "<script> block")
                text = script.string.replace("\\/", "/")
                for ref in re.findall(
                        r"""["'](/(?:wp-content|wp-includes|cf-fonts|cdn-cgi)"""
                        r"""/[^"'\s\\]+?)["'\\]""", text):
                    check_ref(rel_file, ref)

        for css_file in sorted(self.public_dir.rglob("*.css")):
            rel_file = css_file.relative_to(self.public_dir).as_posix()
            try:
                text = css_file.read_text("utf-8", errors="replace")
            except OSError:
                continue
            if self.host_probe_re.search(text):
                note_unexpected(rel_file, "absolute origin URL in CSS")
            css_dir = "/" + posixpath.dirname(rel_file)
            for m in list(CSS_URL_RE.finditer(text)) + list(CSS_IMPORT_RE.finditer(text)):
                ref = m.group(1).strip()
                if ref.startswith(("data:", "#", "//")) or "://" in ref:
                    continue
                if not ref.startswith("/"):
                    ref = posixpath.normpath(posixpath.join(css_dir, ref))
                check_ref(rel_file, ref)

    # ----------------------------------------------------------------------
    # Phase 4: deployment files + report
    # ----------------------------------------------------------------------

    def write_deploy_files(self) -> None:
        rewrites = ""
        internal_redirects = [
            r for r in self.redirects
            if self.is_internal(r["to"]) and urlsplit(r["from"]).path != urlsplit(r["to"]).path
        ]
        if internal_redirects:
            lines = [f"    rewrite ^{re.escape(urlsplit(r['from']).path)}$ "
                     f"{urlsplit(r['to']).path} permanent;"
                     for r in internal_redirects]
            rewrites = ("\n    # Redirects observed on the origin site "
                        "(review before enabling)\n" + "\n".join(f"#{l}" for l in lines) + "\n")

        nginx_conf = f"""server {{
    listen 80;
    server_name _;

    root /usr/share/nginx/html;
    index index.html;

    error_page 404 /404.html;
{rewrites}
    location / {{
        try_files $uri $uri/ =404;
    }}

    location ~* \\.(css|js|mjs|png|jpe?g|gif|svg|webp|avif|ico|woff2?|ttf|otf)$ {{
        expires 30d;
        add_header Cache-Control "public";
    }}

    location = /robots.txt  {{ access_log off; }}
    gzip on;
    gzip_types text/html text/css application/javascript application/json
               image/svg+xml application/xml;
}}
"""
        (self.cfg.out_dir / "nginx.conf").write_text(nginx_conf, encoding="utf-8")

        dockerfile = """FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY public/ /usr/share/nginx/html/
"""
        (self.cfg.out_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8")

        # docker-compose.yml: builds an image with the content baked in (via
        # the Dockerfile's COPY -- no bind mount, so re-exports never leave a
        # stale mount behind). Service and container are named after the site
        # host; the published port is overridable via $PORT.
        site_slug = re.sub(r"[^a-z0-9]+", "-", self.host.lower()).strip("-") or "site"
        compose = """\
services:
  __SITE_SLUG__:
    build: .
    image: wpstatic-__SITE_SLUG__
    container_name: wpstatic-__SITE_SLUG__
    ports:
      - "${PORT:-__PORT__}:80"
    restart: unless-stopped
""".replace("__SITE_SLUG__", site_slug).replace("__PORT__", str(self.cfg.port))
        (self.cfg.out_dir / "docker-compose.yml").write_text(compose, encoding="utf-8")

        # server.sh: thin wrapper that drives docker compose.
        server_sh = r'''#!/usr/bin/env sh
# Control the containerized static site via docker compose.
# The image is built from the Dockerfile with the content baked in (no bind
# mount), so it survives re-exports cleanly. Service/container are named
# after the site host (see docker-compose.yml).
#
# Usage:
#   ./server.sh [up|down|restart|status|logs|build] [PORT]   (default: up, port __PORT__)
#   ./server.sh up 9000          # publish on port 9000
#   PORT=9000 ./server.sh up     # same, via environment
set -eu
cd "$(dirname "$0")"

cmd="${1:-up}"
PORT="${PORT:-${2:-__PORT__}}"
export PORT

case "$cmd" in
  up|start)
    docker compose up -d --build
    echo "up -> http://localhost:${PORT}"
    ;;
  down|stop)
    docker compose down
    ;;
  restart)
    docker compose up -d --build --force-recreate
    echo "restarted -> http://localhost:${PORT}"
    ;;
  status|ps)
    docker compose ps
    ;;
  logs)
    docker compose logs -f
    ;;
  build)
    docker compose build
    ;;
  *)
    echo "usage: $0 [up|down|restart|status|logs|build] [PORT]" >&2
    exit 1
    ;;
esac
'''.replace("__PORT__", str(self.cfg.port))
        server_path = self.cfg.out_dir / "server.sh"
        server_path.write_text(server_sh, encoding="utf-8")
        server_path.chmod(0o755)

    def write_report(self) -> None:
        ok_pages = [p for p in self.pages if not p.error]
        failed_pages = [p for p in self.pages if p.error]
        noindex = [p for p in ok_pages if p.noindex]
        canonical_mismatch = [
            p for p in ok_pages
            if p.canonical and self.is_internal(p.canonical)
            and self.normalize_page_url(self.to_base_host(p.canonical)) != p.url
        ]
        html_pages = [p for p in ok_pages if p.content_type.endswith("html")]
        no_title = [p for p in html_pages if not p.has_title]
        no_desc = [p for p in html_pages if not p.has_description]
        no_viewport = [p for p in html_pages if not p.has_viewport]
        link_only = [p for p in ok_pages if p.source == "link"]

        if self.mobile_diff:
            self.warnings.append(
                "Some pages serve DIFFERENT HTML to mobile user agents (UA-based "
                "dynamic serving). public/ contains the desktop variant; the "
                "mobile HTML was saved under mobile-variants/ for review. "
                "Options: switch theme/plugin to responsive rendering (one HTML "
                "for all devices), or serve both variants via an nginx "
                "user-agent map plus a 'Vary: User-Agent' response header.")

        report = {
            "tool": f"{TOOL_NAME} {VERSION}",
            "base_url": self.cfg.base_url,
            "host_header": self.cfg.host_header,
            "logical_host": f"{self.scheme}://{self.host}",
            "generated": time.strftime("%Y-%m-%d %H:%M:%S %z"),
            "pages_exported": len(ok_pages),
            "pages_failed": [{"url": p.url, "error": p.error} for p in failed_pages],
            "assets_exported": self.asset_count,
            "asset_errors": self.asset_errors,
            "sitemaps_exported": self.sitemap_files,
            "redirects": self.redirects,
            "pages_found_only_via_links_not_in_sitemap": [p.url for p in link_only],
            "seo": {
                "soft_404": self.soft_404,
                "noindex_pages": [p.url for p in noindex],
                "canonical_mismatches": [
                    {"url": p.url, "canonical": p.canonical} for p in canonical_mismatch],
                "pages_without_title": [p.url for p in no_title],
                "pages_without_meta_description": [p.url for p in no_desc],
                "pages_without_viewport_meta": [p.url for p in no_viewport],
                "skipped_query_string_urls": sorted(self.skipped_query_urls),
            },
            "mobile": {
                "checked": self.cfg.mobile_check,
                "pages_with_different_mobile_html": self.mobile_diff,
                "pages_with_per_request_dynamic_content": self.mobile_dynamic,
                "pages_sending_vary_user_agent": self.vary_ua_pages,
                "amp_variants_exported": sorted(self.amp_links),
            },
            "external_hosts_referenced": sorted(self.external_hosts),
            "verification": {
                "enabled": self.cfg.rewrite,
                "policy": "external hosts are linked as-is and never fetched;"
                          " wp-admin/wp-json/xmlrpc/admin-ajax and other"
                          " dynamic endpoints are never requested; redirects"
                          " are only followed while they stay on the target"
                          " site",
                "missing_local_files": self.verify_missing,
                "unexpected_absolute_refs": self.verify_unexpected,
            },
            "warnings": self.warnings,
        }
        (self.cfg.out_dir / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

        def section(title: str, items: list, fmt=lambda x: str(x)) -> str:
            if not items:
                return f"{title}: none\n"
            body = "\n".join(f"  - {fmt(i)}" for i in items)
            return f"{title} ({len(items)}):\n{body}\n"

        txt = [
            f"{TOOL_NAME} {VERSION} -- export report",
            f"Site: {self.cfg.base_url}"
            + (f" (Host: {self.cfg.host_header})" if self.cfg.host_header else ""),
            f"Generated: {report['generated']}",
            "",
            f"Pages exported:  {len(ok_pages)}",
            f"Assets exported: {self.asset_count}",
            f"Sitemap files:   {len(self.sitemap_files)}",
            "",
            section("Failed pages", failed_pages, lambda p: f"{p.url} ({p.error})"),
            section("Asset errors", self.asset_errors,
                    lambda a: f"{a['url']} ({a['error']})"),
            section("Redirects observed", self.redirects,
                    lambda r: f"{r['from']} -> {r['to']} ({r['status']})"),
            section("Pages found via links but missing from sitemap", link_only,
                    lambda p: p.url),
            section("SEO: pages with noindex (but present in crawl)", noindex,
                    lambda p: p.url),
            section("SEO: canonical mismatches", canonical_mismatch,
                    lambda p: f"{p.url} -> canonical {p.canonical}"),
            section("SEO: pages without <title>", no_title, lambda p: p.url),
            section("SEO: pages without meta description", no_desc, lambda p: p.url),
            section("SEO: pages without viewport meta (mobile usability)",
                    no_viewport, lambda p: p.url),
            section("Mobile: pages serving DIFFERENT HTML to a mobile UA",
                    self.mobile_diff,
                    lambda d: f"{d['url']} (variant: {d['variant']})"),
            section("Mobile: per-request dynamic content (UA comparison "
                    "inconclusive)", self.mobile_dynamic),
            section("Mobile: 'Vary: User-Agent' response header seen",
                    self.vary_ua_pages),
            section("AMP variants exported", sorted(self.amp_links)),
            section("Skipped URLs with query strings", sorted(self.skipped_query_urls)),
            section("External hosts (linked as-is, intentionally NOT "
                    "downloaded)", sorted(self.external_hosts)),
            section("Verification: referenced local files missing",
                    self.verify_missing,
                    lambda v: f"{v['file']} -> {v['ref']}"
                              + (" (already failing on the origin site)"
                                 if v.get("origin_error") else "")),
            section("Verification: unexpected absolute self-references",
                    self.verify_unexpected,
                    lambda v: f"{v['file']} -> {v['ref']}"),
            section("Warnings", self.warnings),
        ]
        (self.cfg.out_dir / "report.txt").write_text("\n".join(txt), encoding="utf-8")

    # ----------------------------------------------------------------------

    def run(self) -> int:
        print(f"{TOOL_NAME} {VERSION}")
        print(f"[init] Site: {self.cfg.base_url}"
              + (f" (Host header: {self.cfg.host_header})"
                 if self.cfg.host_header else ""))
        print(f"[init] Output: {self.cfg.out_dir}")
        if self.public_dir.exists():
            if self.cfg.clean:
                shutil.rmtree(self.public_dir)
                print("[init] --clean: removed existing public/")
            elif any(self.public_dir.iterdir()):
                self.warnings.append(
                    "output dir public/ was not empty and --clean was not "
                    "set; stale files from previous runs may remain.")
        self.public_dir.mkdir(parents=True, exist_ok=True)

        seeds = self.discover()
        print(f"[discover] {len(set(seeds))} unique pages from sitemaps")
        self.crawl(seeds)
        self.capture_404()
        self.capture_favicon()
        self.verify_export()
        self.write_deploy_files()
        self.write_report()

        ok = len([p for p in self.pages if not p.error])
        failed = len(self.pages) - ok
        print()
        print(f"[done] {ok} pages, {self.asset_count} assets exported "
              f"-> {self.public_dir}")
        if self.cfg.mobile_check:
            if self.mobile_diff:
                print(f"[done] Mobile: {len(self.mobile_diff)} pages serve "
                      f"DIFFERENT mobile HTML -- variants under "
                      f"mobile-variants/, details in the report")
            elif self.mobile_dynamic:
                print(f"[done] Mobile: {len(self.mobile_dynamic)} pages with "
                      f"dynamic content, UA comparison inconclusive -- "
                      f"see the report")
            else:
                print("[done] Mobile: HTML for a mobile UA is identical "
                      "(responsive) -- the export covers the mobile rendering")
        if self.cfg.rewrite:
            if self.verify_missing or self.verify_unexpected:
                print(f"[verify] {len(self.verify_missing)} missing local "
                      f"files, {len(self.verify_unexpected)} unexpected "
                      f"absolute references -- details in the report")
            else:
                print("[verify] export is self-contained: all references "
                      "resolve locally, no unexpected absolute URLs")
            print("[note] Not functional statically: form submissions "
                  "(WPForms & co.), consent/analytics AJAX and WordPress "
                  "search (dynamic endpoints).")
            print("[note] External hosts (font/analytics/CDN domains) stay "
                  "linked and are never downloaded; "
                  "wp-admin/wp-json/xmlrpc are never requested.")
        if failed or self.asset_errors or self.warnings:
            print(f"[done] {failed} page errors, {len(self.asset_errors)} "
                  f"asset errors, {len(self.warnings)} warnings -- see report.txt")
        print(f"[done] Report: {self.cfg.out_dir / 'report.txt'}")
        print(f"[done] Serve: {self.cfg.out_dir / 'server.sh'} up "
              f"-> http://localhost:{self.cfg.port}  (stop: server.sh down)")
        return 0 if ok else 1


# --------------------------------------------------------------------------

def parse_args(argv: list[str]) -> Config:
    ap = argparse.ArgumentParser(
        prog=TOOL_NAME,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="SEO-aware static export of a WordPress (or any) site: "
                    "sitemap-driven crawl, asset mirroring, 1:1 URL structure, "
                    "SEO report, nginx/Docker deployment files.",
        epilog="""\
examples:
  # simplest: mirror a live site into ./export
  wp-static-export https://www.example.at

  # re-run cleanly (wipes ./export/public first) with polite throttling
  wp-static-export https://www.example.at -o ./export --clean --delay 0.2

  # server reachable only by IP, no DNS entry: connect to the IP, send the
  # site's hostname as Host header + TLS SNI (like curl --resolve)
  wp-static-export http://10.0.0.5:8080 --host example.at -o ./export --clean

  # same, for a WordPress origin behind an HTTPS-terminating proxy that would
  # otherwise 301 to its https:// URL -- add the forwarded-proto header
  wp-static-export http://10.0.0.5:8080 --host example.at \\
      --header 'X-Forwarded-Proto: https' -o ./export --clean

  # keep HTML byte-identical to the origin (only works under the same hostname)
  wp-static-export https://www.example.at --no-rewrite

  # bake the serving port into the generated compose file / server.sh
  wp-static-export https://www.example.at --port 9000

  # view the result locally (writes docker-compose.yml + server.sh into <out>);
  # server.sh is a thin wrapper around docker compose, image with content baked in
  cd export && ./server.sh up                # -> http://localhost:8080 (or --port)
  cd export && ./server.sh up 9000           # override the port at run time
  cd export && ./server.sh down

Notes:
  * A scheme-less base_url defaults to https:// -- type http://IP explicitly
    for plain-http servers.
  * External hosts (fonts/analytics/CDN) stay linked as-is and are never
    downloaded; wp-admin / wp-json / xmlrpc are never requested.
  * report.txt / report.json summarize errors, redirects, SEO signals and a
    self-containedness verification pass.""")
    ap.add_argument("base_url", help="Site root, e.g. https://www.example.at")
    ap.add_argument("-o", "--out", default="./export", help="output directory "
                    "(default: ./export). Web root ends up in <out>/public")
    ap.add_argument("--no-rewrite", dest="rewrite", action="store_false",
                    help="keep HTML/CSS byte-identical to the origin instead "
                         "of rewriting same-site URLs to root-relative "
                         "(the export then only renders correctly when served "
                         "under the original hostname)")
    ap.add_argument("--make-relative", action="store_true",
                    help="deprecated: URL localization is now the default; "
                         "use --no-rewrite to disable it")
    ap.add_argument("--clean", action="store_true",
                    help="delete <out>/public before exporting "
                         "(recommended for re-runs)")
    ap.add_argument("--port", type=int, default=8080,
                    help="port the container listens on; baked as the default "
                         "into the generated docker-compose.yml / server.sh "
                         "(still overridable at run time via $PORT or "
                         "'server.sh up <port>'). Default: 8080")
    ap.add_argument("--no-follow-links", dest="follow_links", action="store_false",
                    help="only export URLs listed in the sitemaps")
    ap.add_argument("-c", "--concurrency", type=int, default=5)
    ap.add_argument("--delay", type=float, default=0.0,
                    help="seconds to sleep before each request (politeness)")
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--max-pages", type=int, default=5000)
    ap.add_argument("--user-agent", default=None)
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS certificate verification")
    ap.add_argument("--host", dest="host_header", default=None,
                    help="logical site hostname, sent as HTTP Host header "
                         "and used for TLS SNI/verification -- for servers "
                         "reachable only by IP without a DNS entry (like "
                         "curl --resolve), e.g. "
                         "http://10.0.0.5:8080 --host example.at. May "
                         "include :port if the vhost expects one. Note: a "
                         "scheme-less base_url defaults to https:// -- type "
                         "http://IP explicitly for plain-http servers")
    ap.add_argument("--header", action="append", default=[], metavar="'NAME: VALUE'",
                    help="extra HTTP request header, repeatable. Common case: "
                         "--header 'X-Forwarded-Proto: https' so a WordPress "
                         "origin behind an HTTPS proxy serves pages instead of "
                         "redirecting to its https:// URL")
    ap.add_argument("--extra-sitemap", action="append", default=[],
                    help="additional sitemap URL (repeatable)")
    ap.add_argument("--no-mobile-check", dest="mobile_check", action="store_false",
                    help="skip the mobile-vs-desktop HTML comparison "
                         "(saves one extra request per page)")
    ap.add_argument("--mobile-user-agent", default=None,
                    help="user agent used for the mobile comparison "
                         "(default: iPhone Safari)")
    args = ap.parse_args(argv)

    base = args.base_url.strip()
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    base = base.rstrip("/")

    host_header = (args.host_header or "").strip()
    host_header = re.sub(r"^https?://", "", host_header).rstrip("/") or None

    extra_headers: dict = {}
    for raw in args.header:
        if ":" not in raw:
            ap.error(f"--header must be 'Name: Value', got: {raw!r}")
        name, value = raw.split(":", 1)
        extra_headers[name.strip()] = value.strip()

    if args.make_relative:
        print("[note] --make-relative is deprecated: URL localization is now "
              "the default (use --no-rewrite to disable).", file=sys.stderr)

    if not 1 <= args.port <= 65535:
        ap.error(f"--port must be between 1 and 65535, got {args.port}")

    cfg = Config(
        base_url=base,
        out_dir=Path(args.out).resolve(),
        rewrite=args.rewrite,
        clean=args.clean,
        follow_links=args.follow_links,
        concurrency=max(1, args.concurrency),
        delay=max(0.0, args.delay),
        timeout=args.timeout,
        max_pages=args.max_pages,
        insecure=args.insecure,
        host_header=host_header,
        extra_headers=extra_headers,
        extra_sitemaps=args.extra_sitemap,
        mobile_check=args.mobile_check,
        port=args.port,
    )
    if args.user_agent:
        cfg.user_agent = args.user_agent
    if args.mobile_user_agent:
        cfg.mobile_user_agent = args.mobile_user_agent
    return cfg


def main(argv: list[str]) -> int:
    cfg = parse_args(argv)
    try:
        return Exporter(cfg).run()
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
