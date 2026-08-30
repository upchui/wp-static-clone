# Changelog

## 2.15.1 (2026-08-30)

### Fixed
- **A search box on the 404 page counts as a search entry point** (hole in
  the rule added in 2.14.0). Ruling it out was wrong: many themes put a
  search box in their 404 template, and it is a real one a visitor can
  use. On the reference site it is the *only* one left, and 2.14.0 shipped
  it as `<form action="/">` — submitting it lands on `/?s=term`, which
  every static host answers with the homepage. That silent wrong success
  is precisely what this plugin exists to prevent.

  `pre_discover_soup()` skipped `/404.html` outright; that skip had two
  jobs and only one was right. The 404 page is still kept out of the
  search index (it is not content), but it is now asked whether it offers
  a search. `capture_404()` runs it through that hook as the first
  post-crawl step, so the answer is known before anything is wired.
  `seo.search.search_entry` names where the entry point was found.

## 2.15.0 (2026-08-30)

### Changed
- **`/search-index.json` is always loaded as its own file.** Until now an
  index of up to 256 KB was also inlined into every results page. It is
  now only ever fetched: one request the browser caches across searches
  and across pages, and a results page that carries no copy of the index
  (85 KB instead of 105 KB on the reference site). The inlining path,
  its size threshold and the `index_inlined` report field are gone.

  Trade-off, stated plainly: the cards now arrive *after* the theme's
  grid code has initialized, so the renderer has to tell the grid to pick
  them up — Isotope `reloadItems`/`layout`, else a `resize` event. That
  path already existed for indexes above the old threshold and is
  unchanged. A results page opened straight from disk (`file://`) can no
  longer render, since the fetch is blocked there; serving the export
  over HTTP was already documented as required.

## 2.14.0 (2026-08-30)

### Changed
- **A site with no search gets no results page.** If not one exported page
  offers a way *into* a search — no search box, no internal `?s=`/`?q=`
  link — the results page, `/search-index.json`, the live probes it costs
  and the `?s=` redirect script on every page are all skipped. Nothing
  would lead there. On the reference site, whose search box was taken out
  of the menu, that is one page, one 19 KB index, 5 live requests and 42
  card assets that are no longer produced, plus a redirect script gone
  from 47 pages.

  The entry point is looked for with the plugin's existing generic form
  detector (`role="search"` or an `<input name="s|q">`) plus internal
  links carrying a search query — an external `google.com/search?q=…`
  does not count. The theme's **404 template does** — see 2.15.1.

  The JSON-LD `SearchAction` is left untouched in that case, as it is
  under `--no-search` — repointing it at a page that will not exist
  would be worse.
- **`--force-search`** builds the page regardless, for a theme that
  creates its search box in JavaScript or when you link the path
  yourself.
- The closing note no longer points at an `[seo] site search` line that
  may not be there.

## 2.13.0 (2026-08-30)

### Added
- **Empty term archives are recognized as artifact pages.** A plugin that
  registers a taxonomy for its own bookkeeping — statistics, import
  batches — on a post type that is not publicly queryable leaves one term
  archive per term whose loop returns nothing. The page is WordPress'
  "Nothing found" template and Yoast still declares it `index, follow`,
  so neither the `noindex` filter nor "the origin sitemap declared it"
  keeps it out. On one real site six such archives made up **more than
  half of the generated `sitemap.xml`** (6 of 11 URLs) and 6 of 15
  documents in the search index.

  Detected from two WordPress *core* signals, not from theme class names:
  `body_class()` marks every `is_archive()` page with `archive`, and
  core's own `content-none.php` renders `class="no-results not-found"`.
  Only both together count, class attributes are compared as whole
  tokens, and the check runs on the response bytes the artifact
  classifier already reads (so it works under `--no-rewrite` too).
  Deliberately excluded: `blog` (on a site whose front page is the blog
  that class sits on the homepage) and `search` (our own results page
  carries it).

  Such pages are still **exported** — a stray link must not 404 — but
  kept out of `sitemap.xml` and the site search, like attachment and
  image-cache pages. `report.json` counts them under
  `seo.artifact_pages.empty_archives`.
- **`--list-empty-archives`** lists them anyway, for a site where an
  archive that happens to be empty is still meant to be advertised. It is
  separate from `--list-attachment-pages`; neither switches the other off.

