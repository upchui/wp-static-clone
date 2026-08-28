# The plugin system — developer reference

Everything feature- or vendor-specific in wp-static-clone lives in this
directory. The core (`wp-static-export.py`) provides the generic export
pipeline — sitemap discovery, crawl, URL rewriting, verification, deploy
files, report — plus a fixed set of **hook points** and **registries**
that plugins attach to. The ten shipped plugins are ordinary users of
this API; your own plugin is a first-class citizen the moment its file
lands in this folder.

What plugins do today (each line names a shipped example to read):

* register extra HTML attributes for crawling/rewriting — [`lazyload.py`](lazyload.py), [`complianz.py`](complianz.py)
* transform every page's DOM (strip, decode, hydrate) — [`wordpress.py`](wordpress.py), [`cloudflare.py`](cloudflare.py), [`slider_revolution.py`](slider_revolution.py)
* filter CSS/JS text before it is written — [`minify.py`](minify.py)
* inspect page responses and run extra requests — [`mobile_check.py`](mobile_check.py)
* claim non-HTML responses and materialize files — [`downloads.py`](downloads.py)
* post-process the exported HTML tree after the crawl — [`image_optimize.py`](image_optimize.py)
* skip URLs, contribute 301 rules to the server configs, and add their
  own CLI flags, console summary lines and report sections — most of them

## Anatomy of a plugin

One file per plugin. The file must expose `PLUGIN = <subclass of Plugin>`
with a unique `name`:

```python
from wp_static_export import Plugin

class MyThing(Plugin):
    name = "my_thing"
    ...

PLUGIN = MyThing
```

A plugin has two kinds of surface:

* **Class-level registries** — tuples of data (attribute names, regex
  fragments, directory names) that the loader merges into the core's
  module constants once at startup. Purely declarative; a plugin that
  only registers attributes needs no methods at all (see
  [`lazyload.py`](lazyload.py), 18 lines).
* **Instance hooks** — methods the core calls at fixed points of the
  export. The base class defines every hook as a no-op with a docstring;
  override only what you need. Instances are created fresh per export
  run with the `Exporter` as `self.exp`.

The `Plugin` base class in `wp-static-export.py` is the source of truth
for signatures and semantics — every hook carries a docstring.

## Loading and lifecycle

1. **Import time.** The last top-level statements of
   `wp-static-export.py` register the module under the name
   `wp_static_export` in `sys.modules` (that is why
   `from wp_static_export import Plugin` works even though the file name
   is hyphenated) and call `load_plugins()`.
2. **Discovery.** `load_plugins()` scans `plugins/*.py` **sorted by file
   name** — alphabetical order is the load order and therefore the hook
   call order. Files starting with `_` are skipped (that is how you
   disable a plugin without deleting it). Each file is imported as module
   `wps_plugin_<stem>` (also registered in `sys.modules`, so tests can
   monkeypatch it).
3. **Fail loudly.** A missing `plugins/` directory, a file without a
   valid `PLUGIN`, a duplicate `name`, or any exception during import
   **aborts the run**. A half-loaded feature set must never silently
   produce a degraded export.
4. **Registry aggregation.** After all files are imported, each class's
   registries are appended to the core constants (`URL_ATTRS`,
   `SRCSET_ATTRS`, …) and `PAGE_SKIP_PATTERNS` is recompiled from the
   core fragments plus all plugin fragments. This happens **once**,
   after every plugin is loaded — see the load-order pitfall below.
5. **CLI phase.** `parse_args()` calls each plugin's
   `add_cli_args(group)` classmethod with its own argparse group (shown
   as `plugin: <name>` in `--help`) *before* parsing, and
   `finish_args(ap, args, cfg)` *after* the `Config` is built — validate
   with `ap.error(...)` and copy your options onto `cfg` there. Because
   loading happens at import time, plugin flags exist before the CLI
   ever parses — delete a plugin file and its flags disappear from
   `--help`.
6. **Per-run instances.** `Exporter.__init__` ends with
   `self.plugins = [cls(self) for cls in PLUGIN_REGISTRY]` — fresh
   instances per run, so plugin state (stats, caches) never leaks
   between exports. `exporter.plugin("minify")` returns the instance by
   name.

## Where hooks fire in the export pipeline

