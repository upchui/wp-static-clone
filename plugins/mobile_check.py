"""Mobile rendering check: responsive sites (one HTML + CSS media
queries) are covered by the export automatically -- this plugin VERIFIES
that assumption instead of silently relying on it. Every page is fetched
a second time with an iPhone user agent and compared against the desktop
response (nonces/whitespace normalized; a desktop control fetch rules out
per-request randomness):

  * identical  -> responsive, the export covers mobile as-is
  * different  -> UA-based dynamic serving; the mobile HTML is saved
                  under mobile-variants/ for inspection
  * additionally, 'Vary: User-Agent' response headers are recorded

Disable with --no-mobile-check (saves one extra request per page).
"""
import re

import requests

import wp_static_export as core
from wp_static_export import Plugin, report_section

VARIANT_DIR = "mobile-variants"

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
             "Mobile/15E148 Safari/604.1")

# Normalize away per-request noise before comparing two HTML responses, so
# WordPress dynamics don't cause false "different"s: CSRF nonces (both the
# JS and the <input name="_wpnonce"> form), cache-buster query params
# (?ver=..., ?v=...), plugin-registered noise patterns (HTML_NOISE_EXTRA,
# e.g. Ultimate Addons' per-request uniqid()/rand() element ids), HTML
# comments (generator/debug stamps) and whitespace. Over-normalizing is
# fine here -- worst case a truly dynamic page is classified "same".
NONCE_RE = re.compile(
    rb"""(_wpnonce|nonce|csrf[\w-]*|token)(["']?\s*[:=]\s*["'])"""
    rb"""[A-Za-z0-9+/=._-]{6,}(["'])""", re.IGNORECASE)
WPNONCE_ATTR_RE = re.compile(
    rb"""(name=["']_wpnonce["']\s+value=["'])[^"']+""", re.IGNORECASE)
HTML_COMMENT_RE = re.compile(rb"<!--.*?-->", re.DOTALL)
VER_QS_RE = re.compile(rb"[?&](?:ver|v|t|cache|nocache)=[^\s\"'&<>]+",
                       re.IGNORECASE)
WS_RE = re.compile(rb"\s+")


def normalize_html(data: bytes) -> bytes:
    data = HTML_COMMENT_RE.sub(b"", data)
    data = NONCE_RE.sub(rb"\1\2\3", data)
    data = WPNONCE_ATTR_RE.sub(rb"\1", data)
    data = VER_QS_RE.sub(b"", data)
    # HTML_NOISE_EXTRA read via the module at call time: this plugin loads
    # before the plugins that register those patterns (e.g. theme_fixes),
    # so a from-import would freeze the un-aggregated base tuple
    for rx, rep in core.HTML_NOISE_EXTRA:
        data = rx.sub(rep, data)
    return WS_RE.sub(b" ", data).strip()


