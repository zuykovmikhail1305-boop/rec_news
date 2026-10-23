#!/usr/bin/env python3
"""Build a network-free single-file demo using only Python's standard library."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
html = (ROOT / 'web_app/templates/insights/parser.html').read_text(encoding="utf-8")
for name in ('insights.css', 'parser.css'):
    css = (ROOT / 'web_app/static' / name).read_text(encoding="utf-8")
    html = html.replace(f'<link rel="stylesheet" href="/static/{name}">', f'<style>\n{css}\n</style>')
js = (ROOT / 'web_app/static/parser.js').read_text(encoding="utf-8")
html = html.replace('<script src="/static/parser.js"></script>', f'<script>\n{js}\n</script>')
html = html.replace('<a class="parser-back" href="/insights/prototype">← К прототипу</a>', '<span class="parser-back">Автономное демо · без сервера</span>')
html = html.replace('<a href="/insights/prototype" class="text-link">Открыть UI инсайтов →</a>', '<span class="text-link">Полный UI доступен при запуске FastAPI</span>')
(ROOT / 'docs/parser-demo.html').write_text(html, encoding="utf-8")
print('Built docs/parser-demo.html')
