#!/usr/bin/env python3
"""
agent-doc-collab — inline comment server for human↔agent doc review.

Serves any HTML document in the browser with an injected annotation layer so a
human can highlight text (or pin an element / diagram / image) and leave inline
comments. A coding agent watches for new comments (see watch.py, designed for an
agent's background "monitor" tool — or poll GET /api/state yourself), replies
through the JSON API, and the browser shows the reply inline. Iterate
comment-by-comment, no big summaries.

Design notes:
- Stdlib only (http.server + urllib). No third-party deps.
- The served doc is read fresh on every request and the annotation layer is
  injected before </body>. Editing the underlying doc just works on reload.
- Comments persist to a JSON sidecar (default <doc>.comments.json) so they
  survive a server restart.
- Anchoring uses a simplified W3C-style TextQuoteSelector (quote + prefix +
  suffix + char offsets within the content root). Element pins store a CSS path.
  If the doc changes enough that an anchor can't be re-found, the comment still
  shows in the sidebar as "orphaned" — never lost.

Multiple agents, optionally blind:
- Reviewer identity is a self-declared, arbitrary `author` label — 'claude',
  'codex', 'gemini', a second human, anything. The server keeps no allow-list;
  'human' is the only reserved author (what the browser posts).
- A "blind round" (POST /api/blind {"blind":true}, or --blind, or the sidebar
  button) makes GET /api/state?as=<label> show that agent only its own + the
  human's comments. The human reveals (POST /api/blind {"blind":false}) to drop
  the walls so agents can see and react to each other. Kills anchoring/groupthink.

Usage:
    python3 comment-server.py --doc example.html --port 8802
    python3 comment-server.py --doc example.html --webhook http://localhost:9999/hook
    python3 comment-server.py --doc example.html --blind   # start a blind round

Agent reply helpers (author=<your label> → not re-surfaced to you as feedback):
    curl -s 'http://localhost:8802/api/state?as=claude' | python3 -m json.tool
    curl -s -X POST http://localhost:8802/api/comments/<id>/reply \
         -H 'Content-Type: application/json' \
         -d '{"text": "your reply", "author": "claude"}'
    curl -s -X POST http://localhost:8802/api/comments/<id>/resolve \
         -H 'Content-Type: application/json' -d '{"author":"claude"}'
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Docs and the comment store are resolved relative to the current directory
# (or pass absolute paths). No project-specific roots.

# ---------------------------------------------------------------------------
# Persistent store
# ---------------------------------------------------------------------------

class Store:
    """Thread-safe JSON-backed comment store. One lock guards the whole file."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {'version': 0, 'next_id': 1, 'comments': [], 'blind': False}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
                self.data.setdefault('version', 0)
                self.data.setdefault('next_id', 1)
                self.data.setdefault('comments', [])
                self.data.setdefault('blind', False)
            except (json.JSONDecodeError, OSError):
                # Corrupt sidecar: back it up, start fresh rather than crash.
                try:
                    path.rename(path.with_suffix('.json.corrupt'))
                except OSError:
                    pass

    def _flush(self):
        self.data['version'] += 1
        tmp = self.path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)

    def snapshot(self):
        with self.lock:
            return {'version': self.data['version'],
                    'blind': self.data['blind'],
                    'comments': self.data['comments']}

    def add_comment(self, anchor, text, author):
        with self.lock:
            cid = self.data['next_id']
            self.data['next_id'] += 1
            c = {
                'id': cid,
                'anchor': anchor,
                'text': (text or '').strip(),
                'author': author or 'human',
                'created': time.strftime('%Y-%m-%d %H:%M'),
                'resolved': False,
                'replies': [],
            }
            self.data['comments'].append(c)
            self._flush()
            return c

    def add_reply(self, cid, text, author):
        with self.lock:
            for c in self.data['comments']:
                if c['id'] == cid:
                    r = {
                        'text': (text or '').strip(),
                        'author': author or 'human',
                        'created': time.strftime('%Y-%m-%d %H:%M'),
                    }
                    c['replies'].append(r)
                    self._flush()
                    return c
            return None

    def set_resolved(self, cid, resolved):
        with self.lock:
            for c in self.data['comments']:
                if c['id'] == cid:
                    c['resolved'] = bool(resolved)
                    self._flush()
                    return c
            return None

    def delete(self, cid):
        with self.lock:
            before = len(self.data['comments'])
            self.data['comments'] = [c for c in self.data['comments'] if c['id'] != cid]
            if len(self.data['comments']) != before:
                self._flush()
                return True
            return False

    def set_blind(self, blind):
        """Flip the round-level blind flag. While blind, /api/state filters
        each agent's view to its own + human comments (see filter_for_viewer)."""
        with self.lock:
            self.data['blind'] = bool(blind)
            self._flush()
            return self.data['blind']


# A "human" reviewer authors comments as 'human' (what the browser posts). Every
# other author string is an agent/reviewer label, self-declared and arbitrary —
# 'claude', 'codex', 'gemini', 'aider', a second human, anything. The server
# keeps no allow-list; identity is honor-system, which fits the local-trust model.
HUMAN_AUTHOR = 'human'


def is_human(author):
    return (author or HUMAN_AUTHOR) == HUMAN_AUTHOR


def filter_for_viewer(snap, viewer):
    """Blind view for an agent labelled `viewer`: it sees the human's comments
    and its own, but NOT other agents' notes (and not their replies on otherwise
    visible threads). The human's own browser passes no viewer and sees all."""
    out = []
    for c in snap['comments']:
        if is_human(c.get('author')) or c.get('author') == viewer:
            nc = dict(c)
            nc['replies'] = [r for r in (c.get('replies') or [])
                             if is_human(r.get('author')) or r.get('author') == viewer]
            out.append(nc)
    return {'version': snap['version'], 'blind': snap['blind'], 'comments': out}


