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
3. **You get woken.** Run `watch.py` (identifying yourself with `RC_AS`) under
   your background/monitor tool (Claude Code's `Monitor`). It baselines existing
   items on startup (no backlog spam), then prints one line per new item *not
   authored by you*. Or poll `GET /api/state?as=<you>` and diff `version`.
   ```bash
   RC_AS=claude python3 watch.py
   ```
4. **You reply** via the JSON API, under your own label. The browser polls and
   shows your reply inline. You may instead edit the underlying doc and the human
   reloads.
   ```bash
   curl -s -X POST http://localhost:8802/api/comments/<id>/reply \
        -H 'Content-Type: application/json' \
        -d '{"text":"Good catch — fixed.","author":"claude"}'
   ```

**Reply under your own `author` label** (e.g. `"claude"`). The only reserved
author is `"human"` (what the browser posts); the server treats every other
label as an agent, so anything you post is not re-surfaced to you as feedback.

## Multiple agents & blind review

Reviewer identity is a **self-declared, arbitrary `author` label** — `claude`,
`codex`, `gemini`, a second human, anything. No allow-list; `human` is the only
reserved author. When several agents review the same doc you usually want
**independent first opinions** (no anchoring on whoever commented first):

- **Blind round:** `--blind`, `POST /api/blind {"blind":true}`, or the 🙈 sidebar
  button. While blind, `GET /api/state?as=<label>` returns only that agent's own
  + the human's comments (and strips other agents' replies on shared threads).
  The human's browser (no `as`) always sees everything.
- **Reveal:** the human clicks 👁 Reveal / `POST /api/blind {"blind":false}`.
  Walls drop; everyone sees all, and each agent's `watch.py` starts surfacing the
  others' notes so they can compare/rebut.

Pass `RC_AS=<label>` to `watch.py` so it appends `?as=<label>` (blind filtering)
and never wakes you on your own notes. Identity is honor-system — no auth, which
suits the local-trust setting.

## To review a doc you generated

Render it to a standalone HTML file, wrap the content in a `.layout` element
(or pass `--content-selector <css>`), serve it, share the URL, and answer
comments as they land. The `.layout` wrapper is what the annotation layer
treats as the commentable content root.

## Orchestrator recipe — "set up review of my doc with you and another AI"

When a human hands you (the AI) a document plus this repo and asks you to set up
a review with you **and** a second AI (Codex, another Claude, etc.), do this:

1. **Render their doc to HTML.** Convert it to one standalone HTML file with the
   content inside a `<div class="layout">…</div>` wrapper. Do this yourself — no
   markdown dependency, keep the repo stdlib-only. Save it (e.g. `review.html`).
2. **Start the server** (add `--blind` if they want each AI to form an
   independent first pass before seeing the other):
   ```bash
   python3 comment-server.py --doc review.html --port 8802 [--blind]
   ```
3. **Become the first reviewer yourself.** Run `watch.py` under your
   background/monitor tool with your own label, and answer comments via the API:
   ```bash
   RC_AS=claude python3 watch.py
   ```
4. **Hand off the second reviewer.** In your reply to the human, give them a
   ready-to-paste block to drop into a fresh **Codex** (or other shell-capable
   agent) session — see the template below. Tell them to `cd` into this repo
   first so the agent can read it.
5. **Tell the human the URL** (`http://localhost:8802`) to open, highlight text,
   and comment. If you started blind, remind them to click **👁 Reveal** once both
   AIs have done their independent passes.

### The paste-in handoff for the second AI

Codex has **no Monitor-equivalent autonomous wake** (confirmed with Codex): a
live Codex session reviews only while its turn is alive, so the instruction must
tell it to *keep watching and stay in the loop*. Generate a block like this for
the human to paste (swap the port/label if you changed them):

