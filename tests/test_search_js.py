"""The renderer's ranking must reproduce WordPress' own search order.

The expected orders below were MEASURED against the live WordPress site
the static export mirrors (logged out, `GET /?s=<term>`, DOM order of the
result cards). WordPress core orders search results by
`WP_Query::parse_search_order`: a relevance bucket first, `post_date`
descending inside it.

The renderer is executed the way it ships -- run through the same rjsmin
the minify plugin uses -- inside node against a fake DOM. Skipped when
node is unavailable.
"""
import json
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not installed")

# (path, post_title, post_date, excerpt, body text) -- the real corpus of
# the reference site, trimmed to what the queries below touch.
CORPUS = [
    ("/", "Home", "2016-06-14T01:53:06+02:00",
     "Huthansl Althaussanierung & Bau", "Maurerarbeiten Malerarbeiten "
     "Fassaden Estricharbeiten Bodenlegerarbeiten Anstreicherarbeiten "
     "Fliesenlegerarbeiten Kleine Grabarbeiten Raeumungen Maschinen"),
    ("/datenschutzerklaerung/", "Datenschutzerklärung",
     "2021-11-25T11:05:38+01:00", "",
     "Der Schutz Ihrer persönlichen Daten ist uns ein besonderes Anliegen. "
     "Wir verarbeiten Ihre Daten auf gesetzlicher Grundlage."),
    ("/kleine-grabarbeiten/", "Kleine Grabarbeiten",
     "2017-10-06T17:42:41+02:00", "",
     "Fundamente Gartengestaltungen Leitungsarbeiten Aushub"),
    ("/bodenlegerarbeiten/", "Bodenlegerarbeiten",
     "2017-10-06T17:40:46+02:00", "",
     "Bodenleger Laminat PVC Teppich Holzböden Verlegungen"),
    ("/maschinen-geraete/", "Maschinen & Geräte",
     "2017-10-05T21:30:22+02:00", "",
     "Hebebühne Arbeitsbühne Teleskop Arbeiten OHNE Gerüst Fassade "
     "ausbessern Maler Team"),
    ("/fliesenlegerarbeiten/", "Fliesenlegerarbeiten",
     "2017-10-05T17:47:43+02:00", "",
     "Innenbereiche Badezimmer WC Küche Wohnräume Stiegenhäuser Terrassen"),
    ("/estricharbeiten/", "Estricharbeiten", "2017-10-05T17:47:25+02:00", "",
     "Herstellen von herkömmlichen Nassestrichen Trockenestrich"),
    ("/malerarbeiten/", "Malerarbeiten", "2017-10-05T17:43:55+02:00", "",
     "Maler Spalierer Wohnräume Stiegenhäuser Hausfassaden Farbanpassung"),
    ("/maurerarbeiten/", "Maurerarbeiten", "2017-10-05T17:42:59+02:00", "",
     "Wanddurchbrüche Wohnungsumbauarbeiten Sanierung Fassaden Gartenmauern"),
    ("/anstreicherarbeiten/", "Anstreicherarbeiten",
     "2017-10-05T17:42:37+02:00", "",
     "Anstrich Wohnraumtüren Aufzugstüren Leitungsrohre Türstöcke"),
    ("/raeumungen/", "Räumungen", "2017-12-29T22:17:58+01:00", "",
     "Wohnungen Keller Dachböden Entrümpelung Arbeiten"),
    ("/fassaden/", "Fassaden", "2017-10-05T17:42:15+02:00", "",
     "Vollwärmeschutz VWS Vollwärmeschutzfassade Farbgestaltungen"),
    ("/impressum/", "Impressum", "2017-10-04T22:41:43+02:00", "",
     "Huthansl Althaussanierung und Bau Ges.m.b.H. Daten Wien"),
    ("/kontakt/", "Kontakt", "2016-06-28T01:40:45+02:00", "",
     "Einfache Kontaktaufnahme Telefonischer Kontakt Huthansl"),
    ("/ueber-uns/partner/", "Partnerfirmen", "2016-06-24T19:52:48+02:00", "",
     "Sochor Landegger Tefilak Sto Katzbeck Kampichler Veit"),
    ("/ueber-uns/", "Über uns", "2016-06-23T11:12:53+02:00", "",
     "Familienbetrieb seit 1950 mit 30 Mitarbeitern Malerarbeiten Bau Team"),
]