```
parse_args        add_cli_args (per-plugin argparse group)
                  finish_args  (validate, args -> cfg)
run() start       run_start    (env warnings, e.g. missing libs)
discovery         skip_page_path gates what counts as a page
crawl, per PAGE   page_fetched            after a successful HTML response,
                                          BEFORE the redirect-stub decision
                  pre_discover_soup       rewrite mode, before URL discovery
                                          (mutations are crawled+rewritten)
                  -- URL discovery reads URL_ATTRS / SRCSET_ATTRS /
                     PAGE_URL_ATTRS / B64_URL_ATTRS / CSS_URL_ATTRS;
                     skip_asset_candidate can veto single candidates;
                     inline JS text runs through expand_scan_text, then
                     scan_text_urls may derive extra URLs
                  strip_resource_hints (core), then clean_soup
                                          [cfg.rewrite + cfg.strip_wp_cruft]
                  rewrite_soup_relative (core), then rewrite_soup
                  serialize = pre_serialize -> str(soup) -> post_serialize
                  text_asset_written("html", ...)
                  page_saved              after the page landed on disk
crawl, per ASSET  save_non_html_response  claim e.g. download endpoints
                                          (first claimant wins)
                  filter_text_asset       LAST transform of CSS/JS text
                  text_asset_written("css"|"js", ...)
post-crawl        wants_postprocess       any True -> extra pass over every
                  postprocess_soup        exported HTML page (return True
                                          when you changed the soup)
verify            reads VERIFY_SCRIPT_REF_DIRS / VERIFY_SKIP_REF_PREFIXES
deploy files      redirect_rules          extra (from_path, to_ref) 301s
console summary   summary_lines
report            add_report              mutate report.json, append
                                          report.txt lines/sections
```

Ordering guarantees live in the **fixed hook points**, not in priorities:
no two plugins share a hook where their relative order matters. Within
one hook, call order is load order (alphabetical file names).

## Registry reference

| Class attribute | Aggregated into | Meaning | Shipped user |
|---|---|---|---|
| `url_attrs` | `URL_ATTRS` | attributes holding a single URL — discovered, downloaded, rewritten like `src`/`href` | lazyload, complianz, theme_fixes |
| `page_url_attrs` | `PAGE_URL_ATTRS` | attributes holding internal *page* links | theme_fixes (The7 `data-dt-location`) |
| `b64_url_attrs` | `B64_URL_ATTRS` | attributes holding base64-encoded URLs (decoded for discovery, re-encoded on rewrite) | slider_revolution (`data-dbsrc`) |
| `srcset_attrs` | `SRCSET_ATTRS` | srcset-style comma-separated candidate lists | lazyload |
| `css_url_attrs` | `CSS_URL_ATTRS` | attributes whose value is a full CSS declaration containing `url(...)` | lazyload (`data-bg`) |
| `lazy_img_attrs` | `LAZY_IMG_ATTRS` | attributes marking an `<img>` as managed by a lazy-load plugin (image_optimize then leaves it alone) | lazyload |
| `html_noise_patterns` | `HTML_NOISE_EXTRA` | `(bytes_regex, replacement)` pairs normalized away before the mobile HTML comparison | theme_fixes |
| `page_skip_pattern_fragments` | recompiles `PAGE_SKIP_PATTERNS` | regex fragments for URL paths that must never be fetched | wordpress (WooCommerce `/cart/`, `/checkout/`) |
| `verify_script_ref_dirs` | `VERIFY_SCRIPT_REF_DIRS` | extra top-level dirs whose `"/dir/..."` refs inside inline scripts the verification resolves | cloudflare (`cf-fonts`, `cdn-cgi`) |
| `verify_skip_ref_prefixes` | `VERIFY_SKIP_REF_PREFIXES` | local refs the verification never checks (resolved at runtime) | cloudflare (email-protection) |
| `extra_output_dirs` | `EXTRA_OUTPUT_DIRS` | extra output trees next to `public/` that `--clean` removes and `.dockerignore` excludes | mobile_check (`mobile-variants`) |

## Hook reference

`[T]` = called from crawl worker threads — see thread safety below.
Hooks returning a value are filters; the rest are notifications.

