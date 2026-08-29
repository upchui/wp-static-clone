"""Static site search: the WordPress search box keeps working in the export.

WordPress answers `GET /?s=term` with a themed results page -- a dynamic
endpoint a static mirror cannot serve. Worse: on every deploy target this
tool generates (nginx `try_files $uri $uri/ =404`, Netlify, Apache
DirectoryIndex) `/?s=term` silently returns the HOMEPAGE with HTTP 200 --
a wrong success nobody notices.

This plugin reproduces the ORIGINAL behavior client-side without changing
what the visitor does: type into the theme's own search box, press Enter,
land on a themed results page listing matching pages with title and text
snippet. No live/AJAX dropdown, no extra asset, no dependency.

Three pieces:

1. INDEX -- during the crawl every fetched page contributes
   {path, title, meta description, main-content text} taken from the soup
   the core already parsed (`pre_discover_soup`, no extra parse cost). At
   `run_end` the SAME PageRecord filter the generated sitemap uses selects
   the indexable pages and `/search-index.json` is written (root-relative;
   `application/json` is already in the generated nginx `gzip_types`).

2. RESULTS PAGE -- generated at a language-derived path (`<html lang>`
   starts with "de" -> `/suche/`, else `/search/`; `--search-path`
   overrides) by CLONING an already-exported page -- `404.html` first
   (complete theme chrome, empty content well), else the homepage, else a
   minimal self-contained page. The main content well is emptied and
   filled with the results container plus a small dependency-free
   renderer. The page is `noindex,follow`ed, exactly like WordPress
   noindexes search results.

3. WIRING -- every search form on every page gets `action="<search path>"`
   (the `name="s"` input stays, so URLs keep the WordPress shape
   `/suche/?s=term`), the Yoast JSON-LD SearchAction `urlTemplate` is
   pointed at the same path, and a ~190-byte script at the top of `<head>`
   redirects any surviving `?s=`/`?q=` URL (bookmarks, external links,
   the JSON-LD entry point, forms this plugin did not recognize) to the
   results page before first paint.

The wiring runs in `postprocess_soup`, not `rewrite_soup`: the search path
depends on the site language, which is only known after the crawl.

Rewrite mode only (`--no-rewrite` re-serializes nothing, so nothing can be
injected). Disable with `--no-search`.
"""
import json
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from bs4 import (BeautifulSoup, CData, Comment, Declaration, Doctype,
                 NavigableString, ProcessingInstruction, Tag)

from wp_static_export import Plugin, canon_path

INDEX_PATH = "/search-index.json"
SCHEMA_VERSION = 1
RESULTS_ID = "wpse-search-results"
MARKER = "wpse-search"                  # data attribute on our injected tags

# -- content extraction ------------------------------------------------------
# Generic, NOT theme-specific: the main content well of a WordPress page in
# decreasing order of confidence. `main` before `[role=main]` because a
# theme that has both means them to be the same element.
CONTENT_SELECTORS = ("main", "[role=main]", "#content", "#primary", "article",
                     ".entry-content", ".post-content", ".wpb-content-wrapper",
                     ".site-content")
# tags whose text is never page content
DROP_TAGS = frozenset((
    "script", "style", "noscript", "template", "svg", "canvas", "nav",
    "header", "footer", "aside", "form", "iframe", "button", "select",
    "textarea", "map", "audio", "video"))
DROP_ROLES = frozenset((
    "banner", "navigation", "contentinfo", "search", "complementary",
    "menu", "menubar", "dialog", "alertdialog", "toolbar"))
DROP_IDS = frozenset((
    "comments", "respond", "colophon", "masthead", "footer", "bottom-bar",
    "sidebar", "secondary", "wpadminbar", "cmplz-cookiebanner-container",
    "cmplz-manage-consent", "primary-menu", "mobile-menu"))
# WHOLE class/id tokens (never substrings -- "innovation" must not match
# "nav", and a naive `"menu" in cls` would eat real content classes)
DROP_CLASS_RE = re.compile(
    r"(?:screen-reader[\w-]*|assistive-text|skip-link|sr-only|visually-hidden"
    r"|breadcrumbs?|[\w-]*-breadcrumbs"
    # menu-item*, not menu-*: a content block may legitimately be called
    # "menu-card-page" (a restaurant's Speisekarte), and dropping real
    # content is far worse than keeping a bit of chrome
    r"|nav|navigation|menu|[\w-]*-(?:nav|navigation|menu)|menu-item[\w-]*"
    r"|widget|widget[_-][\w-]*|[\w-]*-widgets?|sidebar|site-header|site-footer"
    r"|masthead|top-bar|bottom-bar|header-bar|page-title-head"
    r"|cmplz[\w-]*|cookie[\w-]*|pswp[\w_-]*"
    r"|social[\w-]*|shar(?:e|ing)[\w-]*|comments?|comment[\w-]*"
    r"|pagination|nav-links|post-navigation|scroll-top|back-to-top)$")
# bs4 string subclasses that are markup, not text
SPECIAL_STRINGS = (Comment, CData, Doctype, ProcessingInstruction, Declaration)
# block-level elements get a separating space so "</p><p>" doesn't glue words
BLOCK_TAGS = frozenset((
    "p", "div", "li", "tr", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "td", "th", "dt", "dd", "blockquote", "pre",
    "figcaption", "address", "table", "ul", "ol", "dl"))
WS_RE = re.compile(r"\s+")

