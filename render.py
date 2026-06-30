#!/usr/bin/env python3
"""
agent-doc-collab document renderer — turn a source document into the standalone
HTML page the comment server serves.

Stdlib only, cross-platform (no `textutil`/`pandoc`/shell). The point of putting
this in the tool — rather than letting each agent hand-roll HTML — is ONE code
path: the produced page always has the `.layout` content wrapper the annotation
layer expects, with consistent styling and proper escaping.

Supported inputs (by extension):
  .md / .markdown   minimal but real Markdown (headings, lists, code, quotes,
                    bold/italic/links, rules)
  .txt / .text      plain text → paragraphs (blank-line separated)
  .html / .htm      passed through unchanged (already a page; the server injects
                    into it as-is)
  .docx             Word — text + headings extracted via stdlib zip + XML
  .pdf              best-effort: uses `pdftotext` if present on PATH, else errors
                    with guidance (export to .docx/.txt, or have the agent paste
                    the text into a .md)

Usage:
    python render.py --in report.docx --out review.html
    python render.py --in notes.md --out review.html --title "Design notes"

Programmatic: `render_document(path) -> str` returns a full HTML page. The comment
server imports this and calls it for any non-HTML `--doc`.
"""
from __future__ import annotations

import argparse
import html
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

# Styling mirrors example.html so rendered docs look like a real document.
PAGE_CSS = """
  body { margin:0; background:#faf8f3; color:#23201a;
    font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; }
  .layout { max-width:720px; margin:0 auto; padding:48px 28px 120px; }
  h1 { font-size:30px; margin:24px 0 6px; }
  h2 { font-size:23px; margin:30px 0 8px; border-top:1px solid #e2dbcd; padding-top:16px; }
  h3 { font-size:19px; margin:24px 0 6px; }
  h4,h5,h6 { font-size:16px; margin:18px 0 4px; }
  p { margin:10px 0; }
  ul,ol { margin:10px 0; padding-left:26px; }
  li { margin:4px 0; }
  blockquote { margin:12px 0; padding:2px 16px; border-left:3px solid #d8cfba; color:#5d564a; }
  code { background:#efeada; border-radius:4px; padding:1px 5px;
    font-family:ui-monospace,Menlo,Consolas,monospace; font-size:14px; }
  pre { background:#efeada; border-radius:8px; padding:12px 14px; overflow:auto; }
  pre code { background:none; padding:0; }
  hr { border:none; border-top:1px solid #e2dbcd; margin:24px 0; }
  table { border-collapse:collapse; margin:12px 0; }
  td,th { border:1px solid #ddd6c9; padding:5px 10px; }
"""

