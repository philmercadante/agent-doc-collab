#!/usr/bin/env python3
"""
agent-doc-collab hub — one always-on server, an index of docs, per-doc reviews.

The single-doc `comment-server.py` is great for a one-off review; the hub is for
teams (or a human+agent pair) who review docs all the time and don't want a new
server + port + URL per doc. One process on one well-known port:

    /                       index: every registered doc, newest first
    /d/<slug>/              that doc with the full inline-comment layer
    /d/<slug>/api/...       the per-doc comment API (same contract as
                            comment-server.py, just prefixed)
    /api/docs               register / list docs (JSON)

Registering a doc is one call — no process management:

    curl -s -X POST http://localhost:8800/api/docs \
         -H 'Content-Type: application/json' \
         -d '{"path": "/abs/path/to/spec.md", "title": "Foo spec"}'
    # -> {"slug": "foo-spec", "url": "/d/foo-spec/"}

An agent watches ONE doc with the unchanged watch.py:

    RC_AS=claude RC_STATE_URL=http://localhost:8800/d/foo-spec/api/state \
        python3 watch.py

Design notes:
- Stdlib only, same as the rest of the tool.
- Reuses comment-server.py's Store, annotation injection, and render pipeline —
  the review experience is identical to the single-doc server.
- The registry (data-dir/registry.json) is read fresh on every request, so
  `hub.py add` from a shell registers into a RUNNING hub with no reload.
- Docs are read fresh per request (edit + reload just works). Comment sidecars
  default to data-dir/comments/<slug>.comments.json; pass "store" to adopt an
  existing sidecar in place (e.g. one written by a past comment-server.py run).
- Archiving hides a doc from the index's active list; nothing is ever deleted.

Usage:
    python3 hub.py --port 8800 --host 0.0.0.0 --data-dir ./hub-data
    python3 hub.py add /abs/path/spec.md --title "Foo spec" [--data-dir ./hub-data]
    python3 hub.py list [--data-dir ./hub-data]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import render  # noqa: E402

# comment-server.py has a hyphen in its name; load it as a module the stdlib way.
_spec = importlib.util.spec_from_file_location(
    'comment_server', os.path.join(_HERE, 'comment-server.py'))
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    s = re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')
    return s or 'doc'


class Registry:
    """JSON-file doc registry. Read fresh per access; atomic writes."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.path = data_dir / 'registry.json'
        self.lock = threading.Lock()
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / 'comments').mkdir(exist_ok=True)

    def load(self) -> dict:
        if not self.path.exists():
            return {'docs': {}}
        try:
            data = json.loads(self.path.read_text())
            data.setdefault('docs', {})
            return data
        except (json.JSONDecodeError, OSError):
            return {'docs': {}}

    def _save(self, data: dict):
        tmp = self.path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)

    def add(self, path: str, title=None, slug=None, store=None,
            content_selector=None) -> dict:
        doc_path = Path(path).resolve()
        if not doc_path.exists():
            raise FileNotFoundError(str(doc_path))
        with self.lock:
            data = self.load()
            # Re-registering the same source path updates it instead of duping.
            for existing_slug, e in data['docs'].items():
                if e['path'] == str(doc_path):
                    if title:
                        e['title'] = title
                    self._save(data)
                    return {'slug': existing_slug, **e}
            base = slugify(slug or title or doc_path.stem)
            s, n = base, 2
            while s in data['docs']:
                s, n = f'{base}-{n}', n + 1
            entry = {
                'path': str(doc_path),
                'title': title or doc_path.stem,
                'added': time.strftime('%Y-%m-%d %H:%M'),
                'status': 'active',
                'store': str(Path(store).resolve()) if store
                         else str(self.data_dir / 'comments' / f'{s}.comments.json'),
            }
            if content_selector:
                entry['content_selector'] = content_selector
            data['docs'][s] = entry
            self._save(data)
            return {'slug': s, **entry}

    def set_status(self, slug: str, status: str):
        with self.lock:
            data = self.load()
            if slug not in data['docs']:
                return None
            data['docs'][slug]['status'] = status
            self._save(data)
            return data['docs'][slug]


