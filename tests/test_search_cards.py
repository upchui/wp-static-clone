"""Reading a theme's result card: what is markup, what is the post.

Four card shapes taken from REAL WordPress sites, each with the trait that
breaks a selector-based reader:

  the7-blog    the classic dt-the7 blog list -- excerpt in <p>, author
               link, <time datetime> in ISO, thumbnail on ONE of the two
               cards (huthansl.at)
  the7-list    the7's `articles-list` shortcode -- data-name/data-date on
               the entry, excerpt in a DIV, and a "Mehr Info" button whose
               href points at the post (zurichimmobilien.at)
  parldigi     excerpt in div.entry-summary, no <p> anywhere, and a
               LOCALIZED <time datetime="2. Juni 2026"> (parldigi.ch)
  minimal      p.entry-title, a meta line, NO excerpt at all -- nothing
               may be invented here (wikimedia.ch)

The proof for each: harvest the cards, then render the harvested template
back through the renderer as it ships and get the ORIGINAL cards out
again, byte for byte after re-parsing.

The card markup below is root-relative because the harvest only ever sees
localized soup (`_prepare_soup` runs first).
"""
import json
import shutil
import subprocess

import pytest
from bs4 import BeautifulSoup

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not installed")


def the7_blog(p) -> str:
    thumb = ('<div class="post-thumbnail"><img src="/wp-content/%(slug)s.jpg" '
             'alt="%(title)s" width="600" height="400"></div>' % p
             if p["img"] else "")
    return (
        '<div class="wf-cell iso-item" data-post-id="%(id)s" '
        'data-date="%(iso)s" data-name="%(title)s">'
        '<article class="post post-%(id)s page hentry">' % p
        + thumb
        + '<div class="blog-content wf-td">'
          '<h3 class="entry-title"><a href="%(href)s" title="%(title)s" '
          'rel="bookmark">%(title)s</a></h3>'
          '<div class="entry-meta">'
          '<a class="author vcard" href="/author/admin/" rel="author">'
          'Von <span class="fn">%(author)s</span></a>'
          '<a href="/author/admin/" class="data-link">'
          '<time class="entry-date" datetime="%(iso)s">%(shown)s</time></a>'
          '</div><p>%(excerpt)s</p></div></article></div>' % p)


def the7_list(p) -> str:
    return (
        '<article class="post project-odd visible no-img post-%(id)s page '
        'type-page status-publish hentry description-off" '
        'data-name="%(title)s" data-date="%(iso)s">'
        '<div class="post-entry-content">'
        '<h3 class="entry-title"><a href="%(href)s">%(title)s</a></h3>'
        '<div class="entry-excerpt">%(excerpt)s</div>'
        '<a class="dt-btn dt-btn-m" href="%(href)s">'
        '<span>Mehr Info</span></a>'
        '</div></article>' % p)


def parldigi(p) -> str:
    return (
        '<article class="post-%(id)s post type-post status-publish hentry">'
        '<h2 class="entry-title"><a href="%(href)s" rel="bookmark">'
        '%(title)s</a></h2>'
        '<div class="news-last-edited">Zuletzt bearbeitet: '
        '<time class="entry-date published" datetime="%(shown)s">'
        '%(shown)s</time></div>'
        '<div class="entry-summary">%(excerpt)s</div>'
        '</article>' % p)


def minimal(p) -> str:
    return (
        '<article class="post-%(id)s post type-post status-publish hentry">'
        '<p class="entry-title"><a href="%(href)s">%(title)s</a></p>'
        '<span class="meta">Aktualisiert</span>'
        '</article>' % p)


POSTS = [
    {"id": "11", "href": "/fassaden/", "slug": "fassaden", "title": "Fassaden",
     "iso": "2021-11-25T11:05:38+01:00", "shown": "25. November 2021",
     "author": "admin", "img": True,
     "excerpt": "Vollwärmeschutz für die ganze Straße, vom "
                "Sockel bis zum Dach."},
    {"id": "12", "href": "/kontakt/", "slug": "kontakt", "title": "Kontakt",
     "iso": "2017-10-05T17:47:25+02:00", "shown": "5. Oktober 2017",
     "author": "admin", "img": False,
     "excerpt": "Rennweg 81 in Brunn am Gebirge, telefonisch und per "
                "Mail erreichbar."},
]