PAGE_TMPL = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style></head>
<body><div class="layout">
{body}
</div></body></html>"""


def esc(s: str) -> str:
    return html.escape(s, quote=False)


# --------------------------------------------------------------------------
# Markdown (compact, dependency-free)
# --------------------------------------------------------------------------

def _inline(s: str) -> str:
    s = esc(s)
    s = re.sub(r'`([^`]+)`', lambda m: '<code>' + m.group(1) + '</code>', s)
    s = re.sub(r'\[([^\]]+)\]\(([^)\s]+)\)',
               lambda m: '<a href="' + html.escape(m.group(2), quote=True) + '">' + m.group(1) + '</a>', s)
    s = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', s)
    s = re.sub(r'__([^_]+)__', r'<strong>\1</strong>', s)
    s = re.sub(r'(?<![\*\w])\*([^*\n]+)\*(?!\*)', r'<em>\1</em>', s)
    s = re.sub(r'(?<![_\w])_([^_\n]+)_(?!\w)', r'<em>\1</em>', s)
    return s


def md_to_body(text: str) -> str:
    out, para = [], []
    list_type = None          # 'ul' | 'ol' | None
    in_fence, fence_buf = False, []
    quote_buf = []

    def flush_para():
        if para:
            out.append('<p>' + _inline(' '.join(para)) + '</p>')
            para.clear()

    def flush_list():
        nonlocal list_type
        if list_type:
            out.append('</' + list_type + '>')
            list_type = None

    def flush_quote():
        if quote_buf:
            out.append('<blockquote>' + _inline(' '.join(quote_buf)) + '</blockquote>')
            quote_buf.clear()

    for raw in text.split('\n'):
        line = raw.rstrip('\n')
        fence = re.match(r'^\s*```(.*)$', line)
        if fence:
            if in_fence:
                out.append('<pre><code>' + esc('\n'.join(fence_buf)) + '</code></pre>')
                fence_buf, in_fence = [], False
            else:
                flush_para(); flush_list(); flush_quote(); in_fence = True
            continue
        if in_fence:
            fence_buf.append(line); continue

        if not line.strip():
            flush_para(); flush_list(); flush_quote(); continue

        h = re.match(r'^(#{1,6})\s+(.*)$', line)
        if h:
            flush_para(); flush_list(); flush_quote()
            lvl = len(h.group(1))
            out.append(f'<h{lvl}>' + _inline(h.group(2).strip()) + f'</h{lvl}>')
            continue

        if re.match(r'^\s*([-*_])(\s*\1){2,}\s*$', line):   # --- *** ___
            flush_para(); flush_list(); flush_quote()
            out.append('<hr>'); continue

        q = re.match(r'^\s*>\s?(.*)$', line)
        if q:
            flush_para(); flush_list()
            quote_buf.append(q.group(1)); continue
        flush_quote()

        m = re.match(r'^\s*([-*+])\s+(.*)$', line)
        if m:
            flush_para()
            if list_type != 'ul':
                flush_list(); out.append('<ul>'); list_type = 'ul'
            out.append('<li>' + _inline(m.group(2)) + '</li>'); continue
        m = re.match(r'^\s*\d+\.\s+(.*)$', line)
        if m:
            flush_para()
            if list_type != 'ol':
                flush_list(); out.append('<ol>'); list_type = 'ol'
            out.append('<li>' + _inline(m.group(1)) + '</li>'); continue
        flush_list()

        para.append(line.strip())

    flush_para(); flush_list(); flush_quote()
    if in_fence:   # unterminated fence
        out.append('<pre><code>' + esc('\n'.join(fence_buf)) + '</code></pre>')
    return '\n'.join(out)


# --------------------------------------------------------------------------
# Plain text
# --------------------------------------------------------------------------

def txt_to_body(text: str) -> str:
    blocks = re.split(r'\n\s*\n', text.strip())
    return '\n'.join('<p>' + esc(b.strip()).replace('\n', '<br>') + '</p>'
                     for b in blocks if b.strip())


# --------------------------------------------------------------------------
# Word .docx (stdlib zip + XML; no python-docx dependency)
# --------------------------------------------------------------------------

_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def docx_to_body(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read('word/document.xml')
    root = ET.fromstring(xml)
    body = root.find(_W + 'body')
    out = []
    for p in (body.iter(_W + 'p') if body is not None else []):
        text = ''.join(t.text or '' for t in p.iter(_W + 't'))
        if not text.strip():
            continue
        style = ''
        ppr = p.find(_W + 'pPr')
        if ppr is not None:
            pstyle = ppr.find(_W + 'pStyle')
            if pstyle is not None:
                style = (pstyle.get(_W + 'val') or '').lower()
        m = re.search(r'heading\s*([1-6])|^h([1-6])$', style)
        if m:
            lvl = int(m.group(1) or m.group(2))
            out.append(f'<h{lvl}>' + esc(text.strip()) + f'</h{lvl}>')
        elif style in ('title',):
            out.append('<h1>' + esc(text.strip()) + '</h1>')
        else:
            out.append('<p>' + esc(text.strip()) + '</p>')
    if not out:
        raise ValueError('no readable text found in .docx')
    return '\n'.join(out)


# --------------------------------------------------------------------------
# PDF (best-effort, optional external tool)
# --------------------------------------------------------------------------

def pdf_to_body(path: Path) -> str:
    exe = shutil.which('pdftotext')
    if not exe:
        raise ValueError(
            'PDF needs `pdftotext` (poppler), which is not on PATH. On a machine '
            'without it, export the PDF to .docx/.txt first, or have the agent read '
            'the PDF and save the text as a .md — then render that.')
    proc = subprocess.run([exe, '-layout', str(path), '-'],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise ValueError('pdftotext failed: ' + (proc.stderr or '').strip())
    return txt_to_body(proc.stdout)


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

def render_document(path, title: str | None = None) -> str:
    """Return a full standalone HTML page (with the `.layout` wrapper) for a
    source document. `.html`/`.htm` is returned unchanged. Raises ValueError with
    a human-readable message for unsupported / unreadable inputs."""
    path = Path(path)
    ext = path.suffix.lower()
    title = title or path.stem

    if ext in ('.html', '.htm'):
        return path.read_text(encoding='utf-8', errors='replace')

    if ext in ('.md', '.markdown'):
        body = md_to_body(path.read_text(encoding='utf-8', errors='replace'))
    elif ext in ('.txt', '.text', ''):
        body = txt_to_body(path.read_text(encoding='utf-8', errors='replace'))
    elif ext == '.docx':
        body = docx_to_body(path)
    elif ext == '.pdf':
        body = pdf_to_body(path)
    else:
        # Last resort: try to read as UTF-8 text; binary blobs fail clearly.
        try:
            body = txt_to_body(path.read_text(encoding='utf-8'))
        except (UnicodeDecodeError, OSError):
            raise ValueError(
                f'unsupported document type "{ext}". Convert to .md, .txt, .html, '
                'or .docx (or export a .pdf) first.')
    return PAGE_TMPL.format(title=esc(title), css=PAGE_CSS, body=body)


HTML_EXTS = ('.html', '.htm')


def main():
    ap = argparse.ArgumentParser(description='Render a document to comment-ready HTML.')
    ap.add_argument('--in', dest='inp', required=True, help='source document')
    ap.add_argument('--out', required=True, help='output .html path')
    ap.add_argument('--title', default=None, help='page title (default: filename)')
    args = ap.parse_args()
    html_out = render_document(args.inp, title=args.title)
    Path(args.out).write_text(html_out, encoding='utf-8')
    print(f'rendered {args.inp} -> {args.out}')


if __name__ == '__main__':
    main()
