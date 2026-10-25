# -*- coding: utf-8 -*-
"""Build a self-contained Chinese dashboard snapshot with live data embedded.

Why this exists: the dashboard page renders client-side (tables are filled by
JS fetch calls), so a plain "Save Page As" only captures the empty skeleton.
This tool freezes the four API responses into the page itself by overriding
window.fetch -- the page's own rendering logic then runs unchanged against the
embedded data. Result: a single HTML file that shows exactly what the board
showed, works offline (no server needed), and stays interactive (the status /
runner filters still operate on the embedded data).

Usage:
    python tools/build_dashboard_snapshot.py \
        --data-dir <dir containing api_tasks.json api_runners.json \
                       api_approvals.json api_dag.json> \
        --out dashboard_snapshot_zh.html

The four JSON files are produced by curling the board's API, e.g.:
    curl -s http://127.0.0.1:8090/api/tasks?limit=500 -o api_tasks.json
    curl -s http://127.0.0.1:8090/api/runners           -o api_runners.json
    curl -s http://127.0.0.1:8090/api/approvals?limit=50 -o api_approvals.json
    curl -s http://127.0.0.1:8090/api/dag               -o api_dag.json

Page skeleton comes from task_center_web.HTML (always the current code, so a
stale server process cannot leak an old broken page into the snapshot).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import task_center_web                                          # noqa: E402

API_FILES = {
    "tasks": "api_tasks.json",
    "runners": "api_runners.json",
    "approvals": "api_approvals.json",
    "dag": "api_dag.json",
}


def safe_json(obj) -> str:
    # Escape "</" so embedded JSON can never terminate the <script> block early.
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def build(data_dir: str, out_path: str) -> str:
    data = {}
    for key, fname in API_FILES.items():
        path = os.path.join(data_dir, fname)
        with open(path, encoding="utf-8") as f:
            data[key] = json.load(f)

    html = task_center_web.HTML
    assert "<script>" in html, "page template lost its <script> block"

    override = (
        "<script>\n"
        "// === snapshot: frozen live data, fetch overridden (page logic unchanged) ===\n"
        "var __SNAP__ = " + safe_json(data) + ";\n"
        "window.fetch = function(u){\n"
        "  var url = String(u);\n"
        "  var d = url.indexOf('/api/tasks') === 0 ? __SNAP__.tasks\n"
        "        : url.indexOf('/api/runners') === 0 ? __SNAP__.runners\n"
        "        : url.indexOf('/api/approvals') === 0 ? __SNAP__.approvals\n"
        "        : url.indexOf('/api/dag') === 0 ? __SNAP__.dag : [];\n"
        "  return Promise.resolve({ json: function(){ return Promise.resolve(d); } });\n"
        "};\n"
        "</script>\n"
    )
    # Insert BEFORE the original tag (do not consume it), else the page's own JS
    # ends up outside any <script> block and renders as visible text.
    html = html.replace("<script>", override + "<script>", 1)

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    banner = (
        '<div style="background:#fff8e1;border-bottom:1px solid #ffe082;'
        'padding:8px 24px;font-size:13px;color:#6d4c00;">'
        "\u8fd0\u884c\u5feb\u7167 \u00b7 \u6293\u53d6\u4e8e " + ts +
        " \u00b7 \u6570\u636e\u6765\u81ea\u672c\u673a\u770b\u677f http://127.0.0.1:8090"
        " \u00b7 \u6b64\u6587\u4ef6\u81ea\u5e26\u6570\u636e\uff0c\u65e0\u9700\u670d\u52a1\u5668\u5373\u53ef\u6253\u5f00\uff08\u7b5b\u9009\u5668\u4ecd\u53ef\u7528\uff09"
        "</div>\n"
    )
    html = html.replace("<body>", "<body>\n" + banner, 1)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return html


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", required=True,
                    help="directory holding api_tasks.json / api_runners.json / "
                         "api_approvals.json / api_dag.json")
    ap.add_argument("--out", required=True, help="output HTML path")
    args = ap.parse_args()

    html = build(args.data_dir, args.out)
    print("written:", args.out)
    print("bytes:", len(html.encode("utf-8")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
