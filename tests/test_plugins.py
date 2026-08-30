"""Plugin loader mechanics (fail-loudly policy) and registry sanity."""
import textwrap

import pytest


def test_plugins_dir_missing_fails(mod, tmp_path):
    with pytest.raises(SystemExit):
        mod._scan_plugins(tmp_path / "nope")


def test_plugin_without_class_fails(mod, tmp_path):
    f = tmp_path / "empty.py"
    f.write_text("x = 1\n")
    with pytest.raises(SystemExit):
        mod._load_plugin_file(f)


def test_broken_plugin_raises(mod, tmp_path):
    f = tmp_path / "zz_broken.py"
    f.write_text("raise RuntimeError('boom')\n")
    with pytest.raises(RuntimeError):
        mod._load_plugin_file(f)


def test_underscore_files_skipped_and_valid_loaded(mod, tmp_path):
    (tmp_path / "_disabled.py").write_text(
        "raise RuntimeError('must not load')\n")
    (tmp_path / "demo.py").write_text(textwrap.dedent("""\
        from wp_static_export import Plugin

        class Demo(Plugin):
            name = "demo"

        PLUGIN = Demo
    """))
    loaded = mod._scan_plugins(tmp_path)
    assert [cls.name for _, cls in loaded] == ["demo"]
    assert issubclass(loaded[0][1], mod.Plugin)


def test_failed_plugin_not_left_in_sys_modules(mod, tmp_path):
    import sys
    f = tmp_path / "zz_poison.py"
    f.write_text("raise RuntimeError('boom')\n")
    with pytest.raises(RuntimeError):
        mod._load_plugin_file(f)
    assert "wps_plugin_zz_poison" not in sys.modules
    f2 = tmp_path / "zz_noplugin.py"
    f2.write_text("x = 1\n")
    with pytest.raises(SystemExit):
        mod._load_plugin_file(f2)
    assert "wps_plugin_zz_noplugin" not in sys.modules


def test_load_plugins_idempotent(mod):
    before = list(mod.PLUGIN_REGISTRY)
    mod.load_plugins()
    assert mod.PLUGIN_REGISTRY == before


def test_shipped_plugins_registered(mod):
    # alphabetical file order = load order
    assert [cls.name for cls in mod.PLUGIN_REGISTRY] == [
        "cloudflare", "complianz", "downloads", "image_compress",
        "image_optimize", "lazyload", "minify", "mobile_check", "search",
        "slider_revolution", "theme_fixes", "wordfence", "wordpress"]
    assert set(mod.PLUGIN_MODULES) == {cls.name
                                       for cls in mod.PLUGIN_REGISTRY}


def test_plugin_registries_aggregated(mod):
    assert "data-cmplz-src" in mod.URL_ATTRS            # complianz
    assert "data-src-cmplz" in mod.URL_ATTRS
    assert "data-dt-location" in mod.PAGE_URL_ATTRS     # theme_fixes (The7)
    assert "data-dbsrc" in mod.B64_URL_ATTRS            # slider_revolution
    assert mod.HTML_NOISE_EXTRA                         # theme_fixes id noise
    assert "cf-fonts" in mod.VERIFY_SCRIPT_REF_DIRS     # cloudflare
    assert "/cdn-cgi/l/email-protection" in mod.VERIFY_SKIP_REF_PREFIXES
    assert "data-src" in mod.URL_ATTRS                  # lazyload
    assert "data-placeholder-image" in mod.URL_ATTRS    # theme_fixes
    assert "data-lazy-srcset" in mod.SRCSET_ATTRS       # lazyload
    assert "data-bg" in mod.CSS_URL_ATTRS               # lazyload
    assert "data-lazy-src" in mod.LAZY_IMG_ATTRS        # lazyload
    assert mod.PAGE_SKIP_PATTERNS.search("/cart/")      # wordpress (Woo)
    assert mod.PAGE_SKIP_PATTERNS.search("/checkout/")
    assert mod.PAGE_SKIP_PATTERNS.search("/wp-admin/")  # core fragment
    assert "mobile-variants" in mod.EXTRA_OUTPUT_DIRS   # mobile_check


# -- v2.4.0: plugin-declared config fields -----------------------------------

def test_plugin_config_fields_merged(mod, tmp_path):
    import sys
    base = sys.modules["wps_config"].Config
    assert issubclass(mod.Config, base)
    # plugin-owned fields exist on the final class with their defaults ...
    cfg = mod.Config(base_url="https://example.at", out_dir=tmp_path)
    assert cfg.minify is True                           # minify
    assert cfg.optimize_images is True                  # image_optimize
    assert cfg.compress_images is True                  # image_compress
    assert cfg.image_quality == 85
    assert cfg.mobile_check is True                     # mobile_check
    assert cfg.mobile_user_agent == ""
    assert cfg.sr7_hydrate is True                      # slider_revolution
    # ... but not on the plugin-free core Config
    core_fields = {f.name for f in mod.dataclasses.fields(base)}
    assert "minify" not in core_fields
    assert "image_quality" not in core_fields
    assert "strip_wp_cruft" in core_fields              # core-read: stays
    # direct construction with plugin options keeps working
    assert mod.Config(base_url="https://example.at", out_dir=tmp_path,
                      minify=False, image_quality=70).image_quality == 70


