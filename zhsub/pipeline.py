"""单集处理流水线：探针选轨 → 抽取(中文) / 翻译(外文) → 写字幕文件 → 记账。"""

import os
import time

from . import extract, paths, probe, srtio, store
from .translate import Ollama, SubTranslator, load_glossary


class BudgetExceeded(RuntimeError):
    pass


class Pipeline:
    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.log = log
        self.store = store.Store(cfg["paths"]["data_dir"])

    # ---- helpers -------------------------------------------------------
    def _translator(self, series: str) -> SubTranslator:
        t = self.cfg["translate"]
        glossary = load_glossary(self.cfg["paths"]["glossary_dir"], series)
        ai = Ollama(
            base_url=t["base_url"], model=t["model"], num_ctx=t.get("num_ctx", 4096),
            temperature=t.get("temperature", 0.2), top_p=t.get("top_p", 0.9),
            request_timeout=t.get("request_timeout", 180),
            max_retries=t.get("max_retries", 3),
        )
        return SubTranslator(ai, glossary=glossary, batch_size=t.get("batch_size", 10),
                             context_window=t.get("context_window", 2), log=self.log)

    def _media_mtime(self, local_path: str, media: str) -> float:
        for p in (media, local_path):
            try:
                return os.path.getmtime(p)
            except OSError:
                continue
        return 0.0

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

        # ② 内嵌外文字幕 → 本地 LLM 翻译
        foreign = probe.pick_foreign_text_track(tracks)
        if foreign:
            budget_min = self.cfg["translate"].get("per_item_budget_min", 60)
            deadline = t0 + budget_min * 60
            src_srt = extract_out + ".src.tmp"
            try:
                if not extract.extract_track(media, foreign[0], src_srt):
                    raise RuntimeError(f"外文字幕轨#{foreign[0]} 抽取失败")
                with open(src_srt, "r", encoding="utf-8", errors="replace") as f:
                    cues = srtio.parse_srt(f.read())
                work = [(c, c.as_line()) for c in cues if c.as_line()]
                translator = self._translator(series)
                lines = [w[1] for w in work]

                def guard(done_n, total_n):
                    if time.time() > deadline:
                        raise BudgetExceeded(f"超过单集预算 {budget_min} 分钟")

                translated = translator.translate_lines(lines, progress=guard)
                for (cue, _), zh in zip(work, translated):
                    cue.set_line(zh)
                tmp_out = ai_out + ".part"
                with open(tmp_out, "w", encoding="utf-8") as f:
                    f.write(srtio.dumps_srt(cues))
                os.replace(tmp_out, ai_out)
                st = translator.stats
                self.store.mark(local_path, mtime, "translate", ai_out, len(lines),
                                time.time() - t0)
                self.log(f"[translate] {os.path.basename(local_path)} "
                         f"{len(lines)}行 / 批{st['batch_ok']} 单行{st['single_fallback']} "
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