# ---------------------------------------------------------------------------
# Optional webhook on new comments
# ---------------------------------------------------------------------------

def post_webhook(webhook_url, payload):
    """Best-effort POST of a new-comment event to an optional webhook URL.
    Fire-and-forget; never blocks the HTTP response. Most agents won't need
    this — watch.py (or polling GET /api/state) is the simpler integration."""
    if not webhook_url:
        return
    def _send():
        try:
            body = json.dumps(payload).encode()
            req = urllib.request.Request(
                webhook_url, data=body,
                headers={'Content-Type': 'application/json'}, method='POST')
            urllib.request.urlopen(req, timeout=8)
        except Exception:
            pass  # webhook is best-effort; the API still has the comment
    threading.Thread(target=_send, daemon=True).start()


# ---------------------------------------------------------------------------
# Injected annotation layer (CSS + sidebar + JS). Kept as plain strings so no
# Python brace-escaping is needed; runtime config is passed via a JSON blob.
# ---------------------------------------------------------------------------

INJECT_CSS = """
<style id="rc-style">
:root { --rc-w: 360px; --rc-green:#3a6c3f; --rc-green-d:#2f5a34; }
* { -webkit-tap-highlight-color: rgba(58,108,63,.15); }

/* ---- Drawer (mobile-first: off-canvas overlay) ---- */
#rc-sidebar {
  position: fixed; top: 0; right: 0; width: min(92vw, var(--rc-w));
  height: 100vh; height: 100dvh;
  background: #fbfaf7; border-left: 1px solid #ddd6c9;
  box-shadow: -6px 0 22px rgba(0,0,0,.18);
  display: flex; flex-direction: column; z-index: 2147483300;
  transform: translateX(100%); transition: transform .26s ease;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px; color: #2b2b2b;
  -webkit-overflow-scrolling: touch;
  padding-bottom: env(safe-area-inset-bottom);
}
#rc-sidebar.open { transform: translateX(0); }
#rc-backdrop {
  position: fixed; inset: 0; background: rgba(0,0,0,.35);
  z-index: 2147483200; opacity: 0; pointer-events: none; transition: opacity .26s;
}
#rc-backdrop.open { opacity: 1; pointer-events: auto; }

#rc-head { padding: calc(12px + env(safe-area-inset-top)) 14px 12px; border-bottom: 1px solid #e7e0d4;
  display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
#rc-head h3 { margin:0; font-size:15px; font-weight:700; flex:1; }
#rc-head .rc-count { font-size:11px; color:#7a7363; background:#efe9dc; border-radius:10px; padding:2px 8px; }
#rc-blindtog.on { background:#3a2f5a; color:#fff; border-color:#2f255a; }
#rc-blindbanner { margin:0; padding:9px 14px; font-size:12px; line-height:1.5;
  background:#efe9f7; color:#3a2f5a; border-bottom:1px solid #ddd2ee; }
#rc-close { font-size:20px; line-height:1; background:none; border:none; color:#7a7363; cursor:pointer;
  min-width:40px; min-height:40px; }

.rc-btn { font: inherit; font-size:13px; cursor:pointer; border:1px solid #cfc6b4; background:#fff; color:#39342a;
  border-radius:8px; padding:9px 12px; min-height:40px; }
.rc-btn:active { background:#ece6da; }
.rc-btn.on { background:var(--rc-green); color:#fff; border-color:var(--rc-green-d); }
.rc-btn.primary { background:var(--rc-green); color:#fff; border-color:var(--rc-green-d); }
.rc-btn.danger { color:#9a3a2a; }
.rc-btn.small { padding:6px 10px; min-height:34px; font-size:12px; }

#rc-list { flex:1; overflow-y:auto; padding: 8px 10px 40px; -webkit-overflow-scrolling: touch; }
.rc-empty { color:#9a9382; padding: 18px 6px; text-align:center; line-height:1.6; }
.rc-card { border:1px solid #e2dbcd; border-radius:10px; background:#fff; margin-bottom:10px; overflow:hidden; }
.rc-card.resolved { opacity:.55; }
.rc-card.active { border-color:var(--rc-green); box-shadow:0 0 0 2px rgba(58,108,63,.18); }
.rc-card.orphan { border-style:dashed; }
.rc-quote { font-size:12px; color:#6b6453; background:#f6f2e8; border-bottom:1px solid #ece5d6;
  padding:7px 10px; cursor:pointer; max-height:64px; overflow:hidden; }
.rc-quote .rc-tag { display:inline-block; font-size:9px; font-weight:700; letter-spacing:.04em; text-transform:uppercase;
  color:#947d52; margin-right:6px; }
.rc-body { padding:9px 10px; }
.rc-msg { white-space:pre-wrap; line-height:1.5; word-wrap:break-word; overflow-wrap:anywhere; }
.rc-meta { font-size:10.5px; color:#9a9382; margin-top:4px; }
.rc-reply { border-top:1px dashed #e7e0d4; margin-top:8px; padding-top:8px; }
.rc-reply.agent { background:#eef4ee; margin:8px -10px -9px; padding:8px 10px; border-top:1px solid #d8e6d8; }
.rc-author { font-weight:700; font-size:10.5px; text-transform:uppercase; letter-spacing:.03em; }
.rc-author.human { color:#9a5a2a; }
.rc-author.agent { color:var(--rc-green); }
.rc-actions { display:flex; gap:8px; margin-top:10px; flex-wrap:wrap; }
.rc-replybox { margin-top:10px; display:none; }
.rc-replybox.open { display:block; }
textarea.rc-ta {
  width:100%; box-sizing:border-box; font:inherit; font-size:16px; /* 16px = no iOS zoom */
  border:1px solid #cfc6b4; border-radius:8px; padding:9px; resize:vertical; min-height:64px; }

/* ---- Highlights & pins in the doc ---- */
mark.rc-hl { background: #fff1a8; border-bottom:2px solid #e6c200; cursor:pointer; padding:0; color:inherit; }
mark.rc-hl.resolved { background:#e9e9e9; border-bottom-color:#bbb; }
mark.rc-hl.active { background:#ffd54a; }
.rc-pin {
  position:absolute; z-index:2147482000; width:28px; height:28px; border-radius:50%;
  background:var(--rc-green); color:#fff; font-size:13px; font-weight:700; line-height:28px; text-align:center;
  cursor:pointer; box-shadow:0 1px 5px rgba(0,0,0,.35); border:2px solid #fff; }
.rc-pin.resolved { background:#999; }
.rc-pin-target { outline:2px dashed var(--rc-green) !important; outline-offset:2px; }

/* ---- Floating action button (open drawer) ---- */
#rc-fab {
  position:fixed; right:16px; bottom:calc(16px + env(safe-area-inset-bottom));
  width:56px; height:56px; border-radius:50%; background:var(--rc-green); color:#fff;
  font-size:24px; border:none; box-shadow:0 4px 16px rgba(0,0,0,.32); z-index:2147483250; cursor:pointer; }
#rc-fab:active { background:var(--rc-green-d); }
#rc-fab .rc-badge {
  position:absolute; top:-4px; right:-4px; min-width:22px; height:22px; border-radius:11px;
  background:#c0392b; color:#fff; font-size:12px; line-height:22px; padding:0 6px; font-weight:700; }
#rc-fab .rc-badge.zero { display:none; }

/* ---- Selection action bar (mobile-friendly comment trigger) ---- */
#rc-selbar {
  position:fixed; left:50%; transform:translateX(-50%);
  bottom:calc(16px + env(safe-area-inset-bottom));
  z-index:2147483400; display:none; align-items:center; gap:10px;
  background:#2b2b2b; color:#fff; border-radius:26px; padding:8px 10px 8px 16px;
  box-shadow:0 6px 24px rgba(0,0,0,.32); max-width:92vw; }
#rc-selbar.show { display:flex; }
#rc-selbar .rc-seltext { font-size:12px; opacity:.85; max-width:46vw; overflow:hidden; white-space:nowrap; text-overflow:ellipsis;
  font-family:-apple-system,sans-serif; }
#rc-selbar button { font: inherit; font-size:14px; font-weight:600; cursor:pointer; border:none;
  background:var(--rc-green); color:#fff; border-radius:20px; padding:9px 14px; min-height:40px; white-space:nowrap; }

/* ---- Composer (centered modal) ---- */
#rc-modal-back {
  position:fixed; inset:0; background:rgba(0,0,0,.4); z-index:2147483500; display:none; }
#rc-modal-back.show { display:block; }
#rc-composer {
  position:fixed; z-index:2147483600; display:none;
  left:50%; top:calc(12px + env(safe-area-inset-top)); transform:translateX(-50%);
  width:min(92vw, 460px); max-height:78vh; overflow:auto; background:#fff;
  border:1px solid #cfc6b4; border-radius:14px; box-shadow:0 12px 40px rgba(0,0,0,.3); padding:14px;
  font-family:-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
#rc-composer.show { display:block; }
#rc-composer .rc-ctitle { font-size:13px; font-weight:700; color:#39342a; margin-bottom:8px; }
#rc-composer .rc-cq { font-size:12px; color:#6b6453; background:#f6f2e8; border-radius:7px; padding:7px 9px;
  margin-bottom:10px; max-height:80px; overflow:auto; line-height:1.45; }
#rc-composer .rc-actions { justify-content:flex-end; }

body.rc-pinmode, body.rc-pinmode * { cursor: crosshair !important; }
body.rc-pinmode #rc-pinhint { display:block; }
#rc-pinhint { display:none; position:fixed; left:50%; top:calc(10px + env(safe-area-inset-top)); transform:translateX(-50%);
  z-index:2147483400; background:var(--rc-green); color:#fff; font-size:13px; padding:8px 14px; border-radius:20px;
  box-shadow:0 3px 12px rgba(0,0,0,.3); font-family:-apple-system,sans-serif; }

#rc-toast { position:fixed; bottom:calc(84px + env(safe-area-inset-bottom)); left:50%; transform:translateX(-50%);
  z-index:2147483647; background:#2b2b2b; color:#fff; padding:9px 16px; border-radius:8px; font-size:13px;
  opacity:0; transition:opacity .25s; pointer-events:none; font-family:-apple-system,sans-serif; max-width:90vw; text-align:center; }
#rc-toast.show { opacity:.96; }

/* ---- Desktop: dock the drawer open, push content, hide FAB ---- */
@media (min-width: 900px) {
  body.rc-docked { margin-right: var(--rc-w); }
  body.rc-docked #rc-sidebar { transform: translateX(0); box-shadow:-2px 0 12px rgba(0,0,0,.06); }
  body.rc-docked #rc-backdrop { display:none; }
  body.rc-docked #rc-fab { display:none; }
  body.rc-docked #rc-close { display:none; }
}
</style>
"""

