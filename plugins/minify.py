"""Minification of exported HTML, CSS and JS (rewrite mode only).

Conservative: ALL HTML comments are removed (including the IE
conditional-comment relics and /*! license banners */), indentation is
collapsed; JSON-LD, pre/textarea and non-JS script blocks stay untouched.
Needs the rjsmin and rcssmin packages -- a stale venv fails loudly at the
CLI instead of silently producing an unminified export.
"""
import re

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


def minify_css(text: str) -> str:
    # keep_bang_comments=False: /*! license banners */ go too -- explicit,
    # so a future library-default change can't bring them back
    if not rcssmin:
        return text
    out = rcssmin.cssmin(text, keep_bang_comments=False)
    # rcssmin deliberately preserves the ancient IE5/Mac hack pair
    # ("/*\*/ rule /**/") -- obsolete for two decades, strip exactly
    # those two tokens (nothing else matches this pattern)
    return re.sub(r"/\*\\?\*/", "", out)


def minify_js(text: str) -> str:
    return rjsmin.jsmin(text, keep_bang_comments=False) if rjsmin else text


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
        self.stats = {"html": 0, "css": 0, "js": 0, "bytes_saved": 0}

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
        for style in soup.find_all("style"):
            if style.string:
                style.string = minify_css(style.string)
        for script in soup.find_all("script"):
            stype = (script.get("type") or "").lower()
            if (script.get("src") or "json" in stype or "template" in stype
                    or (stype and "javascript" not in stype)):
                continue    # external / data blocks / non-JS types: untouched
            if script.string:
                script.string = minify_js(script.string)

    def post_serialize(self, data: bytes) -> bytes:
        return minify_html_bytes(data) if self.enabled else data

    def summary_lines(self) -> list[str]:
        if not (self.enabled and any(self.stats.values())):
            return []
        return [f"[seo] minified: {self.stats['html']} HTML, "
                f"{self.stats['css']} CSS, "
                f"{self.stats['js']} JS files "
                f"({self.stats['bytes_saved'] // 1024} KB saved)"]

    def add_report(self, report: dict, txt_head: list,
                   txt_sections: list) -> None:
        report["seo"]["minified"] = self.stats


PLUGIN = Minify
