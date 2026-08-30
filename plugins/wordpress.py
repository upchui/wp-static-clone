"""WordPress-ecosystem specifics beyond the core's crawl policy: head
cruft pointing at dynamic origin infrastructure, WPForms AJAX routes,
WooCommerce cart/checkout pages (session-dependent, useless statically),
and machine-generated artifact pages.

ARTIFACT PAGES: WordPress gives every uploaded file its own "attachment
page", and image plugins add cache directories (phpThumb & co.). They
carry no editorial content -- a title generated from the file name and
the image itself -- yet Yoast's attachment sitemap lists them, so the
"the origin sitemap declared it" rule cannot filter them out.

The same goes for an EMPTY TERM ARCHIVE. A plugin that registers a
taxonomy for its own bookkeeping (statistics, import batches) on a
post type that is not publicly queryable leaves a term archive per term
whose loop returns nothing -- the page is WordPress' "Nothing found"
template, and Yoast happily declares it index,follow. Six such pages made
up more than half of one site's sitemap.

Both kinds are still exported (a theme's lightbox, a stray link) but kept
out of the generated sitemap.xml and the site search by setting
`rec.artifact`, which Exporter.index_exclusion() consults. Opt out with
--list-attachment-pages / --list-empty-archives.
"""
import re

from wp_static_export import Plugin

# WordPress' own body_class() output (wp-includes/post-template.php),
# emitted by every theme that calls body_class(). Matched as WHOLE class
# tokens on <body> only: "attachment-png"/"attachment-jpeg" are fine as
# tokens, but an <img class="attachment-thumbnail"> must never count.
ATTACHMENT_BODY_CLASSES = frozenset((
    "attachment", "attachment-template-default", "single-attachment"))
