"""Minification of exported HTML, CSS and JS (rewrite mode only).

EVERY comment is removed, whatever the type: HTML comments (IE
conditional relics too, and even inside <pre> -- they never render),
CSS/JS comments incl. /*! license banners */, legacy <!-- ... //-->
hiding wrappers, comments in style="" attributes, and inline module
scripts. Indentation is collapsed. JSON-LD, template/data script blocks
and textarea content (visible text) stay untouched. XML comments in
exported .svg assets go too (outside CDATA sections).
Needs the rjsmin and rcssmin packages -- a stale venv fails loudly at the
CLI instead of silently producing an unminified export.
"""
import re

from bs4 import Comment

from wp_static_export import Plugin

try:                        # optional at import time; the CLI enforces them
    import rcssmin
    import rjsmin
except ImportError:         # pragma: no cover
    rcssmin = rjsmin = None

# content of these tags must never be touched by the HTML whitespace pass
# (pre/textarea render whitespace; script/style content is minified
# separately by rjsmin/rcssmin, which know where newlines matter)
MINIFY_PROTECT_RE = re.compile(
    r"(<(?:pre|textarea|script|style)\b.*?</(?:pre|textarea|script|style)\s*>)",
    re.IGNORECASE | re.DOTALL)
HTML_COMMENT_ALL_RE = re.compile(r"<!--.*?-->", re.DOTALL)
INDENT_RE = re.compile(r"[ \t]*\n[ \t\n]*")

# legacy SGML comment-hiding wrappers around inline CSS/JS: both minifiers
# PRESERVE the tokens (CDO/CDC are legal CSS; <!-- is Annex-B JS syntax).
# Strip anchored (start/end) plus own-line occurrences -- concatenated
# bundles carry wrappers mid-file on their own lines. Own-line is safe:
# CSS has no multi-line strings; in JS a line-start <!-- / --> IS a
# comment per Annex B. A global sub would clobber url("a-->b") / "-->".
CDO_RE = re.compile(r"\A\s*<!--")
CSS_CDC_RE = re.compile(r"-->\s*\Z")
JS_CDC_RE = re.compile(r"(?://[ \t]*)?-->\s*\Z")
HIDE_LINE_RE = re.compile(r"(?m)^[ \t]*(?:<!--|(?://[ \t]*)?-->)[ \t]*$\n?")


def minify_css(text: str) -> str:
    # keep_bang_comments=False: /*! license banners */ go too -- explicit,
    # so a future library-default change can't bring them back
    if not rcssmin:
        return text
    text = HIDE_LINE_RE.sub("", CSS_CDC_RE.sub("", CDO_RE.sub("", text)))
    out = rcssmin.cssmin(text, keep_bang_comments=False)
    # rcssmin deliberately preserves the ancient IE5/Mac hack pair
    # ("/*\*/ rule /**/") -- obsolete for two decades, strip exactly
    # those two tokens (nothing else matches this pattern)
    return re.sub(r"/\*\\?\*/", "", out)


def minify_js(text: str) -> str:
    if not rjsmin:
        return text
    text = HIDE_LINE_RE.sub("", JS_CDC_RE.sub("", CDO_RE.sub("", text)))
    return rjsmin.jsmin(text, keep_bang_comments=False)


def minify_html_bytes(data: bytes) -> bytes:
    """Conservative HTML minification: drop ALL comments (including the
    IE conditional-comment relics -- the downlevel-revealed pattern keeps
    its enclosed markup, hidden IE-only blocks vanish entirely) and
    collapse indentation to a single newline -- whitespace between inline
    elements is render-relevant, so exactly one newline survives (renders
    as one space, like before). pre/textarea/script/style content is left
    byte-identical."""
    text = data.decode("utf-8", "replace")
    parts = MINIFY_PROTECT_RE.split(text)
    for i in range(0, len(parts), 2):       # even indexes = unprotected
        part = HTML_COMMENT_ALL_RE.sub("", parts[i])
        parts[i] = INDENT_RE.sub("\n", part)
    return "".join(parts).encode("utf-8")


SVG_CDATA_RE = re.compile(r"(<!\[CDATA\[.*?\]\]>)", re.DOTALL)


def strip_svg_comments(text: str) -> str:
    """XML comments out of SVG text; CDATA sections stay byte-identical
    (their '<!--' is character data, e.g. inside embedded scripts)."""
    parts = SVG_CDATA_RE.split(text)
    for i in range(0, len(parts), 2):       # even indexes = outside CDATA
        parts[i] = HTML_COMMENT_ALL_RE.sub("", parts[i])
    return "".join(parts)


