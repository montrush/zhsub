"""SRT 解析/序列化（容错实现，仅依赖标准库）。"""

import re

TIMING_RE = re.compile(
    r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})"
)


def _norm_ts(ts: str) -> str:
    return ts.replace(".", ",")


class Cue:
    __slots__ = ("index", "start", "end", "text")

    def __init__(self, start: str, end: str, text: str):
        self.index = 0
        self.start = _norm_ts(start.strip())
        self.end = _norm_ts(end.strip())
        self.text = text.strip("\n").strip()

    def as_line(self) -> str:
        """作为单行文本送翻译（cue 内部换行折成空格）。"""
        return " ".join(self.text.splitlines()).strip()

    def set_line(self, line: str):
        self.text = line.strip()


def parse_srt(text: str):
    """宽容解析：块间空行分隔，序号行可有可无。返回 [Cue]。"""
    cues = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        m = TIMING_RE.search(lines[0]) or (
            TIMING_RE.search(lines[1]) if len(lines) > 1 else None
        )
        if not m:
            continue  # 非字幕块（如 WEBVTT 头）
        if TIMING_RE.search(lines[0]):
            body = lines[1:]
        else:
            body = lines[2:]
        cues.append(Cue(m.group(1), m.group(2), "\n".join(body)))
    for i, c in enumerate(cues, 1):
        c.index = i
    return cues


def dumps_srt(cues) -> str:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{c.start} --> {c.end}\n{c.text}\n")
    return "\n".join(out)