## 2.12.1 (2026-08-30)

### Fixed
- **A one-result probe response could decide the shape of every card**
  (regression in 2.12.0). The harvest opens with a deliberately rare
  term, and on a small site that term matches exactly one page. 2.12.0
  taught the new `post_class()` fallback to read a card out of such a
  response — but `post-<id>`/`hentry` sit on the `<article>`, which on
  themes that wrap entries in grid cells (dt-the7) is one level *below*
  the item. The signature learned from it then matched `<article>` in
  every later response, so the results container became a single grid
  **cell**: every result was rendered into one cell of the masonry, and
  the cell's `data-post-id`/`data-date`/`data-name` were lost from the
  template. Two rules now prevent it: a card signature is only learned
  from a response that actually **repeated** something, and the
  `post_class()` fallback is a genuine last resort — it runs only after
  no response at all has shown a real loop, which is exactly the case it
  was built for (a search that can never return two results).
- The live regression check that missed this compared the `<article>`
  inside each grid cell — the one part that stayed correct. It now
  compares the loop container and each card *including* its wrapper.

## 2.12.0 (2026-08-30)

The static search now *reads* a theme's result card instead of guessing
at it. Which part of a card holds the excerpt, the date or a "read more"
button is no longer a list of class names in this repo — it is measured
from the live cards themselves.

### Changed
- **Card slots are found by comparing live cards, not by matching
  selectors.** Up to six cards of one response are walked position by
  position: what two cards render *identically* is the theme's own
  markup and is kept verbatim (a button label, the word before the
  author, an icon); what *differs* is the post's own value and becomes a
  slot filled per result. A position we can also name — the post link,
  the title, the date, the author, the excerpt — always becomes a slot,
  even when this handful of cards happens to agree on it. Everything
  else that varies gets a numbered slot of its own, so a value with no
  name is still rendered per post instead of being frozen on the card it
  was harvested from. Index schema `v3` (slots gained `v` and `o`).
- **The excerpt is found wherever the theme keeps it.** The old rule was
  "the last `<p>` in the card", which silently produced a bare list of
  title links on any theme that uses a `div` — `div.entry-summary`
  (parldigi.ch) and `div.entry-excerpt` (the7's `articles-list`
  shortcode) both do. It is now the longest per-post text the card
  shows, confirmed against the page's own indexed text.
- **`report.txt` says which parts of the live card were recognized**
  (`Site search: parts of the live result card recognized`, also
  `seo.search.harvest.slots`), and how many live cards the verdict was
  measured on. A card layout that had to be guessed from a single live
  result now warns instead of quietly rendering less.

### Fixed
- **A second link to the same post kept the first card's URL.** Themes
  that render a "Mehr Info"/"Read more" button next to the title (the7's
  `articles-list`) sent *every* result to whichever post the template
  was harvested from. Every link to the card's own post is now a slot.
- **The card signature carried per-post classes.** It was taken from one
  card verbatim, so a theme whose entries carry `post-1575` or an
  odd/even alternation matched only the single card it came from and
  every later probe response was silently discarded. The signature is
  now what all cards of a response share.
- **A result loop directly inside the content well was not found.**
  `_find_results` only looked at descendants, so themes without a
  wrapper around the loop (Twenty Twenty-One and its children) fell
  through to the built-in markup.
- **A card containing two links to its own post looked like a
  two-result loop**, which made the reader mistake the inside of one
  card for the loop. Children now have to point at *different* posts.
- **A card wrapped entirely in one `<a>` looked linkless**, because
  bs4's `find_all` never returns the node itself.
- **A single-result response is recognized** through WordPress' own
  `post_class()` marks (`post-<id>`/`hentry`) instead of being given up
  on as ambiguous.
- The per-post tooltip on a date link (`title="17:42"`) was deleted from
  the template; it is kept per post now.
- More content wells are recognized (`.site-main`, `.page-content`,
  Elementor, Divi, Beaver Builder). Pages where none is found are
  counted and warned about: their whole `<body>` goes into the index, so
  menu and footer text can make them match searches the live site
  answers with nothing.

## 2.11.0 (2026-08-30)

Multilingual step 2: the language-shaped output is now actually per
language. (Step 1 in 2.10.0 stopped the silent failures.)

