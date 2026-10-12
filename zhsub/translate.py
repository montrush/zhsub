"""翻译：ollama 原生接口 + 批翻译/行号对齐/单行降级 + 译名表注入。

策略平移自实战验证的 AutoSubv2 补丁（v3/__init__.py 的 __process_batch 系列）与
openai_translate.py 的 prompt，改进点：批次内每行带编号，按编号对齐，
不再依赖"按换行拆分数组"这种对空行敏感的做法。

端点：默认 ollama 原生协议；translate.gpu 可配 GPU 加速端点（ollama 或
OpenAI 兼容），启用后优先走 GPU，失败自动回退 base_url。
"""

import json
import os
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

REPLY_RE = re.compile(r"^\s*(\d+)\s*[|｜:：.、)）]\s?(.*)$")

THINK_RE = re.compile(r"<think>.*?</think>", re.S)
HAS_CJK = re.compile(r"[一-鿿]")
NUM_PREFIX_RE = re.compile(r"^\s*\d+\s*[|｜:：.、)）]?\s*")


def strip_think(reply: str) -> str:
    """剥残留思考块（think=false 生效时不出现；OpenAI 兼容端点/旧版 ollama 可能漏）。"""
    return THINK_RE.sub("", reply).replace("</think>", "").strip()


def positional_align(reply: str, n: int):
    """位置对齐修复（aisub 0917/0922 补丁口径移植）：编号解析失败时的兜底。

    剥无 CJK 的首尾说明行 → 尝试按"编号."拆回被合并的行 → 超长贪心合并相邻最短对
    → 不足补空。返回等长译文列表；完全不可救药时返回 None（交单行降级）。
    """
    lines = [NUM_PREFIX_RE.sub("", x).strip() for x in reply.split("\n") if x.strip()]
    while lines and not HAS_CJK.search(lines[0]):
        lines.pop(0)
    while lines and not HAS_CJK.search(lines[-1]):
        lines.pop()
    # 中文前导说明行（"好的，以下是翻译："类）：以冒号结尾且不含句号正文特征
    while len(lines) > 1 and lines[0].endswith(("：", ":")):
        lines.pop(0)
    if not lines:
        return None
    expanded = []
    for ln in lines:
        parts = re.split(r"(?<=。|！|？|，|;|；)\s*(?=\d+[|｜.、)])", ln)
        got = [NUM_PREFIX_RE.sub("", p).strip() for p in parts]
        got = [p for p in got if p]
        expanded.extend(got if len(got) > 1 else [ln])
    lines = expanded
    while len(lines) > n:
        i = min(range(len(lines) - 1), key=lambda j: len(lines[j]) + len(lines[j + 1]))
        merged = lines[i] + lines[i + 1]
        if len(merged) > 180:
            break
        lines[i:i + 2] = [merged]
    while len(lines) < n:
        lines.append("")
    return lines if len(lines) == n else None


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


def _post_json(url: str, payload: dict, timeout: int, max_retries: int, headers=None) -> dict:
    """POST JSON 并指数退避重试，返回解析后的响应。"""
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=timeout, headers=headers)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001 - 网络与服务端错误统一退避重试
            last_err = e
            if attempt < max_retries:
                time.sleep(2 ** attempt + random.uniform(0.1, 0.9))
    raise RuntimeError(f"{url} 调用失败（已重试 {max_retries} 次）: {last_err}")


class Ollama:
    def __init__(self, base_url: str, model: str, num_ctx: int = 4096,
                 temperature: float = 0.2, top_p: float = 0.9,
                 request_timeout: int = 180, max_retries: int = 3,
                 think: bool = None, num_gpu: int = None, api_key: str = "",
                 num_predict: int = None):
        self.base = base_url.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = request_timeout
        self.max_retries = max_retries
        self.think = think  # None=跟随模型默认；False=关闭 qwen3 思考（ollama ≥0.9）
        self.num_gpu = num_gpu  # GPU 卸载层数；None=服务端自动
        self.num_predict = num_predict  # 生成 token 上限；None=不设（防跑飞可设 2048）
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else None

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
        if self.think is not None:
            payload["think"] = self.think
        if self.num_predict is not None:
            payload["options"]["num_predict"] = int(self.num_predict)
        if self.num_gpu is not None:
            payload["options"]["num_gpu"] = int(self.num_gpu)
        data = _post_json(f"{self.base}/api/chat", payload, self.timeout,
                          self.max_retries, self.headers)
        msg = data.get("message") or {}
        content = msg.get("content") or ""
        if not content.strip():
            # 偶发空回复兜底：内容被塞进 thinking/reasoning 字段时剥标签取正文
            for k in ("thinking", "reasoning"):
                raw = msg.get(k) or ""
                if raw.strip():
                    content = THINK_RE.sub("", raw).replace("</think>", "").strip()
                    break
        return content


