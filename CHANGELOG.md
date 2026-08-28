# Changelog

## 2.1.1 (2026-08-28)

### Fixed
- The mobile comparison's id-noise normalization (theme_fixes) now also
  covers 2-digit rand() suffixes (`-\d{2,7}` instead of `-\d{3,7}`).
  Observed on a real site: Ultimate Addons emitted `class="uvc-48"` on
  one request and a 3-digit suffix on the next, so a responsive page was
  falsely reported as serving different mobile HTML.

## 2.1.0 (2026-08-28)

### Changed
- **Plugin extraction round 2**: everything feature/vendor-specific that
  remained in the core moved to plugins -- new `plugins/lazyload.py`
  (data-src/data-lazy-src/data-bg/srcset lazy attributes),
  `plugins/downloads.py` (dynamic download endpoints incl. their 301
  rules), `plugins/wordpress.py` (WP head cruft, WPForms AJAX skip,
  WooCommerce cart/checkout skip) and `plugins/mobile_check.py` (the
  whole mobile-vs-desktop comparison incl. `normalize_html`). The
  image-size readers moved into `plugins/image_optimize.py`. The core
  keeps only generic machinery: resource-hint stripping stayed core
  (renamed `strip_resource_hints`; Local-Network-Access policy), as did
  the wp-admin/wp-login/wp-json/xmlrpc crawl policy, `--staging` and the
  internal-host detection.
- New plugin API surface: registries `srcset_attrs`, `css_url_attrs`,
  `lazy_img_attrs`, `page_skip_pattern_fragments`, `extra_output_dirs`;
  hooks `skip_asset_candidate`, `save_non_html_response`, `page_fetched`,
  `page_saved`, `redirect_rules` (first deploy-file hook).
- CLI flags unchanged; `--no-mobile-check` / `--mobile-user-agent` now
  appear under the `plugin: mobile_check` group in `--help`.
