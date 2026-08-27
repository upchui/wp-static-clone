"""Exporter unit tests -- constructed against a fictitious host, no network."""

from bs4 import BeautifulSoup

from test_pure import png_bytes


# -- URL classification / localization --------------------------------------

def test_lookalike_domain_untouched(exporter):
    text = ('see https://example.athletic.de/shop and '
            'https://example.at/page plus https://example.company/x')
    out = exporter.localize_text(text)
    assert "https://example.athletic.de/shop" in out
    assert "https://example.company/x" in out
    assert "/page" in out and "https://example.at/page" not in out
    assert not exporter.host_probe_re.search("https://example.athletic.de/x")


def test_foreign_port_untouched(exporter):
    assert (exporter.localize_text("https://example.at:8443/x")
            == "https://example.at:8443/x")


def test_localize_all_three_spellings(exporter):
    assert exporter.localize_text("https://www.example.at/a?b=1") == "/a?b=1"
    assert exporter.localize_text("//example.at/x") == "/x"
    assert exporter.localize_text(r"https:\/\/example.at\/x\/y") == r"\/x\/y"
    assert exporter.localize_text("https://example.at") == "/"


def test_idn_both_spellings(mod, tmp_path):
    e = mod.Exporter(mod.Config(base_url="https://müller.de",
                                out_dir=tmp_path / "o"))
    assert e.base_norm == "xn--mller-kva.de"
    assert e.is_internal("https://www.müller.de/a")
    assert e.is_internal("https://xn--mller-kva.de/a")
    assert e.localize_text("https://müller.de/a") == "/a"
    assert e.localize_text("https://xn--mller-kva.de/a") == "/a"
    hosts = e._sub_filter_hosts()
    assert "xn--mller-kva.de" in hosts and "müller.de" in hosts


def test_retarget(mod, tmp_path):
    e = mod.Exporter(mod.Config(base_url="https://example.at",
                                out_dir=tmp_path / "o",
                                target_domain="neu.example"))
    assert (e.retarget_text(r'{"u":"https:\/\/example.at\/x"}')
            == r'{"u":"https:\/\/neu.example\/x"}')
    assert e.retarget_url("https://example.at/p/") == "https://neu.example/p/"
    assert e.retarget_url("https://other.tld/p/") == "https://other.tld/p/"


def test_normalize_page_url(exporter):
    n = exporter.normalize_page_url
    assert n("https://example.at/über-uns") == n("https://example.at/über-uns/")
    assert n("https://example.at/x?p=1") is None
    assert "https://example.at/x?p=1" in exporter.skipped_query_urls
    exporter.skipped_query_urls.clear()
    assert n("https://example.at/y?p=2", record_skips=False) is None
    assert not exporter.skipped_query_urls
    assert n("https://example.at/wp-admin/") is None
    assert n("https://example.at/wp-content/dir") is None


def test_local_path_for_traversal_jail(exporter):
    # ../ segments are normalized away -- the result must stay inside public/
    jailed = exporter.local_path_for(
        "https://example.at/../../etc/passwd", is_page=False)
    assert jailed is not None
    jailed.relative_to(exporter.public_dir.resolve())    # raises on escape
    ok = exporter.local_path_for("https://example.at/a/b/", is_page=True)
    assert ok is not None and ok.name == "index.html"


# -- HTML rewriting ---------------------------------------------------------

CRUFT_HTML = """<html><head>
<meta name="generator" content="WordPress 6.5">
<link rel="canonical" href="https://example.at/">
<link rel="alternate" hreflang="de" href="https://example.at/">
<link rel="alternate" type="application/rss+xml" href="https://example.at/feed/">
<link rel="alternate" type="application/json+oembed" href="https://example.at/wp-json/oembed/">
<link rel="https://api.w.org/" href="https://example.at/wp-json/">
<link rel="EditURI" href="https://example.at/xmlrpc.php?rsd">
<link rel="shortlink" href="https://example.at/?p=1">
<link rel="pingback" href="https://example.at/xmlrpc.php">
<link rel="stylesheet" href="https://example.at/s.css">
<script src="https://static.cloudflareinsights.com/beacon.min.js/v123"></script>
</head><body></body></html>"""


def test_strip_wp_cruft(exporter):
    soup = BeautifulSoup(CRUFT_HTML, "html.parser")
    exporter.strip_wp_cruft(soup)
    out = str(soup)
    for gone in ("generator", "api.w.org", "EditURI", "shortlink",
                 "pingback", "oembed", "rss+xml", "cloudflareinsights"):
        assert gone not in out, gone
    assert 'rel="canonical"' in out
    assert 'hreflang="de"' in out
    assert 'rel="stylesheet"' in out


