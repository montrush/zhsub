"""翻译前资料收集：本地 qwen 模型 + 联网工具（TMDB / DuckDuckGo）整理成词表。

流程（Researcher.collect）：
  1. 已有词表且不 force → 直接返回
  2. 拉 TMDB 中文数据（官方译名/简介/演职员中文↔原文对照）
  3. agent 循环：模型可发起 SEARCH: 补充搜索（DDG，走配置的代理），
     或 ANSWER: 输出词表 JSON；最多 max_rounds 轮
  4. 兜底：模型不给答案时，直接用 TMDB 演职员对照生成词表
"""

import json
import re
import time

import requests

from .glossary import find_glossary, save_glossary
from .translate import build_client

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

SYSTEM = """你是字幕组的译名资料收集员。任务：为一部影视作品收集「翻译成简体中文字幕时的术语对照表」。
系统会提供已知资料（TMDB 等）。如需补充资料，你可以发起网络搜索。
每轮回复只允许输出以下两种格式之一（不要输出其他内容）：

SEARCH: 搜索关键词

或：

ANSWER: {"terms": {"原文": "简体中文译名"}, "notes": "一句话翻译风格提示"}

要求：
- terms 只收「原文→中文」有实际翻译意义的条目：剧名原文→官方中文译名、人名/店名/菜名/环节名等专有名词→通行中文译名（8~25 条）；中文词不需要收录
- 只使用资料中出现过的译名，不要编造；没有把握的条目不要写
- notes 用一句话提示翻译风格（如：实境秀竞技类，口语化，评委点评语气夸张）"""


def _proxies(proxy):
    return {"http": proxy, "https": proxy} if proxy else None


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def tmdb_tv(tmdb_id, api_key: str, proxy: str = None, timeout: int = 25):
    """TMDB 剧集中文数据 + 演职员中英对照。失败返回 None。"""
    if not tmdb_id or not api_key:
        return None
    base = f"https://api.themoviedb.org/3/tv/{int(tmdb_id)}"
    px = _proxies(proxy)
    try:
        zh = requests.get(base, params={"language": "zh-CN", "api_key": api_key},
                          proxies=px, timeout=timeout).json()
        cred_zh = requests.get(f"{base}/credits", params={"language": "zh-CN", "api_key": api_key},
                               proxies=px, timeout=timeout).json()
        cred_en = requests.get(f"{base}/credits", params={"language": "en-US", "api_key": api_key},
                               proxies=px, timeout=timeout).json()
    except (requests.RequestException, ValueError):
        return None
    if not zh.get("name"):
        return None
    cast_zh = (cred_zh.get("cast") or [])[:20]
    cast_en = (cred_en.get("cast") or [])[:20]
    pairs = []
    for i, czh in enumerate(cast_zh):
        cen = cast_en[i] if i < len(cast_en) else None
        en_name = (cen or {}).get("name") or czh.get("original_name") or ""
        pairs.append((en_name, czh.get("name") or "", czh.get("character") or ""))
    return {
        "zh_name": zh.get("name"),
        "original_name": zh.get("original_name"),
        "first_air": zh.get("first_air_date"),
        "genres": "/".join(g.get("name", "") for g in zh.get("genres") or []),
        "overview": (zh.get("overview") or "")[:600],
        "cast": pairs,
    }


