# agent-doc-collab — guide for coding agents

**Inline comments for human↔agent document review.** A stdlib-only Python
comment server: serve any HTML doc, a human highlights text and leaves
anchored inline comments, and a coding agent (you) is woken to reply — inline,
comment by comment, or by editing the doc itself. Google-Docs-style margin
comments, but the reviewer on the other side is an AI agent.

No dependencies, no build step. Two files do the work:
`comment-server.py` (HTTP server + injected annotation UI) and `watch.py`
(the agent's change-notifier).

GitHub: https://github.com/philmercadante/agent-doc-collab

## The loop you run

1. **Serve** an HTML doc. The server injects the commenting layer before
   `</body>` *at request time* — the doc file on disk is never modified.
   ```bash
   python3 comment-server.py --doc example.html --port 8802
   ```
2. **The human comments** in the browser. Comments persist to a JSON sidecar
   (`<doc>.comments.json`).
3. **You get woken.** Run `watch.py` under your background/monitor tool (Claude
   Code's `Monitor`). It baselines existing items on startup (no backlog spam),
   then prints one line per *new human* comment or reply. Or poll
   `GET /api/state` and diff the `version` field yourself.
   ```bash
   RC_STATE_URL=http://localhost:8802/api/state python3 watch.py
   ```
4. **You reply** via the JSON API. The browser polls and shows your reply
   inline under the comment. You may instead edit the underlying doc and the
   human reloads.
   ```bash
   curl -s -X POST http://localhost:8802/api/comments/<id>/reply \
        -H 'Content-Type: application/json' \
        -d '{"text":"Good catch — fixed.","author":"agent"}'
   ```

**Always reply with `author:"agent"`** (or your own agent name). `watch.py`
filters out non-human authors, so anything you post is not re-surfaced to you
as new feedback.

## To review a doc you generated

Render it to a standalone HTML file, wrap the content in a `.layout` element
(or pass `--content-selector <css>`), serve it, share the URL, and answer
comments as they land. The `.layout` wrapper is what the annotation layer
treats as the commentable content root.

## HTTP API

| Method | Path | Body | Purpose |
|---|---|---|---|
| GET  | `/api/state` | — | `{version, comments[]}` snapshot — poll `version` for changes |
| POST | `/api/comments` | `{anchor, text, author}` | add a comment |
| POST | `/api/comments/<id>/reply` | `{text, author}` | reply to a comment |
| POST | `/api/comments/<id>/resolve` | `{resolved, author}` | resolve / unresolve |
| POST | `/api/comments/<id>/delete` | `{author}` | delete |

A comment is `{id, anchor, text, author, created, resolved, replies[]}`. A
reply is `{text, author, created}`.

## Code map

- **`comment-server.py`**
  - `Store` — thread-safe JSON-backed comment store; one lock guards the whole
    file, `version` bumps on every write, atomic temp-file flush. A corrupt
    sidecar is backed up (`.json.corrupt`) and reset rather than crashing.
    Methods: `add_comment`, `add_reply`, `set_resolved`, `delete`, `snapshot`.
  - `INJECT_CSS` / the injected JS — the in-browser annotation layer (selection
    → "Comment" button → anchored comment, plus the sidebar drawer). Kept as
    plain strings so there's no Python brace-escaping; runtime config is passed
    in as a JSON blob via `build_injection`.
  - `make_handler` — the `BaseHTTPRequestHandler`; `do_GET` serves the doc +
    `/api/state`, `do_POST` handles the comment endpoints.
  - `post_webhook` — optional fire-and-forget POST on each new human comment
    (`--webhook`); most integrations use `watch.py` instead.
- **`watch.py`** — polls `/api/state`, prints one line per new human item.
  Env: `RC_STATE_URL` (default `http://localhost:8802/api/state`), `RC_POLL_SEC`
  (default 3).

## Anchoring

Comments anchor with a simplified W3C-style TextQuoteSelector (quoted text + a
little surrounding context + char offsets within the content root). Element
pins store a CSS path. If the doc changes enough that an anchor can't be
re-found, the comment isn't lost — it shows in the sidebar as "orphaned."

## CLI options

```
--doc PATH               HTML doc to serve (default example.html)
--port N                 (default 8802)
--host HOST              bind host (default 127.0.0.1; 0.0.0.0 to reach from another device)
--store PATH             comment JSON sidecar (default <doc>.comments.json)
--content-selector CSS   commentable content root (default ".layout"; falls back to <body>)
--webhook URL            optional JSON event POST on each new human comment/reply
```

## Conventions when working on this repo

- **Stdlib only.** No third-party deps, no build step — that's a core
  constraint. Keep it that way.
- The injected CSS/JS lives as plain Python strings in `comment-server.py`;
  per-request config flows through the JSON blob in `build_injection`, not
  string interpolation into the markup.
- `author` is currently a free-form string defaulting to `"human"`; `watch.py`
  treats anything ≠ `"agent"` as human-authored. Multi-agent reviewers (e.g.
  distinct `claude` / `codex` authors) build on this field.
- Python 3.8+. MIT licensed.
