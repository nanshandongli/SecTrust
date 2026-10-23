"""
渠道联调工具（tools/channel_doctor.py）—— 企微 / 飞书真实联调的自检与回环验证。

⚠️ 本工具**不会**替你配置平台后台，也不会替你申请凭据。它做三件事：
  1. **凭据自检**：检查环境变量是否齐备、格式是否像真的（不打印明文，只报长度/前缀）；
  2. **回环验证**：在本机起一个模拟平台（按官方协议签名/加密），
     打自己起的回调端点，验证「验签 → 解密 → 解析 → 回消息」整条链路是通的；
  3. **连通性探测**：真连平台官方接口（取 access_token / tenant_token），
     凭据无效时给出平台返回的原始错误码与可读解释。

用法：
    python tools/channel_doctor.py --channel wecom            # 凭据自检 + 回环
    python tools/channel_doctor.py --channel feishu
    python tools/channel_doctor.py --channel wecom --probe    # 额外真连平台（需真凭据）
    python tools/channel_doctor.py --channel wecom --json     # 机器可读

为什么需要它：真实联调最耗时的不是写代码，而是**逐个排除"是凭据错了还是代码错了"**。
本工具把「代码侧」全部自动化验证掉，剩下的只可能是平台后台配置或凭据问题。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from channels.feishu import FeishuChannel  # noqa: E402
from channels.wecom import WeComChannel, _sha1_signature  # noqa: E402
from config import ChannelConfig  # noqa: E402


# ---------------------------------------------------------------- 凭据自检
# 每项：(环境变量名, 必填, 说明, 校验函数)
# 校验函数返回 (ok: bool, 详情: str)——只报长度/前缀，绝不打印明文。
def _chk_len(min_len: int):
    def _f(v: str) -> Tuple[bool, str]:
        if not v:
            return False, "未设置"
        if len(v) < min_len:
            return False, f"长度 {len(v)} < 期望 {min_len}，格式可能不对"
        return True, f"长度 {len(v)} ✓"
    return _f


def _chk_wecom_aes(v: str) -> Tuple[bool, str]:
    if not v:
        return False, "未设置"
    if len(v) != 43:
        return False, f"长度 {len(v)}（企业微信 EncodingAESKey 应为 43 字符）"
    try:
        base64.b64decode(v + "=")
    except Exception:  # noqa: BLE001
        return False, "非合法 base64"
    return True, f"长度 43 ✓ base64 合法 ✓"


def _chk_feishu_secret(v: str) -> Tuple[bool, str]:
    if not v:
        return False, "未设置"
    if not v.startswith(("cli_", "t_")):
        return False, "前缀不像飞书 App Secret（通常 cli_ 开头）"
    return True, f"长度 {len(v)} ✓ 前缀 ✓"


def _chk_int(v: str) -> Tuple[bool, str]:
    if not v:
        return False, "未设置"
    if not v.isdigit():
        return False, "应为纯数字"
    return True, f"{v} ✓"


CRED_SPECS = {
    "wecom": [
        ("WECHAT_TOKEN", True, "回调 URL 验证用的 Token（后台自定义）", _chk_len(3)),
        ("WECHAT_AES_KEY", True, "消息加解密密钥（43 字符 base64）", _chk_wecom_aes),
        ("WECHAT_CORP_ID", True, "企业 ID", _chk_len(10)),
        ("WECHAT_AGENT_ID", True, "自建应用 AgentId", _chk_int),
        ("WECHAT_SECRET", True, "自建应用 Secret", _chk_len(20)),
    ],
    "feishu": [
        ("FEISHU_APP_ID", True, "应用 ID（cli_ 开头）", _chk_len(10)),
        ("FEISHU_APP_SECRET", True, "应用密钥", _chk_feishu_secret),
        ("FEISHU_VERIFICATION_TOKEN", False, "Verification Token（未用 sign 验签时需要）", _chk_len(10)),
        ("FEISHU_ENCRYPT_KEY", False, "事件加密 Encrypt Key（开启加密时必填）", _chk_len(10)),
    ],
}


def check_credentials(channel: str) -> Dict:
    """检查该渠道所需环境变量是否齐备。只报长度/前缀，不打印明文。"""
    specs = CRED_SPECS.get(channel, [])
    items = []
    ok_count = 0
    for name, required, desc, validator in specs:
        value = os.getenv(name, "")
        ok, detail = validator(value)
        if not value:
            ok, detail = False, "未设置"
        if not required and not value:
            ok, detail = True, "未设置（可选）"
        if ok:
            ok_count += 1
        items.append({
            "name": name, "required": required, "desc": desc,
            "ok": ok, "detail": detail,
        })
    required_missing = [i["name"] for i in items if i["required"] and not i["ok"]]
    return {
        "channel": channel,
        "items": items,
        "ok_count": ok_count,
        "total": len(items),
        "ready": not required_missing,
        "missing_required": required_missing,
    }


# ---------------------------------------------------------------- 回环验证
def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wecom_echo_plain(token: str, corp_id: str, aes_key: str,
                      user: str, text: str) -> str:
    """构造一条企业微信加密消息体（明文 XML -> 按官方格式加密 -> base64）。

    官方明文格式：random(16) + msg_len(4, BE) + msg + receive_id
    密钥派生：EncodingAESKey 补 '=' 后 base64 解码得 32 字节 key，**IV = key[:16]**
    （企业微信用固定 IV，不是随机 IV——用随机 IV 解不开）。
    """
    from security.crypto import aes_cbc_encrypt
    import struct
    import os as _os

    msg = (
        f"<xml><ToUserName><![CDATA[{corp_id}]]></ToUserName>"
        f"<FromUserName><![CDATA[{user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        f"<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{text}]]></Content>"
        f"<MsgId>1234567890123456</MsgId></xml>"
    ).encode("utf-8")
    body = _os.urandom(16) + struct.pack(">I", len(msg)) + msg + corp_id.encode("utf-8")
    key = base64.b64decode(aes_key + "=")
    iv = key[:16]
    cipher = aes_cbc_encrypt(key, iv, body)
    return base64.b64encode(cipher).decode()


def loopback_wecom(cfg: ChannelConfig) -> Dict:
    """在本机起回调端点，按官方协议造请求打自己，验证验签→解密→解析全链路。"""
    from http.server import HTTPServer
    import threading

    result = {"channel": "wecom", "steps": [], "ok": False}
    received = []

    def handler(inbound):
        received.append(inbound)

    token = cfg.wecom_token
    aes_key = cfg.wecom_aes_key
    corp_id = cfg.wecom_corp_id
    if not token or not aes_key:
        result["steps"].append({"step": "前置", "ok": False,
                                "detail": "缺 WECHAT_TOKEN 或 WECHAT_AES_KEY，无法回环"})
        return result

    port = _free_port()
    ch = WeComChannel(cfg)
    ch.port = port
    # 换掉凭据来源（回环用本地值）
    ch.token = token
    ch.aes_key = aes_key
    ch.corp_id = corp_id

    httpd = HTTPServer(("127.0.0.1", port), ch._make_handler(handler))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.2)

    try:
        # --- 步骤 1：URL 验证（GET）---
        # 企业微信的 echostr 也是按官方格式加密的 base64，需用同一套加解密回显
        ts, nonce = str(int(time.time())), "abc123"
        echo_plain = "ping-echo"
        from security.crypto import aes_cbc_encrypt
        import struct
        import os as _os
        _msg = echo_plain.encode("utf-8")
        _body = _os.urandom(16) + struct.pack(">I", len(_msg)) + _msg + corp_id.encode("utf-8")
        _key = base64.b64decode(aes_key + "=")
        echo_str = base64.b64encode(
            aes_cbc_encrypt(_key, _key[:16], _body)).decode()
        sig = _sha1_signature(token, ts, nonce)
        url = (f"http://127.0.0.1:{port}/?msg_signature={sig}"
               f"&timestamp={ts}&nonce={nonce}&echostr={urllib.parse.quote(echo_str)}")
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                body = r.read().decode("utf-8")
                got = r.status
            result["steps"].append({
                "step": "URL 验证(GET)", "ok": got == 200,
                "detail": f"HTTP {got}，回显 {body[:40]!r}"})
        except Exception as e:  # noqa: BLE001
            result["steps"].append({"step": "URL 验证(GET)", "ok": False,
                                    "detail": f"失败: {e}"})

        # --- 步骤 2：错误签名必须被拒（403）---
        bad_url = (f"http://127.0.0.1:{port}/?msg_signature=bad"
                   f"&timestamp={ts}&nonce={nonce}&echostr=x")
        try:
            with urllib.request.urlopen(bad_url, timeout=5) as r:
                code = r.status
            result["steps"].append({"step": "错误签名应被拒", "ok": False,
                                    "detail": f"竟然返回 HTTP {code}（应为 403）——验签失效！"})
        except urllib.error.HTTPError as e:
            result["steps"].append({"step": "错误签名应被拒", "ok": e.code == 403,
                                    "detail": f"HTTP {e.code} ✓"})

        # --- 步骤 3：加密消息 POST → 解密 → 解析 ---
        encrypt = _wecom_echo_plain(token, corp_id, aes_key, "testuser", "你好，联调测试")
        ts2, nonce2 = str(int(time.time())), "xyz789"
        sig2 = _sha1_signature(token, ts2, nonce2, encrypt)
        body_xml = f"<xml><Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>"
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/?msg_signature={sig2}&timestamp={ts2}&nonce={nonce2}",
            data=body_xml.encode("utf-8"),
            headers={"Content-Type": "text/xml"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                code = r.status
            ok = bool(received) and received[0].text == "你好，联调测试"
            result["steps"].append({
                "step": "加密消息(POST)", "ok": ok and code == 200,
                "detail": (f"HTTP {code}，解出文本 {received[0].text!r}" if received
                           else f"HTTP {code}，但未解析出消息")})
        except Exception as e:  # noqa: BLE001
            result["steps"].append({"step": "加密消息(POST)", "ok": False,
                                    "detail": f"失败: {e}"})
    finally:
        httpd.shutdown()
        httpd.server_close()

    result["ok"] = all(s["ok"] for s in result["steps"]) and bool(result["steps"])
    return result


def loopback_feishu(cfg: ChannelConfig) -> Dict:
    """飞书回环：URL 验证 + 签名消息事件。"""
    from http.server import HTTPServer
    import threading

    result = {"channel": "feishu", "steps": [], "ok": False}
    received = []

    def handler(inbound):
        received.append(inbound)

    if not cfg.feishu_app_secret:
        result["steps"].append({"step": "前置", "ok": False,
                                "detail": "缺 FEISHU_APP_SECRET，无法验签回环"})
        return result

    port = _free_port()
    ch = FeishuChannel(cfg)
    ch.port = port
    ch.app_secret = cfg.feishu_app_secret
    ch.verification_token = cfg.feishu_verification_token

    httpd = HTTPServer(("127.0.0.1", port), ch._make_handler(handler))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.2)

    try:
        # --- 步骤 1：URL 验证 ---
        payload = {"type": "url_verification", "challenge": "chal-123"}
        ts = str(int(time.time()))
        sig = hmac.new((ts + "\n" + cfg.feishu_app_secret).encode(),
                        digestmod=hashlib.sha256).hexdigest()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "X-Lark-Signature-Timestamp": ts,
                     "X-Lark-Signature": sig})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                body = json.loads(r.read())
                ok = body.get("challenge") == "chal-123"
                result["steps"].append({"step": "URL 验证", "ok": ok,
                                        "detail": f"challenge 回传 {body.get('challenge')!r}"})
        except Exception as e:  # noqa: BLE001
            result["steps"].append({"step": "URL 验证", "ok": False, "detail": f"失败: {e}"})

        # --- 步骤 2：错误签名必须被拒 ---
        bad = urllib.request.Request(
            f"http://127.0.0.1:{port}/",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "X-Lark-Signature-Timestamp": ts,
                     "X-Lark-Signature": "deadbeef"})
        try:
            with urllib.request.urlopen(bad, timeout=5) as r:
                code = r.status
            result["steps"].append({"step": "错误签名应被拒", "ok": False,
                                    "detail": f"竟然返回 HTTP {code}（应为 403）"})
        except urllib.error.HTTPError as e:
            result["steps"].append({"step": "错误签名应被拒", "ok": e.code == 403,
                                    "detail": f"HTTP {e.code} ✓"})

        # --- 步骤 3：签名消息事件 ---
        text = "你好，联调测试"
        content = urllib.parse.quote(json.dumps({"text": text}))
        event = {
            "schema": "2.0", "type": "event_callback",
            "event": {
                "message": {"message_id": "om_1", "message_type": "text",
                            "content": content},
                "sender": {"sender_id": {"open_id": "ou_1"}},
            },
        }
        ts3 = str(int(time.time()))
        sig3 = hmac.new((ts3 + "\n" + cfg.feishu_app_secret).encode(),
                         digestmod=hashlib.sha256).hexdigest()
        req3 = urllib.request.Request(
            f"http://127.0.0.1:{port}/",
            data=json.dumps(event).encode(),
            headers={"Content-Type": "application/json",
                     "X-Lark-Signature-Timestamp": ts3,
                     "X-Lark-Signature": sig3})
        try:
            with urllib.request.urlopen(req3, timeout=5) as r:
                code = r.status
            ok = bool(received) and received[0].text == text
            result["steps"].append({
                "step": "签名消息事件", "ok": ok and code == 200,
                "detail": (f"HTTP {code}，解出文本 {received[0].text!r}" if received
                           else f"HTTP {code}，但未解析出消息")})
        except Exception as e:  # noqa: BLE001
            result["steps"].append({"step": "签名消息事件", "ok": False, "detail": f"失败: {e}"})
    finally:
        httpd.shutdown()
        httpd.server_close()

    result["ok"] = all(s["ok"] for s in result["steps"]) and bool(result["steps"])
    return result


# ---------------------------------------------------------------- 真连探测
def probe_wecom(cfg: ChannelConfig) -> Dict:
    """真连企业微信取 access_token（唯一能证明凭据有效的接口）。"""
    url = (f"https://qyapi.weixin.qq.com/cgi-bin/gettoken"
           f"?corpid={cfg.wecom_corp_id}&corpsecret={cfg.wecom_secret}")
    try:
        with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310
            data = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"请求失败: {e}"}
    if data.get("errcode") == 0:
        return {"ok": True, "detail": "凭据有效，已取到 access_token",
                "expires_in": data.get("expires_in")}
    return {"ok": False, "detail": f"平台返回 errcode={data.get('errcode')} "
                                   f"errmsg={data.get('errmsg')}",
            "hint": _wecom_err_hint(data.get("errcode"))}


def probe_feishu(cfg: ChannelConfig) -> Dict:
    """真连飞书取 tenant_access_token。"""
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    req = urllib.request.Request(
        url,
        data=json.dumps({"app_id": cfg.feishu_app_id,
                         "app_secret": cfg.feishu_app_secret}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            data = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"请求失败: {e}"}
    if data.get("code") == 0:
        return {"ok": True, "detail": "凭据有效，已取到 tenant_access_token",
                "expires_in": data.get("expire")}
    return {"ok": False, "detail": f"平台返回 code={data.get('code')} "
                                   f"msg={data.get('msg')}"}


def _wecom_err_hint(code) -> str:
    return {
        40001: "corpsecret 不正确——检查 WECHAT_SECRET 是否为该应用的 Secret",
        40013: "corpid 不正确——检查 WECHAT_CORP_ID",
        40001.1: "corpsecret 不正确",
        41001: "缺少 access_token——先取 token",
        60011: "无权限——确认应用已启用并有发送消息权限",
        81013: "userid 不在企业通讯录—— touser 要填 UserID 不是姓名",
    }.get(code, "对照企业微信官方错误码文档排查")


# ---------------------------------------------------------------- 审批推送闭环
def check_approval_push_loop(db_path: str, channel: str) -> Dict:
    """验证「审批发起 → CLI 决策 → 状态落库」闭环（渠道推送的前置条件）。

    真实推送还差最后一步（平台侧网络与权限），但审批中心这层可以完全本地验证——
    渠道只是把决策结果"读出来发出去"，读不出来才是真问题。
    """
    from approval import ApprovalCenter
    ac = ApprovalCenter(db_path=db_path)
    try:
        aid = ac.request("scan_repo", "https://example.invalid/repo",
                         task_id="t-doctor", channel=channel, user_id="u-doctor")
        st1 = ac.status(aid)
        ok1 = st1 == "pending"
        ok2 = ac.decide(aid, "approve", by="doctor")
        st2 = ac.status(aid)
        ac.close()
        return {
            "ok": ok1 and ok2 and st2 == "approved",
            "approval_id": aid,
            "detail": f"发起后状态={st1}（应 pending），"
                      f"决策后状态={st2}（应 approved）",
        }
    except Exception as e:  # noqa: BLE001
        ac.close()
        return {"ok": False, "detail": f"异常: {e}"}


# ---------------------------------------------------------------- 主流程
def run_doctor(channel: str, probe: bool = False,
               db_path: str = "data/task_registry.db") -> Dict:
    cfg = ChannelConfig()
    # 从环境变量灌入渠道配置（与 config.load_config 同一套变量名）
    cfg.wecom_token = os.getenv("WECHAT_TOKEN", "")
    cfg.wecom_aes_key = os.getenv("WECHAT_AES_KEY", "")
    cfg.wecom_corp_id = os.getenv("WECHAT_CORP_ID", "")
    cfg.wecom_agent_id = os.getenv("WECHAT_AGENT_ID", "")
    cfg.wecom_secret = os.getenv("WECHAT_SECRET", "")
    cfg.feishu_app_id = os.getenv("FEISHU_APP_ID", "")
    cfg.feishu_app_secret = os.getenv("FEISHU_APP_SECRET", "")
    cfg.feishu_verification_token = os.getenv("FEISHU_VERIFICATION_TOKEN", "")
    cfg.feishu_encrypt_key = os.getenv("FEISHU_ENCRYPT_KEY", "")

    out = {
        "channel": channel,
        "credentials": check_credentials(channel),
        "loopback": (loopback_wecom(cfg) if channel == "wecom" else loopback_feishu(cfg)),
        "approval_loop": check_approval_push_loop(db_path, channel),
    }
    if probe:
        out["probe"] = (probe_wecom(cfg) if channel == "wecom" else probe_feishu(cfg))
    out["ready"] = (
        out["loopback"].get("ok")
        and out["approval_loop"].get("ok")
        and (not probe or out.get("probe", {}).get("ok"))
    )
    return out


def _print_human(res: Dict) -> None:
    ch = res["channel"]
    print(f"\n=== 渠道联调自检：{ch} ===\n")

    cred = res["credentials"]
    print("[1] 凭据自检")
    for it in cred["items"]:
        mark = "✓" if it["ok"] else ("○" if not it["required"] else "✗")
        print(f"    {mark} {it['name']:28s} {it['detail']}  ({it['desc']})")
    if cred["ready"]:
        print("    → 凭据齐备")
    else:
        print(f"    → 缺必填项：{', '.join(cred['missing_required'])}")

    print("\n[2] 本地回环（验签 → 解密 → 解析）")
    for s in res["loopback"].get("steps", []):
        mark = "✓" if s["ok"] else "✗"
        print(f"    {mark} {s['step']:20s} {s['detail']}")

    print("\n[3] 审批推送闭环（审批中心本地闭环）")
    ap = res["approval_loop"]
    mark = "✓" if ap["ok"] else "✗"
    print(f"    {mark} {ap['detail']}")

    if "probe" in res:
        print("\n[4] 平台连通性探测（真实凭据）")
        p = res["probe"]
        mark = "✓" if p["ok"] else "✗"
        print(f"    {mark} {p['detail']}")
        if not p["ok"] and p.get("hint"):
            print(f"      提示: {p['hint']}")

    print("\n" + "=" * 46)
    if res["ready"]:
        print("结论：代码侧链路全通。若真实收发仍失败，问题在平台后台配置或网络。")
    else:
        print("结论：代码侧存在问题，请按上面标 ✗ 的项排查。")
    print("=" * 46)


def main():
    ap = argparse.ArgumentParser(description="企微/飞书渠道联调自检工具")
    ap.add_argument("--channel", choices=["wecom", "feishu"], required=True)
    ap.add_argument("--probe", action="store_true",
                    help="额外真连平台官方接口（需真实凭据，会发出网络请求）")
    ap.add_argument("--db-path", default="data/task_registry.db")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args()

    res = run_doctor(args.channel, probe=args.probe, db_path=args.db_path)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        _print_human(res)
    return 0 if res["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