- Cosmetic (content unchanged): `report.json` keys `downloads` and
  `mobile` are appended by their plugins (position shifts);
  in `report.txt` the downloads and the three mobile sections moved into
  the plugin block near the end; the console mobile summary line moved up
  into the plugin summary block; the two closing `[note]` lines no longer
  name WPForms/SR7 ("form submissions, consent/analytics AJAX and site
  search", "wp-admin/xmlrpc are never requested"); the
  `verification.policy` string is now assembled from a generic base plus
  the Slider Revolution clause ("; wp-json is requested only for the
  read-only Slider Revolution endpoint when a slider has lazy slides
  (--no-sr7-hydrate disables)").
- `Config.mobile_user_agent` defaults to `""` now (meaning: the plugin's
  iPhone UA); `--mobile-user-agent` behaves exactly as before.

## 2.0.0 (2026-08-28)

### Changed
- **Plugin architecture**: everything feature- or vendor-specific moved out
  of the core into `plugins/*.py`, loaded automatically at startup --
  `minify`, `slider_revolution` (SR7 hydration, `data-dbsrc`, runtime
  resources), `image_optimize`, `cloudflare` (cfemail decode, Insights
  beacon, `/cdn-cgi/`), `complianz` and `theme_fixes` (The7
  `data-dt-location`, Ultimate-Addons id normalization). The core now only
  provides discovery, crawl, URL rewriting, verification, deploy files and
  the report, plus the hook points plugins attach to
  (see `plugins/README.md`).
- All CLI flags (`--no-minify`, `--no-sr7-hydrate`, `--no-optimize-images`)
  keep working unchanged; in `--help` they now appear under
  `plugin: <name>` groups. Exported output is identical to 1.5.3 for
  identical input.
- Cosmetic: `report.json` key order shifts slightly (plugin-contributed
  keys like `sr7_hydrated` and `seo.minified` are appended); in
  `report.txt` the SR7 section moved towards the end, and the position of
  the images/minify/SR7 console summary lines shifted. Content unchanged.
- Plugin loading fails loudly (missing `plugins/` directory, a plugin file
  without a valid `PLUGIN`, or an import error abort the run); disable a
  plugin by deleting its file or renaming it to `_<name>.py`.

## 1.5.3 (2026-08-27)

### Changed
- Missing `rjsmin`/`rcssmin` packages are now a hard CLI error (with the
  `pip install -r requirements.txt` fix in the message) instead of a
  warning that silently produced an unminified export from a stale venv.
  `--no-minify` still allows running without them.

## 1.5.2 (2026-08-27)

### Changed
- HTML minification now removes IE conditional comments as well (the
  downlevel-revealed pattern keeps its enclosed markup); CSS minification
  strips the preserved IE5/Mac hack pair `/*\*/ ... /**/`. With that, no
  comments remain in exported HTML/CSS/JS. Remaining `/*...*/` matches in
  some JS files are string literals used by plugin code (SR7 WebGL shader
  placeholders, injected style text) -- functional code, not comments,
  intentionally untouched.

## 1.5.1 (2026-08-27)

### Changed
- Minification also processes `*.min.css`/`*.min.js` files (safe near-no-op
  for already-minified code) and explicitly drops `/*! license banner */`
  bang comments everywhere.

## 1.5.0 (2026-08-27)

### Added
- **Minification** (default on, `--no-minify` disables, requires the new
  `rjsmin`/`rcssmin` dependencies): CSS and JS assets are minified
  (`*.min.*` files skipped), inline `<style>`/`<script>` blocks too, and
  HTML gets a conservative pass — comments removed (conditional comments
  stay), indentation collapsed to single newlines. pre/textarea content,
  JSON-LD and byte-identical HTML assets are never touched; without
  `--rewrite` nothing is minified. The report shows file counts and bytes
  saved.

## 1.4.3 (2026-08-27)

### Added
- **Split-DNS auto-detection**: hosts found on the homepage that resolve
  (from the export machine) exclusively to private addresses — e.g. an
  internal WP admin domain the siteurl points at — are automatically
  treated as additional spellings of the site and localized. Disable with
  `--no-resolve-internal`. Privately-resolving hosts first seen later in
  the crawl produce a warning with a ready-made `--internal-host` hint.
- Foreign hosts serving `/wp-content/`/`/wp-includes/` paths (in markup or
  SR7 REST layers) are reported prominently as "very likely the same
  WordPress site under another name" with the exact `--internal-host`
  command to use — not auto-localized, since they could be a real upload
  CDN.

## 1.4.2 (2026-08-27)

### Fixed
- **Resource hints no longer trigger the Local Network Access prompt**:
  WordPress emits `<link rel="dns-prefetch">`/`preconnect` for every
  resource host — pointing at an internal host they make browsers attempt
  a connection WITHOUT any visible network request. dns-prefetch hints are
  now always stripped; preconnect hints are stripped when they target an
  internal or private host (public font preconnects stay).

### Added
- `--internal-host HOST` (repeatable): declare additional spellings of the
  same site (e.g. an internal WP admin domain the siteurl points at) —
  their URLs are localized like the main domain.
- Verification also scans standalone `.js` files and CSS for private/
  loopback URLs (incl. `ws://`/`wss://`).

## 1.4.1 (2026-08-27)

### Fixed
- **No more browser "Local Network Access" prompts**: WordPress behind a
  mapped port (crawled via `10.x.x.x:81`, running on `:80` inside a
  container) emits some URLs — e.g. in SR7 REST responses — with the bare
  IP and no port. That spelling wasn't recognized as internal, survived
  unlocalized, and made browsers prompt before loading slide images from
  the private IP. Every port spelling of the connect address is now
  treated as internal and localized.
- New verification safety net: any leftover private/loopback URL
  (RFC 1918, 169.254, 127.x, localhost, `*.local`) whose host is not the
  site's own is flagged in the report.

## 1.4.0 (2026-08-27)

### Fixed
- **Slider Revolution 7 sliders no longer freeze on slide 1**: SR7
  lazy-loads slides whose inline layers are empty via
  `/wp-json/sliderrevolution/sliders/<id>?slideid=…` at runtime — a request
  that 404s on a static mirror, leaving the runtime awaiting a promise that
  never resolves. The exporter now fetches the full slider object once per
  slider at export time and embeds the missing layers into the inline
  `SR7.JSON` blob; the runtime's own cache check then skips the fetch
  entirely, and the later-slide images are discovered, downloaded and
  localized for the first time. This is the only wp-json request the
  exporter ever makes; disable with `--no-sr7-hydrate`. Stream-source
  sliders (separate endpoint) are reported as a warning.

## 1.3.1 (2026-08-27)

### Fixed
- **Dynamic download endpoints work statically**: extensionless URLs like
  `/download/123/?tmstv=…` (Download Monitor & co., served with
  `Content-Disposition: attachment`) are now materialized as real files
  next to their URL path (`/download/123/Vollmacht-04.2021.pdf`, name from
  Content-Disposition incl. RFC 5987 `filename*`), all HTML links are
  rewritten onto the file, and the old endpoint URLs (with or without
  trailing slash, any cache-buster query) 301 onto it in all three deploy
  formats. Previously these were saved as a misplaced extensionless file
  and every download link 404'd.
- Query-string links additionally queue their bare-path variant for
  crawling, so such endpoints are discovered even when no sitemap lists
  them.

## 1.3.0 (2026-08-26)

### Fixed
- **Redirect rules no longer 301-loop**: an origin redirect that only adds a
  query string (`/a/` → `/a/?x=1`, common with consent/language plugins)
  produced a rule matching its own target — the page became unreachable.
- **Visitor query strings survive 301s again**: the generated nginx rules
  only suppress argument forwarding when the target carries its own query,
  so `?utm_…` parameters are no longer stripped on every redirect.
- Generated `sitemap.xml` / `robots.txt` now use the site's public scheme
  (`https` when crawling an internal origin with `X-Forwarded-Proto: https`)
  instead of contradicting the pages' canonical tags.
- Scheme-default ports (`https://host:443/…`, `http://host:80/…`) are
  recognized as the same origin and localized.
- A redirect onto a query-string URL writes a noindex stub instead of
  duplicating the target page under the source path.
- WordPress REST discovery links (`rel="alternate" type="application/json"`
  pointing at `/wp-json/…`) are stripped like the other WP cruft.
- Quoted `charset="…"` Content-Type parameters are honored when decoding
  CSS/JS.
- The dynamic-endpoint policy check no longer flags innocent uploads like
  `wp-login-screenshot.png` (exact-name matching below the top level).
- `capture_404`/`capture_favicon` run inside the crash guard; the
  postprocess list only contains files that were actually written.
- Old origin sitemap paths (`/sitemap_index.xml`, …) get 301s onto the
  generated `/sitemap.xml`; `--max-pages` truncation fails the run under
  `--fail-on`; remaining `--quiet` gaps closed; report now distinguishes
  `policy_checks` from `reference_checks`.

### Added
- `--exclude REGEX` (repeatable): skip pages and assets by URL path.
- `--respect-robots`: honor `User-agent: *` Allow/Disallow (with `*`/`$`,
  longest match wins) and adopt `Crawl-delay` as a minimum `--delay`.
- Large media (`mp4`, `pdf`, `zip`, …) is streamed to disk in 1 MiB chunks
  instead of being buffered fully in RAM.

## 1.2.0 (2026-08-26)

- Fixed silent output corruption: unanchored host regexes destroyed
  lookalike external domains; CSS without a charset header was decoded as
  Latin-1 (mojibake); re-serialization lowercased case-sensitive SVG
  attributes (`viewBox`).
- Redirect deploy rules: query preservation, nginx quoting/validation,
  Apache `RedirectMatch` instead of prefix-matching `Redirect`.
- `--staging` X-Robots-Tag now also covers assets; redirects onto
  `wp-login.php` & co. are never followed; attachment pages run through the
  full rewrite pipeline; 404-page assets are fetched; crashed workers count
  as failed pages; link-only junk is excluded from the generated sitemap;
  the mobile comparison normalizes WordPress per-request noise; IDN hosts
  match in both spellings; JPEG EXIF orientation is respected.
- Added: test suite (pure/unit/e2e), GitHub Actions CI, `requirements.txt`,
  `--version`, `--fail-on`, `--quiet`, `--sitemap-include-linked`, global
  `--delay` rate limiting, security headers + healthcheck + `.dockerignore`
  in the deploy files.

## 1.1.0 (2026-08-26)

- SEO toolkit: serve-time domain substitution via nginx `sub_filter`,
  generated `/sitemap.xml` + robots rewrite, real 301s (`redirects.inc`),
  trailing-slash canonicalization, non-indexable `404.html`, WP head cruft
  stripping (incl. Cloudflare beacon), `loading=lazy`/`width`/`height`
  injection, `--staging`, `--target-domain`, Netlify `_redirects` +
  Apache `.htaccess`.

## 1.0.1

- Initial public version: sitemap-driven crawl, asset mirroring, URL
  structure preserved 1:1, verification pass, nginx/Docker deploy files,
  mobile comparison, report.
