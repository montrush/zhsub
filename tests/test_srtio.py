from zhsub import paths, srtio
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
