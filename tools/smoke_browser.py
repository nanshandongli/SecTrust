"""真机冒烟：真 Edge + 真 CDP 全流程（goto/get_text/click/type/wait_for/screenshot）。

一次性验证脚本，不进 unittest（unittest 全用替身，不依赖本机浏览器）。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

from execution.browser import BrowserSkill  # noqa: E402

PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>smoke</title></head>
<body>
<h1 id="title">初始文本</h1>
<button id="btn" onclick="document.getElementById('title').textContent='已点击';
 document.getElementById('q').value='按钮已填';
 const d=document.createElement('div'); d.id='done'; d.textContent='完成标记';
 document.body.appendChild(d);">点我</button>
<input id="q" value="">
</body></html>
"""


def main():
    tmp = tempfile.mkdtemp(prefix="sectrust-smoke-")
    page = os.path.join(tmp, "page.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(PAGE)
    url = "file:///" + page.replace("\\", "/")

    skill = BrowserSkill(timeout=20.0)
    print("[smoke] edge =", skill.edge_path or "(未找到)")
    mode = skill.start()
    print("[smoke] mode =", mode, skill._cdp_error or "")
    assert mode == "cdp", "CDP 未起来，无法真机验证"

    skill.goto(url)
    text = skill.get_text()["text"]
    print("[smoke] get_text =", repr(text[:60]))
    assert "初始文本" in text

    print("[smoke] click =", skill.click("#btn"))
    text2 = skill.get_text()["text"]
    print("[smoke] after click =", repr(text2[:80]))
    assert "已点击" in text2

    print("[smoke] type =", skill.type_text("#q", "手动输入"))

    ok = skill.wait_for("#done", timeout=5)
    print("[smoke] wait_for(#done) =", ok)
    assert ok

    shot = os.path.join(tmp, "shot.png")
    print("[smoke] screenshot =", skill.screenshot(shot))
    with open(shot, "rb") as f:
        magic = f.read(8)
    print("[smoke] png magic =", magic)
    assert magic == b"\x89PNG\r\n\x1a\n", "截图不是 PNG"

    skill.close()
    print("[smoke] 全部通过；临时目录:", tmp)


if __name__ == "__main__":
    main()
