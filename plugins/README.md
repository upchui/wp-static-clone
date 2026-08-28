# Plugins

Feature-specific behavior lives here; the core (`wp-static-export.py`)
provides the crawl/rewrite/verify/report machinery and loads every
`*.py` file in this directory at startup (sorted by file name, files
starting with `_` are skipped).

Rules:

* One plugin per file, exposing `PLUGIN = <subclass of Plugin>` with a
  unique `name`. The `Plugin` base class in `wp-static-export.py`
  documents every available hook.
* Loading fails **loudly**: a missing `plugins/` directory, a file
  without a valid `PLUGIN`, or an exception during import abort the run.
  Disable a plugin by deleting its file or renaming it to `_<name>.py`.
* Hooks marked `[threaded]` run in crawl worker threads -- guard shared
  mutable state with `self.exp.stats_lock`, exactly like the core does.
* Plugins are trusted local code: they receive the `Exporter` instance
  (`self.exp`) and may use its helpers (`fetch`, `warnings`,
  `speculative_urls`, ...) directly.

Minimal example:

```python
from wp_static_export import Plugin


class MyFix(Plugin):
    name = "my-fix"
    url_attrs = ("data-my-src",)        # extra URL attribute to crawl

    def rewrite_soup(self, soup):       # runs after core URL rewriting
        for tag in soup.find_all("div", attrs={"data-legacy": True}):
            del tag["data-legacy"]


PLUGIN = MyFix
```