# <title> separators WordPress themes use before the site name
TITLE_SEPARATORS = (" - ", " \u2013 ", " \u2014 ", " | ", " \u00b7 ",
                    " \u00bb ", " :: ", " / ")

DEFAULT_PATHS = (("de", "/suche/"),)    # language prefix -> path
FALLBACK_PATH = "/search/"


def path_for_lang(lang: str) -> str:
    """Results-page path derived from the site's <html lang>."""
    low = (lang or "").strip().lower()
    for prefix, path in DEFAULT_PATHS:
        if low.startswith(prefix):
            return path
    return FALLBACK_PATH


def validate_search_path(path: str) -> str:
    """Canonical form of a --search-path value. Raises ValueError with a
    user-facing message for anything that cannot become a real directory
    under public/."""
    raw = (path or "").strip()
    if not raw:
        raise ValueError("must not be empty")
    if not raw.startswith("/"):
        raise ValueError(f"must start with '/': {raw!r}")
    if not raw.endswith("/"):
        raise ValueError(f"must end with '/': {raw!r}")
    if raw == "/":
        raise ValueError("must not be the site root '/' -- the root is the "
                         "homepage and '/?s=' is exactly what cannot work "
                         "statically")
    if "?" in raw or "#" in raw:
        raise ValueError(f"must not contain a query or fragment: {raw!r}")
    if (any(ord(c) < 33 for c in raw) or '"' in raw or "'" in raw
            or "\\" in raw):
        raise ValueError(f"must not contain whitespace, quotes or "
                         f"backslashes: {raw!r}")
    if any(seg in ("", ".", "..") for seg in raw.strip("/").split("/")):
        raise ValueError(f"must not contain empty or '.'/'..' segments: "
                         f"{raw!r}")
    if raw.lower().startswith(("/wp-content/", "/wp-includes/", "/wp-admin/")):
        raise ValueError(f"must not live under a WordPress system "
                         f"directory: {raw!r}")
    return canon_path(raw)


def _dropped(tag: Tag) -> bool:
    """True when this element is site chrome, not page content."""
    if tag.name in DROP_TAGS:
        return True
    if (tag.get("role") or "").strip().lower() in DROP_ROLES:
        return True
    if (tag.get("aria-hidden") or "").strip().lower() == "true":
        return True
    if (tag.get("hidden") is not None
            or "display:none" in (tag.get("style") or "")
                                 .replace(" ", "").lower()):
        return True
    ident = (tag.get("id") or "").strip().lower()
    if ident and (ident in DROP_IDS or DROP_CLASS_RE.fullmatch(ident)):
        return True
    for cls in (tag.get("class") or ()):
        if DROP_CLASS_RE.fullmatch(cls.strip().lower()):
            return True
    return False


def content_root(soup):
    """The page's main content element -- None when only <body> is left
    (callers decide whether that fallback is good enough)."""
    body = soup.body or soup
    for sel in CONTENT_SELECTORS:
        try:
            el = body.select_one(sel)
        except Exception:               # noqa: BLE001 -- exotic selector
            el = None
        if el is not None and not _dropped(el):
            return el
    return None


def extract_text(root, limit: int) -> str:
    """Visible text of a subtree, chrome pruned, whitespace normalized,
    NFC-normalized and capped at `limit` characters.

    Prunes WITHOUT mutating: `pre_discover_soup` hands us the live soup
    that the crawl still has to discover URLs in and rewrite."""
    out: list = []
    size = 0

    def walk(node) -> None:
        nonlocal size
        for child in node.children:
            if size >= limit:
                return
            if isinstance(child, NavigableString):
                if isinstance(child, SPECIAL_STRINGS):
                    continue
                s = str(child)
                if s.strip():
                    out.append(s)
                    size += len(s)
            elif isinstance(child, Tag):
                if _dropped(child):
                    continue
                walk(child)
                if child.name in BLOCK_TAGS:
                    out.append(" ")

    walk(root)
    text = WS_RE.sub(" ", "".join(out)).strip()
    return unicodedata.normalize("NFC", text)[:limit].rstrip()


def title_suffix(titles, site: str) -> str:
    """The ' <sep> <site name>' tail WordPress themes append to every
    <title> ('' when it cannot be determined). Deterministic on ties."""
    if not site:
        return ""
    counts: dict = {}
    for t in titles:
        for sep in TITLE_SEPARATORS:
            tail = sep + site
            if t.endswith(tail) and len(t) > len(tail):
                counts[tail] = counts.get(tail, 0) + 1
                break
    if not counts:
        return ""
    return max(sorted(counts), key=counts.get)


def strip_title_suffix(title: str, suffix: str) -> str:
    """'Fassaden - Huthansl' -> 'Fassaden' (WordPress shows the bare post
    title in search results)."""
    if suffix and title.endswith(suffix) and len(title) > len(suffix):
        return title[: -len(suffix)].rstrip()
    return title


def fix_search_action(node, target_url: str) -> int:
    """Repoint every schema.org SearchAction in a parsed JSON-LD graph at
    `target_url`. Returns how many were ACTUALLY changed (an already
    correct graph reports 0, so re-running the wiring is a no-op).
    Handles all three shapes Yoast/WP emit: an EntryPoint object, a bare
    string, a list."""
    changed = 0
    if isinstance(node, list):
        for item in node:
            changed += fix_search_action(item, target_url)
        return changed
    if not isinstance(node, dict):
        return changed
    types = node.get("@type")
    types = types if isinstance(types, list) else [types]
    if "SearchAction" in types and "target" in node:
        tgt = node["target"]
        if isinstance(tgt, dict) and isinstance(tgt.get("urlTemplate"), str):
            new = _retarget(tgt["urlTemplate"], target_url)
            if new != tgt["urlTemplate"]:
                tgt["urlTemplate"] = new
                changed += 1
        elif isinstance(tgt, str):
            new = _retarget(tgt, target_url)
            if new != tgt:
                node["target"] = new
                changed += 1
        elif isinstance(tgt, list):
            new_list = [_retarget(t, target_url) if isinstance(t, str) else t
                        for t in tgt]
            if new_list != tgt:
                node["target"] = new_list
                changed += 1
    for value in node.values():
        if isinstance(value, (dict, list)):
            changed += fix_search_action(value, target_url)
    return changed