### Added
- **`Exporter.language_prefixes()`** derives the site's layout from the
  URLs: each language's shared path prefix, the default language at `/`.
  Handles both `/de/` + `/en/` and Polylang's unrewritten
  `/language/de/`; refuses to guess (and says so) when two languages
  share a prefix. Reported as `seo.language_prefixes`.
- **The site search is per language.** One results page per language,
  each harvested from *that* language's own `/xx/?s=` endpoint, so it
  carries its own chrome, heading, `<title>` pattern and "nothing found"
  block. Every page's search form, JSON-LD `SearchAction` and `?s=`
  redirect point at the results page of its own language, chosen from
  the page's `<html lang>`. The shared index tags every document with
  its language and a results page only offers its own — as Polylang and
  WPML filter the live search. The probe budget is split across the
  languages, so a multilingual export costs no more live requests than a
  monolingual one, and each language's probe terms come from its own
  corpus. A collision on one language's path now stands down only that
  language.
- **One themed `404.html` per language subtree** (`/404.html`,
  `/fr/404.html`), with matching `error_page` blocks in the generated
  nginx config (including a nested asset block, since a `^~` prefix
  location outranks the regex one) and a per-directory `ErrorDocument`
  for Apache. Netlify needs nothing. Listed in `deploy.error_pages`.
- **`hreflang` in the generated `sitemap.xml`**: new
  `PageRecord.alternates`, collected from each page's own
  `<link rel="alternate" hreflang>`, emitted as `xhtml:link` and
  filtered to URLs the sitemap actually lists. Verification now checks
  those hrefs too — and no longer swallows a sitemap parse error.

### Fixed
- Two latent bugs a second real theme surfaced: the results-page heading
  is now the `<h1>` that actually **echoes the query** (a theme printing
  three `<h1>` would otherwise have had its site title rewritten), and a
  localized `<time datetime="2. Juni 2026">` no longer poisons the
  result order — only an ISO-ish value is used as the sort key, else the
  page's own JSON-LD date.

## 2.10.0 (2026-08-30)

Multilingual hardening, step 1: stop the silent failure modes and make
the language situation visible. (Per-language search follows separately.)

### Fixed
- **A `Set-Cookie` is never stored any more.** The `requests.Session`
  jar was shared by every crawl worker and never cleared, so a single
  language or consent cookie (`pll_language`, `_icl_current_language`)
  was replayed for the rest of the run — the crawl could pin itself to
  one language mid-export and write the wrong-language body under the
  right path, with no warning. A static mirror is now always built from
  cookie-free responses; `--header 'Cookie: …'` still works for sites
  behind a cookie gate. Regression-tested end to end: the fixture origin
  serves a different body when its cookie comes back, and removing the
  policy makes that test fail.

### Added
- **`Vary: Accept-Language` / `Vary: Cookie` is detected** and reported:
  the origin picks the variant per request, a mirror can only freeze one
  per URL. Listed in `report.json` under `seo.runtime_negotiated_pages`.
- **Language breakdown per export.** New `PageRecord.lang` (from
  `<html lang>`), a `Languages:` line in `report.txt` and
  `seo.languages` in `report.json`, counted per primary subtag (`de-AT`
  and `de-DE` are one `de`).
- **A warning when links to another internal host are folded onto the
  main host.** They share one output tree and the first write of a path
  wins — harmless for a `www.`/IP alias, silent content loss when the
  hosts serve different content (one language each). Counted in
  `seo.folded_internal_hosts`.
- README section "Multilingual sites": what is supported (one language
  per path on one host), what is generated single-language (search,
  `404.html`), what is lost (sitemap `hreflang`) and what is not
  supported (one host per language, `?lang=` URLs).

### Changed
- The search plugin's multilingual warning said the language decides the
  results-page path and offered `--search-path` as the fix — both wrong
  since 2.9.0. It now names what is actually degraded (one results page,
  one mixed-language index) and points at `--no-search`. It also no
  longer fires for mere dialects (`de-DE` + `de-AT`).

## 2.9.1 (2026-08-30)