class OpenAICompat:
    """OpenAI 兼容端点（/v1/chat/completions）：vLLM、llama.cpp server、ollama /v1 等。

    接口与 Ollama 对齐（chat / chat_messages）；num_ctx / num_gpu / think 是
    ollama 概念，OpenAI 协议无对应字段，收下但忽略。
    """

    def __init__(self, base_url: str, model: str, num_ctx: int = None,
                 temperature: float = 0.2, top_p: float = 0.9,
                 request_timeout: int = 180, max_retries: int = 3,
                 think: bool = None, num_gpu: int = None, api_key: str = "",
                 num_predict: int = None):
        self.base = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = request_timeout
        self.max_retries = max_retries
        self.num_predict = num_predict  # OpenAI 协议的 max_tokens
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else None

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
            "temperature": self.temperature,
            "top_p": self.top_p,
        }
        if self.num_predict is not None:
            payload["max_tokens"] = int(self.num_predict)
        data = _post_json(f"{self.base}/v1/chat/completions", payload, self.timeout,
                          self.max_retries, self.headers)
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"OpenAI 兼容端点返回异常: {e}（响应片段: {str(data)[:200]}）")


class GpuFallback:
    """GPU 端点优先、失败自动回退基础端点的聊天客户端。

    GPU 连续失败后进入冷却期（默认 10 分钟），期间直接走基础端点，
    避免每批字幕都先吃一遍超时；冷却结束后自动重试 GPU。
    """

    def __init__(self, primary, fallback, log=print, cooldown_sec: int = 600):
        self.primary = primary
        self.fallback = fallback
        self.log = log
        self.cooldown_sec = cooldown_sec
        self.stats = {"gpu_ok": 0, "fallback": 0}
        self._down_until = 0.0

    def chat(self, system: str, user: str) -> str:
        return self.chat_messages([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])

    def chat_messages(self, messages) -> str:
        if time.time() >= self._down_until:
            try:
                out = self.primary.chat_messages(messages)
                self.stats["gpu_ok"] += 1
                return out
            except Exception as e:  # noqa: BLE001
                self._down_until = time.time() + self.cooldown_sec
                self.log(f"[gpu] GPU 端点失败，冷却 {self.cooldown_sec // 60} 分钟，"
                         f"期间走基础端点: {e}")
        self.stats["fallback"] += 1
        return self.fallback.chat_messages(messages)


def endpoint_client(d: dict, num_ctx: int, temperature: float = 0.2,
                    top_p: float = 0.9, request_timeout: int = 180,
                    max_retries: int = 3, think: bool = None,
                    num_predict: int = None):
    """按端点描述构造单端点客户端。d: {base_url, model, provider, api_key, num_gpu}"""
    cls = OpenAICompat if d.get("provider") == "openai" else Ollama
    return cls(base_url=d["base_url"], model=d["model"], num_ctx=num_ctx,
               temperature=temperature, top_p=top_p,
               request_timeout=request_timeout, max_retries=max_retries,
               think=think, api_key=d.get("api_key") or "",
               num_gpu=d.get("num_gpu"), num_predict=num_predict)


def build_client(t: dict, num_ctx: int, log=print):
    """按 translate 配置构造聊天客户端；gpu.enabled 时 GPU 优先 + base_url 回退。

    think 默认 False：qwen3 是混合思考模型，ollama 请求不显式关闭时默认开思考，
    单行字幕也能生成 ~300 token（1012 实测 316 tok/行 vs 正常 ~25 tok/行，
    整集慢 5-10×）。ollama 端点显式传 think:false 即可；OpenAI 兼容端点忽略此参。
    """
    kw = dict(temperature=t.get("temperature", 0.2), top_p=t.get("top_p", 0.9),
              request_timeout=t.get("request_timeout", 180),
              max_retries=t.get("max_retries", 3), think=t.get("think", False),
              num_predict=t.get("num_predict"))
    base = endpoint_client({"base_url": t["base_url"], "model": t["model"],
                            "provider": t.get("provider", "ollama"),
                            "api_key": t.get("api_key") or "",
                            "num_gpu": t.get("num_gpu")}, num_ctx, **kw)
    g = t.get("gpu") or {}
    if g.get("enabled") and g.get("base_url"):
        gpu = endpoint_client({"base_url": g["base_url"],
                               "model": g.get("model") or t["model"],
                               "provider": g.get("provider", "ollama"),
                               "api_key": g.get("api_key") or "",
                               "num_gpu": g.get("num_gpu")}, num_ctx, **kw)
        return GpuFallback(gpu, base, log=log)
    return base


