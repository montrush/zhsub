"""zhsub WebUI：配置 / 扫描 / 处理 / 词表库 / 研究资料收集。

单进程：uvicorn + 夜窗 worker 线程（可用 --no-worker 关掉）。
仅面向家庭内网，无鉴权——不要暴露到公网。
"""

import json
import threading
import time
from collections import deque
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from . import __version__, glossary, scan as scan_mod, store as store_mod
from .config import DEFAULTS, load_config
from .pipeline import Pipeline

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
            Worker(state["cfg"], log=log).run(source="emby")
        except Exception as e:  # noqa: BLE001
            log(f"[worker] 线程退出: {e}")

    if start_worker:
        threading.Thread(target=worker_loop, daemon=True).start()
        log("zhsub WebUI 启动（夜窗 worker 线程已随启）")

    app = FastAPI(title="zhsub")

    # ---------- 页面 ----------
    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    # ---------- 状态 ----------
    @app.get("/api/status")
    def status():
        st = store_mod.Store(state["cfg"]["paths"]["data_dir"]).stats()
        latest = scan_mod.load_latest(state["cfg"]["paths"]["data_dir"])
        return {
            "version": __version__,
            "store": st,
            "last_scan": {k: latest[k] for k in ("generated_at", "source", "summary")} if latest else None,
            "glossaries": len(glossary.list_glossaries(state["cfg"]["paths"]["glossary_dir"])),
            "worker_enabled": start_worker,
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

    @app.post("/api/scan")
    def api_scan(req: ScanReq):
        def run(source, probe, probe_limit):
            r = scan_mod.scan(state["cfg"], source=source, do_probe=probe,
                              probe_limit=probe_limit, log=log)
            path = scan_mod.save(r, state["cfg"]["paths"]["data_dir"])
            miss = sum(s.get("missing", 0) for s in r["summary"].values())
            return {"missing": miss, "summary": r["summary"], "file": path}
        return {"job": spawn("scan", run, source=req.source, probe=req.probe,
                             probe_limit=req.probe_limit)}

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
        out = json.loads(json.dumps(doc))
        for sec in out.values():
            if isinstance(sec, dict):
                for k in keys:
                    if sec.get(k):
                        sec[k] = "******"
        return out

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

    return app
