"""
voice.py —— 语音输出（把文本回复变成语音文件/语音消息的最小版）。

定位（对照 Grok Bot 的语音交互）：**框架级可用，音质取决于外部 TTS API**——
没有配 key 时走 MockMediaProvider 出**假音频字节**（meta.fake=True，明示
不是真语音），链路照样跑通；配了 OpenAI 兼容 key 就是真 TTS。

诚实边界（写死在返回结构里，不装）：
- 渠道层 OutboundMessage 目前**只有文本字段**，没有语音消息类型。
  因此 deliver() 的行为是：
    1. 渠道对象若有 send_audio/send_file 方法（duck-typing）→ 直接发语音；
    2. 否则 → 发文本消息，正文附语音文件路径（人能拿到文件，但不谎称已发语音）。
- MediaResult.data 为空且只有 url 时（服务端只回链接）→ 原样返回 url，
  不下载不落盘（本地框架不擅自拉远端资源）。

纪律：
- 合成走 models.multimodal.MediaRouter（与图像/视频同一套路由，不另起炉灶）；
- 文件落盘用原子写（tmp + replace），缺目录先建；
- speak() 失败返回 {"ok": False, "error": ...}，不抛栈给调用方刷屏。

运行：
    python voice.py --text "大家好，这是语音测试"
    python voice.py --text "..." --out /tmp/voice.mp3 --deliver
"""

from __future__ import annotations

import argparse
import os
import tempfile
import time

DEFAULT_OUT_DIR = os.path.join("data", "voice")


def _default_media(model: str = None):
    """按环境变量构造媒体路由；缺 key 时路由落 Mock（假字节，明示）。"""
    try:
        from config import load_config
        from models.multimodal import (MediaRouter,
                                        OpenAICompatibleMediaProvider)
        md = load_config().model
        return MediaRouter(
            [OpenAICompatibleMediaProvider(md.openai_api_key, md.openai_base_url)])
    except Exception:
        from models.multimodal import MediaRouter
        return MediaRouter()


class VoiceSpeaker:
    """文本 → 语音。media / out_dir 可注入（测试替身）。"""

    def __init__(self, media=None, out_dir: str = None, model: str = None,
                 voice: str = "alloy", fmt: str = "mp3"):
        self.media = media if media is not None else _default_media(model)
        self.out_dir = out_dir or os.environ.get("SECTRUST_VOICE_DIR",
                                                 DEFAULT_OUT_DIR)
        self.model = model
        self.voice = voice
        self.fmt = fmt

    # ---- 合成 + 落盘 ----
    def speak(self, text: str, name: str = None) -> dict:
        if not text or not text.strip():
            return {"ok": False, "error": "文本为空，无可合成的语音"}
        try:
            res = self.media.generate("audio", text, model=self.model,
                                      voice=self.voice, fmt=self.fmt)
        except Exception as e:
            return {"ok": False, "error": "TTS 合成失败: %s" % e}
        out = {
            "ok": True,
            "provider": getattr(res, "provider", ""),
            "model": getattr(res, "model", ""),
            "mime": getattr(res, "mime", ""),
            "fake": bool((getattr(res, "meta", None) or {}).get("fake")),
            "chars": len(text),
        }
        if getattr(res, "data", None):
            path = self._save(res.data, name)
            out["path"] = path
            out["bytes"] = os.path.getsize(path)
        elif getattr(res, "url", None):
            # 只回链接的服务端：原样透出，不擅自下载
            out["url"] = res.url
            out["note"] = "服务端只返回 URL，未落盘（本地框架不擅自拉远端资源）"
        else:
            return {"ok": False, "error": "TTS 结果既无数据也无 URL"}
        return out

    def _save(self, data: bytes, name: str = None) -> str:
        os.makedirs(self.out_dir, exist_ok=True)
        fname = name or ("voice-%d.%s" % (int(time.time() * 1000), self.fmt))
        path = os.path.join(self.out_dir, fname)
        fd, tmp = tempfile.mkstemp(dir=self.out_dir, suffix=".part")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)                 # 原子落盘
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return path

    # ---- 交付给渠道（诚实分级，见模块 docstring）----
    def deliver(self, channel, user_id: str, text: str,
                name: str = None) -> dict:
        """合成语音并尝试经渠道发出。返回交付结果（含降级原因）。"""
        spoken = self.speak(text, name=name)
        if not spoken.get("ok"):
            return spoken
        sender = None
        for attr in ("send_audio", "send_file"):
            fn = getattr(channel, attr, None)
            if callable(fn):
                sender = (attr, fn)
                break
        if sender is not None:
            attr, fn = sender
            try:
                if attr == "send_audio":
                    fn(user_id, spoken.get("path") or spoken.get("url"),
                       text=text)
                else:
                    fn(user_id, spoken.get("path") or spoken.get("url"))
                spoken["delivered"] = "audio"
                return spoken
            except Exception as e:
                spoken["delivered"] = "text-fallback"
                spoken["deliver_error"] = str(e)
        else:
            spoken["delivered"] = "text-fallback"
            spoken["note"] = ("渠道无语音消息能力（OutboundMessage 仅文本），"
                              "已发文本+文件路径")
        # 文本兜底：把路径/URL 附在正文里
        target = spoken.get("path") or spoken.get("url") or ""
        try:
            from channels import OutboundMessage
            channel.send(OutboundMessage(
                channel=getattr(channel, "name", ""), user_id=user_id,
                text="%s\n[语音文件: %s]" % (text, target)))
        except Exception as e:
            spoken["deliver_error"] = str(e)
        return spoken


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="语音输出 CLI")
    ap.add_argument("--text", required=True)
    ap.add_argument("--out", default="", help="输出目录或文件名")
    ap.add_argument("--model", default="")
    ap.add_argument("--voice", default="alloy")
    args = ap.parse_args(argv)

    out_dir, name = DEFAULT_OUT_DIR, None
    if args.out:
        if os.path.isdir(args.out) or args.out.endswith(("/", "\\")):
            out_dir = args.out
        else:
            out_dir, name = os.path.split(args.out)
    speaker = VoiceSpeaker(out_dir=out_dir, model=args.model or None,
                           voice=args.voice)
    res = speaker.speak(args.text, name=name)
    for k, v in res.items():
        print("%s = %s" % (k, v))
    if not res.get("ok"):
        return 1
    if res.get("fake"):
        print("（注意：未配置真实 TTS key，以上是 Mock 假音频字节，不是真人声）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
