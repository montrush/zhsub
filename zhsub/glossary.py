"""词表库：每剧一个 JSON，用「视频名 + TMDB 号」识别。

目录结构（config paths.glossary_dir，仓库内种子在 glossaries/）：
    地狱厨房.tmdb2370.json   ← 优先：系列名 + TMDB 号
    地狱厨房.json            ← 次选：仅系列名
    _default.json            ← 兜底：全局默认词表

文件格式（两种都认）：
    平铺:  {"Gordon Ramsay": "戈登·拉姆齐", ...}
    结构化: {"title": "...", "tmdb_id": 2370,
             "terms": {"原文": "译名", ...}, "notes": "翻译风格提示", "meta": {...}}
"""

import json
import os
import re
import time

_UNSAFE = re.compile(r'[\\/:*?"<>|\s]+')


def sanitize(name: str) -> str:
    return _UNSAFE.sub("_", (name or "").strip()) or "unknown"


def candidate_names(series: str, tmdb_id=None):
    names = []
    s = sanitize(series)
    if tmdb_id:
        try:
            names.append(f"{s}.tmdb{int(tmdb_id)}.json")
        except (TypeError, ValueError):
            pass
    names.append(f"{s}.json")
    names.append("_default.json")
    return names


def find_glossary(glossary_dir: str, series: str, tmdb_id=None):
    """返回命中的词表文件路径；没有则 None。"""
    if not glossary_dir or not series:
        return None
    for name in candidate_names(series, tmdb_id):
        p = os.path.join(glossary_dir, name)
        if os.path.isfile(p):
            return p
    return None


def normalize(raw: dict) -> dict:
    """平铺/结构化 → {"terms", "notes", "meta"}。terms 只留非空 str→str。"""
    if not isinstance(raw, dict):
        return {"terms": {}, "notes": "", "meta": {}}
    terms = raw.get("terms") if isinstance(raw.get("terms"), dict) else raw
    clean = {}
    for k, v in (terms or {}).items():
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
            clean[k.strip()] = v.strip()
    return {
        "terms": clean,
        "notes": str(raw.get("notes") or ""),
        "meta": raw.get("meta") if isinstance(raw.get("meta"), dict) else {},
    }


def load_glossary_file(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return normalize(json.load(f))
    except (OSError, json.JSONDecodeError):
        return None


def load_glossary(glossary_dir: str, series: str, tmdb_id=None):
    """便利接口：按优先级找并加载，返回 terms dict 或 None。"""
    p = find_glossary(glossary_dir, series, tmdb_id)
    if not p:
        return None
    g = load_glossary_file(p)
    return g["terms"] or None


def save_glossary(glossary_dir: str, series: str, tmdb_id, terms: dict,
                  notes: str = "", meta: dict = None, filename: str = None) -> str:
    """原子写入一份词表（结构化格式）。filename 可显式指定（WebUI 用）。"""
    os.makedirs(glossary_dir, exist_ok=True)
    if filename:
        name = os.path.basename(filename)
    else:
        name = candidate_names(series, tmdb_id)[0] if tmdb_id else f"{sanitize(series)}.json"
    doc = {
        "title": series,
        "tmdb_id": int(tmdb_id) if tmdb_id else None,
        "terms": dict(terms or {}),
        "notes": notes or "",
        "meta": meta or {},
        "updated_at": time.strftime("%F %T"),
    }
    path = os.path.join(glossary_dir, name)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def list_glossaries(glossary_dir: str):
    out = []
    if not glossary_dir or not os.path.isdir(glossary_dir):
        return out
    for fn in sorted(os.listdir(glossary_dir)):
        if not fn.endswith(".json"):
            continue
        p = os.path.join(glossary_dir, fn)
        g = load_glossary_file(p) or {}
        m = re.match(r"(.+?)(?:\.tmdb(\d+))?\.json$", fn)
        out.append({
            "file": fn,
            "title": g.get("meta", {}).get("queried_title") or (m.group(1) if m else fn),
            "tmdb_id": int(m.group(2)) if (m and m.group(2)) else None,
            "terms_count": len(g.get("terms") or {}),
            "notes": (g.get("notes") or "")[:120],
            "mtime": time.strftime("%F %T", time.localtime(os.path.getmtime(p))),
            "size": os.path.getsize(p),
        })
    return out
