"""Recompression of exported raster images (smaller files, same pixels).

Every exported PNG/JPEG/GIF/WebP is decoded and re-encoded with better
settings than WordPress/PHP typically produce: PNG and static GIF
losslessly (optimize=True, plus an exact-lossless palette reduction for
PNGs that fit 256 colors), JPEG at --image-quality (default 85,
progressive, EXIF orientation baked into the pixels, ICC profile kept,
all other metadata dropped), lossless-coded WebP repacked at maximum
lossless effort. A re-encode replaces the original only when it is
significantly smaller (>=10% for the lossy JPEG path, >=2% for lossless
paths) AND the new bytes decode back to the expected dimensions -- for
the lossless paths additionally to pixel-identical RGBA renderings.
Animated images (GIF, APNG, animated WebP) are never touched. Runs
post-crawl over the whole output tree (run_end), so images are caught
regardless of how they arrived (crawl, sitemap, favicon, 404 assets,
download endpoints).

Needs the Pillow package -- a stale venv fails loudly at the CLI
instead of silently producing an uncompressed export (same policy as
plugins/minify.py).
"""
import io
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from wp_static_export import Plugin

try:                        # optional at import time; the CLI enforces it
    from PIL import Image, ImageOps
except ImportError:         # pragma: no cover
    Image = ImageOps = None

# .ico/.bmp/.avif are deliberately NOT here: Pillow's ICO/BMP re-encode
# support is shaky and AVIF needs an extra codec plugin; .svg is text
# (the minify plugin strips its comments); .jpe is not in
# ASSET_EXTENSIONS, so it can never exist in an export.
RASTER_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
LOSSY_MIN_GAIN = 0.10       # JPEG re-encode adds generational loss --
                            # only worth it for a real payoff
LOSSLESS_MIN_GAIN = 0.02    # pixel-identical: any noticeable gain counts


def significant_gain(orig_size: int, new_size: int, min_gain: float) -> bool:
    """True when new_size undercuts orig_size by at least min_gain
    (fraction). An empty re-encode (0 bytes) is never a gain."""
    return (0 < new_size < orig_size
            and (orig_size - new_size) >= orig_size * min_gain)


