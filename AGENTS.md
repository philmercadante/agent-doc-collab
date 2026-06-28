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

## The loop

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
a background command), Codex has **no autonomous per-line wake**. So a live Codex
session reviews only while its turn is active. To act as a continuous reviewer:

- Run `watch.py` (or the poll) as a long-lived command, **keep the turn alive**,
  and keep reading its output — replying to each new `[doc review]` line.
- Do **not** end the turn just because the watcher is armed. Stay in the loop
  until the human says to stop.
- For hands-off / unattended review, the robust pattern is an external
  supervisor: poll `/api/state?as=codex` and shell out to `codex exec` per new
  comment, then POST the reply.

## Blind review

If the human started a **blind round**, `GET /api/state?as=codex` shows you only
your own + the human's comments until they click Reveal — so you form an
independent opinion first. Nothing extra to do; passing `?as=codex` (which
`RC_AS` does) is what scopes your view. See CLAUDE.md for the full model.

## Working on this repo

Stdlib only, no build step, `human` is the only reserved author — see the
"Conventions" section of [CLAUDE.md](CLAUDE.md).
