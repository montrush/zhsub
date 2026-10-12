"""Emby 客户端：媒体库扫描与「缺中文字幕」判定。"""

import requests

from .probe import TEXT_CODECS

ZH_LANG_PREFIX = "zh"
ZH_LANG_CODES = {"chi", "zho"}

# Emby 与 ffprobe 对图形字幕轨的编码名不一致，统一到 ffprobe 命名
_EMBY_CODEC_MAP = {
    "pgssub": "hdmv_pgs_subtitle",
    "pgs": "hdmv_pgs_subtitle",
    "dvdsub": "dvd_subtitle",
    "dvbsub": "dvb_subtitle",
}


class Emby:
    def __init__(self, base_url: str, api_key: str, timeout: int = 30):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.timeout = timeout
        self.http = requests.Session()

    def get(self, path: str, **params):
        params["api_key"] = self.key
        r = self.http.get(f"{self.base}/emby/{path.lstrip('/')}", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def libraries(self):
        """返回 [{Name, ItemId, CollectionType, Locations}]。"""
        return self.get("Library/VirtualFolders")

    def resolve_library_id(self, name_or_id) -> str:
        s = str(name_or_id)
        if s.isdigit():
            return s
        for lib in self.libraries():
            if lib.get("Name") == s:
                return str(lib.get("ItemId"))
        raise SystemExit(f"Emby 中找不到媒体库: {name_or_id}")

    def count(self, parent_id: str, item_types) -> int:
        d = self.get(
            "Items", ParentId=parent_id, IncludeItemTypes=",".join(item_types),
            Recursive="true", Limit=0,
        )
        return int(d.get("TotalRecordCount", 0))

    def walk(self, parent_id: str, item_types, page: int = 500):
        """分页遍历库内条目，带 Path/MediaStreams/ProviderIds 字段。"""
        start = 0
        while True:
            d = self.get(
                "Items",
                ParentId=parent_id,
                IncludeItemTypes=",".join(item_types),
                Recursive="true",
                Fields="Path,MediaStreams,ProviderIds",
                StartIndex=start,
                Limit=page,
                EnableImages="false",
                EnableUserData="false",
            )
            items = d.get("Items", [])
            yield from items
            start += len(items)
            if len(items) < page:
                return


def series_tmdb_map(emb: "Emby", series_ids) -> dict:
    """批量查系列条目的 TMDB 号：{SeriesId: tmdb_id}。"""
    out = {}
    ids = [str(s) for s in series_ids if s]
    for i in range(0, len(ids), 150):
        chunk = ids[i:i + 150]
        try:
            d = emb.get("Items", Ids=",".join(chunk), Fields="ProviderIds",
                        Limit=len(chunk) + 1, EnableImages="false", EnableUserData="false")
        except Exception:  # noqa: BLE001 - 查询失败只影响词表精度
            continue
        for s in d.get("Items", []):
            tm = (s.get("ProviderIds") or {}).get("Tmdb")
            if tm and str(tm).isdigit():
                out[str(s.get("Id"))] = int(tm)
    return out


def _stream_is_zh(s: dict) -> bool:
    lang = (s.get("Language") or "").lower()
    if lang.startswith(ZH_LANG_PREFIX) or lang in ZH_LANG_CODES:
        return True
    label = f"{s.get('DisplayTitle') or ''} {s.get('Title') or ''}"
    return ("中" in label) or ("Chinese" in label)


def has_zh_streams(item: dict) -> bool:
    """Emby 可见的字幕流里是否有中文（含外挂 .zh*.srt，前提是 Emby 已扫到）。"""
    for s in item.get("MediaStreams") or []:
        if s.get("Type") == "Subtitle" and _stream_is_zh(s):
            return True
    return False


def stream_tracks(item: dict):
    """MediaStreams → probe.classify 可用的轨道元组 [(index, lang, title, codec, is_text)]。

    Emby 没扫出流信息时返回 None（调用方需回退 ffprobe）；扫过但确实无字幕轨
    则返回空列表（可直接判 none，不必再探测）。
    """
    streams = item.get("MediaStreams")
    if not streams:
        return None
    tracks = []
    for s in streams:
        if s.get("Type") != "Subtitle":
            continue
        codec = (s.get("Codec") or "").lower()
        codec = _EMBY_CODEC_MAP.get(codec, codec)
        tracks.append((
            int(s.get("Index") or -1),
            (s.get("Language") or "").lower(),
            s.get("Title") or s.get("DisplayTitle") or "",
            codec,
            codec in TEXT_CODECS,
        ))
    return tracks
