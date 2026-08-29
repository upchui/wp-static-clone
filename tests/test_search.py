"""Static site search plugin: extraction helpers, index, results page,
form/JSON-LD/head wiring, collisions."""
import json
import re

import pytest
import requests
from bs4 import BeautifulSoup


PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="description" content="W&auml;rmeschutzfassaden und mehr">
<meta property="og:site_name" content="Huthansl">
<title>Fassaden - Huthansl</title>
<script type="application/ld+json">{"@context":"https://schema.org",
"@graph":[{"@type":"WebSite","url":"https://example.at/","potentialAction":
[{"@type":"SearchAction","target":{"@type":"EntryPoint","urlTemplate":
"https://example.at/?s={search_term_string}"},"query-input":
"required name=search_term_string"}]}]}</script>
</head>
<body class="page">
<a class="skip-link screen-reader-text" href="#content">Zum Inhalt</a>
<div class="masthead"><div class="top-bar">Telefon</div>
<ul id="primary-menu"><li><a href="/">Startseite</a></li></ul>
<form role="search" class="searchform" method="get" action="/">
<label class="screen-reader-text" for="s1">Suche:</label>
<input id="s1" class="field" name="s" type="text" value="">
<input class="searchsubmit" type="submit" value="Los!">
<a class="submit" href=""><svg viewBox="0 0 16 16"></svg></a></form></div>
<div class="page-title"><div class="page-title-head hgroup"><h1>Fassaden</h1>
</div><div class="page-title-breadcrumbs"><ol class="breadcrumbs">
<li><a href="/"><span itemprop="name">Start</span></a></li>
<li class="current"><span itemprop="name">Fassaden</span></li></ol></div></div>
<div class="content" id="content" role="main" style="min-height:500px;
text-align:center">
<article class="post"><p>Vollw&auml;rmeschutz f&uuml;r die Stra&szlig;e.</p>
<!-- ein Kommentar --><script>var x = "nicht indexieren";</script>
<div class="sharing-buttons">Teilen</div></article></div>
<footer id="footer"><p>Fu&szlig;zeile</p></footer>
<div id="cmplz-cookiebanner-container"><p>Cookies</p></div>
</body></html>
"""

ORIGIN_PAGE = b"<html><body>Echte Seite der Website</body></html>"


SEARCH_PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<title>Du hast nach %(term)s gesucht - Huthansl</title>
<meta property="og:url" content="https://example.at/?s=%(term)s">
<script src="https://example.at/wp-includes/js/masonry.min.js"></script>
</head><body class="search search-results layout-masonry">
<div class="page-title-head hgroup"><h1>Suchergebnisse für: <span>%(term)s</span></h1></div>
<div class="page-title-breadcrumbs"><ol class="breadcrumbs">
<li><a href="https://example.at/"><span itemprop="name">Start</span></a></li>
<li class="current"><span itemprop="name">Ergebnisse für "%(term)s"</span></li>
</ol></div>
<div class="content" id="content" role="main">%(body)s</div>
<script>var cfg={"page_location":"https://example.at/?s=%(term)s"};</script>
</body></html>"""

CARD = """<div class="wf-cell iso-item" data-post-id="%(id)s" data-date="%(iso)s" data-name="%(title)s">
<article class="post no-img post-%(id)s page hentry"><div class="blog-content wf-td">
<h3 class="entry-title"><a href="https://example.at%(href)s" title="%(title)s" rel="bookmark">%(title)s</a></h3>
<div class="entry-meta"><a class="author vcard" href="https://example.at/author/admin/" rel="author">Von <span class="fn">admin</span></a><a href="javascript:void(0);" title="11:05" class="data-link"><time class="entry-date" datetime="%(iso)s">%(shown)s</time></a></div><p>%(excerpt)s</p>
</div></article></div>"""

RESULTS_BODY = (
    '<div class="wf-container iso-container" data-padding="10px" '
    'data-cur-page="1" data-columns="3">'
    + CARD % {"id": "11", "href": "/fassaden/", "title": "Fassaden",
              "iso": "2021-11-25T11:05:38+01:00", "shown": "25. November 2021",
              "excerpt": "Vollwärmeschutz für die Straße."}
    + CARD % {"id": "12", "href": "/kontakt/", "title": "Kontakt",
              "iso": "2017-10-05T17:47:25+02:00", "shown": "5. Oktober 2017",
              "excerpt": "Rennweg 81, Brunn"}
    + '</div><div class="paginator"><a href="https://example.at/page/2/?s=x" '
      'class="page-numbers next">→</a></div>')

