"""SQLite 记账：已处理历史（幂等）与失败列表。线程安全（WebUI + worker 共用）。"""

import os
import sqlite3
import threading
import time


class Store:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(os.path.join(data_dir, "zhsub.db"),
                                  check_same_thread=False)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS processed(
                path TEXT PRIMARY KEY,
                media_mtime REAL,
                kind TEXT,
                out_path TEXT,
                lines INTEGER,
                seconds REAL,
                finished_at TEXT
            )"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS failures(
                path TEXT,
                reason TEXT,
                ts TEXT
            )"""
        )
        self.db.commit()

    def done(self, path: str, media_mtime: float):
        """该文件处理过且媒体未变化时返回记录，否则 None。"""
        with self._lock:
            row = self.db.execute(
                "SELECT media_mtime, kind, out_path FROM processed WHERE path=?", (path,)
            ).fetchone()
        if row and row[0] is not None and abs(row[0] - (media_mtime or 0)) < 1:
            return row
        return None

    def mark(self, path: str, media_mtime: float, kind: str, out_path: str,
             lines: int, seconds: float):
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO processed VALUES(?,?,?,?,?,?,?)",
                (path, media_mtime, kind, out_path, lines, seconds,
                 time.strftime("%F %T")),
            )
            self.db.commit()

    def fail(self, path: str, reason: str):
        with self._lock:
            now = time.strftime("%F %T")
            row = self.db.execute(
                "SELECT reason FROM failures WHERE path=? AND ts>=? "
                "ORDER BY rowid DESC LIMIT 1",
                (path, now[:13] + ":00"),
            ).fetchone()
            if row and row[0] == reason[:500]:
                return  # 同一小时内同原因失败只记一次，别让失败表无限膨胀
            self.db.execute("INSERT INTO failures VALUES(?,?,?)",
                            (path, reason[:500], now))
            self.db.commit()

    def last_retryable_fail(self, path: str):
        """最近一次「值得冷却后再试」的失败 (ts, reason)；媒体缺失不算（文件一出现就该重试）。"""
        with self._lock:
            row = self.db.execute(
                "SELECT ts, reason FROM failures WHERE path=? "
                "AND reason NOT LIKE '媒体文件不存在%' "
                "ORDER BY rowid DESC LIMIT 1",
                (path,),
            ).fetchone()
        return row

    def stats(self):
        with self._lock:
            done = self.db.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
            by_kind = self.db.execute(
                "SELECT kind, COUNT(*) FROM processed GROUP BY kind"
            ).fetchall()
            fails = self.db.execute("SELECT COUNT(*) FROM failures").fetchone()[0]
        return {"done": done, "by_kind": dict(by_kind), "failures": fails}
