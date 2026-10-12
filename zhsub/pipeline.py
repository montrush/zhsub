"""单集处理流水线：探针选轨 → 抽取(中文) / 翻译(外文) → 写字幕文件 → 记账。"""

import hashlib
import os
import time
from datetime import datetime

from . import extract, glossary, paths, probe, srtio, store
from .translate import BudgetExceeded, SubTranslator, build_client


def _within_hours(ts: str, hours: float) -> bool:
    """'YYYY-MM-DD HH:MM:SS' 是否在最近 hours 小时内（解析失败按不久前处理）。"""
    try:
        t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return True
    return (datetime.now() - t).total_seconds() < hours * 3600


class Pipeline:
    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.log = log
        self.store = store.Store(cfg["paths"]["data_dir"])

    # ---- helpers -------------------------------------------------------
    def _translator(self, series: str, tmdb_id=None, deadline: float = None) -> SubTranslator:
        t = self.cfg["translate"]
        terms = glossary.load_glossary(self.cfg["paths"]["glossary_dir"], series, tmdb_id)
        ai = build_client(t, num_ctx=t.get("num_ctx", 4096), log=self.log)
        return SubTranslator(ai, glossary=terms, batch_size=t.get("batch_size", 10),
                             context_window=t.get("context_window", 2), log=self.log,
                             deadline=deadline,
                             batch_retries=t.get("batch_retries", 3))

    def _media_mtime(self, local_path: str, media: str) -> float:
        for p in (media, local_path):
            try:
                return os.path.getmtime(p)
            except OSError:
                continue
        return 0.0

    def _ensure_glossary(self, series: str, tmdb_id=None):
        """翻译前保证有词表：缺失时（配置允许）先联网收集译名资料。"""
        if not series:
            return
        if glossary.find_glossary(self.cfg["paths"]["glossary_dir"], series, tmdb_id):
            return
        r = self.cfg.get("research") or {}
        if not (r.get("enabled") and r.get("auto", True)):
            return
        self.log(f"[research] 《{series}》缺词表，先联网收集译名资料…")
        try:
            from .research import Researcher
            Researcher(self.cfg, log=self.log).collect(series, tmdb_id)
        except Exception as e:  # noqa: BLE001 - 资料收集失败不阻塞翻译
            self.log(f"[research] 收集失败（继续翻译，稍后可重试）: {e}")

    # ---- main ----------------------------------------------------------
    def process(self, local_path: str, item: dict = None) -> dict:
        """处理一个条目（strm 或真实媒体文件）。返回 {status, kind, out, detail}。

        status: done / skip / fail
        kind:   already(已有中字) / extract(内嵌抽取) / translate(LLM 翻译) /
                need_asr(无文本轨，二期) / probe_failed ...
        """
        item = item or {}
        series = item.get("series") or ""
        markers = self.cfg["detect"]["zh_markers"]
        extract_out, ai_out = paths.sub_output_paths(local_path)
        t0 = time.time()

        media = paths.resolve_media(local_path, self.cfg["paths"]["path_map"])
        if not media or not os.path.exists(media):
            reason = f"媒体文件不存在: {media or local_path}"
            self.store.fail(local_path, reason)
            return {"status": "fail", "kind": "no_media", "out": "", "detail": reason}

        mtime = self._media_mtime(local_path, media)

        # 幂等：处理过且媒体没变 / 产物已存在 / 兄弟中字已出现
        if self.store.done(local_path, mtime):
            return {"status": "skip", "kind": "done_before", "out": "", "detail": "已处理过"}
        if os.path.exists(extract_out) or os.path.exists(ai_out) \
                or paths.sibling_zh(local_path, markers):
            self.store.mark(local_path, mtime, "already", extract_out if os.path.exists(extract_out) else ai_out, 0, 0)
            return {"status": "skip", "kind": "already", "out": "", "detail": "已有中文字幕"}

        # 失败冷却：探测/翻译类失败 N 小时内不重试（媒体缺失例外——文件一出现就该重试）
        retry_hours = float(self.cfg["detect"].get("fail_retry_hours", 24))
        if retry_hours > 0:
            last = self.store.last_retryable_fail(local_path)
            if last and _within_hours(last[0], retry_hours):
                return {"status": "skip", "kind": "recent_fail", "out": "",
                        "detail": f"{last[0]} 失败过（{last[1][:60]}），冷却中"}

        streams = probe.ffprobe_streams(media)
        if streams is None:
            reason = "ffprobe 失败（文件损坏或挂载不可读）"
            self.store.fail(local_path, reason)
            return {"status": "fail", "kind": "probe_failed", "out": "", "detail": reason}

        tracks = probe.subtitle_tracks(streams)

        # ① 内嵌中文字幕：直接抽取（空壳轨换下一轨）
        min_bytes = self.cfg["detect"].get("min_extract_bytes", 2000)
        chi = probe.pick_chi_text_track(tracks)
        if chi:
            candidates = [t for t in tracks if t[4] and t[1] and
                          (t[1].startswith("zh") or t[1] in ("chi", "zho"))]
            candidates.sort(key=lambda t: 0 if probe.SIMPLIFIED_RE.search(t[2]) else 1)
            for idx, _lang, _title, _codec, _txt in candidates:
                if extract.extract_track(media, idx, extract_out):
                    if not extract.looks_empty(extract_out, min_bytes):
                        self.store.mark(local_path, mtime, "extract", extract_out, 0,
                                        time.time() - t0)
                        self.log(f"[extract] {os.path.basename(local_path)} ← 轨#{idx}")
                        return {"status": "done", "kind": "extract", "out": extract_out,
                                "detail": f"内嵌中字轨#{idx}"}
            self.log(f"[warn] {os.path.basename(local_path)} 中字轨均为空壳，降级翻译")

        # ② 内嵌外文字幕 → 本地 LLM 翻译（译名资料收集 → 批翻译）
        foreign = probe.pick_foreign_text_track(tracks)
        if foreign:
            self._ensure_glossary(series, item.get("tmdb_id"))
            budget_min = self.cfg["translate"].get("per_item_budget_min", 60)
            deadline = t0 + budget_min * 60
            src_srt = extract_out + ".src.tmp"
            try:
                if not extract.extract_track(media, foreign[0], src_srt):
                    raise RuntimeError(f"外文字幕轨#{foreign[0]} 抽取失败")
                with open(src_srt, "r", encoding="utf-8", errors="replace") as f:
                    cues = srtio.parse_srt(f.read())
                work = [(c, c.as_line()) for c in cues if c.as_line()]
                translator = self._translator(series, item.get("tmdb_id"),
                                              deadline=deadline)
                lines = [w[1] for w in work]
                # 断点续跑：按批落盘已译行（中断/预算杀死后重跑不浪费）
                ckpt_path = os.path.join(
                    self.cfg["paths"]["data_dir"], "ckpt",
                    hashlib.sha1(local_path.encode("utf-8")).hexdigest()[:16] + ".json")
                os.makedirs(os.path.dirname(ckpt_path), exist_ok=True)

                def guard(done_n, total_n):
                    if time.time() > deadline:
                        raise BudgetExceeded(f"超过单集预算 {budget_min} 分钟")

                translated = translator.translate_lines(lines, progress=guard,
                                                         ckpt_path=ckpt_path,
                                                         ckpt_meta=mtime)
                for (cue, _), zh in zip(work, translated):
                    cue.set_line(zh)
                tmp_out = ai_out + ".part"
                with open(tmp_out, "w", encoding="utf-8") as f:
                    f.write(srtio.dumps_srt(cues))
                os.replace(tmp_out, ai_out)
                st = translator.stats
                self.store.mark(local_path, mtime, "translate", ai_out, len(lines),
                                time.time() - t0)
                try:
                    os.remove(ckpt_path)
                except OSError:
                    pass
                self.log(f"[translate] {os.path.basename(local_path)} "
                         f"{len(lines)}行 / 批{st['batch_ok']} 修复{st['repair_ok']} "
                         f"重试{st['retry_ok']} 单行{st['single_fallback']} "
                         f"/ {time.time()-t0:.0f}s ← 轨#{foreign[0]}({foreign[1]})")
                return {"status": "done", "kind": "translate", "out": ai_out,
                        "detail": f"{len(lines)} 行"}
            except BudgetExceeded as e:
                self.store.fail(local_path, str(e))
                return {"status": "fail", "kind": "budget", "out": "", "detail": str(e)}
            except Exception as e:  # noqa: BLE001
                reason = f"翻译失败: {e}"
                self.store.fail(local_path, reason)
                return {"status": "fail", "kind": "translate_error", "out": "", "detail": reason}
            finally:
                try:
                    os.remove(src_srt)
                except OSError:
                    pass

        # ③ 无可用文本轨 → ASR 通路（二期）
        detail = "无文本字幕轨（图形轨或无轨），待 ASR"
        return {"status": "skip", "kind": "need_asr", "out": "", "detail": detail}
