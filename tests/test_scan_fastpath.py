"""扫描提速与新账本逻辑测试：Emby 流信息分类、ffprobe 并发兜底、失败冷却。"""

import time

from zhsub import emby, paths as paths_mod, probe, scan
from zhsub.config import DEFAULTS
from zhsub.pipeline import _within_hours
from zhsub.store import Store


# ---------- emby.stream_tracks ----------

def test_stream_tracks_none_when_no_info():
    assert emby.stream_tracks({}) is None
    assert emby.stream_tracks({"MediaStreams": []}) is None


def test_stream_tracks_maps_codecs():
    item = {"MediaStreams": [
        {"Type": "Video", "Codec": "h264", "Index": 0},
        {"Type": "Subtitle", "Codec": "SubRip", "Language": "chi", "Title": "简体", "Index": 5},
        {"Type": "Subtitle", "Codec": "PGSSub", "Language": "eng", "Title": "", "Index": 6},
    ]}
    tracks = emby.stream_tracks(item)
    assert tracks == [
        (5, "chi", "简体", "subrip", True),
        (6, "eng", "", "hdmv_pgs_subtitle", False),
    ]
    assert probe.classify(tracks) == "chi_text"


def test_stream_tracks_empty_subs_means_none():
    # Emby 扫过（有视频流）但确实没有字幕轨 → 直接判 none，不必 ffprobe
    item = {"MediaStreams": [{"Type": "Video", "Codec": "h264", "Index": 0}]}
    assert emby.stream_tracks(item) == []
    assert probe.classify(emby.stream_tracks(item)) == "none"


# ---------- scan._probe_pending ----------

def _mk_items(tmp_path, n):
    items = []
    for i in range(n):
        p = tmp_path / f"ep{i}.strm"
        p.write_text("/media/cloud115/x.mkv\n", encoding="utf-8")
        items.append({"local_path": str(p)})
    return items


def test_probe_pending_no_probe_marks_unprobed(tmp_path):
    items = _mk_items(tmp_path, 3)
    cfg = {**DEFAULTS, "paths": {**DEFAULTS["paths"], "path_map": []}}
    scan._probe_pending(items, cfg, do_probe=False, probe_limit=0)
    assert all(it["kind"] == "unprobed" for it in items)
    assert all("mtime" in it for it in items)


def test_probe_pending_parallel_and_keep_known_kind(tmp_path, monkeypatch):
    items = _mk_items(tmp_path, 4)
    items[0]["kind"] = "chi_text"          # Emby 已分类的条目不应被探测覆盖
    cfg = {**DEFAULTS, "paths": {**DEFAULTS["paths"], "path_map": []},
           "detect": {**DEFAULTS["detect"], "probe_workers": 4}}
    calls = []

    def fake_streams(media, timeout=90):
        calls.append(media)
        return [{"index": 2, "codec_name": "subrip", "codec_type": "subtitle",
                 "tags": {"language": "eng"}}]

    monkeypatch.setattr(scan.probe, "ffprobe_streams", fake_streams)
    monkeypatch.setattr(scan.paths, "resolve_media",
                        lambda p, m: p[:-5] + ".mkv")
    monkeypatch.setattr(scan.os.path, "exists", lambda p: True)
    scan._probe_pending(items, cfg, do_probe=True, probe_limit=0)
    assert items[0]["kind"] == "chi_text"
    assert [it["kind"] for it in items[1:]] == ["foreign_text"] * 3
    assert len(calls) == 3
    assert all(it["kind"] in ("foreign_text",) for it in items[1:])


def test_probe_pending_probe_limit(tmp_path, monkeypatch):
    items = _mk_items(tmp_path, 5)
    cfg = {**DEFAULTS, "paths": {**DEFAULTS["paths"], "path_map": []}}
    monkeypatch.setattr(scan.probe, "ffprobe_streams", lambda m, timeout=90: None)
    monkeypatch.setattr(scan.paths, "resolve_media", lambda p, m: p[:-5] + ".mkv")
    monkeypatch.setattr(scan.os.path, "exists", lambda p: True)
    scan._probe_pending(items, cfg, do_probe=True, probe_limit=2)
    kinds = [it["kind"] for it in items]
    assert kinds.count("probe_failed") == 2   # 探测了 2 条
    assert kinds.count("unprobed") == 3       # 其余保持 unprobed


# ---------- store 失败账本 ----------

def test_fail_dedupe_same_hour(tmp_path):
    st = Store(str(tmp_path / "d"))
    st.fail("/a.strm", "媒体文件不存在: /x.mkv")
    st.fail("/a.strm", "媒体文件不存在: /x.mkv")
    assert st.stats()["failures"] == 1
    st.fail("/a.strm", "ffprobe 失败（文件损坏或挂载不可读）")
    assert st.stats()["failures"] == 2


def test_last_retryable_fail_ignores_no_media(tmp_path):
    st = Store(str(tmp_path / "d"))
    st.fail("/a.strm", "媒体文件不存在: /x.mkv")
    assert st.last_retryable_fail("/a.strm") is None
    st.fail("/a.strm", "翻译失败:boom")
    ts, reason = st.last_retryable_fail("/a.strm")
    assert reason.startswith("翻译失败")
    assert _within_hours(ts, 24)
    assert not _within_hours("2000-01-01 00:00:00", 24)
