"""候选清单生成：Emby 扫描（媒体库感知）或纯文件系统扫描。"""

import json
import os
import time

from . import emby as emby_mod
from . import paths, probe


def _series_of(local_path: str) -> str:
    parent = os.path.basename(os.path.dirname(local_path))
    if parent.lower().startswith("season"):
        return os.path.basename(os.path.dirname(os.path.dirname(local_path)))
    return parent


def _excluded(cfg, name) -> bool:
    return name in set(cfg["detect"].get("exclude_series") or [])


def scan(cfg, source: str = "emby", do_probe: bool = False, probe_limit: int = 0,
         log=print) -> dict:
    """返回 {generated_at, source, summary, items}；items 仅含缺中字条目，按 mtime 新→旧。"""
    markers = cfg["detect"]["zh_markers"]
    items = []
    summary = {}

    if source == "emby":
        emb = emby_mod.Emby(cfg["emby"]["base_url"], cfg["emby"]["api_key"],
                            timeout=cfg["emby"].get("timeout", 30))
        for lib in cfg["emby"]["libraries"]:
            lib_id = emb.resolve_library_id(lib)
            total = missing = unresolved = 0
            for it in emb.walk(lib_id, cfg["emby"].get("item_types") or ["Episode", "Movie"]):
                total += 1
                raw_path = it.get("Path") or ""
                local = paths.map_prefix(raw_path, cfg["paths"]["emby_path_map"])
                if not local or not os.path.exists(local):
                    unresolved += 1
                    continue
                if emby_mod.has_zh_streams(it) or paths.sibling_zh(local, markers):
                    continue
                if _excluded(cfg, it.get("SeriesName") or _series_of(local)):
                    continue
                missing += 1
                items.append({
                    "item_id": it.get("Id"),
                    "type": it.get("Type"),
                    "name": it.get("Name"),
                    "series": it.get("SeriesName") or _series_of(local),
                    "emby_path": raw_path,
                    "local_path": local,
                })
            summary[str(lib)] = {"total": total, "missing": missing, "unresolved": unresolved}
    else:
        exts = (".strm", ".mkv", ".mp4", ".ts", ".avi", ".rmvb", ".mov", ".wmv", ".m4v")
        for root in cfg["paths"]["strm_roots"]:
            total = missing = 0
            for dirpath, _dirs, files in os.walk(root):
                for fn in files:
                    if not fn.lower().endswith(exts):
                        continue
                    total += 1
                    local = os.path.join(dirpath, fn)
                    if paths.sibling_zh(local, markers):
                        continue
                    if _excluded(cfg, _series_of(local)):
                        continue
                    missing += 1
                    items.append({
                        "item_id": None,
                        "type": "Video",
                        "name": os.path.splitext(fn)[0],
                        "series": _series_of(local),
                        "emby_path": None,
                        "local_path": local,
                    })
            summary[str(root)] = {"total": total, "missing": missing, "unresolved": 0}

    probed = 0
    for it in items:
        it["kind"] = "unprobed"
        if do_probe and (probe_limit <= 0 or probed < probe_limit):
            media = paths.resolve_media(it["local_path"], cfg["paths"]["path_map"])
            streams = probe.ffprobe_streams(media) if media and os.path.exists(media) else None
            it["kind"] = probe.classify(probe.subtitle_tracks(streams)) if streams else "probe_failed"
            probed += 1
        it["mtime"] = os.path.getmtime(it["local_path"])

    items.sort(key=lambda x: x.get("mtime") or 0, reverse=True)
    return {
        "generated_at": time.strftime("%F %T"),
        "source": source,
        "tool": "zhsub",
        "summary": summary,
        "items": items,
    }


def save(result: dict, data_dir: str, out: str = None) -> str:
    os.makedirs(data_dir, exist_ok=True)
    path = out or os.path.join(data_dir, "scan.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    return path


def load_latest(data_dir: str, max_age_hours: float = None):
    path = os.path.join(data_dir, "scan.json")
    if not os.path.isfile(path):
        return None
    if max_age_hours is not None:
        age = time.time() - os.path.getmtime(path)
        if age > max_age_hours * 3600:
            return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
