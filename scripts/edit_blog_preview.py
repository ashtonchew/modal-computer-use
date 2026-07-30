#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["markdown-it-py==4.2.0"]
# ///

"""Edit one blog draft in the browser and write the Markdown source back to disk.

The page is the tracked preview: same renderer, same stylesheet, same layout. Every rendered
block is anchored to the exact slice of Markdown that produced it, and double-clicking a
block opens that slice in place. Saving reassembles the document from the untouched slices
plus the edited ones, then regenerates the tracked HTML through the renderer, so the
artifact stays byte-reproducible by scripts/render_blog_preview.py.

Rendered HTML is never converted back to Markdown, which is what keeps an untouched block
byte-identical across a save.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import secrets
import sys
import threading
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from collections.abc import Sequence

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from render_blog_preview import (  # noqa: E402
    ROOT,
    SOURCE,
    build_document,
    document_title,
    make_renderer,
    render,
    render_article,
    render_tokens,
)

MAX_BODY = 8 * 1024 * 1024

BLOCK_LABELS = {
    "paragraph_open": "Paragraph",
    "heading_open": "Heading",
    "fence": "Code block",
    "code_block": "Code block",
    "table_open": "Table",
    "bullet_list_open": "List",
    "ordered_list_open": "List",
    "blockquote_open": "Quote",
    "hr": "Divider",
    "html_block": "HTML block",
}

# House rule for this document: no em dashes, en dashes, or curly quotes, ever. A browser
# or a paste from a rich source can introduce them, so an edited block is normalized on
# save. Untouched blocks are never passed through this.
TYPOGRAPHY = (
    ("\u2014", "--", "em dash"),
    ("\u2013", "-", "en dash"),
    ("\u2018", "'", "curly quote"),
    ("\u2019", "'", "curly quote"),
    ("\u201c", '"', "curly quote"),
    ("\u201d", '"', "curly quote"),
    ("\u2026", "...", "ellipsis"),
    ("\u00a0", " ", "non-breaking space"),
    ("\u202f", " ", "narrow no-break space"),
)


class DraftError(RuntimeError):
    """The Markdown could not be split into round-trippable blocks."""


@dataclass(frozen=True)
class Block:
    """One top-level Markdown block and the bytes that separate it from the previous one."""

    index: int
    kind: str
    gap: str
    source: str
    terminator: str


@dataclass(frozen=True)
class Draft:
    """A decomposition of the Markdown that reassembles to the original byte for byte."""

    markdown: str
    digest: str
    blocks: tuple[Block, ...]
    suffix: str
    article: str


@dataclass(frozen=True)
class EditorConfig:
    source: Path
    serve_root: Path
    page_path: str
    token: str


def digest_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def line_starts(text: str) -> list[int]:
    starts = [0]
    for position, character in enumerate(text):
        if character == "\n":
            starts.append(position + 1)
    return starts


def compose(blocks: Sequence[Block], suffix: str, sources: Sequence[str]) -> str:
    parts: list[str] = []
    for block, source in zip(blocks, sources, strict=True):
        parts.append(block.gap)
        parts.append(source)
        parts.append(block.terminator)
    parts.append(suffix)
    return "".join(parts)


def decompose(markdown: str) -> Draft:
    """Split the Markdown into top-level blocks and render it with block anchors.

    Each block records the source slice that produced it and the text that precedes it, so
    concatenating the parts reproduces the input exactly. That invariant is asserted here
    rather than trusted, because every later guarantee rests on it.
    """
    renderer = make_renderer()
    tokens = renderer.parse(markdown)
    starts = line_starts(markdown)
    blocks: list[Block] = []
    cursor = 0
    for token in tokens:
        if token.level != 0 or token.nesting < 0 or token.map is None:
            continue
        begin = starts[token.map[0]]
        end = starts[token.map[1]] if token.map[1] < len(starts) else len(markdown)
        if begin < cursor or end < begin:
            raise DraftError(f"block {len(blocks)} overlaps the previous block")
        raw = markdown[begin:end]
        terminator = "\n" if raw.endswith("\n") else ""
        blocks.append(
            Block(
                index=len(blocks),
                kind=token.type,
                gap=markdown[cursor:begin],
                source=raw[: len(raw) - len(terminator)],
                terminator=terminator,
            )
        )
        token.attrSet("data-block", str(len(blocks) - 1))
        cursor = end

    suffix = markdown[cursor:]
    if compose(blocks, suffix, [block.source for block in blocks]) != markdown:
        raise DraftError("block decomposition does not reassemble to the source")
    return Draft(
        markdown=markdown,
        digest=digest_of(markdown),
        blocks=tuple(blocks),
        suffix=suffix,
        article=render_tokens(renderer, tokens),
    )


def normalize_typography(text: str) -> tuple[str, list[str]]:
    found: list[str] = []
    for character, replacement, label in TYPOGRAPHY:
        if character in text:
            text = text.replace(character, replacement)
            if label not in found:
                found.append(label)
    return text, found


def merge(draft: Draft, submitted: Sequence[str]) -> tuple[str, list[int], list[str]]:
    """Rebuild the document from the draft, substituting only the blocks that changed.

    A block whose submitted text matches the draft is written back from the draft's own
    bytes, not from what the browser echoed, so an untouched block cannot drift.
    """
    sources: list[str] = []
    changed: list[int] = []
    notes: list[str] = []
    for block, raw in zip(draft.blocks, submitted, strict=True):
        text = raw.replace("\r\n", "\n").replace("\r", "\n")
        if text != block.source:
            text, found = normalize_typography(text)
            text = text.rstrip("\n")
            for label in found:
                if label not in notes:
                    notes.append(label)
        if text == block.source:
            sources.append(block.source)
            continue
        sources.append(text)
        changed.append(block.index)
    return compose(draft.blocks, draft.suffix, sources), changed, notes


def write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".editor-tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


EDITOR_CSS = """
    :root { --be-tint: color-mix(in oklch, var(--link) 14%, transparent); }

    main > [data-block]:hover {
      background: var(--be-tint);
      box-shadow: 0 0 0 0.4rem var(--be-tint);
      cursor: text;
    }

    main > [data-block][hidden] { display: none; }

    .be-empty {
      color: var(--muted);
      font-style: italic;
    }

    .be-editor { margin: 1.1rem 0; }

    .be-editor textarea {
      display: block;
      width: 100%;
      padding: 0.85rem 1rem;
      border: 1px solid var(--rule);
      background: var(--surface);
      color: var(--ink);
      font: 0.78rem/1.65 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      resize: vertical;
      tab-size: 2;
    }

    .be-editor textarea:focus {
      outline: 2px solid var(--link);
      outline-offset: 1px;
    }

    .be-hint {
      margin-top: 0.35rem;
      color: var(--muted);
      font: 0.62rem/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }

    /* The bar is glass over the article: a thin veil of --surface plus a blur, so the
       page reads through it. Both the veil and the edge are variables because every
       state below moves them together, and the text on top never dims with them. */
    .be-bar {
      --be-veil: color-mix(in oklch, var(--surface) 55%, transparent);
      --be-edge: color-mix(in oklch, var(--rule) 34%, transparent);
      position: fixed;
      right: 1rem;
      bottom: 1rem;
      z-index: 20;
      display: flex;
      gap: 0.9rem;
      align-items: center;
      padding: 0.5rem 0.5rem 0.5rem 1rem;
      border: 1px solid var(--be-edge);
      border-radius: 999px;
      background: var(--surface);
      color: var(--muted);
      font: 0.62rem/1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      box-shadow: 0 0.3rem 1rem oklch(0% 0 0 / 0.28);
      transition:
        background-color 150ms ease,
        border-color 150ms ease,
        box-shadow 150ms ease;
    }

    /* Without a backdrop filter the veil would sit on unblurred content, so the opaque
       surface above stands as the fallback and only supporting browsers go translucent. */
    @supports (backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px)) {
      .be-bar {
        background: var(--be-veil);
        -webkit-backdrop-filter: blur(14px) saturate(130%);
        backdrop-filter: blur(14px) saturate(130%);
      }
    }

    /* Quiet while it has nothing to report; solid once it does, or once you reach for it. */
    .be-bar:hover,
    .be-bar:focus-within,
    .be-bar:not([data-tone=""]) {
      --be-veil: color-mix(in oklch, var(--surface) 88%, transparent);
      --be-edge: color-mix(in oklch, var(--rule) 72%, transparent);
      box-shadow: 0 0.4rem 1.4rem oklch(0% 0 0 / 0.42);
    }

    .be-bar .be-status { color: var(--ink); }
    .be-bar[data-tone="dirty"] .be-status { color: var(--link); }
    .be-bar[data-tone="error"] { --be-edge: var(--link); }
    .be-bar[data-tone="error"] .be-status { color: var(--link); }

    .be-bar button {
      padding: 0.45rem 0.95rem;
      border: 1px solid color-mix(in oklch, var(--rule) 70%, transparent);
      border-radius: 999px;
      background: color-mix(in oklch, var(--bg) 80%, transparent);
      color: var(--ink);
      font: inherit;
      cursor: pointer;
      transition:
        background-color 150ms ease,
        border-color 150ms ease,
        color 150ms ease;
    }

    /* Enabled only ever means there is something to save, so the rim says so. */
    .be-bar button:enabled { border-color: color-mix(in oklch, var(--link) 60%, transparent); }

    .be-bar button:hover:enabled,
    .be-bar button:focus-visible {
      border-color: var(--link);
      background: var(--bg);
      color: var(--link);
    }

    .be-bar button:disabled { opacity: 0.5; cursor: default; }
