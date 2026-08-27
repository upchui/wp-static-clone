"""End-to-end test: export a small WordPress-like fixture site served by a
local HTTP server, then assert on the generated tree and deploy files."""
import json
import secrets
import shutil
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from test_pure import png_bytes

FIXTURE_SRC = Path(__file__).parent / "fixture"

REDIRECTS = {"/alt/": "/ueber-uns/", "/alt": "/ueber-uns/",
             "/alt2": "/ueber-uns/?ref=alt2", "/alt2/": "/ueber-uns/?ref=alt2",
             # query-only self-redirect (consent/region plugin pattern)
             "/loop": "/loop/?x=1", "/loop/": "/loop/?x=1"}


def _make_handler(root: Path):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in REDIRECTS and "?" not in self.path:
                self.send_response(301)
                self.send_header("Location", REDIRECTS[path])
                self.end_headers()
                return
            if path in ("/download/9", "/download/9/"):
                # dynamic download endpoint (Download Monitor pattern)
                body = b"%PDF-1.4\nvollmacht"
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header(
                    "Content-Disposition",
                    'attachment; filename="Vollmacht-04.2021.pdf"; '
                    "filename*=UTF-8''Vollmacht-04.2021.pdf")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path in ("/dynamic", "/dynamic/"):
                # uniqid()-style noise fresh on every request -- must be
                # classified "same", not "dynamic", by the mobile check
                body = (f'<!doctype html><html><head><meta charset="utf-8">'
                        f'<meta name="viewport" content="width=device-width">'
                        f'<title>Dyn</title></head><body>'
                        f'<div id="ultimate-heading-{secrets.token_hex(9)}">'
                        f'Inhalt</div></body></html>').encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path.endswith("/"):
                path += "index.html"
            target = root / path.lstrip("/")
            if not target.is_file():
                body = (root / "404-body.html").read_bytes()
                self.send_response(404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.path = "/" + path.lstrip("/")
            super().do_GET()

        def log_message(self, *a):
            pass

    return Handler


@pytest.fixture(scope="session")
def export(mod, tmp_path_factory):
    """Serve the fixture, run a full export once, yield the paths."""
    tmp = tmp_path_factory.mktemp("e2e")
    site = tmp / "site"
    shutil.copytree(FIXTURE_SRC, site)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(site))
    port = server.server_address[1]
    origin = f"http://127.0.0.1:{port}"

    for f in site.rglob("*"):
        if f.suffix in (".html", ".xml", ".txt", ".css"):
            text = f.read_text(encoding="utf-8")
            text = (text.replace("__ESCAPED_ORIGIN__",
                                 origin.replace("/", r"\/"))
                        .replace("__ORIGIN__", origin))
            f.write_text(text, encoding="utf-8")
    uploads = site / "wp-content" / "uploads"
    uploads.mkdir(parents=True)
    for name, w, h in (("hero.png", 640, 480), ("plain.png", 320, 200),
                       ("lazyplugin.png", 100, 50), ("bg.png", 10, 10),
                       ("inline.png", 10, 10), ("notfound.png", 20, 20)):
        (uploads / name).write_bytes(png_bytes(w, h))
    (uploads / "dokument.pdf").write_bytes(b"%PDF-1.4\n" + b"stream" * 500)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    out = tmp / "export"
    try:
        code = mod.main([origin, "-o", str(out), "--clean"])
    finally:
        server.shutdown()
    assert code == 0
    return {"out": out, "public": out / "public", "origin": origin,
            "report": json.loads((out / "report.json").read_text())}


def test_export_self_contained(export):
    assert (export["public"] / "index.html").is_file()
    verification = export["report"]["verification"]
    assert verification["missing_local_files"] == []
    assert verification["unexpected_absolute_refs"] == []
    assert export["report"]["pages_failed"] == []
    assert export["report"]["asset_errors"] == []


def test_css_charset_and_localization(export):
    css = (export["public"] / "wp-content" / "themes" / "fix" /
           "style.css").read_text(encoding="utf-8")
    assert "→ü" in css                          # no latin-1 mojibake (A2)
    assert "url('/wp-content/uploads/bg.png')" in css


def test_svg_viewbox_preserved(export):
    html = (export["public"] / "index.html").read_text(encoding="utf-8")
    assert 'viewBox="0 0 24 24"' in html        # A3
    assert 'preserveAspectRatio="xMidYMid meet"' in html


def test_external_lookalike_untouched(export):
    html = (export["public"] / "index.html").read_text(encoding="utf-8")
    assert "https://example.athletic-shop.de/produkt" in html


def test_wp_cruft_stripped(export):
    html = (export["public"] / "index.html").read_text(encoding="utf-8")
    for gone in ("generator", "shortlink", "EditURI", "oembed",
                 "api.w.org", "pingback", "cloudflareinsights", "rss+xml"):
        assert gone not in html, gone
    assert 'rel="canonical"' in html
    assert 'hreflang="de"' in html