class StoreCache:
    """One comment Store per slug, created lazily, re-created if the sidecar
    path in the registry changes."""

    def __init__(self):
        self.lock = threading.Lock()
        self.stores = {}   # slug -> (store_path, Store)

    def get(self, slug: str, store_path: str):
        with self.lock:
            cached = self.stores.get(slug)
            if cached and cached[0] == store_path:
                return cached[1]
            store = cs.Store(Path(store_path))
            self.stores[slug] = (store_path, store)
            return store


# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------

INDEX_CSS = """
<style>
  :root { --green:#3a6c3f; --paper:#fbfaf7; --ink:#2b2b2b; --line:#e2dbcd; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--paper); color:var(--ink);
         font-family:-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  .wrap { max-width:720px; margin:0 auto; padding:28px 18px 60px; }
  h1 { font-size:22px; margin:0 0 4px; }
  .sub { color:#7a7363; font-size:13px; margin-bottom:22px; }
  h2 { font-size:13px; text-transform:uppercase; letter-spacing:.05em;
       color:#947d52; margin:26px 0 10px; }
  a.doc { display:block; text-decoration:none; color:inherit;
          border:1px solid var(--line); border-radius:12px; background:#fff;
          padding:13px 15px; margin-bottom:10px; }
  a.doc:hover { border-color:var(--green); }
  .doc .t { font-weight:600; font-size:15px; }
  .doc .m { color:#9a9382; font-size:12px; margin-top:3px; }
  .badge { display:inline-block; min-width:20px; text-align:center;
           background:var(--green); color:#fff; border-radius:10px;
           font-size:11px; font-weight:700; padding:2px 7px; float:right; }
  .badge.zero { background:#cfc6b4; }
  .archived a.doc { opacity:.6; }
  .empty { color:#9a9382; padding:30px 0; text-align:center; line-height:1.7; }
  code { background:#f6f2e8; border-radius:5px; padding:1px 5px; font-size:12px; }
</style>
"""


def render_index(reg_data: dict, stores: StoreCache) -> str:
    docs = reg_data.get('docs', {})
    def counts(slug, e):
        try:
            snap = stores.get(slug, e['store']).snapshot()
            total = len(snap['comments'])
            open_ = sum(1 for c in snap['comments'] if not c.get('resolved'))
            return open_, total
        except Exception:
            return 0, 0

    def row(slug, e):
        open_, total = counts(slug, e)
        badge = (f'<span class="badge{" zero" if not open_ else ""}">'
                 f'{open_}</span>')
        meta = f'{e.get("added", "")} · {total} comment{"s" if total != 1 else ""}'
        return (f'<a class="doc" href="/d/{slug}/">{badge}'
                f'<div class="t">{e.get("title", slug)}</div>'
                f'<div class="m">{meta}</div></a>')

    newest_first = sorted(docs.items(), key=lambda kv: kv[1].get('added', ''),
                          reverse=True)
    active = [row(s, e) for s, e in newest_first if e.get('status') != 'archived']
    archived = [row(s, e) for s, e in newest_first if e.get('status') == 'archived']

    body = ''.join(active) if active else (
        '<div class="empty">No docs yet.<br>Register one:<br><br>'
        '<code>curl -X POST /api/docs -d \'{"path": "/abs/doc.md"}\'</code></div>')
    if archived:
        body += '<h2>Archived</h2><div class="archived">' + ''.join(archived) + '</div>'
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>Doc Hub</title>{INDEX_CSS}</head><body><div class="wrap">'
            '<h1>Doc Hub</h1><div class="sub">every review, one place</div>'
            f'{body}</div></body></html>')


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

