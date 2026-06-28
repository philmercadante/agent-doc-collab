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
RC_AS=claude python3 watch.py        # RC_AS = your reviewer label

# 3. open http://localhost:8802 , highlight some text, leave a comment.
#    watch.py prints it; the agent replies under its own label:
curl -s -X POST http://localhost:8802/api/comments/1/reply \
     -H 'Content-Type: application/json' \
     -d '{"text": "Good catch — fixed.", "author": "claude"}'
```

That's the whole loop. The reply appears inline in the browser within a few
seconds.

## Using it with a coding agent

> Coding agents working in this repo: see [CLAUDE.md](CLAUDE.md) (Claude) /
> [AGENTS.md](AGENTS.md) (Codex) for a full integration guide — the loop, API,
> code map, conventions, and an orchestrator recipe for setting up a review with
> two AIs at once.

Give your agent two facts and it runs the loop itself:
- **Watch:** run `watch.py` (with `RC_AS=<your-label>`) under your
  monitor/background-task tool; each line is a new comment to address.
- **Reply:** `POST /api/comments/<id>/reply` with `{"text": "...", "author":
  "<your-label>"}`. Replying under your own label means your replies aren't
  re-surfaced to you as new feedback.

To review a doc you generated: render it to a standalone HTML file (wrap the
content in a `.layout` element, or pass `--content-selector`), serve it, share
the URL, and respond to comments as they land.

## Multiple agents, and blind review

Reviewer identity is just a **self-declared `author` label** — `claude`,
`codex`, `gemini`, `aider`, a second human, anything. The server keeps no
allow-list; the only reserved author is `human` (what the browser posts). So any
agent or harness can join a review by labelling its comments and replies.

When more than one agent reviews the same doc, you often want them to form
**independent** opinions first, instead of the second agent anchoring on the
first one's take. That's a **blind round**:

- Start blind: `--blind`, or `POST /api/blind {"blind": true}`, or the 🙈 button
  in the sidebar.
- While blind, an agent fetches `GET /api/state?as=<label>` and sees only **its
  own** comments **and the human's** — never another agent's notes (or their
  replies on a shared thread). The human's own browser always sees everything.
- **Reveal:** the human clicks 👁 Reveal (`POST /api/blind {"blind": false}`).
  The walls drop; every agent now sees all comments and — via `watch.py` — starts
  getting woken on each other's notes, so they can compare and rebut.

Identity is honor-system (no auth), which suits the local-trust setting. Point
each agent's watcher at itself with `RC_AS`:

```bash
RC_AS=claude python3 watch.py    # one terminal
RC_AS=codex  python3 watch.py    # another
```

Both Claude Code and Codex can run as live reviewers this way. One difference:
Claude Code has a `Monitor` tool that re-prompts it on each new watcher line,
whereas a Codex session has no autonomous wake — it must keep its turn alive and
keep reading the watcher (or poll `/api/state?as=<label>`). For an AI to set this
up end-to-end for someone — render the doc, start the server, become one
reviewer, and hand the human a paste-in block for the second — see the
**orchestrator recipe** in [CLAUDE.md](CLAUDE.md) / [AGENTS.md](AGENTS.md).

## HTTP API

| Method | Path | Body | Purpose |
|---|---|---|---|
| GET  | `/api/state` | — | `{version, blind, comments[]}` snapshot (poll `version`) |
| GET  | `/api/state?as=<label>` | — | same, but blind-filtered to that agent's view during a blind round |
| POST | `/api/comments` | `{anchor, text, author}` | add a comment |
| POST | `/api/comments/<id>/reply` | `{text, author}` | reply to a comment |
| POST | `/api/comments/<id>/resolve` | `{resolved, author}` | resolve/unresolve |
| POST | `/api/comments/<id>/delete` | `{author}` | delete |
| POST | `/api/blind` | `{blind: true\|false}` | start / end the blind round |

A comment is `{id, anchor, text, author, created, resolved, replies[]}`. Use your
own label as `author` (e.g. `"claude"`); `human` is reserved for the browser.

## Options

```
--doc PATH               HTML doc to serve (default example.html)
--port N                 (default 8802)
--host HOST              bind host (default 127.0.0.1; use 0.0.0.0 to reach from another device)
--store PATH             comment JSON sidecar (default <doc>.comments.json)
--content-selector CSS   commentable content root (default ".layout"; falls back to <body>)
--webhook URL            optional: POST a JSON event on each new human comment/reply
--blind                  start in a blind round (agents see only own + human until Reveal)
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
