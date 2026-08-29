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
   overrides). Its DESIGN IS HARVESTED FROM THE LIVE SITE: a handful of
   `GET /?s=<term>` probes (at most PROBE_MAX_REQUESTS, all inside
   run_end) give us the theme's own results skeleton, its result-card
   markup -- turned into a template with `%%X%%` slots -- the per-page
   meta WordPress renders (author, date, excerpt) and the theme's own
   "nothing found" block. The static page ships the theme's own
   container and, whenever the index fits, renders into it while the
   document is still parsing, so the theme's grid/masonry code
   initializes over real cards exactly as it does live. When the
   origin's search cannot be harvested (disabled, dead,
   --no-search-harvest) the page falls back to cloning the exported
   404.html, then the homepage, then minimal built-in markup. It is
   `noindex,follow`ed, exactly like WordPress noindexes search results.

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
import copy
import json
import random
import re
import string
import unicodedata
from urllib.parse import parse_qs, quote, urljoin, urlsplit, urlunsplit

import requests
from bs4 import (BeautifulSoup, CData, Comment, Declaration, Doctype,
                 NavigableString, ProcessingInstruction, Tag)

import wp_static_export as core          # registries are rebound at load
from wp_static_export import Plugin, canon_path

INDEX_PATH = "/search-index.json"
SCHEMA_VERSION = 2                       # docs gained a 5th slot element
RESULTS_ID = "wpse-search-results"
MARKER = "wpse-search"                  # data attribute on our injected tags

# -- live-design harvesting --------------------------------------------------
PROBE_MAX_REQUESTS = 12      # HARD ceiling on live /?s= requests per export
PROBE_MAX_PAGES = 8          # paginator follow depth per term (loop guard)
PROBE_MAX_ITEMS = 200        # sanity cap on cards taken from one response
# index size up to which the docs are inlined into the results page: doing
# so makes the renderer run BEFORE DOMContentLoaded, so the theme's own
# grid/masonry code initializes over real cards (an XHR always loses that
# race and leaves the cards unpositioned -- see relayout())
INLINE_MAX_BYTES = 256 * 1024
# words usable as probe terms: letters only, no digits/underscore
WORD_RE = re.compile(r"[^\W\d_]{2,32}", re.UNICODE)
# pagination blocks to drop from the harvested content well (second net;
# the primary rule is "contains a link that still carries ?s=")
PAGER_CLASS_RE = re.compile(
    r"(?:paginator|pagination|page-numbers|nav-links|page-links"
    r"|posts-nav|post-nav|paging[\w-]*)")

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


# -- probe-term selection (pure) ---------------------------------------------

def fold_word(word: str) -> str:
    """Lowercase, NFD-stripped, ss-folded -- the same normalization the
    renderer's fold() applies, so probe terms and matching agree."""
    w = unicodedata.normalize("NFD", (word or "").lower())
    w = "".join(c for c in w if not unicodedata.combining(c))
    return w.replace("\u00df", "ss")


def doc_words(text: str) -> set:
    """Folded word set of one indexed document."""
    return {fold_word(m.group(0)) for m in WORD_RE.finditer(text or "")}


def word_forms(text: str) -> dict:
    """folded token -> one original spelling from the text. Frequency
    counting folds ('Straße' and 'Strasse' are one term), but the PROBE
    must go out in the spelling the site actually contains."""
    out: dict = {}
    for m in WORD_RE.finditer(text or ""):
        out.setdefault(fold_word(m.group(0)), m.group(0))
    return out


def rare_term(words_per_doc: list, titles: list) -> str:
    """A term the live search will answer with a SMALL, unambiguous result
    set: the longest ASCII word of >= 8 characters that occurs in exactly
    one indexed document. Rare enough that its echo in the results page
    ("<title>Du hast nach X gesucht") can be split off without guessing,
    and taken from our own corpus, so the origin really does find it."""
    freq: dict = {}
    for ws in words_per_doc:
        for w in ws:
            freq[w] = freq.get(w, 0) + 1
    unique = [w for w, n in freq.items() if n == 1]
    for minlen, ascii_only in ((8, True), (8, False), (6, False)):
        cands = [w for w in unique
                 if len(w) >= minlen and (w.isascii() or not ascii_only)]
        if cands:
            return max(sorted(cands), key=len)
    # no usable body text (--search-max-chars 0, image-only site): the
    # longest word of a page TITLE still identifies a real page
    tw = sorted({w for t in titles for w in doc_words(t) if len(w) >= 6})
    return max(tw, key=len) if tw else ""


def broad_term(texts_by_path: dict, uncovered: set, used: set) -> str:
    """Greedy set cover: the candidate term that appears in the most still
    uncovered documents. Single characters first (a WordPress LIKE search
    on one letter matches nearly every page -- maximum coverage per
    request), then whole words. Language-agnostic: the candidates come
    from the corpus, not from a hardcoded list."""
    if not uncovered:
        return ""
    chars: dict = {}
    words: dict = {}
    for path in uncovered:
        raw = texts_by_path.get(path) or ""
        # characters from the ORIGINAL text: folding 'ä' to 'a' could
        # hand us a letter the page does not literally contain
        for c in {c for c in raw.lower() if c.isalpha()}:
            chars[c] = chars.get(c, 0) + 1
        for w in {w for w in doc_words(raw) if 4 <= len(w) <= 24}:
            words[w] = words.get(w, 0) + 1
    for pool in (chars, words):
        best, best_n = "", 0
        for term in sorted(pool):
            if term not in used and pool[term] > best_n:
                best, best_n = term, pool[term]
        if best:                       # single letters beat words outright
            return best
    return ""


def split_echo(text: str, term: str) -> tuple | None:
    """(prefix, suffix) around the LAST occurrence of `term`, or None.
    The echo is always the tail of these strings ('Ergebnisse für "te"',
    'Du hast nach te gesucht - Site'), so rfind is exact -- verified
    against the live markup for 1-, 2- and 21-character terms."""
    if not term or not text:
        return None
    i = text.rfind(term)
    if i < 0:
        return None
    return text[:i], text[i + len(term):]