# MEASURED live orders (logged out). Only pages present in CORPUS.
LIVE_ORDER = {
    # single term: titles containing it first (date DESC), then the rest
    "te": ["/datenschutzerklaerung/", "/kleine-grabarbeiten/",
           "/bodenlegerarbeiten/", "/maschinen-geraete/",
           "/fliesenlegerarbeiten/", "/estricharbeiten/", "/malerarbeiten/",
           "/maurerarbeiten/", "/anstreicherarbeiten/",
           "/raeumungen/", "/impressum/", "/kontakt/",
           "/ueber-uns/partner/", "/ueber-uns/", "/"],
    "maler": ["/malerarbeiten/", "/maschinen-geraete/", "/ueber-uns/", "/"],
    "fassade": ["/fassaden/", "/maschinen-geraete/", "/malerarbeiten/",
                "/maurerarbeiten/", "/"],
    "arbeiten": ["/kleine-grabarbeiten/", "/bodenlegerarbeiten/",
                 "/fliesenlegerarbeiten/", "/estricharbeiten/",
                 "/malerarbeiten/", "/maurerarbeiten/",
                 "/anstreicherarbeiten/",
                 "/datenschutzerklaerung/", "/raeumungen/",
                 "/maschinen-geraete/", "/ueber-uns/", "/"],
    "estrich": ["/estricharbeiten/", "/"],
    # multi-term: "any term in title" (bucket 3) beats the newer page
    "maler fassade": ["/malerarbeiten/", "/maschinen-geraete/", "/"],
    "fassade maler": ["/malerarbeiten/", "/maschinen-geraete/", "/"],
    # bucket 2 -- ALL terms in the title
    "maler arbeiten": ["/malerarbeiten/", "/maschinen-geraete/",
                       "/ueber-uns/", "/"],
    "wohnräume": ["/fliesenlegerarbeiten/", "/malerarbeiten/"],
    "beton": [],
}

HARNESS = r"""
const docs = DOCS, queries = QUERIES, out = {};
for (const q of queries) {
  let html = "", replaced = null;
  const box = { get innerHTML() { return html; }, set innerHTML(v) { html = v; },
                set outerHTML(v) { replaced = v; } };
  const doc = { readyState: "complete", title: "",
    getElementById: (i) => (i === "wpse-search-results" ? box : null),
    querySelectorAll: () => [], createTextNode: (t) => ({ t }),
    addEventListener: () => {},
    get body() { return { className: "search search-results" }; } };
  const location = { search: "?s=" + encodeURIComponent(q), pathname: "/search/" };
  RENDERER(doc, location, function () {}, {});
  out[q] = [...html.matchAll(/data-wpse-path="([^"]*)"/g)].map(m => m[1]);
  if (replaced) { out[q] = []; }
}
console.log(JSON.stringify(out));
"""


def _run(mod, tmp_path, docs, queries):
    s = mod.PLUGIN_MODULES["search"]
    rjsmin = __import__("rjsmin")
    cfg = {"de": True, "idx": "/search-index.json", "max": 50, "snip": 180,
           "sfx": " - Huthansl", "cls": "post",
           # a template whose only job here is to expose the ORDER
           "tpl": '<div data-wpse-path="%%U%%">%%T%%</div>',
           "empty": "<p>nix</p>", "tt": None, "docs": docs}
    js = (s.RENDERER_JS.replace("__RESULTS_ID__", s.RESULTS_ID)
          .replace("__MARKER__", s.MARKER)
          .replace("__CFG__", s.js_literal(cfg)))
    js = rjsmin.jsmin(js, keep_bang_comments=False)   # as it ships
    script = (HARNESS
              .replace("DOCS", json.dumps(docs, ensure_ascii=False))
              .replace("QUERIES", json.dumps(queries, ensure_ascii=False))
              .replace("RENDERER",
                       "new Function('document','location',"
                       "'XMLHttpRequest','window'," + json.dumps(js) + ")"))
    f = tmp_path / "run.js"
    f.write_text(script, encoding="utf-8")
    res = subprocess.run(["node", str(f)], capture_output=True, text=True,
                         timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _docs():
    return [[path, f"{title} - Huthansl", desc, text,
             {"t": title, "i": date, "e": desc or text[:80]}]
            for path, title, date, desc, text in CORPUS]


def test_ranking_matches_the_live_wordpress_order(mod, tmp_path):
    got = _run(mod, tmp_path, _docs(), sorted(LIVE_ORDER))
    for query, expected in sorted(LIVE_ORDER.items()):
        assert got[query] == expected, (
            f"{query!r}\n  live:   {expected}\n  static: {got[query]}")


def test_ranking_is_stable_without_dates(mod, tmp_path):
    """A corpus with no post_date must still rank by bucket and stay
    deterministic (path order), never crash."""
    docs = [[p, t, "", txt] for p, t, _d, _e, txt in CORPUS]
    got = _run(mod, tmp_path, docs, ["maler"])
    assert got["maler"][0] == "/malerarbeiten/"      # title bucket first
    assert got["maler"][1:] == sorted(got["maler"][1:])