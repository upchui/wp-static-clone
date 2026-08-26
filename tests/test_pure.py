"""Unit tests for the module-level pure helpers (no network, no Exporter)."""
import struct
import zlib


# -- image byte builders ----------------------------------------------------

def png_bytes(w, h):
    def chunk(typ, data):
        c = typ + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00" + b"\x00" * 3 * w))
            + chunk(b"IEND", b""))


def gif_bytes(w, h):
    return b"GIF89a" + struct.pack("<HH", w, h) + b"\x00" * 10


def jpeg_bytes(w, h, orientation=None):
    out = b"\xff\xd8"
    if orientation is not None:
        tiff = (b"II" + struct.pack("<H", 42) + struct.pack("<I", 8)
                + struct.pack("<H", 1)
                + struct.pack("<HHI", 0x0112, 3, 1)
                + struct.pack("<HH", orientation, 0))
        exif = b"Exif\x00\x00" + tiff
        out += b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif
    sof = struct.pack(">BHHB", 8, h, w, 3)
    out += b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof
    return out


def webp_vp8x(w, h):
    body = (b"WEBPVP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00"
            + struct.pack("<I", w - 1)[:3] + struct.pack("<I", h - 1)[:3])
    return b"RIFF" + struct.pack("<I", len(body)) + body


def webp_vp8l(w, h):
    bits = (w - 1) | ((h - 1) << 14)
    body = (b"WEBPVP8L" + struct.pack("<I", 5) + b"\x2f"
            + struct.pack("<I", bits))
    return b"RIFF" + struct.pack("<I", len(body)) + body


def webp_vp8(w, h):
    body = (b"WEBPVP8 " + struct.pack("<I", 10) + b"\x00\x00\x00"
            + b"\x9d\x01\x2a" + struct.pack("<HH", w, h))
    return b"RIFF" + struct.pack("<I", len(body)) + body


# -- read_image_size --------------------------------------------------------

def test_read_image_size(mod, tmp_path):
    cases = [("a.png", png_bytes(320, 200), (320, 200)),
             ("a.gif", gif_bytes(10, 20), (10, 20)),
             ("a.jpg", jpeg_bytes(200, 100), (200, 100)),
             ("x.webp", webp_vp8x(64, 32), (64, 32)),
             ("l.webp", webp_vp8l(33, 17), (33, 17)),
             ("v.webp", webp_vp8(48, 24), (48, 24))]
    for name, data, expect in cases:
        p = tmp_path / name
        p.write_bytes(data)
        assert mod.read_image_size(p) == expect, name


def test_read_image_size_exif_orientation_swaps(mod, tmp_path):
    p = tmp_path / "rot.jpg"
    p.write_bytes(jpeg_bytes(200, 100, orientation=6))
    assert mod.read_image_size(p) == (100, 200)
    p.write_bytes(jpeg_bytes(200, 100, orientation=1))
    assert mod.read_image_size(p) == (200, 100)


def test_read_image_size_garbage(mod, tmp_path):
    p = tmp_path / "t.png"
    p.write_bytes(png_bytes(10, 10)[:12])          # truncated
    assert mod.read_image_size(p) is None
    p2 = tmp_path / "t.jpg"
    p2.write_bytes(b"\xff\xd8\xff")                # truncated JPEG
    assert mod.read_image_size(p2) is None
    p3 = tmp_path / "t.svg"
    p3.write_bytes(b"<svg/>")
    assert mod.read_image_size(p3) is None
    assert mod.read_image_size(tmp_path / "missing.png") is None


# -- canon_path / canon_ref -------------------------------------------------

def test_canon_path_collapses_spellings(mod):
    nfc = mod.canon_path("/wp/%C3%A4rzte/")
    assert mod.canon_path("/wp/ärzte/") == nfc
    assert mod.canon_path("/wp/ärzte/") == nfc          # NFD
    assert mod.canon_path(nfc) == nfc                         # idempotent


