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