EMPTY_BODY = ('<article id="post-0" class="post no-results not-found">'
              '<h1 class="entry-title">Nichts gefunden</h1>'
              '<p>Leider konnten wir nichts finden.</p>'
              '<form class="searchform" method="get" '
              'action="https://example.at/"><input name="s" value="x">'
              '</form></article>')


class _Resp:
    """The fetch() response shape off_site_redirect() needs."""

    def __init__(self, body: str, status=200, ctype="text/html"):
        self.content = body.encode("utf-8")
        self.status_code = status
        self.ok = 200 <= status < 300
        self.headers = {"Content-Type": ctype}
        self.url = "https://example.at/"
        self.is_redirect = self.is_permanent_redirect = False
        self.history = []


CORPUS = "Fassaden Vollwärmeschutz für die Straße Kontakt Rennweg 81 Brunn"


def live_search():
    """A fake origin that answers /?s=<term> like WordPress does: a themed
    results page for terms its content actually contains, the theme's
    'nothing found' block for everything else."""
    def fetch(url, headers=None, **kw):
        from urllib.parse import parse_qs, urlsplit
        term = (parse_qs(urlsplit(url).query).get("s") or [""])[0]
        body = (RESULTS_BODY if term and term.lower() in CORPUS.lower()
                else EMPTY_BODY)
        return _Resp(SEARCH_PAGE % {"term": term, "body": body})
    return fetch


@pytest.fixture
def plug(mod, exporter):
    exporter.public_dir.mkdir(parents=True, exist_ok=True)

    def no_network(url, *a, **kw):
        raise AssertionError(f"unexpected network request: {url}")
    exporter.fetch = no_network          # tests opt in via live_search()
    exporter.cfg.search_harvest = False  # ... and via search_harvest
    return exporter.plugin("search")


def _mod(mod):
    return mod.PLUGIN_MODULES["search"]


def _page(mod, url, **kw):
    rec = mod.PageRecord(url=url, source="sitemap",
                         content_type="text/html", status=200)
    for k, v in kw.items():
        setattr(rec, k, v)
    return rec


# -- pure helpers ------------------------------------------------------------

def test_content_root_prefers_the_main_well(mod):
    s = _mod(mod)
    soup = BeautifulSoup(PAGE, "html.parser")
    root = s.content_root(soup)
    assert root.get("id") == "content"          # via [role=main]
    # a page without any known container falls back to None (caller uses body)
    bare = BeautifulSoup("<html><body><p>Hi</p></body></html>", "html.parser")
    assert s.content_root(bare) is None


def test_extract_text_drops_chrome(mod):
    s = _mod(mod)
    soup = BeautifulSoup(PAGE, "html.parser")
    text = s.extract_text(s.content_root(soup), 2000)
    assert text == "Vollwärmeschutz für die Straße."
    for gone in ("Telefon", "Startseite", "Zum Inhalt", "Fußzeile",
                 "Cookies", "nicht indexieren", "ein Kommentar", "Teilen",
                 "Suche:", "Los!"):
        assert gone not in text, gone


def test_extract_text_does_not_mutate_the_soup(mod):
    s = _mod(mod)
    soup = BeautifulSoup(PAGE, "html.parser")
    before = str(soup)
    s.extract_text(s.content_root(soup), 2000)
    assert str(soup) == before                  # crawl still needs this tree


def test_extract_text_truncates_and_normalizes(mod):
    s = _mod(mod)
    soup = BeautifulSoup(
        "<div id='content'><p>a  b\n\tc</p><p>d</p></div>", "html.parser")
    assert s.extract_text(s.content_root(soup), 2000) == "a b c d"
    assert s.extract_text(s.content_root(soup), 3) == "a b"   # rstripped


def test_drop_class_regex_matches_whole_tokens_only(mod):
    rx = _mod(mod).DROP_CLASS_RE
    for keep in ("innovation", "content", "entry-content", "navi-box",
                 "wf-wrap", "post", "menu-card-page"):
        assert not rx.fullmatch(keep), keep
    for drop in ("menu", "sub-menu", "main-navigation", "screen-reader-text",
                 "breadcrumbs", "widget", "mini-widgets", "top-bar",
                 "bottom-bar", "masthead", "cmplz-cookiebanner", "pswp__bg",
                 "comments", "page-title-head"):
        assert rx.fullmatch(drop), drop


