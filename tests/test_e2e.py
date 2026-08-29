"""End-to-end test: export a small WordPress-like fixture site served by a
local HTTP server, then assert on the generated tree and deploy files."""
import json
import secrets
import shutil
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from bs4 import BeautifulSoup

from test_pure import png_bytes

FIXTURE_SRC = Path(__file__).parent / "fixture"

REDIRECTS = {"/alt/": "/ueber-uns/", "/alt": "/ueber-uns/",
             "/alt2": "/ueber-uns/?ref=alt2", "/alt2/": "/ueber-uns/?ref=alt2",
             # query-only self-redirect (consent/region plugin pattern)
             "/loop": "/loop/?x=1", "/loop/": "/loop/?x=1",
             # slash-STRIPPING canonicalization (no-trailing-slash permalinks)
             "/dienst/": "/dienst"}


def _make_handler(root: Path):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            query = parse_qs(urlsplit(self.path).query)
            if path in ("/", "/page/2/") and query.get("s"):
                # the WordPress search endpoint the search plugin harvests
                # its results-page design from
                self._search_results(query["s"][0], path == "/page/2/")
                return
            if path in REDIRECTS and "?" not in self.path:
                self.send_response(301)
                self.send_header("Location", REDIRECTS[path])
                self.end_headers()
                return
            if path == "/wp-json/sliderrevolution/sliders/2":
                origin = f"http://{self.headers.get('Host')}"
                body = json.dumps({
                    "success": True, "message": "",
                    "slides": {
                        "1": {"id": 1, "slide": {"id": 1}, "layers": {}},
                        "2": {"id": 3, "slide": {"id": 3}, "layers": {
                            "7": {"type": "image", "bg": {"image": {
                                "src": f"{origin}/wp-content/uploads/"
                                       f"slide2.png"}}}}},
                    },
                    "addOns": [],
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type",
                                 "application/json; charset=UTF-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
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
            if path == "/dienst":
                # content served at the slash-LESS URL (see REDIRECTS)
                body = (b'<!doctype html><html><head><meta charset="utf-8">'
                        b'<meta name="viewport" content="width=device-width">'
                        b'<title>Dienst</title></head>'
                        b'<body>Dienstseite ohne Slash</body></html>')
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
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

        # -- the WordPress search endpoint -------------------------------
        # Themed like a real theme: a grid container of card items with
        # post meta, a paginator, and the query echoed in <title>, the
        # page-title <h1> and the breadcrumb leaf -- everything the
        # search plugin harvests its design from.
        HITS = [("/", "Fixture Home", "admin", "2021-11-25T11:05:38+01:00",
                 "25. November 2021", "Startseite mit Fassaden und "
                 "Vollwärmeschutz."),
                ("/ueber-uns/", "Über uns", "admin",
                 "2017-10-05T17:47:25+02:00", "5. Oktober 2017",
                 "Wir über uns: Vollwärmeschutz seit 1950.")]

        def _search_results(self, term, page2):
            origin = f"http://{self.headers.get('Host')}"
            hits = [h for h in self.HITS
                    if term.lower() in (h[1] + " " + h[5]).lower()]
            cards = "".join(
                f'<div class="wf-cell iso-item" data-post-id="{i + 11}" '
                f'data-date="{iso}" data-name="{title}">'
                f'<article class="post no-img post-{i + 11} page hentry">'
                f'<div class="blog-content wf-td">'
                f'<h3 class="entry-title"><a href="{origin}{href}" '
                f'title="{title}" rel="bookmark">{title}</a></h3>'
                f'<div class="entry-meta">'
                f'<a class="author vcard" href="{origin}/author/{who}/" '
                f'rel="author">Von <span class="fn">{who}</span></a>'
                f'<a href="javascript:void(0);" class="data-link">'
                f'<time class="entry-date" datetime="{iso}">{shown}</time>'
                f'</a></div><p>{body}</p>'
                f"</div></article></div>"
                for i, (href, title, who, iso, shown, body)
                in enumerate(hits))
            q = quote(term)
            body = (
                f'<!doctype html><html lang="de"><head><meta charset="utf-8">'
                f"<title>Du hast nach {term} gesucht - Fixture</title>"
                f'<meta property="og:url" content="{origin}/?s={q}">'
                f'<script src="{origin}/wp-includes/js/masonry.min.js">'
                f"</script></head>"
                f'<body class="search search-results layout-masonry">'
                f'<div class="page-title-head hgroup">'
                f"<h1>Suchergebnisse für: <span>{term}</span></h1></div>"
                f'<div class="page-title-breadcrumbs"><ol class="breadcrumbs">'
                f'<li><a href="{origin}/"><span itemprop="name">Start</span>'
                f'</a></li><li class="current"><span itemprop="name">'
                f'Ergebnisse für "{term}"</span></li></ol></div>'
                f'<div class="content" id="content" role="main">'
                + (f'<div class="wf-container iso-container" '
                   f'data-padding="10px" data-cur-page="{2 if page2 else 1}" '
                   f'data-columns="3">{cards}</div>'
                   f'<div class="paginator"><a href="{origin}/page/2/?s={q}" '
                   f'class="page-numbers next">→</a></div>'
                   if hits else
                   f'<article id="post-0" class="post no-results not-found">'
                   f'<h1 class="entry-title">Nichts gefunden</h1>'
                   f"<p>Leider konnten wir nichts finden.</p>"
                   f'<form class="searchform" method="get" '
                   f'action="{origin}/"><input name="s" type="text" '
                   f'value="{term}"></form></article>')
                + "</div></body></html>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
                       ("inline.png", 10, 10), ("notfound.png", 20, 20),
                       ("slide2.png", 400, 300)):
        (uploads / name).write_bytes(png_bytes(w, h))
    (uploads / "dokument.pdf").write_bytes(b"%PDF-1.4\n" + b"stream" * 500)
    # referenced ONLY by the search-results template -- proves the search
    # plugin pulls in assets the crawl itself never sees
    wpi = site / "wp-includes" / "js"
    wpi.mkdir(parents=True)
    (wpi / "masonry.min.js").write_bytes(b"/* masonry */var Masonry=1;\n")

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    out = tmp / "export"
    try:
        # --no-resolve-internal: no real DNS lookups in CI
        code = mod.main([origin, "-o", str(out), "--clean",
                         "--no-resolve-internal"])
    finally:
        server.shutdown()
        server.server_close()       # release the listening socket
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


def test_resource_hints_stripped(export):
    html = (export["public"] / "index.html").read_text(encoding="utf-8")
    assert "dns-prefetch" not in html                    # always stripped
    assert 'rel="preconnect"' in html                    # public font host stays
    assert "fonts.gstatic.com" in html


def test_static_search_exported(export):
    import json as _json
    pub = export["public"]
    # results page at the German path (the fixture declares lang="de")
    page = pub / "search" / "index.html"
    assert page.is_file()
    html = page.read_text(encoding="utf-8")
    assert 'id="wpse-search-results"' in html
    assert 'data-wpse-search="renderer"' in html
    assert 'data-wpse-search="redirect"' not in html     # no redirect loop
    assert "noindex,follow" in html
    # index: real pages with their titles and text, filtered like the sitemap
    index = _json.loads((pub / "search-index.json").read_text("utf-8"))
    assert index["v"] == 2 and index["lang"] == "de"
    paths = [d[0] for d in index["docs"]]
    assert "/" in paths and "/ueber-uns/" in paths
    # noindex keeps a page out of the SITEMAP but not out of the site
    # search -- WordPress' own search returns noindexed pages too
    assert "/geheim/" in paths
    assert "/geheim/" not in (pub / "sitemap.xml").read_text(encoding="utf-8")
    corpus = " ".join(d[3] for d in index["docs"])
    assert "Vollwärmeschutz" in corpus
    # every page's form points at the results page, every page can redirect
    home = (pub / "index.html").read_text(encoding="utf-8")
    assert 'action="/search/"' in home
    assert 'data-wpse-search="redirect"' in home
    assert "location.replace" in home
    # search results are not indexable content
    assert "/search/" not in (pub / "sitemap.xml").read_text(encoding="utf-8")
    stats = export["report"]["seo"]["search"]
    assert stats["page_written"] is True and stats["collision"] is None
    assert stats["path"] == "/search/" and stats["pages_indexed"] >= 2
    assert stats["forms_rewritten"] >= 1


def test_artifact_pages_excluded_but_exported(export):
    """A WordPress attachment page the ORIGIN's own sitemap declares:
    exported (a lightbox may link to it), but never advertised."""
    pub = export["public"]
    assert (pub / "partner" / "logo" / "index.html").is_file()
    xml = (pub / "sitemap.xml").read_text(encoding="utf-8")
    assert "/partner/logo/" not in xml
    assert export["report"]["sitemap"]["excluded_artifacts"] == 1
    index = json.loads((pub / "search-index.json").read_text("utf-8"))
    assert "/partner/logo/" not in [d[0] for d in index["docs"]]
    stats = export["report"]["seo"]["artifact_pages"]
    assert stats["attachment_pages"] == 1 and stats["listed"] is False
    # keeping it on disk is what keeps verification green
    assert export["report"]["verification"]["missing_local_files"] == []


def test_static_search_uses_the_live_design(export):
    """The results page must be the THEME's search page, not our own
    markup: harvested container, harvested card template, harvested
    heading prefix, and the search-only asset pulled in behind it."""
    pub = export["public"]
    html = (pub / "search" / "index.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    stats = export["report"]["seo"]["search"]
    assert stats["page_source"] == "live search page"
    h = stats["harvest"]
    assert h["used"] is True and h["template"] is True
    assert h["probe_requests"] <= 12 and h["pages_with_slots"] >= 1
    # the theme's own grid container carries the renderer's id
    box = soup.find(id="wpse-search-results")
    assert "iso-container" in (box.get("class") or [])
    assert box["data-columns"] == "3"
    assert soup.body["class"][:2] == ["search", "search-results"]
    # the theme's static heading prefix survived verbatim
    h1 = soup.select_one(".page-title-head h1")
    assert h1.get_text().startswith("Suchergebnisse für:")
    assert h1.find(attrs={"data-wpse-search": "term"}) is not None
    assert soup.find(class_="paginator") is None          # stale
    # the card template with its slots rides in the renderer config
    script = soup.find("script", attrs={"data-wpse-search": "renderer"}).string
    assert "wf-cell" in script and "%%U%%" in script and "%%A%%" in script
    assert "Nichts gefunden" in script                    # empty state
    # an asset ONLY the search template references was downloaded
    assert (pub / "wp-includes" / "js" / "masonry.min.js").is_file()


def test_wp_cruft_stripped(export):
    html = (export["public"] / "index.html").read_text(encoding="utf-8")
    for gone in ("generator", "shortlink", "EditURI", "wlwmanifest", "oembed",
                 "api.w.org", "pingback", "cloudflareinsights", "rss+xml",
                 "wordfence_lh", "wfLogHumanRan"):
        assert gone not in html, gone
    assert 'rel="canonical"' in html
    assert 'hreflang="de"' in html
    # the stripped wlwmanifest's target must not have been crawled either
    assert not (export["public"] / "wp-includes" / "wlwmanifest.xml").exists()


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


def test_lazyload_attrs_localized(export):
    html = (export["public"] / "index.html").read_text(encoding="utf-8")
    assert 'data-lazy-src="/wp-content/uploads/plain.png"' in html
    assert ('data-lazy-srcset="/wp-content/uploads/plain.png 1x, '
            '/wp-content/uploads/hero.png 2x"') in html
    assert ("data-bg=\"background-image: "
            "url('/wp-content/uploads/bg.png')\"") in html


def test_vendor_page_skips(export):
    # /cart/ (WooCommerce) and /wpforms-ajax (WPForms) are linked but must
    # never be fetched as pages -- a broken skip surfaces in pages_failed
    # (asserted empty in test_export_self_contained) and as export files
    assert not (export["public"] / "cart").exists()
    assert not (export["public"] / "wpforms-ajax").exists()


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


def test_slash_strip_redirect_no_loop_rule(export):
    # /dienst/ 301s to /dienst on the origin; the export saves the page at
    # /dienst/ and must NOT emit a /dienst/ -> /dienst rule (it would fight
    # the deploy configs' own /a -> /a/ canonicalization into a 301 loop)
    inc = (export["out"] / "redirects.inc").read_text()
    assert "/dienst" not in inc
    page = (export["public"] / "dienst" / "index.html").read_text()
    assert "Dienstseite ohne Slash" in page


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


def test_sr7_slides_hydrated(export):
    html = (export["public"] / "sr7-page" / "index.html").read_text(
        encoding="utf-8")
    # lazy slide 3 got its layers embedded, image localized + downloaded
    assert "/wp-content/uploads/slide2.png" in html
    assert '"title":"Zweite"},"layers":{"7":' in html
    assert '"global":true},"layers":[]' in html      # global slide untouched
    assert (export["public"] / "wp-content" / "uploads" /
            "slide2.png").is_file()
    # nothing under wp-json may exist on disk (policy check stays green)
    assert not (export["public"] / "wp-json").exists()
    assert export["report"]["sr7_hydrated"] == {"SR7_2_1": 1}


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


def test_no_html_comments_left(export):
    html = (export["public"] / "index.html").read_text(encoding="utf-8")
    assert "<!--" not in html                   # incl. IE conditionals
    assert '<html lang="de">' in html           # revealed markup survives


def test_min_files_lose_license_banners(export):
    css = (export["public"] / "wp-content" / "themes" / "fix" /
           "vendor.min.css").read_text()
    assert "vendor banner" not in css
    assert ".vendor{color:blue;margin:0}" in css
    theme = (export["public"] / "wp-content" / "themes" / "fix" /
             "style.css").read_text()
    assert "/*" not in theme                    # no comments at all


def test_inline_module_script_minified(export):
    html = (export["public"] / "index.html").read_text(encoding="utf-8")
    assert "module banner" not in html
    assert "// note" not in html
    assert "const mk=1;" in html
    assert "e2e-pre-comment" not in html        # comments inside <pre> go too


def test_images_recompressed(export, mod):
    stats = export["report"]["seo"]["image_compression"]
    assert stats["failed"] == 0                 # every fixture PNG decodes
    assert stats["animated_skipped"] == 0
    assert stats["png"] >= 1                    # solid-color PNGs shrink
    assert stats["bytes_saved"] > 0
    # replaced files are still valid PNGs with unchanged dimensions
    read = mod.PLUGIN_MODULES["image_optimize"].read_image_size
    up = export["public"] / "wp-content" / "uploads"
    assert read(up / "plain.png") == (320, 200)
    assert read(up / "hero.png") == (640, 480)


def test_svg_comments_stripped(export):
    svg = (export["public"] / "wp-content" / "themes" / "fix" /
           "icon.svg").read_text(encoding="utf-8")
    assert "e2e-svg-comment" not in svg
    assert "keep-me" in svg                     # CDATA content untouched
