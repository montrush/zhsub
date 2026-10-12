"""候选清单生成：Emby 扫描（媒体库感知）或纯文件系统扫描。

分类优先级：Emby MediaStreams 能看到字幕轨 → 直接 classify（零探测成本）；
流信息缺失（Emby 未扫过）→ 并发 ffprobe 兜底（detect.probe_workers）。
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from . import emby as emby_mod
from . import paths, probe


def _series_of(local_path: str) -> str:
    parent = os.path.basename(os.path.dirname(local_path))
    if parent.lower().startswith("season"):
        return os.path.basename(os.path.dirname(os.path.dirname(local_path)))
    return parent


def _int_tmdb(v):
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


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
            series_ids = set()
            raw_items = []
            for it in emb.walk(lib_id, cfg["emby"].get("item_types") or ["Episode", "Movie"]):
                total += 1
                if it.get("SeriesId"):
                    series_ids.add(it["SeriesId"])
                raw_items.append(it)
            tmdb_map = emby_mod.series_tmdb_map(emb, series_ids)
            for it in raw_items:
                raw_path = it.get("Path") or ""
                local = paths.map_prefix(raw_path, cfg["paths"]["emby_path_map"])
                if not local or not os.path.exists(local):
                    unresolved += 1
                    continue
                if emby_mod.has_zh_streams(it) or paths.sibling_zh(local, markers):
                    continue
                series = it.get("SeriesName") or _series_of(local)
                if _excluded(cfg, series):
                    continue
                missing += 1
                tmdb = tmdb_map.get(str(it.get("SeriesId") or "")) or \
                    _int_tmdb((it.get("ProviderIds") or {}).get("Tmdb"))
                # Emby 已扫出流信息 → 现场分类；没扫过（None）→ 留给 ffprobe 兜底
                tracks = emby_mod.stream_tracks(it)
                items.append({
                    "item_id": it.get("Id"),
                    "type": it.get("Type"),
                    "name": it.get("Name"),
                    "series": series,
                    "tmdb_id": tmdb,
                    "emby_path": raw_path,
                    "local_path": local,
                    "kind": probe.classify(tracks) if tracks is not None else None,
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

    _probe_pending(items, cfg, do_probe, probe_limit, log)
    items.sort(key=lambda x: x.get("mtime") or 0, reverse=True)
    return {
        "generated_at": time.strftime("%F %T"),
        "source": source,
        "tool": "zhsub",
        "summary": summary,
        "items": items,
    }


def _probe_pending(items, cfg, do_probe: bool, probe_limit: int, log=print):
    """给 kind 未定的条目补分类：并发 ffprobe（受 probe_limit 约束），其余标 unprobed。"""
    for it in items:
        it.setdefault("kind", None)
        it["mtime"] = os.path.getmtime(it["local_path"])
    if not do_probe:
        for it in items:
            if it["kind"] is None:
                it["kind"] = "unprobed"
        return
    pending = [it for it in items if it["kind"] is None]
    limit = len(pending) if probe_limit <= 0 else min(probe_limit, len(pending))
    todo, rest = pending[:limit], pending[limit:]
    for it in rest:
        it["kind"] = "unprobed"
    if not todo:
        return
    workers = max(1, int(cfg["detect"].get("probe_workers") or 4))
    log(f"ffprobe 预判 {len(todo)} 条（{workers} 并发，Emby 元数据已先行分类其余条目）...")

    def run(it):
        try:
            media = paths.resolve_media(it["local_path"], cfg["paths"]["path_map"])
            streams = probe.ffprobe_streams(media) if media and os.path.exists(media) else None
            it["kind"] = probe.classify(probe.subtitle_tracks(streams)) if streams else "probe_failed"
        except Exception:  # noqa: BLE001 - 单条失败不拖垮整个扫描
            it["kind"] = "probe_failed"

    if workers == 1:
        for it in todo:
            run(it)
        return
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(run, todo))


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