SHAPES = {"the7-blog": the7_blog, "the7-list": the7_list,
          "parldigi": parldigi, "minimal": minimal}

# the loop's own wrapper, where the theme has one -- parldigi and the
# minimal shape put their entries straight into the content well, the way
# Twenty Twenty-One does, and must be found there too
WRAPPERS = {
    "the7-blog": '<div class="wf-container iso-container" data-cur-page="1">',
    "the7-list": '<div class="articles-list blog-shortcode mode-list" '
                 'data-cur-page="1" data-post-limit="-1">',
}

# what each shape's cards actually show -- asserted so a shape that stops
# being recognized fails loudly instead of quietly rendering less
EXPECTED_SLOTS = {
    "the7-blog": {"url", "title", "excerpt", "date", "date-iso", "author",
                  "post-id", "classes", "media"},
    "the7-list": {"url", "title", "excerpt", "date-iso", "classes"},
    "parldigi": {"url", "title", "excerpt", "date", "date-iso", "classes"},
    "minimal": {"url", "title", "classes"},
}

# every href the cards use has to resolve, or _drop_dead_hrefs would (very
# correctly) strip it and the round-trip would compare against a card the
# export would never have written
LINKED = ["/fassaden/", "/kontakt/", "/author/admin/"]


def _mod(mod):
    return mod.PLUGIN_MODULES["search"]


@pytest.fixture
def plug(mod, exporter):
    for path in LINKED:
        target = exporter.public_dir / path.strip("/") / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("<html></html>", encoding="utf-8")

    def no_network(url, *a, **kw):
        raise AssertionError(f"unexpected network request: {url}")
    exporter.fetch = no_network
    return exporter.plugin("search")


def _cards(shape) -> str:
    inner = "".join(SHAPES[shape](p) for p in POSTS)
    open_tag = WRAPPERS.get(shape)
    return f"{open_tag}{inner}</div>" if open_tag else inner


def _texts() -> dict:
    return {p["href"]: f"{p['title']} {p['excerpt']}" for p in POSTS}


def _harvest(mod, plug, shape) -> dict:
    """Drive the reading of one response's cards, without the network."""
    s = _mod(mod)
    soup = BeautifulSoup(f'<div id="content">{_cards(shape)}</div>',
                         "html.parser")
    paths = {p["href"] for p in POSTS}
    container, items = s.Search._find_results(soup.find(id="content"), paths)
    assert container is not None, shape
    assert len(items) == len(POSTS), (shape, len(items))
    h = s.new_harvest()
    assert plug._take_cards(h, items, paths) == len(POSTS)
    plug._finish_template(h, _texts())
    return h


# -- what gets recognized ----------------------------------------------------

def test_every_shape_is_read_the_same_way(mod, plug):
    for shape in SHAPES:
        h = _harvest(mod, plug, shape)
        assert h["measured"] is True, shape
        assert set(plug.stats["harvest"]["slots"]) >= EXPECTED_SLOTS[shape], (
            shape, plug.stats["harvest"]["slots"])
        plug.stats["harvest"]["slots"] = []          # per-shape accounting


def test_the_excerpt_is_found_wherever_the_theme_keeps_it(mod, plug):
    """A <p>, a div.entry-excerpt and a div.entry-summary are all the same
    thing to a reader that measures instead of matching selectors."""
    for shape in ("the7-blog", "the7-list", "parldigi"):
        h = _harvest(mod, plug, shape)
        assert "%%X%%" in h["tpl"], shape
        for post in POSTS:
            assert h["slots"][post["href"]]["e"] == post["excerpt"], shape


