"""Image recompression plugin: run_end pass, per-format policy, guards.

Uses Pillow-generated REAL images (Pillow is a hard dependency since
2.3.0). No test pins encoded byte sizes -- only relative comparisons.
"""
import pytest
from PIL import Image


def _exporter(mod, tmp_path, **cfg):
    e = mod.Exporter(mod.Config(base_url="https://example.at",
                                out_dir=tmp_path / "out", **cfg))
    e.public_dir.mkdir(parents=True, exist_ok=True)
    return e


def _gradient(w, h):
    im = Image.new("RGB", (w, h))
    im.putdata([(x % 256, y % 256, (x * y) % 256)
                for y in range(h) for x in range(w)])
    return im


def test_png_recompressed_smaller_and_lossless(mod, tmp_path):
    e = _exporter(mod, tmp_path)
    p = e.public_dir / "big.png"
    im = Image.new("RGB", (256, 256), (200, 30, 30))
    im.save(p, format="PNG", compress_level=1)          # deliberately fat
    before = p.stat().st_size
    mode_before = p.stat().st_mode
    plug = e.plugin("image_compress")
    plug.run_end()
    after = p.stat().st_size
    assert after < before
    assert p.stat().st_mode == mode_before      # not mkstemp's 0600
    with Image.open(p) as out:
        assert out.size == (256, 256)
        assert out.convert("RGB").tobytes() == im.tobytes()   # lossless
    assert plug.stats["png"] == 1
    assert plug.stats["bytes_saved"] == before - after
    assert e.warnings == []


def test_recompression_idempotent(mod, tmp_path):
    # the plugin's own output must never be rewritten again (no-gain guard)
    e1 = _exporter(mod, tmp_path)
    p = e1.public_dir / "img.png"
    Image.new("RGB", (64, 64), (0, 0, 0)).save(p, format="PNG",
                                               compress_level=1)
    e1.plugin("image_compress").run_end()
    once = p.read_bytes()
    e2 = mod.Exporter(mod.Config(base_url="https://example.at",
                                 out_dir=tmp_path / "out"))   # fresh stats
    e2.plugin("image_compress").run_end()
    assert p.read_bytes() == once
    assert e2.plugin("image_compress").stats["kept"] == 1
    assert e2.plugin("image_compress").stats["png"] == 0


def test_jpeg_orientation_baked_dimensions_consistent(mod, tmp_path):
    e = _exporter(mod, tmp_path)
    p = e.public_dir / "photo.jpg"
    im = Image.effect_noise((160, 90), 60).convert("RGB")  # incompressible
    exif = Image.Exif()
    exif[0x0112] = 6                                    # 90-degree rotation
    im.save(p, format="JPEG", quality=100, subsampling=0, exif=exif)
    read = mod.PLUGIN_MODULES["image_optimize"].read_image_size
    assert read(p) == (90, 160)                # DISPLAYED dims pre-pass
    before = p.stat().st_size
    e.plugin("image_compress").run_end()
    assert p.stat().st_size <= before * 0.9    # 10% lossy guard honored
    with Image.open(p) as out:
        assert out.size == (90, 160)                    # baked
        assert out.getexif().get(0x0112) in (None, 1)   # tag gone/neutral
    assert read(p) == (90, 160)      # header parser agrees post-pass ->
                                     # the injected width/height stay valid
    assert e.plugin("image_compress").stats["jpeg"] == 1


def test_animated_gif_untouched(mod, tmp_path):
    e = _exporter(mod, tmp_path)
    p = e.public_dir / "anim.gif"
    # visually distinct frames -- Pillow's GIF writer drops duplicates
    frames = [Image.new("RGB", (32, 32), c)
              for c in ((255, 0, 0), (0, 255, 0))]
    frames[0].save(p, format="GIF", save_all=True,
                   append_images=frames[1:], duration=100)
    before = p.read_bytes()
    e.plugin("image_compress").run_end()
    assert p.read_bytes() == before
    assert e.plugin("image_compress").stats["animated_skipped"] == 1


def test_corrupt_image_warns_not_crashes(mod, tmp_path):
    e = _exporter(mod, tmp_path)
    (e.public_dir / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\nGARBAGE")
    e.plugin("image_compress").run_end()                 # must not raise
    assert e.plugin("image_compress").stats["failed"] == 1
    assert any("broken.png" in w for w in e.warnings)


def test_lossless_webp_repacked(mod, tmp_path):
    e = _exporter(mod, tmp_path)
    p = e.public_dir / "pic.webp"
    im = _gradient(256, 256)
    im.save(p, format="WEBP", lossless=True, quality=0, method=0)  # fat
    before = p.stat().st_size
    e.plugin("image_compress").run_end()
    assert p.stat().st_size < before
    with Image.open(p) as out:
        assert out.convert("RGB").tobytes() == im.tobytes()   # still exact
    assert e.plugin("image_compress").stats["webp"] == 1


def test_lossy_webp_kept(mod, tmp_path):
    e = _exporter(mod, tmp_path)
    p = e.public_dir / "lossy.webp"
    _gradient(64, 64).save(p, format="WEBP", quality=80)
    before = p.read_bytes()
    e.plugin("image_compress").run_end()
    assert p.read_bytes() == before
    assert e.plugin("image_compress").stats["kept"] == 1


def test_disabled_by_flag_and_no_rewrite(mod, tmp_path):
    e = _exporter(mod, tmp_path, compress_images=False)
    assert not e.plugin("image_compress").enabled
    e2 = mod.Exporter(mod.Config(base_url="https://example.at",
                                 out_dir=tmp_path / "o2", rewrite=False))
    assert not e2.plugin("image_compress").enabled


# -- the minify-missing-libs idiom, for Pillow ------------------------------

def test_missing_pillow_fails_cli(mod, monkeypatch, capsys):
    monkeypatch.setattr(mod.PLUGIN_MODULES["image_compress"], "Image", None)
    with pytest.raises(SystemExit):
        mod.parse_args(["https://example.at"])
    assert "Pillow" in capsys.readouterr().err
    cfg = mod.parse_args(["https://example.at", "--no-compress-images"])
    assert cfg.compress_images is False


def test_missing_pillow_warns_programmatic(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(mod.PLUGIN_MODULES["image_compress"], "Image", None)
    e = _exporter(mod, tmp_path)
    plug = e.plugin("image_compress")
    assert not plug.enabled                             # graceful skip
    plug.run_start()
    assert any("Pillow" in w for w in e.warnings)
    plug.run_end()                                      # no-op, no crash


def test_image_quality_bounds(mod):
    with pytest.raises(SystemExit):
        mod.parse_args(["https://example.at", "--image-quality", "0"])
    with pytest.raises(SystemExit):
        mod.parse_args(["https://example.at", "--image-quality", "96"])
    cfg = mod.parse_args(["https://example.at", "--image-quality", "70"])
    assert cfg.image_quality == 70