def test_rewrite_keeps_canonical_localizes_assets(exporter):
    soup = BeautifulSoup(CRUFT_HTML, "html.parser")
    exporter.rewrite_soup_relative(soup)
    out = str(soup)
    assert 'href="https://example.at/"' in out          # canonical absolute
    assert 'href="/s.css"' in out                       # stylesheet localized


def test_serialize_soup_preserves_viewbox(mod):
    soup = BeautifulSoup(
        '<div><svg viewBox="0 0 10 10" preserveAspectRatio="none">'
        '<feGaussianBlur stdDeviation="2"></feGaussianBlur></svg></div>',
        "html.parser")
    out = mod.serialize_soup(soup).decode()
    assert 'viewBox="0 0 10 10"' in out
    assert 'preserveAspectRatio="none"' in out
    assert 'stdDeviation="2"' in out


def test_optimize_images(exporter):
    (exporter.public_dir / "wp-content").mkdir(parents=True)
    (exporter.public_dir / "wp-content" / "a.png").write_bytes(
        png_bytes(320, 200))
    soup = BeautifulSoup(
        '<img src="/wp-content/first.png">'
        '<noscript><img src="/wp-content/ns.png"></noscript>'
        '<img data-src="/wp-content/lazy.png" class="lazyload">'
        '<img src="/wp-content/a.png">', "html.parser")
    changed = exporter._optimize_images(soup)
    assert changed
    imgs = soup.find_all("img")
    first, ns, lazy, plain = imgs
    assert not first.has_attr("loading")                # LCP protection
    assert not ns.has_attr("loading")                   # noscript untouched
    assert not lazy.has_attr("loading")                 # plugin lazyload
    assert plain["loading"] == "lazy"
    assert plain["decoding"] == "async"
    assert (plain["width"], plain["height"]) == ("320", "200")
    assert exporter.img_stats["missing"] == 1           # first.png not on disk


def test_inject_staging_meta(exporter):
    soup = BeautifulSoup("<html><head><title>x</title></head></html>",
                         "html.parser")
    assert exporter._inject_staging_meta(soup)
    assert soup.find("meta", attrs={"name": "robots"})["content"] == "noindex"
    soup2 = BeautifulSoup('<head><meta name="robots" content="noindex"></head>',
                          "html.parser")
    assert not exporter._inject_staging_meta(soup2)


# -- sitemap / robots / deploy files ----------------------------------------

def _page(mod, url, source="sitemap", **kw):
    rec = mod.PageRecord(url=url, source=source,
                         content_type="text/html", status=200)
    for k, v in kw.items():
        setattr(rec, k, v)
    return rec


def test_generated_sitemap_filters(mod, exporter):
    exporter.public_dir.mkdir(parents=True, exist_ok=True)
    exporter.sitemap_discovery_ok = True
    exporter.pages = [
        _page(mod, "https://example.at/"),
        _page(mod, "https://example.at/geheim/", noindex=True),
        _page(mod, "https://example.at/attachment/", source="link"),
        _page(mod, "https://example.at/alt/",
              save_url="https://example.at/neu/"),
        _page(mod, "https://example.at/err/", error="HTTP 500"),
    ]
    exporter.write_generated_sitemap()
    xml = (exporter.public_dir / "sitemap.xml").read_text()
    assert "https://example.at/</loc>" in xml
    assert "https://example.at/neu/" in xml             # redirect target
    assert "/geheim/" not in xml                        # noindex
    assert "/attachment/" not in xml                    # link-only
    assert "/alt/" not in xml and "/err/" not in xml
    assert exporter.generated_sitemap["excluded_link_discovered"] == 1


def test_generated_sitemap_empty_not_written(mod, exporter):
    exporter.public_dir.mkdir(parents=True, exist_ok=True)
    exporter.pages = [_page(mod, "https://example.at/x/", error="boom")]
    exporter.write_generated_sitemap()
    assert not (exporter.public_dir / "sitemap.xml").exists()
    assert exporter.generated_sitemap is None
    assert any("NOT generated" in w for w in exporter.warnings)