INJECT_HTML = """
<button id="rc-fab" title="Open review panel">💬<span class="rc-badge zero" id="rc-fab-badge">0</span></button>
<div id="rc-backdrop"></div>
<div id="rc-pinhint">Tap a diagram, image, or section to pin a comment</div>
<div id="rc-sidebar">
  <div id="rc-head">
    <h3>Review</h3>
    <span class="rc-count" id="rc-count">0</span>
    <button class="rc-btn small" id="rc-pinbtn" title="Pin a comment to an element / diagram / image">📌 Pin</button>
    <button class="rc-btn small" id="rc-togresolved" title="Show / hide resolved">Hide done</button>
    <button class="rc-btn small" id="rc-blindtog" title="Blind review: agents only see their own + your comments until you reveal">👁 Reveal</button>
    <button id="rc-close" title="Close">✕</button>
  </div>
  <div id="rc-blindbanner" hidden>🙈 <b>Blind round.</b> Each agent sees only its own notes and yours. Click <b>Reveal</b> to let them see each other.</div>
  <div id="rc-list"></div>
</div>

<div id="rc-selbar">
  <span class="rc-seltext" id="rc-seltext"></span>
  <button id="rc-selbtn">💬 Comment</button>
</div>

<div id="rc-modal-back"></div>
<div id="rc-composer">
  <div class="rc-ctitle" id="rc-composer-title">Add comment</div>
  <div class="rc-cq" id="rc-composer-q"></div>
  <textarea class="rc-ta" id="rc-composer-text" placeholder="Write a comment…"></textarea>
  <div class="rc-actions">
    <button class="rc-btn" id="rc-composer-cancel">Cancel</button>
    <button class="rc-btn primary" id="rc-composer-save">Comment</button>
  </div>
</div>
<div id="rc-toast"></div>
"""

