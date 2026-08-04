# Drafts

Long-form articles written from this repository's benchmark evidence. A draft stays here while it is
being written and until it is published elsewhere.

Each draft owns three things:

| Path | What it is |
| --- | --- |
| `<slug>.md` | the article, the only file you edit by hand |
| `<slug>.html` | the rendered preview, generated |
| `<slug>-images/` | the publishing bundle, generated |

## Write

Edit the Markdown directly, or run the browser editor for paragraph-level editing with a live
preview:

```
uv run --script scripts/edit_blog_preview.py --source docs/drafts/<slug>.md --port 8787
```

The editor writes the Markdown and nothing else. It runs no git commands, so saving in the browser
leaves the change in the working tree for you to commit. It refuses to save when the file changed on
disk underneath it, which is what keeps a browser session and a terminal edit from overwriting each
other. The editor binds to loopback by default. A non-loopback `--host` also requires
`--allow-remote`, because anyone who can load the page receives its write token and can browse the
repository working tree.

## Render the preview

```
uv run scripts/render_blog_preview.py
```

This regenerates `<slug>.html` from the Markdown. Never hand-edit the HTML. The browser editor does
not regenerate it, so run this after a browser session if you want the tracked preview current.

## Export the publishing bundle

```
uv run --script scripts/export_article_images.py
```

This walks the draft for image references in document order and writes, into `<slug>-images/`:

- one PNG per figure at 2x, named by position and source stem, so `docs/assets/modal-optimized-agent-loop.svg`
  referenced second becomes `2_agent-loop.png`
- `paste.md`, the draft with each image reference replaced by a placeholder naming the PNG that
  belongs at that spot
- `.article-image-export.json`, the ownership manifest used to identify stale generated PNGs

Numbering follows the article, not the assets folder, so moving a section renumbers the exports.
Only stale PNGs listed in the ownership manifest are deleted, which stops a reorder from leaving an
orphan without touching unrelated files. A custom `--output` must be empty or already contain a
matching manifest. Most publishing surfaces cannot resolve the relative asset paths, so they need
the prose and the uploads separately; `paste.md` is the prose, and the placeholder says which file
to drop where.

The draft cites artifacts by path relative to its own folder, which resolves where the draft lives
and nowhere else. `paste.md` sits one folder deeper, and the published copy sits on a site with no
repository under it, so the export rewrites every relative link to the dedicated article branch on
GitHub. The draft keeps its relative paths. Those URLs resolve once the repository is public, even
for supporting files that intentionally stay off `main`.

Rendering uses cairosvg. On macOS, Homebrew installs libcairo outside the dyld search path and
cairocffi resolves it by soname at import, so the script locates the library and re-execs itself with
`DYLD_LIBRARY_PATH` set. Install it with `brew install cairo` if the script reports it missing.

## Keep figures honest

A diagram that repeats a number from the article is a second copy of that number, and it drifts. When
a measurement changes, update the prose, the diagram that shows it, and the exported PNG in the same
change. The SVGs under `docs/assets/` are hand-authored and have no generator, so nothing catches a
stale figure for you.

Every figure in a draft should resolve to a tracked artifact under `benchmark-data/`. The
[benchmark data policy](../../benchmark-data/README.md) defines what is eligible to cite.
