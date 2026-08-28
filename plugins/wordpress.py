"""WordPress-ecosystem specifics beyond the core's crawl policy: head
cruft pointing at dynamic origin infrastructure, WPForms AJAX routes, and
WooCommerce cart/checkout pages (session-dependent, useless statically).
"""
import re

from wp_static_export import Plugin


class WordPress(Plugin):
    name = "wordpress"
    # WooCommerce: session/dynamic pages, never part of a static mirror
    page_skip_pattern_fragments = (r"/cart/?$", r"/checkout/?$")

    def skip_page_path(self, path: str) -> bool:
        return path.startswith("/wpforms-ajax")   # form AJAX routes

    def skip_asset_candidate(self, url: str, tag_name: str,
                             rel: set) -> bool:
        # clean_soup strips wlwmanifest/EditURI links -- don't mirror
        # their targets either
        return (tag_name == "link"
                and bool(rel & {"wlwmanifest", "edituri"})
                and self.exp.cfg.rewrite and self.exp.cfg.strip_wp_cruft)

    def clean_soup(self, soup) -> None:
        """Remove WordPress head elements that only point at dynamic origin
        infrastructure a static mirror does not have (REST/oEmbed/RSD/feed
        discovery, pingback, wlwmanifest, ?p= shortlinks) plus the
        version-revealing generator meta. canonical / hreflang / og: /
        twitter: / JSON-LD stay untouched."""
        removed: dict[str, int] = {}

        def drop(tag, kind: str) -> None:
            tag.decompose()
            removed[kind] = removed.get(kind, 0) + 1

        for meta in soup.find_all(
                "meta", attrs={"name": re.compile("^generator$", re.I)}):
            drop(meta, "generator-meta")
        for link in soup.find_all("link"):
            rel_attr = link.get("rel")
            rel = {r.lower() for r in (rel_attr if isinstance(rel_attr, list)
                                       else (rel_attr or "").split())}
            hit = rel & {"pingback", "shortlink", "wlwmanifest", "edituri"}
            if hit:
                drop(link, sorted(hit)[0])
                continue
            if any("api.w.org" in r for r in rel):
                drop(link, "rest-api-discovery")
                continue
            ltype = (link.get("type") or "").lower()
            href = link.get("href") or ""
            # feed/oEmbed/REST discovery only -- hreflang alternates carry
            # no type attribute and stay untouched
            if "alternate" in rel and (
                    "oembed" in ltype or "rss+xml" in ltype
                    or "atom+xml" in ltype):
                drop(link, "oembed-discovery" if "oembed" in ltype
                     else "feed-discovery")
                continue
            if ("alternate" in rel and ltype == "application/json"
                    and "/wp-json/" in href):
                drop(link, "rest-api-discovery")
        if removed:
            with self.exp.stats_lock:
                for kind, n in removed.items():
                    self.exp.cruft_removed[kind] = (
                        self.exp.cruft_removed.get(kind, 0) + n)


PLUGIN = WordPress
