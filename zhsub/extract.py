"""字幕轨抽取：ffmpeg 提取 + 空壳轨校验（平移自 driver v5 的 extract_chi）。"""

import os
import subprocess


def extract_track(media_path: str, stream_index: int, out_path: str, timeout: int = 900) -> bool:
    """把文本字幕轨抽成 srt（ass/ssa 自动转码为 srt，样式丢弃）。原子写入。"""
    tmp = out_path + ".part"
    try:
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-xerror", "-y",
                "-i", media_path, "-map", f"0:{stream_index}",
                "-c:s", "srt", "-f", "srt", tmp,
            ],
            capture_output=True, text=True, timeout=timeout, check=True,
        )
        os.replace(tmp, out_path)
        return True
    except (subprocess.SubprocessError, OSError):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def looks_empty(path: str, min_bytes: int = 2000) -> bool:
    """空壳轨判定：内容小于阈值（driver v5 同款 2KB）。"""
    return file_size(path) < min_bytes