def ddg_search(query: str, proxy: str = None, timeout: int = 15, n: int = 6):
    """DuckDuckGo HTML 版搜索，返回 [{title, snippet, url}]。失败返回 []。"""
    try:
        r = requests.post("https://html.duckduckgo.com/html/", data={"q": query},
                          headers={"User-Agent": UA}, proxies=_proxies(proxy), timeout=timeout)
        r.raise_for_status()
    except requests.RequestException:
        return []
    text = r.text
    titles = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', text)
    snippets = [_strip_tags(s) for s in
                re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', text, re.S)]
    out = []
    for i, (href, title) in enumerate(titles[:n]):
        m = re.search(r"uddg=([^&]+)", href)
        url = requests.utils.unquote(m.group(1)) if m else href
        out.append({"title": _strip_tags(title), "snippet": snippets[i] if i < len(snippets) else "", "url": url})
    return out


def parse_answer(reply: str):
    """从模型回复提取 ANSWER JSON。返回 (terms, notes) 或 (None, None)。"""
    m = re.search(r"ANSWER:\s*(\{.*\})", reply, re.S)
    if not m:
        return None, None
    blob = m.group(1)
    # 截到最后一个 } ，容忍模型在 JSON 后补充说明
    end = blob.rfind("}")
    try:
        d = json.loads(blob[:end + 1])
    except json.JSONDecodeError:
        return None, None
    terms = d.get("terms") if isinstance(d.get("terms"), dict) else d
    if not isinstance(terms, dict):
        return None, None
    clean = {}
    for k, v in terms.items():
        k, v = str(k).strip(), str(v).strip()
        if k and v and k != v:  # 自映射（中文→中文）条目无翻译价值
            clean[k] = v
    if not clean:
        return None, None
    return clean, str(d.get("notes") or "")[:300]


def tmdb_fallback_terms(facts) -> dict:
    """确定性兜底：剧名 + 演职员中英对照直接成词表。"""
    if not facts:
        return {}
    terms = {}
    if facts.get("original_name") and facts.get("zh_name"):
        terms[facts["original_name"]] = facts["zh_name"]
    for en, zh, _char in facts.get("cast") or []:
        if en and zh and en != zh:
            terms[en] = zh
    return terms


class Researcher:
    def __init__(self, cfg: dict, log=print):
        self.cfg = cfg
        self.log = log
        r = cfg.get("research") or {}
        t = cfg.get("translate") or {}
        self.dir = cfg["paths"]["glossary_dir"]
        self.proxy = r.get("proxy") or ""
        self.tmdb_key = r.get("tmdb_api_key") or ""
        self.max_rounds = int(r.get("max_rounds", 6))
        self.ai = build_client(
            t, num_ctx=int(r.get("num_ctx", 8192)), log=self.log,
        )

    def collect(self, series: str, tmdb_id=None, force: bool = False):
        """返回词表路径；无需/未能生成返回 None。"""
        existing = find_glossary(self.dir, series, tmdb_id)
        if existing and not force:
            return existing
        if not force:
            self.log(f"[research] 《{series}》无词表，开始联网收集译名资料…")

        facts = tmdb_tv(tmdb_id, self.tmdb_key, self.proxy)
        facts_text = self._format_facts(series, tmdb_id, facts)

        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"任务：为《{series}》收集译名词表。\n\n已知资料：\n{facts_text}"}]
        for _round in range(self.max_rounds):
            try:
                out = self.ai.chat_messages(msgs)
            except Exception as e:  # noqa: BLE001
                self.log(f"[research] 模型调用失败：{e}")
                break
            if "ANSWER:" in out:
                terms, notes = parse_answer(out)
                if terms:
                    # TMDB 演职员对照兜底合并：模型词条优先，缺的用官方对照补齐
                    merged = tmdb_fallback_terms(facts)
                    merged.update(terms)
                    return self._save(series, tmdb_id, merged, notes, facts, via="model")
            m = re.search(r"SEARCH:\s*(.+)", out)
            if m:
                q = m.group(1).strip()[:120]
                results = ddg_search(q, self.proxy)
                self.log(f"[research] 搜索: {q} → {len(results)} 条")
                body = "\n".join(f"- {x['title']}：{x['snippet']}" for x in results) or "（无结果）"
                msgs.append({"role": "assistant", "content": out})
                msgs.append({"role": "user", "content": f"搜索「{q}」的结果：\n{body}\n\n继续：资料够了就输出 ANSWER:，不够再 SEARCH:"})
            else:
                msgs.append({"role": "assistant", "content": out})
                msgs.append({"role": "user", "content": "格式无效。只允许输出 SEARCH: 关键词 或 ANSWER: {JSON}"})

        fallback = tmdb_fallback_terms(facts)
        if fallback:
            self.log(f"[research] 模型未给出答案，用 TMDB 对照兜底（{len(fallback)} 条）")
            return self._save(series, tmdb_id, fallback, "", facts, via="tmdb_fallback")
        self.log("[research] 未收集到资料（无 TMDB 数据且模型无输出）")
        return None

    @staticmethod
    def _format_facts(series: str, tmdb_id, facts) -> str:
        if not facts:
            return f"（TMDB 无数据或未配置 tmdb_api_key，剧名：{series}，tmdb_id={tmdb_id}）"
        cast = "\n".join(f"  {en} → {zh}（饰 {char}）" for en, zh, char in facts["cast"][:15])
        return (f"剧名（官方中文）: {facts['zh_name']}\n原名: {facts['original_name']}\n"
                f"首播: {facts['first_air']} | 类型: {facts['genres']}\n"
                f"简介: {facts['overview']}\n主要演职员（原文→中文）:\n{cast}")

    def _save(self, series, tmdb_id, terms, notes, facts, via: str):
        path = save_glossary(
            self.dir, series, tmdb_id, terms, notes=notes,
            meta={"source": via, "model": self.cfg["translate"]["model"],
                  "queried_title": series,
                  "tmdb_zh_name": (facts or {}).get("zh_name"),
                  "created_at": time.strftime("%F %T")},
        )
        self.log(f"[research] 词表已生成: {path}（{len(terms)} 条，来源={via}）")
        return path
