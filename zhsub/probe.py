"""ffprobe 探针：字幕轨枚举与选轨策略（平移自 driver v5 的 embed_chi_idxs，扩展英文降级）。"""

import json
import re
import subprocess

# 可抽取为文本的字幕编码；PGS/VOBSub 是图形轨，走 ASR 通路
TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "subviewer", "subviewer1"}
IMAGE_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"}

SIGN_RE = re.compile(r"sign|song|sing|karaoke|credit|forced|title", re.I)
SIMPLIFIED_RE = re.compile(r"Sim|简|Hans", re.I)


def ffprobe_streams(media_path: str, timeout: int = 90):
    """返回全部流信息（index/codec_name/codec_type/tags）。失败返回 None。"""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "stream=index,codec_name,codec_type:stream_tags=language,title",
                "-of", "json", media_path,
            ],
            capture_output=True, text=True, timeout=timeout, check=True,
        ).stdout
        return json.loads(out).get("streams", [])
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None


def subtitle_tracks(streams):
    """[(index, lang, title, codec, is_text)]，仅字幕轨。"""
    result = []
    for s in streams or []:
        if s.get("codec_type") != "subtitle":
            continue
        codec = (s.get("codec_name") or "").lower()
        tags = s.get("tags") or {}
        result.append((
            int(s.get("index", -1)),
            (tags.get("language") or "").lower(),
            tags.get("title") or "",
            codec,
            codec in TEXT_CODECS,
        ))
    return result


def pick_chi_text_track(tracks):
    """内嵌中文文本轨：简体优先、非 sign/song 优先，找不到返回 None。"""
    chi = [t for t in tracks if t[4] and (t[1].startswith("zh") or t[1] in ("chi", "zho"))]
    if not chi:
        return None
    clean = [t for t in chi if not SIGN_RE.search(t[2])]
    pool = clean or chi
    pool = sorted(pool, key=lambda t: 0 if SIMPLIFIED_RE.search(t[2]) else 1)
    return pool[0]


def pick_foreign_text_track(tracks):
    """翻译源轨：优先英文，其次任一非中文文本轨。"""
    clean = [t for t in tracks if t[4] and not SIGN_RE.search(t[2])]
    pool = clean or [t for t in tracks if t[4]]
    eng = [t for t in pool if t[1] in ("eng", "en")]
    if eng:
        return eng[0]
    others = [t for t in pool if not (t[1].startswith("zh") or t[1] in ("chi", "zho"))]
    return others[0] if others else None


def classify(tracks):
    """给扫描用的粗分类：chi_text / foreign_text / image_only / none。"""
    chi = pick_chi_text_track(tracks)
    if chi:
        return "chi_text"
    if pick_foreign_text_track(tracks):
        return "foreign_text"
    if any((not t[4]) and t[3] in IMAGE_CODECS for t in tracks):
        return "image_only"
    return "none"