def test_finalize_robots(mod, exporter):
    exporter.public_dir.mkdir(parents=True, exist_ok=True)
    (exporter.public_dir / "robots.txt").write_text(
        "User-agent: *\nDisallow: /wp-admin/\n"
        "Sitemap: https://example.at/old_index.xml\n")
    exporter.generated_sitemap = {"url_count": 1}
    exporter.finalize_robots()
    text = (exporter.public_dir / "robots.txt").read_text()
    assert "old_index.xml" not in text
    assert "Sitemap: https://example.at/sitemap.xml" in text
    assert "Disallow: /wp-admin/" in text


def test_finalize_robots_staging(mod, tmp_path):
    e = mod.Exporter(mod.Config(base_url="https://example.at",
                                out_dir=tmp_path / "o", staging=True))
    e.public_dir.mkdir(parents=True)
    e.finalize_robots()
    assert (e.public_dir / "robots.txt").read_text() == \
        "User-agent: *\nDisallow: /\n"


def test_deploy_files(mod, tmp_path):
    e = mod.Exporter(mod.Config(base_url="https://example.at",
                                out_dir=tmp_path / "o", staging=True))
    e.public_dir.mkdir(parents=True)
    (e.public_dir / "404.html").write_text("nope")
    e.redirects = [
        {"from": "https://example.at/alt", "to": "https://example.at/neu/?r=1",
         "status": 301},
        {"from": "https://example.at/x", "to": "https://example.at/x/",
         "status": 301},
        {"from": "https://example.at/bad", "to": "https://example.at/$var",
         "status": 301},
    ]
    e.write_deploy_files()
    out = e.cfg.out_dir

    inc = (out / "redirects.inc").read_text()
    assert 'rewrite "^/alt$" "/neu/?r=1?" permanent;' in inc
    assert "$var" not in inc                    # unsafe rule skipped
    assert "/x/" not in inc                     # trailing-slash rule covers it

    conf = (out / "nginx.conf").read_text()
    assert "$served_host" in conf and "sub_filter_once off;" in conf
    assert "charset utf-8;" in conf and "server_tokens off;" in conf
    assert "location = /404.html { internal; }" in conf
    # staging header must ALSO sit in the asset location block
    # (add_header inheritance is all-or-nothing)
    asset_block = conf.split("location ~*")[1]
    assert "X-Robots-Tag" in asset_block
    assert "nosniff" in asset_block

    ht = (e.public_dir / ".htaccess").read_text()
    assert 'RedirectMatch 301 "^/alt$" "/neu/?r=1"' in ht
    assert "Redirect 301 " not in ht            # no prefix-matching directive
    assert "X-Robots-Tag" in ht

    nl = (e.public_dir / "_redirects").read_text()
    assert "/alt /neu/?r=1 301" in nl

    compose = (out / "docker-compose.yml").read_text()
    assert "healthcheck:" in compose
    lines = compose.splitlines()
    assert "name: wpstatic-example-at" in lines          # top-level project name
    assert "    name: wpstatic-example-at" in lines      # named default network
    assert 'COMPOSE_PROJECT_NAME="wpstatic-example-at"' in \
        (out / "server.sh").read_text()
    assert (out / ".dockerignore").is_file()
    assert '"${2:-${PORT:-' in (out / "server.sh").read_text()


def test_deploy_files_no_404(mod, tmp_path):
    e = mod.Exporter(mod.Config(base_url="https://example.at",
                                out_dir=tmp_path / "o"))
    e.public_dir.mkdir(parents=True)
    e.write_deploy_files()
    conf = (e.cfg.out_dir / "nginx.conf").read_text()
    assert "error_page 404" not in conf
    assert "ErrorDocument" not in (e.public_dir / ".htaccess").read_text()
    assert any("no 404.html" in w for w in e.warnings)


def test_safe_redirect_rules_keeps_query(exporter):
    exporter.redirects = [
        {"from": "https://example.at/a", "to": "https://example.at/b?x=1",
         "status": 301},
        {"from": "https://example.at/c?p=1", "to": "https://example.at/d",
         "status": 301},
    ]
    rules = exporter._safe_redirect_rules()
    assert rules == [("/a", "/b?x=1")]          # query kept; query-source dropped


# -- v1.3.0: redirect regressions, ports, robots, exclude, streaming --------

def test_query_self_redirect_emits_no_rule(exporter):
    exporter.redirects = [
        {"from": "https://example.at/a/", "to": "https://example.at/a/?x=1",
         "status": 301},
        {"from": "https://example.at/alt", "to": "https://example.at/neu/",
         "status": 301},
    ]
    assert exporter._safe_redirect_rules() == [("/alt", "/neu/")]


