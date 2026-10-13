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


def _path_in(local: str, raw_path: str, path: str) -> bool:
    """范围过滤：本地映射路径或 emby 原始路径任一命中前缀即可（容器内路径/挂载路径都好使）。"""
    p = (path or "").rstrip("/").rstrip("\\")
    if not p:
        return True
    return local.startswith(p) or (raw_path or "").startswith(p) \
        or local.replace("\\", "/").startswith(p.replace("\\", "/"))


def _category_ok(it_type: str, local: str, category: str) -> bool:
    """范围过滤：series=剧集(Season 目录/emby Episode)；movies=电影。None=不过滤。"""
    if not category:
        return True
    season_dir = "season" in os.path.basename(os.path.dirname(local)).lower()
    if category == "series":
        return it_type == "Episode" or season_dir
    if category == "movies":
        return it_type == "Movie" or not season_dir
    return True


_MEDIA_EXTS = ('.mkv', '.mp4', '.ts', '.avi', '.rmvb', '.mov', '.wmv', '.m4v', '.strm')
_SUB_SUFFIXES = ('.zh.ai', '.chs', '.cht', '.zh', '.chi', '.zho', '.cn')


def _bare_stem(fn: str) -> str:
    """字幕/媒体文件名 → 裸条目stem（剥 .srt / .zh.ai 等字幕后缀 / 媒体扩展名），
    使 'xxx.mkv.zh.ai.srt' 与 'xxx.mkv' / 'xxx.strm' 归一到 'xxx'。"""
    stem = os.path.splitext(fn)[0]
    while True:
        low = stem.lower()
        for suf in _SUB_SUFFIXES + _MEDIA_EXTS:
            if low.endswith(suf):
                stem = stem[:-len(suf)]
                break
        else:
            return stem


def build_zh_side_index(cfg) -> dict:
    """一次走查 strm_roots 建 {裸stem: 字幕文件名} 索引——云侧媒体的字幕实际交付在
    strm 树（本栈布局），按 stem（含剧集号，唯一性足够）对射检测已有中字幕。"""
    idx = {}
    marks = tuple(m.lower() for m in cfg["detect"]["zh_markers"])
    for root in cfg["paths"]["strm_roots"]:
        if not root or not os.path.isdir(root):
            continue
        for _dp, _dn, fns in os.walk(root):
            for fn in fns:
                low = fn.lower()
                if low.endswith(('.srt', '.ass')) and any(m in low for m in marks):
                    idx.setdefault(_bare_stem(fn), fn)
    return idx


def existing_zh_sub(local_path: str, cfg, side_index: dict) -> str:
    """已有中文字幕检测（双路）：①同目录伴生（含 xxx.mkv.zh.ai.srt 双后缀形态）
    ②strm 侧索引。返回字幕文件名/描述，空串=没有。"""
    marks = tuple(m.lower() for m in cfg["detect"]["zh_markers"])
    sdir, fn = os.path.split(local_path)
    bare = _bare_stem(fn)
    try:
        sibs = os.listdir(sdir)
    except OSError:
        sibs = []
    for f in sibs:
        low = f.lower()
        if low.endswith(('.srt', '.ass')) and any(m in low for m in marks) and \
                _bare_stem(f) == bare:
            return f
    return side_index.get(bare) or ''


def scan(cfg, source: str = "emby", do_probe: bool = False, probe_limit: int = 0,
         path: str = None, category: str = None, library: str = None, log=print) -> dict:
    """返回 {generated_at, source, summary, items}；items 仅含缺中字条目，按 mtime 新→旧。

    path: 只扫该目录（本地路径或 emby 路径前缀均可命中），如单扫地狱厨房；
    category: 'series' 只扫剧集 / 'movies' 只扫电影（emby Type 或 Season 目录启发）；
    library: 只扫指定 emby 媒体库（配置中的库名精确匹配，如 '电视剧115'/'电影'）。"""
    markers = cfg["detect"]["zh_markers"]
    items = []
    summary = {}

    if source == "emby":
        emb = emby_mod.Emby(cfg["emby"]["base_url"], cfg["emby"]["api_key"],
                            timeout=cfg["emby"].get("timeout", 30))
        side_index = None
        for lib in cfg["emby"]["libraries"]:
            if library and lib != library:
                continue
            lib_id = emb.resolve_library_id(lib)
            total = missing = unresolved = excluded_n = 0
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
                if not _path_in(local, raw_path, path) or \
                        not _category_ok(it.get("Type") or "", local, category):
                    continue
                # ⭐1013用户令: 已有中字幕不再静默丢弃 → 默认排除+备注(WebUI可勾回重译)
                if emby_mod.has_zh_streams(it):
                    zh_hit = "内嵌中文字幕轨"
                else:
                    if side_index is None:
                        side_index = build_zh_side_index(cfg)
                    zh_hit = existing_zh_sub(local, cfg, side_index)
                series = it.get("SeriesName") or _series_of(local)
                if _excluded(cfg, series):
                    continue
                if zh_hit:
                    excluded_n += 1
                else:
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
                    "excluded": bool(zh_hit),
                    "exclude_reason": f"已有中字幕: {zh_hit}" if zh_hit else "",
                })
            summary[str(lib)] = {"total": total, "missing": missing,
                                 "unresolved": unresolved, "excluded": excluded_n}
    else:
        exts = (".strm", ".mkv", ".mp4", ".ts", ".avi", ".rmvb", ".mov", ".wmv", ".m4v")
        roots = [path] if path else cfg["paths"]["strm_roots"]
        side_index = build_zh_side_index(cfg)
        for root in roots:
            total = missing = excluded_n = 0
            for dirpath, _dirs, files in os.walk(root):
                for fn in files:
                    if not fn.lower().endswith(exts):
                        continue
                    total += 1
                    local = os.path.join(dirpath, fn)
                    if not _category_ok("Video", local, category):
                        continue
                    zh_hit = existing_zh_sub(local, cfg, side_index)
                    if _excluded(cfg, _series_of(local)):
                        continue
                    if zh_hit:
                        excluded_n += 1
                    else:
                        missing += 1
                    items.append({
                        "item_id": None,
                        "type": "Video",
                        "name": os.path.splitext(fn)[0],
                        "series": _series_of(local),
                        "emby_path": None,
                        "local_path": local,
                        "excluded": bool(zh_hit),
                        "exclude_reason": f"已有中字幕: {zh_hit}" if zh_hit else "",
                    })
            summary[str(root)] = {"total": total, "missing": missing,
                                 "unresolved": 0, "excluded": excluded_n}

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
    pending = [it for it in items if it["kind"] is None and not it.get("excluded")]
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
