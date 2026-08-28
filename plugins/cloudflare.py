"""Cloudflare support: undo what the Cloudflare proxy injected into the
origin's HTML -- none of it can work on a static mirror served elsewhere.

* Email obfuscation: pre-decode data-cfemail spans and
  /cdn-cgi/l/email-protection links into real mailto: addresses; the
  runtime decode script's backend does not exist statically.
* The Insights RUM beacon reports to Cloudflare for the ORIGIN zone --
  off Cloudflare it only produces console/CORS errors.
* /cdn-cgi/ runtime endpoints are never pages; /cf-fonts and /cdn-cgi
  script refs are verified like wp-content ones.
"""

from wp_static_export import Plugin


def decode_cfemail(enc: str) -> str | None:
    """Decode a Cloudflare email-obfuscation hex string (XOR with first byte)."""
    try:
        data = bytes.fromhex(enc)
    except ValueError:
        return None
    if len(data) < 2:
        return None
    try:
        return bytes(b ^ data[0] for b in data[1:]).decode("utf-8")
    except UnicodeDecodeError:
        return None


class Cloudflare(Plugin):
    name = "cloudflare"
    verify_script_ref_dirs = ("cf-fonts", "cdn-cgi")
    verify_skip_ref_prefixes = ("/cdn-cgi/l/email-protection",)

    def skip_page_path(self, path: str) -> bool:
        return path.startswith("/cdn-cgi/")  # runtime endpoints, never pages

    def clean_soup(self, soup) -> None:
        # Cloudflare-injected RUM beacon: reports to Cloudflare for the
        # ORIGIN zone -- off Cloudflare it only produces console/CORS errors
        removed = 0
        for script in soup.find_all("script", src=True):
            if "cloudflareinsights.com" in script["src"]:
                script.decompose()
                removed += 1
        if removed:
            with self.exp.stats_lock:
                self.exp.cruft_removed["cloudflare-beacon"] = (
                    self.exp.cruft_removed.get("cloudflare-beacon", 0)
                    + removed)

    def rewrite_soup(self, soup) -> None:
        # pre-decode obfuscated addresses so email links work on the static
        # site even without the runtime decode script (whose
        # /cdn-cgi/l/email-protection backend does not exist here)
        for tag in soup.find_all(attrs={"data-cfemail": True}):
            email = decode_cfemail(tag["data-cfemail"])
            if not email:
                continue
            tag.string = email
            del tag["data-cfemail"]
            classes = [c for c in (tag.get("class") or [])
                       if c != "__cf_email__"]
            if classes:
                tag["class"] = classes
            elif tag.get("class") is not None:
                del tag["class"]
            if tag.name == "a" and "email-protection" in (tag.get("href") or ""):
                tag["href"] = "mailto:" + email
        for a in soup.find_all("a", href=True):
            if "/cdn-cgi/l/email-protection#" in a["href"]:
                email = decode_cfemail(a["href"].split("#", 1)[1])
                if email:
                    a["href"] = "mailto:" + email


PLUGIN = Cloudflare