def _set_text(el, text: str) -> None:
    el.clear()
    el.append(NavigableString(text))


def _wrap(el, before: str, after: str) -> None:
    """Bracket an optional element with the renderer's cut markers."""
    el.insert_before(NavigableString(before))
    el.insert_after(NavigableString(after))


def _rel_set(tag) -> set:
    """bs4 gives `rel` as a LIST on parsed markup but as a plain STRING on
    tags we build ourselves -- iterating the latter would walk single
    characters."""
    rel = tag.get("rel") or []
    if isinstance(rel, str):
        rel = rel.split()
    return {str(r).lower() for r in rel}


def _href_path(a) -> str:
    """Root-relative path of an <a> in ALREADY LOCALIZED markup ('' when
    the href is external, absolute or empty)."""
    href = (a.get("href") or "").split("#")[0].split("?")[0]
    if not href.startswith("/") or href.startswith("//"):
        return ""
    return canon_path(href)


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
    z: "Keine Ergebnisse f\u00fcr \u201e%s\u201c.",
    e: "Bitte einen Suchbegriff eingeben.",
    f: "Der Suchindex konnte nicht geladen werden.",
    t: "Suchergebnisse f\u00fcr \u201e%s\u201c"
  } : {
    z: "No results for \u201c%s\u201d.",
    e: "Please enter a search term.",
    f: "The search index could not be loaded.",
    t: "Search results for \u201c%s\u201d"
  };
  var MARKS = /[\u0300-\u036f]/g;
  var WORDCH = /[0-9a-z\u00c0-\u024f\u0370-\u1fff\u3040-\uffff]/;
  var SPLIT = /[^0-9A-Za-z\u00c0-\u024f\u0370-\u1fff\u3040-\uffff]+/;
  var TOK = /%%([A-Z])%%/g;

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
  function clip(s, n) {                  // WP-style auto excerpt
    s = String(s);
    if (s.length <= n) { return s; }
    var t = s.slice(0, n), i = t.lastIndexOf(" ");
    if (i > n * 0.6) { t = t.slice(0, i); }
    return t + "\u2026";
  }
  function cut(t, a, b, keep) {          // keep/drop an optional block
    var i = t.indexOf(a), j = t.indexOf(b);
    if (i < 0 || j < 0 || j < i) { return t; }
    return keep ? t.slice(0, i) + t.slice(i + a.length, j) + t.slice(j + b.length)
                : t.slice(0, i) + t.slice(j + b.length);
  }
  function card(d) {                     // harvested theme card
    var s = d[4] || {}, t = C.tpl;
    var body = s.e || clip(d[3] || d[2] || "", C.snip);
    t = cut(t, "%%QB%%", "%%QE%%", !!s.m);
    t = cut(t, "%%AB%%", "%%AE%%", !!s.a);
    t = cut(t, "%%TB%%", "%%TE%%", !!s.d);
    t = cut(t, "%%MB%%", "%%ME%%", !!(s.a || s.d));
    t = cut(t, "%%XB%%", "%%XE%%", !!body);
    var v = { U: esc(d[0]), T: esc(s.t || d[1] || d[0]), X: esc(body),
              D: esc(s.d || ""), I: esc(s.i || ""), A: esc(s.a || ""),
              P: esc(s.p || ""), C: esc(s.c || C.cls || ""), M: s.m || "" };
    return t.replace(TOK, function (mm, k) {
      return v[k] === undefined ? "" : v[k];
    });
  }
  function plain(d) {                    // no harvest: built-in markup
    var body = clip(d[3] || d[2] || "", C.snip);
    return '<article class="wpse-result post">'
      + '<h2 class="entry-title"><a href="' + esc(d[0]) + '">'
      + esc(d[1] || d[0]) + "</a></h2>"
      + (body ? '<div class="entry-content"><p>' + esc(body) + "</p></div>" : "")
      + "</article>";
  }
  function box() { return document.getElementById("__RESULTS_ID__"); }
  function swap(html) {
    // REPLACES the container: a message left inside a grid container the
    // theme sized to height:0 would be invisible
    var el = box();
    if (el) { el.outerHTML = html; }
    var b = document.body;
    if (b && b.className) {
      b.className = b.className.replace(/\bsearch-results\b/,
                                        "search-no-results");
    }
  }
  function message(text) {
    swap('<p class="wpse-search-status">' + text + "</p>");
  }
  function relayout(el) {
    // Only needed on the XHR path: with an inlined index this code runs
    // while the document is still parsing, so the theme's own DOM-ready
    // grid init sees the finished cards and nothing has to be redone.
    if (document.readyState === "loading") { return; }
    var $ = window.jQuery;
    try {
      if ($ && $.fn && $.fn.isotope && $.data(el, "isotope")) {
        $(el).isotope("reloadItems").isotope("layout");
        return;
      }
    } catch (e) { /* no isotope grid on this theme */ }
    try {
      var ev;
      if (typeof window.Event === "function") { ev = new window.Event("resize"); }
      else {
        ev = document.createEvent("Event");
        ev.initEvent("resize", true, false);
      }
      window.dispatchEvent(ev);          // debounced-resize grid handlers
    } catch (e2) { /* ancient browser: nothing more we can do */ }
  }
  function run(docs, q) {
    var ts = terms(q), hits = [], i, s;
    for (i = 0; i < docs.length; i++) {
      s = ts.length ? score(docs[i], ts) : 0;
      if (s > 0) { hits.push([s, docs[i]]); }
    }
    hits.sort(function (x, y) {
      return y[0] - x[0] || String(x[1][1]).localeCompare(String(y[1][1]));
    });
    if (!hits.length) {
      if (C.empty) { swap(C.empty); } else { message(put(T.z, "%s", esc(q))); }
      return;
    }
    var html = "", n = hits.length < C.max ? hits.length : C.max;
    for (i = 0; i < n; i++) {
      html += C.tpl ? card(hits[i][1]) : plain(hits[i][1]);
    }
    var el = box();
    if (!el) { return; }
    el.innerHTML = html;
    relayout(el);
  }
  function echo(q) {                     // page title band + breadcrumb
    var n = document.querySelectorAll("[data-__MARKER__=term]"), i;
    for (i = 0; i < n.length; i++) {
      n[i].innerHTML = "";
      n[i].appendChild(document.createTextNode(q));
    }
    document.title = C.tt ? (C.tt[0] + q + C.tt[1])
                          : (put(T.t, "%s", q) + C.sfx);
  }
  function prefill(q) {
    var ins = document.querySelectorAll("input[name=s],input[name=q]"), i;
    for (i = 0; i < ins.length; i++) { ins[i].value = q; }
  }
  function render() {
    var q = param("s") || param("q");
    echo(q);
    if (!q) { message(T.e); return; }
    if (C.docs) { run(C.docs, q); return; }
    var x = new XMLHttpRequest();
    x.open("GET", C.idx, true);
    x.onreadystatechange = function () {
      if (x.readyState !== 4) { return; }
      var data = null;
      if (x.status >= 200 && x.status < 300) {
        try { data = JSON.parse(x.responseText); } catch (e) { data = null; }
      }
      if (data && data.docs) { run(data.docs, q); } else { message(T.f); }
    };
    x.send();
  }
  render();                              // synchronous: before DOM-ready
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      prefill(param("s") || param("q"));  // footer forms exist only now
    });
  } else { prefill(param("s") || param("q")); }
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
                     "search_max_chars": 2000, "search_harvest": True}

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
        group.add_argument(
            "--no-search-harvest", dest="search_harvest",
            action="store_false",
            help="do not probe the live '/?s=' endpoint for the results-"
                 f"page design (default: on, at most {PROBE_MAX_REQUESTS} "
                 "extra GET requests). The results page is then built from "
                 "the exported 404 page with built-in markup")

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
        cfg.search_harvest = args.search_harvest

    # -- per-run state -------------------------------------------------------
    def __init__(self, exporter):
        super().__init__(exporter)
        # page_url -> {"desc", "text", "lang", "site"}; filled from crawl
        # worker threads under exp.stats_lock
        self.docs: dict = {}
        self._settings = None
        self._probe_budget = PROBE_MAX_REQUESTS
        self._assets: list = []          # absolute asset URLs of our page
        self._path_hint = "/"            # base for resolving harvested refs
        self.stats = {
            "enabled": True, "path": "", "lang": "", "ui_language": "",
            "index_path": INDEX_PATH, "pages_indexed": 0,
            "pages_without_text": 0, "index_bytes": 0, "index_inlined": False,
            "page_written": False, "page_source": "", "forms_rewritten": 0,
            "pages_wired": 0, "jsonld_search_actions_fixed": 0,
            "collision": None,
            "harvest": {"used": False, "reason": "", "probe_requests": 0,
                        "terms": [], "cards": 0, "pages_with_slots": 0,
                        "template": False, "empty_state": False,
                        "title_pattern": False, "assets_downloaded": 0},
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
            return              # warned in settings(); NO probing either
        self._path_hint = cfg["path"]
        # 1) harvest the live design (the ONLY place probes happen, so
        #    --no-rewrite / --no-search / a collision cost zero requests)
        harvest = None
        if self.exp.cfg.search_harvest:
            harvest = self._harvest(cfg)
            if harvest is None and not self.stats["harvest"]["reason"]:
                self.stats["harvest"]["reason"] = "no usable probe response"
        else:
            self.stats["harvest"]["reason"] = "--no-search-harvest"
        # 2) index (harvested card slots ride along in the docs)
        docs, data = self._build_index(cfg, harvest)
        self._write_index(data)
        # 3) results page, 4) the assets only that page references
        self._write_results_page(cfg, harvest, docs, len(data))
        self._download_assets()

    def _build_index(self, cfg: dict, harvest) -> tuple:
        docs = []
        empty = 0
        slots = (harvest or {}).get("slots") or {}
        for url, rec in self._selected_pages():
            entry = self.docs.get(url) or {}
            path = urlsplit(url).path or "/"
            title = strip_title_suffix(rec.title or "", cfg["suffix"])
            desc, text = entry.get("desc", ""), entry.get("text", "")
            if not (title or desc or text):
                empty += 1
                continue
            if not text:
                empty += 1                  # counted, still indexed
            doc = [path, title, desc, text]
            slot = slots.get(path)
            if slot:
                doc.append(slot)            # docs without slots stay 4 long
            docs.append(doc)
        payload = {"v": SCHEMA_VERSION, "lang": cfg["lang"], "docs": docs}
        data = json.dumps(payload, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        self.stats["pages_indexed"] = len(docs)
        self.stats["pages_without_text"] = empty
        self.stats["index_bytes"] = len(data)
        return docs, data

    def _write_index(self, data: bytes) -> None:
        target = self.exp.local_path_for(
            f"{self.exp.scheme}://{self.exp.host}{INDEX_PATH}", is_page=False)
        if target is None or self.exp.write_bytes(target, data) is None:
            self.exp.warnings.append(
                f"static search: {INDEX_PATH} could not be written -- the "
                f"search will not work")
            return
        if len(data) > 2 << 20:
            self.exp.warnings.append(
                f"static search: {INDEX_PATH} is "
                f"{len(data) // 1024} KB -- every visitor downloads it on "
                f"the first search; consider --search-max-chars")

    def _write_results_page(self, cfg: dict, harvest, docs,
                            index_bytes: int) -> None:
        path = cfg["path"]
        target = self.exp.local_path_for(
            f"{self.exp.scheme}://{self.exp.host}{path}", is_page=True)
        if target is None:
            return                          # already warned in _collision()
        if harvest is not None:
            soup, source = harvest["soup"], "live search page"
            self._build_from_harvest(soup, cfg, harvest)
        else:                               # 404.html -> homepage -> minimal
            soup, source = self._clone_skeleton(cfg)
            self._build_results_page(soup, cfg)
        self._inject_renderer(soup, cfg, harvest, docs, index_bytes)
        self._assets.extend(self._scan_assets(soup, path))
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
        h = self.stats["harvest"]
        extra = (f", {h['cards']} cards harvested in "
                 f"{h['probe_requests']} requests" if h["used"] else "")
        self.exp.say(f"[seo] static search: {path} generated from {source} "
                     f"({self.stats['pages_indexed']} pages indexed{extra})")

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

        # 5) head hygiene (shared with the harvested builder)
        self._clean_head(soup, cfg, path)

        # 6) the results container
        loading = ("Suchergebnisse werden geladen \u2026" if is_de
                   else "Loading search results \u2026")
        block = BeautifulSoup(
            f'<div class="wpse-search" id="wpse-search">'
            f'<div class="wpse-search-results" id="{RESULTS_ID}">'
            f'<p class="wpse-search-status">{loading}</p></div></div>',
            "html.parser")
        container = block.find("div", id="wpse-search")
        if form is None and soup.find("form") is None:
            # the template had no search box anywhere (a bare origin 404
            # page, say) -- a results page you cannot search again from is
            # a dead end, so give it a minimal form of its own
            form = self._own_form(cfg)
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
        host = root if root is not None else soup.find("body")
        if host is None:                    # pathological markup
            host = soup
        host.append(block)

    def _own_form(self, cfg: dict):
        label = "Suchbegriff" if cfg["de"] else "Search term"
        heading = "Suchergebnisse" if cfg["de"] else "Search results"
        return BeautifulSoup(
            f'<form class="searchform" method="get" role="search" '
            f'action="{cfg["path"]}">'
            f'<label class="screen-reader-text" for="wpse-s">{label}'
            f'</label><input id="wpse-s" name="s" type="text" value="">'
            f'<input type="submit" value="{heading}"></form>',
            "html.parser").find("form")

    def _clean_head(self, soup, cfg: dict, path: str) -> None:
        """This page is not the page it was cloned from, and search
        results must never be indexed."""
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
            rel = _rel_set(link)
            ltype = (link.get("type") or "").lower()
            if (rel & {"canonical", "shortlink", "next", "prev"}
                    or ("alternate" in rel
                        and (link.get("hreflang") or "xml" in ltype
                             or "json" in ltype))):
                link.decompose()            # + the search-result RSS feed
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

    def _inject_renderer(self, soup, cfg, harvest, docs,
                         index_bytes: int) -> None:
        h = harvest or {}
        snips = [len(s["e"]) for s in (h.get("slots") or {}).values()
                 if s.get("e")]
        inline = index_bytes <= INLINE_MAX_BYTES
        self.stats["index_inlined"] = inline
        conf = {"de": bool(cfg["de"]), "idx": INDEX_PATH, "max": 50,
                "snip": max(180, min(600, max(snips))) if snips else 300,
                "sfx": cfg["suffix"], "tpl": h.get("tpl") or "",
                "cls": h.get("cls") or "", "empty": h.get("empty") or "",
                "tt": list(h["title"]) if h.get("title") else None}
        if inline:
            # the whole point: rendering then happens while the document
            # is still parsing, so the theme's own footer scripts
            # initialize their grid over finished cards
            conf["docs"] = docs
        script = soup.new_tag("script")
        script["data-" + MARKER] = "renderer"
        script.string = (RENDERER_JS
                         .replace("__RESULTS_ID__", RESULTS_ID)
                         .replace("__MARKER__", MARKER)
                         .replace("__CFG__", js_literal(conf)))
        host = (content_root(soup) or soup.find("body") or soup)
        host.append(script)
        noscript = soup.new_tag("noscript")
        np = soup.new_tag("p")
        np["class"] = ["wpse-search-status"]
        np.append("F\u00fcr die Suche wird JavaScript ben\u00f6tigt."
                  if cfg["de"] else "Search requires JavaScript.")
        noscript.append(np)
        script.insert_before(noscript)

    # -- live-design harvest -------------------------------------------------

    def _probe(self, url: str):
        """One GET against the live search. Budgeted, never raises: any
        failure just means one rung less of fidelity."""
        exp = self.exp
        if self._probe_budget <= 0:
            return None
        self._probe_budget -= 1
        self.stats["harvest"]["probe_requests"] += 1
        try:
            resp = exp.fetch(url)
        except requests.RequestException as exc:
            self._harvest_stop(f"probe failed ({exc.__class__.__name__})")
            return None
        if exp.off_site_redirect(resp):
            self._harvest_stop("the search endpoint redirects off-site")
            return None
        if not resp.ok:
            self._harvest_stop(f"the search endpoint answered "
                               f"HTTP {resp.status_code}")
            return None
        if "html" not in (resp.headers.get("Content-Type") or "").lower():
            self._harvest_stop("the search endpoint does not answer HTML")
            return None
        return BeautifulSoup(resp.content, "html.parser")

    def _harvest_stop(self, reason: str) -> None:
        h = self.stats["harvest"]
        if not h["reason"]:
            h["reason"] = reason
            self.exp.warnings.append(
                f"static search: could not harvest the live results-page "
                f"design ({reason}) -- the results page uses the exported "
                f"404 page and built-in markup instead")

    def _search_url(self, term: str) -> str:
        return (f"{self.exp.scheme}://{self.exp.host}"
                f"/?s={quote(term, safe='')}")

    @staticmethod
    def _echoes(soup, term: str) -> bool:
        """A real results page repeats the query. A site that answers
        /?s=xyz with the homepage (soft 404) does not -- and harvesting
        the homepage as a 'results page' is exactly the failure mode this
        guard exists for."""
        t = soup.find("title")
        blob = (t.get_text() if t else "")
        for sel in ("h1", '[itemprop="name"]', ".breadcrumbs"):
            for el in soup.select(sel):
                blob += " " + el.get_text()
        return term.lower() in blob.lower()

    def _next_probe_url(self, soup, term: str, seen: set):
        """The theme's OWN 'next page' link -- no /page/N/ guessing, so
        every permalink shape (?paged=2 included) works."""
        exp = self.exp
        base = f"{exp.scheme}://{exp.host}/"
        best = None
        for a in soup.find_all("a", href=True):
            href = urljoin(base, a["href"].replace("&amp;", "&"))
            if not exp.is_internal(href) or href in seen:
                continue
            if parse_qs(urlsplit(href).query).get("s") != [term]:
                continue
            rel = {r.lower() for r in (a.get("rel") or [])}
            cls = " ".join(a.get("class") or []).lower()
            rank = 2 if ("next" in rel or "next" in cls) else 1
            if best is None or rank > best[0]:
                best = (rank, href)
        return best[1] if best else None

    def _prepare_soup(self, soup) -> None:
        """Exactly what parse_and_save_html does to a crawled page:
        resource hints + plugin clean_soup, localization, plugin
        rewrite_soup. Without this our page would be the only one in the
        export still carrying the Wordfence beacon and the CDN cruft."""
        exp = self.exp
        if exp.cfg.strip_wp_cruft:
            exp.strip_resource_hints(soup)
            for p in exp.plugins:
                if p is not self:
                    p.clean_soup(soup)
        exp.rewrite_soup_relative(soup)
        for p in exp.plugins:
            if p is not self:
                p.rewrite_soup(soup)

    @staticmethod
    def _find_results(root, paths: set, sig=None):
        """(container, items) of the theme's result loop.

        With a known item signature: match it directly. Without one: the
        DEEPEST element at least TWO of whose direct children contain a
        link to an indexed page -- theme-agnostic. A one-result response
        is genuinely ambiguous (the whole ancestor chain scores 1), which
        is why the signature is learned from a broad probe and applied to
        the held-back responses afterwards."""
        if sig is not None:
            name, classes = sig
            found = [t for t in root.find_all(name)
                     if classes <= frozenset(t.get("class") or ())]
            ids = {id(t) for t in found}
            items = [t for t in found
                     if not any(id(p) in ids for p in t.parents)]
            if items and items[0].parent is not None:
                return items[0].parent, items[:PROBE_MAX_ITEMS]
            return None, []
        hits = [a for a in root.find_all("a", href=True)
                if _href_path(a) in paths]
        anc_ids: set = set()
        for a in hits:
            node = a
            while node is not None and node is not root:
                anc_ids.add(id(node))
                node = node.parent
        best = None
        for anc in root.find_all(True):
            kids = [c for c in anc.find_all(True, recursive=False)
                    if id(c) in anc_ids]
            if len(kids) < 2:
                continue
            depth = len(list(anc.parents))
            if best is None or depth > best[0]:
                best = (depth, anc, kids)
        if best is None:
            return None, []
        return best[1], best[2][:PROBE_MAX_ITEMS]

    def _take_cards(self, h: dict, items: list, paths: set) -> int:
        """Per-card slot values; builds the ONE template from the first
        usable card. Returns how many NEW pages got slots."""
        gained = 0
        for item in items:
            link = next((a for a in item.find_all("a", href=True)
                         if _href_path(a) in paths), None)
            if link is None:
                continue
            path = _href_path(link)
            if path in h["slots"]:
                continue
            h["slots"][path] = self._slot_values(item, link)
            gained += 1
            if not h["tpl"]:
                h["tpl"], h["cls"] = self._build_template(item, link)
        return gained

    @staticmethod
    def _media_block(item, link):
        """Highest ancestor of the card's <img> that does NOT contain the
        title link -- the thumbnail block, whatever the theme calls it."""
        img = item.find("img")
        if img is None:
            return None
        node = img
        while (node.parent is not None and node.parent is not item
               and link not in node.parent.descendants):
            node = node.parent
        return node

    @staticmethod
    def _meta_block(item, link, time_el, author_el):
        node = time_el if time_el is not None else author_el
        if node is None:
            return None
        while (node.parent is not None and node.parent is not item
               and link not in node.parent.descendants):
            node = node.parent
        return node

    def _slot_values(self, item, link) -> dict:
        """Generic, NOT tied to dt-the7 class names: title = the <a> whose
        localized href is an indexed path, excerpt = the last <p>, date =
        <time datetime>, author = .author/.vcard/[rel=author]/.fn."""
        d = {"t": link.get_text(" ", strip=True)}
        tm = item.find("time")
        if tm is not None:
            iso = (tm.get("datetime") or "").strip()
            if iso:
                d["i"] = iso
            txt = tm.get_text(" ", strip=True)
            if txt:
                d["d"] = txt
        au = item.select_one(".author, .vcard, [rel=author], .fn")
        if au is not None:
            name = (au.select_one(".fn") or au).get_text(" ", strip=True)
            if name:
                d["a"] = name
        ps = item.find_all("p")
        if ps:
            # PLAIN TEXT, escaped at render time: an excerpt is prose, and
            # shipping origin HTML through innerHTML buys nothing visible
            txt = ps[-1].get_text(" ", strip=True)
            if txt:
                d["e"] = txt
        pid = (item.get("data-post-id") or "").strip()
        if pid:
            d["p"] = pid
        art = item.find("article") or item
        cls = " ".join(art.get("class") or ())
        if cls:
            d["c"] = cls
        media = self._media_block(item, link)
        if media is not None:
            self._drop_dead_hrefs(media)
            self._assets.extend(self._scan_assets(media, self._path_hint))
            d["m"] = str(media)
        return d

    def _build_template(self, item, link) -> tuple:
        """The card with every variable value replaced by a %%X%% token
        and every optional element bracketed by a %%?B%%/%%?E%% pair the
        renderer can cut out. Built from a DEEP COPY: the live item stays
        intact for its own slot extraction."""
        href = link.get("href")
        tpl = copy.copy(item)
        link = next(a for a in tpl.find_all("a", href=True)
                    if a.get("href") == href)
        for attr, tok in (("data-post-id", "%%P%%"), ("data-date", "%%I%%"),
                          ("data-name", "%%T%%")):
            if tpl.has_attr(attr):
                tpl[attr] = tok
        art = tpl.find("article") or tpl
        cls = " ".join(art.get("class") or ())
        if cls:
            art["class"] = "%%C%%"
        link["href"] = "%%U%%"
        if link.has_attr("title"):
            link["title"] = "%%T%%"
        _set_text(link, "%%T%%")
        tm = tpl.find("time")
        if tm is not None:
            if tm.has_attr("datetime"):
                tm["datetime"] = "%%I%%"
            _set_text(tm, "%%D%%")
        au = tpl.select_one(".author, .vcard, [rel=author], .fn")
        if au is not None:
            _set_text(au.select_one(".fn") or au, "%%A%%")
        ps = tpl.find_all("p")
        ex = ps[-1] if ps else None
        if ex is not None:
            _set_text(ex, "%%X%%")
        media = self._media_block(tpl, link)
        mb = self._meta_block(tpl, link, tm, au)
        self._drop_dead_hrefs(tpl)          # /author/admin/ & co.
        if ex is not None:
            _wrap(ex, "%%XB%%", "%%XE%%")
        tanchor = (tm.parent if (tm is not None and tm.parent is not mb
                                 and tm.parent is not tpl) else tm)
        if tanchor is not None and tanchor is not mb:
            if tanchor.has_attr("title"):
                del tanchor["title"]        # per-post tooltip ("11:05")
            _wrap(tanchor, "%%TB%%", "%%TE%%")
        if au is not None and au is not mb:
            _wrap(au, "%%AB%%", "%%AE%%")
        if mb is not None:
            _wrap(mb, "%%MB%%", "%%ME%%")
        if media is not None:
            media.replace_with(NavigableString("%%QB%%%%M%%%%QE%%"))
        generic = " ".join(c for c in cls.split()
                           if not re.fullmatch(r"post-\d+", c))
        return str(tpl), generic

    def _drop_dead_hrefs(self, node) -> None:
        """Every root-relative href in HARVESTED markup that does not
        resolve to an exported file loses its href -- the tag, its classes
        and its text stay, so the card looks identical, but verify_export
        never sees a dead reference. Author archives (/author/admin/) are
        the normal case; a site that DOES export them keeps a live link."""
        exp = self.exp
        for a in node.find_all("a", href=True):
            href = a["href"].split("#")[0].split("?")[0]
            if not href.startswith("/") or href.startswith("//"):
                continue
            target = exp.local_path_for(
                f"{exp.scheme}://{exp.host}{href}", is_page=True)
            try:
                ok = target is not None and (
                    target.is_file()
                    or (target.parent / "index.html").is_file())
            except OSError:
                ok = False
            if not ok:
                del a["href"]

    def _title_pattern(self, soup, term: str):
        t = soup.find("title")
        return split_echo(t.get_text() if t else "", term)

    def _take_empty(self, soup, cfg: dict, token: str) -> str:
        """The theme's own 'nothing found' block, query echoes removed."""
        self._prepare_soup(soup)
        root = content_root(soup)
        if root is None or not root.find(True):
            return ""
        self._scrub_query(root, token)
        for form in root.find_all("form"):
            form["action"] = cfg["path"]
            form["method"] = "get"
            for a in form.find_all("a"):
                if not (a.get("href") or "").strip():
                    a["href"] = cfg["path"]
        for inp in root.find_all("input"):
            if (inp.get("name") or "") in ("s", "q"):
                inp["value"] = ""
        self._drop_dead_hrefs(root)
        self._assets.extend(self._scan_assets(root, cfg["path"]))
        return "".join(str(c) for c in root.contents).strip()

    def _harvest(self, cfg: dict) -> dict | None:
        """Skeleton + card template + per-page slots + empty state, taken
        from the live search endpoint. None when nothing usable came back
        (every rung of the caller's ladder still works)."""
        indexed = self._selected_pages()
        paths, texts = {}, {}
        for url, rec in indexed:
            p = urlsplit(url).path or "/"
            entry = self.docs.get(url) or {}
            paths[p] = rec
            texts[p] = " ".join(x for x in (rec.title or "",
                                            entry.get("desc") or "",
                                            entry.get("text") or "") if x)
        if not paths:
            self._harvest_stop("no indexable pages")
            return None
        h = {"soup": None, "term": "", "sig": None, "container": None,
             "tpl": "", "cls": "", "slots": {}, "empty": "", "title": None}
        pending = []                     # responses seen before the sig
        used_terms: set = set()
        forms: dict = {}                 # folded token -> original spelling
        for text in texts.values():
            for folded, original in word_forms(text).items():
                forms.setdefault(folded, original)

        def consume(soup, term):
            """Localize, then take container + cards out of one response."""
            self._prepare_soup(soup)
            root = content_root(soup) or soup.body
            if root is None:
                return False
            container, items = self._find_results(root, set(paths), h["sig"])
            if container is None:
                return False
            if h["sig"] is None:
                h["sig"] = (items[0].name,
                            frozenset(items[0].get("class") or ()))
            gained = self._take_cards(h, items, set(paths))
            if h["soup"] is None:
                h["soup"], h["term"], h["container"] = soup, term, container
            return gained

        # 1) rare probe. It is also the GATE: only a term this
        #    distinctive proves the endpoint is a real search. A site that
        #    answers /?s=anything with the homepage would echo a
        #    one-letter broad term by pure chance, so the cheap guard has
        #    to run against the rare term or not at all.
        rare = rare_term([doc_words(texts[p]) for p in paths],
                         [rec.title or "" for rec in paths.values()])
        if not rare:
            self._harvest_stop("no distinctive term to probe with")
            return None
        used_terms.add(rare)
        rare_probe = forms.get(rare, rare)
        soup = self._probe(self._search_url(rare_probe))
        if soup is None:
            return None                      # _probe already explained why
        if not self._echoes(soup, rare_probe):
            self._harvest_stop("the search endpoint does not echo the query "
                               "(no WordPress search behind it?)")
            return None
        h["title"] = self._title_pattern(soup, rare_probe)
        if not consume(soup, rare_probe):
            pending.append((soup, rare_probe))   # sig not known yet
        # 2) broad probes -- greedy cover, paginator-driven. Two terms in
        #    a row that add nothing mean the live search simply does not
        #    return the remaining pages (attachments, excluded post types)
        #    -- spending the rest of the budget on that is pure waste.
        fruitless = 0
        while (self._probe_budget > 1 and len(h["slots"]) < len(paths)
               and fruitless < 2):
            uncovered = set(paths) - set(h["slots"])
            folded = broad_term(texts, uncovered, used_terms)
            if not folded:
                break
            used_terms.add(folded)
            term = forms.get(folded, folded)
            before = len(h["slots"])
            url, seen, hops = self._search_url(term), set(), 0
            while url and hops < PROBE_MAX_PAGES and self._probe_budget > 1:
                seen.add(url)
                hops += 1
                soup = self._probe(url)
                if soup is None or not self._echoes(soup, term):
                    break
                nxt = self._next_probe_url(soup, term, seen)
                gained = consume(soup, term)
                for held, t in pending:              # sig may exist now
                    consume(held, t)
                pending = []
                if not gained:
                    break                            # nothing new here
                url = nxt
            fruitless = 0 if len(h["slots"]) > before else fruitless + 1
        if h["soup"] is None:
            self._harvest_stop("no results container found")
            return None
        # 3) empty-state probe (same token idiom as capture_404)
        token = "".join(random.choices(string.ascii_lowercase + string.digits,
                                       k=18))
        soup = self._probe(self._search_url(token))
        if soup is not None and self._echoes(soup, token):
            h["empty"] = self._take_empty(soup, cfg, token)
        st = self.stats["harvest"]
        st.update(used=True, terms=sorted(used_terms), cards=len(h["slots"]),
                  pages_with_slots=len(h["slots"]), template=bool(h["tpl"]),
                  empty_state=bool(h["empty"]),
                  title_pattern=h["title"] is not None)
        return h

    # -- assembling the harvested page ---------------------------------------

    def _build_from_harvest(self, soup, cfg: dict, h: dict) -> None:
        path, term = cfg["path"], h["term"]
        root = content_root(soup) or soup.body
        container = h["container"]

        # 1) content well: keep ONLY the results container. The paginator
        #    is a sibling of it and is stale by definition (it links to
        #    /page/2/?s=<probe term>).
        for child in list(root.find_all(True, recursive=False)):
            if child is container or container in child.descendants:
                continue
            classes = (c.lower() for c in (child.get("class") or ()))
            has_query_link = any("s=" in (a.get("href") or "")
                                 for a in child.find_all("a"))
            if has_query_link or any(PAGER_CLASS_RE.fullmatch(c)
                                     for c in classes):
                child.decompose()
        container["id"] = RESULTS_ID
        if container.has_attr("data-cur-page"):
            container["data-cur-page"] = "1"
        container.clear()
        p = soup.new_tag("p")
        p["class"] = ["wpse-search-status"]
        p.append("Suchergebnisse werden geladen \u2026" if cfg["de"]
                 else "Loading search results \u2026")
        container.append(p)

        # 2) head hygiene (shared with the fallback builder)
        self._clean_head(soup, cfg, path)

        # 3) structural echo replacement -- NO blind string replace: the
        #    probe term is a real word from our own corpus and appears in
        #    the harvested excerpts as legitimate content
        self._echo_marker_h1(soup, root, term, cfg)
        self._echo_marker_breadcrumb(soup, term)
        self._scrub_query(soup, term)

        # 4) the theme's own search box, pointed at us
        for form in soup.find_all("form"):
            if form.find("input", attrs={"name": re.compile(r"^(?:s|q)$")}):
                form["action"] = path
                form["method"] = "get"
                for a in form.find_all("a"):
                    if not (a.get("href") or "").strip():
                        a["href"] = path
        for inp in soup.find_all("input"):
            if (inp.get("name") or "") in ("s", "q"):
                inp["value"] = ""
        if soup.find("form") is None:
            container.insert_before(self._own_form(cfg))

        # 5) the image attributes postprocess_html can no longer add (it
        #    ran before run_end); NOT our own hook -- that would inject
        #    the ?s= redirect into the page it redirects to
        for other in self.exp.plugins:
            if other is self:
                continue
            try:
                other.postprocess_soup(soup)
            except Exception as exc:        # noqa: BLE001
                self.exp.warnings.append(
                    f"static search: {other.name}.postprocess_soup failed "
                    f"on the results page ({exc.__class__.__name__})")

    def _echo_marker_h1(self, soup, root, term: str, cfg: dict) -> None:
        """Keep the theme's static prefix ('Suchergebnisse f\u00fcr: ') VERBATIM
        -- that string is the whole point of harvesting -- and leave an
        empty marker the renderer fills with the real query."""
        h1 = next((c for c in soup.find_all("h1")
                   if root is None or (c is not root
                                       and root not in c.parents)), None)
        if h1 is None:
            return
        span = next((s for s in h1.find_all("span")
                     if s.get_text(strip=True) == term), None)
        if span is not None:
            span.clear()
            span[f"data-{MARKER}"] = "term"
            return
        parts = split_echo(h1.get_text(), term)
        if parts is None:
            h1.clear()                       # unrecognized: generic heading
            h1.append("Suchergebnisse" if cfg["de"] else "Search results")
            return
        self._retext(soup, h1, parts)

    def _echo_marker_breadcrumb(self, soup, term: str) -> None:
        for bc in soup.select('[itemtype*="BreadcrumbList"], ol.breadcrumbs, '
                              'ul.breadcrumbs, .breadcrumbs'):
            items = bc.find_all("li")
            if not items:
                continue
            leaf = items[-1]
            span = leaf.find("span", attrs={"itemprop": "name"}) or leaf
            inner = next((s for s in span.find_all(True)
                          if s.get_text(strip=True) == term), None)
            if inner is not None:
                inner.clear()
                inner[f"data-{MARKER}"] = "term"
                return
            parts = split_echo(span.get_text(), term)
            if parts is not None:            # keeps the surrounding quotes
                self._retext(soup, span, parts)
            return

    def _retext(self, soup, el, parts) -> None:
        mark = soup.new_tag("span")
        mark[f"data-{MARKER}"] = "term"
        el.clear()
        if parts[0]:
            el.append(NavigableString(parts[0]))
        el.append(mark)
        if parts[1]:
            el.append(NavigableString(parts[1]))

    def _scrub_query(self, node, term: str) -> None:
        """Remove the probe query from inline analytics/config scripts
        (`page_location`, `page_path`, pretty /search/<term>/ URLs). The
        value becomes an EMPTY query -- exactly what a visitor arriving at
        the results page without a term produces. Targeted patterns only:
        the term is a real word from our own corpus and must survive
        inside the harvested excerpts."""
        spellings = {term, quote(term, safe=""), quote(term, safe="").lower(),
                     quote(term, safe="+")}
        pats = []
        for sp in spellings:
            e = re.escape(sp)
            pats.append((re.compile(r"([?&](?:amp;)?s=)" + e + r"(?![\w%])"),
                         r"\1"))
            pats.append((re.compile(r"(/search/)" + e + r"/"), r"\1"))
            pats.append((re.compile(r"(\\/search\\/)" + e + r"\\/"), r"\1"))
        for script in node.find_all("script"):
            if "ld+json" in (script.get("type") or "").lower():
                continue                 # decomposed by _clean_head anyway
            text = "".join(c for c in script.contents if isinstance(c, str))
            if not text:
                continue
            new = text
            for rx, repl in pats:
                new = rx.sub(repl, new)
            if new != text:
                script.string = new

    # -- assets only this page references ------------------------------------

    def _scan_assets(self, node, base_path: str) -> list:
        """Absolute internal ASSET urls a fragment/document references --
        the subset of parse_and_save_html's candidate walk we need,
        WITHOUT its side effects (its discovery would dump every remaining
        ?s= URL into the reported skipped_query_urls and, with save_as
        omitted, map '/?s=x' onto public/index.html)."""
        exp = self.exp
        base = f"{exp.scheme}://{exp.host}{base_path}"
        out: list = []

        def take(val, tag_name):
            val = (val or "").strip()
            if not val or val.startswith(("data:", "mailto:", "tel:",
                                          "javascript:", "#")):
                return
            absolute, _ = exp._defrag(urljoin(base, val))
            if not exp.is_internal(absolute):
                return
            absolute = exp.to_base_host(absolute)
            if core.PAGE_SKIP_PATTERNS.search(urlsplit(absolute).path):
                return
            if exp.looks_like_asset(absolute) or tag_name in ("script",
                                                              "link"):
                out.append(absolute)

        for tag in node.find_all(True):
            if tag.name in ("a", "meta"):
                continue                 # page links / SEO metadata
            if tag.name == "link" and _rel_set(tag) & {
                    "canonical", "alternate", "shortlink", "pingback"}:
                continue
            for attr in core.URL_ATTRS:
                if tag.get(attr):
                    take(tag[attr], tag.name)
            for attr in core.SRCSET_ATTRS:
                if tag.get(attr):
                    for cand, _d in core.iter_srcset(tag[attr]):
                        take(cand, "img")
            for attr in core.CSS_URL_ATTRS + ("style",):
                val = tag.get(attr)
                if isinstance(val, str) and "url(" in val:
                    for mm in core.CSS_URL_RE.finditer(val):
                        take(mm.group(1), "style")
            for attr in core.B64_URL_ATTRS:
                dec = core.try_b64_url(tag.get(attr) or "")
                if dec:
                    take(dec, "img")
        for style in node.find_all("style"):
            css = style.string or ""
            for mm in (list(core.CSS_URL_RE.finditer(css))
                       + list(core.CSS_IMPORT_RE.finditer(css))):
                take(mm.group(1), "style")
        return out

    def _download_assets(self) -> None:
        """capture_404's mini-BFS: fetch what only this page references
        (masonry.min.js, imagesloaded.min.js, the search template's own
        CSS/JS bundle, card thumbnails) so verify_export stays green."""
        exp = self.exp
        before = len(exp.asset_urls_seen)
        pending = [a for a in dict.fromkeys(self._assets)
                   if a not in exp.asset_urls_seen]
        for _ in range(3):
            if not pending:
                break
            nxt: list = []
            for a in pending:
                if a in exp.asset_urls_seen:
                    continue
                exp.asset_urls_seen.add(a)
                try:
                    _, more = exp.process_asset(a)
                except requests.RequestException:
                    continue
                nxt.extend(more)
            pending = [a for a in nxt if a not in exp.asset_urls_seen]
        self.stats["harvest"]["assets_downloaded"] = (
            len(exp.asset_urls_seen) - before)

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
        h = self.stats["harvest"]
        extra = (f", design harvested live: {h['probe_requests']} requests, "
                 f"{h['pages_with_slots']}/{self.stats['pages_indexed']} "
                 f"cards, {h['assets_downloaded']} assets"
                 if h["used"] else ", built-in markup")
        return [f"[seo] site search: {self.stats['path']} "
                f"({self.stats['pages_indexed']} pages indexed, "
                f"{self.stats['index_bytes'] // 1024 or 1} KB index, "
                f"{self.stats['forms_rewritten']} forms rewritten{extra})"]

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
