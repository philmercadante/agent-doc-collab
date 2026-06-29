# agent-doc-collab — agent guide (Codex / any coding agent)

This is the Codex-convention entry point. **The full integration guide is
[CLAUDE.md](CLAUDE.md)** — the loop, HTTP API, blind-review model, code map, and
conventions all live there and apply to every agent equally. This file is the
short version plus the Codex-specific operational notes; read CLAUDE.md for
detail. (Keep the two in sync — CLAUDE.md is canonical.)

## What this is

A stdlib-only Python comment server. A human serves an HTML doc, highlights text,
and leaves anchored inline comments; you (an agent) watch for new comments and
reply via an HTTP JSON API. Google-Docs-style margin comments, AI on the other
side.

## On-request review (the simplest path)

You don't need a watcher. When a human asks you to review, just:

1. **Bring your context.** Read any project `AGENTS.md` / context files in your
   working directory first, so you review with full project context — not only
   the doc. (Tip: run in the **project's** directory, not this repo, so that
   project's `AGENTS.md` auto-loads.)
2. **Read the state.** `GET http://localhost:8802/api/state?as=codex` for the
   comments + threads, and open the served URL for the full doc text.
3. **Answer everything open.** For every comment you (`codex`) haven't already
   replied to, POST a concise reply (see below). You may also raise your own
   points as new comments. Reply ONLY as `codex`, never `human`.
4. When the human later says "address any new comments," repeat 2–3 for the ones
   you haven't answered yet.

Because you're a normal interactive session, the human can **keep talking to you
after the review** — you still hold the doc + thread context.

## The watch loop (optional, for continuous review)

1. **Identify yourself.** Pick an `author` label (e.g. `codex`). `human` is the
   only reserved author (what the browser posts); every other label is an agent.
2. **Watch.** Run the notifier, telling it who you are:
   ```bash
   RC_AS=codex RC_STATE_URL=http://localhost:8802/api/state python3 watch.py
   ```
   It prints one line per new comment not authored by you. Or poll
   `GET /api/state?as=codex` yourself and diff the `version` field.
3. **Reply** under your own label:
   ```bash
   curl -s -X POST http://localhost:8802/api/comments/<id>/reply \
        -H 'Content-Type: application/json' \
        -d '{"text":"...","author":"codex"}'
   ```

## Codex-specific: no background-wake tool

Unlike Claude Code (whose `Monitor` tool re-prompts the agent on each new line of
a background command), Codex has **no autonomous per-line wake**. That's why the
default here is **on-request review** (above): the human just asks you to address
the open comments, and again when they add more. Don't design around per-comment
wake.

If you do want continuous review in one sitting, run `watch.py` and **keep the
turn alive**, replying to each new `[doc review]` line until told to stop. A true
hands-off daemon (an external supervisor polling `/api/state?as=codex` and
shelling out to `codex exec` per comment) is possible but not shipped.

## Blind review

If the human started a **blind round**, `GET /api/state?as=codex` shows you only
your own + the human's comments until they click Reveal — so you form an
independent opinion first. Nothing extra to do; passing `?as=codex` (which
`RC_AS` does) is what scopes your view. See CLAUDE.md for the full model.

## Working on this repo

Stdlib only, no build step, `human` is the only reserved author — see the
"Conventions" section of [CLAUDE.md](CLAUDE.md).
