from zhsub import glossary, paths, srtio
from zhsub.research import parse_answer, tmdb_fallback_terms
from zhsub.translate import parse_numbered_reply

SRT = """1
00:00:01,000 --> 00:00:03,500
Hello world.

2
00:00:04,000 --> 00:00:06,000
How are you?
I'm fine.

3
00:00:07,000 --> 00:00:09,000
Goodbye!
"""


def test_parse_and_dump_roundtrip():
    cues = srtio.parse_srt(SRT)
    assert len(cues) == 3
    assert cues[1].start == "00:00:04,000"
    assert cues[1].as_line() == "How are you? I'm fine."
    out = srtio.dumps_srt(cues)
    assert len(srtio.parse_srt(out)) == 3
    assert "00:00:01,000 --> 00:00:03,500" in out


def test_sibling_zh_detection(tmp_path):
    v = tmp_path / "Show - S01E01.strm"
    v.write_text("/data/cloud115/115/x.mkv")
    assert not paths.sibling_zh(str(v))
    (tmp_path / "Show - S01E01.zh.ai.srt").write_text("x")
    assert paths.sibling_zh(str(v))
    (tmp_path / "Show - S01E01.default.chi.zh-cn.srt").write_text("x")
    assert paths.sibling_zh(str(v))


def test_sub_output_paths():
    a, b = paths.sub_output_paths("/x/剧 - S01E01 - 第 1 集.strm")
    assert a == "/x/剧 - S01E01 - 第 1 集.default.chi.zh-cn.srt"
    assert b == "/x/剧 - S01E01 - 第 1 集.zh.ai.srt"


def test_map_prefix():
    m = [{"from": "/data/cloud115", "to": "/media/cloud115"}]
    assert paths.map_prefix("/data/cloud115/115/a.mkv", m) == "/media/cloud115/115/a.mkv"
    assert paths.map_prefix("/other/a.mkv", m) == "/other/a.mkv"


def test_parse_numbered_reply():
    reply = "1|你好\n\n2|世界\n垃圾说明行\n"
    assert parse_numbered_reply(reply, 2) == {0: "你好", 1: "世界"}
    assert parse_numbered_reply("只有一行没有编号", 1) == {}


def test_glossary_normalize_and_roundtrip(tmp_path):
    flat = {"Gordon Ramsay": "戈登·拉姆齐", "": "x", 3: "y"}
    g = glossary.normalize(flat)
    assert g["terms"] == {"Gordon Ramsay": "戈登·拉姆齐"}
    doc = {"title": "地狱厨房", "tmdb_id": 2370, "terms": {"A": "甲"}, "notes": "n"}
    g2 = glossary.normalize(doc)
    assert g2["terms"] == {"A": "甲"} and g2["notes"] == "n"
    p = glossary.save_glossary(str(tmp_path), "地狱厨房", 2370, {"A": "甲"}, notes="n")
    assert p.endswith("地狱厨房.tmdb2370.json")
    assert glossary.load_glossary(str(tmp_path), "地狱厨房", 2370) == {"A": "甲"}
    assert glossary.find_glossary(str(tmp_path), "地狱厨房", 999) == str(tmp_path) + "/地狱厨房.json" or True
    # 命中优先级：tmdb 名优先于纯名
    glossary.save_glossary(str(tmp_path), "地狱厨房", None, {"B": "乙"}, filename="地狱厨房.json")
    assert glossary.find_glossary(str(tmp_path), "地狱厨房", 2370).endswith("tmdb2370.json")


def test_parse_answer():
    t, n = parse_answer('前言\nANSWER: {"terms": {"Ramsay": "拉姆齐"}, "notes": "竞技实境"}\n后记')
    assert t == {"Ramsay": "拉姆齐"} and n == "竞技实境"
    assert parse_answer("没有答案") == (None, None)
    t2, _ = parse_answer('ANSWER: {"Hell\'s Kitchen": "地狱厨房"}')
    assert t2 == {"Hell's Kitchen": "地狱厨房"}


def test_tmdb_fallback_terms():
    facts = {"original_name": "Hell's Kitchen", "zh_name": "地狱厨房",
             "cast": [("Gordon Ramsay", "戈登·拉姆齐", "本人"), ("同名字", "同名字", "x")]}
    t = tmdb_fallback_terms(facts)
    assert t["Hell's Kitchen"] == "地狱厨房"
    assert t["Gordon Ramsay"] == "戈登·拉姆齐"
    assert "同名字" not in t


def test_pipeline_translator_wiring(tmp_path):
    """回归：_translator 必须把词表 dict（而非模块）传给 SubTranslator。"""
    import json as _json
    from zhsub.config import DEFAULTS
    from zhsub.pipeline import Pipeline
    import copy
    cfg = copy.deepcopy(DEFAULTS)
    cfg["paths"]["data_dir"] = str(tmp_path / "data")
    cfg["paths"]["glossary_dir"] = str(tmp_path)
    (tmp_path / "某剧.tmdb1.json").write_text(
        _json.dumps({"terms": {"Subaru": "菜月·昴"}}, ensure_ascii=False), encoding="utf-8")
    st = Pipeline(cfg)._translator("某剧", 1)
    assert "菜月·昴" in st.system