class ImageCompress(Plugin):
    name = "image_compress"
    config_fields = {"compress_images": True, "image_quality": 85}

    @classmethod
    def add_cli_args(cls, group):
        group.add_argument(
            "--no-compress-images", dest="compress_images",
            action="store_false",
            help="skip recompressing exported images (PNG/GIF lossless, "
                 "JPEG re-encoded at --image-quality, lossless WebP "
                 "repacked; originals stay unless the result is "
                 "significantly smaller). No effect with --no-rewrite")
        group.add_argument(
            "--image-quality", type=int, default=85, metavar="N",
            help="JPEG re-encode quality 1-95 (default 85); lossless "
                 "formats are unaffected")

    @classmethod
    def finish_args(cls, ap, args, cfg):
        if not 1 <= args.image_quality <= 95:
            ap.error(f"--image-quality must be between 1 and 95, "
                     f"got {args.image_quality}")
        cfg.compress_images = args.compress_images
        cfg.image_quality = args.image_quality
        if args.compress_images and args.rewrite and Image is None:
            # a stale venv would otherwise silently produce an
            # uncompressed export -- fail loudly with the fix instead
            ap.error("image recompression (default on) needs the Pillow "
                     "package -- run: pip install -r requirements.txt "
                     "(or pass --no-compress-images)")

    def __init__(self, exporter):
        super().__init__(exporter)
        self.stats = {"png": 0, "jpeg": 0, "gif": 0, "webp": 0,
                      "kept": 0, "animated_skipped": 0, "failed": 0,
                      "bytes_saved": 0}

    @property
    def enabled(self) -> bool:
        return (self.exp.cfg.rewrite and self.exp.cfg.compress_images
                and Image is not None)

    def run_start(self) -> None:
        # programmatic Config use bypasses the CLI hard error -- warn
        if (self.exp.cfg.compress_images and self.exp.cfg.rewrite
                and Image is None):
            self.exp.warnings.append(
                "image recompression skipped: Pillow not installed "
                "(pip install Pillow)")

    # -- the post-crawl pass -------------------------------------------------

    def run_end(self) -> None:
        if not self.enabled:
            return
        files = sorted(p for p in self.exp.public_dir.rglob("*")
                       if p.suffix.lower() in RASTER_EXTENSIONS
                       and p.is_file())
        if not files:
            return
        # Pillow releases the GIL while (de)coding -- reuse the crawl's
        # concurrency setting for the file pass
        with ThreadPoolExecutor(max_workers=self.exp.cfg.concurrency) as pool:
            list(pool.map(self._compress_file, files))

    def _compress_file(self, path: Path) -> None:
        # per-file isolation: one broken image must never kill the pass
        # (step() already guards the whole hook, but a crash there would
        # abandon every remaining image)
        try:
            self._compress_one(path)
        except Exception as exc:            # noqa: BLE001
            self._bump("failed")
            self.exp.warnings.append(
                f"image recompression skipped "
                f"{path.relative_to(self.exp.public_dir).as_posix()} "
                f"({exc.__class__.__name__}: {exc})")

    def _compress_one(self, path: Path) -> None:
        orig = path.read_bytes()
        with Image.open(io.BytesIO(orig)) as im:
            if getattr(im, "is_animated", False):   # GIF, APNG, anim WebP
                self._bump("animated_skipped")
                return
            im.load()
            fmt = im.format     # trust the real codec, not the file name
            lossless = fmt != "JPEG"
            if fmt == "JPEG":
                new, min_gain, expect = self._encode_jpeg(im)
            elif fmt == "PNG":
                new, min_gain, expect = self._encode_png(im)
            elif fmt == "GIF":
                new, min_gain, expect = self._encode_gif(im)
            elif fmt == "WEBP":
                new, min_gain, expect = self._encode_webp(im, orig)
            else:               # mis-labeled file (e.g. AVIF as .png)
                new, min_gain, expect = None, 0.0, None
            if (new is None
                    or not significant_gain(len(orig), len(new), min_gain)):
                self._bump("kept")
                return
            # paranoia before replacing bytes that already passed the
            # crawl: the re-encode must decode back to the expected
            # dimensions, and the lossless paths must render
            # pixel-identically to the original
            with Image.open(io.BytesIO(new)) as check:
                check.load()
                if check.size != expect:
                    raise ValueError(f"re-encode changed dimensions "
                                     f"{expect} -> {check.size}")
                if (lossless and check.convert("RGBA").tobytes()
                        != im.convert("RGBA").tobytes()):
                    raise ValueError("re-encode changed pixels")
        # atomic in-place replace: the path stays identical, so the
        # first-write-wins bookkeeping (exp.written_paths) stays valid
        fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                                   prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(new)
            # mkstemp creates 0600 -- keep the original's mode, or a
            # web server running as another user can't read the image
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
            os.replace(tmp, path)           # atomic, Windows-safe overwrite
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        with self.exp.stats_lock:
            self.stats[fmt.lower()] += 1
            self.stats["bytes_saved"] += len(orig) - len(new)

    # -- per-format encoders: (bytes|None, min_gain, expected_size) ----------

    def _encode_jpeg(self, im):
        # bake EXIF orientation into the pixels: for orientations 5-8 the
        # stored W/H swap -- exactly the DISPLAYED dimensions that
        # image_optimize.read_image_size() injected into the HTML, so the
        # width/height attributes stay consistent
        baked = ImageOps.exif_transpose(im)
        if baked.mode not in ("RGB", "L", "CMYK"):
            return None, 0.0, None          # never touch exotic modes
        kwargs = dict(format="JPEG", quality=self.exp.cfg.image_quality,
                      optimize=True, progressive=True)
        icc = im.info.get("icc_profile")
        if icc:
            kwargs["icc_profile"] = icc     # color fidelity; EXIF/XMP drop
        buf = io.BytesIO()
        baked.save(buf, **kwargs)
        return buf.getvalue(), LOSSY_MIN_GAIN, baked.size

    def _encode_png(self, im):
        icc = im.info.get("icc_profile")
        best = self._png_save(im, icc)
        if im.mode in ("RGB", "RGBA"):
            colors = im.getcolors(256)      # None when > 256 colors
            if colors:
                pal = im.convert("P", palette=Image.ADAPTIVE,
                                 colors=len(colors),
                                 dither=Image.Dither.NONE)
                # only EXACT-lossless reductions count -- prove it by
                # round-tripping (self-disqualifies on any Pillow where
                # RGBA->P is not pixel-perfect)
                if pal.convert(im.mode).tobytes() == im.tobytes():
                    cand = self._png_save(pal, icc)
                    if len(cand) < len(best):
                        best = cand
        return best, LOSSLESS_MIN_GAIN, im.size

    @staticmethod
    def _png_save(im, icc) -> bytes:
        kwargs = dict(format="PNG", optimize=True)
        if icc:
            kwargs["icc_profile"] = icc
        buf = io.BytesIO()
        # no pnginfo: ancillary text/time chunks drop; transparency and
        # palette carry over from im.info via the encoder
        im.save(buf, **kwargs)
        return buf.getvalue()

    def _encode_gif(self, im):
        buf = io.BytesIO()
        # static GIF only (is_animated already handled); optimize=True
        # shrinks the palette; comment extension blocks drop
        im.save(buf, format="GIF", optimize=True)
        return buf.getvalue(), LOSSLESS_MIN_GAIN, im.size

    def _encode_webp(self, im, orig: bytes):
        if orig[12:16] != b"VP8L":
            # lossy (VP8) or extended (VP8X) source: re-encoding adds
            # generational loss for typically marginal gains, and VP8X
            # bundles alpha/animation/lossless combinations -- keep it
            return None, 0.0, None
        kwargs = dict(format="WEBP", lossless=True, quality=100, method=6)
        icc = im.info.get("icc_profile")
        if icc:
            kwargs["icc_profile"] = icc
        buf = io.BytesIO()
        im.save(buf, **kwargs)
        return buf.getvalue(), LOSSLESS_MIN_GAIN, im.size

    # -- bookkeeping ---------------------------------------------------------

    def _bump(self, key: str) -> None:
        with self.exp.stats_lock:
            self.stats[key] += 1

    def summary_lines(self) -> list[str]:
        done = sum(self.stats[k] for k in ("png", "jpeg", "gif", "webp"))
        if not (self.enabled and (done or self.stats["failed"])):
            return []
        return [f"[seo] recompressed: {done} images "
                f"({self.stats['bytes_saved'] // 1024} KB saved, "
                f"{self.stats['kept']} already optimal)"]

    def add_report(self, report: dict, txt_head: list,
                   txt_sections: list) -> None:
        report["seo"]["image_compression"] = self.stats
        txt_head.append(
            f"Recompressed:    {self.stats['png']} PNG, "
            f"{self.stats['jpeg']} JPEG, {self.stats['gif']} GIF, "
            f"{self.stats['webp']} WebP "
            f"({self.stats['bytes_saved'] // 1024} KB saved)"
            if self.enabled else
            "Recompressed:    image recompression disabled")


PLUGIN = ImageCompress