class MobileCheck(Plugin):
    name = "mobile_check"
    extra_output_dirs = (VARIANT_DIR,)

    @classmethod
    def add_cli_args(cls, group):
        group.add_argument(
            "--no-mobile-check", dest="mobile_check", action="store_false",
            help="skip the mobile-vs-desktop HTML comparison "
                 "(saves one extra request per page)")
        group.add_argument(
            "--mobile-user-agent", default=None,
            help="user agent used for the mobile comparison "
                 "(default: iPhone Safari)")

    @classmethod
    def finish_args(cls, ap, args, cfg):
        cfg.mobile_check = args.mobile_check
        if args.mobile_user_agent:
            cfg.mobile_user_agent = args.mobile_user_agent

    def __init__(self, exporter):
        super().__init__(exporter)
        # [threaded] plain list appends from crawl workers (GIL-atomic)
        self.mobile_diff: list[dict] = []
        self.mobile_dynamic: list[str] = []
        self.vary_ua_pages: list[str] = []

    def page_fetched(self, url: str, resp, rec) -> None:
        if "user-agent" in (resp.headers.get("Vary") or "").lower():
            self.vary_ua_pages.append(url)

    def page_saved(self, save_url: str, resp, rec) -> None:
        if self.exp.cfg.mobile_check:
            self._check_mobile_variant(save_url, resp.content, rec)

    def _check_mobile_variant(self, url: str, desktop_raw: bytes,
                              rec) -> None:
        """Fetch the page with a mobile UA and compare against the desktop
        response. A second desktop fetch distinguishes UA-based dynamic
        serving from per-request randomness."""
        exp = self.exp
        mobile_ua = exp.cfg.mobile_user_agent or MOBILE_UA
        try:
            mresp = exp.fetch(url, headers={"User-Agent": mobile_ua})
        except requests.RequestException as exc:
            rec.mobile = "check-failed"
            exp.warnings.append(f"mobile check failed for {url}: {exc}")
            return
        if not mresp.ok or "html" not in (mresp.headers.get("Content-Type") or ""):
            rec.mobile = "check-failed"
            return
        if normalize_html(mresp.content) == normalize_html(desktop_raw):
            rec.mobile = "same"
            return
        # control fetch: does a second *desktop* request differ as well?
        try:
            dresp = exp.fetch(url)
        except requests.RequestException:
            rec.mobile = "check-failed"
            return
        if dresp.ok and normalize_html(dresp.content) != normalize_html(desktop_raw):
            rec.mobile = "dynamic"
            self.mobile_dynamic.append(url)
            return
        rec.mobile = "different"
        target = exp.local_path_for(url, is_page=True)
        variant_rel = ""
        if target:
            rel = target.relative_to(exp.public_dir.resolve())
            variant = exp.cfg.out_dir / VARIANT_DIR / rel
            with exp.write_lock:
                # two source URLs redirecting to the same save_url must not
                # write (and report) the same variant file concurrently
                if variant in exp.written_paths:
                    return
                exp.written_paths.add(variant)
            variant.parent.mkdir(parents=True, exist_ok=True)
            variant.write_bytes(mresp.content)
            variant_rel = f"{VARIANT_DIR}/{rel}"
        self.mobile_diff.append({"url": url, "variant": variant_rel})

    def summary_lines(self) -> list[str]:
        if not self.exp.cfg.mobile_check:
            return []
        if self.mobile_diff:
            return [f"[done] Mobile: {len(self.mobile_diff)} pages serve "
                    f"DIFFERENT mobile HTML -- variants under "
                    f"{VARIANT_DIR}/, details in the report"]
        if self.mobile_dynamic:
            return [f"[done] Mobile: {len(self.mobile_dynamic)} pages with "
                    f"dynamic content, UA comparison inconclusive -- "
                    f"see the report"]
        return ["[done] Mobile: HTML for a mobile UA is identical "
                "(responsive) -- the export covers the mobile rendering"]

    def add_report(self, report: dict, txt_head: list,
                   txt_sections: list) -> None:
        if self.mobile_diff:
            # report["warnings"] aliases exp.warnings (same list object),
            # so this appears in report.json, report.txt and the console
            # count -- exactly like the core append did
            self.exp.warnings.append(
                "Some pages serve DIFFERENT HTML to mobile user agents (UA-based "
                "dynamic serving). public/ contains the desktop variant; the "
                "mobile HTML was saved under mobile-variants/ for review. "
                "Options: switch theme/plugin to responsive rendering (one HTML "
                "for all devices), or serve both variants via an nginx "
                "user-agent map plus a 'Vary: User-Agent' response header.")
        report["mobile"] = {
            "checked": self.exp.cfg.mobile_check,
            "pages_with_different_mobile_html": self.mobile_diff,
            "pages_with_per_request_dynamic_content": self.mobile_dynamic,
            "pages_sending_vary_user_agent": self.vary_ua_pages,
            "amp_variants_exported": sorted(self.exp.amp_links),
        }
        txt_sections.append(report_section(
            "Mobile: pages serving DIFFERENT HTML to a mobile UA",
            self.mobile_diff,
            lambda d: f"{d['url']} (variant: {d['variant']})"))
        txt_sections.append(report_section(
            "Mobile: per-request dynamic content (UA comparison "
            "inconclusive)", self.mobile_dynamic))
        txt_sections.append(report_section(
            "Mobile: 'Vary: User-Agent' response header seen",
            self.vary_ua_pages))


PLUGIN = MobileCheck
