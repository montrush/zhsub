"""夜窗 worker：排班式调度（统一/分日窗口、跨零点、开新集保护、卡死兜底）。

排班模型：schedule.window 是统一默认窗口；schedule.windows 按 mon..sun 单独
覆盖（[] = 当天休息）；窗口可跨零点（如 fri 23:00–09:00 属于周五，覆盖到周六
上午 9 点）。last_fire_min = 距窗口结束 N 分钟内不再开新集（做完手头为止）。

cfg 可以是 dict，也可以是返回 dict 的 callable（WebUI 传入热更新配置的取值
函数），每轮循环开始时重新取用，设置页保存的配置下一轮即生效。
"""

import re
import time
from datetime import datetime, time as dtime, timedelta

from . import scan as scan_mod
from .pipeline import Pipeline

WORKABLE_KINDS = {"chi_text", "foreign_text", "unprobed", "probe_failed", None}

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_CN = {"mon": "周一", "tue": "周二", "wed": "周三", "thu": "周四",
          "fri": "周五", "sat": "周六", "sun": "周日"}


def parse_hhmm(s) -> tuple:
    """'01:00' / '0100' → (1, 0)；'24:00' → (24, 0)（只允许作结束时间）。"""
    t = str(s).strip().replace(":", "")
    if len(t) != 4 or not t.isdigit():
        raise ValueError(f"时间格式应为 HH:MM：{s!r}")
    hh, mm = int(t[:2]), int(t[2:])
    if mm > 59 or hh > 24 or (hh == 24 and mm != 0):
        raise ValueError(f"时间超出范围（结束时间最大 24:00）：{s!r}")
    return hh, mm


def parse_days(sch: dict) -> dict:
    """schedule 配置 → {mon..sun: ((时,分),(时,分)) | None(休息)}。"""
    default = sch.get("window") or ["01:00", "07:00"]
    over = sch.get("windows") or {}
    days = {}
    for d in WEEKDAYS:
        w = over.get(d, default)
        try:
            days[d] = (parse_hhmm(w[0]), parse_hhmm(w[1])) if w and len(w) == 2 else None
        except (ValueError, TypeError):
            days[d] = None  # 手写配置坏了按休息处理，别让 worker 崩
    return days


def day_segments(days: dict, now: datetime):
    """把排班展开成 now 前后一周的绝对时间段 [(start, end)]（end 开区间）。"""
    segs = []
    base = now.date()
    for i, d in enumerate(WEEKDAYS):
        w = days.get(d)
        if not w:
            continue
        (ah, am), (bh, bm) = w
        for delta in (-1, 0, 1):
            day = base + timedelta(days=delta)
            if day.weekday() != i:
                continue
            start = datetime.combine(day, dtime(ah % 24, am))
            if (bh, bm) == (24, 0):
                end = datetime.combine(day + timedelta(days=1), dtime(0, 0))
            elif (ah, am) > (bh, bm):
                end = datetime.combine(day + timedelta(days=1), dtime(bh, bm))
            else:
                end = datetime.combine(day, dtime(bh, bm))
            if end > start:
                segs.append((start, end))
    return segs


def in_window(days: dict, now: datetime = None) -> bool:
    now = now or datetime.now()
    return any(s <= now < e for s, e in day_segments(days, now))


def last_fire_ok(days: dict, last_fire_min: int, now: datetime = None) -> bool:
    """False = 已进入当前窗口的收尾段，不再开新集。"""
    if last_fire_min <= 0:
        return True
    now = now or datetime.now()
    for s, e in day_segments(days, now):
        if s <= now < e:
            return (e - now).total_seconds() / 60 >= last_fire_min
    return False


def window_desc(w) -> str:
    if not w:
        return "休息"
    (ah, am), (bh, bm) = w
    return f"{ah:02d}:{am:02d}–{bh:02d}:{bm:02d}" + ("（跨零点）" if (ah, am) > (bh, bm) else "")


# ---------- YAML schedule 段的文本重写（保注释、不动其他段） ----------

def replace_yaml_section(text: str, key: str, block: str) -> str:
    """把顶层 key 段整体替换为 block；没有该段则追加到文末。与相邻段之间保持空行。"""
    lines = text.split("\n")
    pat = re.compile(rf"^{key}:\s*(#.*)?$")
    start = next((i for i, l in enumerate(lines) if pat.match(l)), None)
    if start is None:
        return text.rstrip("\n") + "\n\n" + block + "\n"
    end = start + 1
    while end < len(lines) and not re.match(r"^\S", lines[end]):
        end += 1
    new_lines = lines[:start] + block.split("\n") + lines[end:]
    # 与紧邻的上一段/下一段之间补空行（段内的空行会被并入替换范围吃掉）
    i0 = start
    if i0 > 0 and new_lines[i0 - 1].strip():
        new_lines.insert(i0, "")
    j = start + len(block.split("\n"))
    if j < len(new_lines) and new_lines[j].strip():
        new_lines.insert(j, "")
    return "\n".join(new_lines)