### Fixed
- **An empty query listed nothing.** `/search/?s=` answered "Bitte einen
  Suchbegriff eingeben."; WordPress treats an empty search as
  `LIKE '%%'` and returns *every* page, all of them in the title bucket,
  i.e. plain `post_date` descending. Verified against the live site: 16
  results in the identical order. The prompt string is gone.

## 2.9.0 (2026-08-30)

The exported search now returns results in WordPress' own order.

### Changed
- **Ranking is `WP_Query::parse_search_order`, verbatim.** The old
  hand-rolled score (title 10/5, description 4/2, text 3/1, alphabetical
  tie-break) put results in an order the live site never produces. Now:
  a relevance bucket first -- one term → "the post_title contains it",
  several terms → WordPress' CASE ladder (whole phrase in title / all
  terms in title / any term in title / phrase in excerpt / phrase in
  content / rest) -- and `post_date` descending inside each bucket.
  Measured against the live site for seven queries: **the static order
  is now identical to the live order**, where it previously matched on
  zero of the first seven positions for a query like "arbeiten".
  - The bucket reads the real `post_title` from the harvested card, not
    the SEO `<title>` (WordPress ranks the former; for a home page
    called "Home" the two differ completely).
  - `post_date` comes from the harvested card; pages without one fall
    back to the `datePublished` of their JSON-LD (or an OpenGraph
    `article:` timestamp / a `<time datetime>`), which sorts identically.
- **The site search now returns `noindex` and link-only pages**, while
  the sitemap still excludes them: both are instructions to search
  *engines*, and WordPress' own site search returns such pages (on the
  reference site this brings back `/impressum/`). Artifact, stub, error
  and canonical-duplicate pages stay out of both.
- **The results page lives at `/search/` for every site.** The path is a
  URL, not UI text -- the page's own wording still follows
  `<html lang>`. `--search-path` overrides as before.

### Fixed
- The theme's `<!-- #post-0 .post .no-results .not-found -->` comment
  showed up as **visible text** under the "nothing found" message:
  bs4's `str()` on a Comment node returns its bare text without the
  `<!--`/`-->` delimiters. Harvested fragments now drop comment nodes
  before serialization (which also keeps the export's
  no-comments-anywhere policy).

### Known differences from the live search
- No pagination: live shows 10 hits per page via `/page/2/?s=…`, a URL a
  static host cannot serve; all hits (up to 50) render in one grid.
- WordPress matches the **raw** `post_content`, page builder shortcodes
  included, so it occasionally returns a page whose visible text does
  not contain the term at all. A front-end crawl cannot see that source.

## 2.8.0 (2026-08-30)

### Fixed
- **WordPress artifact pages no longer pollute `sitemap.xml` and the
  site search.** WordPress gives every uploaded file its own attachment
  page, and image plugins add cache directories (phpThumb & co.). The
  previous rule -- "keep what the origin sitemap declared" -- let all of
  them through, because Yoast's *attachment sitemap* declares them: on
  the reference site 30 of 42 sitemap entries and 30 of 42 search
  documents were logos, cache dirs and image pages (`sitemap.xml` is now
  15 URLs, the search index 15 documents, all of them real content).
  They are still exported -- a theme's lightbox may link to them -- but
  are advertised nowhere. `--list-attachment-pages` restores the old
  behavior for sites where the attachment page *is* the content.
  - Detection is WordPress' own `body_class()` output (whole class
    tokens `attachment` / `attachment-template-default` /
    `single-attachment` / `attachmentid-N` on `<body>`), so it is
    theme-independent; measured on the reference site it flags 30 of 30
    artifacts and 0 of 17 content pages. Cache directories are also
    caught by URL (`phpthumb_cache…`) for themes that do not emit the
    body classes.

### Changed
- New `Exporter.index_exclusion(page)` is now the single source of truth
  for "does this page belong in the generated sitemap and the site
  search". `write_generated_sitemap` and `plugins/search.py` both use
  it, so the two answers can no longer drift apart -- the duplicated
  filter is exactly why this bug hit both outputs at once. The sitemap
  report gains an `excluded_artifacts` counter; every existing counter
  key is unchanged.
- New plugin-owned `PageRecord.artifact` field (written by
  `plugins/wordpress.py`).

## 2.7.0 (2026-08-29)

The static search-results page now looks like the live one.

