"""SQLite 记账：已处理历史（幂等）与失败列表。"""

import os
import sqlite3
import time


class Store:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.db = sqlite3.connect(os.path.join(data_dir, "zhsub.db"))
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
        row = self.db.execute(
            "SELECT media_mtime, kind, out_path FROM processed WHERE path=?", (path,)
        ).fetchone()
        if row and row[0] is not None and abs(row[0] - (media_mtime or 0)) < 1:
            return row
        return None

    def mark(self, path: str, media_mtime: float, kind: str, out_path: str,
             lines: int, seconds: float):
        self.db.execute(
            "INSERT OR REPLACE INTO processed VALUES(?,?,?,?,?,?,?)",
            (path, media_mtime, kind, out_path, lines, seconds,
             time.strftime("%F %T")),
        )
        self.db.commit()

    def fail(self, path: str, reason: str):
        self.db.execute(
            "INSERT INTO failures VALUES(?,?,?)",
            (path, reason[:500], time.strftime("%F %T")),
        )
        self.db.commit()

    def stats(self):
        done = self.db.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
        by_kind = self.db.execute(
            "SELECT kind, COUNT(*) FROM processed GROUP BY kind"
        ).fetchall()
        fails = self.db.execute("SELECT COUNT(*) FROM failures").fetchone()[0]
        return {"done": done, "by_kind": dict(by_kind), "failures": fails}
