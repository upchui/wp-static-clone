"""Wordfence relics: the Live Traffic human-detection beacon.

Wordfence injects one inline script per page that, on the first user
interaction, requests '/?wordfence_lh=1&hid=<id>&r=<rand>' -- root-
relative, so on a static mirror every visitor fires it against the
static host for nothing (no Wordfence backend there). Removed with the
rest of the WP cruft (--no-strip-wp-cruft keeps it)."""
from wp_static_export import Plugin

# stable anchors across Wordfence versions; the hid differs per page,
# so no literal match works
BEACON_MARKERS = ("wordfence_lh", "wfLogHumanRan")


class Wordfence(Plugin):
    name = "wordfence"

    def clean_soup(self, soup) -> None:
        removed = 0
        for script in soup.find_all("script"):
            if script.get("src"):
                continue                    # beacon is always inline
            text = "".join(c for c in script.contents if isinstance(c, str))
            if any(m in text for m in BEACON_MARKERS):
                script.decompose()
                removed += 1
        if removed:
            with self.exp.stats_lock:
                self.exp.cruft_removed["wordfence-beacon"] = (
                    self.exp.cruft_removed.get("wordfence-beacon", 0)
                    + removed)


PLUGIN = Wordfence