ATTACHMENT_ID_RE = re.compile(r"attachmentid-\d+")
BODY_CLASS_RE = re.compile(
    rb"""<body[^>]*?\sclass\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
BODY_SCAN_BYTES = 200_000        # <body> sits well inside the first chunk
# image-cache directories (phpThumb): pure derivatives, and some themes
# render them without WordPress' attachment body classes
CACHE_PATH_RE = re.compile(r"/phpthumb_cache[^/]*/", re.IGNORECASE)

# EMPTY TERM ARCHIVES: body_class() marks every is_archive() page (category,
# tag, custom taxonomy, author, date, post-type archive) with "archive" --
# core, not a theme detail. Deliberately WITHOUT "blog" (on a site whose
# front page is the blog that class sits on the HOMEPAGE) and without
# "search" (our own generated results page carries "search search-results").
ARCHIVE_BODY_CLASS = "archive"
# core's own content-none.php: <section class="no-results not-found">, which
# _s/Twenty*-derived and most commercial themes (the7 among them) inherit
NO_RESULTS_CLASSES = frozenset(("no-results", "not-found"))
CLASS_ATTR_RE = re.compile(
    rb"""\sclass\s*=\s*["']([^"']*)["']""", re.IGNORECASE)


def _class_tokens(raw: bytes) -> set:
    """A class attribute as WHOLE tokens -- never substrings, so a
    "wpa-no-results" can not pass for "no-results"."""
    return set(raw.decode("ascii", "replace").lower().split())


def _says_nothing_found(blob: bytes) -> bool:
    """True when the page renders WordPress' own 'nothing here' block --
    matched in a class ATTRIBUTE, so the same words in running text or in
    a stylesheet mean nothing."""
    return any(NO_RESULTS_CLASSES <= _class_tokens(m.group(1))
               for m in CLASS_ATTR_RE.finditer(blob))


class WordPress(Plugin):
    name = "wordpress"
    # WooCommerce: session/dynamic pages, never part of a static mirror
    page_skip_pattern_fragments = (r"/cart/?$", r"/checkout/?$")
    config_fields = {"list_attachment_pages": False,
                     "list_empty_archives": False}

    @classmethod
    def add_cli_args(cls, group):
        group.add_argument(
            "--list-attachment-pages", action="store_true",
            help="list WordPress attachment and image-cache pages in the "
                 "generated sitemap.xml and the site search (default: they "
                 "are exported but treated as machine-generated artifacts). "
                 "For sites where the attachment page IS the content")
        group.add_argument(
            "--list-empty-archives", action="store_true",
            help="list category/tag/taxonomy archives whose loop is empty "
                 "(the page says 'Nothing found') in the generated "
                 "sitemap.xml and the site search (default: they are "
                 "exported but treated as machine-generated artifacts)")

    @classmethod
    def finish_args(cls, ap, args, cfg):
        cfg.list_attachment_pages = args.list_attachment_pages
        cfg.list_empty_archives = args.list_empty_archives

    def __init__(self, exporter):
        super().__init__(exporter)
        self.stats = {"attachment_pages": 0, "cache_pages": 0,
                      "empty_archives": 0}

    def page_saved(self, save_url: str, resp, rec) -> None:
        """Flag artifact pages on their PageRecord. Reads the raw origin
        bytes rather than a soup: this hook is the only one with both the
        record and the response, it needs no second parse, and it works
        in --no-rewrite mode too."""
        if rec is None or rec.artifact:
            return
        cfg = self.exp.cfg
        kind = ""
        blob = resp.content[:BODY_SCAN_BYTES]
        if CACHE_PATH_RE.search(save_url) and not cfg.list_attachment_pages:
            kind = "phpthumb-cache"
        else:
            m = BODY_CLASS_RE.search(blob)
            classes = _class_tokens(m.group(1)) if m else set()
            if not cfg.list_attachment_pages and (
                    classes & ATTACHMENT_BODY_CLASSES
                    or any(ATTACHMENT_ID_RE.fullmatch(c) for c in classes)):
                kind = "wp-attachment"
            elif (not cfg.list_empty_archives
                  and ARCHIVE_BODY_CLASS in classes
                  and _says_nothing_found(blob)):
                # a term archive whose loop came back empty: WordPress
                # renders "Nothing found" and Yoast still says index,follow,
                # so nothing else keeps it out of the sitemap or the search
                kind = "empty-archive"
        if not kind:
            return
        rec.artifact = kind
        with self.exp.stats_lock:
            self.stats[{"phpthumb-cache": "cache_pages",
                        "empty-archive": "empty_archives"}
                       .get(kind, "attachment_pages")] += 1

    def skip_page_path(self, path: str) -> bool:
        return path.startswith("/wpforms-ajax")   # form AJAX routes

    def skip_asset_candidate(self, url: str, tag_name: str,
                             rel: set) -> bool:
        # clean_soup strips wlwmanifest/EditURI links -- don't mirror
        # their targets either
        return (tag_name == "link"
                and bool(rel & {"wlwmanifest", "edituri"})
                and self.exp.cfg.rewrite and self.exp.cfg.strip_wp_cruft)

    def clean_soup(self, soup) -> None:
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
            href = link.get("href") or ""
            # feed/oEmbed/REST discovery only -- hreflang alternates carry
            # no type attribute and stay untouched
            if "alternate" in rel and (
                    "oembed" in ltype or "rss+xml" in ltype
                    or "atom+xml" in ltype):
                drop(link, "oembed-discovery" if "oembed" in ltype
                     else "feed-discovery")
                continue
            if ("alternate" in rel and ltype == "application/json"
                    and "/wp-json/" in href):
                drop(link, "rest-api-discovery")
        if removed:
            with self.exp.stats_lock:
                for kind, n in removed.items():
                    self.exp.cruft_removed[kind] = (
                        self.exp.cruft_removed.get(kind, 0) + n)

    def summary_lines(self) -> list[str]:
        kinds = []
        n = self.stats["attachment_pages"] + self.stats["cache_pages"]
        if n:
            kinds.append(f"{n} WP attachment/image cache "
                         f"(--list-attachment-pages lists them)")
        if self.stats["empty_archives"]:
            kinds.append(f"{self.stats['empty_archives']} empty term "
                         f"archives (--list-empty-archives lists them)")
        if not kinds:
            return []
        return ["[seo] artifact pages exported but kept out of sitemap.xml "
                "and the site search: " + ", ".join(kinds)]

    def add_report(self, report: dict, txt_head: list,
                   txt_sections: list) -> None:
        report["seo"]["artifact_pages"] = dict(
            self.stats, listed=self.exp.cfg.list_attachment_pages,
            empty_archives_listed=self.exp.cfg.list_empty_archives)


PLUGIN = WordPress
