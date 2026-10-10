"""翻译：ollama 原生接口 + 批翻译/行号对齐/单行降级 + 译名表注入。

策略平移自实战验证的 AutoSubv2 补丁（v3/__init__.py 的 __process_batch 系列）与
openai_translate.py 的 prompt，改进点：批次内每行带编号，按编号对齐，
不再依赖"按换行拆分数组"这种对空行敏感的做法。
"""

import random
import re
import time

import requests

SYSTEM_PROMPT = """您是一位专业字幕翻译专家，请严格遵循以下规则：
1. 将原文精准翻译为简体中文，保持原文本意
2. 使用自然的口语化表达，符合中文观影习惯
3. 结合上下文语境，人物称谓、专业术语、情感语气在上下文中保持连贯
4. 输入为带编号的字幕行（格式：编号|文本）。输出必须逐行对应：保持相同编号，每行输出一条译文，不要合并或拆分
5. 输出内容必须仅包括译文。不要输出任何开场白，解释说明或总结
/no_think"""

REPLY_RE = re.compile(r"^\s*(\d+)\s*[|｜:：]\s?(.*)$")


def build_system_prompt(glossary: dict = None) -> str:
    if not glossary:
        return SYSTEM_PROMPT
    terms = "\n".join(f"- {k} → {v}" for k, v in glossary.items())
    return SYSTEM_PROMPT + f"\n6. 术语表（必须按此翻译）：\n{terms}"


def _format_batch(lines, start_no: int = 1) -> str:
    return "\n".join(f"{i}|{t}" for i, t in enumerate(lines, start_no))


def _format_context(before, after) -> str:
    parts = []
    if before:
        parts.append("（上文）\n" + "\n".join(before))
    if after:
        parts.append("（下文）\n" + "\n".join(after))
    return "\n".join(parts)


class BudgetExceeded(RuntimeError):
    pass


class Ollama:
    def __init__(self, base_url: str, model: str, num_ctx: int = 4096,
                 temperature: float = 0.2, top_p: float = 0.9,
                 request_timeout: int = 180, max_retries: int = 3):
        self.base = base_url.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = request_timeout
        self.max_retries = max_retries

    def chat(self, system: str, user: str) -> str:
        return self.chat_messages([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])

    def chat_messages(self, messages) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            # num_ctx 限制 KV 上下文，防 ollama 默认大分配挤占 VRAM（0917 实证补丁）
            "options": {
                "num_ctx": self.num_ctx,
                "temperature": self.temperature,
                "top_p": self.top_p,
            },
        }
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                r = requests.post(f"{self.base}/api/chat", json=payload, timeout=self.timeout)
                r.raise_for_status()
                return r.json()["message"]["content"]
            except Exception as e:  # noqa: BLE001 - 网络与服务端错误统一退避重试
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt + random.uniform(0.1, 0.9))
        raise RuntimeError(f"ollama 调用失败（已重试 {self.max_retries} 次）: {last_err}")


def parse_numbered_reply(reply: str, n: int):
    """把回复解析成 {行号0基: 译文}。容忍编号缺失/多余行。"""
    result = {}
    for raw in reply.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = REPLY_RE.match(line)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < n and m.group(2).strip():
                result[idx] = m.group(2).strip()
        elif len(result) == n and n > 0:
            break
    return result


class SubTranslator:
    def __init__(self, ollama: Ollama, glossary: dict = None,
                 batch_size: int = 10, context_window: int = 2, log=print,
                 deadline: float = None):
        self.ai = ollama
        self.system = build_system_prompt(glossary)
        self.batch_size = max(1, batch_size)
        self.ctx_win = max(0, context_window)
        self.log = log
        self.deadline = deadline  # 单集预算（time.time() 时刻），单行降级时也检查
        self.stats = {"total": 0, "batch_ok": 0, "batch_fail": 0, "single_fallback": 0}

    def _chat_once(self, content: str) -> str:
        return self.ai.chat(self.system, content)

    def _translate_chunk(self, lines, lo: int, texts) -> bool:
        """翻一个 chunk；成功对齐则写入 texts 并返回 True。"""
        n = len(lines)
        before = lines[max(0, lo - self.ctx_win):lo] if self.ctx_win else []
        after = lines[lo + n:lo + n + self.ctx_win] if self.ctx_win else []
        user = _format_batch(lines[lo:lo + n])
        ctx = _format_context(before, after)
        content = f"翻译上下文：\n{ctx}\n\n需要翻译的内容：\n{user}" if ctx else f"请翻译：\n{user}"
        reply = self._chat_once(content)
        aligned = parse_numbered_reply(reply, n)
        if len(aligned) == n:
            for i, t in aligned.items():
                texts[lo + i] = t
            return True
        # 部分对齐：只补缺的行（单行降级）
        for i in range(n):
            if i not in aligned:
                texts[lo + i] = self._single(lines[lo + i])
        return len(aligned) == n

    def _single(self, line: str) -> str:
        if self.deadline and time.time() > self.deadline:
            raise BudgetExceeded(f"超过单集预算（单行降级阶段）")
        self.stats["single_fallback"] += 1
        reply = self._chat_once(f"请翻译：\n1|{line}")
        aligned = parse_numbered_reply(reply, 1)
        if aligned:
            return next(iter(aligned.values()))
        return reply.strip().splitlines()[0] if reply.strip() else line

    def translate_lines(self, lines, progress=None):
        """翻译一批字幕文本行，返回等长译文列表。批次失败自动降级单行。"""
        texts = [""] * len(lines)
        lo = 0
        while lo < len(lines):
            n = min(self.batch_size, len(lines) - lo)
            self.stats["total"] += n
            try:
                ok = self._translate_chunk(lines, lo, texts)
            except Exception as e:  # noqa: BLE001
                self.log(f"  批次翻译异常（{e}），降级单行")
                ok = False
            if ok:
                self.stats["batch_ok"] += n
            else:
                self.stats["batch_fail"] += n
                for i in range(lo, lo + n):
                    if not texts[i]:
                        texts[i] = self._single(lines[i])
            if progress:
                progress(min(lo + n, len(lines)), len(lines))
            lo += n
        return texts


def load_glossary(glossary_dir: str, series: str):
    """兼容旧签名，见 glossary.load_glossary。"""
    from .glossary import load_glossary as _load
    return _load(glossary_dir, series)
