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


def test_load_plugins_idempotent(mod):
    before = list(mod.PLUGIN_REGISTRY)
    mod.load_plugins()
    assert mod.PLUGIN_REGISTRY == before


def test_shipped_plugins_registered(mod):
    # alphabetical file order = load order
    assert [cls.name for cls in mod.PLUGIN_REGISTRY] == [
        "cloudflare", "complianz", "image_optimize", "minify",
        "slider_revolution", "theme_fixes"]
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


def test_exporter_gets_fresh_plugin_instances(mod, tmp_path):
    e1 = mod.Exporter(mod.Config(base_url="https://example.at",
                                 out_dir=tmp_path / "a"))
    e2 = mod.Exporter(mod.Config(base_url="https://example.at",
                                 out_dir=tmp_path / "b"))
    assert len(e1.plugins) == len(mod.PLUGIN_REGISTRY)
    assert all(p.exp is e1 for p in e1.plugins)
    assert not (set(map(id, e1.plugins)) & set(map(id, e2.plugins)))