def test_redirect_rule_suffix_only_with_query(mod, tmp_path):
    e = mod.Exporter(mod.Config(base_url="https://example.at",
                                out_dir=tmp_path / "o"))
    e.public_dir.mkdir(parents=True)
    e.redirects = [
        {"from": "https://example.at/q", "to": "https://example.at/z/?r=1",
         "status": 301},
        {"from": "https://example.at/p", "to": "https://example.at/z/",
         "status": 301},
    ]
    e.write_deploy_files()
    inc = (e.cfg.out_dir / "redirects.inc").read_text()
    assert 'rewrite "^/q$" "/z/?r=1?" permanent;' in inc     # own query: no args
    assert 'rewrite "^/p$" "/z/" permanent;' in inc          # visitor args survive


def test_default_ports_internal(exporter):
    assert exporter.is_internal("https://example.at:443/x")
    assert exporter.is_internal("http://example.at:80/x")
    assert not exporter.is_internal("https://example.at:8443/x")
    assert exporter.localize_text("https://example.at:443/x") == "/x"


def test_loc_prefix_forwarded_proto(mod, tmp_path):
    e = mod.Exporter(mod.Config(
        base_url="http://10.0.0.5:8080", host_header="example.at",
        extra_headers={"X-Forwarded-Proto": "https"}, out_dir=tmp_path / "o"))
    assert e._loc_prefix() == "https://example.at"
    e2 = mod.Exporter(mod.Config(base_url="http://127.0.0.1:8123",
                                 out_dir=tmp_path / "o2"))
    assert e2._loc_prefix() == "http://127.0.0.1:8123"


def test_policy_check_exact_names_below_top_level(mod, tmp_path):
    e = mod.Exporter(mod.Config(base_url="https://example.at",
                                out_dir=tmp_path / "o"))
    up = e.public_dir / "wp-content" / "uploads"
    up.mkdir(parents=True)
    (up / "wp-login-screenshot.png").write_bytes(b"x")       # innocent
    (up / "wp-json").mkdir()                                 # real artifact
    (e.public_dir / "wp-login.php").write_bytes(b"x")        # top-level prefix
    (e.public_dir / "index.html").write_text("<html></html>")
    e.verify_export()
    flagged = {v["file"] for v in e.verify_unexpected}
    assert "wp-login.php" in flagged
    assert "wp-content/uploads/wp-json" in flagged
    assert not any("screenshot" in f for f in flagged)


def test_origin_sitemap_paths_get_301(mod, tmp_path):
    e = mod.Exporter(mod.Config(base_url="https://example.at",
                                out_dir=tmp_path / "o"))
    e.public_dir.mkdir(parents=True)
    e.generated_sitemap = {"url_count": 1}
    e.origin_sitemap_paths = ["/sitemap_index.xml", "/page-sitemap.xml"]
    e.write_deploy_files()
    inc = (e.cfg.out_dir / "redirects.inc").read_text()
    assert 'rewrite "^/sitemap_index\\.xml$" "/sitemap.xml" permanent;' in inc
    assert 'rewrite "^/page\\-sitemap\\.xml$" "/sitemap.xml" permanent;' in inc
    assert "/sitemap_index.xml /sitemap.xml 301" in \
        (e.public_dir / "_redirects").read_text()


def test_write_bytes_returns_written_path(exporter, tmp_path):
    exporter.public_dir.mkdir(parents=True, exist_ok=True)
    t = exporter.public_dir / "a.txt"
    assert exporter.write_bytes(t, b"x") == t
    assert exporter.write_bytes(t, b"y") is None            # first write wins
    d = exporter.public_dir / "dir"
    d.mkdir()
    assert exporter.write_bytes(d, b"z") == d / "index.html"


def test_exclude_patterns(mod, tmp_path):
    e = mod.Exporter(mod.Config(base_url="https://example.at",
                                out_dir=tmp_path / "o",
                                excludes=[r"^/members/", r"\.zip$"]))
    assert e.normalize_page_url("https://example.at/members/x/") is None
    assert e.normalize_page_url("https://example.at/blog/") is not None
    assert e.is_excluded("https://example.at/dl/a.zip")
    assert "/members/x/" in e.excluded_urls


