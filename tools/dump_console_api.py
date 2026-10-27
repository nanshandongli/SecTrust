"""把协作审计台的真实 API JSON 落盘，供 Node 渲染冒烟脚本驱动（tools/console_render_smoke.js）。

用法：
    python tools/dump_console_api.py http://127.0.0.1:8099 data/api_dump.json
"""
import json
import sys
import urllib.request


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=15) as r:  # noqa: S310
        return json.loads(r.read().decode("utf-8"))


def main():
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099").rstrip("/")
    out = sys.argv[2] if len(sys.argv) > 2 else "data/api_dump.json"

    data = {}
    simple = ["/api/overview", "/api/roles", "/api/roster", "/api/assistants",
              "/api/connectors", "/api/schedule", "/api/library", "/api/security",
              "/api/skills", "/api/chat/status", "/api/chat/models", "/api/dag",
              "/api/runners", "/api/actions", "/api/schedule/due"]
    for p in simple:
        try:
            data[p] = get(base, p)
        except Exception as e:  # noqa: BLE001
            data[p] = {"__error__": str(e)}

    for p in ["/api/tasks?limit=6", "/api/tasks?limit=40", "/api/tasks?limit=500"]:
        try:
            data["/api/tasks"] = get(base, p)
        except Exception as e:  # noqa: BLE001
            data["/api/tasks"] = {"__error__": str(e)}
        if "/api/tasks" in data and "__error__" not in data["/api/tasks"]:
            break
    try:
        data["/api/traces"] = get(base, "/api/traces?limit=50")
    except Exception as e:  # noqa: BLE001
        data["/api/traces"] = {"__error__": str(e)}
    try:
        data["/api/cost"] = get(base, "/api/cost?days=30")
    except Exception as e:  # noqa: BLE001
        data["/api/cost"] = {"__error__": str(e)}
    try:
        ap = get(base, "/api/approvals?limit=100")
        data["/api/approvals"] = ap
        if isinstance(ap, list) and ap:
            data["/api/audit/approval/" + str(ap[0].get("approval_id"))] = get(
                base, "/api/audit/approval/" + str(ap[0].get("approval_id")))
    except Exception as e:  # noqa: BLE001
        data["/api/approvals"] = {"__error__": str(e)}
    # 一条真实轨迹（供轨迹回放面板）
    try:
        tr = data.get("/api/traces")
        if isinstance(tr, list) and tr:
            tid = tr[0].get("trace_id")
            data["/api/trace/" + str(tid)] = get(base, "/api/trace/" + str(tid))
    except Exception:  # noqa: BLE001
        pass

    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    print("dumped %d endpoints -> %s" % (len(data), out))
    for k, v in data.items():
        if isinstance(v, dict) and "__error__" in v:
            print("  [warn] %s: %s" % (k, v["__error__"]))


if __name__ == "__main__":
    main()
