#!/usr/bin/env python3
"""
agent-doc-collab comment watcher — for a coding agent's background monitor.

Polls the comment server's /api/state and prints ONE line per NEW item that is
NOT authored by you. Run this under your agent's background "monitor" tool
(Claude Code's Monitor, or any equivalent that turns each stdout line into a
prompt), so the agent is woken on feedback without the server pushing anywhere —
or just poll /api/state yourself.

Identify yourself with RC_AS (e.g. RC_AS=claude). Then:
  - you're woken on the human's comments + any OTHER agent's comments, never your
    own (so your replies aren't re-surfaced as feedback);
  - the URL carries ?as=<you>, so during a BLIND round the server shows you only
    your own + the human's comments — other agents stay hidden until the human
    clicks Reveal, after which their notes start waking you too.
If RC_AS is unset it defaults to 'agent' (the legacy single-agent behavior:
surface everything not authored by 'agent').

On startup it baselines everything already present (no backlog spam) and then
emits only new activity. Transient fetch errors are swallowed so a blip doesn't
kill the watch. Cheap change-detection via the state `version` field.

Usage:
    RC_AS=claude python3 watch.py
Optional env: RC_STATE_URL (default http://localhost:8802/api/state),
              RC_POLL_SEC (default 3), RC_AS (your reviewer label, default 'agent').
"""
import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get('RC_STATE_URL', 'http://localhost:8802/api/state')
POLL = float(os.environ.get('RC_POLL_SEC', '3'))
ME = os.environ.get('RC_AS', 'agent')
# Tell the server who's asking so blind rounds filter our view to own + human.
sep = '&' if '?' in BASE else '?'
URL = f'{BASE}{sep}as={ME}'
REPLY_HINT = ('curl -s -X POST http://localhost:8802/api/comments/{id}/reply '
              '-H "Content-Type: application/json" -d \'{{"text":"...","author":"%s"}}\'' % ME)


def fetch():
    with urllib.request.urlopen(URL, timeout=8) as r:
        return json.load(r)


def human_items(state):
    """Yield (key, comment, reply_or_None) for every item NOT authored by ME."""
    for c in state.get('comments', []):
        if c.get('author') != ME:
            yield (('c', c['id']), c, None)
        for i, rep in enumerate(c.get('replies', []) or []):
            if rep.get('author') != ME:
                yield (('r', c['id'], i), c, rep)


def snip(s, n=160):
    s = ' '.join((s or '').split())
    return s if len(s) <= n else s[:n] + '…'


def loc_of(c):
    a = c.get('anchor') or {}
    return a.get('label') if a.get('type') == 'element' else a.get('quote')


def main():
    seen = set()
    last_version = None
    # Baseline: mark current items as seen so we only surface NEW activity.
    try:
        st = fetch()
        last_version = st.get('version')
        for key, _c, _r in human_items(st):
            seen.add(key)
    except Exception:
        pass
    print(f'[doc-review watcher armed as "{ME}"] {URL} — {len(seen)} existing item(s) baselined', flush=True)

    while True:
        try:
            st = fetch()
            v = st.get('version')
            if v != last_version:
                last_version = v
                for key, c, rep in human_items(st):
                    if key in seen:
                        continue
                    seen.add(key)
                    cid = c['id']
                    hint = REPLY_HINT.format(id=cid)
                    if rep is None:
                        print(f'[doc review] New comment #{cid} on "{snip(loc_of(c), 60)}": '
                              f'{snip(c.get("text"))}  |  reply: {hint}', flush=True)
                    else:
                        print(f'[doc review] New reply on #{cid} ("{snip(loc_of(c), 50)}"): '
                              f'{snip(rep.get("text"))}  |  reply: {hint}', flush=True)
        except Exception:
            pass  # transient — keep polling
        time.sleep(POLL)


if __name__ == '__main__':
    main()