def render_schedule_section(cur: dict, days_input: dict,
                            last_fire_min: int = None) -> str:
    """生成重排后的 schedule 段文本。

    cur: 当前生效配置（继承 scan_interval_min / rescanned_hours / window）；
    days_input: {day: ("HH:MM","HH:MM") | None(休息)}。七天同窗时收敛为统一
    window + 空 windows，否则逐日显式列出。
    """
    sch = cur.get("schedule") or {}
    enabled = {d: w for d, w in days_input.items() if w}
    uniform = len(enabled) == len(WEEKDAYS) and len(set(enabled.values())) == 1
    window = (next(iter(enabled.values())) if uniform
              else (sch.get("window") or ["01:00", "07:00"]))
    lines = ["schedule:",
             f'  window: ["{window[0]}", "{window[1]}"]  # 统一窗口：未在 windows 单独列出的日子用它']
    if uniform:
        lines.append('  windows: {}  # 分日覆盖 mon..sun；[] = 当天不干活；可跨零点如 ["22:00","02:00"]')
    else:
        lines.append('  windows:  # 分日覆盖 mon..sun；[] = 当天不干活；可跨零点如 ["22:00","02:00"]')
        for d in WEEKDAYS:
            w = days_input.get(d)
            lines.append(f'    {d}: ["{w[0]}", "{w[1]}"]' if w else f'    {d}: []')
    lfm = max(0, int(sch.get("last_fire_min", 60)) if last_fire_min is None
              else last_fire_min)
    si = max(1, int(sch.get("scan_interval_min", 5)))
    rh = float(sch.get("rescanned_hours", 24))
    rh_s = str(int(rh)) if rh.is_integer() else str(rh)
    lines += [f'  last_fire_min: {lfm}  # 距窗口结束 N 分钟不再开新集（0=干满全程）',
              f'  scan_interval_min: {si}',
              f'  rescanned_hours: {rh_s}']
    return "\n".join(lines)


class Worker:
    def __init__(self, cfg, log=print):
        self._cfg = cfg
        self.log = log

    @property
    def cfg(self) -> dict:
        return self._cfg() if callable(self._cfg) else self._cfg

    def _refresh(self):
        """每轮循环重读配置：排班 + 流水线（翻译端点等改动即热生效）。"""
        sch = self.cfg["schedule"]
        self.days = parse_days(sch)
        self.last_fire_min = max(0, int(sch.get("last_fire_min", 60)))
        self.interval = max(60, int(sch.get("scan_interval_min", 5)) * 60)
        self.max_age = float(sch.get("rescanned_hours", 24))
        self.pipe = Pipeline(self.cfg, log=self.log)

    def _candidates(self, source: str):
        cached = scan_mod.load_latest(self.cfg["paths"]["data_dir"], self.max_age)
        if cached:
            self.log(f"使用缓存清单（{cached.get('generated_at')}，{len(cached['items'])} 条）")
            return cached
        # ⭐1013用户令"停自动扫库": scan.auto=false → worker 永不主动全库重扫（云端
        # ffprobe 17k 条要数小时吃光窗口）；用过期清单兜底，无清单则空转等手动扫描
        # （WebUI 扫描按钮 / CLI `zhsub scan [--path 目录] [--category series|movies]`）。
        if not (self.cfg.get("scan") or {}).get("auto", True):
            stale = scan_mod.load_latest(self.cfg["paths"]["data_dir"], None)
            if stale:
                self.log(f"自动扫描已关（scan.auto=false），沿用旧清单"
                         f"（{stale.get('generated_at')}，{len(stale.get('items', []))} 条）")
                return stale
            self.log("自动扫描已关且无清单：等待手动扫描（WebUI 扫描 / zhsub scan）")
            return {"items": [], "summary": {}}
        self.log(f"扫描生成候选清单（source={source}）...")
        result = scan_mod.scan(self.cfg, source=source, do_probe=True, log=self.log)
        path = scan_mod.save(result, self.cfg["paths"]["data_dir"])
        miss = sum(s.get("missing", 0) for s in result["summary"].values())
        self.log(f"扫描完成：缺中字 {miss} 条 → {path}")
        return result

    def run(self, source: str = "emby", once: bool = False):
        self._refresh()
        self.log(f"zhsub worker 启动（排班："
                 f"{' / '.join(f'{DAY_CN[d]}{window_desc(w)}' for d, w in self.days.items())}；"
                 f"收尾保护 {self.last_fire_min} 分钟）")
        while True:
            self._refresh()
            if not in_window(self.days):
                if once:
                    self.log("不在排班窗口内，退出（--once）")
                    return
                time.sleep(self.interval)
                continue
            result = self._candidates(source)
            pending = [it for it in result.get("items", [])
                       if it.get("kind") in WORKABLE_KINDS]
            if not pending:
                if once:
                    return
                time.sleep(self.interval)
                continue
            done = fail = skip = 0
            for it in pending:
                if not in_window(self.days) or \
                        not last_fire_ok(self.days, self.last_fire_min):
                    self.log("到达窗口边界/收尾段，收工")
                    break
                r = self.pipe.process(it["local_path"], it)
                if r["status"] == "done":
                    done += 1
                elif r["status"] == "fail":
                    fail += 1
                else:
                    skip += 1
            self.log(f"本轮完成：成功 {done} / 失败 {fail} / 跳过 {skip}")
            if once:
                return
            time.sleep(self.interval)