| Hook | When / contract | Shipped user |
|---|---|---|
| `add_cli_args(cls, group)` | classmethod, before argparse parsing; `group` is this plugin's own option group | minify, mobile_check |
| `finish_args(cls, ap, args, cfg)` | classmethod, after the `Config` is built; validate via `ap.error`, copy args onto `cfg` | minify (hard error on missing libs) |
| `run_start()` | top of `run()`: environment validation / warnings | minify |
| `skip_page_path(path) -> bool` [T] | URL normalization: `True` = this path is never a page | cloudflare (`/cdn-cgi/`), wordpress |
| `skip_asset_candidate(url, tag_name, rel) -> bool` [T] | HTML discovery: `True` drops one asset candidate | wordpress (wlwmanifest targets) |
| `pre_discover_soup(soup, page_url)` [T] | rewrite mode, BEFORE URL discovery and rewriting — mutations are seen by both | slider_revolution (slide hydration) |
| `expand_scan_text(work) -> str` [T] | pre-transform of JS/JSON text before the URL sweep | complianz (template placeholders) |
| `scan_text_urls(work) -> (pages, assets)` [T] | derive URLs from runtime URL-construction patterns | slider_revolution |
| `clean_soup(soup)` [T] | after core `strip_resource_hints()`, under the `cfg.rewrite` + `cfg.strip_wp_cruft` gate | wordpress, cloudflare |
| `rewrite_soup(soup)` [T] | after core `rewrite_soup_relative()` | cloudflare (cfemail → mailto:) |
| `pre_serialize(soup)` [T] | inside `Exporter.serialize()`, before `str(soup)` | minify (inline CSS/JS) |
| `post_serialize(data) -> bytes` [T] | last step on the serialized HTML bytes | minify (whitespace/comments) |
| `text_asset_written(kind, orig_len, new_len)` [T] | a transformed text asset was written (`"html"`/`"css"`/`"js"`) — stats notification | minify |
| `page_fetched(url, resp, rec)` [T] | successful same-site HTML response, BEFORE the redirect-stub/save decision | mobile_check (Vary header) |
| `page_saved(save_url, resp, rec)` [T] | after a real (non-stub) page was parsed and saved | mobile_check (UA comparison) |
| `save_non_html_response(url, resp) -> bool` [T] | claim a non-HTML response on a page/extensionless URL; `True` = claimed, first claimant wins | downloads |
| `filter_text_asset(url, kind, text) -> str` [T] | LAST transform of a rewritten CSS/JS asset before it is written | minify |
| `wants_postprocess() -> bool` | gates the post-crawl read-modify-write pass over every exported page | image_optimize, downloads |
| `postprocess_soup(soup) -> bool` | post-crawl transform per exported page; return `True` when changed | image_optimize, downloads |
| `redirect_rules(seen_from) -> list` | extra `(from_path, to_ref)` 301 rules for nginx/Netlify/Apache configs; skip paths already in `seen_from`, filter unsafe characters yourself | downloads |
| `summary_lines() -> list[str]` | console lines for the final summary | minify, mobile_check, downloads |
| `add_report(report, txt_head, txt_sections)` | mutate `report.json` (dict), append `report.txt` head lines / `report_section()` blocks; runs before the JSON dump | all feature plugins |

## Working with the Exporter (`self.exp`)

Plugins are **trusted local code**: they get the full `Exporter`. The
pieces plugins actually use:

* `exp.cfg` — the run's `Config`. Options a plugin owns are still
  *declared* on the dataclass in the core (with a `# plugin-owned`
  comment) so that programmatic `Config(...)` construction keeps
  working; only the plugin reads them (`minify`, `sr7_hydrate`,
  `optimize_images`, `mobile_check`, `mobile_user_agent`). A
  third-party plugin can instead set its own attribute in
  `finish_args` (`cfg.my_option = args.my_option`) and read it with
  `getattr(cfg, "my_option", default)`.
* `exp.fetch(url, headers=..., stream=...)` — the ONLY way to make
  requests: it enforces the global rate gate and never follows a
  redirect off the target site. Never talk to foreign hosts.
* URL helpers: `exp.is_internal`, `exp.to_base_host`,
  `exp.normalize_page_url`, `exp.localize_url`, `exp.localize_text`.
* Filesystem: `exp.local_path_for(url, is_page=...)`,
  `exp.write_bytes(target, data)` (collision-safe, first write wins),
  `exp.public_dir`, `exp.cfg.out_dir`.
* Shared state: `exp.warnings` (list, printed + reported),
  `exp.cruft_removed`, `exp.speculative_urls` (URLs whose 404s are not
  errors), `exp.asset_count`, `exp.stats_lock`.
* Module helpers importable from `wp_static_export`: `canon_path`,
  `path_extension`, `norm_host`, `report_section`, `FOREIGN_WP_RE`, …

## Pitfalls

**Load order vs. aggregated constants.** Registry aggregation completes
only after *all* plugin files are imported, but files import in
alphabetical order — so a plugin that *consumes* an aggregated constant
may load before the plugin that *contributes* to it. Therefore: read
aggregated constants **lazily via the module object**, never freeze them
with a from-import at module level:

