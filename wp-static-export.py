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
import ipaddress
import json
import posixpath
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
VERSION = "1.2.0"

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

# Normalize away per-request noise before comparing two HTML responses, so
# WordPress dynamics don't cause false "different"s: CSRF nonces (both the
# JS and the <input name="_wpnonce"> form), cache-buster query params
# (?ver=..., ?v=...), uniqid()-style hex ids (Ultimate Addons emits
# id="ultimate-heading-<hextime>" fresh on every request), HTML comments
# (generator/debug stamps) and whitespace. Over-normalizing is fine here --
# worst case a truly dynamic page is classified "same".
NONCE_RE = re.compile(
    rb"""(_wpnonce|nonce|csrf[\w-]*|token)(["']?\s*[:=]\s*["'])"""
    rb"""[A-Za-z0-9+/=._-]{6,}(["'])""", re.IGNORECASE)
WPNONCE_ATTR_RE = re.compile(
    rb"""(name=["']_wpnonce["']\s+value=["'])[^"']+""", re.IGNORECASE)
HTML_COMMENT_RE = re.compile(rb"<!--.*?-->", re.DOTALL)
VER_QS_RE = re.compile(rb"[?&](?:ver|v|t|cache|nocache)=[^\s\"'&<>]+",
                       re.IGNORECASE)
HEX_ID_RE = re.compile(rb"\b[0-9a-f]{8,}\b", re.IGNORECASE)
# Ultimate Addons & co. also emit short DECIMAL rand() suffixes fresh per
# request (id="Info-box-wrap-9913" plus matching selectors)
NUM_ID_RE = re.compile(rb"-\d{3,7}\b")
WS_RE = re.compile(rb"\s+")


def normalize_html(data: bytes) -> bytes:
    data = HTML_COMMENT_RE.sub(b"", data)
    data = NONCE_RE.sub(rb"\1\2\3", data)
    data = WPNONCE_ATTR_RE.sub(rb"\1", data)
    data = VER_QS_RE.sub(b"", data)
    data = HEX_ID_RE.sub(b"H", data)
    data = NUM_ID_RE.sub(b"-N", data)
    return WS_RE.sub(b" ", data).strip()


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
    """canon_path for a reference that may carry ?query / #fragment.
    Splits at the EARLIEST of the two separators: in '/x#frag?y' the '?'
    belongs to the fragment and must not be treated as a query."""
    cut = min((i for i in (ref.find("?"), ref.find("#")) if i != -1),
              default=-1)
    if cut != -1:
        return canon_path(ref[:cut] or "/") + ref[cut:]
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


def _exif_orientation(tiff: bytes) -> int:
    """Orientation tag (0x0112) from an EXIF TIFF block; 1 if absent."""
    try:
        if tiff[:2] == b"II":
            end = "little"
        elif tiff[:2] == b"MM":
            end = "big"
        else:
            return 1
        if int.from_bytes(tiff[2:4], end) != 42:
            return 1
        off = int.from_bytes(tiff[4:8], end)
        count = int.from_bytes(tiff[off:off + 2], end)
        for i in range(count):
            entry = tiff[off + 2 + 12 * i: off + 14 + 12 * i]
            if len(entry) < 12:
                return 1
            if int.from_bytes(entry[0:2], end) == 0x0112:
                return int.from_bytes(entry[8:10], end) or 1
    except (IndexError, ValueError):
        pass
    return 1