def test_seo_urls_keep_origin(export):
    html = (export["public"] / "index.html").read_text(encoding="utf-8")
    origin = export["origin"]
    assert f'href="{origin}/"' in html                       # canonical
    assert f'content="{origin}/"' in html                    # og:url
    assert origin.replace("://", r":\/\/") in html           # JSON-LD


def test_images_optimized(export):
    html = (export["public"] / "index.html").read_text(encoding="utf-8")
    assert 'loading="lazy"' in html
    assert 'width="320"' in html and 'height="200"' in html  # plain.png


def test_generated_sitemap(export):
    xml = (export["public"] / "sitemap.xml").read_text()
    origin = export["origin"]
    assert f"<loc>{origin}/</loc>" in xml
    assert f"<loc>{origin}/ueber-uns/</loc>" in xml
    assert "/geheim/" not in xml                # noindex (B5)
    assert "/alt/" not in xml                   # redirect source
    assert "/dynamic/" not in xml               # link-only (B5)


def test_robots_points_at_generated_sitemap(export):
    robots = (export["public"] / "robots.txt").read_text()
    assert f"Sitemap: {export['origin']}/sitemap.xml" in robots
    assert "sitemap_index.xml" not in robots


def test_redirects_keep_query(export):
    inc = (export["out"] / "redirects.inc").read_text()
    # queryless target: NO trailing ? -- visitor args (?utm_...) must be
    # forwarded by nginx
    assert 'rewrite "^/alt/$" "/ueber-uns/" permanent;' in inc
    assert 'rewrite "^/alt2/$" "/ueber-uns/?ref=alt2?" permanent;' in inc
    ht = (export["public"] / ".htaccess").read_text()
    assert 'RedirectMatch 301 "^/alt2/$" "/ueber-uns/?ref=alt2"' in ht


def test_query_self_redirect_no_loop(export):
    # /loop/ 301s to /loop/?x=1 on the origin -- the export must contain the
    # real page and NO rule (a rule would 301-loop forever)
    inc = (export["out"] / "redirects.inc").read_text()
    assert "/loop" not in inc
    page = (export["public"] / "loop" / "index.html").read_text()
    assert "Echter Inhalt der Loop-Seite" in page
    xml = (export["public"] / "sitemap.xml").read_text()
    assert f"<loc>{export['origin']}/loop/</loc>" in xml


def test_query_redirect_different_path_gets_stub(export):
    # /alt2/ redirects onto /ueber-uns/?ref=alt2 -- a noindex stub, never a
    # duplicate copy of the target page
    stub = (export["public"] / "alt2" / "index.html").read_text()
    assert 'content="noindex"' in stub
    assert "url=/ueber-uns/?ref=alt2" in stub
    assert "Fixture" not in stub                # no copied page body


def test_origin_sitemap_301(export):
    inc = (export["out"] / "redirects.inc").read_text()
    assert 'rewrite "^/sitemap_index\\.xml$" "/sitemap.xml" permanent;' in inc


def test_media_streamed(export):
    pdf = export["public"] / "wp-content" / "uploads" / "dokument.pdf"
    assert pdf.read_bytes().startswith(b"%PDF-1.4")


def test_download_endpoint_materialized(export):
    f = export["public"] / "download" / "9" / "Vollmacht-04.2021.pdf"
    assert f.read_bytes().startswith(b"%PDF-1.4")
    # link rewritten onto the file, cache-buster query gone
    html = (export["public"] / "index.html").read_text(encoding="utf-8")
    assert 'href="/download/9/Vollmacht-04.2021.pdf"' in html
    assert "tmstv" not in html
    # old endpoint URLs 301 onto the file
    inc = (export["out"] / "redirects.inc").read_text()
    assert ('rewrite "^/download/9/$" "/download/9/Vollmacht-04.2021.pdf" '
            "permanent;") in inc
    assert '"^/download/9$"' in inc
    assert export["report"]["downloads"] == {
        "/download/9/": "/download/9/Vollmacht-04.2021.pdf"}


def test_redirect_stub_noindexed(export):
    stub = (export["public"] / "alt" / "index.html").read_text()
    assert 'content="noindex"' in stub
    assert 'http-equiv="refresh"' in stub


def test_404_page_and_its_assets(export):
    assert (export["public"] / "404.html").is_file()
    # referenced ONLY by the 404 page -> proves the capture_404 asset
    # mini-crawl ran (B2)
    assert (export["public"] / "wp-content" / "uploads" /
            "notfound.png").is_file()


def test_mobile_check_ignores_uniqid_noise(export):
    mobile = export["report"]["mobile"]
    assert mobile["pages_with_per_request_dynamic_content"] == []  # B7
    assert mobile["pages_with_different_mobile_html"] == []


def test_deploy_files_present(export):
    out = export["out"]
    for name in ("nginx.conf", "redirects.inc", "Dockerfile",
                 "docker-compose.yml", "server.sh", ".dockerignore"):
        assert (out / name).is_file(), name
    for name in ("_redirects", ".htaccess"):
        assert (export["public"] / name).is_file(), name