INJECT_JS = r"""
<script>
(function(){
  "use strict";
  var CFG = window.RC_CONFIG || {};
  var POLL = CFG.pollMs || 3000;
  var root = document.querySelector(CFG.contentSelector) ||
             document.querySelector(".layout") || document.body;

  var state = { version: -1, comments: [], blind: false };
  var activeId = null;
  var pinMode = false;
  var showResolved = true;
  var pending = null;         // live text-selection candidate (from selectionchange)
  var composerAnchor = null;  // anchor LOCKED into the open composer — never touched by selectionchange
  var isWide = false;

  // ---- helpers ----
  function el(id){ return document.getElementById(id); }
  function esc(s){ var d=document.createElement("div"); d.textContent=s==null?"":s; return d.innerHTML; }
  function toast(msg){ var t=el("rc-toast"); t.textContent=msg; t.classList.add("show");
    clearTimeout(t._t); t._t=setTimeout(function(){t.classList.remove("show");},1900); }

  function api(method, path, body, cb){
    var x=new XMLHttpRequest(); x.open(method, path, true);
    x.setRequestHeader("Content-Type","application/json");
    x.onreadystatechange=function(){ if(x.readyState===4){
      var j=null; try{ j=JSON.parse(x.responseText);}catch(e){}
      cb && cb(x.status>=200&&x.status<300, j); } };
    x.send(body?JSON.stringify(body):null);
  }

  // ---- drawer ----
  function openDrawer(){ if(isWide) return; el("rc-sidebar").classList.add("open"); el("rc-backdrop").classList.add("open"); }
  function closeDrawer(){ el("rc-sidebar").classList.remove("open"); el("rc-backdrop").classList.remove("open"); }
  function applyLayout(){
    isWide = window.matchMedia("(min-width: 900px)").matches;
    document.body.classList.toggle("rc-docked", isWide);
    if(isWide){ closeDrawer(); }
  }

  // ---- text offset model (within content root) ----
  function textNodes(){
    var out=[], w=document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode:function(n){
        if(!n.nodeValue) return NodeFilter.FILTER_REJECT;
        var p=n.parentNode;
        while(p){ if(p.id==="rc-sidebar"||p.id==="rc-composer"||p.id==="rc-selbar"||
                      p.nodeName==="SCRIPT"||p.nodeName==="STYLE") return NodeFilter.FILTER_REJECT; p=p.parentNode; }
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var n; while((n=w.nextNode())) out.push(n);
    return out;
  }
  function fullText(nodes){ var s=""; for(var i=0;i<nodes.length;i++) s+=nodes[i].nodeValue; return s; }
  function globalOffset(nodes, node, off){
    var acc=0;
    for(var i=0;i<nodes.length;i++){
      if(nodes[i]===node) return acc+off;
      acc+=nodes[i].nodeValue.length;
    }
    return -1;
  }

  // ---- highlight rendering ----
  function clearMarks(){
    root.querySelectorAll("mark.rc-hl").forEach(function(m){
      var t=document.createTextNode(m.textContent); m.parentNode.replaceChild(t, m); });
    root.normalize();
    document.querySelectorAll(".rc-pin").forEach(function(p){p.remove();});
    document.querySelectorAll(".rc-pin-target").forEach(function(e){e.classList.remove("rc-pin-target");});
  }
  function locate(anchor, hay){
    var q=anchor.quote||""; if(!q) return null;
    if(typeof anchor.startOffset==="number" && hay.substr(anchor.startOffset, q.length)===q){
      return [anchor.startOffset, anchor.startOffset+q.length]; }
    var pre=anchor.prefix||"";
    if(pre){ var i=hay.indexOf(pre+q); if(i>=0) return [i+pre.length, i+pre.length+q.length]; }
    var suf=anchor.suffix||"";
    if(suf){ var j=hay.indexOf(q+suf); if(j>=0) return [j, j+q.length]; }
    var k=hay.indexOf(q); if(k>=0) return [k, k+q.length];
    return null;
  }
  function wrapRange(nodes, start, end, id, resolved){
    var segs=[], acc=0;
    for(var i=0;i<nodes.length;i++){
      var n=nodes[i], len=n.nodeValue.length, ns=acc, ne=acc+len; acc=ne;
      var s=Math.max(start,ns), e=Math.min(end,ne);
      if(e>s) segs.push({node:n, s:s-ns, e:e-ns});
    }
    for(var k=segs.length-1;k>=0;k--){
      var g=segs[k];
      try{
        var r=document.createRange(); r.setStart(g.node,g.s); r.setEnd(g.node,g.e);
        var m=document.createElement("mark");
        m.className="rc-hl"+(resolved?" resolved":""); m.setAttribute("data-rc-id",id);
        r.surroundContents(m);
        m.addEventListener("click", function(ev){ ev.stopPropagation();
          setActive(parseInt(this.getAttribute("data-rc-id"),10), true); });
      }catch(e){}
    }
  }
  function cssPath(node){
    if(!(node instanceof Element)) node=node.parentElement;
    var parts=[];
    while(node && node.nodeType===1 && node!==root && parts.length<8){
      var name=node.nodeName.toLowerCase();
      if(node.id){ parts.unshift(name+"#"+CSS.escape(node.id)); break; }
      var p=node.parentNode, idx=1, sib=node;
      while((sib=sib.previousElementSibling)){ if(sib.nodeName===node.nodeName) idx++; }
      parts.unshift(name+":nth-of-type("+idx+")");
      node=p;
    }
    return parts.join(" > ");
  }
  function placePin(target, id, resolved, label){
    var rect=target.getBoundingClientRect();
    var pin=document.createElement("div");
    pin.className="rc-pin"+(resolved?" resolved":"");
    pin.textContent=label;
    pin.style.left=(window.scrollX+rect.right-10)+"px";
    pin.style.top=(window.scrollY+rect.top-10)+"px";
    pin.setAttribute("data-rc-id",id);
    pin.addEventListener("click", function(ev){ ev.stopPropagation();
      setActive(parseInt(this.getAttribute("data-rc-id"),10), true); });
    document.body.appendChild(pin);
    target.classList.add("rc-pin-target");
  }
  function renderMarks(){
    clearMarks();
    var pinIdx=0;
    state.comments.forEach(function(c){
      if(c.resolved && !showResolved) return;
      var a=c.anchor||{};
      if(a.type==="element"){
        var t=null; try{ t=document.querySelector(a.selector); }catch(e){}
        if(t){ pinIdx++; placePin(t, c.id, c.resolved, String(pinIdx)); c._orphan=false; }
        else c._orphan=true;
      } else {
        // Recompute the text-node list + haystack PER comment. Each wrapRange
        // below calls surroundContents(), which splits the wrapped text node and
        // shortens the original node's value — so a shared node list would
        // under-count earlier nodes and drift every LATER comment's highlight
        // downward (the bug). Marks add no text, so offsets stay valid.
        var nodes=textNodes(), hay=fullText(nodes);
        var loc=locate(a, hay);
        if(loc){ wrapRange(nodes, loc[0], loc[1], c.id, c.resolved); c._orphan=false; }
        else c._orphan=true;
      }
    });
    applyActiveClass();
  }
  function applyActiveClass(){
    document.querySelectorAll("mark.rc-hl,.rc-pin").forEach(function(m){
      m.classList.toggle("active", parseInt(m.getAttribute("data-rc-id"),10)===activeId);
    });
  }

  // ---- sidebar list ----
  // 'human' shows as "Reviewer"; every other author is a self-declared agent
  // label (claude, codex, …) — show the label itself so reviewers are distinct.
  function authorTag(a){ var who=(!a||a==="human")?"Reviewer":a;
    return '<span class="rc-author '+esc(a||"human")+'">'+esc(who)+'</span>'; }
  function byId(id){ for(var i=0;i<state.comments.length;i++) if(state.comments[i].id===id) return state.comments[i]; return null; }

  function updateBadge(){
    var open=state.comments.filter(function(c){return !c.resolved;}).length;
    var b=el("rc-fab-badge"); b.textContent=open; b.classList.toggle("zero", open===0);
    el("rc-count").textContent=open+(state.comments.length>open?(" / "+state.comments.length):"");
  }
  function renderList(){
    var list=el("rc-list");
    updateBadge();
    // Preserve any in-progress reply you're typing across this rebuild, so a
    // comment landing from another reviewer (which triggers a refresh) can't wipe
    // it. We snapshot each open reply box's text / open-state / focus + caret and
    // the list scroll position, then restore them after re-rendering.
    var drafts={}, openBoxes={}, focusedId=null, caret=null, scrollTop=list.scrollTop;
    list.querySelectorAll(".rc-replybox").forEach(function(box){
      var id=box.getAttribute("data-rc-box"), ta=box.querySelector("textarea");
      if(ta && ta.value) drafts[id]=ta.value;
      if(box.classList.contains("open")) openBoxes[id]=true;
      if(ta && document.activeElement===ta){ focusedId=id; caret=[ta.selectionStart, ta.selectionEnd]; }
    });
    var shown=state.comments.filter(function(c){return showResolved||!c.resolved;});
    if(!shown.length){
      list.innerHTML='<div class="rc-empty">No comments yet.<br><br>'+
        'Select any text and tap <b>💬 Comment</b>,<br>or tap <b>📌 Pin</b> then a diagram / image.</div>';
      return;
    }
    var html="";
    shown.forEach(function(c){
      var cls="rc-card"+(c.resolved?" resolved":"")+(c.id===activeId?" active":"")+(c._orphan?" orphan":"");
      var a=c.anchor||{};
      var qlabel=(a.type==="element")?("📌 "+esc(a.label||a.selector||"element")):esc(a.quote||"");
      html+='<div class="'+cls+'" data-rc-id="'+c.id+'">';
      html+='<div class="rc-quote" data-rc-goto="'+c.id+'"><span class="rc-tag">'+
            (c._orphan?"orphaned · ":"")+(a.type==="element"?"pin":"quote")+'</span>'+qlabel+'</div>';
      html+='<div class="rc-body">';
      html+='<div class="rc-msg">'+authorTag(c.author)+' '+esc(c.text)+'</div>';
      html+='<div class="rc-meta">'+esc(c.created)+'</div>';
      (c.replies||[]).forEach(function(r){
        html+='<div class="rc-reply '+esc(r.author)+'">'+authorTag(r.author)+' '+
              '<span class="rc-msg">'+esc(r.text)+'</span>'+
              '<div class="rc-meta">'+esc(r.created)+'</div></div>';
      });
      html+='<div class="rc-actions">';
      html+='<button class="rc-btn small" data-rc-replytog="'+c.id+'">Reply</button>';
      html+='<button class="rc-btn small" data-rc-resolve="'+c.id+'">'+(c.resolved?"Reopen":"Resolve")+'</button>';
      html+='<button class="rc-btn small danger" data-rc-del="'+c.id+'">Delete</button>';
      html+='</div>';
      html+='<div class="rc-replybox" data-rc-box="'+c.id+'"><textarea class="rc-ta" placeholder="Reply…"></textarea>'+
            '<div class="rc-actions"><button class="rc-btn primary small" data-rc-replysend="'+c.id+'">Send reply</button></div></div>';
      html+='</div></div>';
    });
    list.innerHTML=html;
    // Restore the snapshotted drafts / open-state / focus + caret / scroll.
    Object.keys(drafts).forEach(function(id){
      var box=list.querySelector('[data-rc-box="'+id+'"]'); if(!box) return;
      var ta=box.querySelector("textarea"); if(ta) ta.value=drafts[id];
    });
    Object.keys(openBoxes).forEach(function(id){
      var box=list.querySelector('[data-rc-box="'+id+'"]'); if(box) box.classList.add("open");
    });
    if(focusedId!=null){
      var box=list.querySelector('[data-rc-box="'+focusedId+'"]');
      var ta=box&&box.querySelector("textarea");
      if(ta){ ta.focus(); try{ ta.setSelectionRange(caret[0], caret[1]); }catch(e){} }
    }
    list.scrollTop=scrollTop;
  }

  function setActive(id, scroll){
    activeId=id; applyActiveClass(); renderList(); openDrawer();
    if(scroll){
      var m=document.querySelector('[data-rc-id="'+id+'"].rc-card');
      if(m) m.scrollIntoView({block:"center", behavior:"smooth"});
    }
  }

  // ---- list interactions (delegated) ----
  el("rc-list").addEventListener("click", function(ev){
    var t=ev.target, id;
    // Never treat a click on a form field (or inside an open reply box) as a
    // card activation — re-rendering the list would destroy the field the user
    // just focused. Let the textarea/input keep focus.
    if(t.closest(".rc-replybox") || /^(TEXTAREA|INPUT|BUTTON)$/.test(t.nodeName)){
      // still allow the explicit button branches below to run for BUTTONs
      if(t.nodeName!=="BUTTON") return;
    }
    if((id=t.getAttribute("data-rc-goto"))){ setActive(parseInt(id,10), true); return; }
    if((id=t.getAttribute("data-rc-replytog"))){
      var box=document.querySelector('[data-rc-box="'+id+'"]'); if(box) box.classList.toggle("open"); return; }
    if((id=t.getAttribute("data-rc-resolve"))){
      var c=byId(parseInt(id,10));
      api("POST","/api/comments/"+id+"/resolve",{resolved:!(c&&c.resolved),author:"human"},function(ok){ if(ok) refresh(true); });
      return; }
    if((id=t.getAttribute("data-rc-del"))){
      if(confirm("Delete this comment?")) api("POST","/api/comments/"+id+"/delete",{author:"human"},function(ok){ if(ok) refresh(true); });
      return; }
    if((id=t.getAttribute("data-rc-replysend"))){
      var ta=document.querySelector('[data-rc-box="'+id+'"] textarea');
      var txt=ta&&ta.value.trim(); if(!txt) return;
      api("POST","/api/comments/"+id+"/reply",{text:txt,author:"human"},function(ok){ if(ok){ ta.value=""; refresh(true);} });
      return; }
    var card=t.closest && t.closest(".rc-card");
    if(card) setActive(parseInt(card.getAttribute("data-rc-id"),10), false);
  });

  // ---- selection capture (iOS Safari friendly) ----
  function hideSelBar(){ el("rc-selbar").classList.remove("show"); }
  function captureSelection(){
    if(pinMode) return;
    // While the composer is open, ignore selection changes entirely. Focusing
    // the textarea on iOS collapses the document selection and would otherwise
    // wipe the anchor we're about to post.
    if(el("rc-composer").classList.contains("show")) return;
    var sel=window.getSelection();
    if(!sel || sel.isCollapsed || !sel.rangeCount){ hideSelBar(); pending=null; return; }
    var rng=sel.getRangeAt(0);
    if(!root.contains(rng.commonAncestorContainer)){ hideSelBar(); pending=null; return; }
    var qstr=sel.toString();
    if(!qstr.trim()){ hideSelBar(); pending=null; return; }
    var nodes=textNodes();
    var start=globalOffset(nodes, rng.startContainer, rng.startOffset);
    var end=globalOffset(nodes, rng.endContainer, rng.endOffset);
    if(start<0||end<0||end<=start){ hideSelBar(); pending=null; return; }
    var hay=fullText(nodes);
    pending={ type:"text", quote:hay.slice(start,end),
              prefix:hay.slice(Math.max(0,start-40), start),
              suffix:hay.slice(end, end+40),
              startOffset:start, endOffset:end };
    el("rc-seltext").textContent='“'+qstr.trim().slice(0,40)+(qstr.trim().length>40?'…':'')+'”';
    el("rc-selbar").classList.add("show");
  }
  // selectionchange fires reliably on iOS; debounce so we read the *settled* selection
  document.addEventListener("selectionchange", function(){
    clearTimeout(window._rcSel); window._rcSel=setTimeout(captureSelection, 220);
  });

  el("rc-selbtn").addEventListener("click", function(){
    if(!pending) return;
    hideSelBar();
    openComposer(pending);
  });

  // ---- composer modal ----
  function openComposer(anchor){
    composerAnchor=anchor;   // locked for the life of the modal
    pending=null;            // consumed; selectionchange is also gated while open
    el("rc-composer-title").textContent=(anchor.type==="element")?"Comment on element":"Comment on selection";
    el("rc-composer-q").textContent=(anchor.type==="element")?("📌 "+(anchor.label||anchor.selector)):anchor.quote;
    el("rc-composer-text").value="";
    el("rc-modal-back").classList.add("show");
    el("rc-composer").classList.add("show");
    setTimeout(function(){ el("rc-composer-text").focus(); }, 50);
  }
  function closeComposer(){
    el("rc-composer").classList.remove("show");
    el("rc-modal-back").classList.remove("show");
    composerAnchor=null;
  }
  el("rc-composer-cancel").addEventListener("click", closeComposer);
  el("rc-modal-back").addEventListener("click", closeComposer);
  function submitComposer(){
    var txt=el("rc-composer-text").value.trim();
    if(!txt){ toast("Type a comment first"); el("rc-composer-text").focus(); return; }
    if(!composerAnchor){ toast("Lost the anchor — please reselect"); closeComposer(); return; }
    var btn=el("rc-composer-save"); btn.disabled=true;
    api("POST","/api/comments",{anchor:composerAnchor,text:txt,author:"human"},function(ok){
      btn.disabled=false;
      if(ok){ closeComposer(); try{ window.getSelection().removeAllRanges(); }catch(e){}
              toast("Comment added"); refresh(true); }
      else { toast("Save failed — try again"); }
    });
  }
  el("rc-composer-save").addEventListener("click", submitComposer);
  // Cmd/Ctrl+Enter as a keyboard shortcut for desktop
  el("rc-composer-text").addEventListener("keydown", function(ev){
    if((ev.metaKey||ev.ctrlKey) && ev.key==="Enter") submitComposer();
  });

  // ---- pin mode ----
  el("rc-pinbtn").addEventListener("click", function(){
    pinMode=!pinMode; this.classList.toggle("on",pinMode);
    document.body.classList.toggle("rc-pinmode",pinMode);
    hideSelBar();
    if(pinMode){ closeDrawer(); }   // so the doc is tappable on mobile
  });
  document.addEventListener("click", function(ev){
    if(!pinMode) return;
    var t=ev.target;
    if(t.closest("#rc-sidebar")||t.closest("#rc-composer")||t.closest("#rc-selbar")||
       t.closest("#rc-fab")||t.closest("#rc-pinhint")) return;
    ev.preventDefault(); ev.stopPropagation();
    pinMode=false; el("rc-pinbtn").classList.remove("on"); document.body.classList.remove("rc-pinmode");
    var label=(t.textContent||t.nodeName).trim().slice(0,50)||t.nodeName.toLowerCase();
    openComposer({ type:"element", selector:cssPath(t), label:label });
  }, true);

  // ---- header / fab / backdrop ----
  el("rc-fab").addEventListener("click", openDrawer);
  el("rc-close").addEventListener("click", closeDrawer);
  el("rc-backdrop").addEventListener("click", closeDrawer);
  el("rc-togresolved").addEventListener("click", function(){
    showResolved=!showResolved; this.textContent=showResolved?"Hide done":"Show done";
    renderMarks(); renderList();
  });

  // ---- keep element pins glued on scroll/resize ----
  function repositionPins(){
    document.querySelectorAll(".rc-pin").forEach(function(pin){
      var id=parseInt(pin.getAttribute("data-rc-id"),10), c=byId(id); if(!c||!c.anchor) return;
      var t=null; try{ t=document.querySelector(c.anchor.selector);}catch(e){}
      if(t){ var r=t.getBoundingClientRect();
        pin.style.left=(window.scrollX+r.right-10)+"px"; pin.style.top=(window.scrollY+r.top-10)+"px"; }
    });
  }
  document.addEventListener("scroll", repositionPins, {passive:true});
  window.addEventListener("resize", function(){ applyLayout(); renderMarks(); });
  window.addEventListener("orientationchange", function(){ setTimeout(function(){ applyLayout(); renderMarks(); }, 300); });

  // ---- polling ----
  function renderBlind(){
    var on=!!state.blind;
    var btn=el("rc-blindtog");
    btn.classList.toggle("on", on);
    // Button shows the action it performs: while blind, offer Reveal; otherwise
    // offer to start a blind round.
    btn.textContent=on?"👁 Reveal":"🙈 Blind";
    var banner=el("rc-blindbanner"); if(banner) banner.hidden=!on;
  }
  el("rc-blindtog").addEventListener("click", function(){
    api("POST","/api/blind",{blind:!state.blind,author:"human"},function(ok){ if(ok) refresh(true); });
  });

  function refresh(force){
    api("GET","/api/state",null,function(ok,j){
      if(!ok||!j) return;
      var blindChanged=(!!j.blind)!==(!!state.blind);
      if(!force && !blindChanged && j.version===state.version) return;
      state.version=j.version; state.comments=j.comments||[]; state.blind=!!j.blind;
      renderBlind(); renderMarks(); renderList();
    });
  }

  applyLayout();
  setTimeout(function(){ refresh(true); }, 400);     // after layout settles
  setTimeout(function(){ renderMarks(); }, 1600);    // after mermaid renders
  setInterval(function(){ refresh(false); }, POLL);
})();
</script>
"""


