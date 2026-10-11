"""夜窗 worker：平移 driver v5 的调度策略（窗口/批处理/卡死兜底/每日一扫）。

cfg 可以是 dict，也可以是返回 dict 的 callable（WebUI 传入热更新配置的取值函数），
每轮循环开始时重新取用，设置页保存的配置下一轮即生效。
"""

import time

from . import scan as scan_mod
from .pipeline import Pipeline

WORKABLE_KINDS = {"chi_text", "foreign_text", "unprobed", "probe_failed", None}


def _hhmm(dt=None) -> int:
    return int(time.strftime("%H%M", time.localtime(time.time() if dt is None else dt)))


class Worker:
    def __init__(self, cfg, log=print):
        self._cfg = cfg
        self.log = log

    @property
    def cfg(self) -> dict:
        return self._cfg() if callable(self._cfg) else self._cfg

    def _refresh(self):
        """每轮循环重读配置：夜窗参数 + 流水线（翻译端点等改动即热生效）。"""
        sch = self.cfg["schedule"]
        self.win = tuple(sch.get("window") or ["01:00", "07:00"])
        self.last_fire = sch.get("last_fire", "06:00")
        self.interval = max(60, int(sch.get("scan_interval_min", 5)) * 60)
        self.max_age = float(sch.get("rescanned_hours", 24))
        self.pipe = Pipeline(self.cfg, log=self.log)

    def in_window(self, hhmm: int = None) -> bool:
        c = _hhmm() if hhmm is None else hhmm
        a, b = int(self.win[0].replace(":", "")), int(self.win[1].replace(":", ""))
        return (a <= c <= b) if a <= b else (c >= a or c <= b)

    def _candidates(self, source: str):
        cached = scan_mod.load_latest(self.cfg["paths"]["data_dir"], self.max_age)
        if cached:
            self.log(f"使用缓存清单（{cached.get('generated_at')}，{len(cached['items'])} 条）")
            return cached
        self.log(f"扫描生成候选清单（source={source}）...")
        result = scan_mod.scan(self.cfg, source=source, do_probe=True, log=self.log)
        path = scan_mod.save(result, self.cfg["paths"]["data_dir"])
        miss = sum(s.get("missing", 0) for s in result["summary"].values())
        self.log(f"扫描完成：缺中字 {miss} 条 → {path}")
        return result

    def run(self, source: str = "emby", once: bool = False):
        self._refresh()
        self.log(f"zhsub worker 启动（夜窗 {self.win[0]}–{self.win[1]}，"
                 f"最晚开新集 {self.last_fire}）")
        while True:
            self._refresh()
            if not self.in_window():
                if once:
                    self.log("不在夜窗内，退出（--once）")
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
                if not self.in_window() or _hhmm() > int(self.last_fire.replace(":", "")):
                    self.log("到达窗口边界，收工")
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
