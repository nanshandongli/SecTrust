"""多模态 provider 测试（models/multimodal.py）：Mock 离线、路由降级、HTTP 路径（假 urlopen）。"""

import base64
import io
import json
import os
import sys
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.multimodal import (MEDIA_KINDS, MediaRouter, MediaResult,  # noqa: E402
                               MockMediaProvider,
                               OpenAICompatibleMediaProvider)


class TestMockMediaProvider(unittest.TestCase):
    def test_all_kinds_produce_fake_bytes(self):
        p = MockMediaProvider()
        for kind in MEDIA_KINDS:
            r = p.generate(kind, "一只猫")
            self.assertIsInstance(r, MediaResult)
            self.assertEqual(r.kind, kind)
            self.assertTrue(r.data)
            self.assertTrue(r.meta["fake"])       # 明确标注是假字节
            self.assertEqual(r.prompt, "一只猫")

    def test_deterministic(self):
        p = MockMediaProvider()
        self.assertEqual(p.generate("image", "x").data,
                         p.generate("image", "x").data)

    def test_bad_kind_rejected(self):
        with self.assertRaises(ValueError):
            MockMediaProvider().generate("hologram", "x")


class TestMediaRouter(unittest.TestCase):
    def test_no_providers_falls_back_to_mock(self):
        r = MediaRouter().generate("image", "测试")
        self.assertEqual(r.provider, "mock")
        self.assertTrue(r.meta["fake"])

    def test_unconfigured_provider_degrades_to_mock(self):
        p = OpenAICompatibleMediaProvider(api_key="")  # 未配置
        r = MediaRouter([p]).generate("image", "测试", model="gpt-image-1")
        self.assertEqual(r.provider, "mock")

    def test_configured_provider_routes_by_prefix(self):
        p = OpenAICompatibleMediaProvider(api_key="k")
        router = MediaRouter([p])
        self.assertIs(router.route("gpt-image-1"), p)
        self.assertIs(router.route("unknown-model"), router.fallback)


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestOpenAICompatibleMedia(unittest.TestCase):
    def setUp(self):
        self.p = OpenAICompatibleMediaProvider(api_key="test-key")

    def _patch(self, payload):
        """把 urlopen 换成返回 payload 的假函数（JSON dict 或 bytes）。"""
        orig = urllib.request.urlopen

        def fake(req, timeout=None):
            body = (json.dumps(payload).encode() if isinstance(payload, dict)
                    else payload)
            return _FakeResponse(body)
        urllib.request.urlopen = fake
        self.addCleanup(setattr, urllib.request, "urlopen", orig)

    def test_image_b64_json(self):
        self._patch({"data": [{"b64_json": base64.b64encode(b"IMG").decode()}]})
        r = self.p.generate("image", "海报")
        self.assertEqual(r.data, b"IMG")
        self.assertEqual(r.kind, "image")
        self.assertFalse(r.meta.get("fake", False))

    def test_image_url_fallback(self):
        self._patch({"data": [{"url": "https://cdn.example/x.png"}]})
        r = self.p.generate("image", "海报")
        self.assertEqual(r.url, "https://cdn.example/x.png")
        self.assertIsNone(r.data)

    def test_audio_binary(self):
        self._patch(b"MP3DATA")
        r = self.p.generate("audio", "朗读这段", model="tts-1")
        self.assertEqual(r.data, b"MP3DATA")
        self.assertEqual(r.mime, "audio/mpeg")

    def test_video_requires_explicit_endpoint(self):
        # 未配置 video 端点 -> 明确报错，不静默造假
        with self.assertRaises(RuntimeError):
            self.p.generate("video", "一个镜头")

    def test_video_with_override(self):
        self._patch({"data": [{"url": "https://cdn.example/v.mp4"}]})
        p = OpenAICompatibleMediaProvider(
            api_key="k", endpoint_overrides={"video": "/videos/generations"})
        r = p.generate("video", "一个镜头")
        self.assertEqual(r.url, "https://cdn.example/v.mp4")

    def test_missing_key_raises_clearly(self):
        p = OpenAICompatibleMediaProvider(api_key="")
        with self.assertRaises(RuntimeError):
            p.generate("image", "x")


if __name__ == "__main__":
    unittest.main()
