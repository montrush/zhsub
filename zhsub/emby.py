"""Emby 客户端：媒体库扫描与「缺中文字幕」判定。"""

import requests

ZH_LANG_PREFIX = "zh"
ZH_LANG_CODES = {"chi", "zho"}


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
        """分页遍历库内条目，带 Path 与 MediaStreams 字段。"""
        start = 0
        while True:
            d = self.get(
                "Items",
                ParentId=parent_id,
                IncludeItemTypes=",".join(item_types),
                Recursive="true",
                Fields="Path,MediaStreams",
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
