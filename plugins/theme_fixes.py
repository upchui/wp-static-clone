"""Fixes for specific WordPress themes / page-builder plugins.

* The7 theme: clickable images carry their internal page link in a
  data-dt-location attribute -- register it so those pages get crawled.
* Ultimate Addons & co.: uniqid()/rand() element ids change on every
  request (id="ultimate-heading-<hextime>", id="Info-box-wrap-9913" plus
  matching selectors) -- normalize them away so the mobile-vs-desktop
  HTML comparison doesn't report false "different"s.
"""
import re

from wp_static_export import Plugin

HEX_ID_RE = re.compile(rb"\b[0-9a-f]{8,}\b", re.IGNORECASE)
NUM_ID_RE = re.compile(rb"-\d{3,7}\b")


class ThemeFixes(Plugin):
    name = "theme_fixes"
    url_attrs = ("data-placeholder-image",)   # builder placeholder images
    page_url_attrs = ("data-dt-location",)
    html_noise_patterns = ((HEX_ID_RE, b"H"), (NUM_ID_RE, b"-N"))


PLUGIN = ThemeFixes
