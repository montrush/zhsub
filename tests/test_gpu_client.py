"""GPU 端点配置与回退逻辑测试（不发真实网络请求）。"""

import zhsub.translate as tr
from zhsub.config import DEFAULTS
from zhsub.translate import (GpuFallback, Ollama, OpenAICompat, build_client,
                             endpoint_client)

BASE = {"provider": "ollama", "base_url": "http://base:11434", "model": "m-base"}
GPU = {"enabled": True, "provider": "ollama", "base_url": "http://gpu:11435",
       "model": "m-gpu", "api_key": "", "num_gpu": None}


def test_defaults_contain_gpu():
    g = DEFAULTS["translate"]["gpu"]
    assert g["enabled"] is False and g["provider"] == "ollama"


def test_build_client_disabled_returns_base():
    t = {**BASE, "gpu": {"enabled": False, "base_url": "http://x"}}
    c = build_client(t, num_ctx=4096)
    assert isinstance(c, Ollama)
    assert c.base == "http://base:11434" and c.model == "m-base"


def test_build_client_enabled_returns_fallback():
    c = build_client({**BASE, "gpu": dict(GPU)}, num_ctx=4096)
    assert isinstance(c, GpuFallback)
    assert c.primary.base == "http://gpu:11435" and c.primary.model == "m-gpu"
    assert c.fallback.base == "http://base:11434"


def test_gpu_model_defaults_to_base_model():
    c = build_client({**BASE, "gpu": {**GPU, "model": ""}}, num_ctx=1)
    assert c.primary.model == "m-base"


def test_gpu_fallback_on_error():
    calls = []

    class P:
        def chat_messages(self, msgs):
            raise RuntimeError("boom")

    class F:
        def chat_messages(self, msgs):
            calls.append("fb")
            return "fb"

    fb = GpuFallback(P(), F(), log=lambda *a: None, cooldown_sec=600)
    assert fb.chat("s", "u") == "fb"
    assert fb.stats["fallback"] == 1

    # 冷却期内即使 primary 已恢复也走 fallback，避免每批都吃超时
    class P2:
        def chat_messages(self, msgs):
            calls.append("primary")
            return "gpu"

    fb.primary = P2()
    assert fb.chat("s", "u") == "fb"
    assert "primary" not in calls


def test_gpu_recovers_after_cooldown():
    class P:
        def __init__(self):
            self.n = 0

        def chat_messages(self, msgs):
            self.n += 1
            return "gpu"

    class F:
        def chat_messages(self, msgs):
            raise AssertionError("冷却结束后不应再走 fallback")

    p = P()
    fb = GpuFallback(p, F(), log=lambda *a: None, cooldown_sec=1)
    fb._down_until = tr.time.time() - 0.01  # 冷却已过
    assert fb.chat("s", "u") == "gpu" and p.n == 1


def test_openai_client_payload_and_parse(monkeypatch):
    seen = {}

    def fake_post(url, payload, timeout, max_retries, headers=None):
        seen.update(url=url, payload=payload, headers=headers)
        return {"choices": [{"message": {"content": " hi "}}]}

    monkeypatch.setattr(tr, "_post_json", fake_post)
    c = OpenAICompat("http://gpu:8000/", "m", api_key="k1")
    assert c.chat("sys", "user") == " hi "  # 与 Ollama 一致返回原文，strip 交给下游
    assert seen["url"] == "http://gpu:8000/v1/chat/completions"
    assert seen["payload"]["model"] == "m"
    assert seen["payload"]["messages"][0]["content"] == "sys"
    assert seen["headers"] == {"Authorization": "Bearer k1"}


def test_ollama_num_gpu_and_key(monkeypatch):
    seen = {}

    def fake_post(url, payload, timeout, max_retries, headers=None):
        seen.update(url=url, payload=payload, headers=headers)
        return {"message": {"content": "ok"}}

    monkeypatch.setattr(tr, "_post_json", fake_post)
    Ollama("http://b:11434", "m", num_gpu=28, api_key="kk").chat("s", "u")
    assert seen["payload"]["options"]["num_gpu"] == 28
    assert seen["headers"] == {"Authorization": "Bearer kk"}

    Ollama("http://b:11434", "m").chat("s", "u")
    assert "num_gpu" not in seen["payload"]["options"]
    assert seen["headers"] is None


def test_endpoint_client_provider_select():
    assert isinstance(endpoint_client({"base_url": "http://x", "model": "m",
                                       "provider": "openai"}, 4096), OpenAICompat)
    assert isinstance(endpoint_client({"base_url": "http://x", "model": "m",
                                       "provider": "ollama"}, 4096), Ollama)