### Changed
- **The results-page design is harvested from the live site** instead of
  being invented. Up to `PROBE_MAX_REQUESTS` (12) `GET /?s=<term>`
  probes at the end of the export yield the theme's own results
  skeleton (container classes, `data-*` grid config, body classes and
  the search-only scripts such as `masonry.js`/`imagesloaded.js`), its
  result-card markup -- turned into a template with `%%X%%` slots -- the
  per-page meta WordPress renders (author, date, excerpt) and the
  theme's own "nothing found" block. Measured on a dt-the7 site: body
  classes, container classes and `data-*`, stylesheets, scripts and the
  page-title band are byte-identical to `/?s=`.
  - The probe terms come from the site's own index: one rare word
    (unique across pages) that also proves the endpoint really is a
    WordPress search -- a site answering `/?s=x` with the homepage is
    detected and the harvest abandoned -- then a greedy set cover of
    broad terms, following the theme's own paginator links.
  - The static heading keeps the theme's prefix verbatim
    ("Suchergebnisse für: ") with the query filled in at runtime; same
    for the breadcrumb leaf and the `<title>` pattern.
  - Whenever the index fits (256 KB), it is inlined and the cards are
    rendered while the document is still parsing, so the theme's own
    grid/masonry code initializes over real cards instead of an empty
    container.
  - Harvested links that were never exported (author archives) keep
    their markup but lose the dead `href`, and assets only the search
    template references are downloaded, so verification stays green.
  - `--no-search-harvest` keeps the probes off and falls back to the
    2.6.0 behavior (exported 404 page + built-in markup); the same
    ladder catches a dead, disabled or unthemed search endpoint.
- Faithfulness over embellishment: the results list no longer prints a
  "N Treffer" status line and no longer `<mark>`s the query inside
  excerpts -- WordPress does neither. Matching is unchanged.
- Search index schema `v2`: documents may carry a fifth element with
  the harvested display slots.

## 2.6.0 (2026-08-29)

The site search works statically.

### Added
- **Search plugin** (`plugins/search.py`, on by default): WordPress
  answers `/?s=term` dynamically, and every static host this tool
  generates configs for silently returns the *homepage* for that URL
  instead -- a wrong success nobody notices. The export now carries its
  own search:
  - `/search-index.json`, built from the pages the crawl already parsed
    (title, meta description, main-content text with site chrome
    pruned), filtered exactly like the generated sitemap (no noindex,
    no redirect stubs, no link-only attachment pages).
  - A themed results page at `/suche/` (German sites) or `/search/`,
    cloned from the export's own 404 page so it inherits the theme CSS,
    marked `noindex,follow` like WordPress marks search results, and
    given its own search form when the template has none.
  - Every search form's `action` and the Yoast JSON-LD `SearchAction`
    point at that page; a ~190-byte script at the top of every
    `<head>` sends any surviving `?s=`/`?q=` URL there before first
    paint (bookmarks, external links, forms we did not recognize).
  - Matching is case- and diacritics-insensitive (`strasse` finds
    `Straße`, `ss` and `ß` are interchangeable), terms are ANDed like
    WordPress does, results carry a snippet with the term in `<mark>`,
    and the UI speaks German or English following the site language.
    No JS library, no extra asset, everything escaped.
  - `--no-search`, `--search-path PATH`, `--search-max-chars N`;
    results under `seo.search` in the report.
  - If the origin site already serves the search path or
    `/search-index.json`, origin content wins: the feature stands down
    entirely (nothing wired, nothing written) with a warning naming
    `--search-path`.
- The export must be served over HTTP for the search to work -- the
  index cannot be fetched from a `file://` page.

### Changed
- The closing "not functional statically" note no longer lists site
  search.

## 2.5.0 (2026-08-29)

### Added
- **Wordfence plugin**: the Wordfence "Live Traffic" human-detection
  beacon -- an inline script on every page that fires
  `/?wordfence_lh=1&hid=…&r=…` against the (static) host on the first
  user interaction -- is now stripped with the rest of the WP cruft
  (kept with `--no-strip-wp-cruft`). Counted as `wordfence-beacon` in
  the cruft summary/report.

## 2.4.0 (2026-08-29)

Structural: configuration moves out of the core file, and plugin-owned
options move into their plugins. No behavior change.

