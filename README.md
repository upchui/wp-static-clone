# wp-static-export

Pulls a **self-contained static mirror** of a WordPress site (or any other CMS site): sitemap-driven crawl, all assets mirrored, URL structure preserved 1:1, plus ready-to-use nginx/Docker deployment files and a verification report.

The result renders exactly like the live site when served over HTTP — without a single request to the origin domain for its own resources.

---

## Installation

Only `requests` and `beautifulsoup4` are required (Python 3.10+).

```bash
python3 -m venv .venv
.venv/bin/pip install requests beautifulsoup4
```

All examples below use `.venv/bin/python`; if you have the packages installed globally, `python3` works too.

---

## Quick start

```bash
# mirror a live site into ./export
.venv/bin/python wp-static-export.py https://www.example.at

# clean re-run (wipes ./export/public first) with polite throttling
.venv/bin/python wp-static-export.py https://www.example.at -o ./export --clean --delay 0.2
```

Afterwards the site is under `./export/public/`, along with `report.txt` / `report.json`, an `nginx.conf`, a `redirects.inc`, a `Dockerfile`, a `docker-compose.yml` and a `server.sh` (plus `_redirects` / `.htaccess` inside `public/` for Netlify/Apache).

---

## Viewing the result

The mirror uses **root-relative paths** and must therefore be served over HTTP (not by double-clicking `index.html` / `file://`). The generated `server.sh` is a thin wrapper around `docker compose`; the `docker-compose.yml` builds an image with the content **baked in** (via the `Dockerfile`, no bind mount):

```bash
cd export
./server.sh up             # docker compose up -d --build -> http://localhost:8080
./server.sh up 9000        # override the port at run time (or: PORT=9000 ./server.sh up)
./server.sh down           # docker compose down
./server.sh restart        # rebuild + recreate (after a re-export)
./server.sh status         # docker compose ps
./server.sh logs           # follow nginx logs
```

The service and container are named after the site host (e.g. `wpstatic-example-at`), so several exports can run side by side — just give each a distinct port.

The serving port can also be **baked in at export time** with `--port`, which becomes the default in the generated `docker-compose.yml` / `server.sh`:

```bash
.venv/bin/python wp-static-export.py https://www.example.at --port 9000
```

Because the content is copied **into** the image (no bind mount), `up` always rebuilds and re-exporting with `--clean` can never leave a stale mount behind. After a re-export, run `./server.sh up` again to pick up the new content.

Without Docker: `cd export/public && python3 -m http.server 8080`.

---

## Server reachable only by IP (no DNS entry)

If the target server has no DNS entry (e.g. an origin behind a CDN, reachable directly by IP), pass the **IP as `base_url`** and the logical hostname via **`--host`**. It is sent as the HTTP `Host:` header and used for TLS SNI / certificate verification — just like `curl --resolve`:

```bash
.venv/bin/python wp-static-export.py http://10.0.0.5:8080 --host example.at -o ./export --clean
```

All connections go to the IP, but URL classification and rewriting run on the logical host, so the HTML is localized cleanly to local paths.

### WordPress origin behind an HTTPS proxy

Many WordPress origins force HTTPS and answer an HTTP request carrying the real Host header with a **301 to their `https://` URL**. In that case the `X-Forwarded-Proto` header that a CDN/proxy normally sets is missing. Just send it along:

```bash
.venv/bin/python wp-static-export.py http://10.0.0.5:8080 --host example.at \
    --header 'X-Forwarded-Proto: https' -o ./export --clean
```

`--header 'Name: Value'` is repeatable (e.g. for auth headers or cookies too).

> Note: a scheme-less `base_url` defaults to `https://`. For plain-HTTP servers write `http://IP` explicitly. With HTTPS + IP the certificate is verified against the `--host` name; for self-signed certificates add `--insecure`.

---

## What is guaranteed

- **External hosts** (Google Fonts, Gravatar, analytics, CDNs …) stay **linked in the HTML but are never downloaded**.
- **Only the target site** is contacted: redirects are followed only while they stay on the target host; foreign sitemap references are skipped.
- **`wp-admin`, `wp-json`, `xmlrpc.php`, `admin-ajax` & co.** are never requested.
- After the export a **verification pass** checks that every local reference resolves to a file on disk and that no unexpected absolute origin URLs remain (result in `report.txt` / `report.json`).

## What cannot work statically

Form submissions (e.g. WPForms), consent/analytics AJAX and WordPress search need a server and do not work in the static mirror. URLs with query strings (`?p=123`, `?s=…`, `?lang=…`) are dropped from the export (listed in the report), and RSS/Atom feeds are intentionally excluded.

---

## SEO

The export is SEO-complete out of the box:

- **Generated sitemap** — a fresh `/sitemap.xml` is written from the URL set that actually made it into the export: `noindex` pages, redirect sources and canonical mismatches are excluded, `<lastmod>` comes from the `Last-Modified` header. The `robots.txt` `Sitemap:` line is pointed at it (a missing robots.txt is generated). `--no-generate-sitemap` keeps the origin sitemap files verbatim instead. After a domain move, submit `/sitemap.xml` in Search Console once.
- **One canonical URL per page** — the generated nginx config 301s `/page` → `/page/` (matching the exported structure) and serves the redirects observed on the origin as real 301s (`redirects.inc`). Redirect stubs carry `noindex`; `/404.html` answers 404 instead of an indexable 200.
- **WordPress cruft removed** — generator meta, wp-json/oEmbed/feed discovery, EditURI/RSD, wlwmanifest, pingback, `?p=` shortlinks and the Cloudflare Insights beacon (which only produces CORS errors off Cloudflare) are stripped. `canonical`, `hreflang`, `og:*`/`twitter:*` and JSON-LD stay. Disable with `--no-strip-wp-cruft`.
- **Image optimization** — `loading="lazy"` + `decoding="async"` on images (except each page's first image and plugin-lazyloaded ones) and `width`/`height` attributes read straight from the local PNG/JPEG/GIF/WebP headers (CLS). Disable with `--no-optimize-images`.

### Which domain do the SEO URLs point to?

`canonical`, `og:url`, hreflang, JSON-LD, sitemap `<loc>` and robots.txt keep **absolute URLs on the origin domain** in the exported files. How they become correct on the serving domain depends on the platform:

| Platform | Mechanism |
|---|---|
| **nginx / Docker** (generated files) | nothing to do — the generated `nginx.conf` substitutes the origin host with the **actually requested host at serve time** (`sub_filter`, `X-Forwarded-Proto`-aware). Any domain pointed at the container gets correct SEO URLs. |
| **Netlify, Apache, other static hosts** | no serve-time substitution possible — re-export with `--target-domain neue-domain.at`, which hard-rewrites all SEO-bearing URLs at export time. `public/_redirects` (Netlify) and `public/.htaccess` (Apache) carry the 301s/404 config there. |

For non-Docker nginx, check the module with `nginx -V 2>&1 | grep -o with-http_sub_module` (Debian/Ubuntu/Alpine builds have it). Never enable `gzip_static` in that server block — `sub_filter` cannot look inside precompressed files (the on-the-fly `gzip` runs after it and is fine).

### Staging deployments

`--staging` keeps a preview mirror out of every index — `robots.txt` `Disallow: /`, an `X-Robots-Tag: noindex, nofollow` header in the nginx/Apache configs and a `noindex` robots meta injected into every page — so it never competes with the live site as duplicate content.

---

## Key options

| Option | Effect |
|---|---|
| `-o, --out DIR` | output directory (default `./export`; web root ends up under `<out>/public`) |
| `--clean` | delete `<out>/public` before exporting (recommended for re-runs) |
| `--host HOST` | logical hostname sent as Host header + TLS SNI (for access by IP without DNS) |
| `--header 'N: V'` | extra request header, repeatable (e.g. `X-Forwarded-Proto: https`) |
| `--port N` | serving port baked into the generated `docker-compose.yml` / `server.sh` (default 8080) |
| `--no-rewrite` | keep HTML/CSS byte-identical to the origin (then only renders under the original domain) |
| `--delay SEC` | pause before each request (politeness) |
| `-c, --concurrency N` | parallel requests (default 5) |
| `--no-follow-links` | export only URLs listed in the sitemaps |
| `--no-mobile-check` | skip the mobile-vs-desktop comparison (saves one request per page) |
| `--insecure` | skip TLS certificate verification |
| `--extra-sitemap URL` | additional sitemap URL (repeatable) |
| `--max-pages N` | page cap (default 5000) |
| `--no-generate-sitemap` | keep origin sitemap files verbatim instead of generating `/sitemap.xml` |
| `--no-strip-wp-cruft` | keep WP head cruft (generator, wp-json/oEmbed discovery, shortlink, …) |
| `--no-optimize-images` | skip `loading=lazy` / `width`/`height` injection |
| `--staging` | full noindex mode (robots.txt, `X-Robots-Tag`, meta robots) for previews |
| `--target-domain D` | hard-rewrite canonical/og/JSON-LD/sitemap URLs to domain `D` (Netlify/Apache) |

Full list including examples: `wp-static-export.py --help`.

---

## Report

Every run writes `report.txt` (human-readable) and `report.json` (machine-readable) with:

- exported / failed pages and asset errors,
- observed redirects,
- SEO signals (noindex, canonical mismatches, missing titles/meta descriptions),
- referenced external hosts (linked, not downloaded),
- the verification result (missing local files, unexpected absolute references).

If the verification section is empty, the mirror is self-contained.

---

## License

This project is licensed under the **GNU General Public License, version 2 only (GPLv2)**. See [LICENSE](LICENSE) for the full text.