def test_path_for_lang(mod):
    s = _mod(mod)
    assert s.path_for_lang("de") == "/suche/"
    assert s.path_for_lang("de-AT") == "/suche/"
    assert s.path_for_lang("DE_at") == "/suche/"
    assert s.path_for_lang("en-US") == "/search/"
    assert s.path_for_lang("") == "/search/"


def test_validate_search_path(mod):
    v = _mod(mod).validate_search_path
    assert v("/suche/") == "/suche/"
    assert v(" /hilfe/suche/ ") == "/hilfe/suche/"
    for bad in ("", "/", "suche/", "/suche", "/suche/?x=1", "/suche/#a",
                "/../etc/", "/a//b/", "/wp-content/suche/", '/su"che/',
                "/su che/", "/su\\che/"):
        with pytest.raises(ValueError):
            v(bad)


def test_title_suffix_and_strip(mod):
    s = _mod(mod)
    titles = ["Fassaden - Huthansl", "Kontakt - Huthansl",
              "Huthansl Bau GmbH in Wien"]          # homepage: no suffix
    suffix = s.title_suffix(titles, "Huthansl")
    assert suffix == " - Huthansl"
    assert s.strip_title_suffix("Fassaden - Huthansl", suffix) == "Fassaden"
    assert s.strip_title_suffix("Huthansl Bau GmbH in Wien",
                                suffix) == "Huthansl Bau GmbH in Wien"
    assert s.strip_title_suffix(" - Huthansl", suffix) == " - Huthansl"
    assert s.title_suffix(titles, "") == ""


def test_fix_search_action_all_shapes(mod):
    fix = _mod(mod).fix_search_action
    data = {"@graph": [
        {"@type": "WebSite", "potentialAction": [
            {"@type": "SearchAction",
             "target": {"@type": "EntryPoint",
                        "urlTemplate": "https://x.at/?s={search_term_string}"}}
        ]},
        {"@type": "SearchAction", "target": "https://x.at/?s={q}"},
        {"@type": ["Thing", "SearchAction"], "target": ["https://x.at/?s={q}"]},
        {"@type": "ReadAction", "target": ["https://x.at/keep/"]},
    ]}
    assert fix(data, "/suche/?s={search_term_string}") == 3
    g = data["@graph"]
    assert (g[0]["potentialAction"][0]["target"]["urlTemplate"]
            == "https://x.at/suche/?s={search_term_string}")
    assert g[1]["target"] == "https://x.at/suche/?s={search_term_string}"
    assert g[2]["target"] == ["https://x.at/suche/?s={search_term_string}"]
    assert g[3]["target"] == ["https://x.at/keep/"]     # untouched


def test_dump_jsonld_escapes_slashes_and_angle_brackets(mod):
    out = _mod(mod).dump_jsonld({"u": "https://x.at/a/", "t": "</script>"})
    assert r"https:\/\/x.at\/a\/" in out
    assert "</script>" not in out and "\\u003c" in out
    assert json.loads(out)["t"] == "</script>"          # still valid JSON


# -- collection --------------------------------------------------------------

def test_pre_discover_collects(mod, exporter, plug):
    soup = BeautifulSoup(PAGE, "html.parser")
    plug.pre_discover_soup(soup, "https://example.at/fassaden/")
    entry = plug.docs["https://example.at/fassaden/"]
    assert entry["lang"] == "de"
    assert entry["site"] == "Huthansl"
    assert entry["desc"] == "Wärmeschutzfassaden und mehr"
    assert entry["text"] == "Vollwärmeschutz für die Straße."
    # the 404 capture is never indexed
    plug.pre_discover_soup(soup, "https://example.at/404.html")
    assert "https://example.at/404.html" not in plug.docs