class Minify(Plugin):
    name = "minify"

    @classmethod
    def add_cli_args(cls, group):
        group.add_argument(
            "--no-minify", dest="minify", action="store_false",
            help="skip minifying exported HTML, CSS and JS "
                 "(conservative: ALL comments removed incl. IE "
                 "conditionals and license banners, indentation "
                 "collapsed; JSON-LD and pre/textarea untouched). "
                 "No effect with --no-rewrite")

    @classmethod
    def finish_args(cls, ap, args, cfg):
        cfg.minify = args.minify
        if args.minify and args.rewrite and (rjsmin is None or rcssmin is None):
            # a stale venv would otherwise silently produce an unminified
            # export -- fail loudly with the fix instead
            ap.error("minification (default on) needs the rjsmin and rcssmin "
                     "packages -- run: pip install -r requirements.txt "
                     "(or pass --no-minify)")

    def __init__(self, exporter):
        super().__init__(exporter)
        self.stats = {"html": 0, "css": 0, "js": 0, "svg": 0,
                      "bytes_saved": 0}

    @property
    def enabled(self) -> bool:
        return (self.exp.cfg.rewrite and self.exp.cfg.minify
                and rjsmin is not None and rcssmin is not None)

    def run_start(self) -> None:
        # programmatic Config use bypasses the CLI hard error -- warn
        if (self.exp.cfg.minify and self.exp.cfg.rewrite
                and (rjsmin is None or rcssmin is None)):
            self.exp.warnings.append(
                "minification skipped: rjsmin/rcssmin not installed "
                "(pip install rjsmin rcssmin)")

    def filter_text_asset(self, url: str, kind: str, text: str) -> str:
        # .min.* files run through as well: a safe near-no-op that still
        # strips their /*! license banners */
        if not self.enabled:
            return text
        return minify_css(text) if kind == "css" else minify_js(text)

    def text_asset_written(self, kind: str, orig_len: int,
                           new_len: int) -> None:
        if not self.enabled:
            return
        with self.exp.stats_lock:
            self.stats[kind] += 1
            self.stats["bytes_saved"] += max(0, orig_len - new_len)

    def pre_serialize(self, soup) -> None:
        if not self.enabled:
            return
        # real HTML Comment nodes ANYWHERE in the tree go -- this also
        # covers <pre> blocks, which the byte-level pass must protect for
        # whitespace reasons (comments render as nothing, so removal is
        # invisible). textarea is RCDATA (its "<!-- -->" is visible text,
        # a NavigableString under current bs4) -- guard anyway in case an
        # older parser hands us a Comment node there
        for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
            if c.find_parent("textarea") is None:
                c.extract()
        for tag in soup.find_all(style=True):
            if "/*" in tag["style"] or "<!--" in tag["style"]:
                tag["style"] = minify_css(tag["style"])
        for style in soup.find_all("style"):
            # .string is None for 0 or >=2 children (empty tags, or a
            # plugin appended text) -- join the text children and set
            # .string, which replaces them all. NOT .strings: that
            # filters on the tag's string-container class (Stylesheet/
            # Script) and would drop plain appended NavigableStrings.
            css = "".join(c for c in style.contents if isinstance(c, str))
            if css:
                style.string = minify_css(css)
        for script in soup.find_all("script"):
            stype = (script.get("type") or "").strip().lower()
            if (script.get("src") or "json" in stype or "template" in stype
                    or (stype not in ("", "module")
                        and "javascript" not in stype
                        and "ecmascript" not in stype)):
                continue    # external / data blocks / non-JS types: untouched
            js = "".join(c for c in script.contents if isinstance(c, str))
            if js:
                script.string = minify_js(js)

    def post_serialize(self, data: bytes) -> bytes:
        return minify_html_bytes(data) if self.enabled else data

    def run_end(self) -> None:
        """SVG assets are text but copied byte-identical by the crawl --
        strip their XML comments in the post-crawl pass. Comments only:
        no whitespace games, CDATA untouched, non-UTF-8 files skipped."""
        if not self.enabled:
            return
        for svg in sorted(self.exp.public_dir.rglob("*")):
            if not (svg.suffix.lower() == ".svg" and svg.is_file()):
                continue
            try:
                raw = svg.read_bytes()
                text = raw.decode("utf-8")   # strict: exotic encodings stay
            except (OSError, UnicodeDecodeError):
                continue
            out = strip_svg_comments(text)
            if out == text:
                continue
            data = out.encode("utf-8")
            try:
                svg.write_bytes(data)
            except OSError as exc:
                self.exp.warnings.append(
                    f"svg comment strip: cannot write {svg.name}: {exc}")
                continue
            with self.exp.stats_lock:
                self.stats["svg"] += 1
                self.stats["bytes_saved"] += len(raw) - len(data)

    def summary_lines(self) -> list[str]:
        if not (self.enabled and any(self.stats.values())):
            return []
        return [f"[seo] minified: {self.stats['html']} HTML, "
                f"{self.stats['css']} CSS, "
                f"{self.stats['js']} JS, "
                f"{self.stats['svg']} SVG files "
                f"({self.stats['bytes_saved'] // 1024} KB saved)"]

    def add_report(self, report: dict, txt_head: list,
                   txt_sections: list) -> None:
        report["seo"]["minified"] = self.stats


PLUGIN = Minify
