"""Lazy-load plugin attributes (WP Rocket, Smush, BJ Lazy Load, common
theme lazyloaders): where those plugins park the real image URL until
their runtime swaps it in. Registered here so discovery/rewriting treats
them like src/srcset, and so image optimization knows such images are
plugin-managed (no loading=lazy/width/height injection).
"""
from wp_static_export import Plugin


class LazyLoad(Plugin):
    name = "lazyload"
    url_attrs = ("data-src", "data-lazy-src", "data-bg")
    srcset_attrs = ("data-srcset", "data-lazy-srcset")
    css_url_attrs = ("data-bg",)        # full CSS declaration in the attr
    lazy_img_attrs = ("data-src", "data-lazy-src")


PLUGIN = LazyLoad
