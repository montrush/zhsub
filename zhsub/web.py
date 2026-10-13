"""zhsub WebUI：配置 / 扫描 / 处理 / 词表库 / 研究资料收集。

单进程：uvicorn + 夜窗 worker 线程（可用 --no-worker 关掉）。
仅面向家庭内网，无鉴权——不要暴露到公网。
"""

import json
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from . import __version__, glossary, scan as scan_mod, store as store_mod
from .config import DEFAULTS, load_config
from .pipeline import Pipeline
from .scheduler import (DAY_CN, WEEKDAYS, in_window as sched_in_window,
                        parse_hhmm, parse_days, render_schedule_section,
                        replace_yaml_section, window_desc)

STATIC = Path(__file__).parent / "static"


def serve(cfg: dict, cfg_path: str, host: str = "0.0.0.0", port: int = 8085,
          worker: bool = True):
    app = create_app(cfg, cfg_path, start_worker=worker)
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning")


def create_app(cfg: dict, cfg_path: str, start_worker: bool = True) -> FastAPI:
    state = {"cfg": cfg, "cfg_path": cfg_path}
    LOG = deque(maxlen=1500)
    JOBS = {}
    LOCK = threading.Lock()
    _logseq = [0]

    def log(msg):
        with LOCK:
            _logseq[0] += 1
            LOG.append({"seq": _logseq[0], "ts": time.strftime("%T"), "msg": str(msg)})
        print(msg, flush=True)

    def spawn(kind: str, fn, **kwargs) -> str:
        job_id = f"{kind}-{int(time.time()*1000)}"
        JOBS[job_id] = {"id": job_id, "kind": kind, "status": "running",
                        "started": time.strftime("%T"), "result": None}
        def run():
            try:
                JOBS[job_id]["result"] = fn(**kwargs)
                JOBS[job_id]["status"] = "done"
            except Exception as e:  # noqa: BLE001
                JOBS[job_id]["status"] = "error"
                JOBS[job_id]["result"] = str(e)
                log(f"[{kind}] 任务失败: {e}")
        threading.Thread(target=run, daemon=True).start()
        return job_id

    def worker_loop():
        from .scheduler import Worker
        try:
            # 传取值函数而非 dict：设置页保存的配置 worker 下一轮即热生效
            Worker(lambda: state["cfg"], log=log).run(source="emby")
        except Exception as e:  # noqa: BLE001
            log(f"[worker] 线程退出: {e}")

    if start_worker:
        threading.Thread(target=worker_loop, daemon=True).start()
        log("zhsub WebUI 启动（夜窗 worker 线程已随启）")

    app = FastAPI(title="夜译 zhsub")

    # ---------- 页面 ----------
    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    # ---------- 状态 ----------
    @app.get("/api/status")
    def status():
        st = store_mod.Store(state["cfg"]["paths"]["data_dir"]).stats()
        latest = scan_mod.load_latest(state["cfg"]["paths"]["data_dir"])
        t = state["cfg"].get("translate") or {}
        g = t.get("gpu") or {}
        return {
            "version": __version__,
            "store": st,
            "last_scan": {k: latest[k] for k in ("generated_at", "source", "summary")} if latest else None,
            "glossaries": len(glossary.list_glossaries(state["cfg"]["paths"]["glossary_dir"])),
            "worker_enabled": start_worker,
            "translate": {
                "base_url": t.get("base_url"), "model": t.get("model"),
                "gpu_enabled": bool(g.get("enabled") and g.get("base_url")),
                "gpu_base_url": g.get("base_url") or "",
                "gpu_model": g.get("model") or t.get("model") or "",
            },
        }

    @app.get("/api/log")
    def api_log(after: int = 0):
        with LOCK:
            lines = [x for x in LOG if x["seq"] > after]
        return lines

    @app.get("/api/jobs/{job_id}")
    def jobs(job_id: str):
        if job_id not in JOBS:
            raise HTTPException(404)
        return JOBS[job_id]

    # ---------- 扫描 / 处理 ----------
    class ScanReq(BaseModel):
        source: str = "emby"
        probe: bool = True
        probe_limit: int = 300
        library: str = None      # ⭐1013: 只扫指定emby媒体库(库名精确匹配)
        path: str = None        # ⭐1013: 范围扫描——只扫该目录(如单扫地狱厨房)
        category: str = None    # 'series' / 'movies' 单扫剧集或电影

    @app.post("/api/scan")
    def api_scan(req: ScanReq):
        def run(source, probe, probe_limit, library, path, category):
            log(f"手动扫描：source={source} library={library or '全部库'} "
                f"path={path or '全库'} category={category or '全部'}")
            r = scan_mod.scan(state["cfg"], source=source, do_probe=probe,
                              probe_limit=probe_limit, path=path, category=category,
                              library=library, log=log)
            path_out = scan_mod.save(r, state["cfg"]["paths"]["data_dir"])
            miss = sum(s.get("missing", 0) for s in r["summary"].values())
            return {"missing": miss, "summary": r["summary"], "file": path_out}
        return {"job": spawn("scan", run, source=req.source, probe=req.probe,
                             probe_limit=req.probe_limit, library=req.library,
                             path=req.path, category=req.category)}

    class RunReq(BaseModel):
        limit: int = 5

    @app.post("/api/run")
    def api_run(req: RunReq):
        def run(limit):
            from . import paths as paths_mod
            latest = scan_mod.load_latest(state["cfg"]["paths"]["data_dir"])
            if not latest:
                raise RuntimeError("没有候选清单，先扫描")
            items = [it for it in latest.get("items", []) if it.get("kind") in
                     ("chi_text", "foreign_text", "unprobed", "probe_failed", None)][:limit]
            pipe = Pipeline(state["cfg"], log=log)
            done = fail = skip = 0
            for it in items:
                if pipe.process(it["local_path"], it)["status"] == "done":
                    done += 1
                else:
                    skip += 1
            return {"total": len(items), "done": done, "skip_or_fail": skip}
        return {"job": spawn("run", run, limit=req.limit)}

    class ResearchReq(BaseModel):
        series: str
        tmdb_id: int = None
        force: bool = False

    @app.post("/api/research")
    def api_research(req: ResearchReq):
        from .research import Researcher
        def run(series, tmdb_id, force):
            p = Researcher(state["cfg"], log=log).collect(series, tmdb_id, force=force)
            return {"path": p}
        return {"job": spawn("research", run, series=req.series,
                             tmdb_id=req.tmdb_id, force=req.force)}

    # ---------- 词表库 ----------
    @app.get("/api/glossaries")
    def gl_list():
        return glossary.list_glossaries(state["cfg"]["paths"]["glossary_dir"])

    @app.get("/api/glossaries/{name}")
    def gl_get(name: str):
        p = (Path(state["cfg"]["paths"]["glossary_dir"]) / Path(name).name)
        if not p.is_file():
            raise HTTPException(404)
        return JSONResponse(json.loads(p.read_text(encoding="utf-8")))

    @app.put("/api/glossaries/{name}")
    async def gl_put(name: str, doc: dict):
        d = glossary.normalize(doc)
        fn = Path(name).name
        title = doc.get("title") or fn.removesuffix(".json")
        tmdb = doc.get("tmdb_id")
        p = glossary.save_glossary(state["cfg"]["paths"]["glossary_dir"], title, tmdb,
                                   d["terms"], notes=d["notes"], meta=d["meta"],
                                   filename=fn)
        return {"file": p, "terms": len(d["terms"])}

    @app.delete("/api/glossaries/{name}")
    def gl_del(name: str):
        p = Path(state["cfg"]["paths"]["glossary_dir"]) / Path(name).name
        if p.is_file():
            p.unlink()
        return {"deleted": p.name}

    # ---------- 配置 ----------
    def _mask(doc, keys=("api_key", "tmdb_api_key")):
        def rec(o):
            if isinstance(o, dict):
                return {k: ("******" if k in keys and v else rec(v))
                        for k, v in o.items()}
            if isinstance(o, list):
                return [rec(x) for x in o]
            return o
        return rec(doc)

    @app.get("/api/config")
    def cfg_get():
        return {"path": state["cfg_path"], "config": _mask(state["cfg"])}

    @app.get("/api/config_raw")
    def cfg_raw():
        return PlainTextResponse(Path(state["cfg_path"]).read_text(encoding="utf-8"))

    class CfgReq(BaseModel):
        text: str

    @app.put("/api/config")
    async def cfg_put(req: CfgReq):
        try:
            doc = yaml.safe_load(req.text)
            if not isinstance(doc, dict):
                raise ValueError("顶层必须是映射")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"YAML 无效: {e}")
        p = Path(state["cfg_path"])
        tmp = p.with_suffix(".yaml.part")
        tmp.write_text(req.text, encoding="utf-8")
        tmp.replace(p)
        state["cfg"] = load_config(str(p))
        log("[config] 配置已更新并热生效")
        return {"ok": True}

    class TestLlmReq(BaseModel):
        target: str = "gpu"  # gpu | base
        # 填了 base_url 就直接测该端点（不读配置）——设置页表单"试测"用
        base_url: str = ""
        model: str = ""
        provider: str = "ollama"
        api_key: str = ""
        num_gpu: int = None

    @app.post("/api/test_llm")
    def api_test_llm(req: TestLlmReq):
        """向指定端点发一条小对话验证连通性（不重试；90s 超时容忍模型冷加载）。"""
        from .translate import endpoint_client
        t = dict(state["cfg"].get("translate") or {})
        if req.base_url.strip():
            d = {"base_url": req.base_url.strip(),
                 "model": req.model.strip() or t.get("model"),
                 "provider": req.provider or "ollama",
                 "api_key": req.api_key or "", "num_gpu": req.num_gpu}
            desc = f"自定义 {d['base_url']}"
        elif req.target == "gpu":
            g = t.get("gpu") or {}
            if not (g.get("enabled") and g.get("base_url")):
                raise HTTPException(
                    400, "GPU 端点还没启用：在上方表单填好地址后可直接点本按钮试测"
                         "（点「应用到配置并保存」后才会成为夜窗主力）")
            d = {"base_url": g["base_url"], "model": g.get("model") or t.get("model"),
                 "provider": g.get("provider", "ollama"),
                 "api_key": g.get("api_key") or "", "num_gpu": g.get("num_gpu")}
            desc = f"GPU {d['base_url']}"
        else:
            if not t.get("base_url"):
                raise HTTPException(400, "基础端点未配置 base_url")
            d = {"base_url": t["base_url"], "model": t.get("model"),
                 "provider": t.get("provider", "ollama"),
                 "api_key": t.get("api_key") or "", "num_gpu": t.get("num_gpu")}
            desc = f"基础 {d['base_url']}"
        cli = endpoint_client(d, num_ctx=int(t.get("num_ctx", 4096)),
                              temperature=t.get("temperature", 0.2),
                              top_p=t.get("top_p", 0.9),
                              request_timeout=90, max_retries=0,
                              think=t.get("think"))
        t0 = time.time()
        try:
            reply = cli.chat("你是连通性测试助手", "只回复两个字：正常")
            return {"ok": True, "endpoint": desc, "model": d["model"],
                    "latency_ms": int((time.time() - t0) * 1000),
                    "reply": (reply or "").strip()[:100]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "endpoint": desc, "model": d.get("model"),
                    "error": str(e)[:300]}

    # ---------- 夜窗排班 ----------
    @app.get("/api/schedule")
    def api_schedule_get():
        sch = state["cfg"].get("schedule") or {}
        days = parse_days(sch)
        now = datetime.now()
        today = WEEKDAYS[now.weekday()]
        default = sch.get("window") or ["01:00", "07:00"]
        over = sch.get("windows") or {}
        days_ui = {}
        for d in WEEKDAYS:
            raw = over.get(d, default)
            ok = bool(raw) and len(raw) == 2
            days_ui[d] = {"enabled": bool(ok),
                          "start": str(raw[0]) if ok else str(default[0]),
                          "end": str(raw[1]) if ok else str(default[1])}
        return {
            "days": days_ui,
            "last_fire_min": max(0, int(sch.get("last_fire_min", 60))),
            "scan_interval_min": int(sch.get("scan_interval_min", 5)),
            "rescanned_hours": float(sch.get("rescanned_hours", 24)),
            "today": DAY_CN[today],
            "today_window": window_desc(days[today]),
            "in_window": sched_in_window(days, now),
        }

    class SchedDay(BaseModel):
        day: str
        enabled: bool = True
        start: str = "01:00"
        end: str = "07:00"

    class SchedReq(BaseModel):
        days: list[SchedDay]
        last_fire_min: int = 60

    @app.put("/api/schedule")
    async def api_schedule_put(req: SchedReq):
        by_day = {d.day: d for d in req.days}
        if len(req.days) != 7 or set(by_day) != set(WEEKDAYS):
            raise HTTPException(400, "需要 mon..sun 恰好七天的排班")
        days_map = {}
        try:
            for d in WEEKDAYS:
                item = by_day[d]
                if not item.enabled:
                    days_map[d] = None
                    continue
                s, e = parse_hhmm(item.start), parse_hhmm(item.end)
                if s == e:
                    raise ValueError(f"{DAY_CN[d]} 起止时间相同")
                if s[0] == 24:
                    raise ValueError(f"{DAY_CN[d]} 开始时间不能是 24:00")
                days_map[d] = (item.start.strip(), item.end.strip())
        except ValueError as e:
            raise HTTPException(400, str(e))
        block = render_schedule_section(state["cfg"], days_map,
                                        last_fire_min=max(0, int(req.last_fire_min)))
        p = Path(state["cfg_path"])
        new_text = replace_yaml_section(p.read_text(encoding="utf-8"), "schedule", block)
        tmp = p.with_suffix(".yaml.part")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(p)
        state["cfg"] = load_config(str(p))
        today = DAY_CN[WEEKDAYS[datetime.now().weekday()]]
        log(f"[config] 排班已更新并热生效（今天{today}："
            f"{window_desc(parse_days(state['cfg']['schedule'])[WEEKDAYS[datetime.now().weekday()]])}）")
        return {"ok": True}

    return app
