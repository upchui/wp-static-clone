"""Slider Revolution 7 support.

* Slide hydration: SR7 lazy-loads slides whose inline `layers` are empty
  via /wp-json/sliderrevolution/sliders/<id>?slideid=... at runtime -- a
  request that can never succeed on a static mirror, so the slider would
  freeze on slide 1. The full slider object is fetched once at export time
  and the missing layers are merged into the inline SR7.JSON blob.
* Lazy-src attributes: SR7 base64-encodes image URLs into data-dbsrc.
* Runtime resources: sr7.js constructs its module/lib/css URLs from
  SR7.E.plugin_url plus name lists -- those candidates are queued
  speculatively so the slider's lazy-loaded CSS/JS ends up in the export.
"""
import json
import posixpath
import re
from urllib.parse import urljoin, urlsplit

import requests

from wp_static_export import FOREIGN_WP_RE, Plugin, norm_host, report_section

PLUGIN_URL_RE = re.compile(r"SR7\.E\.plugin_url\s*=\s*['\"]([^'\"]+)['\"]")


class SliderRevolution(Plugin):
    name = "slider_revolution"
    b64_url_attrs = ("data-dbsrc",)

    @classmethod
    def add_cli_args(cls, group):
        group.add_argument(
            "--no-sr7-hydrate", dest="sr7_hydrate", action="store_false",
            help="do not embed lazy Slider Revolution 7 slides into "
                 "the pages. Without hydration SR7 sliders freeze on "
                 "slide 1 statically (they fetch later slides from "
                 "wp-json at runtime); hydration performs the only "
                 "wp-json request the exporter ever makes")

    @classmethod
    def finish_args(cls, ap, args, cfg):
        cfg.sr7_hydrate = args.sr7_hydrate

    def __init__(self, exporter):
        super().__init__(exporter)
        # REST full-objects per slider id, and per-module count of slides
        # whose layers were embedded
        self._rest_cache: dict[str, dict | None] = {}
        self.hydrated: dict[str, int] = {}

    def pre_discover_soup(self, soup, page_url: str) -> None:
        # must run BEFORE discovery + rewrite: both need to see the
        # hydrated SR7 blob (later-slide images!)
        if self.exp.cfg.sr7_hydrate:
            self._hydrate(soup)

    def _full_object(self, slider_id: str) -> dict | None:
        """Full slider object from the SR7 REST endpoint (all slides with
        layers, no slideid parameter needed) -- fetched once per slider id.
        This is the ONLY wp-json request the exporter ever makes, and only
        when a slider actually has lazy slides (--no-sr7-hydrate disables)."""
        exp = self.exp
        with exp.stats_lock:
            if slider_id in self._rest_cache:
                return self._rest_cache[slider_id]
        data: dict | None = None
        url = (f"{exp.scheme}://{exp.host}/wp-json/sliderrevolution/"
               f"sliders/{slider_id}?srengine=7")
        try:
            resp = exp.fetch(url)
            if resp.ok and not exp.off_site_redirect(resp):
                obj = resp.json()
                if isinstance(obj, dict) and obj.get("success"):
                    data = obj
        except (requests.RequestException, ValueError):
            pass
        with exp.stats_lock:
            self._rest_cache[slider_id] = data
        return data

    def _hydrate(self, soup) -> None:
        """Merge the lazy slides' layers into the inline SR7.JSON blob --
        the SR7 runtime's own cache check (non-empty layers) then skips its
        wp-json fetch entirely."""
        modules = soup.find_all("sr7-module")
        if not modules:
            return
        exp = self.exp
        decoder = json.JSONDecoder()

        def is_lazy(entry: dict) -> bool:
            slide = entry.get("slide") or {}
            return (len(entry.get("layers") or []) == 0
                    and not slide.get("global") and len(slide) > 0)

        for mod in modules:
            slider_id = (mod.get("data-id") or "").strip()
            dom_id = (mod.get("id") or "").strip()
            if not slider_id or not dom_id:
                continue
            marker_re = re.compile(
                rf"SR7\.JSON\[['\"]{re.escape(dom_id)}['\"]\]\s*=\s*")
            for script in soup.find_all("script"):
                text = script.string or ""
                m = marker_re.search(text)
                if not m:
                    continue
                try:
                    blob, end = decoder.raw_decode(text, m.end())
                except ValueError:
                    exp.warnings.append(
                        f"SR7 {dom_id}: inline JSON not parseable, "
                        f"hydration skipped")
                    break
                slides = blob.get("slides") if isinstance(blob, dict) else None
                if not isinstance(slides, dict):
                    break
                pending = {k: v for k, v in slides.items()
                           if isinstance(v, dict) and is_lazy(v)}
                if not pending:
                    break
                full = self._full_object(slider_id)
                if full is None:
                    exp.warnings.append(
                        f"SR7 slider {slider_id} ({dom_id}) has lazy slides "
                        f"but the REST endpoint gave no usable answer -- the "
                        f"slider will freeze in the static mirror")
                    break
                by_sid: dict[str, dict] = {}
                for entry in (full.get("slides") or {}).values():
                    if isinstance(entry, dict):
                        sid = (entry.get("slide") or {}).get("id")
                        if sid is not None:
                            by_sid[str(sid)] = entry
                merged = 0
                for entry in pending.values():
                    sid = str((entry.get("slide") or {}).get("id"))
                    src = by_sid.get(sid)
                    if src and len(src.get("layers") or []) > 0:
                        entry["layers"] = src["layers"]
                        merged += 1
                if merged:
                    new_blob = json.dumps(blob, ensure_ascii=False,
                                          separators=(",", ":"))
                    script.string = text[:m.end()] + new_blob + text[end:]
                    with exp.stats_lock:
                        self.hydrated[dom_id] = merged
                    # REST layers may carry the siteurl host (admin domain)
                    for fw in FOREIGN_WP_RE.finditer(new_blob):
                        if norm_host(fw.group(1)) not in exp.internal_norms:
                            exp.foreign_wp_hosts.add(fw.group(1))
                still = [1 for v in pending.values()
                         if len(v.get("layers") or []) == 0]
                if still:
                    exp.warnings.append(
                        f"SR7 slider {slider_id} ({dom_id}): {len(still)} "
                        f"lazy slide(s) not hydratable (stream source?) -- "
                        f"the slider may freeze statically")
                break

    def scan_text_urls(self, work: str) -> tuple[list, list]:
        # sr7.js constructs its runtime resource URLs from SR7.E.plugin_url
        # plus module/lib/css name lists (plugin_url+"public/js/"+name+".js"
        # etc.). The CSS ones are loaded lazily by every slider; without
        # them checkResources() never resolves and the slider stays blank.
        # Queue all candidates speculatively -- names satisfied inline by
        # the sr7.js bundle 404 on the origin too and are skipped silently.
        m_pu = PLUGIN_URL_RE.search(work)
        if not m_pu or "SR7.E.modules" not in work:
            return [], []
        exp = self.exp
        base = urlsplit(urljoin(f"{exp.scheme}://{exp.host}/",
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
        assets: list[str] = []
        for cand in candidates:
            absolute = (f"{exp.scheme}://{exp.host}"
                        f"{posixpath.join(base, cand)}")
            exp.speculative_urls.add(absolute)
            assets.append(absolute)
        return [], assets

    def summary_lines(self) -> list[str]:
        if not self.hydrated:
            return []
        return [f"[seo] SR7: {sum(self.hydrated.values())} lazy "
                f"slide(s) hydrated into {len(self.hydrated)} "
                f"slider(s) -- no runtime wp-json requests needed"]

    def add_report(self, report: dict, txt_head: list,
                   txt_sections: list) -> None:
        report["verification"]["policy"] += (
            "; wp-json is requested only for the read-only Slider Revolution"
            " endpoint when a slider has lazy slides (--no-sr7-hydrate"
            " disables)")
        report["sr7_hydrated"] = dict(sorted(self.hydrated.items()))
        txt_sections.append(report_section(
            "SR7 sliders: lazy slides hydrated into the page",
            sorted(self.hydrated.items()),
            lambda s: f"{s[0]}: {s[1]} slide(s)"))


PLUGIN = SliderRevolution