def build_injection(config: dict) -> str:
    cfg = '<script>window.RC_CONFIG=' + json.dumps(config) + ';</script>'
    return INJECT_CSS + INJECT_HTML + cfg + INJECT_JS


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

def make_handler(store: Store, doc_path: Path, config: dict, on_activity):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # quiet

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self):
            try:
                n = int(self.headers.get('Content-Length', 0))
                return json.loads(self.rfile.read(n) or b'{}')
            except Exception:
                return {}

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path == '/api/state':
                snap = store.snapshot()
                # ?as=<label> identifies the requesting agent. While the round is
                # blind, that agent sees only its own + human comments. No `as`
                # (the browser) or as=human always sees everything.
                viewer = (parse_qs(parsed.query).get('as') or [None])[0]
                if snap['blind'] and viewer and not is_human(viewer):
                    snap = filter_for_viewer(snap, viewer)
                self._json(200, snap)
                return
            if path in ('/', '/' + doc_path.name, '/index.html'):
                try:
                    html = doc_path.read_text()
                except OSError as e:
                    self._json(500, {'error': str(e)})
                    return
                inj = build_injection(config)
                if '</body>' in html:
                    html = html.replace('</body>', inj + '\n</body>', 1)
                else:
                    html = html + inj
                body = html.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._json(404, {'error': 'not found'})

        def do_POST(self):
            path = self.path.split('?', 1)[0]
            body = self._read_body()
            parts = path.strip('/').split('/')

            if path == '/api/blind':
                blind = store.set_blind(body.get('blind', True))
                self._json(200, {'blind': blind})
                return

            if path == '/api/comments':
                anchor = body.get('anchor') or {}
                c = store.add_comment(anchor, body.get('text', ''), body.get('author', 'human'))
                if is_human(c['author']):
                    on_activity('comment', c, None)
                self._json(200, c)
                return

            if len(parts) == 4 and parts[0] == 'api' and parts[1] == 'comments':
                try:
                    cid = int(parts[2])
                except ValueError:
                    self._json(400, {'error': 'bad id'}); return
                action = parts[3]
                if action == 'reply':
                    c = store.add_reply(cid, body.get('text', ''), body.get('author', 'human'))
                    if c is None:
                        self._json(404, {'error': 'no such comment'}); return
                    if is_human(body.get('author')):
                        on_activity('reply', c, c['replies'][-1])
                    self._json(200, c); return
                if action == 'resolve':
                    c = store.set_resolved(cid, body.get('resolved', True))
                    self._json(200, c or {'error': 'no such comment'}); return
                if action == 'delete':
                    ok = store.delete(cid)
                    self._json(200 if ok else 404, {'deleted': ok}); return

            self._json(404, {'error': 'not found'})

    return Handler


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description='agent-doc-collab inline comment server.')
    p.add_argument('--doc', default='example.html',
                   help='HTML doc to serve (path relative to CWD, or absolute)')
    p.add_argument('--port', type=int, default=8802)
    p.add_argument('--host', default='127.0.0.1',
                   help='bind host (use 0.0.0.0 to reach it from another device, e.g. over a LAN/VPN)')
    p.add_argument('--store', default=None,
                   help='comment JSON sidecar (default <doc>.comments.json next to the doc)')
    p.add_argument('--content-selector', default='.layout',
                   help='CSS selector for the commentable content root (falls back to <body>)')
    p.add_argument('--webhook', default=None,
                   help='optional URL to POST a JSON event to on each new human comment/reply')
    p.add_argument('--blind', action='store_true',
                   help='start in a blind round: each agent (GET /api/state?as=<label>) '
                        'sees only its own + human comments until the human clicks Reveal')
    p.add_argument('--poll-ms', type=int, default=3000)
    args = p.parse_args()

    doc_path = Path(args.doc).resolve()
    if not doc_path.exists():
        raise SystemExit(f'doc not found: {doc_path}')
    store_path = (Path(args.store).resolve() if args.store
                  else doc_path.with_name(doc_path.name + '.comments.json'))
    store = Store(store_path)
    if args.blind:
        store.set_blind(True)

    config = {'pollMs': args.poll_ms, 'contentSelector': args.content_selector, 'doc': doc_path.name}

    def snippet(s, n=90):
        s = (s or '').replace('\n', ' ').strip()
        return s if len(s) <= n else s[:n] + '…'

    def on_activity(kind, comment, reply):
        item = reply or comment
        if not is_human(item.get('author')):
            return  # only human feedback is surfaced; agents' notes are not
        a = comment.get('anchor') or {}
        loc = a.get('label') if a.get('type') == 'element' else a.get('quote')
        print(f'[{kind}] #{comment["id"]} on "{snippet(loc, 60)}": {snippet(item["text"], 200)}',
              flush=True)
        post_webhook(args.webhook, {
            'kind': kind, 'doc': doc_path.name, 'comment_id': comment['id'],
            'anchor': loc, 'text': item['text'],
            'reply_url': f'http://localhost:{args.port}/api/comments/{comment["id"]}/reply',
        })

    handler = make_handler(store, doc_path, config, on_activity)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(f'agent-doc-collab: serving {doc_path.name} on http://{args.host}:{args.port}/')
    print(f'  comments -> {store_path}')
    if args.webhook:
        print(f'  webhook  -> {args.webhook}')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nshutting down')
        httpd.shutdown()


if __name__ == '__main__':
    main()
