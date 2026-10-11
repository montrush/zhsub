"""排班制夜窗调度测试（统一/分日窗口、跨零点、收尾保护、YAML 段重写）。"""

from datetime import datetime

import yaml

from zhsub.scheduler import (WEEKDAYS, in_window, last_fire_ok, parse_days,
                             parse_hhmm, render_schedule_section,
                             replace_yaml_section, window_desc)

# 2026-10-09=周五 10-10=周六 10-11=周日 10-12=周一 10-14=周三
def f(h, m):
    return datetime(2026, 10, 9, h, m)    # 周五

def sa(h, m):
    return datetime(2026, 10, 10, h, m)   # 周六

def su(h, m):
    return datetime(2026, 10, 11, h, m)   # 周日

def mo(h, m):
    return datetime(2026, 10, 12, h, m)   # 周一

def we(h, m):
    return datetime(2026, 10, 14, h, m)   # 周三


def days_from(default=("01:00", "07:00"), **overrides):
    return parse_days({"window": list(default), "windows": overrides})


def test_parse_hhmm():
    assert parse_hhmm("01:00") == (1, 0)
    assert parse_hhmm("2359") == (23, 59)
    assert parse_hhmm("24:00") == (24, 0)
    for bad in ("24:30", "25:00", "1:00", "ab:cd", "12:60"):
        try:
            parse_hhmm(bad)
            assert False, bad
        except ValueError:
            pass


def test_unified_default_window():
    d = parse_days({})
    assert in_window(d, mo(3, 0))
    assert not in_window(d, mo(0, 30))
    assert not in_window(d, mo(7, 0))  # 结束时刻开区间


def test_overnight_window_belongs_to_start_day():
    d = days_from(**{"fri": ["23:00", "09:00"]})
    assert in_window(d, f(23, 30))
    assert in_window(d, sa(8, 59))     # 周五的班跨到周六上午
    assert not in_window(d, sa(9, 0))
    assert not in_window(d, f(22, 59))
    assert not in_window(d, sa(23, 30))  # 周六没排 → 用统一窗口，23:30 不在
    assert in_window(d, su(3, 0))        # 周日走统一窗口


def test_off_day():
    d = days_from(**{"wed": []})
    assert not in_window(d, we(3, 0))
    assert not in_window(d, we(23, 0))
    assert in_window(d, mo(3, 0))  # 其他天照常


def test_full_day_2400_end():
    d = days_from(default=("00:00", "24:00"))
    assert in_window(d, datetime(2026, 10, 12, 0, 0))
    assert in_window(d, datetime(2026, 10, 12, 23, 59))
    # 周一全天每小时都在窗内
    assert all(in_window(d, datetime(2026, 10, 12, h, m))
               for h in range(24) for m in (0, 30))


def test_last_fire_guard():
    d = parse_days({})
    assert last_fire_ok(d, 60, mo(6, 0))     # 剩 60 分钟 → 可以
    assert not last_fire_ok(d, 60, mo(6, 1))
    assert not last_fire_ok(d, 90, mo(6, 0))
    assert last_fire_ok(d, 0, mo(6, 59))     # 0 = 干满全程
    assert not last_fire_ok(d, 60, mo(9, 0))  # 窗外本就不开新集


def test_malformed_config_becomes_rest_day():
    d = parse_days({"window": ["01:00", "07:00"], "windows": {"fri": ["bad", "xx"]}})
    assert d["fri"] is None
    assert window_desc(d["fri"]) == "休息"


def test_window_desc():
    assert window_desc(parse_days({})["mon"]) == "01:00–07:00"
    assert window_desc(((23, 0), (9, 0))) == "23:00–09:00（跨零点）"
    assert window_desc(((1, 30), (6, 30))) == "01:30–06:30"
    assert window_desc(None) == "休息"


def test_replace_yaml_section():
    text = 'emby:\n  base_url: x  # 注释保留\n\nschedule:\n  window: ["01:00", "07:00"]\n  last_fire: "06:00"\n\nweb:\n  port: 8085\n'
    block = 'schedule:\n  window: ["00:00", "23:59"]'
    out = replace_yaml_section(text, "schedule", block)
    doc = yaml.safe_load(out)
    assert doc["schedule"] == {"window": ["00:00", "23:59"]}
    assert doc["web"] == {"port": 8085}
    assert "# 注释保留" in out
    # 段不存在则追加
    out2 = replace_yaml_section("a: 1\n", "schedule", block)
    assert yaml.safe_load(out2)["schedule"]["window"] == ["00:00", "23:59"]


def _sample_days():
    return {"mon": ("01:00", "07:00"), "tue": ("01:00", "07:00"),
            "wed": ("02:00", "05:00"), "thu": None,
            "fri": ("23:00", "09:00"), "sat": ("23:00", "09:00"), "sun": None}


def test_render_section_per_day_roundtrip():
    cur = {"schedule": {"scan_interval_min": 5, "rescanned_hours": 24}}
    block = render_schedule_section(cur, _sample_days(), last_fire_min=30)
    doc = yaml.safe_load(block)
    assert doc["schedule"]["windows"]["wed"] == ["02:00", "05:00"]
    assert doc["schedule"]["windows"]["thu"] == []
    assert doc["schedule"]["windows"]["fri"] == ["23:00", "09:00"]
    assert doc["schedule"]["last_fire_min"] == 30
    assert doc["schedule"]["scan_interval_min"] == 5
    # 渲染结果能被 parse_days 原样读回
    days = parse_days(doc["schedule"])
    assert in_window(days, we(3, 0))
    assert not in_window(days, we(6, 0))
    assert in_window(days, sa(8, 0))
    assert days["thu"] is None


def test_render_section_uniform_collapses():
    days = {d: ("01:00", "07:00") for d in WEEKDAYS}
    doc = yaml.safe_load(render_schedule_section({"schedule": {}}, days, last_fire_min=60))
    assert doc["schedule"]["windows"] == {}
    assert doc["schedule"]["window"] == ["01:00", "07:00"]
