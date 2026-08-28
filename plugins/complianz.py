"""Complianz (GDPR consent plugin) support.

Complianz references its banner assets via data-cmplz-src / data-src-cmplz
lazy attributes and builds its banner CSS URL from a {banner_id}/{type}
template at runtime -- resolve both so the consent banner renders on the
static mirror.
"""
import re

from wp_static_export import Plugin

BANNER_ID_RE = re.compile(r'"user_banner_id"\s*:\s*"?(\d+)')
CONSENT_TYPE_RE = re.compile(r'"consenttype"\s*:\s*"?(\w+)')


class Complianz(Plugin):
    name = "complianz"
    url_attrs = ("data-cmplz-src", "data-src-cmplz")

    def expand_scan_text(self, work: str) -> str:
        # the banner CSS URL is a template; resolve the placeholders from
        # the config values Complianz emits in the same script text
        if "{banner_id}" in work:
            m_id = BANNER_ID_RE.search(work)
            m_type = CONSENT_TYPE_RE.search(work)
            if m_id and m_type:
                work = (work.replace("{banner_id}", m_id.group(1))
                            .replace("{type}", m_type.group(1)))
        return work


PLUGIN = Complianz