def test_config_field_collision_fails(mod):
    class Evil(mod.Plugin):
        name = "evil"
        config_fields = {"concurrency": 9}              # core field

    class Evil2(mod.Plugin):
        name = "evil2"
        config_fields = {"minify": False}               # other plugin's field

    class Evil3(mod.Plugin):
        name = "evil3"
        config_fields = {"my_list": []}                 # mutable default

    with pytest.raises(SystemExit):
        mod._build_config([Evil])
    with pytest.raises(SystemExit):
        mod._build_config(list(mod.PLUGIN_REGISTRY) + [Evil2])
    with pytest.raises(SystemExit):
        mod._build_config([Evil3])
    # the shipped registry itself builds cleanly
    assert mod._build_config(list(mod.PLUGIN_REGISTRY)) is not None


def test_exporter_gets_fresh_plugin_instances(mod, tmp_path):
    e1 = mod.Exporter(mod.Config(base_url="https://example.at",
                                 out_dir=tmp_path / "a"))
    e2 = mod.Exporter(mod.Config(base_url="https://example.at",
                                 out_dir=tmp_path / "b"))
    assert len(e1.plugins) == len(mod.PLUGIN_REGISTRY)
    assert all(p.exp is e1 for p in e1.plugins)
    assert not (set(map(id, e1.plugins)) & set(map(id, e2.plugins)))


# -- v2.8.0: WordPress artifact-page detection -------------------------------

# verbatim body class attributes from the real site
ARTIFACT_CLASSES = (
    "attachment wp-singular attachment-template-default attachmentid-1164 "
    "attachment-png wp-embed-responsive wp-theme-dt-the7 dt-responsive-on")
ARTIFACT_SINGLE = (
    "attachment wp-singular attachment-template-default single "
    "single-attachment postid-1165 attachmentid-1165 attachment-jpeg")
PAGE_CLASSES = ("wp-singular page-template-default page page-id-1098 "
                "wp-embed-responsive wp-theme-dt-the7 dt-responsive-on")
HOME_CLASSES = "home wp-singular page-template-default page page-id-930"


def _resp(body_classes, extra=""):
    html = (f'<!doctype html><html><head><title>x</title></head>'
            f'<body class="{body_classes}">{extra}</body></html>')

    class R:
        content = html.encode("utf-8")
    return R()


def _rec(mod, url="https://example.at/x/"):
    return mod.PageRecord(url=url, source="sitemap",
                          content_type="text/html", status=200)


def test_attachment_pages_flagged(mod, tmp_path):
    e = mod.Exporter(mod.Config(base_url="https://example.at",
                                out_dir=tmp_path / "o"))
    wp = e.plugin("wordpress")
    for classes in (ARTIFACT_CLASSES, ARTIFACT_SINGLE):
        rec = _rec(mod)
        wp.page_saved("https://example.at/logo/", _resp(classes), rec)
        assert rec.artifact == "wp-attachment", classes
    # real pages are never flagged ...
    for classes in (PAGE_CLASSES, HOME_CLASSES, "search search-results"):
        rec = _rec(mod)
        wp.page_saved("https://example.at/malerarbeiten/", _resp(classes),
                      rec)
        assert rec.artifact == "", classes
    # ... and neither is a page that merely CONTAINS attachment-* classes
    # on an <img> (WordPress' image size classes)
    rec = _rec(mod)
    wp.page_saved("https://example.at/malerarbeiten/",
                  _resp(PAGE_CLASSES,
                        '<img class="attachment-thumbnail attachment" src=x>'),
                  rec)
    assert rec.artifact == ""
    assert wp.stats["attachment_pages"] == 2


def test_phpthumb_cache_pages_flagged_by_url(mod, tmp_path):
    e = mod.Exporter(mod.Config(base_url="https://example.at",
                                out_dir=tmp_path / "o"))
    wp = e.plugin("wordpress")
    rec = _rec(mod)
    # no attachment body class at all -- the URL rule still catches it
    wp.page_saved("https://example.at/ueber-uns/partner/"
                  "phpthumb_cache_huthansl-at_srcabc_dat1382975637/",
                  _resp(PAGE_CLASSES), rec)
    assert rec.artifact == "phpthumb-cache"
    assert wp.stats["cache_pages"] == 1


def test_artifact_detection_tolerates_odd_input(mod, tmp_path):
    e = mod.Exporter(mod.Config(base_url="https://example.at",
                                out_dir=tmp_path / "o"))
    wp = e.plugin("wordpress")
    wp.page_saved("https://example.at/x/", _resp(PAGE_CLASSES), None)  # no rec

    class Latin1:
        content = ('<html><body class="' + ARTIFACT_CLASSES
                   + '">Grüße</body></html>').encode("latin-1")
    rec = _rec(mod)
    wp.page_saved("https://example.at/x/", Latin1(), rec)
    assert rec.artifact == "wp-attachment"          # class tokens are ASCII

    class NoBody:
        content = b"not html at all"
    rec2 = _rec(mod)
    wp.page_saved("https://example.at/x/", NoBody(), rec2)
    assert rec2.artifact == ""


