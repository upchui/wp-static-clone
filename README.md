# wp-static-clone

[![ci](https://github.com/upchui/wp-static-clone/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/upchui/wp-static-clone/actions/workflows/ci.yml)

Pulls a **self-contained static mirror** of a WordPress site (or any other CMS site): sitemap-driven crawl, all assets mirrored, URL structure preserved 1:1, plus ready-to-use nginx/Docker deployment files and a verification report.

The result renders exactly like the live site when served over HTTP — without a single request to the origin domain for its own resources.

---

## Installation

Python 3.10+ (Linux/macOS — URLs containing NTFS-illegal characters produce per-file warnings on Windows). Required packages: `requests`, `beautifulsoup4` and — for the default minification — `rjsmin` + `rcssmin`:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**Updating an existing venv after a `git pull`**: run the same `pip install -r requirements.txt` again — a venv missing the minifier packages aborts with `error: minification (default on) needs the rjsmin and rcssmin packages …` (or run with `--no-minify`).

For development (test suite): `.venv/bin/pip install -r requirements-dev.txt`, then `pytest`.

All examples below use `.venv/bin/python`; if you have the packages installed globally, `python3` works too.

### System Python too old? (CentOS/RHEL 7, old Debian, …)

On hosts whose `python3` is older than 3.10, `pip install -r requirements.txt` fails ("No matching distribution found for requests>=2.32" — requests 2.32 needs Python ≥3.8, the tool itself needs 3.10). Don't fight the system Python — run the exporter in a throwaway container instead (Docker is needed for serving anyway):

```bash
./run-docker.sh https://www.example.at -o ./export --clean
./run-docker.sh http://10.0.0.5:8080 --host example.at -o ./export --clean
```

The wrapper uses `python:3.12-slim` with `--network host` (internal origin IPs stay reachable) and your own uid (export files belong to you, not root). Relative output paths (`-o ./static`) resolve in **your current directory**, so you can call it from any per-site folder; keep `-o` relative (only the current directory is mounted into the container). And if the machine is only meant to **serve** an existing clone, you don't need Python at all — copy the `export/` directory over and run `./server.sh up`.

---

## Quick start

```bash
# mirror a live site into ./export
.venv/bin/python wp-static-export.py https://www.example.at

# clean re-run (wipes ./export/public first) with polite throttling
.venv/bin/python wp-static-export.py https://www.example.at -o ./export --clean --delay 0.2
```

Afterwards the site is under `./export/public/`, along with `report.txt` / `report.json`, an `nginx.conf`, a `redirects.inc`, a `Dockerfile`, a `docker-compose.yml` (healthcheck included), a `.dockerignore` and a `server.sh` (plus `_redirects` / `.htaccess` inside `public/` for Netlify/Apache). If the mobile check finds UA-specific HTML, the mobile variants land under `./export/mobile-variants/` (removed by `--clean` like `public/`).

Note: `--delay` is a **global** minimum spacing between any two requests, shared across all workers — `-c 8 --delay 0.2` really means ~5 requests/second.

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

If the WordPress `siteurl` points at yet another (internal) domain — an admin host like `example-admin.internal.corp` — its URLs can leak into markup and REST responses. Such hosts are usually **detected automatically**: any host on the homepage that resolves to a private address from the export machine is treated as another spelling of the site and localized (`--no-resolve-internal` disables this). For hosts the detection can't see (public DNS via a WAF), declare them with `--internal-host example-admin.internal.corp` (repeatable); the report lists candidates under "Foreign hosts serving wp-content paths".

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
- **`wp-admin`, `xmlrpc.php`, `admin-ajax` & co.** are never requested. `wp-json` has exactly one read-only exception: the Slider Revolution endpoint, fetched once per slider at export time when a slider has lazy slides — without it SR7 sliders freeze on slide 1 in the static mirror (disable with `--no-sr7-hydrate`).
- After the export a **verification pass** checks that every local reference resolves to a file on disk and that no unexpected absolute origin URLs remain (result in `report.txt` / `report.json`).

## What cannot work statically

Form submissions (e.g. WPForms), consent/analytics AJAX and WordPress search need a server and do not work in the static mirror. URLs with query strings (`?p=123`, `?s=…`, `?lang=…`) are dropped from the export (listed in the report), and RSS/Atom feeds are intentionally excluded.

---

## SEO

The export is SEO-complete out of the box:

- **Generated sitemap** — a fresh `/sitemap.xml` is written from the URL set the origin sitemap declared and the export contains: `noindex` pages, redirect sources and canonical mismatches are excluded, and pages only discovered via links (WP attachment pages, cache artifacts) stay out unless you pass `--sitemap-include-linked`. `<lastmod>` comes from the `Last-Modified` header. The `robots.txt` `Sitemap:` line is pointed at it (a missing robots.txt is generated). `--no-generate-sitemap` keeps the origin sitemap files instead (unchanged except the localized XSL reference). After a domain move, submit `/sitemap.xml` in Search Console once.
- **One canonical URL per page** — the generated nginx config 301s `/page` → `/page/` (matching the exported structure) and serves the redirects observed on the origin as real 301s (`redirects.inc`), visitor query strings included; old origin sitemap URLs 301 onto the generated `/sitemap.xml`. Redirect stubs carry `noindex`; `/404.html` answers 404 instead of an indexable 200. On Netlify and Apache the trailing-slash canonicalization comes from the platform itself (Pretty URLs / `DirectorySlash`, both on by default).
- **WordPress cruft removed** — generator meta, wp-json/oEmbed/feed discovery, EditURI/RSD, wlwmanifest, pingback, `?p=` shortlinks, the Cloudflare Insights beacon (which only produces CORS errors off Cloudflare) and `dns-prefetch`/internal `preconnect` resource hints (which trigger browser Local-Network-Access prompts when they point at internal hosts) are stripped. `canonical`, `hreflang`, `og:*`/`twitter:*` and JSON-LD stay. Disable with `--no-strip-wp-cruft`.
- **Image optimization** — `loading="lazy"` + `decoding="async"` on images (except each page's first image and plugin-lazyloaded ones) and `width`/`height` attributes read straight from the local PNG/JPEG/GIF/WebP headers (CLS). Disable with `--no-optimize-images`.
- **Minification** — every exported HTML page, CSS and JS file (incl. `*.min.*`) plus inline styles/scripts (incl. `type="module"`) and `style` attributes is minified, with **all comments removed**: license banners, IE conditional comments and conditional-compilation relics, CSS hack pairs, legacy `<!-- ... //-->` comment-hiding wrappers, even HTML comments inside `<pre>` (they never render). JSON-LD, template/data script blocks and `<textarea>` content stay untouched; comment-lookalike *string literals* in plugin JS (e.g. SR7 WebGL shader placeholders) are functional code and remain. Disable with `--no-minify`.
- **Slider Revolution 7 works statically** — SR7 lazy-loads later slides from `/wp-json/…` at runtime, which freezes the slider on slide 1 in a static mirror. The exporter fetches each slider's full object once at export time and embeds the missing slide layers into the page (the runtime's own cache check then makes zero requests), downloading the later-slide images along the way. Disable with `--no-sr7-hydrate`.
- **Download endpoints become real files** — dynamic download URLs (`/download/123/?tmstv=…`, Download Monitor & co. serving `Content-Disposition: attachment`) are saved under their real file name (`/download/123/Vollmacht.pdf`), all links are rewritten onto the file, and the old endpoint URLs 301 onto it in every deploy format.

### Which domain do the SEO URLs point to?

`canonical`, `og:url`, hreflang, JSON-LD, sitemap `<loc>` and robots.txt keep **absolute URLs on the origin domain** in the exported files. How they become correct on the serving domain depends on the platform:

| Platform | Mechanism |
|---|---|
| **nginx / Docker** (generated files) | nothing to do — the generated `nginx.conf` substitutes the origin host with the **actually requested host at serve time** (`sub_filter`, `X-Forwarded-Proto`-aware). Any domain pointed at the container gets correct SEO URLs. |
| **Netlify, Apache, other static hosts** | no serve-time substitution possible — re-export with `--target-domain neue-domain.at`, which hard-rewrites all SEO-bearing URLs at export time. `public/_redirects` (Netlify) and `public/.htaccess` (Apache) carry the 301s/404 config there. |

For non-Docker nginx, check the module with `nginx -V 2>&1 | grep -o with-http_sub_module` (Debian/Ubuntu/Alpine builds have it). Never enable `gzip_static` in that server block — `sub_filter` cannot look inside precompressed files (the on-the-fly `gzip` runs after it and is fine).

### Staging deployments

`--staging` keeps a preview mirror out of every index — `robots.txt` `Disallow: /`, an `X-Robots-Tag: noindex, nofollow` header in the nginx/Apache configs (assets included) and a `noindex` robots meta injected into every page (rewrite mode) — so it never competes with the live site as duplicate content.

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
| `--no-generate-sitemap` | keep origin sitemap files instead of generating `/sitemap.xml` |
| `--no-strip-wp-cruft` | keep WP head cruft (generator, wp-json/oEmbed discovery, shortlink, …) |
| `--no-optimize-images` | skip `loading=lazy` / `width`/`height` injection |
| `--no-minify` | skip HTML/CSS/JS minification |
| `--staging` | full noindex mode (robots.txt, `X-Robots-Tag`, meta robots) for previews |
| `--target-domain D` | hard-rewrite canonical/og/JSON-LD/sitemap URLs to domain `D` (Netlify/Apache) |
| `--sitemap-include-linked` | also list link-discovered pages in the generated sitemap |
| `--fail-on {none,errors,verify}` | CI exit-code policy, see below |
| `--exclude REGEX` | skip pages and assets whose URL path matches (repeatable) |
| `--internal-host H` | additional spelling of the same site (admin domain etc.), localized like the main domain (repeatable) |
| `--respect-robots` | honor robots.txt Allow/Disallow (`*`/`$` supported) and Crawl-delay |
| `--no-sr7-hydrate` | don't embed lazy Slider Revolution 7 slides (sliders may freeze statically) |
| `--no-resolve-internal` | skip the split-DNS detection of additional same-site hosts |
| `-q, --quiet` | suppress per-round progress output |
| `--version` | print version and exit |

**Exit codes**: `0` success · `1` no page exported, or (with `--fail-on errors|verify`) page/asset errors or a `--max-pages`-truncated crawl · `2` (with `--fail-on verify`) the self-containedness verification found problems · `130` aborted with Ctrl-C.

Full list including examples: `wp-static-export.py --help`.

---

## Plugins

The core (`wp-static-export.py`) contains only the generic export pipeline — sitemap discovery, crawl, URL rewriting, verification, deployment files, report — plus a fixed set of **hook points** (called at defined stages of the pipeline) and **registries** (attribute names, URL patterns and the like that the core reads). Everything feature- or vendor-specific attaches to those and lives as a plugin in the [`plugins/`](plugins/) folder next to the script:

| Plugin | Provides |
|---|---|
| `minify` | HTML/CSS/JS minification (`--no-minify`) |
| `slider_revolution` | SR7 lazy-slide hydration, `data-dbsrc` URLs, runtime resource discovery (`--no-sr7-hydrate`) |
| `image_optimize` | `loading=lazy` / `width`/`height` injection, header-based image-size readers (`--no-optimize-images`) |
| `mobile_check` | the mobile-vs-desktop HTML comparison incl. `mobile-variants/` output (`--no-mobile-check`, `--mobile-user-agent`) |
| `downloads` | dynamic download endpoints materialized as real files, link rewriting, 301 rules |
| `wordpress` | WP head-cruft removal, WPForms AJAX skip, WooCommerce cart/checkout skip |
| `lazyload` | lazy-load plugin attributes (`data-src`, `data-lazy-src`, `data-lazy-srcset`, `data-bg`) |
| `cloudflare` | email de-obfuscation, Insights-beacon removal, `/cdn-cgi/` handling |
| `complianz` | consent-banner lazy attributes and banner-CSS URL template |
| `theme_fixes` | The7 `data-dt-location` links, builder placeholder images, Ultimate-Addons id noise in the mobile comparison |

How it works, in one paragraph: every `plugins/*.py` is loaded automatically at startup (alphabetical file order = hook call order) and must expose `PLUGIN = <subclass of Plugin>`. Loading happens **before** the CLI is parsed, which is why plugin-owned flags (`--no-minify`, `--no-sr7-hydrate`, `--no-optimize-images`, `--no-mobile-check`, …) appear in `--help` under their own `plugin: <name>` groups — delete a plugin file and its flags disappear with it. Loading fails **loudly**: a missing `plugins/` folder or a broken plugin file aborts the run instead of silently producing a degraded export; disable a single plugin by renaming it to `_<name>.py` (or deleting it). Each export run gets fresh plugin instances with full access to the exporter — plugins are trusted local code, not a sandbox.

The complete developer reference — loading lifecycle, all hooks and registries with the pipeline stages they fire at, thread-safety rules, pitfalls, testing patterns and a runnable worked example: [`plugins/README.md`](plugins/README.md).

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

## Changelog

Release history: [CHANGELOG.md](CHANGELOG.md).

---

## License

This project is licensed under the **GNU General Public License, version 2 only (GPLv2)**. See [LICENSE](LICENSE) for the full text.
