"""Image optimization: <img> tags get loading="lazy" + decoding="async"
(except the first image per page -- LCP protection -- and images a
lazy-load plugin already manages) and, where the local file's header
reveals them, width/height attributes against layout shift (CLS).

Runs in the post-crawl pass because image files may only exist after
later crawl rounds than the page that references them.
"""
import unicodedata
from pathlib import Path
from urllib.parse import unquote

import wp_static_export as core
from wp_static_export import Plugin, path_extension


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


class ImageOptimize(Plugin):
    name = "image_optimize"
    config_fields = {"optimize_images": True}

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