# -- v2.13.0: empty term archives --------------------------------------------

# verbatim from www.integra-vd.at/wpa-stats-type/android/ -- a taxonomy a
# plugin registered for its own bookkeeping, on a post type that is not
# publicly queryable, so every term archive renders "Nothing found"
EMPTY_ARCHIVE_CLASSES = (
    "archive tax-wpa-stats-type term-android term-56 wp-embed-responsive "
    "wp-theme-dt-the7 wp-child-theme-dt-the7-child the7-core-ver-2.7.13")
FULL_ARCHIVE_CLASSES = ("archive category category-news category-12 "
                        "wp-embed-responsive wp-theme-dt-the7")
# WordPress core's own content-none.php, as dt-the7 renders it
NO_RESULTS_BLOCK = (
    '<article class="post no-results not-found" id="post-0">'
    '<h1 class="entry-title">Nichts gefunden</h1>'
    '<p>Es scheint, dass wir nicht finden k&ouml;nnen, was Sie suchen.</p>'
    '</article>')


def test_empty_term_archives_flagged(mod, tmp_path):
    """An archive whose loop came back empty: Yoast still says
    index,follow, so nothing else keeps it out of sitemap.xml or the
    search -- and six of them made up more than half of one real site's
    sitemap."""
    e = mod.Exporter(mod.Config(base_url="https://example.at",
                                out_dir=tmp_path / "o"))
    wp = e.plugin("wordpress")
    rec = _rec(mod)
    wp.page_saved("https://example.at/wpa-stats-type/android/",
                  _resp(EMPTY_ARCHIVE_CLASSES, NO_RESULTS_BLOCK), rec)
    assert rec.artifact == "empty-archive"
    assert wp.stats["empty_archives"] == 1
    # ... and it is kept out of BOTH listings, while staying on disk
    assert e.index_exclusion(rec) == "empty-archive"
    assert e.index_exclusion(rec, "search") == "empty-archive"


def test_archives_with_entries_are_left_alone(mod, tmp_path):
    e = mod.Exporter(mod.Config(base_url="https://example.at",
                                out_dir=tmp_path / "o"))
    wp = e.plugin("wordpress")
    entries = ('<article class="post post-12 hentry"><h2>Ein Beitrag</h2>'
               "</article>")
    for classes, body in (
            # an archive that found something
            (FULL_ARCHIVE_CLASSES, entries),
            # a normal page carrying a search widget's empty-state markup
            (PAGE_CLASSES, NO_RESULTS_BLOCK),
            # our own generated results page -- never an artifact
            ("search search-results", NO_RESULTS_BLOCK),
            # the words in running text, not in a class attribute
            (EMPTY_ARCHIVE_CLASSES,
             "<p>Die Klassen no-results not-found kommen von "
             "content-none.php.</p>"),
            # a class that merely CONTAINS the tokens
            (EMPTY_ARCHIVE_CLASSES,
             '<div class="wpa-no-results wpa-not-found">x</div>')):
        rec = _rec(mod)
        wp.page_saved("https://example.at/x/", _resp(classes, body), rec)
        assert rec.artifact == "", (classes, body[:40])
    assert wp.stats["empty_archives"] == 0


def test_list_empty_archives_opt_out(mod, tmp_path):
    cfg = mod.parse_args(["https://example.at", "-o", str(tmp_path)])
    assert cfg.list_empty_archives is False
    cfg = mod.parse_args(["https://example.at", "-o", str(tmp_path),
                          "--list-empty-archives"])
    assert cfg.list_empty_archives is True
    e = mod.Exporter(cfg)
    rec = _rec(mod)
    e.plugin("wordpress").page_saved(
        "https://example.at/wpa-stats-type/android/",
        _resp(EMPTY_ARCHIVE_CLASSES, NO_RESULTS_BLOCK), rec)
    assert rec.artifact == ""                       # opted out
    assert e.index_exclusion(rec) == ""             # so it stays listed
    # the attachment opt-out is separate: it must NOT switch this one off
    rec2 = _rec(mod)
    e.plugin("wordpress").page_saved("https://example.at/logo/",
                                     _resp(ARTIFACT_CLASSES), rec2)
    assert rec2.artifact == "wp-attachment"


def test_list_attachment_pages_opt_out(mod, tmp_path):
    cfg = mod.parse_args(["https://example.at", "-o", str(tmp_path)])
    assert cfg.list_attachment_pages is False
    cfg = mod.parse_args(["https://example.at", "-o", str(tmp_path),
                          "--list-attachment-pages"])
    assert cfg.list_attachment_pages is True
    e = mod.Exporter(cfg)
    rec = _rec(mod)
    e.plugin("wordpress").page_saved("https://example.at/logo/",
                                     _resp(ARTIFACT_CLASSES), rec)
    assert rec.artifact == ""                       # opted out
    assert e.index_exclusion(rec) == ""             # so it stays listed
