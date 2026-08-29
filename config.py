"""Core configuration and state records for wp-static-export.

Loaded by the core from the file next to the script and registered as
the module ``wps_config`` (import it under that name from plugins or
tests). The Config dataclass here holds only CORE options -- options a
plugin owns are declared in the plugin itself via the ``config_fields``
class attribute and merged into the final ``Config`` class by
``load_plugins()`` (see plugins/README.md), so direct ``Config(...)``
construction keeps accepting them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

TOOL_NAME = "wp-static-export"
VERSION = "2.4.0"


@dataclass
class Config:
    base_url: str
    out_dir: Path
    rewrite: bool = True
    clean: bool = False
    follow_links: bool = True
    concurrency: int = 5
    delay: float = 0.0
    timeout: float = 25.0
    max_pages: int = 5000
    user_agent: str = f"{TOOL_NAME}/{VERSION} (+static site exporter)"
    insecure: bool = False
    host_header: str | None = None
    extra_headers: dict = field(default_factory=dict)
    port: int = 8080
    extra_sitemaps: list = field(default_factory=list)
    generate_sitemap: bool = True
    # read by the core clean_soup gate AND plugins/wordpress.py
    strip_wp_cruft: bool = True
    staging: bool = False
    target_domain: str | None = None
    sitemap_include_linked: bool = False
    fail_on: str = "none"            # none | errors | verify
    quiet: bool = False
    excludes: list = field(default_factory=list)
    respect_robots: bool = False
    internal_hosts: list = field(default_factory=list)
    resolve_internal: bool = True


@dataclass
class PageRecord:
    url: str
    status: int = 0
    final_url: str = ""
    error: str = ""
    title: str = ""
    has_title: bool = False
    has_description: bool = False
    has_viewport: bool = False
    noindex: bool = False
    canonical: str = ""
    content_type: str = ""
    source: str = "sitemap"          # sitemap | link | manual
    mobile: str = ""                 # same | different | dynamic | check-failed
                                     # (plugin-owned: plugins/mobile_check.py)
    last_modified: str = ""          # raw Last-Modified response header
    save_url: str = ""               # canonical URL the content was saved under
                                     # (differs from url for redirect sources)
    is_stub: bool = False            # only a redirect stub was written here