```python
import wp_static_export as core

def postprocess_soup(self, soup):
    for attr in core.LAZY_IMG_ATTRS:      # read at call time -- correct
        ...
```

`image_optimize` (reads `LAZY_IMG_ATTRS`, contributed by the
later-loading `lazyload`) and `mobile_check` (reads `HTML_NOISE_EXTRA`,
contributed by `theme_fixes`) both do exactly this.

**Thread safety.** Hooks marked `[T]` run concurrently in crawl worker
threads (`-c`, default 5). Guard shared mutable state with
`self.exp.stats_lock`, exactly like the core does:

```python
with self.exp.stats_lock:
    self.stats["css"] += 1
```

Plain `list.append` on your own instance lists is fine (GIL-atomic);
read-modify-write of counters and dicts is not.

**`report["warnings"]` aliases `exp.warnings`.** The report dict holds
the *same list object*, and `add_report` runs before the JSON dump — so
a warning appended inside `add_report` still lands in `report.json`,
`report.txt` and the console count (`mobile_check` relies on this).

**Behavior gates you inherit.** `pre_discover_soup`, `clean_soup` and
`rewrite_soup` only fire in rewrite mode (`clean_soup` additionally only
with cruft stripping enabled) — the *call sites* gate this, don't
re-check `cfg.rewrite` yourself. But feature flags of your own
(`cfg.mobile_check` etc.) are yours to check inside the hook.

**Docker wrapper.** `run-docker.sh` mounts the whole repo read-only at
`/tool`, so plugins work inside the container without extra steps.

## Testing your plugin

The test suite loads the core by file path (see `tests/conftest.py`),
which pulls the plugins in automatically. Two access patterns:

```python
def test_module_function(mod):                   # module-level helpers
    minify = mod.PLUGIN_MODULES["minify"]
    assert minify.minify_css("a {  x: 1; }") == "a{x:1}"

def test_instance_hook(exporter):                # per-run hook behavior
    soup = BeautifulSoup(html, "html.parser")
    exporter.plugin("wordpress").clean_soup(soup)
    assert "generator" not in str(soup)
```

For hooks that fetch, monkeypatch `exporter.fetch` (see the SR7 and
mobile_check tests in `tests/test_exporter_unit.py` for the fake-fetch
pattern); for optional imports, monkeypatch the plugin *module*
(`monkeypatch.setattr(mod.PLUGIN_MODULES["minify"], "rjsmin", None)`).

When you add a shipped plugin, update the exact-list and registry
asserts in `tests/test_plugins.py`. The e2e test
(`tests/test_e2e.py` + `tests/fixture/`) exports a small fixture site
end-to-end — extend the fixture with markup that would break loudly if
your plugin stopped working.

## Worked example

A small but complete plugin: registers a crawlable attribute, owns a CLI
flag, strips a tracking pixel from every page (thread-safe counting) and
reports what it did. Drop it in as `plugins/tracker_strip.py`:

```python
"""Strip example.com tracking pixels and crawl their fallback images."""
import re

from wp_static_export import Plugin

PIXEL_RE = re.compile(r"//tracker\.example\.com/pixel")


class TrackerStrip(Plugin):
    name = "tracker_strip"
    # <img data-tracker-fallback="..."> is discovered, downloaded and
    # rewritten like any src/href
    url_attrs = ("data-tracker-fallback",)

    @classmethod
    def add_cli_args(cls, group):
        group.add_argument("--keep-tracker", dest="strip_tracker",
                           action="store_false",
                           help="keep the tracking pixel in the export")

    @classmethod
    def finish_args(cls, ap, args, cfg):
        cfg.strip_tracker = args.strip_tracker    # plugin-owned option

    def __init__(self, exporter):
        super().__init__(exporter)
        self.removed = 0

    def clean_soup(self, soup):                   # [threaded]
        # only fires in rewrite mode with cruft stripping on (call-site
        # gate); our own flag is ours to check
        if not getattr(self.exp.cfg, "strip_tracker", True):
            return
        removed = 0
        for img in soup.find_all("img", src=PIXEL_RE):
            img.decompose()
            removed += 1
        if removed:
            with self.exp.stats_lock:
                self.removed += removed

    def summary_lines(self):
        if not self.removed:
            return []
        return [f"[seo] tracker: {self.removed} pixel(s) removed"]

    def add_report(self, report, txt_head, txt_sections):
        report["tracker_pixels_removed"] = self.removed


PLUGIN = TrackerStrip
```

Run `python3 wp-static-export.py --help` afterwards: a
`plugin: tracker_strip` group with `--keep-tracker` appears — that is
the whole integration.
