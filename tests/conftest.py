"""Load wp-static-export.py (hyphenated filename) as an importable module."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "wp_static_export", ROOT / "wp-static-export.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["wp_static_export"] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load()


@pytest.fixture(scope="session")
def mod():
    return _MOD


@pytest.fixture
def exporter(mod, tmp_path):
    """Exporter against a fictitious host -- no network calls happen in
    __init__ or in any of the pure helpers under test."""
    return mod.Exporter(mod.Config(base_url="https://example.at",
                                   out_dir=tmp_path / "out"))