def read_image_size(path: Path) -> tuple[int, int] | None:
    """Pixel size straight from the file header -- PNG/GIF/JPEG/WebP,
    no image library needed. None for other formats or malformed files.
    JPEG dimensions are reported as DISPLAYED, i.e. swapped when the EXIF
    orientation transposes the image (phone photos, orientation 5-8)."""
    try:
        with path.open("rb") as fh:
            head = fh.read(32)
            if head.startswith(b"\x89PNG\r\n\x1a\n") and head[12:16] == b"IHDR":
                w = int.from_bytes(head[16:20], "big")
                h = int.from_bytes(head[20:24], "big")
                return (w, h) if w and h else None
            if head[:6] in (b"GIF87a", b"GIF89a"):
                w = int.from_bytes(head[6:8], "little")
                h = int.from_bytes(head[8:10], "little")
                return (w, h) if w and h else None
            if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
                fmt = head[12:16]
                if fmt == b"VP8X":
                    return (int.from_bytes(head[24:27], "little") + 1,
                            int.from_bytes(head[27:30], "little") + 1)
                if fmt == b"VP8L" and len(head) >= 25 and head[20] == 0x2F:
                    bits = int.from_bytes(head[21:25], "little")
                    return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
                if fmt == b"VP8 " and head[23:26] == b"\x9d\x01\x2a":
                    w = int.from_bytes(head[26:28], "little") & 0x3FFF
                    h = int.from_bytes(head[28:30], "little") & 0x3FFF
                    return (w, h) if w and h else None
                return None
            if head.startswith(b"\xff\xd8"):        # JPEG: scan for a SOF marker
                fh.seek(2)
                orientation = 1
                while True:
                    marker = fh.read(2)
                    if len(marker) < 2 or marker[0] != 0xFF:
                        return None
                    code = marker[1]
                    while code == 0xFF:             # fill bytes
                        nxt = fh.read(1)
                        if not nxt:
                            return None
                        code = nxt[0]
                    if code in (0x01,) or 0xD0 <= code <= 0xD8:
                        continue                    # standalone marker
                    seg = fh.read(2)
                    if len(seg) < 2:
                        return None
                    seglen = int.from_bytes(seg, "big")
                    if seglen < 2:
                        return None
                    if code == 0xE1 and seglen >= 10:   # APP1: EXIF orientation
                        body = fh.read(seglen - 2)
                        if len(body) < seglen - 2:
                            return None
                        if body[:6] == b"Exif\x00\x00":
                            orientation = _exif_orientation(body[6:])
                        continue
                    if code in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        body = fh.read(5)
                        if len(body) < 5:
                            return None
                        h = int.from_bytes(body[1:3], "big")
                        w = int.from_bytes(body[3:5], "big")
                        if not (w and h):
                            return None
                        return (h, w) if orientation in (5, 6, 7, 8) else (w, h)
                    fh.seek(seglen - 2, 1)
    except OSError:
        return None
    return None


