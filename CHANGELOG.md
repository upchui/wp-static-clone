# Changelog

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