def test_canon_path_keeps_template_chars(mod):
    assert mod.canon_path("/x/{banner_id}/*") == "/x/{banner_id}/*"


def test_canon_ref_earliest_separator(mod):
    assert mod.canon_ref("/page#frag?x=1") == "/page#frag?x=1"
    assert mod.canon_ref("/page?x=1#frag") == "/page?x=1#frag"
    assert mod.canon_ref("") == "/"


# -- norm_host / host_spellings ---------------------------------------------

def test_norm_host(mod):
    assert mod.norm_host("WWW.Example.AT") == "example.at"
    assert mod.norm_host("example.at:8080") == "example.at:8080"
    assert mod.norm_host("müller.de") == "xn--mller-kva.de"


def test_host_spellings(mod):
    assert mod.host_spellings("example.at") == {"example.at"}
    assert mod.host_spellings("xn--mller-kva.de") == {
        "xn--mller-kva.de", "müller.de"}


# -- try_b64_url / decode_cfemail -------------------------------------------

def test_try_b64_url(mod):
    import base64
    enc = base64.b64encode(b"/wp-content/x.jpg").decode()
    assert mod.try_b64_url(enc) == "/wp-content/x.jpg"
    assert mod.try_b64_url(base64.b64encode(b"not a url!").decode()) is None
    assert mod.try_b64_url("short") is None
    assert mod.try_b64_url("") is None


def test_decode_cfemail(mod):
    key = 0x42
    email = "a@b.at"
    enc = f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in email)
    assert mod.decode_cfemail(enc) == email
    assert mod.decode_cfemail("zz") is None
    assert mod.decode_cfemail("00") is None


# -- normalize_html (mobile-check comparison) -------------------------------

def test_normalize_html_ignores_wp_noise(mod):
    a = mod.normalize_html(
        b'<div id="ultimate-heading-13076a8ee702062f3">'
        b'<!-- Page generated in 0.42s -->'
        b'<link href="/a.css?ver=1787653111">'
        b'<script src="/b.js?v=414eb1150da600f432d59790220db2ca"></script>'
        b'<input name="_wpnonce" value="ab12cd34">'
        b'<div id="Info-box-wrap-9913" data-target="#Info-box-wrap-9913">'
        b'<i data="nonce":"abcdef123456"></i>')
    b = mod.normalize_html(
        b'<div id="ultimate-heading-99f9e8d7c6b5a4321">'
        b'<!-- Page generated in 0.55s -->'
        b'<link href="/a.css?ver=999999">'
        b'<script src="/b.js?v=ffffffffffffffffffffffffffffffff"></script>'
        b'<input name="_wpnonce" value="ff99ee88">'
        b'<div id="Info-box-wrap-8869" data-target="#Info-box-wrap-8869">'
        b'<i data="nonce":"999999aaaaaa"></i>')
    assert a == b


def test_normalize_html_detects_real_difference(mod):
    a = mod.normalize_html(b"<h1>Desktop</h1>")
    b = mod.normalize_html(b"<h1>Mobile</h1>")
    assert a != b


# -- decode_text_asset / robots_rule_re -------------------------------------

class _FakeResp:
    def __init__(self, content, ctype):
        self.content = content
        self.headers = {"Content-Type": ctype}


def test_decode_text_asset_quoted_charset(mod):
    r = _FakeResp("a{content:'ü'}".encode("iso-8859-15"),
                  'text/css; charset="iso-8859-15"')
    assert "ü" in mod.decode_text_asset(r)
    r2 = _FakeResp("a{content:'→'}".encode("utf-8"), "text/css")
    assert "→" in mod.decode_text_asset(r2)


def test_robots_rule_re(mod):
    assert mod.robots_rule_re("/privat/").match("/privat/x")
    assert not mod.robots_rule_re("/privat/").match("/x/privat/")
    assert mod.robots_rule_re("/*.zip$").match("/dl/a.zip")
    assert not mod.robots_rule_re("/*.zip$").match("/dl/a.zipx")
