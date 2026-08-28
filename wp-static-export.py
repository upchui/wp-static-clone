#!/usr/bin/env python3
#
# wp-static-export -- SEO-aware static site exporter for WordPress (or any CMS).
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
* A fresh /sitemap.xml is generated from the URL set the ORIGIN SITEMAP
  declared and the export contains (noindex'ed pages, redirect sources and
  canonical mismatches excluded, link-only discoveries like attachment
  pages stay out unless --sitemap-include-linked, <lastmod> from the
  Last-Modified header), and the robots.txt Sitemap: line is pointed at
  it. --no-generate-sitemap instead keeps the origin sitemap files
  unchanged (except their XSL stylesheet reference, which is localized),
  XSL stylesheets included. A missing robots.txt is generated.
* By default, same-site URLs are rewritten to root-relative -- in ALL the
  forms page builders emit them: plain absolute, protocol-relative
  (//host/...), JSON-escaped (backslash-escaped slashes) in inline scripts,
  and base64-encoded lazy-load attributes (Slider Revolution data-dbsrc).
  The export is self-contained and renders 1:1 from any hostname.
  canonical / rel=alternate links, og:*/twitter:* meta tags, JSON-LD and
  sitemap <loc> entries are intentionally left absolute (SEO data, never
  fetched by browsers). The generated nginx config swaps the origin host
  for the actually requested one at serve time (sub_filter), so those
  signals are correct on ANY domain without re-exporting; for hosts
  without serve-time substitution (Netlify, Apache), --target-domain
  hard-rewrites them to the new domain at export time instead.
* WordPress head cruft that only points at dynamic origin infrastructure
  (generator meta, wp-json REST + oEmbed + feed discovery, EditURI/RSD,
  wlwmanifest, pingback, ?p= shortlinks, the Cloudflare Insights beacon)
  is stripped; --no-strip-wp-cruft keeps it. canonical / hreflang / og: /
  twitter: / JSON-LD stay.
* <img> tags get loading="lazy" + decoding="async" (except the first image
  per page and plugin-lazyloaded ones) and, where the local file's header
  reveals them, width/height attributes against layout shift (CLS).
  Disable with --no-optimize-images.
* --staging deploys the mirror invisible to search engines: robots.txt
  'Disallow: /', an X-Robots-Tag: noindex header in the server configs and
  a noindex robots meta injected into every page (rewrite mode).
* --no-rewrite instead keeps HTML pages BYTE-IDENTICAL to the server
  response (nothing re-serialized); the export then only renders correctly
  when served under the original hostname, and cruft stripping, image
  optimization, redirect stubs, the staging meta and the HTML side of
  --target-domain are disabled (each with a warning).
* Inline <script> bodies and same-site .js files are additionally scanned
  for internal URLs, so slider configs & co. get their assets exported too.
* --exclude REGEX skips pages and assets by URL path; --respect-robots
  honors the origin's robots.txt Allow/Disallow rules and Crawl-delay.
  Large media files (mp4/pdf/zip/...) are streamed to disk, not buffered.
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
nginx.conf (serve-time domain substitution, active 301s for the redirects
observed on the origin, trailing-slash canonicalization, a non-indexable
404.html, security headers) plus redirects.inc, a Dockerfile, a
docker-compose.yml (with healthcheck), a .dockerignore and a server.sh
wrapper are generated next to the export, so `./server.sh up` in the
output directory gives you a ready-to-run container. public/_redirects
(Netlify) and public/.htaccess (Apache) cover the same redirects on those
platforms -- combine with --target-domain there, since neither
substitutes the host at serve time.

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
import importlib.util
import ipaddress
import json
import posixpath
import random
import re
import shutil
import socket
import string
import sys
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote, quote

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

if sys.version_info < (3, 10):
    sys.exit(f"wp-static-export needs Python 3.10+ "
             f"(running {sys.version.split()[0]})")

TOOL_NAME = "wp-static-export"
VERSION = "2.2.0"

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

# never rewritten, often large: streamed to disk instead of buffered in RAM
MEDIA_EXTENSIONS = {
    "mp4", "webm", "ogv", "mp3", "ogg", "wav", "m4a",
    "pdf", "zip", "gz", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "wasm",
}

# dynamic-endpoint paths that must never be fetched as pages or assets.
# The core fragments are the tool's identity (never talk to WP admin/API
# endpoints); plugins contribute vendor fragments via
# page_skip_pattern_fragments and load_plugins() recompiles the regex.
PAGE_SKIP_FRAGMENTS = (
    r"/wp-admin(/|$)", r"/wp-login\.php", r"/wp-json(/|$)", r"/xmlrpc\.php",
    r"/wp-signup\.php", r"/wp-activate\.php", r"/wp-trackback\.php",
    r"/feed/?$", r"/comments/feed", r"/wp-cron\.php",
)


def _compile_page_skip(fragments: tuple) -> re.Pattern:
    return re.compile("(" + "|".join(fragments) + ")", re.IGNORECASE)


PAGE_SKIP_PATTERNS = _compile_page_skip(PAGE_SKIP_FRAGMENTS)

# attributes that may hold a single URL (plugins register vendor-specific
# ones, e.g. lazy-load and consent-plugin attributes)
URL_ATTRS = ("src", "href", "poster")
# attributes that hold srcset-style comma separated candidate lists
# (plugin-extendable: srcset_attrs)
SRCSET_ATTRS = ("srcset",)
# attributes whose value is a full CSS declaration containing url(...)
# (plugin-registered: css_url_attrs, e.g. data-bg)
CSS_URL_ATTRS: tuple = ()
# attributes that mark an <img> as managed by a lazy-load plugin -- such
# images must not get loading=lazy/width/height injected
# (plugin-registered: lazy_img_attrs)
LAZY_IMG_ATTRS: tuple = ()
# extra top-level output directories next to public/ that --clean removes
# and .dockerignore excludes (plugin-registered: extra_output_dirs)
EXTRA_OUTPUT_DIRS: tuple = ()
# attributes that hold internal *page* links (theme-specific -- registered
# by plugins, e.g. The7's clickable images)
PAGE_URL_ATTRS: tuple = ()
# attributes that hold base64-encoded URLs (registered by plugins, e.g.
# Slider Revolution's lazy src)
B64_URL_ATTRS: tuple = ()

# plugin-extendable registries (aggregated by load_plugins() at import time):
# extra (bytes-regex, replacement) noise patterns normalize_html() applies
# before its whitespace collapse
HTML_NOISE_EXTRA: tuple = ()
# top-level dirs whose "/dir/..." string refs inside inline scripts
# verify_export() resolves against the export
VERIFY_SCRIPT_REF_DIRS = ("wp-content", "wp-includes")
# local refs verify_export() never checks (resolved at runtime instead)
VERIFY_SKIP_REF_PREFIXES: tuple = ()

CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.IGNORECASE)
CSS_IMPORT_RE = re.compile(r"@import\s+['\"]([^'\"]+)['\"]", re.IGNORECASE)
META_REFRESH_RE = re.compile(r"url\s*=\s*(.+)", re.IGNORECASE)

# attributes holding human-readable text -- a full URL inside them is
# CONTENT (alt text, tooltips), never fetched by browsers: exempt from
# localization and from the verify absolute-reference check
TEXT_ATTRS = ("alt", "title", "aria-label", "placeholder")


def iter_srcset(val: str):
    """Parse a srcset-style attribute into (url, descriptor) pairs using
    the HTML spec's whitespace-first tokenization: URLs are whitespace-
    delimited (so data: URIs containing commas survive intact), a URL's
    TRAILING comma acts as the candidate separator, and descriptors run to
    the next comma. A naive split(",") would cut data: URIs in half."""
    pos, n = 0, len(val)
    while pos < n:
        while pos < n and (val[pos].isspace() or val[pos] == ","):
            pos += 1
        if pos >= n:
            break
        start = pos
        while pos < n and not val[pos].isspace():
            pos += 1
        url = val[start:pos]
        if url.endswith(","):
            yield url.rstrip(","), ""
            continue
        dstart = pos
        while pos < n and val[pos] != ",":
            pos += 1
        yield url, val[dstart:pos].strip()
        pos += 1
XML_STYLESHEET_RE = re.compile(r"<\?xml-stylesheet[^>]*href=[\"']([^\"']+)[\"']")
SITEMAP_LINE_RE = re.compile(r"(?im)^\s*sitemap:\s*(\S+)")

