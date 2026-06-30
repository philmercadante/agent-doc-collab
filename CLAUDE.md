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

1. **Point the server straight at their file — don't hand-roll HTML.** The
   server renders Markdown, plain text, `.docx` (Word), and HTML itself through
   `render.py` (one stdlib code path, so every session yields the same `.layout`
   page). Just pass the file (`--blind` if they want each AI to form an
   independent first pass first):
   ```bash
   python comment-server.py --doc their-file.docx --port 8802 [--blind]
   ```
   PDF needs `pdftotext` on PATH; without it, export to `.docx`/`.txt`, or read
   the PDF yourself and save the text as `.md`, then point at that. (Use `python`
   on Windows, `python3` on macOS/Linux.)
2. **Become the first reviewer yourself.** Run `watch.py` under your
   background/monitor tool with your own label, and answer comments via the API:
   ```bash
   RC_AS=claude python watch.py
   ```
3. **Hand off the second reviewer.** In your reply to the human, give them a
   ready-to-paste block to drop into a fresh **Codex** (or other) session — see
   below. Two things that matter:
   - **Project context:** have them open that session in their **project
     directory** (not this repo) — Codex auto-loads that project's `AGENTS.md`
     and files, so the reviewer knows the project, not just the doc. For extra
     review-specific context, drop a `review-context.md` and tell it to read that
     first.
   - The session only needs to **reach the comment server** — fine for a local
     session hitting `localhost`; a cloud session needs a tunnel.
4. **Tell the human the URL** (`http://localhost:8802`) to open, highlight text,
   and comment. If you started blind, remind them to click **👁 Reveal** once both
   AIs have done their independent passes.

### The paste-in handoff for the second AI

**Default = on-request.** Codex has **no Monitor-equivalent autonomous wake**
(confirmed with Codex), so don't design around per-comment wake — the human just
re-runs the prompt when they've added comments. This is the simplest path and
needs no watcher. Generate a block like this (swap port / label / context path as
needed):

```text
You're a reviewer in a shared inline-comment doc tool (agent-doc-collab), joining
as author "codex". Read any project AGENTS.md / context here first, so you review
with full project context — not just the doc.

The doc is served at http://localhost:8802 (read the rendered page there for the
full text). To review:
1. GET http://localhost:8802/api/state?as=codex  — the comments + threads.
2. For every comment you ("codex") have NOT already replied to, post a concise,
   useful reply:
     POST http://localhost:8802/api/comments/<id>/reply
       body {"text":"<your reply>","author":"codex"}
   You may also raise your own points as new comments (POST /api/comments with an
   anchor). Reply ONLY as author "codex", never "human".
3. Tell me when you've addressed everything open.

Later I'll just say "address any new comments" — repeat steps 1–2 for the ones
you haven't answered yet.
```

After the review the human can **keep talking to this same Codex session** — it
still holds the doc + thread context, so discussion can continue outside the tool.

**Want it continuous instead of on-request?** Have that session run
`RC_AS=codex python3 watch.py` and stay in the loop (it prints one line per new
comment). For a true hands-off daemon, an external supervisor (poll
`/api/state?as=codex` + `codex exec` per comment) is the robust option — possible,
but not shipped.

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
- **`render.py`** — stdlib, cross-platform document → HTML. `render_document(path)`
  returns a full `.layout` page; handlers for Markdown, plain text, `.docx` (zip +
  XML, no `python-docx`), HTML passthrough, and PDF (via `pdftotext` if present).
  Used as a CLI (`--in/--out`) **and** imported by the server, so non-HTML `--doc`
  files convert through this one path — agents never hand-roll the HTML.

## Cross-platform / Windows

Everything is **stdlib Python, no shell-specific code** — the server, `watch.py`,
and `render.py` run unchanged on Windows, macOS, and Linux. Two things to tell a
Windows user:
- **The only dependency is a Python 3.8+ interpreter.** macOS usually has one;
  on Windows they install Python (or use the `py` launcher). Invoke with `python`
  (or `py -3`) on Windows, `python3` on macOS/Linux.
- **The "AI" must be a coding agent with a shell + filesystem** (Claude Code,
  Codex CLI) — it's what starts the server and hits the API. A plain desktop chat
  app with no tool access can't launch a local server. All-local means everything
  talks to `localhost`, so there's nothing to expose.

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
