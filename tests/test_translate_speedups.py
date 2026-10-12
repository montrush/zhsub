"""1012 提速改造测试：think 剥离 / 位置对齐修复 / 批次重试 / 单行降级 / 断点续跑。"""

import json
import os

import pytest

import zhsub.translate as tr
from zhsub.translate import (SubTranslator, build_client, parse_numbered_reply,
                             positional_align, strip_think)


class FakeClient:
    """按脚本吐回复的假 LLM 客户端。replies: list 或 callable(lines, call_no)->str"""

    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def chat(self, system, user):
        self.calls.append(user)
        if callable(self.replies):
            return self.replies(len(self.calls), user)
        if isinstance(self.replies, str):
            return self.replies
        return self.replies[len(self.calls) - 1]


# ---- strip_think ----

def test_strip_think_full_block():
    assert strip_think("<think>嗯让我想想</think>\n1|是的主厨") == "1|是的主厨"

def test_strip_think_unclosed_tail():
    assert strip_think("思考中</think>2|拉姆齐主厨").endswith("2|拉姆齐主厨")

def test_parse_numbered_tolerates_think_residue_and_dot_prefix():
    r = parse_numbered_reply("<think>x</think>\n1. 是的主厨\n2、马上就好", 2)
    assert r == {0: "是的主厨", 1: "马上就好"}


# ---- positional_align ----

def test_positional_align_drops_preamble_and_pads():
    reply = "好的，以下是翻译：\n1. 是的主厨\n2. 马上就好"
    assert positional_align(reply, 3) == ["是的主厨", "马上就好", ""]

def test_positional_align_splits_merged_lines():
    reply = "1. 是的主厨。2. 马上就好。3. 收到，主厨。"
    assert positional_align(reply, 3) == ["是的主厨。", "马上就好。", "收到，主厨。"]

def test_positional_align_merges_overflow():
    reply = "1. 是的主厨\n2. 马上\n3. 就好\n4. 收到主厨"
    out = positional_align(reply, 3)
    assert out is not None and len(out) == 3 and out[0] == "是的主厨"

def test_positional_align_garbage_returns_none():
    assert positional_align("", 3) is None
    assert positional_align("no cjk at all here", 3) is None


# ---- SubTranslator 批次策略 ----

def _lines(n):
    return [f"line {i}" for i in range(n)]


def test_batch_ok_no_fallback():
    c = FakeClient("1|甲\n2|乙\n3|丙\n4|丁\n5|戊\n6|己\n7|庚\n8|辛\n9|壬\n10|癸")
    t = SubTranslator(c, batch_size=10, context_window=0, batch_retries=3)
    out = t.translate_lines(_lines(10))
    assert out == ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸"]
    assert t.stats["batch_ok"] == 10 and t.stats["single_fallback"] == 0


def test_unnumbered_reply_repaired_not_degraded():
    c = FakeClient("甲\n乙\n丙\n丁\n戊\n己\n庚\n辛\n壬\n癸")  # 无编号但行数齐
    t = SubTranslator(c, batch_size=10, context_window=0, batch_retries=3)
    out = t.translate_lines(_lines(10))
    assert out == ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸"]
    assert t.stats["repair_ok"] == 1 and t.stats["single_fallback"] == 0


def test_retry_then_success():
    replies = ["乱码一轮", "1|甲\n2|乙"]  # 首轮对不齐，二轮成功
    c = FakeClient(replies)
    t = SubTranslator(c, batch_size=2, context_window=0, batch_retries=3)
    out = t.translate_lines(_lines(2))
    assert out == ["甲", "乙"] and t.stats["retry_ok"] == 1 and len(c.calls) == 2


def test_all_fail_degrades_to_single():
    c = FakeClient(lambda n, u: "totally broken response")  # 永远救不回（无中文）
    t = SubTranslator(c, batch_size=2, context_window=0, batch_retries=2)
    out = t.translate_lines(_lines(2))
    assert t.stats["single_fallback"] == 2  # 2行各自单行兜底（回复无CJK→保留原文）
    assert out == ["line 0", "line 1"]


def test_budget_exceeded_propagates_from_single():
    class DeadlineClient(FakeClient):
        def chat(self, system, user):
            raise RuntimeError("endpoint down")
    c = DeadlineClient(None)
    t = SubTranslator(c, batch_size=2, context_window=0, batch_retries=1, deadline=1.0)
    with pytest.raises(tr.BudgetExceeded):
        t.translate_lines(_lines(2))


# ---- 断点续跑 ----

def test_ckpt_resume_skips_done_batches(tmp_path):
    state = {"n": 0}

    def replies(call_no, user):
        state["n"] += 1  # 全局计数（每个 FakeClient 的 call_no 独立，会归零）
        return "1|译甲\n2|译乙"

    c = FakeClient(replies)
    t = SubTranslator(c, batch_size=2, context_window=0, batch_retries=1)
    lines = _lines(4)
    ck = tmp_path / "ck.json"
    out1 = t.translate_lines(lines, ckpt_path=str(ck), ckpt_meta="m1")
    assert out1 == ["译甲", "译乙"] * 2 and state["n"] == 2  # 无ckpt时2批

    # 手工截成半程ckpt(只留前2行)模拟中断
    ck.write_text(json.dumps({"n": 4, "meta": "m1", "done": ["译甲", "译乙"]},
                             ensure_ascii=False), encoding="utf-8")
    c2 = FakeClient(replies)
    t2 = SubTranslator(c2, batch_size=2, context_window=0, batch_retries=1)
    out2 = t2.translate_lines(lines, ckpt_path=str(ck), ckpt_meta="m1")
    assert out2 == ["译甲", "译乙"] * 2
    assert state["n"] == 3  # 只新调用1批(第3次)：前1批从ckpt恢复

    # meta 不匹配（媒体文件变了）→ 整集重翻
    c3 = FakeClient(replies)
    t3 = SubTranslator(c3, batch_size=2, context_window=0, batch_retries=1)
    t3.translate_lines(lines, ckpt_path=str(ck), ckpt_meta="m2")
    assert state["n"] == 5  # 又2批


def test_ckpt_written_batch_aligned(tmp_path):
    c = FakeClient(lambda n, u: "1|译甲\n2|译乙")
    t = SubTranslator(c, batch_size=2, context_window=0, batch_retries=1)
    ck = tmp_path / "ck.json"
    t.translate_lines(_lines(4), ckpt_path=str(ck), ckpt_meta="m")
    d = json.loads(ck.read_text(encoding="utf-8"))
    assert d["n"] == 4 and d["meta"] == "m" and d["done"] == ["译甲", "译乙"] * 2


# ---- build_client 默认关思考 ----

def test_build_client_default_think_false():
    cl = build_client({"base_url": "http://x:11434", "model": "qwen3-8b-translate"},
                      num_ctx=4096)
    assert cl.think is False


def test_build_client_think_explicit_null_kept():
    cl = build_client({"base_url": "http://x:11434", "model": "m", "think": None},
                      num_ctx=4096)
    assert cl.think is None  # 显式 null = 跟随模型默认（旧行为，向后兼容）


def test_ollama_empty_content_falls_back_to_reasoning(monkeypatch):
    cl = tr.Ollama("http://x:11434", "m", think=False)
    captured = {}

    def fake_post(url, payload, timeout, max_retries, headers=None):
        captured["payload"] = payload
        return {"message": {"content": "", "reasoning": "<think>r</think>1|是的主厨"}}

    monkeypatch.setattr(tr, "_post_json", fake_post)
    assert cl.chat("s", "u") == "1|是的主厨"
    assert captured["payload"]["think"] is False  # 默认显式关思考