CSS_CHARSET_B_RE = re.compile(rb'^@charset\s+["\']([-\w]+)["\']')
CSS_CHARSET_RE = re.compile(r'^@charset\s+["\'][-\w]+["\']')
CHARSET_HDR_RE = re.compile(r"charset=([-\w]+)", re.IGNORECASE)


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
    generate_sitemap: bool = True
    strip_wp_cruft: bool = True
    optimize_images: bool = True
    staging: bool = False
    target_domain: str | None = None
    sitemap_include_linked: bool = False
    fail_on: str = "none"            # none | errors | verify
    quiet: bool = False


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
    last_modified: str = ""          # raw Last-Modified response header
    save_url: str = ""               # canonical URL the content was saved under
                                     # (differs from url for redirect sources)


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
        # --target-domain: SEO-bearing origin URLs (canonical, og:*, JSON-LD,
        # sitemap <loc>, robots.txt) get this prefix instead of staying on
        # the origin host -- for hosts without serve-time substitution
        self.target_prefix = (f"https://{cfg.target_domain}"
                              if cfg.target_domain else "")

        # host-matching regexes for URL localization; cover the www. variant,
        # both IDN spellings, and every textual form page builders emit:
        # plain absolute, protocol-relative (//host/...) and JSON-escaped
        # (https:\/\/host\/...). internal_norms already includes the connect
        # address when --host is used (staging-IP leaks in slider configs).
        # The lookahead anchors the host: without it example.at would match
        # inside example.athletic.de (and localize_text would destroy that
        # external URL), and example.at:8443 -- a DIFFERENT origin -- would
        # half-match. A port is only matched when it is part of base_norm.
        spellings: set[str] = set()
        for n in self.internal_norms:
            spellings |= host_spellings(n)
        hp = (r"(?:"
              + "|".join(rf"(?:www\.)?{re.escape(s)}" for s in sorted(spellings))
              + r")(?![\w.-]|:\d)")
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
        self.robots_txt_found = False
        self.robots_action = "unchanged"   # what finalize_robots() did
        self.generated_sitemap: dict | None = None
        self.sitemap_discovery_ok = False  # a usable origin sitemap was found
        self.cruft_removed: dict[str, int] = {}
        self.stats_lock = threading.Lock()
        self.img_stats = {"lazy": 0, "dimensions": 0, "skipped_plugin": 0,
                          "unparsed": 0, "missing": 0}
        self.deploy_info: dict = {}
        # HTML files parse_and_save_html wrote (re-serialized by bs4) --
        # postprocess_html must only touch these, never byte-identical
        # HTML assets
        self.rewritten_html_paths: set[Path] = set()
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

    def say(self, msg: str) -> None:
        """Progress chatter -- silenced by --quiet (warnings and the final
        summary always print)."""
        if not self.cfg.quiet:
            print(msg)

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
                                    allow_redirects=False)
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
                self.robots_txt_found = True
                target = self.local_path_for(robots_url, is_page=False)
                if target:
                    self.write_bytes(target, resp.content)
                for m in SITEMAP_LINE_RE.finditer(resp.text):
                    sitemap_candidates.append(m.group(1).strip())
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
                remaining = [c for c in sitemap_candidates
                             if c in self.cfg.extra_sitemaps and c not in seen_sitemaps]
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
        rec.save_url = save_url
        if resp.history and self.cfg.rewrite and save_url != url:
            # keep redirect sources working in the static mirror: a
            # meta-refresh stub at the original path points to the target.
            # noindex'ed so the weak meta-refresh signal never competes with
            # the real page (the nginx config 301s this path anyway).
            src_target = self.local_path_for(url, is_page=True)
            if src_target:
                dest = self.localize_url(save_url)
                stub = ('<!doctype html><html><head><meta charset="utf-8">'
                        '<meta name="robots" content="noindex">'
                        f'<meta http-equiv="refresh" content="0;url={dest}">'
                        f'<link rel="canonical" href="{self.retarget_url(save_url)}">'
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
            with self.write_lock:
                # two source URLs redirecting to the same save_url must not
                # write (and report) the same variant file concurrently
                if variant in self.written_paths:
                    return
                self.written_paths.add(variant)
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
            elif tag_name == "link" and rel & {"next", "prev"}:
                # paginated archives often surface only here
                n = self.normalize_page_url(absolute)
                if n:
                    new_pages.append(n)
            elif tag_name == "link" and rel & {"canonical", "alternate",
                                               "shortlink", "pingback"}:
                return  # SEO/meta links, never fetched as assets
            elif (tag_name == "link" and rel & {"wlwmanifest", "edituri"}
                    and self.cfg.rewrite and self.cfg.strip_wp_cruft):
                return  # tag gets stripped -- don't mirror its target either
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
                if self.cfg.strip_wp_cruft:
                    self.strip_wp_cruft(soup)
                self.rewrite_soup_relative(soup)
                self.write_bytes(target, serialize_soup(soup))
                with self.stats_lock:
                    # postprocess_html may only touch files WE re-serialized
                    self.rewritten_html_paths.add(target)
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
        return base64.b64encode(localized.encode("utf-8")).decode("ascii")

    def strip_wp_cruft(self, soup: BeautifulSoup) -> None:
        """Remove WordPress head elements that only point at dynamic origin
        infrastructure a static mirror does not have (REST/oEmbed/RSD/feed
        discovery, pingback, wlwmanifest, ?p= shortlinks) plus the
        version-revealing generator meta. canonical / hreflang / og: /
        twitter: / JSON-LD stay untouched."""
        removed: dict[str, int] = {}

        def drop(tag, kind: str) -> None:
            tag.decompose()
            removed[kind] = removed.get(kind, 0) + 1

        for meta in soup.find_all(
                "meta", attrs={"name": re.compile("^generator$", re.I)}):
            drop(meta, "generator-meta")
        for link in soup.find_all("link"):
            rel_attr = link.get("rel")
            rel = {r.lower() for r in (rel_attr if isinstance(rel_attr, list)
                                       else (rel_attr or "").split())}
            hit = rel & {"pingback", "shortlink", "wlwmanifest", "edituri"}
            if hit:
                drop(link, sorted(hit)[0])
                continue
            if any("api.w.org" in r for r in rel):
                drop(link, "rest-api-discovery")
                continue
            ltype = (link.get("type") or "").lower()
            # feed/oEmbed discovery only -- hreflang alternates carry no type
            if "alternate" in rel and (
                    "oembed" in ltype or "rss+xml" in ltype or "atom+xml" in ltype):
                drop(link, "oembed-discovery" if "oembed" in ltype
                     else "feed-discovery")
        # Cloudflare-injected RUM beacon: reports to Cloudflare for the
        # ORIGIN zone -- off Cloudflare it only produces console/CORS errors
        for script in soup.find_all("script", src=True):
            if "cloudflareinsights.com" in script["src"]:
                drop(script, "cloudflare-beacon")
        if removed:
            with self.stats_lock:
                for kind, n in removed.items():
                    self.cruft_removed[kind] = self.cruft_removed.get(kind, 0) + n

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

                new_text = CSS_IMPORT_RE.sub(
                    rel_import, CSS_URL_RE.sub(rel, text))
                if new_text != text:
                    # transcode only what we changed; the file is now UTF-8,
                    # so a declared legacy @charset must say so too
                    data = CSS_CHARSET_RE.sub('@charset "UTF-8"',
                                              new_text).encode("utf-8")
        elif ext in ("js", "mjs") or "javascript" in ctype:
            text = decode_text_asset(resp)
            page_refs, asset_refs = self.scan_text_for_urls(text)
            discovered_pages.extend(page_refs)
            discovered.extend(asset_refs)
            if self.cfg.rewrite and self.host_probe_re.search(text):
                data = self.localize_text(text).encode("utf-8")

        target = self.local_path_for(url, is_page=False)
        if target:
            self.write_bytes(target, data)
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
                print("[extras] 404 page saved as 404.html")
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
        Stays on the origin (the nginx sub_filter localizes it at serve
        time) unless --target-domain retargets the whole export."""
        return self.target_prefix or f"{self.scheme}://{self.host}"

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
        print(f"[seo] sitemap.xml generated: {len(entries)} URLs"
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
        do_images = self.cfg.optimize_images
        do_staging = self.cfg.staging
        if not (do_images or do_staging):
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
            if do_images:
                changed |= self._optimize_images(soup)
            if changed:
                try:
                    html_file.write_bytes(serialize_soup(soup))
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

    def _optimize_images(self, soup: BeautifulSoup) -> bool:
        changed = False
        first = True
        for img in soup.find_all("img"):
            if img.find_parent("noscript") is not None:
                continue  # lazyload fallback markup; also must not consume
                          # the eager first-image slot
            is_first = first
            first = False
            classes = " ".join(img.get("class") or []).lower()
            plugin_lazy = (img.has_attr("data-src")
                           or img.has_attr("data-lazy-src")
                           or "lazy" in classes)
            if plugin_lazy:
                self.img_stats["skipped_plugin"] += 1
            elif (not img.has_attr("loading") and not is_first
                    and (img.get("fetchpriority") or "").lower() != "high"):
                # first image per page stays eager (LCP protection)
                img["loading"] = "lazy"
                if not img.has_attr("decoding"):
                    img["decoding"] = "async"
                self.img_stats["lazy"] += 1
                changed = True
            if img.has_attr("width") or img.has_attr("height") or plugin_lazy:
                continue
            src = (img.get("src") or "").strip()
            if not src.startswith("/") or src.startswith("//"):
                continue
            path = unicodedata.normalize(
                "NFC", unquote(src.split("?", 1)[0].split("#", 1)[0]))
            if path_extension(path) not in ("png", "jpg", "jpeg", "gif", "webp"):
                continue
            target = self.public_dir / path.lstrip("/")
            if not target.is_file():
                self.img_stats["missing"] += 1
                continue
            size = read_image_size(target)
            if size:
                img["width"], img["height"] = str(size[0]), str(size[1])
                self.img_stats["dimensions"] += 1
                changed = True
            else:
                self.img_stats["unparsed"] += 1
        return changed

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
            for hit in self.public_dir.rglob(pat):
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
                # extensionless page, or a directory base path
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
                "origin_error": unicodedata.normalize(
                    "NFC", unquote(ref.split("?")[0])) in origin_404})

        for html_file in sorted(self.public_dir.rglob("*.html")):
            rel_file = html_file.relative_to(self.public_dir).as_posix()
            soup = BeautifulSoup(html_file.read_bytes(), "html.parser")
            for tag in soup.find_all(True):
                if tag.name == "meta":
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
        """Host spellings that can appear in the exported files -- the only
        forms the __init__ regexes ever match, plus --target-domain ones."""
        hosts: list[str] = []
        try:                              # www. variant is nonsense for IPs
            ipaddress.ip_address(self.base_norm.rsplit(":", 1)[0])
            candidates = (self.base_norm, self.connect_norm)
        except ValueError:
            candidates = (f"www.{self.base_norm}", self.base_norm,
                          self.connect_norm)
        for h in candidates:
            if not h or norm_host(h) not in self.internal_norms:
                continue
            for spelling in sorted(host_spellings(h)):
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
            fp = canon_path(s.path or "/")
            tp = canon_path(t.path or "/") + (f"?{t.query}" if t.query else "")
            if t.path in (s.path, s.path + "/") and not t.query:
                continue  # self-redirect / generic trailing-slash rule
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

        inc_lines = ["# Redirects observed on the origin site, served as real"
                     " 301s (generated)."]
        if not redirect_rules:
            inc_lines.append("# none observed during the export")
        # quoted args; the trailing ? stops nginx from re-appending the
        # request args to a target that already carries its own query
        inc_lines += [f'rewrite "^{re.escape(fp)}$" "{tp}?" permanent;'
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
    listen [::]:80;
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
                   " --target-domain"]
        netlify += [f"{fp} {tp} 301" for fp, tp in redirect_rules]
        (self.public_dir / "_redirects").write_text(
            "\n".join(netlify) + "\n", encoding="utf-8")

        # Apache: mod_substitute cannot interpolate %{HTTP_HOST} into
        # replacements, so Apache deployments need --target-domain as well.
        # RedirectMatch, not Redirect: mod_alias Redirect is prefix-matching
        # and /alt would also hijack /alternative/.
        htaccess = ["# generated -- Apache has no serve-time host substitution:",
                    "# for correct canonical/og/JSON-LD URLs re-export with"
                    " --target-domain"]
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
""".replace("__SITE_SLUG__", site_slug).replace("__PORT__", str(self.cfg.port))
        (self.cfg.out_dir / "docker-compose.yml").write_text(compose, encoding="utf-8")

        # keep report/mobile-variants out of the docker build context
        (self.cfg.out_dir / ".dockerignore").write_text(
            "mobile-variants/\nreport.json\nreport.txt\n", encoding="utf-8")

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
# explicit argument beats environment beats baked-in default
PORT="${2:-${PORT:-__PORT__}}"
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
                "wp_cruft_removed": self.cruft_removed,
                "image_optimization": self.img_stats,
                "staging_noindex": self.cfg.staging,
                "target_domain": self.cfg.target_domain,
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
            (f"Images:          {self.img_stats['lazy']}x loading=lazy, "
             f"{self.img_stats['dimensions']}x width/height injected, "
             f"{self.img_stats['skipped_plugin']}x plugin-lazyload skipped"
             if self.cfg.optimize_images and self.cfg.rewrite else
             "Images:          optimization disabled"),
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
                "--no-rewrite: WP cruft stripping, image optimization and "
                "redirect stub pages are disabled")
        if self.cfg.staging and self.cfg.target_domain:
            self.warnings.append(
                "--staging blocks all indexing -- --target-domain only "
                "affects the markup of this (unindexable) preview")
        if self.public_dir.exists():
            if self.cfg.clean:
                shutil.rmtree(self.public_dir)
                shutil.rmtree(self.cfg.out_dir / "mobile-variants",
                              ignore_errors=True)
                print("[init] --clean: removed existing public/ "
                      "and mobile-variants/")
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

        # a crash in ANY finalization step must never discard the report
        # (and with it every diagnostic) after a potentially hours-long crawl
        def step(name: str, fn) -> None:
            try:
                fn()
            except Exception as exc:            # noqa: BLE001
                self.warnings.append(f"{name} failed: {exc!r}")
                print(f"[warn] {name} failed: {exc!r}")

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
        if self.cfg.rewrite and self.cfg.optimize_images:
            print(f"[seo] images: {self.img_stats['lazy']}x loading=lazy, "
                  f"{self.img_stats['dimensions']}x width/height injected")
        if self.cfg.staging:
            print("[staging] export is noindexed everywhere "
                  "(robots.txt, X-Robots-Tag, meta robots)")
        elif not self.cfg.target_domain:
            print("[seo] canonical/og/JSON-LD/sitemap keep the origin host; "
                  "the generated nginx.conf substitutes the served domain "
                  "at runtime (sub_filter)")
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
        code = 0 if ok else 1
        if (self.cfg.fail_on in ("errors", "verify")
                and (failed or self.asset_errors)):
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
    downloaded; wp-admin / wp-json / xmlrpc are never requested.
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
    ap.add_argument("--no-mobile-check", dest="mobile_check", action="store_false",
                    help="skip the mobile-vs-desktop HTML comparison "
                         "(saves one extra request per page)")
    ap.add_argument("--mobile-user-agent", default=None,
                    help="user agent used for the mobile comparison "
                         "(default: iPhone Safari)")
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
    ap.add_argument("--no-optimize-images", dest="optimize_images",
                    action="store_false",
                    help="skip injecting loading=lazy/decoding=async and "
                         "width/height attributes into <img> tags. "
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
    args = ap.parse_args(argv)

    base = args.base_url.strip()
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    base = base.rstrip("/")

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
        generate_sitemap=args.generate_sitemap,
        strip_wp_cruft=args.strip_wp_cruft,
        optimize_images=args.optimize_images,
        staging=args.staging,
        target_domain=target_domain,
        sitemap_include_linked=args.sitemap_include_linked,
        fail_on=args.fail_on,
        quiet=args.quiet,
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