### Changed
- **`Config` and `PageRecord` live in their own file `config.py`** next
  to the script, loaded by path (like the plugins) and importable as
  `wps_config`. `TOOL_NAME`/`VERSION` moved along with them.
- **Plugin-owned Config options are now declared in the plugin** via
  the new `config_fields` class registry (name → immutable default) and
  merged into the final `Config` class at load time — `minify`,
  `optimize_images`, `compress_images`/`image_quality`,
  `mobile_check`/`mobile_user_agent` and `sr7_hydrate` left the core
  dataclass. Direct `Config(...)` construction keeps accepting them;
  field-name collisions and mutable defaults abort loudly at load time
  like every other plugin-system error. (`strip_wp_cruft` stays in the
  core: the core's clean gate reads it.)

## 2.3.0 (2026-08-29)

Image recompression: exported images get the same "smaller without
looking different" treatment the text assets already had.

### Added
- **Image recompression plugin** (`plugins/image_compress.py`, on by
  default): every exported PNG/JPEG/GIF/WebP is re-encoded -- PNG and
  static GIF losslessly (incl. an exact-lossless palette reduction for
  PNGs with <=256 colors), JPEG at `--image-quality` (default 85,
  progressive, EXIF orientation baked into the pixels, ICC profile
  kept, all other metadata dropped), lossless WebP repacked at maximum
  effort. A re-encode replaces the original only when significantly
  smaller (>=10% lossy / >=2% lossless), it must decode back to the
  expected dimensions, and the lossless paths must render
  pixel-identically to the original; animated images are never
  touched. Results in the console summary and under
  `seo.image_compression` in the report. Disable with
  `--no-compress-images`.
- **New plugin hook `run_end()`**: post-crawl finalization over the
  written output tree, after HTML post-processing and before
  verification/report; crash-isolated per plugin.
- SVG files are text: XML comments in exported `.svg` assets are now
  stripped by the minify plugin (outside CDATA sections) -- the last
  comment-carrying file type.

### Changed
- **New hard dependency: Pillow** (`Pillow>=10.4`). Like the
  minifiers, a stale venv fails loudly at the CLI
  (`pip install -r requirements.txt`, or `--no-compress-images`).

## 2.2.1 (2026-08-29)

Comment-stripping completeness: every remaining path where a comment
could survive minification is closed.

### Fixed
- Inline `<script type="module">` blocks (WordPress 6.x ships the
  wp-emoji loader this way) were skipped by inline minification, so
  their `/*! ... */` banners and comments survived on every page.
  Modules and `ecmascript` type variants are now minified; `importmap`,
  `speculationrules`, JSON-LD and template blocks stay untouched.
- `<style>`/`<script>` tags holding zero or several DOM nodes (empty
  tags, or tags another plugin appended text to) were silently skipped
  by inline minification; the text children are now joined, minified
  and written back as one node.
- Legacy SGML comment-hiding wrappers (`<style><!-- ... --></style>`,
  `<script><!-- ... //--></script>`) survived both minifiers, which
  preserve the `<!--`/`-->` tokens. They are stripped in
  `minify_css`/`minify_js` — anchored plus own-line matching only, so
  string literals like `url("a-->b")` or `"-->"` remain untouched;
  external `.css`/`.js` files (concatenated bundles with mid-file
  wrappers) benefit too.
- CSS comments in `style="..."` attributes were never touched; the
  attribute is now minified when it contains one.
- HTML comments inside `<pre>` survived (the whitespace pass must
  protect those blocks); real Comment nodes are now removed tree-wide
  before serialization — they never render, `<textarea>` content is
  visible text and stays.
- IE JScript conditional compilation (`/*@cc_on ... @*/`) removal is
  pinned by a test (rjsmin already strips it).

## 2.2.0 (2026-08-28)

Bugfix release: a systematic audit of the whole codebase (three parallel
review passes over the plugin system, the crawl/URL core and the
post-crawl phases) surfaced ~30 confirmed defects, almost all of them
predating the 2.x plugin refactor. All are fixed.

### Fixed -- deploy configs
- **Infinite 301 loops**: origin redirects that STRIP the trailing slash
  (`/a/` -> `/a`, no-trailing-slash permalink setups) produced rewrite
  rules that fought the generated configs' own directory-slash
  canonicalization; such rules are now skipped in both slash directions.
- Redirect-rule sources are emitted percent-DECODED -- nginx/Apache match
  the decoded URI, so rules for umlaut/space paths never fired before.
- `sub_filter` host substitution now covers hosts auto-detected via
  split DNS (and the portless connect-address spelling); host spellings
  with characters that would break or inject into the nginx config are
  skipped with a warning.
- `--extra-sitemap` values spelled differently from the crawled variant
  (www. vs bare) are no longer fetched and walked twice; origin-sitemap
  301 rules are deduplicated.
- When sitemap generation yields no entries, origin `Sitemap:` lines are
  now removed from robots.txt (they pointed at files not in the export).

### Fixed -- downloads plugin
- Extensionless CSS/JS routes (`/assets/bundle` served as
  `application/javascript`) are no longer claimed as "downloads": they
  flow through localization/minification/URL discovery again. Previously
  they shipped raw under a wrong name -- unusable under the generated
  configs' `nosniff` header.
- A download endpoint is only claimed (and mapped/301'ed) when its file
  actually landed on disk; `assets_exported` no longer double-counts.

### Fixed -- crawl / CLI
- `--max-pages` now also caps sitemap-seeded URLs, and `truncated` (and
  the `--fail-on errors` exit code) only trigger on real truncation.
- The NFD 404-retry no longer accepts an unfollowed 3xx as page content,
  and refreshes `Last-Modified` (previously `<lastmod>` came from the
  404 response).
- robots.txt fetch no longer saves the body of an unfollowed redirect.
- Streamed asset responses are closed on all error paths.
- CLI validation: `--timeout <= 0`, `--max-pages < 1`, empty `--header`
  names and a base_url with a path component (subdirectory installs are
  unsupported) now produce clean argparse errors instead of tracebacks
  or silent misbehavior.
- `--clean` removes plugin output trees (mobile-variants/) even when
  public/ is already absent; `%00` in URLs and over-long file names are
  handled as clean skips; the sitemap depth/count caps warn when hit.

### Fixed -- rewriting / discovery
- srcset parsing follows the HTML spec's tokenization: `data:` URIs
  containing commas survive byte-identically (previously they were split
  and corrupted), space-less srcsets parse correctly, and untouched
  srcset attributes keep their exact bytes.
- Human-readable attributes (`alt`, `title`, `aria-label`,
  `placeholder`) are no longer "localized" -- URLs there are prose.
- Relative `url()` refs in CSS reached via an internal redirect resolve
  against the FINAL location, like a browser.
- `canon_path` canonicalizes per path segment: an encoded `%2F` no
  longer turns into a real path separator (which changed the fetched URL
  and the on-disk path); single quotes are re-encoded as `%27` so the
  generated `url('...')` stays parseable.
- Base64 URL attributes accept the URL-safe alphabet, keep their
  alphabet on re-encoding, and fall back to plain-URL localization when
  the value is not base64 at all.
- `<meta http-equiv="refresh">` targets are discovered as pages and
  localized (browsers navigate them; previously they bounced the static
  site back to the origin).
- `.json`/`.webmanifest` assets are localized and their URLs discovered
  (PWA manifests pointed at the origin before).
- Verification: private-network URLs inside `<style>` blocks are
  flagged; a bare directory without index.html no longer counts as an
  existing page (only as a JS base path under wp-content & co.).

### Fixed -- plugin system
- Slider Revolution: the REST cache is locked across the fetch, so
  concurrent crawl workers can no longer fire duplicate wp-json requests
  for the same slider; hydration warnings are emitted once per slider
  instead of once per page.
- Conflicting plugin CLI options exit with a readable message naming the
  plugin; a plugin file that fails to import no longer leaves a
  half-initialized module in `sys.modules`; `load_plugins()` dropped its
  never-effective directory parameter; `cfg.user_agent` is final before
  plugin `finish_args` runs.

### Known limitations (documented, unchanged)
- Unquoted CSS `url(...)` containing `)` (invalid CSS) truncates at the
  first `)`.
- Unicode-escaped URLs (`https://...`) in bundler output are
  neither localized nor flagged.
- URLs in prose ending with punctuation can leave a trailing `.` in
  rewritten inline-script text.
- Relative references on the captured 404 page resolve against
  /404.html instead of the probe URL.

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
