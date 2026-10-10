"""路径处理：strm 内容解析、前缀映射、兄弟字幕文件判定。"""

import os

TEXT_SUB_EXTS = (".srt", ".ass", ".ssa", ".sub", ".vtt")


def map_prefix(path: str, path_map) -> str:
    """按配置的 {from, to} 列表做前缀替换，命中即返回。"""
    p = path.replace("\\", "/")
    for m in path_map or []:
        frm = str(m.get("from", "")).rstrip("/") + "/"
        to = str(m.get("to", "")).rstrip("/")
        if p.startswith(frm):
            return to + "/" + p[len(frm):]
    return path


def read_strm(strm_path: str) -> str:
    """strm = 一行文本（Emby 容器视角的媒体路径）。"""
    with open(strm_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                return line
    return ""


def resolve_media(local_path: str, path_map) -> str:
    """本地路径 → 可 ffprobe 的媒体文件路径。strm 则读内容并映射；真实文件直接返回。"""
    if local_path.lower().endswith(".strm"):
        inner = read_strm(local_path)
        return map_prefix(inner, path_map) if inner else ""
    return local_path


def sibling_zh(video_or_strm: str, markers=("zh", "chi", "chs", "cht", "zho", ".cn.")) -> bool:
    """同目录兄弟文件里是否已有中文字幕（判定逻辑平移自 driver v5 的 has_chi_ext）。"""
    base = video_or_strm
    if base.lower().endswith(".strm"):
        base = base[:-5]
    prefix = os.path.basename(base) + "."
    try:
        names = os.listdir(os.path.dirname(base) or ".")
    except OSError:
        return False
    for name in names:
        if not name.startswith(prefix) or name == os.path.basename(base):
            continue
        suffix = name[len(prefix):].lower()
        if not suffix.endswith(TEXT_SUB_EXTS):
            continue
        if any(mk in suffix for mk in markers):
            return True
    return False


def sub_output_paths(video_or_strm: str):
    """两种产物的完整路径：抽取用 .default.chi.zh-cn.srt，翻译用 .zh.ai.srt。"""
    base = video_or_strm
    if base.lower().endswith(".strm"):
        base = base[:-5]
    return base + ".default.chi.zh-cn.srt", base + ".zh.ai.srt"