def test_respect_robots_rules(mod, tmp_path):
    e = mod.Exporter(mod.Config(base_url="https://example.at",
                                out_dir=tmp_path / "o", respect_robots=True))
    e._parse_robots("User-agent: Googlebot\nDisallow: /only-google/\n\n"
                    "User-agent: *\nDisallow: /privat/\nAllow: /privat/ok/\n"
                    "Crawl-delay: 2\n")
    assert e.robots_crawl_delay == 2.0
    assert e.is_excluded("/privat/x")
    assert not e.is_excluded("/privat/ok/y")                # longest match wins
    assert not e.is_excluded("/only-google/x")              # other UA group


def test_write_stream(exporter):
    exporter.public_dir.mkdir(parents=True, exist_ok=True)

    class FakeStream:
        closed = False
        def iter_content(self, chunk_size):
            yield b"%PDF-1.4 "
            yield b"data" * 1000
        def close(self):
            self.closed = True

    target = exporter.public_dir / "doc.pdf"
    resp = FakeStream()
    assert exporter.write_stream(target, resp)
    assert resp.closed
    assert target.read_bytes().startswith(b"%PDF-1.4")
    assert not exporter.write_stream(target, FakeStream())  # first write wins


# -- v1.3.1: dynamic download endpoints -------------------------------------

def test_attachment_filename(mod):
    fn = mod.attachment_filename
    assert fn('attachment; filename="Vollmacht-04.2021.pdf"',
              "application/pdf", "/download/9/") == "Vollmacht-04.2021.pdf"
    # RFC 5987 wins, percent-decoded
    assert fn("attachment; filename=\"x.pdf\"; "
              "filename*=UTF-8''Ma%C3%9Fe.pdf",
              "application/pdf", "/download/9/") == "Maße.pdf"
    # traversal-safe
    assert fn('attachment; filename="../../etc/passwd"',
              "", "/download/9/") == "passwd"
    # fallback: path segment + guessed extension
    assert fn("", "application/pdf", "/download/1668/") == "1668.pdf"
    assert fn("", "", "/") == "download"


def test_rewrite_download_links(exporter):
    exporter.download_map = {"/download/9/": "/download/9/Vollmacht.pdf"}
    soup = BeautifulSoup(
        '<a href="/download/9/?tmstv=123">a</a>'
        '<a href="/download/9">b</a>'
        '<a href="/download/10/">c</a>'
        '<a href="https://extern.tld/download/9/">d</a>', "html.parser")
    assert exporter._rewrite_download_links(soup)
    links = [a["href"] for a in soup.find_all("a")]
    assert links == ["/download/9/Vollmacht.pdf", "/download/9/Vollmacht.pdf",
                     "/download/10/", "https://extern.tld/download/9/"]


def test_download_map_redirect_rules(mod, tmp_path):
    e = mod.Exporter(mod.Config(base_url="https://example.at",
                                out_dir=tmp_path / "o"))
    e.public_dir.mkdir(parents=True)
    e.download_map = {"/download/9/": "/download/9/Vollmacht.pdf"}
    e.write_deploy_files()
    inc = (e.cfg.out_dir / "redirects.inc").read_text()
    assert 'rewrite "^/download/9/$" "/download/9/Vollmacht.pdf" permanent;' in inc
    assert 'rewrite "^/download/9$" "/download/9/Vollmacht.pdf" permanent;' in inc
    assert "/download/9/ /download/9/Vollmacht.pdf 301" in \
        (e.public_dir / "_redirects").read_text()
    assert 'RedirectMatch 301 "^/download/9/$" "/download/9/Vollmacht.pdf"' in \
        (e.public_dir / ".htaccess").read_text()


def test_save_download(mod, tmp_path):
    e = mod.Exporter(mod.Config(base_url="https://example.at",
                                out_dir=tmp_path / "o"))
    e.public_dir.mkdir(parents=True)

    class FakeResp:
        headers = {"Content-Disposition": 'attachment; filename="a b.pdf"',
                   "Content-Type": "application/pdf"}
        content = b"%PDF-1.4"

    path = e._save_download("https://example.at/download/9/", FakeResp())
    assert path == "/download/9/a b.pdf" or path == "/download/9/a%20b.pdf"
    assert (e.public_dir / "download" / "9" / "a b.pdf").read_bytes() == b"%PDF-1.4"
    # URL with extension: untouched
    assert e._save_download("https://example.at/x.pdf", FakeResp()) is None


# -- v1.4.0: SR7 slide hydration --------------------------------------------

