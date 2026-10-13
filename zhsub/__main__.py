"""zhsub 命令行入口。

    zhsub scan          扫描媒体库，产出缺中字清单（data/scan.json）
    zhsub run           处理清单中的条目
    zhsub worker        夜窗常驻循环（Docker 默认入口）
    zhsub status        处理历史统计
    zhsub translate-srt 翻译单个字幕文件
"""

import argparse
import os
import sys
import time

from . import scan as scan_mod
from . import srtio, store
from .config import load_config
from .glossary import load_glossary
from .pipeline import Pipeline
from .scheduler import Worker
from .translate import Ollama, SubTranslator


def cmd_scan(cfg, args):
    result = scan_mod.scan(cfg, source=args.source, do_probe=args.probe,
                           probe_limit=args.probe_limit, path=args.path,
                           category=args.category, library=args.library, log=print)
    out = scan_mod.save(result, cfg["paths"]["data_dir"], args.out)
    print(f"\n=== 扫描结果（source={result['source']}，{result['generated_at']}）===")
    for lib, s in result["summary"].items():
        print(f"  {lib}: 共 {s['total']} / 缺中字 {s['missing']}"
              + (f" / 路径未映射 {s['unresolved']}" if s.get("unresolved") else ""))
    kinds = {}
    for it in result["items"]:
        kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
    print(f"  缺中字分类: {kinds}")
    print(f"  清单已写入: {out}")
    print("  （--probe 可预判每条的处理方式：chi_text=抽取 foreign_text=翻译）")


def cmd_run(cfg, args):
    from . import paths as paths_mod
    result = scan_mod.load_latest(cfg["paths"]["data_dir"])
    if args.from_file:
        import json
        with open(args.from_file, "r", encoding="utf-8") as f:
            result = json.load(f)
    if not result:
        raise SystemExit("没有候选清单：先跑 `zhsub scan`，或用 --from 指定清单文件")
    items = [it for it in result.get("items", []) if it.get("kind") in
             ("chi_text", "foreign_text", "unprobed", "probe_failed", None)
             and not it.get("excluded")]
    if args.limit:
        items = items[:args.limit]
    pipe = Pipeline(cfg, log=print)
    done = fail = skip = 0
    for i, it in enumerate(items, 1):
        if not args.dry_run and paths_mod.sibling_zh(it["local_path"],
                                                     cfg["detect"]["zh_markers"]):
            skip += 1
            continue
        print(f"[{i}/{len(items)}] {it.get('series') or ''} {it.get('name') or ''}")
        if args.dry_run:
            media = paths_mod.resolve_media(it["local_path"], cfg["paths"]["path_map"])
            print(f"    dry-run: {it['local_path']} → {media} (kind={it.get('kind')})")
            done += 1
            continue
        r = pipe.process(it["local_path"], it)
        if r["status"] == "done":
            done += 1
        elif r["status"] == "fail":
            fail += 1
        else:
            skip += 1
            print(f"    skip({r['kind']}): {r['detail']}")
    print(f"\n处理完成：成功 {done} / 失败 {fail} / 跳过 {skip}")


def cmd_worker(cfg, args):
    Worker(cfg, log=print).run(source=args.source, once=args.once)


def cmd_status(cfg, args):
    st = store.Store(cfg["paths"]["data_dir"]).stats()
    print(f"累计已处理: {st['done']}  分类: {st['by_kind']}  失败: {st['failures']}")
    latest = scan_mod.load_latest(cfg["paths"]["data_dir"])
    if latest:
        miss = sum(s.get("missing", 0) for s in latest["summary"].values())
        print(f"最近扫描: {latest['generated_at']}（{latest['source']}）缺中字 {miss} 条")
    else:
        print("尚无扫描结果（zhsub scan）")


def cmd_research(cfg, args):
    from .research import Researcher
    p = Researcher(cfg, log=print).collect(args.series, args.tmdb, force=args.force)
    print("词表:", p or "未生成")


def cmd_web(cfg, args):
    from .web import serve
    serve(cfg, args.config or _default_cfg_path(), host=args.host, port=args.port,
          worker=not args.no_worker)


def _default_cfg_path() -> str:
    import os
    for cand in ("config.yaml", "/config/config.yaml"):
        if os.path.isfile(cand):
            return cand
    return "config.yaml"


