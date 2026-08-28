"""Image optimization: <img> tags get loading="lazy" + decoding="async"
(except the first image per page -- LCP protection -- and images a
lazy-load plugin already manages) and, where the local file's header
reveals them, width/height attributes against layout shift (CLS).

Runs in the post-crawl pass because image files may only exist after
later crawl rounds than the page that references them.
"""
import unicodedata
from urllib.parse import unquote

import wp_static_export as core
from wp_static_export import Plugin, path_extension, read_image_size


class ImageOptimize(Plugin):
    name = "image_optimize"

    @classmethod
    def add_cli_args(cls, group):
        group.add_argument(
            "--no-optimize-images", dest="optimize_images",
            action="store_false",
            help="skip injecting loading=lazy/decoding=async and "
                 "width/height attributes into <img> tags. "
                 "No effect with --no-rewrite")

    @classmethod
    def finish_args(cls, ap, args, cfg):
        cfg.optimize_images = args.optimize_images

    def __init__(self, exporter):
        super().__init__(exporter)
        self.stats = {"lazy": 0, "dimensions": 0, "skipped_plugin": 0,
                      "unparsed": 0, "missing": 0}

    @property
    def enabled(self) -> bool:
        return self.exp.cfg.rewrite and self.exp.cfg.optimize_images

    def wants_postprocess(self) -> bool:
        return self.enabled

    def postprocess_soup(self, soup) -> bool:
        # the postprocess pass may run for other reasons (staging meta,
        # download links) -- only touch images when actually enabled
        if not self.enabled:
            return False
        changed = False
        first = True
        for img in soup.find_all("img"):
            if img.find_parent("noscript") is not None:
                continue  # lazyload fallback markup; also must not consume
                          # the eager first-image slot
            is_first = first
            first = False
            classes = " ".join(img.get("class") or []).lower()
            # LAZY_IMG_ATTRS read via the module at call time: this plugin
            # loads before the plugins that register those attrs, so a
            # from-import would freeze the un-aggregated base tuple
            plugin_lazy = (any(img.has_attr(a) for a in core.LAZY_IMG_ATTRS)
                           or "lazy" in classes)
            if plugin_lazy:
                self.stats["skipped_plugin"] += 1
            elif (not img.has_attr("loading") and not is_first
                    and (img.get("fetchpriority") or "").lower() != "high"):
                # first image per page stays eager (LCP protection)
                img["loading"] = "lazy"
                if not img.has_attr("decoding"):
                    img["decoding"] = "async"
                self.stats["lazy"] += 1
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
            target = self.exp.public_dir / path.lstrip("/")
            if not target.is_file():
                self.stats["missing"] += 1
                continue
            size = read_image_size(target)
            if size:
                img["width"], img["height"] = str(size[0]), str(size[1])
                self.stats["dimensions"] += 1
                changed = True
            else:
                self.stats["unparsed"] += 1
        return changed

    def summary_lines(self) -> list[str]:
        if not self.enabled:
            return []
        return [f"[seo] images: {self.stats['lazy']}x loading=lazy, "
                f"{self.stats['dimensions']}x width/height injected"]

    def add_report(self, report: dict, txt_head: list,
                   txt_sections: list) -> None:
        report["seo"]["image_optimization"] = self.stats
        txt_head.append(
            f"Images:          {self.stats['lazy']}x loading=lazy, "
            f"{self.stats['dimensions']}x width/height injected, "
            f"{self.stats['skipped_plugin']}x plugin-lazyload skipped"
            if self.enabled else
            "Images:          optimization disabled")


PLUGIN = ImageOptimize