"""

EDITOR_JS = r"""
(function () {
  "use strict";

  var state = JSON.parse(document.getElementById("be-state").textContent);
  var main = document.querySelector("main");
  var bar = document.querySelector(".be-bar");
  var statusText = bar.querySelector(".be-status");
  var saveButton = bar.querySelector(".be-save");
  var sources = state.blocks.map(function (block) { return block.source; });
  var baseline = sources.slice();
  var nodes = new Map();
  var session = null;

  function post(path, payload) {
    return fetch(path, {
      method: "POST",
      headers: { "content-type": "application/json", "x-editor-token": state.token },
      body: JSON.stringify(payload)
    });
  }

  function anchorBlocks() {
    document.querySelectorAll("[data-block]").forEach(function (el) {
      var root = el.closest("main > *");
      if (root && root !== el) {
        root.setAttribute("data-block", el.getAttribute("data-block"));
        el.removeAttribute("data-block");
      }
    });
    main.querySelectorAll(":scope > [data-block]").forEach(function (el) {
      var index = Number(el.getAttribute("data-block"));
      var list = nodes.get(index) || [];
      list.push(el);
      nodes.set(index, list);
    });
  }

  function editedCount() {
    var count = 0;
    for (var i = 0; i < sources.length; i += 1) {
      if (sources[i] !== baseline[i]) { count += 1; }
    }
    return count;
  }

  function setStatus(text, tone) {
    statusText.textContent = text;
    bar.setAttribute("data-tone", tone || "");
  }

  function refreshBar() {
    var count = editedCount();
    saveButton.disabled = count === 0;
    if (count === 0) {
      setStatus("No unsaved edits", "");
      return;
    }
    setStatus(count + (count === 1 ? " block edited" : " blocks edited"), "dirty");
  }

  function autosize(area) {
    area.style.height = "auto";
    area.style.height = (area.scrollHeight + 2) + "px";
  }

  function placeholder(index) {
    var el = document.createElement("p");
    el.className = "be-empty";
    el.setAttribute("data-block", String(index));
    el.textContent = "(empty block, double-click to restore)";
    return el;
  }

  function repaint(index) {
    return post("/api/render", { source: sources[index] }).then(function (response) {
      if (!response.ok) { throw new Error("HTTP " + response.status); }
      return response.json();
    }).then(function (payload) {
      var template = document.createElement("template");
      template.innerHTML = payload.html;
      var fresh = Array.prototype.slice.call(template.content.children);
      fresh.forEach(function (el) { el.setAttribute("data-block", String(index)); });
      if (fresh.length === 0) { fresh = [placeholder(index)]; }
      var previous = nodes.get(index) || [];
      var anchor = previous[0];
      if (!anchor || !anchor.parentNode) { return; }
      fresh.forEach(function (el) { anchor.parentNode.insertBefore(el, anchor); });
      previous.forEach(function (el) { el.remove(); });
      nodes.set(index, fresh);
    });
  }

  function closeEditor() {
    if (!session) { return; }
    var current = session;
    session = null;
    current.holder.remove();
    current.list.forEach(function (el) { el.hidden = false; });
  }

  function commitEditor() {
    if (!session) { return; }
    var index = session.index;
    var value = session.area.value;
    closeEditor();
    if (value === sources[index]) { refreshBar(); return; }
    sources[index] = value;
    refreshBar();
    repaint(index).catch(function (error) {
      setStatus("Preview failed: " + error.message, "error");
    });
  }

  function openEditor(index) {
    if (session) {
      if (session.index === index) { return; }
      commitEditor();
    }
    var list = nodes.get(index);
    if (!list || list.length === 0) { return; }
    var holder = document.createElement("div");
    holder.className = "be-editor";
    var area = document.createElement("textarea");
    area.value = sources[index];
    area.setAttribute("autocorrect", "off");
    area.setAttribute("autocapitalize", "off");
    area.setAttribute("autocomplete", "off");
    area.setAttribute("aria-label", "Markdown source");
    var hint = document.createElement("div");
    hint.className = "be-hint";
    hint.textContent = state.blocks[index].label
      + " \u00b7 Markdown source \u00b7 Esc cancels \u00b7 Cmd/Ctrl+Enter applies";
    holder.appendChild(area);
    holder.appendChild(hint);
    list[0].parentNode.insertBefore(holder, list[0]);
    list.forEach(function (el) { el.hidden = true; });
    session = { index: index, holder: holder, area: area, list: list };

    area.addEventListener("input", function () { autosize(area); });
    area.addEventListener("blur", function () {
      if (session && session.area === area) { commitEditor(); }
    });
    area.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeEditor();
        refreshBar();
      } else if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        commitEditor();
      }
    });

    autosize(area);
    area.focus();
    area.setSelectionRange(area.value.length, area.value.length);
  }

  function guard(event) {
    if (editedCount() > 0) {
      event.preventDefault();
      event.returnValue = "";
    }
  }

  function save() {
    if (session) { commitEditor(); }
    if (editedCount() === 0) { setStatus("No changes to save", ""); return; }
    saveButton.disabled = true;
    setStatus("Saving...", "");
    post("/api/save", { hash: state.hash, sources: sources }).then(function (response) {
      return response.json().then(function (payload) {
        return { ok: response.ok, status: response.status, payload: payload };
      });
    }).then(function (result) {
      if (!result.ok) {
        setStatus(result.payload.error || ("HTTP " + result.status), "error");
        saveButton.disabled = false;
        return;
      }
      if (result.payload.status === "unchanged") {
        baseline = sources.slice();
        refreshBar();
        setStatus("No changes to save", "");
        return;
      }
      window.removeEventListener("beforeunload", guard);
      window.location.reload();
    }).catch(function (error) {
      setStatus("Save failed: " + error.message, "error");
      saveButton.disabled = false;
    });
  }

  main.addEventListener("dblclick", function (event) {
    if (event.target.closest(".be-editor")) { return; }
    var el = event.target.closest("[data-block]");
    if (!el) { return; }
    var index = Number(el.getAttribute("data-block"));
    if (Number.isNaN(index)) { return; }
    event.preventDefault();
    openEditor(index);
  });

  document.addEventListener("keydown", function (event) {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      save();
    }
  });

  saveButton.addEventListener("click", save);
  window.addEventListener("beforeunload", guard);
  anchorBlocks();
  refreshBar();
})();
"""

EDITOR_BAR = """
  <div class="be-bar" data-tone="">
    <span class="be-status" role="status">No unsaved edits</span>
    <button type="button" class="be-save" disabled>Save</button>
  </div>"""


def editor_page(config: EditorConfig, draft: Draft) -> str:
    state = {
        "hash": draft.digest,
        "token": config.token,
        "blocks": [
            {
                "index": block.index,
                "label": BLOCK_LABELS.get(block.kind, block.kind),
                "source": block.source,
            }
            for block in draft.blocks
        ],
    }
    payload = json.dumps(state, ensure_ascii=False).replace("<", "\\u003c")
    body_extra = (
        f"{EDITOR_BAR}\n"
        f'  <script type="application/json" id="be-state">{payload}</script>\n'
        f"  <script>{EDITOR_JS}</script>"
    )
    title = document_title(draft.markdown, config.source.stem.replace("-", " ").title())
    return build_document(
        draft.article,
        title,
        head_extra=f"\n  <style>{EDITOR_CSS}  </style>",
        body_extra=body_extra,
    )


class EditorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        config: EditorConfig,
    ) -> None:
        super().__init__(address, handler)
        self.config = config
        self.write_lock = threading.Lock()


class EditorHandler(BaseHTTPRequestHandler):
    server_version = "blog-editor/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def config(self) -> EditorConfig:
        return self.server.config

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send_text(self, status: HTTPStatus, message: str) -> None:
        self._send(status, message.encode("utf-8"), "text/plain; charset=utf-8")

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing or oversized body"})
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Body is not valid JSON"})
            return None
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Body must be a JSON object"})
            return None
        return payload

    def do_GET(self) -> None:
        path = unquote(urlsplit(self.path).path)
        if path in {"/", ""}:
            self.send_response(int(HTTPStatus.FOUND))
            self.send_header("Location", self.config.page_path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == self.config.page_path:
            self._serve_page()
            return
        self._serve_static(path)

    def do_POST(self) -> None:
        path = unquote(urlsplit(self.path).path)
        if path not in {"/api/render", "/api/save"}:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint"})
            return
        if self.headers.get("X-Editor-Token") != self.config.token:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Missing editor token"})
            return
        payload = self._read_json()
        if payload is None:
            return
        if path == "/api/render":
            self._handle_render(payload)
        else:
            self._handle_save(payload)

    def _serve_page(self) -> None:
        try:
            markdown = self.config.source.read_text(encoding="utf-8")
            draft = decompose(markdown)
        except (OSError, DraftError) as error:
            self._send_text(HTTPStatus.INTERNAL_SERVER_ERROR, f"Cannot open the draft: {error}")
            return
        body = editor_page(self.config, draft).encode("utf-8")
        self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")

    def _serve_static(self, path: str) -> None:
        root = self.config.serve_root
        candidate = (root / path.lstrip("/")).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            self._send_text(HTTPStatus.NOT_FOUND, "Not found")
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "image/svg+xml":
            content_type = f"{content_type}; charset=utf-8"
        self._send(HTTPStatus.OK, candidate.read_bytes(), content_type)

    def _handle_render(self, payload: dict[str, Any]) -> None:
        source = payload.get("source")
        if not isinstance(source, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Expected a source string"})
            return
        self._send_json(HTTPStatus.OK, {"html": render_article(source)})

    def _handle_save(self, payload: dict[str, Any]) -> None:
        submitted = payload.get("sources")
        expected = payload.get("hash")
        if not isinstance(submitted, list) or not all(isinstance(x, str) for x in submitted):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Expected a list of sources"})
            return
        if not isinstance(expected, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Expected a baseline hash"})
            return

        source_path = self.config.source
        with self.server.write_lock:
            try:
                markdown = source_path.read_text(encoding="utf-8")
                draft = decompose(markdown)
            except (OSError, DraftError) as error:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
                return
            if draft.digest != expected:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {"error": "The file changed on disk. Reload to pick up the newer version."},
                )
                return
            if len(submitted) != len(draft.blocks):
                self._send_json(HTTPStatus.CONFLICT, {"error": "Block count changed. Reload."})
                return

            updated, changed, notes = merge(draft, submitted)
            if updated == markdown:
                self._send_json(
                    HTTPStatus.OK,
                    {"status": "unchanged", "hash": draft.digest, "changed": [], "normalized": []},
                )
                return
            try:
                # Prove the result still parses, renders, and reassembles before writing it.
                decompose(updated)
            except DraftError as error:
                self._send_json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    {"error": f"Refused to write a draft the editor cannot reopen: {error}"},
                )
                return
            try:
                write_atomic(source_path, updated)
                output = render(source_path)
            except OSError as error:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
                return

        note = f" (normalized: {', '.join(notes)})" if notes else ""
        print(f"saved {source_path} blocks={changed}{note} -> {output}", flush=True)
        self._send_json(
            HTTPStatus.OK,
            {
                "status": "saved",
                "hash": digest_of(updated),
                "changed": changed,
                "normalized": notes,
                "markdown": str(source_path),
                "html": str(output),
            },
        )


def serve_root_for(source: Path) -> Path:
    """Serve the tree that the article's relative links resolve inside.

    The draft links out with `../assets/...` and `../../benchmark-data/...`, so the page is
    published at the URL that mirrors its position in that tree and every relative link
    resolves exactly as it does from the tracked `file://` preview.
    """
    if source.is_relative_to(ROOT):
        return ROOT
    return source.parent.parent


def build_config(source: Path) -> EditorConfig:
    root = serve_root_for(source).resolve()
    relative = source.resolve().parent.relative_to(root).as_posix()
    page_path = "/" if relative == "." else f"/{relative}/"
    return EditorConfig(
        source=source.resolve(),
        serve_root=root,
        page_path=page_path,
        token=secrets.token_urlsafe(24),
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source",
        type=Path,
        default=SOURCE,
        help="Markdown draft to edit (default: the tracked low-latency article)",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="Interface to bind (default: %(default)s)"
    )
    parser.add_argument("--port", type=int, default=8787, help="Port to bind, 0 picks a free one")
    parser.add_argument("--open", action="store_true", help="Open the page in a browser")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    source = args.source.resolve()
    if not source.is_file():
        print(f"No such draft: {source}", file=sys.stderr)
        return 1

    config = build_config(source)
    try:
        decompose(source.read_text(encoding="utf-8"))
    except DraftError as error:
        print(f"Refusing to serve {source}: {error}", file=sys.stderr)
        return 1

    server = EditorServer((args.host, args.port), EditorHandler, config)
    host, port = server.server_address[0], server.server_address[1]
    url = f"http://{host}:{port}{config.page_path}"
    print(f"editing  {source}", flush=True)
    print(f"artifact {source.with_suffix('.html')}", flush=True)
    print(f"open     {url}", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