def test_nothing_is_invented_for_a_theme_without_an_excerpt(mod, plug):
    h = _harvest(mod, plug, "minimal")
    assert "%%X%%" not in h["tpl"]
    assert "e" not in h["slots"]["/fassaden/"]
    # the static meta line is theme markup and survives verbatim
    assert "Aktualisiert" in h["tpl"] and "%%V" not in h["tpl"]


def test_the_read_more_button_gets_its_own_href(mod, plug):
    """The second link to the post would otherwise stay frozen on the card
    it was harvested from and send every result to the same page."""
    h = _harvest(mod, plug, "the7-list")
    assert h["tpl"].count("%%U%%") == 2
    assert "Mehr Info" in h["tpl"]               # the label is NOT a slot


def test_a_localized_time_still_sorts_by_a_real_date(mod, plug):
    """parldigi writes <time datetime="25. November 2021">. It is kept for
    display; the ISO date the renderer sorts on comes from the page."""
    h = _harvest(mod, plug, "parldigi")
    slot = h["slots"]["/fassaden/"]
    assert slot["d"] == "25. November 2021" and slot["i"] == slot["d"]
    assert not _mod(mod).ISO_DATE_RE.match(slot["i"])   # _build_index fixes it


def test_a_part_only_some_cards_have_becomes_a_cut_block(mod, plug):
    h = _harvest(mod, plug, "the7-blog")
    assert h["slots"]["/fassaden/"].get("m"), "thumbnail card lost its media"
    assert not h["slots"]["/kontakt/"].get("m")


def test_a_single_card_says_so_instead_of_pretending(mod, plug):
    s = _mod(mod)
    soup = BeautifulSoup(f'<div id="content">{the7_list(POSTS[0])}</div>',
                         "html.parser")
    paths = {POSTS[0]["href"]}
    root = soup.find(id="content")
    # a lone result is NOT guessed at while anything else might still show a
    # real loop -- only the harvest's last attempt passes lone=True
    assert s.Search._find_results(root, paths) == (None, [])
    _c, items = s.Search._find_results(root, paths, lone=True)
    assert len(items) == 1                       # via WordPress' post_class()
    h = s.new_harvest()
    plug._take_cards(h, items, paths)
    plug._finish_template(h, _texts())
    assert h["measured"] is False
    assert any("only ONE live result card" in w for w in plug.exp.warnings)
    assert "%%X%%" in h["tpl"] and "%%T%%" in h["tpl"]


def badged(p) -> str:
    """A card with a value no name fits: a per-post category badge."""
    return (
        '<article class="post-%(id)s post hentry">'
        '<span class="cat-badge" data-cat="%(slug)s">%(slug)s</span>'
        '<h2><a href="%(href)s">%(title)s</a></h2>'
        '<div class="teaser">%(excerpt)s</div></article>' % p)


def test_a_value_we_cannot_name_still_gets_its_own_slot(mod, plug, tmp_path):
    """Neither title nor date nor excerpt -- and still a per-post value.
    Freezing it on the sample card would put one post's category on every
    result; dropping it would lose what the theme shows."""
    s = _mod(mod)
    SHAPES["badged"] = badged
    try:
        h = _harvest(mod, plug, "badged")
    finally:
        del SHAPES["badged"]
    assert "%%V0%%" in h["tpl"] and "%%V1%%" in h["tpl"]     # attr and text
    assert h["slots"]["/kontakt/"]["v"] == ["kontakt", "kontakt"]
    docs = [[p["href"], p["title"], "", p["excerpt"],
             h["slots"][p["href"]]] for p in POSTS]
    got = BeautifulSoup(_render(mod, tmp_path, h, docs, ""), "html.parser")
    badges = {b["data-cat"]: b.get_text() for b in got.select(".cat-badge")}
    assert badges == {"fassaden": "fassaden", "kontakt": "kontakt"}
    assert s.SLOT_NAMES  # the named slots are reported; these are the extras
    assert plug.stats["harvest"]["extra_slots"] == 2


# -- the round trip ----------------------------------------------------------

