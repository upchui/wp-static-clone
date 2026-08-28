"""Dynamic download endpoints (Download Monitor & co.): an extensionless
URL like /download/123/ answers with a file attachment at runtime -- a
request a static server cannot serve. The endpoint is materialized as a
REAL file next to its URL path, links are rewritten onto it in the
post-crawl pass, and the old endpoint URLs 301 onto the file in the
generated server configs.
"""
import mimetypes
import posixpath
import re
import unicodedata
from urllib.parse import unquote, urlsplit

from wp_static_export import (Plugin, canon_path, path_extension,
                              report_section)

CD_FILENAME_STAR_RE = re.compile(r"filename\*\s*=\s*[\w-]+''([^;]+)",
                                 re.IGNORECASE)
CD_FILENAME_RE = re.compile(r'filename\s*=\s*"?([^";]+)"?', re.IGNORECASE)


def attachment_filename(disposition: str, content_type: str,
                        url_path: str) -> str:
    """File name for a dynamically served download (WP plugins answer
    /download/123/ with Content-Disposition: attachment): RFC 5987
    filename* preferred, then plain filename=, else the last URL path
    segment plus an extension guessed from the content type. The result is
    a bare, traversal-safe file name."""
    name = ""
    m = CD_FILENAME_STAR_RE.search(disposition or "")
    if m:
        name = unquote(m.group(1).strip())
    else:
        m = CD_FILENAME_RE.search(disposition or "")
        if m:
            name = m.group(1).strip()
    name = posixpath.basename(name.replace("\\", "/")).lstrip(". ")
    name = unicodedata.normalize("NFC", name)
    if not name:
        seg = posixpath.basename(url_path.rstrip("/")) or "download"
        ext = mimetypes.guess_extension(
            (content_type or "").split(";")[0].strip()) or ""
        name = seg + ext
    return name


class Downloads(Plugin):
    name = "downloads"

    def __init__(self, exporter):
        super().__init__(exporter)
        # dynamic download endpoints materialized as real files:
        # "/download/123/" -> "/download/123/Vollmacht.pdf"
        self.download_map: dict[str, str] = {}

    def save_non_html_response(self, url: str, resp) -> bool:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "text/css" in ctype or "javascript" in ctype:
            # extensionless CSS/JS routes are NOT downloads -- the core
            # localizes/minifies them and discovers the URLs they contain;
            # claiming them here would ship them raw under a wrong name
            return False
        return self._save_download(url, resp) is not None

    def _save_download(self, url: str, resp) -> str | None:
        """Materialize a dynamic download endpoint (an extensionless URL
        like /download/123/ answering with a file) as a REAL file next to
        its URL path, so the link can be rewritten to something a static
        server can deliver. Returns the root-relative file path, or None
        when the URL already has a file extension (works as-is)."""
        exp = self.exp
        path = canon_path(urlsplit(url).path or "/")
        if path_extension(path):
            return None
        name = attachment_filename(
            resp.headers.get("Content-Disposition", ""),
            resp.headers.get("Content-Type", ""), path)
        base = path if path.endswith("/") else path + "/"
        file_path = canon_path(base + name)
        target = exp.local_path_for(
            f"{exp.scheme}://{exp.host}{file_path}", is_page=False)
        if target is None:
            return None
        written = exp.write_bytes(target, resp.content)
        if written is None and not target.is_file():
            # nothing landed on disk (path conflict) -- do NOT claim the
            # response or register the mapping: links rewritten onto the
            # file and 301 rules pointing at it would all be dead
            return None
        with exp.stats_lock:
            self.download_map[base] = file_path
            if written is not None:
                exp.asset_count += 1
        return file_path

    def wants_postprocess(self) -> bool:
        return bool(self.download_map)

    def postprocess_soup(self, soup) -> bool:
        """Point links at the materialized download files -- the dynamic
        /download/123/?cachebuster endpoints don't exist statically. Runs
        in the post-crawl pass because the map is only complete then."""
        if not self.download_map:
            return False
        changed = False
        for tag in soup.find_all(True):
            for attr in ("href", "src"):
                val = tag.get(attr)
                if not isinstance(val, str) or not val.startswith("/") \
                        or val.startswith("//"):
                    continue
                path = canon_path(
                    val.split("#", 1)[0].split("?", 1)[0] or "/")
                key = path if path.endswith("/") else path + "/"
                new = self.download_map.get(key)
                if new and val != new:
                    tag[attr] = new           # cache-buster query dropped
                    changed = True
        return changed

    def redirect_rules(self, seen_from: set) -> list:
        # externally shared/indexed download-endpoint URLs (with or
        # without trailing slash, any ?cachebuster) 301 onto the file;
        # the FROM side is emitted percent-decoded (nginx/Apache match the
        # decoded URI), the target stays encoded (becomes a Location)
        rules: list[tuple[str, str]] = []
        for endpoint, file_path in sorted(self.download_map.items()):
            endpoint = unicodedata.normalize("NFC", unquote(endpoint))
            for fp in (endpoint, endpoint.rstrip("/")):
                if not fp or fp in seen_from:
                    continue
                if (re.search(r"""[\s'"$\\{}]""", fp + file_path)
                        or any(ord(c) < 32 for c in fp + file_path)):
                    self.exp.warnings.append(
                        f"download redirect skipped (unsafe characters "
                        f"for server configs): {fp} -> {file_path}")
                    break
                seen_from.add(fp)
                rules.append((fp, file_path))
        return rules

    def summary_lines(self) -> list[str]:
        if not self.download_map:
            return []
        return [f"[seo] {len(self.download_map)} download endpoints "
                f"materialized as files (links rewritten, 301s generated)"]

    def add_report(self, report: dict, txt_head: list,
                   txt_sections: list) -> None:
        report["downloads"] = dict(sorted(self.download_map.items()))
        txt_sections.append(report_section(
            "Download endpoints materialized as files",
            sorted(self.download_map.items()),
            lambda d: f"{d[0]} -> {d[1]}"))


PLUGIN = Downloads