def norm_host(host: str) -> str:
    """Canonical comparison form of a hostname: lowercase, www. stripped,
    IDN hosts in their punycode (ASCII) spelling -- WordPress markup may
    emit either spelling of the same host."""
    host = (host or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host.isascii():
        head, sep, port = host.partition(":")
        try:
            head = head.encode("idna").decode("ascii")
        except UnicodeError:
            pass
        host = head + sep + port
    return host


# private/loopback/link-local hosts -- a URL like this in the export makes
# browsers show the Local Network Access permission prompt (or just fail)
PRIVATE_NET_RE = re.compile(
    r"(?:https?|wss?)://(?:"
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|169\.254\.\d{1,3}\.\d{1,3}"
    r"|127\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|localhost\b"
    r"|[\w.-]+\.local\b"
    r")(?::\d+)?", re.IGNORECASE)

# generic absolute-URL host extraction (homepage probe / same-site checks)
ABS_HOST_RE = re.compile(r"https?://([A-Za-z0-9._-]+(?::\d+)?)", re.IGNORECASE)
# a foreign host serving wp-content/wp-includes paths is very likely the
# same WordPress install under another name (siteurl != public domain)
FOREIGN_WP_RE = re.compile(
    r"https?://([A-Za-z0-9._-]+(?::\d+)?)/wp-(?:content|includes)/",
    re.IGNORECASE)


def resolves_private(host: str) -> bool:
    """True when the host resolves EXCLUSIVELY to private/loopback/
    link-local addresses from this machine. With split-horizon DNS that
    means: internal infrastructure (e.g. a WP admin domain) -- browsers
    would hit the Local Network Access permission prompt for it.
    Resolution failure -> False (dead for clients too, no LNA concern)."""
    name = host.partition(":")[0].strip("[]")
    if not name:
        return False
    try:
        infos = socket.getaddrinfo(name, None)
    except OSError:
        return False
    addrs = {info[4][0] for info in infos}
    if not addrs:
        return False
    try:
        ips = [ipaddress.ip_address(a.partition("%")[0]) for a in addrs]
    except ValueError:
        return False
    # loopback/unspecified answers are DNS-sinkhole blocklists (Pi-hole &
    # co. answer tracker domains with 0.0.0.0/127.x) -- NOT internal sites
    return all(ip.is_private and not ip.is_loopback and not ip.is_unspecified
               for ip in ips)


def robots_rule_re(pattern: str) -> re.Pattern:
    """robots.txt Allow/Disallow pattern -> anchored regex ('*' wildcard,
    trailing '$' end-anchor, otherwise prefix match -- Google semantics)."""
    rx = re.escape(pattern).replace(r"\*", ".*")
    if rx.endswith(r"\$"):
        rx = rx[:-2] + "$"
    return re.compile("^" + rx)


def host_spellings(host: str) -> set[str]:
    """ASCII (punycode) and Unicode spellings of a normalized host --
    both can appear in markup and must match the localization regexes."""
    out = {host}
    head, sep, port = host.partition(":")
    try:
        uni = head.encode("ascii").decode("idna")
        if uni and uni != head:
            out.add(uni + sep + port)
    except (UnicodeError, UnicodeDecodeError):
        pass
    return out


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def path_extension(url_path: str) -> str:
    name = posixpath.basename(url_path)
    if "." in name:
        return name.rsplit(".", 1)[-1].lower()
    return ""


def _isdir(p: Path) -> bool:
    """Path.is_dir() that treats an un-stat-able path as 'not a directory'
    instead of raising -- Path.is_dir() propagates ENAMETOOLONG for
    over-long names on Python 3.10-3.13 (3.14 swallows it)."""
    try:
        return p.is_dir()
    except OSError:
        return False


def canon_path(path: str) -> str:
    """Canonical URL path: percent-decoded, Unicode-NFC, re-quoted.

    Collapses spelling variants of the same path (%C3%A4 vs literal ä vs
    NFD a+%CC%88) into one form so crawl keys, rewritten links and on-disk
    paths all agree. Canonicalization runs per SEGMENT so an encoded %2F
    never turns into a real path separator (that would change which URL is
    fetched and where the file lands). RFC 3986 path characters plus {}
    and * stay literal: plugins substitute {placeholder} templates and
    match /path/* globs at runtime, which percent-encoding would break.
    Single quotes are re-encoded (%27) so generated CSS url('...') stays
    parseable."""
    return "/".join(
        quote(unicodedata.normalize("NFC", unquote(seg)),
              safe=":@!$&()*+,;={}")
        for seg in path.split("/"))


def canon_ref(ref: str) -> str:
    """canon_path for a reference that may carry ?query / #fragment.
    Splits at the EARLIEST of the two separators: in '/x#frag?y' the '?'
    belongs to the fragment and must not be treated as a query."""
    cut = min((i for i in (ref.find("?"), ref.find("#")) if i != -1),
              default=-1)
    if cut != -1:
        return canon_path(ref[:cut] or "/") + ref[cut:]
    return canon_path(ref or "/")


def try_b64_url(val: str) -> str | None:
    """Decode a base64 attribute value if (and only if) it holds a URL.
    Accepts the standard AND the URL-safe alphabet."""
    if not val or not re.fullmatch(r"[A-Za-z0-9+/_-]{8,}={0,2}", val):
        return None
    try:
        decoded = base64.b64decode(
            val.replace("-", "+").replace("_", "/"),
            validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if decoded.startswith(("//", "http://", "https://", "/")):
        return decoded
    return None


CSS_CHARSET_B_RE = re.compile(rb'^@charset\s+["\']([-\w]+)["\']')
CSS_CHARSET_RE = re.compile(r'^@charset\s+["\'][-\w]+["\']')
CHARSET_HDR_RE = re.compile(r"""charset=["']?([-\w]+)""", re.IGNORECASE)


def decode_text_asset(resp: requests.Response) -> str:
    """Charset-correct text of a CSS/JS response. requests defaults text/*
    without a charset parameter to ISO-8859-1 (RFC 2616), which mojibakes
    the UTF-8 files real sites serve -- so: header charset if declared,
    else a leading CSS @charset rule, else UTF-8."""
    m = CHARSET_HDR_RE.search(resp.headers.get("Content-Type") or "")
    enc = m.group(1) if m else None
    if not enc:
        m2 = CSS_CHARSET_B_RE.match(resp.content[:64])
        if m2:
            enc = m2.group(1).decode("ascii", "replace")
    try:
        return resp.content.decode(enc or "utf-8")
    except (UnicodeDecodeError, LookupError):
        return resp.content.decode("utf-8", "replace")


# html.parser lowercases attribute names and SVG attributes are
# case-sensitive -- a lowercased viewBox is IGNORED by browsers, breaking
# inline SVG logos/icons. serialize_soup() restores the proper casing.
SVG_ATTR_CASE = {a.lower(): a for a in (
    "allowReorder attributeName attributeType autoReverse baseFrequency "
    "baseProfile calcMode clipPath clipPathUnits contentScriptType "
    "contentStyleType diffuseConstant edgeMode externalResourcesRequired "
    "filterRes filterUnits glyphRef gradientTransform gradientUnits "
    "kernelMatrix kernelUnitLength keyPoints keySplines keyTimes "
    "lengthAdjust limitingConeAngle markerHeight markerUnits markerWidth "
    "maskContentUnits maskUnits numOctaves pathLength patternContentUnits "
    "patternTransform patternUnits pointsAtX pointsAtY pointsAtZ "
    "preserveAlpha preserveAspectRatio primitiveUnits refX refY "
    "repeatCount repeatDur requiredExtensions requiredFeatures "
    "specularConstant specularExponent spreadMethod startOffset "
    "stdDeviation stitchTiles surfaceScale systemLanguage tableValues "
    "targetX targetY textLength viewBox viewTarget xChannelSelector "
    "yChannelSelector zoomAndPan").split()}


def serialize_soup(soup: BeautifulSoup) -> bytes:
    """str(soup) with case-sensitive SVG attribute names restored inside
    <svg> subtrees (viewBox, preserveAspectRatio, ...)."""
    for svg in soup.find_all("svg"):
        for tag in (svg, *svg.find_all(True)):
            for attr in list(tag.attrs):
                proper = SVG_ATTR_CASE.get(attr)
                if proper and proper != attr:
                    tag.attrs[proper] = tag.attrs.pop(attr)
    return str(soup).encode("utf-8")


# --------------------------------------------------------------------------
# Plugin API -- feature-specific behavior (minification, slider handling,
# vendor/theme fixes, ...) lives in plugins/*.py next to this script; the
# core only provides the crawl/rewrite/verify/report machinery plus the
# hook points below.
# --------------------------------------------------------------------------

def report_section(title: str, items: list, fmt=lambda x: str(x)) -> str:
    """One formatted report.txt section -- shared by write_report() and
    plugin add_report() contributions."""
    if not items:
        return f"{title}: none\n"
    body = "\n".join(f"  - {fmt(i)}" for i in items)
    return f"{title} ({len(items)}):\n{body}\n"


class Plugin:
    """Base class for the feature plugins in plugins/*.py.

    One `PLUGIN = <Plugin subclass>` per file; the core discovers and loads
    every plugins/*.py at import time (load_plugins()), so plugin CLI flags
    exist before parse_args() ever runs. Disable a plugin by deleting its
    file or prefixing the file name with '_'.

    Class-level registries are aggregated ONCE at load time into the
    module-level constants (URL_ATTRS & co.). Instance hooks run per
    export run; hooks marked [threaded] are called from crawl worker
    threads -- guard shared mutable state with self.exp.stats_lock,
    exactly like the core does."""

    name = ""                        # unique id, e.g. "minify" -- required

    # -- static registries (aggregated at load time) -----------------------
    url_attrs: tuple = ()            # extra single-URL attributes
    page_url_attrs: tuple = ()       # extra internal-page-link attributes
    b64_url_attrs: tuple = ()        # extra base64-encoded-URL attributes
    html_noise_patterns: tuple = ()  # (bytes-regex, replacement) pairs for
                                     # normalize_html() noise stripping
    verify_script_ref_dirs: tuple = ()    # extra VERIFY_SCRIPT_REF_DIRS
    verify_skip_ref_prefixes: tuple = ()  # extra VERIFY_SKIP_REF_PREFIXES
    srcset_attrs: tuple = ()         # extra srcset-style attributes
    css_url_attrs: tuple = ()        # extra CSS-declaration-in-attr attributes
    lazy_img_attrs: tuple = ()       # extra lazy-load-plugin <img> attributes
    page_skip_pattern_fragments: tuple = ()  # extra PAGE_SKIP regex fragments
    extra_output_dirs: tuple = ()    # extra out-dir trees (--clean/.dockerignore)

    # -- CLI phase (classmethods: run before any Exporter exists) ----------
    @classmethod
    def add_cli_args(cls, group) -> None:
        """Register argparse options (called with this plugin's own
        argument group)."""

    @classmethod
    def finish_args(cls, ap, args, cfg) -> None:
        """Validate parsed args (fail via ap.error) and copy them onto
        the Config."""

    # -- per-run instance hooks --------------------------------------------
    def __init__(self, exporter):
        self.exp = exporter

    def run_start(self) -> None:
        """Top of Exporter.run(): environment validation / warnings."""

    def pre_discover_soup(self, soup, page_url: str) -> None:
        """[threaded] Rewrite mode, BEFORE URL discovery and rewriting --
        mutations here are seen by both (e.g. slide hydration)."""

    def clean_soup(self, soup) -> None:
        """[threaded] Right after core strip_resource_hints(), under the
        same cfg.rewrite + cfg.strip_wp_cruft gate."""

    def rewrite_soup(self, soup) -> None:
        """[threaded] Right after core rewrite_soup_relative()."""

    def expand_scan_text(self, work: str) -> str:
        """[threaded] Pre-transform of JS/JSON text before the URL sweep
        (e.g. resolving runtime URL-template placeholders)."""
        return work

    def scan_text_urls(self, work: str) -> tuple[list, list]:
        """[threaded] (page_urls, asset_urls) derived from runtime
        URL-construction patterns in JS/JSON text."""
        return [], []

    def skip_page_path(self, path: str) -> bool:
        """[threaded] True when this URL path must never be treated as a
        page (reached from crawl workers via normalize_page_url)."""
        return False

    def skip_asset_candidate(self, url: str, tag_name: str,
                             rel: set) -> bool:
        """[threaded] True to drop this discovered asset candidate (url is
        the canonicalized absolute URL, rel the lowercased rel-token set)
        -- e.g. targets of tags another hook is going to strip anyway."""
        return False

    def save_non_html_response(self, url: str, resp) -> bool:
        """[threaded] Claim a non-HTML response served on a page URL or an
        extensionless asset URL (e.g. materialize a dynamic download
        endpoint as a real file). True = claimed, the core skips its own
        raw write; first claimant wins."""
        return False

    def page_fetched(self, url: str, resp, rec) -> None:
        """[threaded] Successful same-site HTML page response, BEFORE the
        redirect-stub/save decision (fires for pages that end up as query-
        redirect stubs too) -- e.g. response-header inspection."""

    def page_saved(self, save_url: str, resp, rec) -> None:
        """[threaded] After parse_and_save_html() for a real (non-stub)
        page; resp.content holds the HTML that was saved under save_url."""

    def redirect_rules(self, seen_from: set) -> list:
        """Extra (from_path, to_ref) 301 rules for the generated server
        configs. seen_from holds every from_path already claimed -- skip
        those and add your accepted ones; return only config-safe rules
        (the plugin owns its character filtering and warnings)."""
        return []

    def filter_text_asset(self, url: str, kind: str, text: str) -> str:
        """[threaded] Last transform of a rewritten text asset
        (kind: 'css' | 'js') before it is encoded and written."""
        return text

    def text_asset_written(self, kind: str, orig_len: int,
                           new_len: int) -> None:
        """[threaded] A transformed text asset was written
        (kind: 'html' | 'css' | 'js') -- stats notification."""

    def pre_serialize(self, soup) -> None:
        """[threaded] Inside Exporter.serialize(), before str(soup)."""

    def post_serialize(self, data: bytes) -> bytes:
        """[threaded] Last step on the serialized HTML bytes."""
        return data

    def wants_postprocess(self) -> bool:
        """True when postprocess_soup() has work to do -- gates the extra
        read-modify-write pass over every exported HTML page."""
        return False

    def postprocess_soup(self, soup) -> bool:
        """Post-crawl transform of an exported page; True = changed."""
        return False

    def summary_lines(self) -> list[str]:
        """Console lines for the final run() summary."""
        return []

    def add_report(self, report: dict, txt_head: list,
                   txt_sections: list) -> None:
        """Contribute to report.json (mutate report) and report.txt
        (append 'Label:  value' head lines / report_section() blocks)."""


PLUGIN_REGISTRY: list[type[Plugin]] = []
PLUGIN_MODULES: dict = {}            # plugin name -> loaded module


def _load_plugin_file(path: Path) -> tuple:
    """Load one plugin file; returns (module, PLUGIN class). A plugin that
    raises during import propagates -- a half-loaded feature set must never
    silently produce a degraded export (same fail-loudly policy as the
    missing-minifier check)."""
    mod_name = f"wps_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module   # findable for tests / monkeypatching
    try:
        spec.loader.exec_module(module)
        cls = getattr(module, "PLUGIN", None)
        if not (isinstance(cls, type) and issubclass(cls, Plugin)
                and cls.name):
            sys.exit(f"{TOOL_NAME}: {path.name}: no PLUGIN = <Plugin "
                     f"subclass with a name> found")
    except BaseException:
        # like importlib: never leave a half-initialized module behind
        sys.modules.pop(mod_name, None)
        raise
    return module, cls


def _scan_plugins(plugins_dir: Path) -> list[tuple]:
    """Load every plugins/*.py (sorted; '_'-prefixed files are skipped)."""
    if not plugins_dir.is_dir():
        sys.exit(f"{TOOL_NAME}: plugins directory not found: {plugins_dir}\n"
                 f"The tool ships with a plugins/ folder next to the script "
                 f"(minification, slider handling, vendor fixes live there) "
                 f"-- restore it, or create an empty one to run bare.")
    return [_load_plugin_file(p) for p in sorted(plugins_dir.glob("*.py"))
            if not p.name.startswith("_")]


def load_plugins() -> None:
    """Discover and register the plugins from the plugins/ directory next
    to this script, then aggregate their static registries into the
    module-level constants. Runs exactly once, at the bottom of this
    module -- later calls are no-ops (which is why there is no directory
    parameter: it could never take effect)."""
    global URL_ATTRS, PAGE_URL_ATTRS, B64_URL_ATTRS, HTML_NOISE_EXTRA
    global VERIFY_SCRIPT_REF_DIRS, VERIFY_SKIP_REF_PREFIXES
    global SRCSET_ATTRS, CSS_URL_ATTRS, LAZY_IMG_ATTRS, EXTRA_OUTPUT_DIRS
    global PAGE_SKIP_PATTERNS
    if PLUGIN_REGISTRY:
        return
    pdir = Path(__file__).resolve().parent / "plugins"
    skip_fragments: tuple = ()
    for module, cls in _scan_plugins(pdir):
        if cls.name in PLUGIN_MODULES:
            sys.exit(f"{TOOL_NAME}: duplicate plugin name {cls.name!r}")
        PLUGIN_REGISTRY.append(cls)
        PLUGIN_MODULES[cls.name] = module
        URL_ATTRS += tuple(cls.url_attrs)
        PAGE_URL_ATTRS += tuple(cls.page_url_attrs)
        B64_URL_ATTRS += tuple(cls.b64_url_attrs)
        HTML_NOISE_EXTRA += tuple(cls.html_noise_patterns)
        VERIFY_SCRIPT_REF_DIRS += tuple(cls.verify_script_ref_dirs)
        VERIFY_SKIP_REF_PREFIXES += tuple(cls.verify_skip_ref_prefixes)
        SRCSET_ATTRS += tuple(cls.srcset_attrs)
        CSS_URL_ATTRS += tuple(cls.css_url_attrs)
        LAZY_IMG_ATTRS += tuple(cls.lazy_img_attrs)
        EXTRA_OUTPUT_DIRS += tuple(cls.extra_output_dirs)
        skip_fragments += tuple(cls.page_skip_pattern_fragments)
    if skip_fragments:
        PAGE_SKIP_PATTERNS = _compile_page_skip(
            PAGE_SKIP_FRAGMENTS + skip_fragments)


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
    # plugin-owned (plugins/mobile_check.py); user_agent "" means the
    # plugin's default (iPhone UA)
    mobile_check: bool = True
    mobile_user_agent: str = ""
    generate_sitemap: bool = True
    strip_wp_cruft: bool = True
    # plugin-owned option (declared here so direct Config() construction
    # keeps working; consumed only by plugins/image_optimize.py)
    optimize_images: bool = True
    staging: bool = False
    target_domain: str | None = None
    sitemap_include_linked: bool = False
    fail_on: str = "none"            # none | errors | verify
    quiet: bool = False
    excludes: list = field(default_factory=list)
    respect_robots: bool = False
    # plugin-owned (plugins/slider_revolution.py)
    sr7_hydrate: bool = True
    internal_hosts: list = field(default_factory=list)
    resolve_internal: bool = True
    # plugin-owned (plugins/minify.py)
    minify: bool = True


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
                                     # (plugin-owned: plugins/mobile_check.py)
    last_modified: str = ""          # raw Last-Modified response header
    save_url: str = ""               # canonical URL the content was saved under
                                     # (differs from url for redirect sources)
    is_stub: bool = False            # only a redirect stub was written here


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
            # the connect address is never a legitimate external link, so
            # treat EVERY port spelling of it as internal: WordPress inside
            # a container (crawled via 10.x.x.x:81) often emits its URLs
            # portless (:80) -- e.g. in SR7 REST responses -- and an
            # unlocalized private IP in the export makes browsers throw the
            # Local Network Access permission prompt
            self.internal_norms.add(self.connect_norm.partition(":")[0])
        # --internal-host: further spellings of the same site (e.g. the
        # internal WP admin domain the siteurl points at)
        for extra in cfg.internal_hosts:
            self.internal_norms.add(norm_host(extra))
        self.public_dir = cfg.out_dir / "public"
        # --target-domain: SEO-bearing origin URLs (canonical, og:*, JSON-LD,
        # sitemap <loc>, robots.txt) get this prefix instead of staying on
        # the origin host -- for hosts without serve-time substitution
        self.target_prefix = (f"https://{cfg.target_domain}"
                              if cfg.target_domain else "")

        self._build_host_regexes()

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
        self.robots_txt_found = False
        self.robots_action = "unchanged"   # what finalize_robots() did
        self.origin_sitemap_paths: list[str] = []  # every parsed origin sitemap
        self.truncated = False             # crawl hit --max-pages
        # --exclude patterns (validated in parse_args) + robots.txt rules
        # ((pattern_length, is_allow, regex), longest match wins)
        self.exclude_res = [re.compile(p) for p in cfg.excludes]
        self.robots_rules: list[tuple[int, bool, re.Pattern]] = []
        self.robots_crawl_delay = 0.0
        self.excluded_urls: set[str] = set()
        # split-DNS detection of additional same-site hosts
        self._dns_cache: dict[str, bool] = {}
        self.auto_internal_hosts: list[str] = []
        self.foreign_wp_hosts: set[str] = set()
        self.generated_sitemap: dict | None = None
        self.sitemap_discovery_ok = False  # a usable origin sitemap was found
        self.cruft_removed: dict[str, int] = {}
        self.stats_lock = threading.Lock()
        self.deploy_info: dict = {}
        # HTML files parse_and_save_html wrote (re-serialized by bs4) --
        # postprocess_html must only touch these, never byte-identical
        # HTML assets
        self.rewritten_html_paths: set[Path] = set()
        self._sitemap_cap_warned = False
        # global politeness gate: --delay is the minimum spacing between
        # ANY two requests, not a per-worker sleep
        self._rate_lock = threading.Lock()
        self._next_request = 0.0
        self.skipped_query_urls: set[str] = set()
        self.external_hosts: set[str] = set()
        self.sitemap_files: list[str] = []
        self.warnings: list[str] = []
        self.soft_404 = False
        self.asset_count = 0
        self.amp_links: set[str] = set()
        self.verify_missing: list[dict] = []
        self.verify_unexpected: list[dict] = []
        # URLs derived from runtime URL-construction patterns (e.g. Slider
        # Revolution module lists): fetched if the origin has them, silently
        # skipped if not -- a 404 here is not an export error
        self.speculative_urls: set[str] = set()

        # fresh plugin instances per run -- plugin state (stats, caches)
        # must never leak between Exporter instances
        self.plugins: list[Plugin] = [cls(self) for cls in PLUGIN_REGISTRY]

    def _build_host_regexes(self) -> None:
        """(Re)build the host-matching regexes for URL localization from
        the CURRENT internal_norms; called again when hosts are absorbed
        later (split-DNS detection). Covers the www. variant, both IDN
        spellings, and every textual form page builders emit: plain
        absolute, protocol-relative (//host/...) and JSON-escaped
        (https:\\/\\/host\\/...). internal_norms already includes the
        connect address when --host is used (staging-IP leaks in slider
        configs). The lookahead anchors the host: without it example.at
        would match inside example.athletic.de (and localize_text would
        destroy that external URL), and example.at:8443 -- a DIFFERENT
        origin -- would half-match. A port is only matched when it is part
        of the spelling itself."""
        spellings: set[str] = set()
        for n in self.internal_norms:
            spellings |= host_spellings(n)
        # scheme-default ports are the same origin: https://host:443/ and
        # http://host:80/ occasionally leak into markup. (Known limitation:
        # a trailing-dot FQDN spelling "host." is not matched.)
        for s in list(spellings):
            if ":" not in s:
                spellings.add(f"{s}:443")
                spellings.add(f"{s}:80")
        hp = (r"(?:"
              + "|".join(rf"(?:www\.)?{re.escape(s)}" for s in sorted(spellings))
              + r")(?![\w.-]|:\d)")
        self.text_url_re = re.compile(
            rf"(?:https?:)?//{hp}(/[^\s'\"<>()\\]*)?", re.IGNORECASE)
        self.esc_url_re = re.compile(
            rf"(?:https?:)?\\/\\/{hp}((?:\\/[^\s'\"<>()\\]*)*)", re.IGNORECASE)
        self.host_probe_re = re.compile(
            rf"(?:https?:)?(?:\\?/){{2}}{hp}", re.IGNORECASE)

    def _dns_private(self, host: str) -> bool:
        if host not in self._dns_cache:
            self._dns_cache[host] = resolves_private(host)
        return self._dns_cache[host]

    def _absorb_private_hosts(self) -> None:
        """Split-horizon DNS detection: probe the homepage for absolute
        hosts and treat every one that resolves to a private address (from
        THIS machine, which sits next to the origin) as another spelling of
        the site -- e.g. the internal WP admin domain the siteurl points
        at. Runs single-threaded before the crawl so the localization
        regexes can be rebuilt safely."""
        if not self.cfg.resolve_internal:
            return
        try:
            resp = self.fetch(f"{self.scheme}://{self.host}/")
        except requests.RequestException:
            return
        text = resp.text.replace("\\/", "/")
        seen: set[str] = set()
        for m in ABS_HOST_RE.finditer(text):
            norm = norm_host(m.group(1))
            if norm in seen or norm in self.internal_norms:
                continue
            seen.add(norm)
            if self._dns_private(norm):
                self.internal_norms.add(norm)
                self.auto_internal_hosts.append(norm)
                self.say(f"[discover] host {norm} resolves to a private "
                         f"address -- treating as internal")
        if self.auto_internal_hosts:
            self._build_host_regexes()

    def _warn_foreign_site_hosts(self) -> None:
        """Post-crawl safety net for same-site hosts that slipped through:
        privately-resolving stragglers and foreign hosts serving wp-content
        paths (likely the same WP install; not auto-localized because it
        could be a real upload CDN)."""
        if self.cfg.resolve_internal:
            for host in sorted(self.external_hosts):
                norm = norm_host(host)
                if norm not in self.internal_norms and self._dns_private(norm):
                    self.warnings.append(
                        f"host {host} resolves to a private address but was "
                        f"first seen after the crawl started -- re-run with "
                        f"--internal-host {host}")
        for host in sorted(self.foreign_wp_hosts):
            if norm_host(host) in self.internal_norms:
                continue
            self.warnings.append(
                f"host {host} serves wp-content paths -- it is very likely "
                f"THIS WordPress site under another name; re-run with "
                f"--internal-host {host} (ignore if it is a real CDN)")

    def plugin(self, name: str) -> Plugin:
        """The per-run instance of the named plugin."""
        for p in self.plugins:
            if p.name == name:
                return p
        raise KeyError(f"plugin {name!r} not loaded")

    def serialize(self, soup: BeautifulSoup) -> bytes:
        """Serialize a page soup through the plugin hooks: pre_serialize
        (tree-level, e.g. inline CSS/JS minification), str(), then
        post_serialize (byte-level, e.g. whitespace collapsing)."""
        for p in self.plugins:
            p.pre_serialize(soup)
        out = serialize_soup(soup)
        for p in self.plugins:
            out = p.post_serialize(out)
        return out

    def say(self, msg: str) -> None:
        """Progress chatter -- silenced by --quiet (warnings and the final
        summary always print)."""
        if not self.cfg.quiet:
            print(msg)

    # -- URL helpers -------------------------------------------------------

    def is_internal(self, url: str) -> bool:
        s = urlsplit(url)
        if s.scheme not in ("http", "https"):
            return False
        netloc = s.netloc
        default = ":443" if s.scheme == "https" else ":80"
        if netloc.endswith(default):
            netloc = netloc[: -len(default)]  # scheme-default port == same origin
        return (norm_host(netloc) in self.internal_norms
                or norm_host(s.netloc) in self.internal_norms)

    def is_excluded(self, ref: str) -> bool:
        """--exclude patterns plus (with --respect-robots) the robots.txt
        Allow/Disallow rules for User-agent: * -- longest match wins."""
        if not self.exclude_res and not self.robots_rules:
            return False
        path = urlsplit(ref).path or "/"
        for rx in self.exclude_res:
            if rx.search(path):
                self.excluded_urls.add(path)
                return True
        if self.robots_rules:
            allow = max((ln for ln, ok, rx in self.robots_rules
                         if ok and rx.match(path)), default=-1)
            deny = max((ln for ln, ok, rx in self.robots_rules
                        if not ok and rx.match(path)), default=-1)
            if deny > allow:
                self.excluded_urls.add(path)
                return True
        return False

    def _parse_robots(self, text: str) -> None:
        """Collect Allow/Disallow/Crawl-delay of the 'User-agent: *' groups
        (only called with --respect-robots)."""
        ua_match = False
        rule_seen = True
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, _, val = line.partition(":")
            key, val = key.strip().lower(), val.strip()
            if key == "user-agent":
                if rule_seen:               # a new UA group starts
                    ua_match, rule_seen = False, False
                ua_match = ua_match or val == "*"
            elif key in ("allow", "disallow"):
                rule_seen = True
                if ua_match and val:        # empty Disallow: = allow all
                    self.robots_rules.append(
                        (len(val), key == "allow", robots_rule_re(val)))
            elif key == "crawl-delay":
                rule_seen = True
                if ua_match:
                    try:
                        self.robots_crawl_delay = float(val.replace(",", "."))
                    except ValueError:
                        pass

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

    def normalize_page_url(self, url: str,
                           record_skips: bool = True) -> str | None:
        """Canonical form of a page URL, or None if it should be skipped.
        record_skips=False for report-time lookups (canonical checks) so
        they don't pollute the skipped-query-URL list."""
        url, _ = self._defrag(url)
        s = urlsplit(url)
        if s.query:
            if record_skips:
                self.skipped_query_urls.add(url)
            return None
        path = s.path or "/"
        if PAGE_SKIP_PATTERNS.search(path):
            return None
        if self.is_excluded(path):
            return None
        # never pages: extensionless plugin/theme directories referenced
        # from JS configs, and plugin-registered runtime endpoints (e.g.
        # Cloudflare's /cdn-cgi/, WPForms AJAX routes)
        if (path.startswith(("/wp-content/", "/wp-includes/"))
                and not path_extension(path)):
            return None
        if any(p.skip_page_path(path) for p in self.plugins):
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
        try:
            # resolve() itself raises ValueError on e.g. embedded NUL (%00)
            target = (self.public_dir / norm).resolve()
            target.relative_to(self.public_dir.resolve())
        except ValueError:
            return None
        return target

    def write_bytes(self, target: Path, data: bytes) -> Path | None:
        """Collision-safe write. First write to a path wins (a URL can reach
        us both as a page and as an 'asset'); a target that already exists
        as a directory falls back to its index.html; conflicting ancestors
        (an extensionless file where a directory is needed, or vice versa)
        are skipped with a warning instead of crashing the crawl.

        Returns the path that was ACTUALLY written (may differ from target,
        e.g. the index.html fallback), or None when nothing was written --
        callers tracking their writes must use the return value."""
        with self.write_lock:
            if target in self.written_paths:
                return None
            if _isdir(target):
                target = target / "index.html"
                if target in self.written_paths:
                    return None
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            except OSError as exc:
                # path conflicts, over-long names (ENAMETOOLONG), permission
                # or space errors -- warn and skip, like write_stream()
                self.warnings.append(
                    f"path conflict, not written: {target} ({exc.__class__.__name__})")
                return None
            self.written_paths.add(target)
            return target

    def write_stream(self, target: Path, resp: requests.Response) -> bool:
        """Chunked write for large media -- same first-write-wins semantics
        as write_bytes(), without buffering the body in RAM."""
        with self.write_lock:
            if target in self.written_paths:
                return False
            if _isdir(target):
                target = target / "index.html"
                if target in self.written_paths:
                    return False
            self.written_paths.add(target)      # reserve, then write unlocked
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
        except (OSError, requests.RequestException) as exc:
            self.warnings.append(f"stream write failed: {target} "
                                 f"({exc.__class__.__name__})")
            with self.write_lock:
                self.written_paths.discard(target)
            try:
                target.unlink()
            except OSError:
                pass
            return False
        finally:
            resp.close()
        return True

    # -- fetching ----------------------------------------------------------

    def fetch(self, url: str, headers: dict | None = None,
              stream: bool = False) -> requests.Response:
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
                # global gate across all workers: without it -c 8 --delay
                # 0.2 would mean ~40 req/s instead of 5
                with self._rate_lock:
                    now = time.monotonic()
                    wait = self._next_request - now
                    self._next_request = max(now, self._next_request) + self.cfg.delay
                if wait > 0:
                    time.sleep(wait)
            resp = self.session.get(self.to_connect_url(current),
                                    timeout=self.cfg.timeout,
                                    headers=headers, verify=self.verify,
                                    allow_redirects=False, stream=stream)
            if self.cfg.host_header:
                resp.url = current  # history/report/callers see logical URLs
            if not (resp.is_redirect or resp.is_permanent_redirect) or hop == 5:
                break
            nxt = urljoin(current, resp.headers.get("Location") or "")
            if (not nxt or not self.is_internal(nxt)
                    or PAGE_SKIP_PATTERNS.search(urlsplit(nxt).path)):
                # off-site, malformed, or dynamic-endpoint redirect
                # (e.g. protected page -> /wp-login.php): stop, never follow
                break
            hops.append(resp)
            if stream:
                resp.close()   # unread streamed body would pin the connection
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
            if resp.ok and resp.content and not self.off_site_redirect(resp):
                self.robots_txt_found = True
                target = self.local_path_for(robots_url, is_page=False)
                if target:
                    self.write_bytes(target, resp.content)
                for m in SITEMAP_LINE_RE.finditer(resp.text):
                    sitemap_candidates.append(m.group(1).strip())
                if self.cfg.respect_robots:
                    self._parse_robots(resp.text)
                    if self.robots_crawl_delay > self.cfg.delay:
                        self.cfg.delay = self.robots_crawl_delay
                        self.say(f"[discover] robots.txt Crawl-delay "
                                 f"{self.robots_crawl_delay:g}s adopted")
                    if self.robots_rules:
                        self.say(f"[discover] respecting "
                                 f"{len(self.robots_rules)} robots.txt "
                                 f"Allow/Disallow rules")
                if not self.cfg.staging and re.search(
                        r"(?im)^\s*disallow:\s*/\s*$", resp.text):
                    self.warnings.append(
                        "robots.txt contains 'Disallow: /' -- the site is blocked "
                        "for crawlers. Intentional?")
                self.say(f"[discover] robots.txt found "
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
                # compare/walk extras in NORMALIZED form -- seen_sitemaps
                # records normalized URLs, so a www./non-www spelling of an
                # already-walked sitemap must not be fetched a second time
                remaining = []
                for c in sitemap_candidates:
                    if c not in self.cfg.extra_sitemaps:
                        continue
                    cn = self.to_base_host(c) if self.is_internal(c) else c
                    if cn not in seen_sitemaps and cn not in remaining:
                        remaining.append(cn)
                for extra in remaining:
                    page_urls.extend(self.walk_sitemap(extra, seen_sitemaps, depth=0))
                break

        self.sitemap_discovery_ok = found_any
        if not found_any:
            self.warnings.append(
                "No usable XML sitemap found -- falling back to link crawling "
                "from the homepage. Orphan pages will be missing.")
            page_urls.append(f"{self.scheme}://{self.host}/")
        return page_urls

    def walk_sitemap(self, url: str, seen: set[str], depth: int) -> list[str]:
        if url in seen:
            return []
        if depth > 5 or len(seen) > 200:
            if not self._sitemap_cap_warned:
                self._sitemap_cap_warned = True
                self.warnings.append(
                    f"sitemap graph cap reached (depth > 5 or > 200 sitemap "
                    f"files) -- further sitemaps are skipped starting at "
                    f"{url}; page discovery may be incomplete")
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
        self.origin_sitemap_paths.append(urlsplit(url).path)

        # keep the origin sitemap file itself in the export -- only when no
        # fresh sitemap is generated from the exported URL set (default is
        # generation; shipping both, with the origin one listing dropped
        # query URLs / noindexed / redirected pages, would be worse)
        if not self.cfg.generate_sitemap:
            data = resp.content
            if self.cfg.rewrite:
                # localize the xml-stylesheet PI so the sitemap renders
                # without the origin; <loc> entries deliberately stay
                # absolute (SEO)
                pi = XML_STYLESHEET_RE.search(
                    data[:2048].decode("utf-8", "replace"))
                if pi:
                    xsl_abs = urljoin(url, pi.group(1))
                    if self.is_internal(xsl_abs):
                        local = self.localize_url(self.to_base_host(xsl_abs))
                        data = data.replace(pi.group(1).encode("utf-8"),
                                            local.encode("utf-8"), 1)
                if self.target_prefix:
                    # --target-domain promises NO origin URL survives --
                    # that includes the <loc> entries of kept origin sitemaps
                    data = self.retarget_text(
                        data.decode("utf-8", "replace")).encode("utf-8")
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
            self.say(f"[discover] {urlsplit(url).path}: {count} URLs")
        return urls

    # ----------------------------------------------------------------------
    # Phase 2: crawl
    # ----------------------------------------------------------------------

    def crawl(self, seed_pages: list[str]) -> None:
        pending_pages: list[tuple[str, str]] = []
        for u in seed_pages:
            if len(self.page_urls_seen) >= self.cfg.max_pages:
                break                   # --max-pages also caps sitemap seeds
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
                self.say(f"[crawl] round {round_no}: {len(pending_pages)} "
                         f"pages, {len(pending_assets)} assets")
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
            self.truncated = True
            self.warnings.append(
                f"--max-pages ({self.cfg.max_pages}) reached, crawl truncated.")

    # -- page processing ---------------------------------------------------

    def process_page(self, url: str, source: str) -> tuple[list[str], list[str]]:
        rec = PageRecord(url=url, source=source)
        self.pages.append(rec)
        try:
            return self._process_page(url, rec)
        except requests.RequestException as exc:
            rec.error = str(exc)
            return [], []
        except Exception as exc:                    # noqa: BLE001
            # mark the record BEFORE safe_page() swallows the exception --
            # otherwise a crashed worker counts as an exported page (and
            # would land in the generated sitemap)
            rec.error = f"unexpected: {exc!r}"
            raise

    def _process_page(self, url: str, rec: PageRecord) -> tuple[list[str], list[str]]:
        resp = self.fetch(url)
        rec.status = resp.status_code
        rec.final_url = resp.url
        rec.content_type = (resp.headers.get("Content-Type") or "").split(";")[0]
        rec.last_modified = resp.headers.get("Last-Modified", "")

        if resp.history:
            self.redirects.append({
                "from": url, "to": resp.url,
                "status": resp.history[0].status_code})
        off_site = self.off_site_redirect(resp)
        if off_site:
            if self.is_internal(off_site):
                if PAGE_SKIP_PATTERNS.search(urlsplit(off_site).path):
                    rec.error = f"redirects to a dynamic endpoint ({off_site})"
                else:
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
                if (retry is not None and retry.ok
                        and not self.off_site_redirect(retry)):
                    resp = retry
                    rec.status = resp.status_code
                    rec.final_url = resp.url
                    rec.content_type = (
                        resp.headers.get("Content-Type") or "").split(";")[0]
                    rec.last_modified = resp.headers.get("Last-Modified", "")
        if not resp.ok:
            rec.error = f"HTTP {resp.status_code}"
            return [], []
        if not self.is_internal(resp.url):
            rec.error = f"redirected off-site to {resp.url}"
            return [], []

        # non-HTML target: a PDF listed in the sitemap, or a response a
        # plugin claims (e.g. a dynamic download endpoint that must become
        # a real file)
        if "html" not in rec.content_type:
            url_b = self.to_base_host(resp.url)
            if not any(p.save_non_html_response(url_b, resp)
                       for p in self.plugins):
                target = self.local_path_for(url_b, is_page=False)
                if target:
                    self.write_bytes(target, resp.content)
            return [], []

        for p in self.plugins:
            p.page_fetched(url, resp, rec)

        final = self.to_base_host(resp.url)
        if (resp.history
                and self.normalize_page_url(final, record_skips=False) is None
                and urlsplit(final).path != urlsplit(url).path):
            # redirect onto a query-string URL under a DIFFERENT path: that
            # target is not part of the export, and saving its body here
            # would duplicate it -- write a noindex stub instead. (A query-
            # only self-redirect like /a/ -> /a/?x=1 falls through: the
            # target IS this page, its body belongs at the source path.)
            rec.is_stub = True
            rec.save_url = url
            if self.cfg.rewrite:
                self._write_redirect_stub(url, final)
            return [], []
        save_url = self.normalize_page_url(final) or url
        rec.save_url = save_url
        if resp.history and self.cfg.rewrite and save_url != url:
            # keep redirect sources working in the static mirror: a
            # meta-refresh stub at the original path points to the target.
            # noindex'ed so the weak meta-refresh signal never competes with
            # the real page (the nginx config 301s this path anyway).
            self._write_redirect_stub(url, save_url)
        new_pages, new_assets = self.parse_and_save_html(
            save_url, resp.content, rec)
        for p in self.plugins:
            p.page_saved(save_url, resp, rec)
        return new_pages, new_assets

    def _write_redirect_stub(self, src_url: str, dest_abs: str) -> None:
        """noindex'ed meta-refresh page at the redirect source path."""
        src_target = self.local_path_for(src_url, is_page=True)
        if not src_target:
            return
        dest = self.localize_url(dest_abs)
        stub = ('<!doctype html><html><head><meta charset="utf-8">'
                '<meta name="robots" content="noindex">'
                f'<meta http-equiv="refresh" content="0;url={dest}">'
                f'<link rel="canonical" href="{self.retarget_url(dest_abs)}">'
                '<title>Redirect</title></head>'
                f'<body><a href="{dest}">{dest}</a></body></html>')
        self.write_bytes(src_target, stub.encode("utf-8"))

    def parse_and_save_html(self, page_url: str, raw: bytes,
                            rec: PageRecord | None,
                            save_as: Path | None = None) -> tuple[list[str], list[str]]:
        soup = BeautifulSoup(raw, "html.parser")
        if self.cfg.rewrite:
            for p in self.plugins:
                p.pre_discover_soup(soup, page_url)
        new_pages: list[str] = []
        new_assets: list[str] = []

        def note_external(u: str) -> None:
            host = urlsplit(u).netloc
            if host:
                self.external_hosts.add(host)
                if FOREIGN_WP_RE.match(u):
                    self.foreign_wp_hosts.add(host)

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
                else:
                    # query-string link (e.g. a /download/123/?cachebuster
                    # endpoint): the query variant is not exportable, but
                    # the bare path may be a real page or download
                    s = urlsplit(absolute)
                    if s.query:
                        n = self.normalize_page_url(
                            urlunsplit((s.scheme, s.netloc, s.path, "", "")),
                            record_skips=False)
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
            elif tag_name == "link" and rel & {"next", "prev"}:
                # paginated archives often surface only here
                n = self.normalize_page_url(absolute)
                if n:
                    new_pages.append(n)
            elif tag_name == "link" and rel & {"canonical", "alternate",
                                               "shortlink", "pingback"}:
                return  # SEO/meta links, never fetched as assets
            elif any(p.skip_asset_candidate(absolute, tag_name, rel)
                     for p in self.plugins):
                return  # e.g. targets of tags a plugin strips anyway
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
                if attr in CSS_URL_ATTRS and "url(" in val:
                    # some themes store a full CSS declaration in the
                    # attribute (e.g. data-bg)
                    for m in CSS_URL_RE.finditer(val):
                        handle_candidate(m.group(1), "style", set())
                else:
                    handle_candidate(val, tag.name, rel)
            for attr in SRCSET_ATTRS:
                val = tag.get(attr)
                if val:
                    for cand, _ in iter_srcset(val):
                        if cand:
                            handle_candidate(cand, "img", rel)
            for attr in PAGE_URL_ATTRS:
                val = tag.get(attr)
                if val:
                    handle_candidate(val, "a", rel)
            for attr in B64_URL_ATTRS:
                bval = tag.get(attr)
                if not bval:
                    continue
                decoded = try_b64_url(bval)
                if decoded:
                    handle_candidate(decoded, "img", rel)
                else:
                    # some installs put a PLAIN URL into the b64 attribute
                    pages, assets = self.scan_text_for_urls(bval)
                    new_pages.extend(pages)
                    new_assets.extend(assets)
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

        # og:image / twitter:image -> download, tag stays untouched;
        # meta-refresh targets are internal PAGE links
        for meta in soup.find_all("meta"):
            key = (meta.get("property") or meta.get("name") or "").lower()
            if key in ("og:image", "og:image:url", "twitter:image"):
                val = meta.get("content")
                if val:
                    handle_candidate(val, "meta-img", set())
            if (meta.get("http-equiv") or "").lower() == "refresh":
                m = META_REFRESH_RE.search(meta.get("content") or "")
                if m:
                    handle_candidate(m.group(1).strip("'\" "), "a", set())

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
                if self.cfg.strip_wp_cruft:
                    self.strip_resource_hints(soup)
                    for p in self.plugins:
                        p.clean_soup(soup)
                self.rewrite_soup_relative(soup)
                for p in self.plugins:
                    p.rewrite_soup(soup)
                out = self.serialize(soup)
                written = self.write_bytes(target, out)
                if written is not None:
                    for p in self.plugins:
                        p.text_asset_written("html", len(raw), len(out))
                    with self.stats_lock:
                        # postprocess_html may only touch files WE
                        # actually re-serialized and wrote
                        self.rewritten_html_paths.add(written)
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
        for p in self.plugins:
            work = p.expand_scan_text(work)
        for p in self.plugins:
            pg, ast = p.scan_text_urls(work)
            pages.extend(pg)
            assets.extend(ast)
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

    def _replace_host(self, text: str, prefix: str) -> str:
        """Replace every textual origin-URL form (plain, protocol-relative,
        JSON-escaped): prefix '' localizes to root-relative, an
        'https://new.tld' prefix retargets to that domain."""
        def plain(m: re.Match) -> str:
            return prefix + canon_ref(m.group(1) or "/")

        def escaped(m: re.Match) -> str:
            ref = (m.group(1) or "").replace("\\/", "/")
            return (prefix + canon_ref(ref or "/")).replace("/", "\\/")

        return self.esc_url_re.sub(escaped, self.text_url_re.sub(plain, text))

    def localize_text(self, text: str) -> str:
        """Host-strip every textual URL form (plain, protocol-relative,
        JSON-escaped) inside JS/CSS/JSON text."""
        return self._replace_host(text, "")

    def retarget_text(self, text: str) -> str:
        """--target-domain twin of localize_text for SEO-bearing text
        (meta content, JSON-LD)."""
        return self._replace_host(text, self.target_prefix)

    def retarget_url(self, u: str) -> str:
        """Same-site URL moved onto --target-domain; unchanged without one."""
        cand = ("https:" + u) if u.startswith("//") else u
        if not self.target_prefix or not self.is_internal(cand):
            return u
        return self.target_prefix + self.localize_url(cand)

    def localize_b64(self, val: str) -> str:
        decoded = try_b64_url(val)
        if decoded is None:
            return val
        localized = self.localize_url(decoded)
        if localized == decoded:
            return val
        # keep the original alphabet -- the runtime that decodes the
        # attribute expects the same variant it emitted
        enc = (base64.urlsafe_b64encode if ("-" in val or "_" in val)
               else base64.b64encode)
        return enc(localized.encode("utf-8")).decode("ascii")

    def strip_resource_hints(self, soup: BeautifulSoup) -> None:
        """Remove resource-hint <link>s: they trigger connection attempts
        WITHOUT a visible network request -- pointing at an internal host
        they make browsers show the Local Network Access permission prompt
        (same policy as verify_export's private-URL checks). dns-prefetch
        is a pure WP automatism: always dropped; preconnect only when it
        targets our own (localized) or a private host. The WP-specific
        head cruft lives in plugins/wordpress.py (clean_soup)."""
        removed = 0
        for link in soup.find_all("link"):
            rel_attr = link.get("rel")
            rel = {r.lower() for r in (rel_attr if isinstance(rel_attr, list)
                                       else (rel_attr or "").split())}
            if "dns-prefetch" in rel:
                link.decompose()
                removed += 1
                continue
            if "preconnect" in rel:
                href_abs = link.get("href") or ""
                if href_abs.startswith("//"):
                    href_abs = "https:" + href_abs
                if self.is_internal(href_abs) or PRIVATE_NET_RE.match(href_abs):
                    link.decompose()
                    removed += 1
        if removed:
            with self.stats_lock:
                self.cruft_removed["resource-hints"] = (
                    self.cruft_removed.get("resource-hints", 0) + removed)

    def rewrite_soup_relative(self, soup: BeautifulSoup) -> None:
        """Rewrite same-site URLs to root-relative -- in every form page
        builders emit them: plain absolute, protocol-relative, JSON-escaped
        inside inline scripts/attributes, base64-encoded lazy-load attrs.

        canonical / rel=alternate / wp-json <link>s, <meta> tags (og:*,
        twitter:*) and JSON-LD stay absolute on purpose: SEO data, never
        fetched by browsers. With --target-domain they are rewritten onto
        the new domain instead of staying on the origin."""
        for tag in soup.find_all(True):
            if tag.name == "meta":
                if (tag.get("http-equiv") or "").lower() == "refresh":
                    # meta-refresh targets ARE navigated by browsers --
                    # localize them like links (og:*/twitter:* stay absolute)
                    content = tag.get("content") or ""
                    m = META_REFRESH_RE.search(content)
                    if m:
                        tag["content"] = (
                            content[:m.start(1)]
                            + self.localize_url(m.group(1).strip("'\" ")))
                    continue
                if self.target_prefix:
                    content = tag.get("content")
                    if (isinstance(content, str)
                            and self.host_probe_re.search(content)):
                        tag["content"] = self.retarget_text(content)
                continue
            rel_attr = tag.get("rel")
            rel = {r.lower() for r in (rel_attr if isinstance(rel_attr, list)
                                       else (rel_attr or "").split())}
            if tag.name == "link":
                href = tag.get("href") or ""
                skip_href = PAGE_SKIP_PATTERNS.search(urlsplit(href).path)
                if rel & {"canonical", "alternate"} or skip_href:
                    # SEO links and dynamic-endpoint metadata (wp-json,
                    # xmlrpc EditURI/pingback) stay absolute: browsers
                    # never fetch them, and localizing them would just
                    # create dead same-origin links
                    if self.target_prefix and href and not skip_href:
                        tag["href"] = self.retarget_url(href)
                    continue
            for attr, val in list(tag.attrs.items()):
                if not isinstance(val, str) or not val:
                    continue
                if attr in SRCSET_ATTRS:
                    parts = []
                    changed = False
                    for cand, desc in iter_srcset(val):
                        new = self.localize_url(cand)
                        changed |= new != cand
                        parts.append(f"{new} {desc}".strip())
                    if changed:     # untouched srcsets keep their exact bytes
                        tag[attr] = ", ".join(parts)
                elif attr == "style" or (attr in CSS_URL_ATTRS
                                         and "url(" in val):
                    tag[attr] = CSS_URL_RE.sub(
                        lambda m: f"url('{self.localize_url(m.group(1))}')", val)
                elif attr in B64_URL_ATTRS:
                    new = self.localize_b64(val)
                    if new == val and self.host_probe_re.search(val):
                        # a PLAIN URL sitting in the b64 attribute
                        new = self.localize_text(val)
                    tag[attr] = new
                elif attr in URL_ATTRS or attr in PAGE_URL_ATTRS:
                    tag[attr] = self.localize_url(val)
                elif attr not in TEXT_ATTRS and self.host_probe_re.search(val):
                    # NOT for alt/title & co. -- URLs there are prose
                    tag[attr] = self.localize_text(val)
        for style in soup.find_all("style"):
            if style.string and self.host_probe_re.search(style.string):
                style.string = self.localize_text(style.string)
        for script in soup.find_all("script"):
            stype = (script.get("type") or "").lower()
            if "ld+json" in stype:
                # SEO structured data stays absolute (or moves to the
                # --target-domain as one piece)
                if (self.target_prefix and script.string
                        and self.host_probe_re.search(script.string)):
                    script.string = self.retarget_text(script.string)
                continue
            if script.string and self.host_probe_re.search(script.string):
                script.string = self.localize_text(script.string)

    # -- asset processing --------------------------------------------------

    def process_asset(self, url: str) -> tuple[list[str], list[str]]:
        if PAGE_SKIP_PATTERNS.search(urlsplit(url).path):
            return [], []  # dynamic endpoint, useless statically
        if self.is_excluded(url):
            return [], []  # --exclude / robots Disallow
        # large media is streamed to disk instead of buffered in RAM
        stream = path_extension(urlsplit(url).path) in MEDIA_EXTENSIONS
        try:
            resp = self.fetch(url, stream=stream)
        except requests.RequestException as exc:
            self.asset_errors.append({"url": url, "error": str(exc)})
            return [], []
        off_site = self.off_site_redirect(resp)
        if off_site:
            if stream:
                resp.close()    # unread streamed body would pin a connection
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
            if stream:
                resp.close()
            if url not in self.speculative_urls:
                self.asset_errors.append(
                    {"url": url, "error": f"HTTP {resp.status_code}"})
            return [], []
        if not self.is_internal(resp.url):
            if stream:
                resp.close()
            self.asset_errors.append({"url": url,
                                      "error": f"redirected off-site to {resp.url}"})
            return [], []

        discovered_pages: list[str] = []
        discovered: list[str] = []
        ext = path_extension(urlsplit(url).path)
        ctype = (resp.headers.get("Content-Type") or "").lower()

        if stream:
            if "html" in ctype:
                self.asset_errors.append({
                    "url": url,
                    "error": f"origin returned HTML for .{ext} asset URL "
                             f"(resource broken on the origin site as well)"})
                resp.close()
                return [], []
            target = self.local_path_for(url, is_page=False)
            if target and self.write_stream(target, resp):
                with self.stats_lock:
                    self.asset_count += 1
            else:
                resp.close()
            return [], []

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

        if not ext and "html" not in ctype:
            # extensionless URL answering with a file, e.g. a dynamic
            # download endpoint reached through an asset context
            if any(p.save_non_html_response(self.to_base_host(resp.url), resp)
                   for p in self.plugins):
                return [], []

        if "html" in ctype and not ext:
            # extensionless URL answering with HTML: that's a page (typically
            # a WordPress *attachment page* referenced in an asset context,
            # e.g. <img src="/home/bg-boxanschrift">). Store it under its
            # page path (.../index.html) and run it through the full page
            # pipeline -- saved raw it would keep absolute origin URLs and
            # its own assets would never be discovered.
            target = self.local_path_for(self.to_base_host(resp.url), is_page=True)
            if target:
                page_url = (self.normalize_page_url(self.to_base_host(resp.url),
                                                    record_skips=False)
                            or self.to_base_host(resp.url))
                return self.parse_and_save_html(page_url, data, rec=None,
                                                save_as=target)
            return [], []

        if ext == "css" or "text/css" in ctype:
            text = decode_text_asset(resp)
            # relative url() refs resolve against where the file was SERVED
            # from (after internal redirects), not where it was requested --
            # the browser on the origin resolved them the same way
            css_base = (self.to_base_host(resp.url)
                        if self.is_internal(resp.url) else url)
            for m in list(CSS_URL_RE.finditer(text)) + list(CSS_IMPORT_RE.finditer(text)):
                ref = m.group(1).strip()
                if ref.startswith(("data:", "#")):
                    continue
                absolute = urljoin(css_base, ref)
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
                    absolute = urljoin(css_base, u)
                    if self.is_internal(absolute):
                        return f"url('{self.localize_url(absolute)}')"
                    return m.group(0)

                def rel_import(m: re.Match) -> str:
                    absolute = urljoin(css_base, m.group(1).strip())
                    if self.is_internal(absolute):
                        return f"@import '{self.localize_url(absolute)}'"
                    return m.group(0)

                new_text = CSS_IMPORT_RE.sub(
                    rel_import, CSS_URL_RE.sub(rel, text))
                for p in self.plugins:
                    new_text = p.filter_text_asset(url, "css", new_text)
                if new_text != text:
                    # transcode only what we changed; the file is now UTF-8,
                    # so a declared legacy @charset must say so too
                    data = CSS_CHARSET_RE.sub('@charset "UTF-8"',
                                              new_text).encode("utf-8")
                    for p in self.plugins:
                        p.text_asset_written("css", len(resp.content),
                                             len(data))
        elif ext in ("js", "mjs") or "javascript" in ctype:
            text = decode_text_asset(resp)
            page_refs, asset_refs = self.scan_text_for_urls(text)
            discovered_pages.extend(page_refs)
            discovered.extend(asset_refs)
            if self.cfg.rewrite:
                new_text = text
                if self.host_probe_re.search(new_text):
                    new_text = self.localize_text(new_text)
                for p in self.plugins:
                    new_text = p.filter_text_asset(url, "js", new_text)
                if new_text != text:
                    data = new_text.encode("utf-8")
                    for p in self.plugins:
                        p.text_asset_written("js", len(resp.content),
                                             len(data))
        elif ext in ("json", "webmanifest") or "json" in ctype:
            # PWA manifests / JSON data: their start_url / icon / api URLs
            # behave like the ones in JS -- discover and localize them
            # (no minification, no plugin text filters)
            text = decode_text_asset(resp)
            page_refs, asset_refs = self.scan_text_for_urls(text)
            discovered_pages.extend(page_refs)
            discovered.extend(asset_refs)
            if self.cfg.rewrite and self.host_probe_re.search(text):
                new_text = self.localize_text(text)
                if new_text != text:
                    data = new_text.encode("utf-8")

        target = self.local_path_for(url, is_page=False)
        if target and self.write_bytes(target, data) is not None:
            with self.stats_lock:
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
                # parse it too, so the 404 page's own assets are exported --
                # the crawl is already over, so fetch them right here
                # (bounded mini-BFS for assets referenced by those assets)
                _, assets = self.parse_and_save_html(
                    f"{self.scheme}://{self.host}/404.html", resp.content,
                    rec=None, save_as=self.public_dir / "404.html")
                pending = [a for a in assets if a not in self.asset_urls_seen]
                for _ in range(3):
                    if not pending:
                        break
                    nxt: list[str] = []
                    for a in pending:
                        if a in self.asset_urls_seen:
                            continue
                        self.asset_urls_seen.add(a)
                        _, more = self.process_asset(a)
                        nxt.extend(more)
                    pending = [a for a in nxt if a not in self.asset_urls_seen]
                self.say("[extras] 404 page saved as 404.html")
        elif resp.ok:
            self.soft_404 = True
            self.warnings.append(
                "Site answers HTTP 200 for a nonsense URL (soft 404) -- "
                "no 404.html captured; check the theme/SEO setup.")
        else:
            self.warnings.append(
                f"404 probe answered HTTP {resp.status_code} -- "
                f"no themed 404.html captured.")

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
    # Phase 3c: SEO finalization -- sitemap, robots.txt, HTML post-process
    # ----------------------------------------------------------------------

    def canonical_target(self, p: PageRecord) -> str | None:
        """Normalized internal canonical URL of a page, or None."""
        if p.canonical and self.is_internal(p.canonical):
            return self.normalize_page_url(self.to_base_host(p.canonical),
                                           record_skips=False)
        return None

    def _loc_prefix(self) -> str:
        """scheme://host for generated sitemap <loc> / robots Sitemap: lines.
        Stays on the origin host (the nginx sub_filter localizes it at
        serve time) unless --target-domain retargets the whole export.
        Scheme: a crawl over plain http against an internal origin with
        --header 'X-Forwarded-Proto: https' means the PUBLIC site is https
        -- emitting http:// there would contradict the pages' own canonical
        tags (verified on a real export)."""
        if self.target_prefix:
            return self.target_prefix
        xfp = next((v for k, v in self.cfg.extra_headers.items()
                    if k.lower() == "x-forwarded-proto"), "")
        scheme = ("https" if self.scheme == "https"
                  or xfp.strip().lower() == "https" else "http")
        return f"{scheme}://{self.host}"

    def write_generated_sitemap(self) -> None:
        """Write /sitemap.xml describing the exported URL set. The origin
        sitemaps may list dropped query URLs, noindexed or redirected pages
        -- this one is truthful about what the mirror actually serves."""
        entries: dict[str, str] = {}            # save_url -> lastmod ISO date
        excluded_noindex = excluded_canonical = 0
        excluded_redirects = excluded_linked = 0
        for p in self.pages:
            if p.error or "html" not in p.content_type:
                continue
            if p.is_stub:
                excluded_redirects += 1     # only a redirect stub lives here
                continue
            if (p.source != "sitemap" and self.sitemap_discovery_ok
                    and not self.cfg.sitemap_include_linked):
                # the origin sitemap deliberately excluded these (attachment
                # pages, cache dirs, ...) -- don't ask Google to index them
                excluded_linked += 1
                continue
            sm_url = p.save_url or p.url
            if sm_url != p.url:
                excluded_redirects += 1         # source path not listed; the
                                                # redirect target still is
            if p.noindex:
                excluded_noindex += 1
                continue
            if (p.canonical and self.is_internal(p.canonical)
                    and self.canonical_target(p) != sm_url):
                excluded_canonical += 1         # page declares another URL as
                continue                        # canonical -- list that one
            lastmod = ""
            if p.last_modified:
                try:
                    lastmod = parsedate_to_datetime(
                        p.last_modified).date().isoformat()
                except (TypeError, ValueError):
                    pass
            if sm_url not in entries or (lastmod and not entries[sm_url]):
                entries[sm_url] = lastmod

        if not entries:
            self.warnings.append(
                "no indexable pages -- sitemap.xml NOT generated")
            return

        def xml_escape(s: str) -> str:
            return (s.replace("&", "&amp;").replace("<", "&lt;")
                     .replace(">", "&gt;"))

        prefix = self._loc_prefix()
        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for url in sorted(entries):
            lines.append("  <url>")
            lines.append(f"    <loc>{xml_escape(prefix + urlsplit(url).path)}"
                         "</loc>")
            if entries[url]:
                lines.append(f"    <lastmod>{entries[url]}</lastmod>")
            lines.append("  </url>")
        lines.append("</urlset>")
        # written directly, not via write_bytes(): its first-write-wins
        # bookkeeping must never block regenerating this well-known path
        (self.public_dir / "sitemap.xml").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
        self.generated_sitemap = {
            "path": "/sitemap.xml",
            "url_count": len(entries),
            "excluded_noindex": excluded_noindex,
            "excluded_canonical_mismatch": excluded_canonical,
            "excluded_link_discovered": excluded_linked,
            "redirected_source_urls_not_listed": excluded_redirects,
        }
        extras = [f"{n} {label} excluded"
                  for n, label in ((excluded_noindex, "noindex"),
                                   (excluded_linked, "link-only"))
                  if n]
        self.say(f"[seo] sitemap.xml generated: {len(entries)} URLs"
                 + (f" ({', '.join(extras)})" if extras else ""))

    def finalize_robots(self) -> None:
        """Bring robots.txt in line with the export: --staging blocks
        everything, otherwise the Sitemap: line points at the (generated)
        sitemap and a missing robots.txt is created. Direct writes on
        purpose -- write_bytes() first-write-wins already holds the origin
        copy of this path."""
        robots_path = self.public_dir / "robots.txt"
        if self.cfg.staging:
            robots_path.write_text("User-agent: *\nDisallow: /\n",
                                   encoding="utf-8")
            self.robots_action = "staging-disallow-all"
            return
        sitemap_line = (f"Sitemap: {self._loc_prefix()}/sitemap.xml"
                        if self.generated_sitemap else None)
        if robots_path.is_file():
            text = robots_path.read_text("utf-8", errors="replace")
            if self.target_prefix:
                # --target-domain: origin Sitemap:/Host: lines must move too
                text = self.retarget_text(text)
            if sitemap_line:
                text = SITEMAP_LINE_RE.sub("", text)
                text = re.sub(r"\n{3,}", "\n\n", text).strip("\n")
                text = (text + "\n\n" if text else "") + sitemap_line + "\n"
                self.robots_action = "sitemap-line-rewritten"
            elif self.cfg.generate_sitemap:
                # generation was expected but produced no entries (e.g. every
                # page noindex'ed): origin Sitemap: lines would point at
                # files this export does not contain -- strip them
                stripped = SITEMAP_LINE_RE.sub("", text)
                if stripped != text:
                    text = re.sub(r"\n{3,}", "\n\n", stripped).strip("\n") + "\n"
                    self.robots_action = "sitemap-lines-removed"
                    self.warnings.append(
                        "sitemap generation produced no entries -- origin "
                        "Sitemap: line(s) removed from robots.txt (their "
                        "files are not part of this export)")
                elif self.target_prefix:
                    self.robots_action = "retargeted"
                else:
                    return
            elif self.target_prefix:
                self.robots_action = "retargeted"
            else:
                return                          # --no-generate-sitemap: keep
            robots_path.write_text(text, encoding="utf-8")
        else:
            body = "User-agent: *\nAllow: /\n"
            if sitemap_line:
                body += "\n" + sitemap_line + "\n"
            robots_path.write_text(body, encoding="utf-8")
            self.robots_action = "generated"

    def postprocess_html(self) -> None:
        """Final pass over the written HTML (before verification): image
        loading attributes / intrinsic sizes and the --staging noindex meta.
        Runs on the files because image files may only exist after later
        crawl rounds than the page that references them."""
        if not self.cfg.rewrite:
            return
        do_staging = self.cfg.staging
        if not (do_staging
                or any(p.wants_postprocess() for p in self.plugins)):
            return
        # only files parse_and_save_html re-serialized -- HTML *assets* were
        # stored byte-identical and must stay that way
        for html_file in sorted(self.rewritten_html_paths):
            try:
                raw = html_file.read_bytes()
            except OSError as exc:
                self.warnings.append(f"postprocess: cannot read "
                                     f"{html_file.name}: {exc}")
                continue
            soup = BeautifulSoup(raw, "html.parser")
            changed = False
            if do_staging:
                changed |= self._inject_staging_meta(soup)
            for p in self.plugins:
                changed |= p.postprocess_soup(soup)
            if changed:
                try:
                    html_file.write_bytes(self.serialize(soup))
                except OSError as exc:
                    self.warnings.append(f"postprocess: cannot write "
                                         f"{html_file.name}: {exc}")

    def _inject_staging_meta(self, soup: BeautifulSoup) -> bool:
        robots = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
        if robots is not None:
            content = robots.get("content") or ""
            if "noindex" in content.lower():
                return False
            robots["content"] = f"noindex, {content}" if content else "noindex"
            return True
        head = soup.find("head")
        if head is None:
            return False
        tag = soup.new_tag("meta")
        tag["name"] = "robots"
        tag["content"] = "noindex"
        head.insert(0, tag)
        return True

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
        # policy check: no dynamic-endpoint artifacts may exist in the export.
        # Top level: prefix match (wp-login.php etc.); deeper levels: EXACT
        # names only, else an innocent upload like wp-login-screenshot.png
        # would fail --fail-on verify
        hits = {h for pat in ("wp-admin*", "wp-login*", "wp-json*", "xmlrpc*")
                for h in self.public_dir.glob(pat)}
        hits |= {h for name in ("wp-admin", "wp-json",
                                "wp-login.php", "xmlrpc.php")
                 for h in self.public_dir.rglob(name)}
        for hit in sorted(hits):
            self.verify_unexpected.append(
                {"file": hit.relative_to(self.public_dir).as_posix(),
                 "ref": "dynamic endpoint artifact in export"})
        if not self.cfg.rewrite:
            return
        origin_404 = {unicodedata.normalize("NFC", unquote(urlsplit(a["url"]).path))
                      for a in self.asset_errors}
        missing_seen: set[tuple[str, str]] = set()
        unexpected_seen: set[tuple[str, str]] = set()

        def note_unexpected(rel_file: str, ref: str) -> None:
            key = (rel_file, ref)
            if key not in unexpected_seen:
                unexpected_seen.add(key)
                self.verify_unexpected.append({"file": rel_file, "ref": ref})

        def note_private(rel_file: str, text: str, where: str) -> None:
            """Flag private/loopback URLs whose host is NOT one of our own
            internal spellings (those get localized) -- leftovers make
            browsers show the Local Network Access prompt."""
            if "//" not in text:
                return
            for m in PRIVATE_NET_RE.finditer(text):
                if not self.is_internal(m.group(0)):
                    note_unexpected(
                        rel_file,
                        f"private-network URL {m.group(0)} in {where} -- "
                        f"browsers prompt for Local Network Access")

        def exists_local(ref: str) -> bool:
            path = ref.split("#", 1)[0].split("?", 1)[0]
            path = unicodedata.normalize("NFC", unquote(path))
            target = self.public_dir / path.lstrip("/")
            if target.is_file():
                return True
            if path.endswith("/"):
                if path.startswith(("/wp-content/", "/wp-includes/")):
                    # directory base path from a JS config var (plugin_url
                    # & co.) that scripts join file names onto -- never a page
                    return target.is_dir() or (target / "index.html").is_file()
                # page URL: a bare directory without index.html would be
                # served as 403 by the generated nginx config, not a page
                return (target / "index.html").is_file()
            if not path_extension(path):
                if (target / "index.html").is_file():
                    return True
                # a bare directory only counts as "exists" for JS base
                # paths under the registered script-ref dirs -- anywhere
                # else a directory without index.html is served 403/404
                return (path.startswith(tuple(f"/{d}/"
                                              for d in VERIFY_SCRIPT_REF_DIRS))
                        and target.is_dir())
            return False

        def check_ref(rel_file: str, ref: str) -> None:
            ref = ref.strip()
            if not ref.startswith("/") or ref.startswith("//"):
                return  # relative or external -- not checked here
            if ("{" in ref or "*" in ref
                    or PAGE_SKIP_PATTERNS.search(ref.split("?")[0])):
                return  # runtime template / glob pattern / dynamic endpoint
            if VERIFY_SKIP_REF_PREFIXES and ref.startswith(
                    VERIFY_SKIP_REF_PREFIXES):
                return  # resolved at runtime (e.g. decoded to mailto:),
                        # never fetched by browsers
            if exists_local(ref):
                return
            key = (rel_file, ref)
            if key in missing_seen:
                return
            missing_seen.add(key)
            self.verify_missing.append({
                "file": rel_file, "ref": ref,
                "origin_error": unicodedata.normalize(
                    "NFC", unquote(ref.split("?")[0])) in origin_404})

        # "/dir/..." string refs inside inline scripts (JS configs join file
        # names onto these); the dir list is plugin-extendable
        script_ref_re = re.compile(
            r"""["'](/(?:"""
            + "|".join(re.escape(d) for d in VERIFY_SCRIPT_REF_DIRS)
            + r""")/[^"'\s\\]+?)["'\\]""")

        for html_file in sorted(self.public_dir.rglob("*.html")):
            rel_file = html_file.relative_to(self.public_dir).as_posix()
            soup = BeautifulSoup(html_file.read_bytes(), "html.parser")
            for tag in soup.find_all(True):
                if tag.name == "meta":
                    content = tag.get("content")
                    if isinstance(content, str) and content:
                        note_private(rel_file, content, "<meta>")
                    continue
                rel_attr = tag.get("rel")
                rel = {r.lower() for r in (rel_attr if isinstance(rel_attr, list)
                                           else (rel_attr or "").split())}
                # with --target-domain NO origin reference may survive, so
                # the SEO-link allowance disappears entirely
                allowed_abs = not self.cfg.target_domain and tag.name == "link" and (
                    rel & {"canonical", "alternate"}
                    or PAGE_SKIP_PATTERNS.search(
                        urlsplit(tag.get("href") or "").path))
                for attr, val in tag.attrs.items():
                    if not isinstance(val, str) or not val:
                        continue
                    if (attr not in TEXT_ATTRS and not allowed_abs
                            and self.host_probe_re.search(val)):
                        note_unexpected(rel_file, f"<{tag.name} {attr}=...>")
                    note_private(rel_file, val, f"<{tag.name} {attr}=...>")
                    if attr in SRCSET_ATTRS:
                        for cand, _ in iter_srcset(val):
                            if cand:
                                check_ref(rel_file, cand)
                    elif attr == "style" or (attr in CSS_URL_ATTRS
                                             and "url(" in val):
                        for m in CSS_URL_RE.finditer(val):
                            check_ref(rel_file, m.group(1))
                    elif attr in URL_ATTRS or attr in PAGE_URL_ATTRS:
                        check_ref(rel_file, val)
            for style in soup.find_all("style"):
                if not style.string:
                    continue
                if self.host_probe_re.search(style.string):
                    note_unexpected(rel_file, "<style> block")
                note_private(rel_file, style.string, "<style> block")
                for m in CSS_URL_RE.finditer(style.string):
                    check_ref(rel_file, m.group(1))
            for script in soup.find_all("script"):
                stype = (script.get("type") or "").lower()
                if script.string:
                    note_private(rel_file,
                                 script.string.replace("\\/", "/"),
                                 "<script> block")
                if "ld+json" in stype:
                    if (self.cfg.target_domain and script.string
                            and self.host_probe_re.search(script.string)):
                        note_unexpected(rel_file,
                                        "<script ld+json> origin URL")
                    continue
                if not script.string:
                    continue
                if self.host_probe_re.search(script.string):
                    note_unexpected(rel_file, "<script> block")
                text = script.string.replace("\\/", "/")
                for ref in script_ref_re.findall(text):
                    check_ref(rel_file, ref)

        for css_file in sorted(self.public_dir.rglob("*.css")):
            rel_file = css_file.relative_to(self.public_dir).as_posix()
            try:
                text = css_file.read_text("utf-8", errors="replace")
            except OSError:
                continue
            if self.host_probe_re.search(text):
                note_unexpected(rel_file, "absolute origin URL in CSS")
            note_private(rel_file, text, "CSS")
            css_dir = "/" + posixpath.dirname(rel_file)
            for m in list(CSS_URL_RE.finditer(text)) + list(CSS_IMPORT_RE.finditer(text)):
                ref = m.group(1).strip()
                if ref.startswith(("data:", "#", "//")) or "://" in ref:
                    continue
                if not ref.startswith("/"):
                    ref = posixpath.normpath(posixpath.join(css_dir, ref))
                check_ref(rel_file, ref)

        for js_file in sorted(self.public_dir.rglob("*.js")):
            rel_file = js_file.relative_to(self.public_dir).as_posix()
            try:
                text = js_file.read_text("utf-8", errors="replace")
            except OSError:
                continue
            if self.host_probe_re.search(text):
                note_unexpected(rel_file, "absolute origin URL in JS")
            note_private(rel_file, text.replace("\\/", "/"), "JS")

        # every <loc> in the generated sitemap must resolve in the export
        if self.generated_sitemap:
            try:
                root = ET.fromstring(
                    (self.public_dir / "sitemap.xml").read_bytes())
            except (OSError, ET.ParseError):
                root = None
            if root is not None:
                for u in root:
                    loc = next((c.text for c in u
                                if local_name(c.tag) == "loc"), None)
                    if not loc:
                        continue
                    path = urlsplit(loc.strip()).path or "/"
                    if not exists_local(path):
                        self.verify_missing.append(
                            {"file": "sitemap.xml (generated)", "ref": path})

        # --target-domain promises NO origin reference survives -- that
        # includes robots.txt and any kept origin sitemaps, which the
        # HTML/CSS walk above never sees
        if self.cfg.target_domain:
            for extra in (self.public_dir / "robots.txt",
                          *sorted(self.public_dir.glob("*sitemap*.xml"))):
                if not extra.is_file():
                    continue
                try:
                    text = extra.read_text("utf-8", errors="replace")
                except OSError:
                    continue
                if self.host_probe_re.search(text):
                    note_unexpected(extra.name,
                                    "origin URL despite --target-domain")

    # ----------------------------------------------------------------------
    # Phase 4: deployment files + report
    # ----------------------------------------------------------------------

    def _sub_filter_hosts(self) -> list[str]:
        """Host spellings that can appear in the exported files -- every
        form the localization regexes match (incl. hosts auto-detected via
        split DNS), plus --target-domain ones."""
        hosts: list[str] = []
        try:                              # www. variant is nonsense for IPs
            ipaddress.ip_address(self.base_norm.rsplit(":", 1)[0])
            candidates = (self.base_norm, self.connect_norm,
                          self.connect_norm.partition(":")[0])
        except ValueError:
            candidates = (f"www.{self.base_norm}", self.base_norm,
                          self.connect_norm,
                          self.connect_norm.partition(":")[0])
        for h in candidates:
            if not h or norm_host(h) not in self.internal_norms:
                continue
            for spelling in sorted(host_spellings(h)):
                if spelling not in hosts:
                    hosts.append(spelling)
        for extra in (*self.cfg.internal_hosts, *self.auto_internal_hosts):
            for spelling in sorted(host_spellings(norm_host(extra))):
                if spelling not in hosts:
                    hosts.append(spelling)
        if self.cfg.target_domain:
            td = self.cfg.target_domain
            for h in (td, f"www.{norm_host(td)}", norm_host(td)):
                if h not in hosts:
                    hosts.append(h)
        return hosts

    def _safe_redirect_rules(self) -> list[tuple[str, str]]:
        """Deduped internal redirects as (from_path, to_ref) that are safe
        to interpolate into the generated nginx/Netlify/Apache configs.
        to_ref keeps the query string. Rules whose characters would break
        the config syntax ($ expands nginx variables, quotes/whitespace
        split directives) are skipped with a warning."""
        rules: list[tuple[str, str]] = []
        seen_from: set[str] = set()
        for r in self.redirects:
            if not self.is_internal(r["to"]):
                continue
            s = urlsplit(r["from"])
            t = urlsplit(r["to"])
            if s.query:
                continue  # query-string sources are not part of the export
            # the FROM side must match nginx's $uri / Apache's r->uri, which
            # are matched percent-DECODED -- emit the decoded spelling. The
            # target becomes a Location header and stays encoded.
            fp = unicodedata.normalize("NFC", unquote(s.path or "/"))
            tp = canon_path(t.path or "/") + (f"?{t.query}" if t.query else "")
            if t.path in (s.path, s.path + "/") or s.path == t.path + "/":
                # trailing-slash rules in EITHER direction: /a -> /a/ is what
                # the deploy configs do themselves, and a /a/ -> /a rule
                # would fight their directory-slash canonicalization into an
                # infinite 301 loop. ALSO when the target only adds a query
                # (/a/ -> /a/?x=1): a rule for that would match its own
                # target's $uri and 301-loop forever
                continue
            if fp in seen_from:
                continue
            if (re.search(r"""[\s'"$\\{}]""", fp + tp)
                    or any(ord(c) < 32 for c in fp + tp)):
                self.warnings.append(
                    f"redirect rule skipped (unsafe characters for server "
                    f"configs): {fp} -> {tp}")
                continue
            seen_from.add(fp)
            rules.append((fp, tp))
        return rules

    def _sub_filter_lines(self) -> str:
        """nginx sub_filter directives swapping the origin host for the one
        actually requested ($host) at serve time -- canonical / og:url /
        JSON-LD / sitemap <loc> / robots.txt stay correct on ANY domain
        without re-exporting. Every pattern is anchored with a trailing /
        or \" so a substring host (example.at inside example.athletic.de)
        can never match; the JSON-LD form matches escaped https:\\/\\/
        (nginx unescapes '\\\\' in quoted strings to a literal backslash)."""
        lines = []
        for h in self._sub_filter_hosts():
            if (re.search(r"""[\s'"$\\{};]""", h)
                    or any(ord(c) < 32 for c in h)):
                # hosts come from the CLI (--internal-host/--target-domain)
                # -- a stray quote/semicolon would break or inject into the
                # generated nginx config
                self.warnings.append(
                    f"sub_filter skipped (unsafe characters for the nginx "
                    f"config): {h!r}")
                continue
            for scheme in ("https", "http"):
                lines.append(f"    sub_filter '{scheme}://{h}/' "
                             "'$canonical_scheme://$served_host/';")
                lines.append(f"    sub_filter '{scheme}://{h}\"' "
                             "'$canonical_scheme://$served_host\"';")
                lines.append(f"    sub_filter '{scheme}:\\\\/\\\\/{h}\\\\/' "
                             "'$canonical_scheme:\\\\/\\\\/$served_host\\\\/';")
        return "\n".join(lines)

    def write_deploy_files(self) -> None:
        redirect_rules = self._safe_redirect_rules()
        if self.generated_sitemap:
            # the origin sitemap URLs are well-known and often registered in
            # Search Console -- 301 them onto the generated /sitemap.xml
            seen_from = {fp for fp, _ in redirect_rules}
            for path in self.origin_sitemap_paths:
                path = unicodedata.normalize("NFC", unquote(path))
                if path not in ("/sitemap.xml", "") and path not in seen_from:
                    seen_from.add(path)
                    redirect_rules.append((path, "/sitemap.xml"))
        seen_from = {fp for fp, _ in redirect_rules}
        for p in self.plugins:
            redirect_rules.extend(p.redirect_rules(seen_from))

        inc_lines = ["# Redirects observed on the origin site, served as real"
                     " 301s (generated)."]
        if not redirect_rules:
            inc_lines.append("# none observed during the export")
        # quoted args; a trailing ? ONLY when the target carries its own
        # query (it stops nginx from re-appending the request args -- on a
        # queryless target those args, e.g. ?utm_..., must survive the 301)
        inc_lines += [
            f'rewrite "^{re.escape(fp)}$" "{tp}{"?" if "?" in tp else ""}" '
            f"permanent;"
            for fp, tp in redirect_rules]
        (self.cfg.out_dir / "redirects.inc").write_text(
            "\n".join(inc_lines) + "\n", encoding="utf-8")

        # nginx add_header inheritance is all-or-nothing: any location that
        # sets its own header (the asset block's Cache-Control) silently
        # drops every server-level one -- so this block is emitted at BOTH
        # levels
        hdr_lines = ['add_header X-Content-Type-Options "nosniff" always;',
                     'add_header Referrer-Policy '
                     '"strict-origin-when-cross-origin" always;']
        if self.cfg.staging:
            hdr_lines.append('add_header X-Robots-Tag "noindex, nofollow" '
                             'always;  # --staging')
        server_hdrs = "\n".join(f"    {l}" for l in hdr_lines)
        asset_hdrs = "\n".join(f"        {l}" for l in hdr_lines)

        has_404 = (self.public_dir / "404.html").is_file()
        error_page = ("""error_page 404 /404.html;
    # direct hits on /404.html answer 404 (via error_page), not an
    # indexable 200
    location = /404.html { internal; }""" if has_404 else
                      "# no themed 404.html was captured (see report)")
        if not has_404:
            self.warnings.append(
                "no 404.html in the export -- the nginx/Apache configs fall "
                "back to their built-in 404 page")

        nginx_conf = f"""# generated by {TOOL_NAME} {VERSION}
# maps must sit at http level -- conf.d/*.conf is included there. Behind a
# TLS-terminating proxy the canonical scheme comes from X-Forwarded-Proto,
# standalone from the connection itself.
map $http_x_forwarded_proto $canonical_scheme {{
    default $scheme;
    https   https;
    http    http;
}}
# host INCLUDING a nonstandard port ($host strips it; a preview on :8080
# would otherwise emit portless canonical URLs). Empty for HTTP/1.0.
map $http_host $served_host {{
    default $http_host;
    ''      $host;
}}

server {{
    listen 80;
    server_name _;
    server_tokens off;

    root /usr/share/nginx/html;
    index index.html;
    charset utf-8;

    # relative Location headers -- with server_name _ an absolute redirect
    # would advertise the literal host "_"
    absolute_redirect off;

{server_hdrs}

    # Serve-time domain substitution (ngx_http_sub_module): the export keeps
    # the origin host in canonical/og:url/JSON-LD/sitemap/robots.txt; these
    # filters swap it for the host actually requested, so the SEO signals
    # are correct on any domain without re-exporting.
    # NOTE: never add gzip_static here -- sub_filter cannot look inside
    # precompressed files (the on-the-fly gzip below runs after sub_filter).
    sub_filter_once off;
    sub_filter_last_modified on;
    sub_filter_types text/xml text/plain application/xml;
{self._sub_filter_lines()}

    {error_page}

    include /etc/nginx/conf.d/redirects.inc;

    location / {{
        # one canonical URL per page: /page -> /page/ (matches the export's
        # trailing-slash structure; without this both answer 200)
        if (-d $request_filename) {{
            rewrite ^(.*[^/])$ $1/ permanent;
        }}
        try_files $uri $uri/ =404;
    }}

    # deploy artifacts for other platforms are not content
    location ~ /\\.(?!well-known) {{ deny all; }}
    location = /_redirects {{ deny all; }}

    location ~* \\.(css|js|mjs|png|jpe?g|gif|svg|webp|avif|ico|woff2?|ttf|otf)$ {{
        expires 30d;
        add_header Cache-Control "public";
{asset_hdrs}
    }}

    location = /robots.txt  {{ access_log off; }}
    gzip on;
    gzip_types text/css application/javascript application/json
               image/svg+xml application/xml;
}}
"""
        (self.cfg.out_dir / "nginx.conf").write_text(nginx_conf, encoding="utf-8")

        dockerfile = """FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY redirects.inc /etc/nginx/conf.d/redirects.inc
COPY public/ /usr/share/nginx/html/
"""
        (self.cfg.out_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8")

        # Netlify: _redirects in the publish dir; 404.html is automatic there
        netlify = ["# generated -- Netlify has no serve-time host substitution:",
                   "# for correct canonical/og/JSON-LD URLs re-export with"
                   " --target-domain",
                   "# trailing-slash canonicalization comes from Netlify's"
                   " Pretty URLs (default on)"]
        netlify += [f"{fp} {tp} 301" for fp, tp in redirect_rules]
        (self.public_dir / "_redirects").write_text(
            "\n".join(netlify) + "\n", encoding="utf-8")

        # Apache: mod_substitute cannot interpolate %{HTTP_HOST} into
        # replacements, so Apache deployments need --target-domain as well.
        # RedirectMatch, not Redirect: mod_alias Redirect is prefix-matching
        # and /alt would also hijack /alternative/.
        htaccess = ["# generated -- Apache has no serve-time host substitution:",
                    "# for correct canonical/og/JSON-LD URLs re-export with"
                    " --target-domain",
                    "# trailing-slash canonicalization comes from Apache's"
                    " DirectorySlash (default on)"]
        if has_404:
            htaccess.append("ErrorDocument 404 /404.html")
        htaccess += [f'RedirectMatch 301 "^{re.escape(fp)}$" "{tp}"'
                     for fp, tp in redirect_rules]
        htaccess += ["<IfModule mod_expires.c>",
                     "  ExpiresActive on",
                     '  ExpiresByType text/css "access plus 30 days"',
                     '  ExpiresByType application/javascript "access plus 30 days"',
                     '  ExpiresByType image/png "access plus 30 days"',
                     '  ExpiresByType image/jpeg "access plus 30 days"',
                     '  ExpiresByType image/gif "access plus 30 days"',
                     '  ExpiresByType image/svg+xml "access plus 30 days"',
                     '  ExpiresByType image/webp "access plus 30 days"',
                     '  ExpiresByType image/avif "access plus 30 days"',
                     '  ExpiresByType font/woff2 "access plus 30 days"',
                     "</IfModule>"]
        if self.cfg.staging:
            htaccess += ["<IfModule mod_headers.c>",
                         '  Header set X-Robots-Tag "noindex, nofollow"',
                         "</IfModule>"]
        (self.public_dir / ".htaccess").write_text(
            "\n".join(htaccess) + "\n", encoding="utf-8")

        self.deploy_info = {
            "nginx_sub_filter_hosts": self._sub_filter_hosts(),
            "redirects_301": len(redirect_rules),
            "netlify_redirects": "public/_redirects",
            "apache_htaccess": "public/.htaccess",
            "staging_noindex_header": self.cfg.staging,
        }

        # docker-compose.yml: builds an image with the content baked in (via
        # the Dockerfile's COPY -- no bind mount, so re-exports never leave a
        # stale mount behind). Service and container are named after the site
        # host; the published port is overridable via $PORT.
        site_slug = re.sub(r"[^a-z0-9]+", "-", self.host.lower()).strip("-") or "site"
        compose = """\
# explicit per-site project name: compose otherwise derives it from the
# DIRECTORY name, so several sites deployed from folders all called
# "static" would share one project -- their up/down runs would then treat
# each other's containers as orphans and fight over the default network
name: wpstatic-__SITE_SLUG__

services:
  __SITE_SLUG__:
    build: .
    image: wpstatic-__SITE_SLUG__
    container_name: wpstatic-__SITE_SLUG__
    ports:
      - "${PORT:-__PORT__}:80"
    restart: always
    healthcheck:
      test: ["CMD", "wget", "-q", "--spider", "http://localhost/"]
      interval: 30s
      timeout: 5s
      retries: 3

# name the auto-created default network after the site (like the
# container) -- otherwise every "-o ./static" deployment ends up with an
# indistinguishable <dirname>_default network
networks:
  default:
    name: wpstatic-__SITE_SLUG__
""".replace("__SITE_SLUG__", site_slug).replace("__PORT__", str(self.cfg.port))
        (self.cfg.out_dir / "docker-compose.yml").write_text(compose, encoding="utf-8")

        # keep reports/plugin output trees out of the docker build context
        (self.cfg.out_dir / ".dockerignore").write_text(
            "".join(f"{d}/\n" for d in EXTRA_OUTPUT_DIRS)
            + "report.json\nreport.txt\n", encoding="utf-8")

        # server.sh: thin wrapper that drives docker compose.
        server_sh = r'''#!/usr/bin/env sh
# Control the containerized static site via docker compose.
# The image is built from the Dockerfile with the content baked in (no bind
# mount), so it survives re-exports cleanly. Service/container are named
# after the site host (see docker-compose.yml).
set -eu
cd "$(dirname "$0")"

usage() {
  cat <<EOF
usage: $0 [up|down|restart|status|logs|build|help] [PORT]

  up|start     build the image and start the container (default)
  down|stop    stop and remove the container
  restart      rebuild + recreate (after a re-export)
  status|ps    show container state
  logs         follow nginx logs
  build        build the image only

PORT precedence: argument > \$PORT environment > __PORT__ (baked in):
  $0 up 9000         # publish on port 9000
  PORT=9000 $0 up    # same, via environment
EOF
}

case "${1:-}" in
  -h|--help|help) usage; exit 0 ;;
esac

command -v docker >/dev/null 2>&1 || {
  echo "error: docker not found -- install it first:" \
       "https://docs.docker.com/get-docker/" >&2
  exit 1
}
if docker compose version >/dev/null 2>&1; then
  compose() { docker compose "$@"; }
elif command -v docker-compose >/dev/null 2>&1; then
  # legacy standalone docker-compose (v1) fallback
  compose() { docker-compose "$@"; }
else
  echo "error: neither 'docker compose' nor 'docker-compose' is available" >&2
  exit 1
fi

cmd="${1:-up}"
# pin the compose project per site (belt-and-braces for compose versions
# without top-level "name:" support and the docker-compose v1 fallback)
COMPOSE_PROJECT_NAME="wpstatic-__SITE_SLUG__"
export COMPOSE_PROJECT_NAME
# explicit argument beats environment beats baked-in default
PORT="${2:-${PORT:-__PORT__}}"
case "$PORT" in
  ''|*[!0-9]*) echo "error: PORT must be a number, got: '$PORT'" >&2; exit 1 ;;
esac
export PORT

case "$cmd" in
  up|start)
    compose up -d --build
    echo "up -> http://localhost:${PORT}"
    ;;
  down|stop)
    compose down
    ;;
  restart)
    compose up -d --build --force-recreate
    echo "restarted -> http://localhost:${PORT}"
    ;;
  status|ps)
    compose ps
    ;;
  logs)
    compose logs -f
    ;;
  build)
    compose build
    ;;
  *)
    echo "error: unknown command: '$cmd'" >&2
    usage >&2
    exit 1
    ;;
esac
'''.replace("__SITE_SLUG__", site_slug).replace("__PORT__", str(self.cfg.port))
        server_path = self.cfg.out_dir / "server.sh"
        server_path.write_text(server_sh, encoding="utf-8")
        server_path.chmod(0o755)

    def write_report(self) -> None:
        ok_pages = [p for p in self.pages if not p.error]
        failed_pages = [p for p in self.pages if p.error]
        noindex = [p for p in ok_pages if p.noindex]
        # compared against save_url (like the sitemap): a page reached via a
        # redirect whose canonical points at its final URL is NOT a mismatch
        canonical_mismatch = [
            p for p in ok_pages
            if p.canonical and self.is_internal(p.canonical)
            and self.canonical_target(p) != (p.save_url or p.url)
        ]
        html_pages = [p for p in ok_pages if "html" in p.content_type]
        no_title = [p for p in html_pages if not p.has_title]
        no_desc = [p for p in html_pages if not p.has_description]
        no_viewport = [p for p in html_pages if not p.has_viewport]
        link_only = [p for p in ok_pages if p.source == "link"]
        # a source URL can be recorded twice (followed hops + final refused
        # redirect) -- report each from/to pair once
        redirects = list({(r["from"], r["to"]): r for r in self.redirects}
                         .values())

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
            "sitemap": {
                "generated": bool(self.generated_sitemap),
                **(self.generated_sitemap or {}),
            },
            "robots_txt": {
                "origin_found": self.robots_txt_found,
                "action": self.robots_action,
            },
            "deploy": self.deploy_info,
            "redirects": redirects,
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
                "excluded_urls": sorted(self.excluded_urls),
                "respect_robots": self.cfg.respect_robots,
                "crawl_truncated_at_max_pages": self.truncated,
                "wp_cruft_removed": self.cruft_removed,
                "staging_noindex": self.cfg.staging,
                "target_domain": self.cfg.target_domain,
            },
            "external_hosts_referenced": sorted(self.external_hosts),
            "auto_internal_hosts": self.auto_internal_hosts,
            "foreign_wp_hosts": sorted(self.foreign_wp_hosts),
            "verification": {
                # policy checks (homepage at root, dynamic-endpoint
                # artifacts) always run; reference resolution needs rewrite
                "policy_checks": True,
                "reference_checks": self.cfg.rewrite,
                # plugins append their own clauses via add_report
                "policy": "external hosts are linked as-is and never fetched;"
                          " wp-admin/xmlrpc/admin-ajax and other dynamic"
                          " endpoints are never requested; redirects"
                          " are only followed while they stay on the target"
                          " site",
                "missing_local_files": self.verify_missing,
                "unexpected_absolute_refs": self.verify_unexpected,
            },
            "warnings": self.warnings,
        }
        # plugin contributions: report.json keys, report.txt head lines and
        # full report.txt sections -- BEFORE the dump below
        txt_head: list[str] = []
        txt_sections: list[str] = []
        for p in self.plugins:
            p.add_report(report, txt_head, txt_sections)
        (self.cfg.out_dir / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

        section = report_section

        txt = [
            f"{TOOL_NAME} {VERSION} -- export report",
            f"Site: {self.cfg.base_url}"
            + (f" (Host: {self.cfg.host_header})" if self.cfg.host_header else ""),
            f"Generated: {report['generated']}",
            "",
            f"Pages exported:  {len(ok_pages)}",
            f"Assets exported: {self.asset_count}",
            (f"Sitemap:         generated /sitemap.xml "
             f"({self.generated_sitemap['url_count']} URLs, "
             f"{self.generated_sitemap['excluded_noindex']} noindex excluded)"
             if self.generated_sitemap else
             f"Sitemap files:   {len(self.sitemap_files)} (origin, verbatim)"),
            f"robots.txt:      {self.robots_action}"
            + ("" if self.robots_txt_found else " (origin had none)"),
            "WP cruft removed: " + (", ".join(
                f"{k}={v}" for k, v in sorted(self.cruft_removed.items()))
                or "none"),
            *txt_head,
            *(["Staging:         noindex everywhere (robots.txt, "
               "X-Robots-Tag, meta robots)"] if self.cfg.staging else []),
            *([f"Target domain:   {self.cfg.target_domain} (canonical/og/"
               f"JSON-LD/sitemap rewritten)"] if self.cfg.target_domain else []),
            "",
            section("Failed pages", failed_pages, lambda p: f"{p.url} ({p.error})"),
            section("Asset errors", self.asset_errors,
                    lambda a: f"{a['url']} ({a['error']})"),
            section("Redirects observed", redirects,
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
            section("AMP variants exported", sorted(self.amp_links)),
            section("Skipped URLs with query strings", sorted(self.skipped_query_urls)),
            section("Excluded by --exclude / robots.txt",
                    sorted(self.excluded_urls)),
            section("External hosts (linked as-is, intentionally NOT "
                    "downloaded)", sorted(self.external_hosts)),
            section("Hosts auto-detected as internal (private DNS)",
                    self.auto_internal_hosts),
            section("Foreign hosts serving wp-content paths (likely the "
                    "same site -- consider --internal-host)",
                    sorted(self.foreign_wp_hosts)),
            section("Verification: referenced local files missing",
                    self.verify_missing,
                    lambda v: f"{v['file']} -> {v['ref']}"
                              + (" (already failing on the origin site)"
                                 if v.get("origin_error") else "")),
            section("Verification: unexpected absolute self-references",
                    self.verify_unexpected,
                    lambda v: f"{v['file']} -> {v['ref']}"),
            *txt_sections,
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
        if not self.cfg.rewrite:
            # several advertised features need re-serialized HTML -- say so
            # instead of silently doing nothing
            if self.cfg.staging:
                self.warnings.append(
                    "--no-rewrite: the staging noindex meta cannot be "
                    "injected into pages (robots.txt and the X-Robots-Tag "
                    "headers in the server configs still apply)")
            if self.cfg.target_domain:
                self.warnings.append(
                    "--no-rewrite: --target-domain cannot rewrite the HTML "
                    "(only the generated sitemap/robots.txt get the new "
                    "domain)")
            self.warnings.append(
                "--no-rewrite: WP cruft stripping, image optimization, "
                "minification and redirect stub pages are disabled")
        for p in self.plugins:
            p.run_start()
        if self.cfg.staging and self.cfg.target_domain:
            self.warnings.append(
                "--staging blocks all indexing -- --target-domain only "
                "affects the markup of this (unindexable) preview")
        if self.cfg.clean:
            # extras (e.g. mobile-variants/) must go even when public/ is
            # already absent -- stale trees would be reported as current
            shutil.rmtree(self.public_dir, ignore_errors=True)
            for extra in EXTRA_OUTPUT_DIRS:
                shutil.rmtree(self.cfg.out_dir / extra, ignore_errors=True)
            self.say("[init] --clean: removed existing public/"
                     + "".join(f" and {d}/" for d in EXTRA_OUTPUT_DIRS))
        elif self.public_dir.exists() and any(self.public_dir.iterdir()):
            self.warnings.append(
                "output dir public/ was not empty and --clean was not "
                "set; stale files from previous runs may remain.")
        self.public_dir.mkdir(parents=True, exist_ok=True)

        seeds = self.discover()
        self.say(f"[discover] {len(set(seeds))} unique pages from sitemaps")
        self._absorb_private_hosts()
        self.crawl(seeds)

        # a crash in ANY post-crawl step must never discard the report
        # (and with it every diagnostic) after a potentially hours-long crawl
        def step(name: str, fn) -> None:
            try:
                fn()
            except Exception as exc:            # noqa: BLE001
                self.warnings.append(f"{name} failed: {exc!r}")
                print(f"[warn] {name} failed: {exc!r}")

        step("404 capture", self.capture_404)
        step("favicon capture", self.capture_favicon)
        step("internal-host detection", self._warn_foreign_site_hosts)
        if self.cfg.generate_sitemap:
            step("sitemap generation", self.write_generated_sitemap)
        step("robots.txt finalization", self.finalize_robots)
        step("HTML post-processing", self.postprocess_html)
        step("verification", self.verify_export)
        step("deploy file generation", self.write_deploy_files)
        step("report", self.write_report)

        ok = len([p for p in self.pages if not p.error])
        failed = len(self.pages) - ok
        print()
        print(f"[done] {ok} pages, {self.asset_count} assets exported "
              f"-> {self.public_dir}")
        if self.cruft_removed:
            print(f"[seo] stripped {sum(self.cruft_removed.values())} WP/CDN "
                  f"head elements "
                  f"({', '.join(sorted(self.cruft_removed))})")
        for p in self.plugins:
            for line in p.summary_lines():
                print(line)
        if self.auto_internal_hosts:
            print(f"[done] auto-detected internal hosts (private DNS): "
                  f"{', '.join(self.auto_internal_hosts)}")
        if self.foreign_wp_hosts:
            print(f"[warn] foreign hosts serving wp-content paths -- likely "
                  f"the same site; consider --internal-host: "
                  f"{', '.join(sorted(self.foreign_wp_hosts))}")
        if self.cfg.staging:
            print("[staging] export is noindexed everywhere "
                  "(robots.txt, X-Robots-Tag, meta robots)")
        elif not self.cfg.target_domain:
            print("[seo] canonical/og/JSON-LD/sitemap keep the origin host; "
                  "the generated nginx.conf substitutes the served domain "
                  "at runtime (sub_filter)")
        if self.cfg.rewrite:
            if self.verify_missing or self.verify_unexpected:
                print(f"[verify] {len(self.verify_missing)} missing local "
                      f"files, {len(self.verify_unexpected)} unexpected "
                      f"absolute references -- details in the report")
            else:
                print("[verify] export is self-contained: all references "
                      "resolve locally, no unexpected absolute URLs")
            print("[note] Not functional statically: form submissions, "
                  "consent/analytics AJAX and site search "
                  "(dynamic endpoints).")
            print("[note] External hosts (font/analytics/CDN domains) stay "
                  "linked and are never downloaded; "
                  "wp-admin/xmlrpc are never requested.")
        if failed or self.asset_errors or self.warnings:
            print(f"[done] {failed} page errors, {len(self.asset_errors)} "
                  f"asset errors, {len(self.warnings)} warnings -- see report.txt")
        print(f"[done] Report: {self.cfg.out_dir / 'report.txt'}")
        print(f"[done] Serve: {self.cfg.out_dir / 'server.sh'} up "
              f"-> http://localhost:{self.cfg.port}  (stop: server.sh down)")
        code = 0 if ok else 1
        if (self.cfg.fail_on in ("errors", "verify")
                and (failed or self.asset_errors or self.truncated)):
            code = max(code, 1)
        if (self.cfg.fail_on == "verify"
                and (self.verify_missing or self.verify_unexpected)):
            code = max(code, 2)
        return code


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

  # staging/preview deployment that must never compete with the live site:
  # robots.txt Disallow, X-Robots-Tag header and meta noindex everywhere
  wp-static-export https://www.example.at --staging

  # deploy to a NEW domain on a host without serve-time substitution
  # (Netlify, Apache): hard-rewrite canonical/og/JSON-LD/sitemap URLs
  wp-static-export https://www.example.at --target-domain neue-domain.at

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
    downloaded; wp-admin / xmlrpc are never requested (wp-json only for the
    read-only Slider Revolution endpoint when a slider has lazy slides).
  * report.txt / report.json summarize errors, redirects, SEO signals and a
    self-containedness verification pass.""")
    ap.add_argument("--version", action="version",
                    version=f"{TOOL_NAME} {VERSION}")
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
    ap.add_argument("--no-generate-sitemap", dest="generate_sitemap",
                    action="store_false",
                    help="keep the origin sitemap files verbatim instead of "
                         "generating a fresh /sitemap.xml from the exported "
                         "URL set (noindex/redirected/canonical-mismatch "
                         "pages excluded, lastmod from Last-Modified)")
    ap.add_argument("--no-strip-wp-cruft", dest="strip_wp_cruft",
                    action="store_false",
                    help="keep WordPress head cruft (generator meta, wp-json/"
                         "oEmbed/feed discovery, EditURI, wlwmanifest, "
                         "pingback, shortlink, Cloudflare beacon) that only "
                         "points at dynamic origin infrastructure. "
                         "No effect with --no-rewrite")
    ap.add_argument("--staging", action="store_true",
                    help="deploy invisible to search engines: robots.txt "
                         "'Disallow: /', X-Robots-Tag noindex header in the "
                         "server configs, meta robots noindex in every page "
                         "(meta injection needs rewrite mode)")
    ap.add_argument("--target-domain", default=None, metavar="DOMAIN",
                    help="hard-rewrite SEO-bearing URLs (canonical, og:*, "
                         "JSON-LD, sitemap <loc>, robots.txt) to this domain "
                         "at export time -- for hosts without serve-time "
                         "substitution (Netlify, Apache). The generated "
                         "nginx.conf makes this unnecessary on nginx/Docker. "
                         "Needs rewrite mode for the HTML parts")
    ap.add_argument("--sitemap-include-linked", action="store_true",
                    help="also list link-discovered pages in the generated "
                         "sitemap.xml (default: only pages the origin "
                         "sitemap declared -- attachment pages and cache "
                         "artifacts found via links stay out)")
    ap.add_argument("--fail-on", choices=("none", "errors", "verify"),
                    default="none",
                    help="exit-code policy for CI: 'errors' fails (exit 1) "
                         "on any page/asset error, 'verify' additionally "
                         "exits 2 when the self-containedness verification "
                         "found problems. Default: none (exit 0 as long as "
                         "any page was exported)")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="suppress per-round/per-sitemap progress output "
                         "(warnings and the final summary still print)")
    ap.add_argument("--exclude", action="append", default=[], metavar="REGEX",
                    help="skip pages AND assets whose URL path matches this "
                         "regular expression (repeatable), e.g. "
                         "--exclude '^/members/' --exclude '\\.zip$'")
    ap.add_argument("--respect-robots", action="store_true",
                    help="honor the origin robots.txt: skip 'User-agent: *' "
                         "Disallow'ed paths ('*' and '$' supported, longest "
                         "match wins) and adopt Crawl-delay as a minimum "
                         "--delay. Off by default (you usually own the site)")
    ap.add_argument("--internal-host", action="append", default=[],
                    metavar="HOST", dest="internal_hosts",
                    help="additional hostname/address of the SAME site "
                         "(repeatable), e.g. an internal admin domain the "
                         "WordPress siteurl points at -- URLs on these hosts "
                         "get localized like the main domain instead of "
                         "surviving as (possibly private) absolute links")
    ap.add_argument("--no-resolve-internal", dest="resolve_internal",
                    action="store_false",
                    help="skip the split-DNS detection that treats hosts "
                         "resolving to private addresses (from this machine) "
                         "as additional spellings of the site")
    for cls in PLUGIN_REGISTRY:
        try:
            cls.add_cli_args(ap.add_argument_group(f"plugin: {cls.name}"))
        except argparse.ArgumentError as exc:
            # two plugins registering the same option string -- name the
            # culprit instead of dumping a raw traceback (also on --help)
            sys.exit(f"{TOOL_NAME}: plugin {cls.name!r} registers a "
                     f"conflicting CLI option: {exc}")
    args = ap.parse_args(argv)

    for pattern in args.exclude:
        try:
            re.compile(pattern)
        except re.error as exc:
            ap.error(f"--exclude: invalid regex {pattern!r} ({exc})")

    if args.timeout <= 0:
        ap.error(f"--timeout must be positive, got {args.timeout:g}")
    if args.max_pages < 1:
        ap.error(f"--max-pages must be at least 1, got {args.max_pages}")

    base = args.base_url.strip()
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    base = base.rstrip("/")
    if urlsplit(base).path:
        # only scheme+host are ever used -- a path would be silently
        # ignored and the ROOT site exported instead
        ap.error(f"base_url must be the site root (subdirectory installs "
                 f"are not supported): {args.base_url!r}")

    host_header = (args.host_header or "").strip()
    host_header = re.sub(r"^https?://", "", host_header).rstrip("/") or None

    target_domain = (args.target_domain or "").strip()
    target_domain = re.sub(r"^https?://", "", target_domain).rstrip("/") or None
    if target_domain:
        logical_host = host_header or urlsplit(base).netloc
        if norm_host(target_domain) == norm_host(logical_host):
            ap.error("--target-domain equals the site's own host -- the "
                     "default export already keeps SEO URLs on that domain")

    extra_headers: dict = {}
    for raw in args.header:
        if ":" not in raw:
            ap.error(f"--header must be 'Name: Value', got: {raw!r}")
        name, value = raw.split(":", 1)
        if not name.strip():
            ap.error(f"--header needs a non-empty header name, got: {raw!r}")
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
        port=args.port,
        generate_sitemap=args.generate_sitemap,
        strip_wp_cruft=args.strip_wp_cruft,
        staging=args.staging,
        target_domain=target_domain,
        sitemap_include_linked=args.sitemap_include_linked,
        fail_on=args.fail_on,
        quiet=args.quiet,
        excludes=args.exclude,
        respect_robots=args.respect_robots,
        internal_hosts=[re.sub(r"^https?://", "", h.strip()).rstrip("/")
                        for h in args.internal_hosts if h.strip()],
        resolve_internal=args.resolve_internal,
    )
    if args.user_agent:
        # before the plugin pass -- finish_args must see the final value
        cfg.user_agent = args.user_agent
    for cls in PLUGIN_REGISTRY:
        cls.finish_args(ap, args, cfg)
    return cfg


def main(argv: list[str]) -> int:
    cfg = parse_args(argv)
    try:
        return Exporter(cfg).run()
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 130


# --------------------------------------------------------------------------
# Plugin bootstrap -- MUST stay the last top-level statements before the
# __main__ guard: plugin files do `from wp_static_export import Plugin`
# while this module is still executing, so every definition above has to
# exist already. The sys.modules alias makes that import work in script
# mode too (where __name__ == "__main__"); under pytest the conftest
# registers the module before executing it.
sys.modules.setdefault("wp_static_export", sys.modules[__name__])
load_plugins()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