def cmd_translate_srt(cfg, args):
    t = cfg["translate"]
    glossary = load_glossary(cfg["paths"]["glossary_dir"], args.series or "")
    tr = SubTranslator(
        Ollama(t["base_url"], t["model"], num_ctx=t.get("num_ctx", 4096),
               temperature=t.get("temperature", 0.2), top_p=t.get("top_p", 0.9),
               request_timeout=t.get("request_timeout", 180),
               max_retries=t.get("max_retries", 3), think=t.get("think"),
               num_predict=t.get("num_predict")),
        glossary=glossary, batch_size=t.get("batch_size", 10),
        context_window=t.get("context_window", 2), log=print,
    )
    with open(args.input, "r", encoding="utf-8", errors="replace") as f:
        cues = srtio.parse_srt(f.read())
    work = [(c, c.as_line()) for c in cues if c.as_line()]
    t0 = time.time()
    translated = tr.translate_lines([w[1] for w in work],
                                    progress=lambda d, n: print(f"\r  {d}/{n}", end="") or None)
    print()
    for (cue, _), zh in zip(work, translated):
        cue.set_line(zh)
    out = args.output or (os.path.splitext(args.input)[0] + ".zh.ai.srt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(srtio.dumps_srt(cues))
    print(f"完成 {len(work)} 行 / {time.time()-t0:.0f}s → {out}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="zhsub", description="给家庭媒体库自动补齐中文字幕")
    p.add_argument("--config", help="配置文件路径（默认 ./config.yaml 或 /config/config.yaml）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan", help="扫描缺中字条目（手动；可限定目录/类别）")
    sp.add_argument("--source", choices=["emby", "fs"], default="emby")
    sp.add_argument("--library", help="只扫指定 emby 媒体库（库名精确匹配，如 电视剧115/电影）")
    sp.add_argument("--path", help="只扫该目录（本地或 emby 路径前缀），如单扫地狱厨房")
    sp.add_argument("--category", choices=["series", "movies"],
                    help="只扫剧集(series)或电影(movies)")
    sp.add_argument("--probe", action="store_true", help="对缺中字条目 ffprobe 预判处理方式")
    sp.add_argument("--probe-limit", type=int, default=0, help="probe 条数上限（0=不限）")
    sp.add_argument("--out", help="清单输出路径（默认 data_dir/scan.json）")
    sp.set_defaults(fn=cmd_scan)

    sp = sub.add_parser("run", help="处理清单条目")
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--from", dest="from_file", help="指定清单文件")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("worker", help="夜窗常驻循环")
    sp.add_argument("--source", choices=["emby", "fs"], default="emby")
    sp.add_argument("--once", action="store_true", help="处理一轮即退出")
    sp.set_defaults(fn=cmd_worker)

    sub.add_parser("status", help="统计").set_defaults(fn=cmd_status)

    sp = sub.add_parser("research", help="联网收集译名资料，生成词表")
    sp.add_argument("--series", required=True, help="系列名")
    sp.add_argument("--tmdb", type=int, default=None, help="TMDB 剧集号（推荐，译名更准）")
    sp.add_argument("--force", action="store_true", help="已有词表也重新收集")
    sp.set_defaults(fn=cmd_research)

    sp = sub.add_parser("web", help="WebUI + 夜窗 worker（Docker 默认）")
    sp.add_argument("--host", default=None)
    sp.add_argument("--port", type=int, default=None)
    sp.add_argument("--no-worker", action="store_true", help="只起 WebUI，不跑 worker 线程")
    sp.set_defaults(fn=cmd_web)

    sp = sub.add_parser("translate-srt", help="翻译单个 srt")
    sp.add_argument("input")
    sp.add_argument("-o", "--output")
    sp.add_argument("--series", help="译名表名（匹配 <series>.json）")
    sp.set_defaults(fn=cmd_translate_srt)

    args = p.parse_args(argv)
    cfg = load_config(args.config)
    if args.cmd == "web":
        import os
        w = cfg.get("web") or {}
        args.host = args.host or w.get("host", "0.0.0.0")
        args.port = args.port or int(w.get("port", 8085))
    return args.fn(cfg, args)


if __name__ == "__main__":
    sys.exit(main() or 0)