SR7_HTML = """<sr7-module data-id="2" id="SR7_2_1"></sr7-module>
<script>window.SR7 = window.SR7 || {}; SR7.JSON = SR7.JSON || {};
SR7.JSON['SR7_2_1'] = {"slides":{"1":{"slide":{"id":1},"layers":{"5":{"x":1}}},"3":{"slide":{"id":3},"layers":[]},"9":{"slide":{"id":9,"global":true},"layers":[]}}};
</script>"""


def _sr7_exporter(mod, tmp_path, response):
    e = mod.Exporter(mod.Config(base_url="https://example.at",
                                out_dir=tmp_path / "o"))
    calls = []

    class FakeResp:
        ok = response is not None
        status_code = 200 if response is not None else 404
        is_redirect = is_permanent_redirect = False

        def json(self):
            return response

    def fake_fetch(url, **kw):
        calls.append(url)
        return FakeResp()

    e.fetch = fake_fetch
    return e, calls


def test_hydrate_sr7_merges_lazy_slides(mod, tmp_path):
    resp = {"success": True, "slides": {
        "1": {"slide": {"id": 1}, "layers": {}},
        "2": {"slide": {"id": 3}, "layers": {
            "7": {"bg": {"image": {"src": "https://example.at/s2.png"}}}}},
    }}
    e, calls = _sr7_exporter(mod, tmp_path, resp)
    soup = BeautifulSoup(SR7_HTML, "html.parser")
    e._hydrate_sr7(soup)
    out = str(soup)
    assert '"slide":{"id":3},"layers":{"7":' in out       # lazy slide filled
    assert '"layers":{"5":{"x":1}}' in out                # slide 1 untouched
    assert '"global":true},"layers":[]' in out            # global untouched
    assert e.sr7_hydrated == {"SR7_2_1": 1}
    assert calls == ["https://example.at/wp-json/sliderrevolution/"
                     "sliders/2?srengine=7"]
    # second page with the same slider: served from the cache
    e._hydrate_sr7(BeautifulSoup(SR7_HTML, "html.parser"))
    assert len(calls) == 1


def test_hydrate_sr7_rest_failure_warns(mod, tmp_path):
    e, calls = _sr7_exporter(mod, tmp_path, None)         # 404
    soup = BeautifulSoup(SR7_HTML, "html.parser")
    e._hydrate_sr7(soup)
    assert '"layers":[]' in str(soup)                     # blob unchanged
    assert e.sr7_hydrated == {}
    assert any("freeze" in w for w in e.warnings)


def test_hydrate_sr7_noop_without_lazy_slides(mod, tmp_path):
    e, calls = _sr7_exporter(mod, tmp_path, {"success": True, "slides": {}})
    html = SR7_HTML.replace('"3":{"slide":{"id":3},"layers":[]},', "")
    e._hydrate_sr7(BeautifulSoup(html, "html.parser"))
    assert calls == []                                    # no wp-json request


# -- v1.4.1: private origin IP handling -------------------------------------

def test_connect_address_all_port_spellings_internal(mod, tmp_path):
    e = mod.Exporter(mod.Config(base_url="http://10.11.16.10:81",
                                host_header="www.example.at",
                                out_dir=tmp_path / "o"))
    assert e.is_internal("http://10.11.16.10:81/x")
    assert e.is_internal("http://10.11.16.10/x")          # portless (WP :80)
    assert e.localize_text("http://10.11.16.10/wp-content/u/x.jpg") \
        == "/wp-content/u/x.jpg"
    assert e.localize_text("http://10.11.16.10:81/wp-content/u/x.jpg") \
        == "/wp-content/u/x.jpg"
    # a DIFFERENT private IP stays foreign
    assert e.localize_text("http://10.99.0.1/x.jpg") == "http://10.99.0.1/x.jpg"
    assert not e.is_internal("http://10.99.0.1/x.jpg")


def test_verify_flags_foreign_private_urls(mod, tmp_path):
    e = mod.Exporter(mod.Config(base_url="http://127.0.0.1:8123",
                                out_dir=tmp_path / "o"))
    e.public_dir.mkdir(parents=True)
    (e.public_dir / "index.html").write_text(
        '<html><head><link rel="canonical" href="http://127.0.0.1:8123/">'
        '</head><body>'
        '<script>var s={"img":"http://10.99.0.1/wp-content/x.jpg"};</script>'
        "</body></html>")
    e.verify_export()
    refs = [v["ref"] for v in e.verify_unexpected]
    assert any("10.99.0.1" in r and "Local Network Access" in r for r in refs)
    # the site's own (loopback) canonical is NOT flagged
    assert not any("127.0.0.1" in r for r in refs)