HARNESS = r"""
const docs = DOCS;
let html = "";
// the index is ALWAYS fetched -- stand in for the network
function XHR() {}
XHR.prototype.open = function () {};
XHR.prototype.send = function () {
  this.readyState = 4;
  this.status = 200;
  this.responseText = JSON.stringify({ v: 3, docs: docs });
  this.onreadystatechange();
};
const box = { get innerHTML() { return html; }, set innerHTML(v) { html = v; },
              set outerHTML(v) { html = v; } };
const doc = { readyState: "complete", title: "",
  getElementById: (i) => (i === "wpse-search-results" ? box : null),
  querySelectorAll: () => [], createTextNode: (t) => ({ t }),
  addEventListener: () => {},
  get body() { return { className: "search search-results" }; } };
const location = { search: "?s=" + encodeURIComponent(QUERY),
                   pathname: "/search/" };
RENDERER(doc, location, XHR, {});
console.log(html);
"""


def _render(mod, tmp_path, h, docs, query) -> str:
    """The harvested template through the renderer exactly as it ships."""
    s = _mod(mod)
    rjsmin = __import__("rjsmin")
    cfg = {"de": True, "idx": "/search-index.json", "max": 50, "snip": 400,
           "sfx": "", "lang": "", "cls": h["cls"], "tpl": h["tpl"],
           "nb": len(h["blocks"]), "empty": "", "tt": None}
    js = (s.RENDERER_JS.replace("__RESULTS_ID__", s.RESULTS_ID)
          .replace("__MARKER__", s.MARKER)
          .replace("__CFG__", s.js_literal(cfg)))
    js = rjsmin.jsmin(js, keep_bang_comments=False)
    script = (HARNESS
              .replace("DOCS", json.dumps(docs, ensure_ascii=False))
              .replace("QUERY", json.dumps(query))
              .replace("RENDERER",
                       "new Function('document','location',"
                       "'XMLHttpRequest','window'," + json.dumps(js) + ")"))
    f = tmp_path / "render.js"
    f.write_text(script, encoding="utf-8")
    res = subprocess.run(["node", str(f)], capture_output=True, text=True,
                         timeout=60)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def _canon(html: str) -> str:
    return str(BeautifulSoup(html, "html.parser"))


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_rendered_card_is_the_live_card(mod, plug, tmp_path, shape):
    h = _harvest(mod, plug, shape)
    docs = [[p["href"], p["title"], "", p["excerpt"],
             h["slots"][p["href"]]] for p in POSTS]
    # the empty query is WordPress' LIKE '%%': every page, so every card
    got = _render(mod, tmp_path, h, docs, "")
    rendered = BeautifulSoup(got, "html.parser").find_all(True,
                                                          recursive=False)
    assert len(rendered) == len(POSTS), got
    order = {p["href"]: p for p in POSTS}
    for card in rendered:
        link = card.find("a", href=lambda v: v in order)
        want = SHAPES[shape](order[link["href"]])
        assert _canon(str(card)) == _canon(want), shape


def test_a_page_the_live_search_never_returned_still_gets_a_card(mod, plug,
                                                                 tmp_path):
    """No harvested card for it: the title and an auto-excerpt come from
    the index, and the parts that would render blank are dropped rather
    than left dangling."""
    h = _harvest(mod, plug, "the7-blog")
    docs = [["/impressum/", "Impressum - Huthansl", "",
             "Huthansl Althaussanierung und Bau Ges.m.b.H. in Wien."]]
    got = _render(mod, tmp_path, h, docs, "huthansl")
    card = BeautifulSoup(got, "html.parser")
    assert card.find("a", href="/impressum/") is not None
    assert "Impressum - Huthansl" in card.get_text()
    assert "Althaussanierung" in card.get_text()      # the auto excerpt
    assert card.find("img") is None                   # no thumbnail invented
    assert card.find(class_="entry-meta") is None     # no empty date/author
    assert "%%" not in got                            # no token left behind
