# agent-doc-collab

**Inline comments for human↔agent document review.** Serve any HTML doc in the
browser, let a human highlight text and leave inline comments, and have a coding
agent watch for those comments and reply — inline, comment by comment, or by
editing the doc itself. Think Google-Docs-style margin comments, but the
reviewer on the other side is your AI agent.

Stdlib-only Python. No dependencies, no build step, two small files.

---

## Why

The best way to give an agent feedback on something it produced is often *on the
thing itself* — "this sentence is wrong," "what's the benefit here?" — anchored
to the exact span, not described from afar in a chat box. This is that, for
documents: a tight loop of highlight → comment → the agent answers right there.

## How it works

```
        browser                    comment-server.py                 your agent
  ┌──────────────────┐            ┌──────────────────┐         ┌──────────────────┐
  │ reads the doc,   │  POST      │ serves the doc + │  GET    │ watch.py under   │
  │ highlights text, ├──comment──▶│ an annotation    │◀─state──┤ the agent's      │
  │ leaves a comment │            │ layer; stores    │         │ "monitor" tool   │
  │                  │◀─reply─────┤ comments as JSON │◀─reply──┤ (or any poller)  │
  └──────────────────┘            └──────────────────┘         └──────────────────┘
```

1. **Serve** an HTML doc. The server injects a commenting layer (highlight a span
   or pin an element, leave a comment) before `</body>` at request time — your
   doc file is never modified.
2. **The human comments** in the browser. Comments persist to a JSON sidecar.
3. **The agent is notified.** Run `watch.py` under your agent's background
   "monitor" tool (e.g. Claude Code's Monitor) — it prints one line per new
   human comment, which wakes the agent. (Or poll `GET /api/state` yourself, or
   point `--webhook` at a URL.)
4. **The agent replies** via the JSON API (`POST /api/comments/<id>/reply`). The
   browser polls and shows the reply inline under the comment. The agent can
   also just edit the underlying doc and the human reloads.

## Quickstart

```bash
# 1. serve a doc (defaults to example.html on :8802)
python3 comment-server.py --doc example.html --port 8802

# 2. in another terminal, watch for comments (point your agent's monitor at this)
RC_STATE_URL=http://localhost:8802/api/state python3 watch.py

# 3. open http://localhost:8802 , highlight some text, leave a comment.
#    watch.py prints it; the agent replies:
curl -s -X POST http://localhost:8802/api/comments/1/reply \
     -H 'Content-Type: application/json' \
     -d '{"text": "Good catch — fixed.", "author": "agent"}'
```

That's the whole loop. The reply appears inline in the browser within a few
seconds.

## Using it with a coding agent

> Coding agents working in this repo: see [CLAUDE.md](CLAUDE.md) for a full
> integration guide (the loop, API, code map, and conventions).

Give your agent two facts and it runs the loop itself:
- **Watch:** run `watch.py` under your monitor/background-task tool; each line is
  a new comment to address.
- **Reply:** `POST /api/comments/<id>/reply` with `{"text": "...", "author":
  "agent"}`. Use `author: "agent"` so your replies aren't re-surfaced as new
  feedback.

To review a doc you generated: render it to a standalone HTML file (wrap the
content in a `.layout` element, or pass `--content-selector`), serve it, share
the URL, and respond to comments as they land.

## HTTP API

| Method | Path | Body | Purpose |
|---|---|---|---|
| GET  | `/api/state` | — | `{version, comments[]}` snapshot (poll `version` for changes) |
| POST | `/api/comments` | `{anchor, text, author}` | add a comment |
| POST | `/api/comments/<id>/reply` | `{text, author}` | reply to a comment |
| POST | `/api/comments/<id>/resolve` | `{resolved, author}` | resolve/unresolve |
| POST | `/api/comments/<id>/delete` | `{author}` | delete |

A comment is `{id, anchor, text, author, created, resolved, replies[]}`.

## Options

```
--doc PATH               HTML doc to serve (default example.html)
--port N                 (default 8802)
--host HOST              bind host (default 127.0.0.1; use 0.0.0.0 to reach from another device)
--store PATH             comment JSON sidecar (default <doc>.comments.json)
--content-selector CSS   commentable content root (default ".layout"; falls back to <body>)
--webhook URL            optional: POST a JSON event on each new human comment/reply
```

## Anchoring

Comments anchor with a simplified W3C-style TextQuoteSelector (the quoted text +
a little surrounding context + char offsets within the content root). Element
pins store a CSS path. If the doc changes enough that an anchor can't be
re-found, the comment isn't lost — it shows in the sidebar as "orphaned."

## Requirements

Python 3.8+. Standard library only.

## License

MIT — see [LICENSE](LICENSE).