def _retarget(old: str, new_path_and_query: str) -> str:
    """Keep the SEO-bearing scheme+host of the old urlTemplate, replace
    only path+query (JSON-LD stays absolute on purpose; --target-domain
    already moved it to the new host before we get here)."""
    s = urlsplit(old)
    if not s.netloc:
        return new_path_and_query
    n = urlsplit(new_path_and_query)
    return urlunsplit((s.scheme, s.netloc, n.path, n.query, ""))


def dump_jsonld(data) -> str:
    """Compact JSON-LD in the style WordPress/Yoast emit: escaped forward
    slashes (keeps the exporter's escaped-URL machinery working) and
    \\u003c for '<' so a string can never terminate the <script> block."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return (text.replace("/", "\\/")
                .replace("<", "\\u003c").replace(">", "\\u003e")
                .replace("&", "\\u0026").replace("\u2028", "\\u2028")
                .replace("\u2029", "\\u2029"))


def js_literal(value) -> str:
    """A JS/JSON literal that is safe to embed inside <script>."""
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026").replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


# -- the injected JavaScript -------------------------------------------------
# Both snippets are ASCII apart from the German UI words; every regex
# character class uses \u escapes so no encoding step can mangle them.

HEAD_REDIRECT_JS = (
    '(function(){var p=__PATH__,c=location.pathname;'
    'if(c.charAt(c.length-1)!=="/"){c+="/";}'
    'if(c===p||!/[?&](?:s|q)=/.test(location.search)){return;}'
    'location.replace(p+location.search);})();')

RENDERER_JS = r'''
(function () {
  "use strict";
  var C = __CFG__;
  var T = C.de ? {
    n: "%n Treffer f\u00fcr \u201e%s\u201c",
    n1: "1 Treffer f\u00fcr \u201e%s\u201c",
    z: "Keine Ergebnisse f\u00fcr \u201e%s\u201c. Bitte mit einem anderen "
       + "Begriff suchen.",
    e: "Bitte einen Suchbegriff eingeben.",
    f: "Der Suchindex konnte nicht geladen werden.",
    t: "Suchergebnisse f\u00fcr \u201e%s\u201c"
  } : {
    n: "%n results for \u201c%s\u201d",
    n1: "1 result for \u201c%s\u201d",
    z: "No results for \u201c%s\u201d. Please try a different term.",
    e: "Please enter a search term.",
    f: "The search index could not be loaded.",
    t: "Search results for \u201c%s\u201d"
  };
  var MARKS = /[\u0300-\u036f]/g;
  var WORDCH = /[0-9a-z\u00c0-\u024f\u0370-\u1fff\u3040-\uffff]/;
  var SPLIT = /[^0-9A-Za-z\u00c0-\u024f\u0370-\u1fff\u3040-\uffff]+/;

  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function put(tpl, key, val) {          // never a $-pattern replacement
    return tpl.replace(key, function () { return val; });
  }
  function fold(s) {
    s = String(s).toLowerCase();
    if (s.normalize) { s = s.normalize("NFD").replace(MARKS, ""); }
    return s.replace(/\u00df/g, "ss");
  }
  function foldMap(s) {                  // folded text + folded->source map
    var t = "", m = [], i, j, f;
    for (i = 0; i < s.length; i++) {
      f = fold(s.charAt(i));
      for (j = 0; j < f.length; j++) { t += f.charAt(j); m.push(i); }
    }
    m.push(s.length);                    // sentinel: end of the last char
    return { t: t, m: m };
  }
  function param(name) {
    var m = new RegExp("[?&]" + name + "=([^&]*)").exec(location.search);
    if (!m) { return ""; }
    var v = m[1].replace(/\+/g, " ");
    try { return decodeURIComponent(v); } catch (e) { return v; }
  }
  function terms(q) {
    var phrase = /^\s*"([^"]+)"\s*$/.exec(q);
    var parts = phrase ? [fold(phrase[1])] : fold(q).split(SPLIT);
    var out = [], i;
    for (i = 0; i < parts.length && out.length < 8; i++) {
      if (parts[i]) { out.push(parts[i]); }
    }
    return out;
  }
  function weigh(text, term, wordScore, subScore) {
    var i = text.indexOf(term);
    if (i < 0) { return 0; }
    return (i === 0 || !WORDCH.test(text.charAt(i - 1))) ? wordScore
                                                         : subScore;
  }
  function score(d, ts) {                // AND over terms, like WordPress
    var s = 0, i, hit;
    if (!d.f) { d.f = [fold(d[1] || ""), fold(d[2] || ""), fold(d[3] || "")]; }
    for (i = 0; i < ts.length; i++) {
      hit = weigh(d.f[0], ts[i], 10, 5) + weigh(d.f[1], ts[i], 4, 2)
          + weigh(d.f[2], ts[i], 3, 1);
      if (!hit) { return 0; }
      s += hit;
    }
    return s;
  }
  function snippet(d, ts) {
    var src = d[3] || d[2] || "";
    if (!src) { return ""; }
    var fm = foldMap(src), t = fm.t, m = fm.m;
    var at = -1, i, p, from;
    for (i = 0; i < ts.length; i++) {
      p = t.indexOf(ts[i]);
      if (p >= 0 && (at < 0 || p < at)) { at = p; }
    }
    if (at < 0) { at = 0; }
    var b = Math.min(t.length, at + Math.ceil(C.snip / 2));
    var a = Math.max(0, b - C.snip);
    b = Math.min(t.length, a + C.snip);
    var ranges = [];
    for (i = 0; i < ts.length; i++) {
      from = a;
      while (from < b) {
        p = t.indexOf(ts[i], from);
        if (p < 0 || p + ts[i].length > b) { break; }
        ranges.push([m[p], m[p + ts[i].length]]);
        from = p + ts[i].length;
      }
    }
    ranges.sort(function (x, y) { return x[0] - y[0]; });
    var sa = m[a], sb = m[b], out = "", cur = sa, k, r;
    for (k = 0; k < ranges.length; k++) {
      r = ranges[k];
      if (r[0] < cur) { continue; }      // overlapping term hits
      out += esc(src.slice(cur, r[0])) + "<mark>"
           + esc(src.slice(r[0], r[1])) + "</mark>";
      cur = r[1];
    }
    out += esc(src.slice(cur, sb));
    return (sa > 0 ? "\u2026 " : "") + out
         + (sb < src.length ? " \u2026" : "");
  }
  function show(status, body) {
    var el = document.getElementById("__RESULTS_ID__");
    if (!el) { return; }
    el.innerHTML = '<p class="wpse-search-status">' + status + "</p>"
      + '<div class="wpse-search-hits">' + (body || "") + "</div>";
  }
  function run(data, q) {
    var ts = terms(q), docs = (data && data.docs) || [], hits = [], i, s;
    for (i = 0; i < docs.length; i++) {
      s = ts.length ? score(docs[i], ts) : 0;
      if (s > 0) { hits.push([s, docs[i]]); }
    }
    hits.sort(function (x, y) {
      return y[0] - x[0] || String(x[1][1]).localeCompare(String(y[1][1]));
    });
    var qe = esc(q);
    if (!hits.length) { show(put(T.z, "%s", qe)); return; }
    var html = "", d, sn;
    for (i = 0; i < hits.length && i < C.max; i++) {
      d = hits[i][1];
      sn = snippet(d, ts);
      html += '<article class="wpse-result post">'
        + '<h2 class="entry-title"><a href="' + esc(d[0]) + '">'
        + esc(d[1] || d[0]) + "</a></h2>"
        + (sn ? '<div class="entry-content"><p>' + sn + "</p></div>" : "")
        + "</article>";
    }
    show(put(hits.length === 1 ? T.n1 : put(T.n, "%n", hits.length),
             "%s", qe), html);
  }
  function boot() {
    var q = param("s") || param("q"), i;
    var ins = document.querySelectorAll("input[name=s],input[name=q]");
    for (i = 0; i < ins.length; i++) { ins[i].value = q; }
    if (q) { document.title = put(T.t, "%s", q) + C.sfx; }
    if (!q) { show(T.e); return; }
    var x = new XMLHttpRequest();
    x.open("GET", C.idx, true);
    x.onreadystatechange = function () {
      if (x.readyState !== 4) { return; }
      var data = null;
      if (x.status >= 200 && x.status < 300) {
        try { data = JSON.parse(x.responseText); } catch (e) { data = null; }
      }
      if (data) { run(data, q); } else { show(T.f); }
    };
    x.send();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else { boot(); }
})();
'''

MINIMAL_PAGE = (
    '<!doctype html><html lang="__LANG__"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<meta name="robots" content="noindex,follow">'
    '<title>__TITLE__</title>'
    '<style>body{font:16px/1.6 system-ui,sans-serif;margin:0 auto;'
    'max-width:44rem;padding:2rem 1rem}mark{background:#ff9}'
    '.wpse-result{margin:1.5rem 0}.entry-title{font-size:1.2rem;margin:0}'
    '</style></head><body class="search search-results">'
    '<main id="content" role="main">'
    '<form class="searchform" method="get" action="__PATH__" role="search">'
    '<label class="screen-reader-text" for="wpse-s">__H1__</label>'
    '<input id="wpse-s" type="text" name="s" value="">'
    '<input type="submit" value="__H1__"></form>'
    '</main></body></html>')


class Search(Plugin):
    name = "search"
    config_fields = {"search": True, "search_path": "",
                     "search_max_chars": 2000}

    # -- CLI -----------------------------------------------------------------
    @classmethod
    def add_cli_args(cls, group):
        group.add_argument(
            "--no-search", dest="search", action="store_false",
            help="do not export a working site search (default: on). The "
                 "theme's search box then keeps pointing at '/?s=...', "
                 "which every static host answers with the homepage")
        group.add_argument(
            "--search-path", default="", metavar="PATH",
            help="path of the generated search-results page, e.g. "
                 "/suche/ (default: derived from <html lang> -- '/suche/' "
                 "for German sites, '/search/' otherwise). Must start and "
                 "end with '/'")
        group.add_argument(
            "--search-max-chars", type=int, default=2000, metavar="N",
            help="per-page text cap in the search index (default 2000; "
                 "lower it for very large sites, 0 indexes titles and "
                 "meta descriptions only)")

    @classmethod
    def finish_args(cls, ap, args, cfg):
        if args.search_path:
            try:
                args.search_path = validate_search_path(args.search_path)
            except ValueError as exc:
                ap.error(f"--search-path {exc}")
        if not 0 <= args.search_max_chars <= 100000:
            ap.error(f"--search-max-chars must be between 0 and 100000, "
                     f"got {args.search_max_chars}")
        cfg.search = args.search
        cfg.search_path = args.search_path
        cfg.search_max_chars = args.search_max_chars

    # -- per-run state -------------------------------------------------------
    def __init__(self, exporter):
        super().__init__(exporter)
        # page_url -> {"desc", "text", "lang", "site"}; filled from crawl
        # worker threads under exp.stats_lock
        self.docs: dict = {}
        self._settings = None
        self.stats = {
            "enabled": True, "path": "", "lang": "", "ui_language": "",
            "index_path": INDEX_PATH, "pages_indexed": 0,
            "pages_without_text": 0, "index_bytes": 0, "page_written": False,
            "page_source": "", "forms_rewritten": 0, "pages_wired": 0,
            "jsonld_search_actions_fixed": 0, "collision": None,
        }

    @property
    def enabled(self) -> bool:
        return bool(self.exp.cfg.rewrite and self.exp.cfg.search)

    def run_start(self) -> None:
        if self.exp.cfg.search and not self.exp.cfg.rewrite:
            self.exp.warnings.append(
                "--no-rewrite: the static site search cannot be exported "
                "(no page is re-serialized, so no search page, index or "
                "form rewriting is possible)")

    # -- phase 1: collect ----------------------------------------------------
    def pre_discover_soup(self, soup, page_url: str) -> None:
        """Harvest the index payload from the soup the core just parsed --
        read-only: the crawl still has to discover URLs in this tree."""
        if not self.enabled or page_url.endswith("/404.html"):
            return
        try:
            html = soup.find("html")
            lang = (html.get("lang") or "") if html is not None else ""
            site = ""
            meta = soup.find("meta", attrs={"property": "og:site_name"})
            if meta is not None:
                site = (meta.get("content") or "").strip()
            desc = ""
            dmeta = soup.find("meta",
                              attrs={"name": re.compile("^description$",
                                                        re.I)})
            if dmeta is not None:
                desc = unicodedata.normalize(
                    "NFC", WS_RE.sub(" ", dmeta.get("content") or "").strip())
            root = content_root(soup) or soup.body
            limit = max(0, int(self.exp.cfg.search_max_chars))
            text = (extract_text(root, limit)
                    if (root is not None and limit) else "")
            entry = {"desc": desc, "text": text, "lang": lang.strip(),
                     "site": site}
        except Exception as exc:            # noqa: BLE001
            # a crash here would abort process_page and lose the whole
            # page from the export -- never worth it for a search index
            self.exp.warnings.append(
                f"search index: skipped {page_url} "
                f"({exc.__class__.__name__}: {exc})")
            return
        with self.exp.stats_lock:
            self.docs[page_url] = entry

    # -- phase 2: decide (once, after the crawl) -----------------------------
    def settings(self) -> dict:
        """Language, path and site name -- resolved once, after the crawl,
        because the path is derived from the pages' <html lang>. Also
        settles the collision question BEFORE any page is wired: if the
        origin already serves the search path or the index path, the whole
        feature stands down instead of pointing the forms at a page that
        cannot answer them."""
        if self._settings is not None:
            return self._settings
        indexed = self._selected_pages()
        langs: dict = {}
        sites: dict = {}
        home_lang = ""
        for url, _rec in indexed:
            entry = self.docs.get(url) or {}
            lang = (entry.get("lang") or "").lower()
            if lang:
                langs[lang] = langs.get(lang, 0) + 1
                if urlsplit(url).path == "/":
                    home_lang = lang
            site = entry.get("site") or ""
            if site:
                sites[site] = sites.get(site, 0) + 1
        if langs:
            top = max(langs.values())
            winners = sorted(k for k, v in langs.items() if v == top)
            lang = home_lang if home_lang in winners else winners[0]
            if len(langs) > 1:
                self.exp.warnings.append(
                    f"static search: pages declare more than one language "
                    f"({', '.join(sorted(langs))}) -- using {lang!r} for the "
                    f"results page path and UI (override with --search-path)")
        else:
            lang = ""
        path = path_for_lang(lang)
        configured = (self.exp.cfg.search_path or "").strip()
        if configured:
            try:
                path = validate_search_path(configured)
            except ValueError as exc:
                # programmatic Config(...) bypasses the CLI validation
                self.exp.warnings.append(
                    f"static search: ignoring invalid search_path "
                    f"{configured!r} ({exc}) -- using {path}")
        site = max(sorted(sites), key=sites.get) if sites else ""
        suffix = title_suffix(
            [rec.title for _u, rec in indexed if rec.title], site)
        self._settings = {"lang": lang, "path": path, "site": site,
                          "suffix": suffix,
                          "de": lang.lower().startswith("de"),
                          "collision": self._collision(path)}
        return self._settings

    def _collision(self, path: str) -> str | None:
        """The origin's own content always wins. Overwriting it would break
        the 1:1 mirror guarantee, and silently renaming our page would put
        the live search URL somewhere no doc, bookmark or config mentions
        -- so the feature stands down and says which flag fixes it."""
        for kind, url_path, is_page in (("results page", path, True),
                                        ("index", INDEX_PATH, False)):
            target = self.exp.local_path_for(
                f"{self.exp.scheme}://{self.exp.host}{url_path}",
                is_page=is_page)
            if target is None:
                msg = (f"static search: {url_path} cannot be mapped to a "
                       f"local path -- the search was NOT exported")
                self.exp.warnings.append(msg)
                print(f"[warn] {msg}")
                return url_path
            taken = target in self.exp.written_paths
            if not taken:
                try:
                    taken = target.is_file()
                except OSError:
                    taken = False
            if taken:
                msg = (f"static search: the exported site already has its "
                       f"own {kind} at {url_path} -- the search was NOT "
                       f"exported (origin content is never overwritten). "
                       f"Re-run with --search-path /another-path/ or "
                       f"--no-search")
                self.exp.warnings.append(msg)
                print(f"[warn] {msg}")
                return url_path
        return None

    def _selected_pages(self) -> list:
        """(save_url, PageRecord) of every page that belongs in the index --
        the SAME filter write_generated_sitemap() applies, so the search
        never offers a URL the mirror does not serve."""
        exp, cfg = self.exp, self.exp.cfg
        seen: dict = {}
        for p in exp.pages:
            if p.error or "html" not in p.content_type or p.is_stub:
                continue
            if (p.source != "sitemap" and exp.sitemap_discovery_ok
                    and not cfg.sitemap_include_linked):
                continue
            if p.noindex:
                continue
            url = p.save_url or p.url
            if (p.canonical and exp.is_internal(p.canonical)
                    and exp.canonical_target(p) != url):
                continue
            seen.setdefault(url, p)
        return sorted(seen.items())

    # -- phase 3: wire every exported page -----------------------------------
    def wants_postprocess(self) -> bool:
        return self.enabled

    def postprocess_soup(self, soup) -> bool:
        """Point the theme's search forms and the JSON-LD SearchAction at
        the results page and inject the ?s= redirect into <head>.

        Runs here rather than in rewrite_soup because the results-page path
        is derived from the site language, which is only known once the
        crawl is over. postprocess_html() iterates exactly the pages the
        core re-serialized (every real page plus 404.html, no redirect
        stubs) and is already gated on cfg.rewrite."""
        if not self.enabled:
            return False
        cfg = self.settings()
        if cfg["collision"]:
            return False        # no results page will exist -- wire nothing
        path = cfg["path"]
        changed = self._wire_forms(soup, path)
        changed |= self._wire_jsonld(soup, path)
        changed |= self._inject_head_redirect(soup, path)
        if changed:
            self.stats["pages_wired"] += 1
        return changed

    def _wire_forms(self, soup, path: str) -> bool:
        changed = False
        for form in self._search_forms(soup):
            if form.get("action") != path:
                form["action"] = path
                changed = True
            if (form.get("method") or "").lower() != "get":
                form["method"] = "get"      # ?s= must land in the URL
                changed = True
            # The7 & co. put a decoy <a class="submit" href=""> next to a
            # hidden submit input; theme JS preventDefault()s the click, so
            # this only matters without JS -- an empty href would reload
            # the current page instead of going anywhere useful.
            for a in form.find_all("a"):
                if not (a.get("href") or "").strip():
                    a["href"] = path
                    changed = True
            self.stats["forms_rewritten"] += 1
        return changed

    @staticmethod
    def _search_forms(soup):
        """Every search form, detected generically: role=search, or simply
        a form carrying an <input name="s"> (WordPress' own field name) or
        name="q"."""
        out = []
        for form in soup.find_all("form"):
            has_s = form.find("input", attrs={
                "name": re.compile(r"^(?:s|q)$")}) is not None
            if has_s or (form.get("role") or "").strip().lower() == "search":
                out.append(form)
        return out

    def _wire_jsonld(self, soup, path: str) -> bool:
        """Repoint the Yoast/WP `SearchAction.urlTemplate` at the results
        page. Parsed and re-dumped rather than regex-patched: the value is
        JSON-escaped ("https:\\/\\/host\\/?s={search_term_string}") and
        string surgery on that is how JSON-LD gets corrupted."""
        target = f"{path}?s={{search_term_string}}"
        changed = False
        for script in soup.find_all("script"):
            if "ld+json" not in (script.get("type") or "").lower():
                continue
            text = "".join(c for c in script.contents if isinstance(c, str))
            if "SearchAction" not in text:
                continue
            try:
                data = json.loads(text)
            except ValueError:
                self.exp.warnings.append(
                    "static search: a JSON-LD block with a SearchAction is "
                    "not valid JSON and was left unchanged")
                continue
            n = fix_search_action(data, target)
            if not n:
                continue
            try:
                script.string = dump_jsonld(data)
            except (TypeError, ValueError):
                continue
            changed = True
            self.stats["jsonld_search_actions_fixed"] += n
        return changed

    def _inject_head_redirect(self, soup, path: str) -> bool:
        """A ~190-byte script at the TOP of <head> (right after the charset
        meta, so the declaration stays in the first 1024 bytes): a URL that
        still carries ?s= / ?q= -- a bookmark, an external link, the
        JSON-LD entry point, a form we failed to recognize -- is replaced
        with the results page BEFORE the browser paints anything."""
        head = soup.find("head")
        if head is None:
            return False
        if head.find("script", attrs={"data-" + MARKER: "redirect"}):
            return False
        tag = soup.new_tag("script")
        tag["data-" + MARKER] = "redirect"
        tag.string = HEAD_REDIRECT_JS.replace("__PATH__", js_literal(path))
        charset = (head.find("meta", charset=True)
                   or head.find("meta", attrs={"http-equiv": re.compile(
                       "^content-type$", re.I)}))
        if charset is not None:
            charset.insert_after(tag)
        else:
            head.insert(0, tag)
        return True

    # -- phase 4: index + results page ---------------------------------------
    def run_end(self) -> None:
        if not self.enabled:
            self.stats["enabled"] = False
            return
        cfg = self.settings()
        self.stats["path"] = cfg["path"]
        self.stats["lang"] = cfg["lang"]
        self.stats["ui_language"] = "de" if cfg["de"] else "en"
        if cfg["collision"]:
            self.stats["collision"] = cfg["collision"]
            return              # warned in settings(); origin content wins
        self._write_index(cfg)
        self._write_results_page(cfg)

    def _write_index(self, cfg: dict) -> None:
        docs = []
        empty = 0
        for url, rec in self._selected_pages():
            entry = self.docs.get(url) or {}
            title = strip_title_suffix(rec.title or "", cfg["suffix"])
            desc, text = entry.get("desc", ""), entry.get("text", "")
            if not (title or desc or text):
                empty += 1
                continue
            if not text:
                empty += 1                  # counted, still indexed
            docs.append([urlsplit(url).path or "/", title, desc, text])
        payload = {"v": SCHEMA_VERSION, "lang": cfg["lang"], "docs": docs}
        data = json.dumps(payload, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        target = self.exp.local_path_for(
            f"{self.exp.scheme}://{self.exp.host}{INDEX_PATH}", is_page=False)
        if target is None or self.exp.write_bytes(target, data) is None:
            self.exp.warnings.append(
                f"static search: {INDEX_PATH} could not be written -- the "
                f"search will not work")
            return
        self.stats["pages_indexed"] = len(docs)
        self.stats["pages_without_text"] = empty
        self.stats["index_bytes"] = len(data)
        if len(data) > 2 << 20:
            self.exp.warnings.append(
                f"static search: {INDEX_PATH} is "
                f"{len(data) // 1024} KB -- every visitor downloads it on "
                f"the first search; consider --search-max-chars")

    def _write_results_page(self, cfg: dict) -> None:
        path = cfg["path"]
        target = self.exp.local_path_for(
            f"{self.exp.scheme}://{self.exp.host}{path}", is_page=True)
        if target is None:
            return                          # already warned in _collision()
        soup, source = self._clone_skeleton(cfg)
        self._build_results_page(soup, cfg)
        data = self.exp.serialize(soup)     # minify & co. apply for free
        if self.exp.write_bytes(target, data) is None:
            msg = f"static search: {path} could not be written"
            self.exp.warnings.append(msg)
            # the forms already point here -- a missing target is exactly
            # what the verification pass exists to surface
            self.exp.verify_missing.append(
                {"file": "(static search)", "ref": path,
                 "origin_error": False})
            return
        self.stats["page_written"] = True
        self.stats["page_source"] = source
        self.exp.say(f"[seo] static search: {path} generated from {source} "
                     f"({self.stats['pages_indexed']} pages indexed)")

    def _clone_skeleton(self, cfg: dict):
        """A themed page skeleton: the exported 404 page first (full theme
        chrome, near-empty content well), else the homepage, else a minimal
        self-contained page."""
        for name, source in (("404.html", "404.html"),
                             ("index.html", "index.html (homepage)")):
            candidate = self.exp.public_dir / name
            try:
                if not candidate.is_file():
                    continue
                raw = candidate.read_bytes()
            except OSError:
                continue
            soup = BeautifulSoup(raw, "html.parser")
            if soup.find("body") is not None:
                return soup, source
        html = (MINIMAL_PAGE
                .replace("__LANG__", cfg["lang"] or "en")
                .replace("__PATH__", cfg["path"])
                .replace("__TITLE__", self._page_title(cfg))
                .replace("__H1__", "Suchergebnisse" if cfg["de"]
                         else "Search results"))
        self.exp.warnings.append(
            "static search: neither 404.html nor a homepage was available "
            "as a template -- the results page uses minimal built-in markup "
            "instead of the site theme")
        return BeautifulSoup(html, "html.parser"), "built-in minimal page"

    @staticmethod
    def _page_title(cfg: dict) -> str:
        return ("Suche" if cfg["de"] else "Search") + cfg["suffix"]

    def _build_results_page(self, soup, cfg: dict) -> None:
        path, is_de = cfg["path"], cfg["de"]
        heading = "Suchergebnisse" if is_de else "Search results"
        root = content_root(soup)

        # 1) keep the theme's own search form, then empty the content well
        form = None
        if root is not None:
            for f in root.find_all("form"):
                if f.find("input", attrs={"name": "s"}) is not None:
                    form = f.extract()
                    break
            root.clear()
            if root.has_attr("style"):
                # 404 templates often center their content; a result LIST
                # must be left-aligned
                style = re.sub(r"(?i)\btext-align\s*:[^;]*;?", "",
                               root["style"]).strip(" ;")
                if style:
                    root["style"] = style
                else:
                    del root["style"]

        # 2) body classes: WordPress marks search results, not a 404
        body = soup.find("body")
        if body is not None:
            keep = [c for c in (body.get("class") or [])
                    if c not in ("error404", "not-found", "search-no-results",
                                 "search-results", "search")]
            body["class"] = ["search", "search-results"] + keep

        # 3) the page-title <h1> (The7 & most themes put it OUTSIDE the
        #    content well, so clearing the well did not remove it)
        h1 = None
        for candidate in soup.find_all("h1"):
            if root is None or (candidate is not root
                                and root not in candidate.parents):
                h1 = candidate
                break
        if h1 is not None:
            h1.clear()
            h1.append(heading)

        # 4) the breadcrumb leaf still says "Fehler 404"
        for bc in soup.select('[itemtype*="BreadcrumbList"], ol.breadcrumbs, '
                              'ul.breadcrumbs, .breadcrumbs'):
            items = bc.find_all("li")
            if not items:
                continue
            leaf = items[-1]
            span = leaf.find("span", attrs={"itemprop": "name"}) or leaf
            span.clear()
            span.append(heading)
            break

        # 5) head: this page is not the 404 page and must not be indexed
        for script in soup.find_all("script"):
            stype = (script.get("type") or "").lower()
            if "ld+json" in stype or script.get("data-" + MARKER):
                script.decompose()          # stale graph / the ?s= redirect
        for meta in soup.find_all("meta"):
            key = (meta.get("property") or meta.get("name") or "").lower()
            if (key.startswith(("og:", "twitter:", "article:"))
                    or key == "description"):
                meta.decompose()
        for link in soup.find_all("link"):
            rel = " ".join(link.get("rel") or []).lower()
            if (rel in ("canonical", "shortlink", "next", "prev")
                    or (rel == "alternate" and link.get("hreflang"))):
                link.decompose()
        head = soup.find("head")
        robots = soup.find("meta", attrs={"name": re.compile("^robots$",
                                                             re.I)})
        if robots is not None:
            robots["content"] = "noindex,follow"
        elif head is not None:
            tag = soup.new_tag("meta")
            tag["name"] = "robots"
            tag["content"] = "noindex,follow"
            head.insert(0, tag)
        if head is not None:
            canon = soup.new_tag("link")
            canon["rel"] = "canonical"
            canon["href"] = self.exp._loc_prefix() + path
            head.append(canon)
        if soup.title is not None:
            soup.title.string = self._page_title(cfg)
        elif head is not None:
            t = soup.new_tag("title")
            t.string = self._page_title(cfg)
            head.append(t)

        # 6) the results container + the renderer
        loading = ("Suchergebnisse werden geladen \u2026" if is_de
                   else "Loading search results \u2026")
        noscript = ("F\u00fcr die Suche wird JavaScript ben\u00f6tigt."
                    if is_de else "Search requires JavaScript.")
        block = BeautifulSoup(
            f'<div class="wpse-search" id="wpse-search">'
            f'<div class="wpse-search-results" id="{RESULTS_ID}">'
            f'<p class="wpse-search-status">{loading}</p></div>'
            f'<noscript><p class="wpse-search-status">{noscript}</p>'
            f'</noscript></div>', "html.parser")
        container = block.find("div", id="wpse-search")
        if form is None and soup.find("form") is None:
            # the template had no search box anywhere (a bare origin 404
            # page, say) -- a results page you cannot search again from is
            # a dead end, so give it a minimal form of its own
            label = "Suchbegriff" if is_de else "Search term"
            form = BeautifulSoup(
                f'<form class="searchform" method="get" role="search">'
                f'<label class="screen-reader-text" for="wpse-s">{label}'
                f'</label><input id="wpse-s" name="s" type="text" value="">'
                f'<input type="submit" value="{heading}"></form>',
                "html.parser").find("form")
        if form is not None:
            form["action"] = path
            form["method"] = "get"
            for a in form.find_all("a"):
                if not (a.get("href") or "").strip():
                    a["href"] = path
            container.insert(0, form)
        if h1 is None:
            own = soup.new_tag("h1")
            own["class"] = ["entry-title"]
            own.append(heading)
            container.insert(0, own)
        script = soup.new_tag("script")
        script["data-" + MARKER] = "renderer"
        script.string = (RENDERER_JS
                         .replace("__RESULTS_ID__", RESULTS_ID)
                         .replace("__CFG__", js_literal({
                             "de": bool(is_de), "idx": INDEX_PATH,
                             "max": 50, "snip": 180, "sfx": cfg["suffix"]})))
        host = root if root is not None else soup.find("body")
        if host is None:                    # pathological markup
            host = soup
        host.append(block)
        host.append(script)

    # -- reporting -----------------------------------------------------------
    def summary_lines(self) -> list[str]:
        if not self.enabled:
            return ["[note] site search NOT exported "
                    + ("(--no-search)" if self.exp.cfg.rewrite
                       else "(--no-rewrite)")
                    + " -- the theme's search box stays broken statically"]
        if not self.stats["page_written"]:
            return ["[warn] site search: results page NOT generated "
                    "-- see report.txt"]
        return [f"[seo] site search: {self.stats['path']} "
                f"({self.stats['pages_indexed']} pages indexed, "
                f"{self.stats['index_bytes'] // 1024 or 1} KB index, "
                f"{self.stats['forms_rewritten']} forms rewritten)"]

    def add_report(self, report: dict, txt_head: list,
                   txt_sections: list) -> None:
        report["seo"]["search"] = self.stats
        if self.enabled and self.stats["page_written"]:
            txt_head.append(
                f"Site search:     {self.stats['path']} "
                f"({self.stats['pages_indexed']} pages, "
                f"{self.stats['index_bytes'] // 1024 or 1} KB index, "
                f"lang {self.stats['ui_language']})")
        else:
            txt_head.append("Site search:     not exported")


PLUGIN = Search