def make_handler(registry: Registry, stores: StoreCache, poll_ms: int,
                 webhook: str | None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, code, html):
            body = html.encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self):
            try:
                n = int(self.headers.get('Content-Length', 0))
                return json.loads(self.rfile.read(n) or b'{}')
            except Exception:
                return {}

        def _doc(self, slug):
            e = registry.load()['docs'].get(slug)
            return (e, stores.get(slug, e['store'])) if e else (None, None)

        # -------------------------------------------------- GET
        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path

            if path in ('/', '/index.html'):
                self._html(200, render_index(registry.load(), stores))
                return

            if path == '/api/docs':
                data = registry.load()
                out = []
                for slug, e in sorted(data['docs'].items(),
                                      key=lambda kv: kv[1].get('added', ''),
                                      reverse=True):
                    snap = stores.get(slug, e['store']).snapshot()
                    out.append({'slug': slug, 'url': f'/d/{slug}/', **e,
                                'comments': len(snap['comments']),
                                'open': sum(1 for c in snap['comments']
                                            if not c.get('resolved'))})
                self._json(200, {'docs': out})
                return

            m = re.match(r'^/d/([a-z0-9-]+)(/.*)?$', path)
            if m:
                slug, rest = m.group(1), m.group(2) or ''
                entry, store = self._doc(slug)
                if not entry:
                    self._json(404, {'error': f'no doc "{slug}"'})
                    return
                if rest == '':
                    self.send_response(301)
                    self.send_header('Location', f'/d/{slug}/')
                    self.end_headers()
                    return
                if rest == '/api/state':
                    snap = store.snapshot()
                    viewer = (parse_qs(parsed.query).get('as') or [None])[0]
                    if snap['blind'] and viewer and not cs.is_human(viewer):
                        snap = cs.filter_for_viewer(snap, viewer)
                    self._json(200, snap)
                    return
                if rest == '/':
                    doc_path = Path(entry['path'])
                    try:
                        if doc_path.suffix.lower() in render.HTML_EXTS:
                            html = doc_path.read_text(encoding='utf-8',
                                                      errors='replace')
                        else:
                            html = render.render_document(doc_path)
                    except (OSError, ValueError) as e:
                        self._json(500, {'error': str(e)})
                        return
                    inj = cs.build_injection({
                        'pollMs': poll_ms,
                        'contentSelector': entry.get('content_selector', '.layout'),
                        'doc': doc_path.name,
                        'apiBase': f'/d/{slug}',
                    })
                    if '</body>' in html:
                        html = html.replace('</body>', inj + '\n</body>', 1)
                    else:
                        html += inj
                    self._html(200, html)
                    return

            self._json(404, {'error': 'not found'})

        # -------------------------------------------------- POST
        def do_POST(self):
            path = self.path.split('?', 1)[0]
            body = self._read_body()

            if path == '/api/docs':
                try:
                    entry = registry.add(
                        body.get('path', ''), title=body.get('title'),
                        slug=body.get('slug'), store=body.get('store'),
                        content_selector=body.get('content_selector'))
                except FileNotFoundError as e:
                    self._json(400, {'error': f'doc not found: {e}'})
                    return
                entry['url'] = f'/d/{entry["slug"]}/'
                print(f'[hub] registered {entry["slug"]} -> {entry["path"]}',
                      flush=True)
                self._json(200, entry)
                return

            m = re.match(r'^/api/docs/([a-z0-9-]+)/(archive|unarchive)$', path)
            if m:
                slug, action = m.groups()
                e = registry.set_status(
                    slug, 'archived' if action == 'archive' else 'active')
                self._json(200 if e else 404,
                           e or {'error': f'no doc "{slug}"'})
                return

            m = re.match(r'^/d/([a-z0-9-]+)/api(/.*)$', path)
            if m:
                slug, rest = m.groups()
                entry, store = self._doc(slug)
                if not entry:
                    self._json(404, {'error': f'no doc "{slug}"'})
                    return
                if rest == '/blind':
                    self._json(200, {'blind': store.set_blind(
                        body.get('blind', True))})
                    return
                if rest == '/comments':
                    c = store.add_comment(body.get('anchor') or {},
                                          body.get('text', ''),
                                          body.get('author', 'human'))
                    if cs.is_human(c['author']):
                        self._activity(slug, entry, 'comment', c, None)
                    self._json(200, c)
                    return
                cm = re.match(r'^/comments/(\d+)/(reply|resolve|delete)$', rest)
                if cm:
                    cid, action = int(cm.group(1)), cm.group(2)
                    if action == 'reply':
                        c = store.add_reply(cid, body.get('text', ''),
                                            body.get('author', 'human'))
                        if c is None:
                            self._json(404, {'error': 'no such comment'})
                            return
                        if cs.is_human(body.get('author')):
                            self._activity(slug, entry, 'reply', c,
                                           c['replies'][-1])
                        self._json(200, c)
                        return
                    if action == 'resolve':
                        c = store.set_resolved(cid, body.get('resolved', True))
                        self._json(200, c or {'error': 'no such comment'})
                        return
                    if action == 'delete':
                        ok = store.delete(cid)
                        self._json(200 if ok else 404, {'deleted': ok})
                        return

            self._json(404, {'error': 'not found'})

        def _activity(self, slug, entry, kind, comment, reply):
            item = reply or comment
            a = comment.get('anchor') or {}
            loc = a.get('label') if a.get('type') == 'element' else a.get('quote')
            def snip(s, n):
                s = (s or '').replace('\n', ' ').strip()
                return s if len(s) <= n else s[:n] + '…'
            print(f'[{slug}] [{kind}] #{comment["id"]} on '
                  f'"{snip(loc, 60)}": {snip(item["text"], 200)}', flush=True)
            cs.post_webhook(webhook, {
                'kind': kind, 'doc': slug, 'comment_id': comment['id'],
                'anchor': loc, 'text': item['text'],
                'reply_url': f'/d/{slug}/api/comments/{comment["id"]}/reply',
            })

    return Handler


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description='agent-doc-collab hub — one server, many docs.')
    p.add_argument('command', nargs='?', default='serve',
                   choices=['serve', 'add', 'list'],
                   help='serve (default) | add <path> | list')
    p.add_argument('path', nargs='?', help='doc path (for add)')
    p.add_argument('--port', type=int, default=8800)
    p.add_argument('--host', default='127.0.0.1',
                   help='bind host (0.0.0.0 to reach over LAN/VPN)')
    p.add_argument('--data-dir', default='hub-data',
                   help='registry + default comment sidecars live here')
    p.add_argument('--title', default=None, help='doc title (for add)')
    p.add_argument('--slug', default=None, help='explicit slug (for add)')
    p.add_argument('--store', default=None,
                   help='adopt an existing comment sidecar in place (for add)')
    p.add_argument('--content-selector', default=None,
                   help='per-doc commentable-root selector (for add; default .layout)')
    p.add_argument('--webhook', default=None,
                   help='optional URL POSTed a JSON event on each human comment/reply')
    p.add_argument('--poll-ms', type=int, default=3000)
    args = p.parse_args()

    registry = Registry(Path(args.data_dir).resolve())

    if args.command == 'add':
        if not args.path:
            raise SystemExit('add needs a doc path')
        entry = registry.add(args.path, title=args.title, slug=args.slug,
                             store=args.store,
                             content_selector=args.content_selector)
        print(json.dumps({**entry, 'url': f'/d/{entry["slug"]}/'}, indent=2))
        return

    if args.command == 'list':
        for slug, e in sorted(registry.load()['docs'].items(),
                              key=lambda kv: kv[1].get('added', ''), reverse=True):
            print(f'{slug:32} {e.get("status", "active"):9} {e["path"]}')
        return

    stores = StoreCache()
    handler = make_handler(registry, stores, args.poll_ms, args.webhook)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(f'doc hub: http://{args.host}:{args.port}/  (data: {registry.data_dir})')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nshutting down')
        httpd.shutdown()


if __name__ == '__main__':
    main()