def test_pre_discover_never_raises(mod, exporter, plug, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("x")
    monkeypatch.setattr(_mod(mod), "extract_text", boom)
    plug.pre_discover_soup(BeautifulSoup(PAGE, "html.parser"),
                           "https://example.at/x/")
    assert plug.docs == {}
    assert any("search index: skipped" in w for w in exporter.warnings)


# -- wiring ------------------------------------------------------------------

def test_postprocess_wires_forms_jsonld_and_head(mod, exporter, plug):
    exporter.pages = [_page(mod, "https://example.at/fassaden/",
                            title="Fassaden - Huthansl")]
    plug.pre_discover_soup(BeautifulSoup(PAGE, "html.parser"),
                           "https://example.at/fassaden/")
    soup = BeautifulSoup(PAGE, "html.parser")
    assert plug.postprocess_soup(soup) is True
    # form
    form = soup.find("form")
    assert form["action"] == "/suche/" and form["method"] == "get"
    assert form.find("input", attrs={"name": "s"}) is not None   # kept
    assert form.find("a", class_="submit")["href"] == "/suche/"  # decoy fixed
    # JSON-LD
    ld = soup.find("script", attrs={"type": "application/ld+json"}).string
    assert r"\/suche\/?s={search_term_string}" in ld
    assert json.loads(ld)["@graph"][0]["potentialAction"][0]["target"][
        "urlTemplate"] == "https://example.at/suche/?s={search_term_string}"
    # head redirect, right after the charset meta, before anything else
    snippet = soup.find("script", attrs={"data-wpse-search": "redirect"})
    assert snippet is not None
    assert snippet.find_previous_sibling().get("charset") == "utf-8"
    assert 'var p="/suche/"' in snippet.string
    assert "location.replace(p+location.search)" in snippet.string
    assert "example.at" not in snippet.string        # stays root-relative
    # idempotent: a second pass changes nothing and adds no second snippet
    before = str(soup)
    assert plug.postprocess_soup(soup) is False
    assert str(soup) == before


def test_english_site_uses_search_path(mod, exporter, plug):
    exporter.pages = [_page(mod, "https://example.at/about/", title="About")]
    plug.pre_discover_soup(
        BeautifulSoup('<html lang="en-US"><head></head><body>'
                      '<main id="content"><p>Hello world</p></main>'
                      '</body></html>', "html.parser"),
        "https://example.at/about/")
    assert plug.settings()["path"] == "/search/"
    assert plug.settings()["de"] is False


def test_cli_search_path_overrides_language(mod, tmp_path):
    cfg = mod.parse_args(["https://example.at", "-o", str(tmp_path),
                          "--search-path", "/finden/"])
    assert cfg.search_path == "/finden/"
    e = mod.Exporter(cfg)
    e.public_dir.mkdir(parents=True, exist_ok=True)
    e.pages = [_page(mod, "https://example.at/x/", title="X")]
    e.plugin("search").pre_discover_soup(
        BeautifulSoup('<html lang="de"><head></head><body></body></html>',
                      "html.parser"), "https://example.at/x/")
    st = e.plugin("search").settings()
    assert st["path"] == "/finden/" and st["de"] is True   # UI stays German


def test_cli_rejects_bad_search_path(mod, tmp_path, capsys):
    with pytest.raises(SystemExit):
        mod.parse_args(["https://example.at", "-o", str(tmp_path),
                        "--search-path", "suche"])
    assert "--search-path" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        mod.parse_args(["https://example.at", "-o", str(tmp_path),
                        "--search-max-chars", "-1"])


# -- run_end -----------------------------------------------------------------

def _prepare(mod, exporter, plug, with_404=True):
    exporter.sitemap_discovery_ok = True
    exporter.pages = [
        _page(mod, "https://example.at/fassaden/",
              title="Fassaden - Huthansl"),
        _page(mod, "https://example.at/kontakt/", title="Kontakt - Huthansl"),
        _page(mod, "https://example.at/geheim/", title="Geheim", noindex=True),
        _page(mod, "https://example.at/anhang/", title="Anhang",
              source="link"),
        _page(mod, "https://example.at/alt/", title="Alt",
              save_url="https://example.at/fassaden/", is_stub=True),
        _page(mod, "https://example.at/err/", title="Err", error="HTTP 500"),
    ]
    plug.pre_discover_soup(BeautifulSoup(PAGE, "html.parser"),
                           "https://example.at/fassaden/")
    plug.pre_discover_soup(
        BeautifulSoup('<html lang="de"><head>'
                      '<meta property="og:site_name" content="Huthansl">'
                      '<title>Kontakt - Huthansl</title></head><body>'
                      '<main id="content"><p>Rennweg 81, Brunn</p></main>'
                      '</body></html>', "html.parser"),
        "https://example.at/kontakt/")
    for url in ("https://example.at/geheim/", "https://example.at/anhang/"):
        plug.pre_discover_soup(
            BeautifulSoup('<html lang="de"><body><main id="content">'
                          '<p>geheim</p></main></body></html>',
                          "html.parser"), url)
    if with_404:
        soup = BeautifulSoup(PAGE, "html.parser")
        plug.postprocess_soup(soup)         # 404.html is wired like any page
        (exporter.public_dir / "404.html").write_bytes(
            exporter.serialize(soup))


def test_run_end_writes_index_with_sitemap_filter(mod, exporter, plug):
    _prepare(mod, exporter, plug)
    plug.run_end()
    data = json.loads(
        (exporter.public_dir / "search-index.json").read_text("utf-8"))
    assert data["v"] == 2 and data["lang"] == "de"
    paths = [d[0] for d in data["docs"]]
    assert paths == ["/fassaden/", "/kontakt/"]
    assert "/geheim/" not in paths          # noindex
    assert "/anhang/" not in paths          # link-only
    assert "/alt/" not in paths             # redirect stub
    assert "/err/" not in paths             # failed
    doc = data["docs"][0]
    assert doc[1] == "Fassaden"             # site suffix stripped
    assert doc[2] == "Wärmeschutzfassaden und mehr"
    assert doc[3] == "Vollwärmeschutz für die Straße."
    assert plug.stats["pages_indexed"] == 2
    assert plug.stats["index_bytes"] > 0


def test_run_end_generates_results_page_from_404(mod, exporter, plug):
    _prepare(mod, exporter, plug)
    plug.run_end()
    page = exporter.public_dir / "suche" / "index.html"
    assert page.is_file()
    html = page.read_text("utf-8")
    soup = BeautifulSoup(html, "html.parser")
    # results container + renderer, no leftover redirect snippet (loop guard)
    assert soup.find(id="wpse-search-results") is not None
    assert soup.find("script", attrs={"data-wpse-search": "renderer"})
    assert soup.find("script", attrs={"data-wpse-search": "redirect"}) is None
    robots = soup.find("meta", attrs={"name": "robots"})
    assert robots["content"] == "noindex,follow"
    assert (soup.find("link", rel="canonical")["href"]
            == "https://example.at/suche/")
    assert soup.title.get_text() == "Suche - Huthansl"
    # theme chrome survived, 404 markers gone
    assert "primary-menu" in html and "Startseite" in html
    assert "error404" not in (soup.body.get("class") or [])
    assert set(soup.body.get("class")[:2]) == {"search", "search-results"}
    # page-title h1 and breadcrumb leaf relabelled
    assert (soup.select_one(".page-title-head h1").get_text()
            == "Suchergebnisse")
    assert (soup.select("ol.breadcrumbs li")[-1].get_text(strip=True)
            == "Suchergebnisse")
    # centered 404 styling removed, min-height kept
    assert "text-align" not in (soup.find(id="content").get("style") or "")
    assert "min-height" in soup.find(id="content")["style"]
    # the theme's header search form came along with the chrome and was
    # already wired during the page pass -- it points here
    assert soup.find("form")["action"] == "/suche/"
    # both UI string sets ship; the config picks German at runtime, and no
    # origin host leaks into our script
    script = soup.find("script", attrs={"data-wpse-search": "renderer"}).string
    assert "Keine Ergebnisse" in script and "No results for" in script
    assert '"de":true' in script.replace(" ", "")
    assert "example.at" not in script
    assert '"idx":"/search-index.json"' in script.replace(" ", "")
    assert '"tpl":""' in script.replace(" ", "")     # no harvest here
    assert plug.stats["page_written"] is True
    assert plug.stats["page_source"] == "404.html"
    assert plug.stats["harvest"]["used"] is False
    assert exporter.verify_missing == []


def test_results_page_falls_back_to_minimal_markup(mod, exporter, plug):
    _prepare(mod, exporter, plug, with_404=False)
    plug.run_end()
    html = (exporter.public_dir / "suche" / "index.html").read_text("utf-8")
    soup = BeautifulSoup(html, "html.parser")
    assert soup.html["lang"] == "de"
    assert soup.find(id="wpse-search-results") is not None
    assert (soup.find("meta", attrs={"name": "robots"})["content"]
            == "noindex,follow")
    assert soup.find("h1").get_text() == "Suchergebnisse"
    assert soup.find("form")["action"] == "/suche/"
    assert plug.stats["page_source"] == "built-in minimal page"
    assert any("minimal built-in markup" in w for w in exporter.warnings)


def test_results_page_gets_its_own_form_when_template_has_none(mod, exporter,
                                                               plug):
    """A bare origin 404 page carries no search box -- a results page you
    cannot search again from would be a dead end."""
    _prepare(mod, exporter, plug, with_404=False)
    (exporter.public_dir / "404.html").write_bytes(
        b'<html lang="de"><head><meta charset="utf-8"><title>404</title>'
        b'</head><body><div id="content" role="main"><p>Nichts</p></div>'
        b'</body></html>')
    plug.run_end()
    soup = BeautifulSoup(
        (exporter.public_dir / "suche" / "index.html").read_text("utf-8"),
        "html.parser")
    form = soup.find(id="wpse-search").find("form")
    assert form["action"] == "/suche/" and form["method"] == "get"
    assert form.find("input", attrs={"name": "s"}) is not None


def test_results_page_does_not_duplicate_an_existing_form(mod, exporter,
                                                          plug):
    _prepare(mod, exporter, plug)                   # PAGE has a header form
    plug.run_end()
    soup = BeautifulSoup(
        (exporter.public_dir / "suche" / "index.html").read_text("utf-8"),
        "html.parser")
    assert len(soup.find_all("form")) == 1


def test_results_page_falls_back_to_homepage(mod, exporter, plug):
    _prepare(mod, exporter, plug, with_404=False)
    (exporter.public_dir / "index.html").write_bytes(
        b'<html lang="de"><head><meta charset="utf-8"><title>Home</title>'
        b'</head><body class="home"><div id="header">Chrome</div>'
        b'<main id="content"><p>Startseiteninhalt</p></main></body></html>')
    plug.run_end()
    html = (exporter.public_dir / "suche" / "index.html").read_text("utf-8")
    assert "Chrome" in html                     # chrome inherited
    assert "Startseiteninhalt" not in html      # content well emptied
    assert plug.stats["page_source"] == "index.html (homepage)"


def test_existing_page_at_search_path_stands_the_feature_down(mod, exporter,
                                                              plug):
    """Origin content always wins -- and then nothing may be wired to it."""
    target = exporter.public_dir / "suche" / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(ORIGIN_PAGE)
    exporter.written_paths.add(target)
    _prepare(mod, exporter, plug, with_404=False)
    plug.run_end()
    assert target.read_bytes() == ORIGIN_PAGE
    assert plug.stats["page_written"] is False
    assert plug.stats["collision"] == "/suche/"
    assert not (exporter.public_dir / "search-index.json").exists()
    assert any("--search-path" in w for w in exporter.warnings)
    # no page may be pointed at a results page that does not exist
    soup = BeautifulSoup(PAGE, "html.parser")
    assert plug.postprocess_soup(soup) is False
    assert soup.find("form")["action"] == "/"
    assert soup.find("script", attrs={"data-wpse-search": "redirect"}) is None
    assert exporter.verify_missing == []        # nothing broken, just off


def test_existing_search_index_stands_the_feature_down(mod, exporter, plug):
    target = exporter.public_dir / "search-index.json"
    target.write_bytes(b'{"origin":true}')
    exporter.written_paths.add(target)
    _prepare(mod, exporter, plug, with_404=False)
    plug.run_end()
    assert target.read_bytes() == b'{"origin":true}'
    assert plug.stats["collision"] == "/search-index.json"
    assert not (exporter.public_dir / "suche").exists()
    assert any("/search-index.json" in w for w in exporter.warnings)


def test_mixed_languages_warn_and_pick_the_homepage(mod, exporter, plug):
    exporter.pages = [_page(mod, "https://example.at/", title="Home"),
                      _page(mod, "https://example.at/en/", title="Home EN")]
    for url, lang in (("https://example.at/", "en"),
                      ("https://example.at/en/", "de")):
        plug.pre_discover_soup(
            BeautifulSoup(f'<html lang="{lang}"><body><main id="content">'
                          f'<p>x</p></main></body></html>', "html.parser"),
            url)
    assert plug.settings()["path"] == "/search/"      # homepage wins the tie
    assert any("more than one language" in w for w in exporter.warnings)


# -- harvesting the live results-page design ---------------------------------

def test_probe_term_helpers(mod):
    s = _mod(mod)
    assert s.fold_word("Straße") == "strasse"
    assert s.fold_word("ÜBER") == "uber"
    assert s.doc_words("Fassaden, Vollwärmeschutz!") == {"fassaden",
                                                        "vollwarmeschutz"}
    assert s.word_forms("Vollwärmeschutz")["vollwarmeschutz"] == \
        "Vollwärmeschutz"                       # probe uses the real spelling
    # rare: unique across documents, longest wins
    docs = [{"haus", "vollwarmeschutz"}, {"haus", "kontakt"}]
    assert s.rare_term(docs, []) == "vollwarmeschutz"
    assert s.rare_term([], ["Datenschutzerklärung"]) == "datenschutzerklarung"
    assert s.rare_term([], []) == ""
    # broad: single characters first, most uncovered documents win
    texts = {"/a/": "aaa bbb", "/b/": "aaa ccc"}
    assert s.broad_term(texts, {"/a/", "/b/"}, set()) == "a"
    assert s.broad_term(texts, set(), set()) == ""


def test_split_echo(mod):
    s = _mod(mod).split_echo
    assert s('Ergebnisse für "te"', "te") == ('Ergebnisse für "', '"')
    assert s("Du hast nach e gesucht - X", "e") == \
        ("Du hast nach e g", "sucht - X")        # rfind: caller uses a rare
    assert s("nothing here", "zz") is None       # term for the <title>
    assert s("", "x") is None


def test_harvest_builds_template_and_slots(mod, exporter, plug):
    exporter.fetch = live_search()
    exporter.cfg.search_harvest = True
    _prepare(mod, exporter, plug, with_404=False)
    plug.run_end()
    h = plug.stats["harvest"]
    assert h["used"] is True and h["template"] is True
    assert h["pages_with_slots"] == 2 and h["empty_state"] is True
    assert h["title_pattern"] is True
    assert h["probe_requests"] <= _mod(mod).PROBE_MAX_REQUESTS
    assert plug.stats["page_source"] == "live search page"
    # the index carries the harvested display slots
    data = json.loads(
        (exporter.public_dir / "search-index.json").read_text("utf-8"))
    slots = {d[0]: (d[4] if len(d) > 4 else None) for d in data["docs"]}
    assert slots["/fassaden/"]["a"] == "admin"
    assert slots["/fassaden/"]["d"] == "25. November 2021"
    assert slots["/fassaden/"]["i"] == "2021-11-25T11:05:38+01:00"
    assert slots["/fassaden/"]["p"] == "11"
    assert "Vollwärmeschutz" in slots["/fassaden/"]["e"]


def test_harvested_page_keeps_the_theme_design(mod, exporter, plug):
    exporter.fetch = live_search()
    exporter.cfg.search_harvest = True
    _prepare(mod, exporter, plug, with_404=False)
    plug.run_end()
    html = (exporter.public_dir / "suche" / "index.html").read_text("utf-8")
    soup = BeautifulSoup(html, "html.parser")
    # the theme's own grid container, tagged for the renderer, emptied
    box = soup.find(id="wpse-search-results")
    assert "iso-container" in (box.get("class") or [])
    assert box["data-columns"] == "3" and box["data-cur-page"] == "1"
    assert box.find("div", class_="wf-cell") is None
    assert soup.body["class"][:2] == ["search", "search-results"]
    # the static heading prefix survives; only the term became a marker
    h1 = soup.select_one(".page-title-head h1")
    assert h1.get_text().startswith("Suchergebnisse für:")
    assert h1.find("span", attrs={"data-wpse-search": "term"}) is not None
    crumb = soup.select("ol.breadcrumbs li")[-1]
    assert 'Ergebnisse für "' in crumb.get_text()
    assert crumb.find(attrs={"data-wpse-search": "term"}) is not None
    # the stale paginator is gone and no probe term survives as a query
    # value anywhere (an emptied "?s=" in an analytics blob is fine -- it
    # is exactly what a visitor without a term produces)
    assert soup.find(class_="paginator") is None
    assert re.search(r"[?&]s=[^\"'&<\s]", html) is None
    # the card template rides in the renderer config, with placeholders
    script = soup.find("script", attrs={"data-wpse-search": "renderer"}).string
    for token in ("%%U%%", "%%T%%", "%%X%%", "%%D%%", "%%A%%", "%%MB%%"):
        assert token in script, token
    assert "wf-cell" in script and "entry-title" in script
    assert '"empty":"' in script.replace(" ", "")
    assert "Nichts gefunden" in script            # harvested empty state
    assert "example.at" not in script
    # the search-only asset was queued for download
    assert any("masonry.min.js" in a for a in plug._assets)


def test_harvest_drops_dead_author_link(mod, exporter, plug):
    exporter.fetch = live_search()
    exporter.cfg.search_harvest = True
    _prepare(mod, exporter, plug, with_404=False)
    plug.run_end()
    script = BeautifulSoup(
        (exporter.public_dir / "suche" / "index.html").read_text("utf-8"),
        "html.parser").find(
            "script", attrs={"data-wpse-search": "renderer"}).string
    # /author/admin/ was never exported: the anchor stays, the href goes
    assert "author vcard" in script and "/author/admin/" not in script


def test_soft_404_search_is_not_harvested(mod, exporter, plug):
    """A site that answers /?s=x with the homepage must never be mistaken
    for a search endpoint."""
    home = ('<html lang="de"><head><title>Startseite</title></head><body>'
            '<div id="content"><a href="https://example.at/fassaden/">A</a>'
            '<a href="https://example.at/kontakt/">B</a></div></body></html>')
    exporter.fetch = lambda url, *a, **kw: _Resp(home)
    exporter.cfg.search_harvest = True
    _prepare(mod, exporter, plug, with_404=True)
    plug.run_end()
    assert plug.stats["harvest"]["used"] is False
    assert "does not echo the query" in plug.stats["harvest"]["reason"]
    assert plug.stats["page_source"] == "404.html"      # fell back
    assert plug.stats["harvest"]["probe_requests"] == 1  # gave up at once
    assert any("could not harvest" in w for w in exporter.warnings)


def test_harvest_failure_falls_back(mod, exporter, plug):
    def boom(url, *a, **kw):
        raise requests.RequestException("nope")
    exporter.fetch = boom
    exporter.cfg.search_harvest = True
    _prepare(mod, exporter, plug, with_404=True)
    plug.run_end()
    assert plug.stats["harvest"]["used"] is False
    assert plug.stats["page_source"] == "404.html"
    assert (exporter.public_dir / "suche" / "index.html").is_file()


def test_no_search_harvest_flag_makes_no_requests(mod, exporter, plug):
    _prepare(mod, exporter, plug, with_404=True)   # fixture blocks network
    plug.run_end()                                  # would raise on a fetch
    assert plug.stats["harvest"]["probe_requests"] == 0
    assert plug.stats["harvest"]["reason"] == "--no-search-harvest"
    assert plug.stats["page_source"] == "404.html"


def test_cli_no_search_harvest(mod, tmp_path):
    cfg = mod.parse_args(["https://example.at", "-o", str(tmp_path)])
    assert cfg.search_harvest is True
    cfg = mod.parse_args(["https://example.at", "-o", str(tmp_path),
                          "--no-search-harvest"])
    assert cfg.search_harvest is False


def test_disabled_by_flag_and_by_no_rewrite(mod, tmp_path):
    for i, kw in enumerate(({"search": False}, {"rewrite": False})):
        e = mod.Exporter(mod.Config(base_url="https://example.at",
                                    out_dir=tmp_path / str(i), **kw))
        e.public_dir.mkdir(parents=True, exist_ok=True)
        p = e.plugin("search")
        assert p.enabled is False
        assert p.wants_postprocess() is False
        soup = BeautifulSoup(PAGE, "html.parser")
        assert p.postprocess_soup(soup) is False
        assert soup.find("form")["action"] == "/"        # untouched
        p.pre_discover_soup(soup, "https://example.at/x/")
        assert p.docs == {}
        p.run_end()                                      # no crash, no files
        assert not (e.public_dir / "search-index.json").exists()
        assert not (e.public_dir / "suche").exists()
