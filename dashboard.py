#!/usr/bin/env python3
"""Local dashboard: serves a freshly built report on every page load.

  python3 dashboard.py [--port 8520] [--bind 127.0.0.1]

  /                       report (query: range=24h|7d|30d|all, or since=...&until=...)
  /pdf?<same query>       same report as a PDF download
"""
import argparse
import datetime as dt
import html
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from modem_check import DEFAULT_CONFIG, HERE, load_config, open_db
from report import build_html, load, resolve_range, write_pdf

PRESETS = [("24h", "Last 24 h", dt.timedelta(hours=24)), ("7d", "Last 7 days", dt.timedelta(days=7)),
           ("30d", "Last 30 days", dt.timedelta(days=30)), ("all", "All data", None)]
LOCK = threading.Lock()  # matplotlib isn't thread-safe; build one report at a time

NAV_CSS = """
<style>
.dash-nav { display:flex; flex-wrap:wrap; gap:8px; align-items:center; padding:10px 0 14px;
            border-bottom:1px solid var(--grid); font-size:13px; }
.dash-nav a, .dash-nav button { color:var(--ink); text-decoration:none; border:1px solid var(--grid);
            background:#fff; border-radius:6px; padding:4px 10px; font:inherit; cursor:pointer; }
.dash-nav a.on { background:var(--ink); color:#fff; border-color:var(--ink); }
.dash-nav form { display:flex; gap:6px; align-items:center; margin-left:auto; flex-wrap:wrap; }
.dash-nav input { font:inherit; padding:3px 6px; border:1px solid var(--grid); border-radius:6px; }
@media print { .dash-nav { display:none; } }
</style>
"""


def nav(query, active):
    links = "".join(f'<a class="{"on" if key == active else ""}" href="/?range={key}">{label}</a>'
                    for key, label, _ in PRESETS)
    since = html.escape(query.get("since", ""))
    until = html.escape(query.get("until", ""))
    pdf_q = urlencode({k: v for k, v in query.items() if v})
    return (f'<nav class="dash-nav">{links}<a href="/pdf?{pdf_q}">Download PDF</a>'
            f'<form method="get" action="/"><label>From <input type="date" name="since" value="{since}"></label>'
            f'<label>to <input type="date" name="until" value="{until}"></label><button>Show</button></form></nav>')


class Handler(BaseHTTPRequestHandler):
    cfg = None

    def _range(self, query):
        db = open_db(self.cfg)
        if query.get("since") or query.get("until"):
            active = None
            since, until = resolve_range(db, query.get("since") or None, query.get("until") or None)
        else:
            active = query.get("range", "24h")
            delta = next((d for k, _, d in PRESETS if k == active), PRESETS[0][2])
            since, until = resolve_range(db)
            if delta:
                since = max(since, until - delta)
        return db, since, until, active

    def do_GET(self):
        url = urlparse(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/":
                with LOCK:
                    db, since, until, active = self._range(query)
                    page = build_html(self.cfg, load(db, since, until), since, until)
                page = page.replace("</head>", NAV_CSS + "</head>", 1)
                page = page.replace("<body>", "<body>\n" + nav(query, active), 1)
                self._send(200, "text/html; charset=utf-8", page.encode())
            elif url.path == "/pdf":
                tmp_root = HERE / "data" / "tmp"
                tmp_root.mkdir(parents=True, exist_ok=True)
                with LOCK, tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
                    db, since, until, _ = self._range(query)
                    src = Path(tmp) / "report.html"
                    src.write_text(build_html(self.cfg, load(db, since, until), since, until))
                    pdf = src.with_suffix(".pdf")
                    write_pdf(src, pdf)
                    body = pdf.read_bytes()
                name = f"modem_report_{since:%Y%m%d_%H%M}_{until:%Y%m%d_%H%M}.pdf"
                self._send(200, "application/pdf", body, {"Content-Disposition": f'attachment; filename="{name}"'})
            else:
                self._send(404, "text/plain", b"not found")
        except Exception as exc:  # show the error rather than a blank page
            self._send(500, "text/plain; charset=utf-8", f"error building report: {exc!r}".encode())

    def _send(self, code, ctype, body, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default=os.environ.get("MODEM_CHECK_CONFIG", DEFAULT_CONFIG))
    ap.add_argument("--port", type=int, default=8520)
    ap.add_argument("--bind", default="127.0.0.1", help="use 0.0.0.0 to allow other devices on the LAN")
    args = ap.parse_args()
    Handler.cfg = load_config(args.config)
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"dashboard on http://{args.bind}:{args.port}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