def parse_numbered_reply(reply: str, n: int):
    """把回复解析成 {行号0基: 译文}。容忍编号缺失/多余行。"""
    result = {}
    for raw in strip_think(reply).replace("\r\n", "\n").split("\n"):
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
    def __init__(self, ollama, glossary: dict = None,
                 batch_size: int = 10, context_window: int = 2, log=print,
                 deadline: float = None, batch_retries: int = 3):
        self.ai = ollama
        self.system = build_system_prompt(glossary)
        self.batch_size = max(1, batch_size)
        self.ctx_win = max(0, context_window)
        self.log = log
        self.deadline = deadline  # 单集预算（time.time() 时刻），单行降级时也检查
        self.batch_retries = max(1, batch_retries)  # 批次总尝试次数（含首次）
        self.stats = {"total": 0, "batch_ok": 0, "batch_fail": 0, "single_fallback": 0,
                      "repair_ok": 0, "retry_ok": 0}

    def _chat_once(self, content: str) -> str:
        return self.ai.chat(self.system, content)

    def _translate_chunk(self, lines, lo: int, texts) -> bool:
        """翻一个 chunk：编号对齐 → 位置修复兜底 → 重试，全部失败才单行降级。"""
        # 1012 修 bug：原实现 n=len(lines) 会把"剩余全部行"塞进一个请求，
        # num_ctx=4096 下必截断 → 对不齐 → 全量单行降级（慢 10×+ 的元凶）。
        n = min(self.batch_size, len(lines) - lo)
        before = lines[max(0, lo - self.ctx_win):lo] if self.ctx_win else []
        after = lines[lo + n:lo + n + self.ctx_win] if self.ctx_win else []
        user = _format_batch(lines[lo:lo + n])
        ctx = _format_context(before, after)
        content = f"翻译上下文：\n{ctx}\n\n需要翻译的内容：\n{user}" if ctx else f"请翻译：\n{user}"
        best = {}  # 历次尝试里对齐上最多的那份
        for attempt in range(1, self.batch_retries + 1):
            reply = self._chat_once(content)
            aligned = parse_numbered_reply(reply, n)
            if len(aligned) == n:
                if attempt > 1:
                    self.stats["retry_ok"] += 1
                best = aligned
                break
            # 编号解析不齐：位置对齐修复（剥说明行/拆合并行/贪心合并）。
            # 位置修复只在"完整且无空洞"时整批采纳——部分位置结果不可信，
            # 只有编号解析的部分结果才允许留作垫底（编号=模型自己声明的行号）。
            repaired = positional_align(reply, n)
            if repaired is not None and all(repaired):
                best = {i: t for i, t in enumerate(repaired)}
                self.stats["repair_ok"] += 1
                break
            if len(aligned) > len(best):
                best = aligned
            if attempt < self.batch_retries:
                time.sleep(2 * attempt)
        if len(best) == n:
            for i, t in best.items():
                texts[lo + i] = t
            return True
        # 部分对齐：先落袋为安，缺的行走单行（与历史行为一致）
        for i, t in best.items():
            texts[lo + i] = t
        for i in range(n):
            if i not in best:
                texts[lo + i] = self._single(lines[lo + i])
        return False

    def _single(self, line: str) -> str:
        if self.deadline and time.time() > self.deadline:
            raise BudgetExceeded("超过单集预算（单行降级阶段）")
        self.stats["single_fallback"] += 1
        reply = self._chat_once(f"请翻译：\n1|{line}")
        aligned = parse_numbered_reply(reply, 1)
        if aligned:
            return next(iter(aligned.values()))
        repaired = positional_align(reply, 1)
        if repaired:
            return repaired[0] or line
        lines = strip_think(reply).strip().splitlines()
        # 空回复/无编号：剥出含中文的首行才用，否则保留原文（宁缺毋错）
        for ln in lines:
            if ln.strip() and HAS_CJK.search(ln):
                return ln.strip()
        return line

    def translate_lines(self, lines, progress=None, ckpt_path=None, ckpt_meta=None):
        """翻译一批字幕文本行，返回等长译文列表。

        批次失败：位置修复+重试后才降级单行；ckpt_path 给定时按批落盘断点续跑
        （中断/预算杀死后重跑不浪费已翻译行）。
        """
        texts = [""] * len(lines)
        lo = 0
        if ckpt_path and os.path.exists(ckpt_path):
            try:
                ck = json.load(open(ckpt_path, encoding="utf-8"))
                if ck.get("n") == len(lines) and ck.get("meta") == ckpt_meta \
                        and len(ck.get("done", [])) <= len(lines):
                    done = ck["done"]
                    texts[:len(done)] = done
                    lo = (len(done) // self.batch_size) * self.batch_size
                    texts = texts[:lo] + [""] * (len(lines) - lo)
                    if lo:
                        self.log(f"  断点续跑: 已有 {lo} 行")
            except Exception:  # noqa: BLE001 - 坏 ckpt 当不存在
                pass
        while lo < len(lines):
            n = min(self.batch_size, len(lines) - lo)
            self.stats["total"] += n
            try:
                ok = self._translate_chunk(lines, lo, texts)
            except BudgetExceeded:
                raise
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
            if ckpt_path:
                tmp = ckpt_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"n": len(lines), "meta": ckpt_meta, "done": texts[:lo]},
                              f, ensure_ascii=False)
                os.replace(tmp, ckpt_path)
        return texts


def load_glossary(glossary_dir: str, series: str):
    """兼容旧签名，见 glossary.load_glossary。"""
    from .glossary import load_glossary as _load
    return _load(glossary_dir, series)