```text
You're a reviewer in a shared inline-comment tool (agent-doc-collab), joining as
author "codex". First `cd` into the agent-doc-collab repo and read ./AGENTS.md
(or ./CLAUDE.md) — it describes the review loop and HTTP API.

Watch for new comments by running this as a long-lived command and keeping it open:

    RC_AS=codex RC_STATE_URL=http://localhost:8802/api/state python3 watch.py

You have no background-wake tool, so keep this turn alive and keep reading the
watcher's output. For each new "[doc review]" line it prints:
  1. GET http://localhost:8802/api/state?as=codex  — read the comment/thread.
  2. Write a concise, useful review reply.
  3. POST http://localhost:8802/api/comments/<id>/reply
       body {"text":"<your reply>","author":"codex"}
Reply ONLY as author "codex", never "human". If you cannot keep a process open,
instead poll http://localhost:8802/api/state?as=codex every few seconds, track
seen comment/reply ids, and reply to each new one. Stay in the loop until I tell
you to stop.
```

**Durability caveat:** a pasted-in live session reviews only as long as that turn
runs — fine for an interactive sitting, but it isn't a hands-off daemon. For
unattended/continuous review, an external supervisor (poll `/api/state?as=codex`,
shell out to `codex exec` per new comment, POST the reply) is the robust option.

## HTTP API

| Method | Path | Body | Purpose |
|---|---|---|---|
| GET  | `/api/state` | — | `{version, blind, comments[]}` snapshot — poll `version` |
| GET  | `/api/state?as=<label>` | — | same, blind-filtered to that agent's view during a blind round |
| POST | `/api/comments` | `{anchor, text, author}` | add a comment |
| POST | `/api/comments/<id>/reply` | `{text, author}` | reply to a comment |
| POST | `/api/comments/<id>/resolve` | `{resolved, author}` | resolve / unresolve |
| POST | `/api/comments/<id>/delete` | `{author}` | delete |
| POST | `/api/blind` | `{blind: true\|false}` | start / end the blind round |

A comment is `{id, anchor, text, author, created, resolved, replies[]}`. A
reply is `{text, author, created}`.

## Code map

- **`comment-server.py`**
  - `Store` — thread-safe JSON-backed comment store; one lock guards the whole
    file, `version` bumps on every write, atomic temp-file flush. A corrupt
    sidecar is backed up (`.json.corrupt`) and reset rather than crashing.
    Methods: `add_comment`, `add_reply`, `set_resolved`, `delete`, `set_blind`,
    `snapshot`. The store also holds the round-level `blind` flag.
  - `is_human` / `filter_for_viewer` — `human` is the only reserved author;
    everything else is a self-declared agent label. `filter_for_viewer` builds an
    agent's blind view (own + human comments, other agents' replies stripped)
    without mutating stored data. `do_GET /api/state` applies it when `blind` is
    set and an `?as=<label>` is present.
  - `INJECT_CSS` / the injected JS — the in-browser annotation layer (selection
    → "Comment" button → anchored comment, plus the sidebar drawer). Kept as
    plain strings so there's no Python brace-escaping; runtime config is passed
    in as a JSON blob via `build_injection`.
  - `make_handler` — the `BaseHTTPRequestHandler`; `do_GET` serves the doc +
    `/api/state`, `do_POST` handles the comment endpoints.
  - `post_webhook` — optional fire-and-forget POST on each new human comment
    (`--webhook`); most integrations use `watch.py` instead.
- **`watch.py`** — polls `/api/state?as=<RC_AS>`, prints one line per new item
  not authored by you. Env: `RC_STATE_URL` (default
  `http://localhost:8802/api/state`), `RC_POLL_SEC` (default 3), `RC_AS` (your
  reviewer label, default `agent`).

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
--blind                  start in a blind round (agents see only own + human until Reveal)
```

## Conventions when working on this repo

- **Stdlib only.** No third-party deps, no build step — that's a core
  constraint. Keep it that way.
- The injected CSS/JS lives as plain Python strings in `comment-server.py`;
  per-request config flows through the JSON blob in `build_injection`, not
  string interpolation into the markup.
- `author` is a free-form, self-declared label; `"human"` is the only reserved
  value (the browser posts it). The server's human/agent split is `is_human()`,
  and blind filtering keys off the `?as=<label>` viewer — keep both honoring that
  one reserved name rather than hardcoding specific agent names.
- Python 3.8+. MIT licensed.
