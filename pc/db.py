# -*- coding: utf-8 -*-
"""
SQLite 存储层（只用标准库）

三张表：
    individuals  个体（一只猫 / 一只狗）
    visits       每次到访（一张照片一条）
    embeddings   个体的外貌特征向量，用来把新照片认成老朋友

所有写操作走同一把锁：ThreadingHTTPServer 是多线程的，
sqlite3 连接默认不能跨线程共用。
"""

import os
import sqlite3
import threading
import time
from array import array

import config as PC

_SCHEMA = """
CREATE TABLE IF NOT EXISTS individuals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    species     TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    created_at  REAL    NOT NULL,
    note        TEXT    DEFAULT ''
);

CREATE TABLE IF NOT EXISTS visits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_id INTEGER,
    species       TEXT    NOT NULL,
    ts            REAL    NOT NULL,          -- 统计用的时刻（默认=PC 收到的时刻）
    device_ts     TEXT,                      -- K230 自己报的时间，可空
    confidence    REAL,
    box           TEXT,
    image         TEXT,                      -- 相对 DATA_DIR
    thumb         TEXT,
    sim           REAL,                      -- 和匹配到的个体的相似度
    device        TEXT,
    bytes         INTEGER,
    uid           TEXT,                      -- K230 生成的一次性 ID，用于去重
    backfill      INTEGER DEFAULT 0          -- 1 = 旧照片补传（不是"刚刚"来的）
);

CREATE TABLE IF NOT EXISTS embeddings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_id INTEGER NOT NULL,
    vec           BLOB    NOT NULL,
    dim           INTEGER NOT NULL,
    created_at    REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_visits_ts   ON visits(ts);
CREATE INDEX IF NOT EXISTS idx_visits_ind  ON visits(individual_id);
CREATE INDEX IF NOT EXISTS idx_emb_ind     ON embeddings(individual_id);
"""


class DB(object):

    def __init__(self, path=None):
        self.path = path or PC.DB_PATH
        d = os.path.dirname(self.path)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.executescript(_SCHEMA)
            self._migrate()
            self.conn.commit()

    def _migrate(self):
        """给老库补新列。不能只改 _SCHEMA —— 已有的 visits.db 不会重新建表。

        uid 的作用见 find_by_uid：客户端超时会重传同一张图，
        没有 uid 的话每次重传都会变成一条新记录（现场已经踩过）。
        """
        try:
            cols = [r["name"] for r in
                    self.conn.execute("PRAGMA table_info(visits)").fetchall()]
        except Exception:
            return
        if "uid" not in cols:
            try:
                self.conn.execute("ALTER TABLE visits ADD COLUMN uid TEXT")
            except Exception:
                pass
        if "backfill" not in cols:
            try:
                self.conn.execute(
                    "ALTER TABLE visits ADD COLUMN backfill INTEGER DEFAULT 0")
            except Exception:
                pass
        # 唯一索引放在迁移之后建：老数据 uid 全是 NULL，SQLite 允许多个 NULL，
        # 所以不会因为历史记录冲突。新记录只要 uid 重复就插不进去。
        try:
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_visits_uid "
                "ON visits(uid)")
        except Exception:
            pass

    # ------------------------------------------------ 个体
    def create_individual(self, species, name=None, now=None):
        now = now or time.time()
        if not name:
            name = self._next_name(species)
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO individuals(species, name, created_at) VALUES(?,?,?)",
                (species, name, now))
            self.conn.commit()
            return cur.lastrowid

    def _next_name(self, species):
        base = PC.SPECIES_NAMES.get(species, "未知")
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) c FROM individuals WHERE species=?", (species,)).fetchone()
        return "%s-%02d" % (base, (row["c"] or 0) + 1)

    def get_individual(self, iid):
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM individuals WHERE id=?", (iid,)).fetchone()

    def list_individuals(self):
        sql = """
        SELECT i.id, i.species, i.name, i.created_at, i.note,
               (SELECT COUNT(*) FROM visits v WHERE v.individual_id=i.id) AS visits,
               (SELECT MAX(ts)     FROM visits v WHERE v.individual_id=i.id) AS last_seen,
               (SELECT MIN(ts)     FROM visits v WHERE v.individual_id=i.id) AS first_seen,
               (SELECT thumb       FROM visits v WHERE v.individual_id=i.id
                 ORDER BY ts DESC LIMIT 1) AS thumb
        FROM individuals i
        ORDER BY last_seen DESC
        """
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def rename_individual(self, iid, name):
        with self.lock:
            self.conn.execute("UPDATE individuals SET name=? WHERE id=?", (name, iid))
            self.conn.commit()

    def delete_individual(self, iid):
        with self.lock:
            self.conn.execute("DELETE FROM individuals WHERE id=?", (iid,))
            self.conn.execute("UPDATE visits SET individual_id=NULL WHERE individual_id=?", (iid,))
            self.conn.execute("DELETE FROM embeddings WHERE individual_id=?", (iid,))
            self.conn.commit()

    def merge_individuals(self, src, dst):
        """把 src 合并进 dst：所有到访记录和特征向量都改挂到 dst 上"""
        if src == dst:
            return 0
        with self.lock:
            cur = self.conn.execute(
                "UPDATE visits SET individual_id=? WHERE individual_id=?", (dst, src))
            n = cur.rowcount
            self.conn.execute(
                "UPDATE embeddings SET individual_id=? WHERE individual_id=?", (dst, src))
            self.conn.execute("DELETE FROM individuals WHERE id=?", (src,))
            self.conn.commit()
            self._trim_embeddings(dst)
            return n

    # ------------------------------------------------ 到访
    def add_visit(self, species, ts, individual_id=None, confidence=None,
                  box=None, image=None, thumb=None, sim=None,
                  device=None, device_ts=None, nbytes=None, uid=None,
                  backfill=0):
        with self.lock:
            cur = self.conn.execute(
                """INSERT INTO visits(individual_id, species, ts, device_ts, confidence,
                                      box, image, thumb, sim, device, bytes, uid,
                                      backfill)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (individual_id, species, ts, device_ts, confidence,
                 box, image, thumb, sim, device, nbytes, uid, 1 if backfill else 0))
            self.conn.commit()
            return cur.lastrowid

    def find_by_uid(self, uid):
        """按客户端生成的 uid 查这次到访是否已经存过。

        为什么需要：K230 等响应会超时（网络慢 + 服务端要做特征提取），
        超时后它会重传**同一张照片**。没有这个查询的话，服务端每次都当成
        新到访 —— 现场表现为一张照片在网页上出现 3 次，统计全乱。
        """
        if not uid:
            return None
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM visits WHERE uid=? LIMIT 1", (uid,)).fetchone()

    def assign_visit(self, vid, individual_id):
        """人工纠正：把某次到访改挂到另一个个体上"""
        with self.lock:
            row = self.conn.execute(
                "SELECT species FROM visits WHERE id=?", (vid,)).fetchone()
            if row is None:
                return False
            self.conn.execute("UPDATE visits SET individual_id=? WHERE id=?",
                              (individual_id, vid))
            self.conn.commit()
            return True

    def get_visit(self, vid):
        with self.lock:
            return self.conn.execute("SELECT * FROM visits WHERE id=?", (vid,)).fetchone()

    def count_visits(self, individual_id):
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) c FROM visits WHERE individual_id=?",
                (individual_id,)).fetchone()["c"]

    def recent_visits(self, limit=40, individual_id=None):
        sql = """SELECT v.*, i.name AS individual_name
                 FROM visits v LEFT JOIN individuals i ON i.id=v.individual_id"""
        args = []
        if individual_id is not None:
            sql += " WHERE v.individual_id=?"
            args.append(individual_id)
        sql += " ORDER BY v.ts DESC LIMIT ?"
        args.append(int(limit))
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # ------------------------------------------------ 特征向量
    def add_embedding(self, individual_id, vec, now=None):
        now = now or time.time()
        blob = array("f", vec).tobytes()
        with self.lock:
            self.conn.execute(
                "INSERT INTO embeddings(individual_id, vec, dim, created_at) VALUES(?,?,?,?)",
                (individual_id, blob, len(vec), now))
            self.conn.commit()
            self._trim_embeddings(individual_id)

    def _trim_embeddings(self, individual_id):
        """只留最新的 N 个向量，防止库无限长"""
        with self.lock:
            self.conn.execute(
                """DELETE FROM embeddings WHERE individual_id=? AND id NOT IN
                   (SELECT id FROM embeddings WHERE individual_id=?
                    ORDER BY created_at DESC LIMIT ?)""",
                (individual_id, individual_id, PC.MAX_EMB_PER_INDIVIDUAL))
            self.conn.commit()

    def embeddings_of_species(self, species):
        """[(individual_id, [float, ...]), ...]"""
        sql = """SELECT e.individual_id, e.vec, e.dim
                 FROM embeddings e JOIN individuals i ON i.id=e.individual_id
                 WHERE i.species=?"""
        out = []
        with self.lock:
            for r in self.conn.execute(sql, (species,)).fetchall():
                a = array("f")
                a.frombytes(r["vec"])
                if len(a) == r["dim"]:
                    out.append((r["individual_id"], list(a)))
        return out

    def count_embeddings_of_species(self, species):
        sql = """SELECT COUNT(*) c FROM embeddings e
                 JOIN individuals i ON i.id=e.individual_id WHERE i.species=?"""
        with self.lock:
            return self.conn.execute(sql, (species,)).fetchone()["c"]

    # ------------------------------------------------ 汇总
    def overview(self):
        with self.lock:
            c = self.conn

            all_time = c.execute("SELECT COUNT(*) c FROM visits").fetchone()["c"]

            day_start = _local_midnight()
            today = c.execute("SELECT COUNT(*) c FROM visits WHERE ts>=?",
                              (day_start,)).fetchone()["c"]

            species = {r["species"]: r["c"] for r in c.execute(
                "SELECT species, COUNT(*) c FROM visits GROUP BY species").fetchall()}

            ind_count = {r["species"]: r["c"] for r in c.execute(
                "SELECT species, COUNT(*) c FROM individuals GROUP BY species").fetchall()}

            # 24 小时分布（全部历史）
            hourly = {"cat": [0] * 24, "dog": [0] * 24, "unknown": [0] * 24}
            rows = c.execute(
                """SELECT CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) h,
                          species, COUNT(*) c
                   FROM visits GROUP BY h, species""").fetchall()
            for r in rows:
                if r["h"] is None:
                    continue
                bucket = hourly.get(r["species"], hourly["unknown"])
                bucket[r["h"] % 24] = r["c"]

            # 最近 7 天，按天 × 物种
            days = c.execute(
                """SELECT date(ts, 'unixepoch', 'localtime') d, species, COUNT(*) c
                   FROM visits
                   WHERE ts >= ?
                   GROUP BY d, species ORDER BY d""",
                (time.time() - 7 * 86400,)).fetchall()
            daily = {}
            for r in days:
                daily.setdefault(r["d"], {"cat": 0, "dog": 0, "unknown": 0})
                daily[r["d"]][r["species"]] = r["c"]

            last = c.execute("SELECT MAX(ts) t FROM visits").fetchone()["t"]

        periods = _period_counts(row_species_hours(rows))

        return {
            "all_time": all_time,
            "today": today,
            "species": {
                "cat": species.get("cat", 0),
                "dog": species.get("dog", 0),
                "unknown": species.get("unknown", 0),
            },
            "individuals_by_species": {
                "cat": ind_count.get("cat", 0),
                "dog": ind_count.get("dog", 0),
            },
            "hourly": hourly,
            "daily": [{"date": d, "cat": v["cat"], "dog": v["dog"],
                       "unknown": v["unknown"]} for d, v in sorted(daily.items())],
            "periods": periods,
            "last_visit": last,
            "server_time": time.time(),
        }

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


# ------------------------------------------------ 小工具
def _local_midnight():
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def row_species_hours(rows):
    """把 sql 结果转成 {species: {hour: count}}"""
    out = {}
    for r in rows:
        if r["h"] is None:
            continue
        out.setdefault(r["species"], {})[int(r["h"]) % 24] = r["c"]
    return out


def _period_counts(by_species):
    out = []
    for label, lo, hi in PC.PERIODS:
        row = {"label": label, "from": lo, "to": hi, "cat": 0, "dog": 0, "unknown": 0}
        for sp, hours in by_species.items():
            if sp not in row:
                continue
            row[sp] = sum(v for h, v in hours.items() if lo <= h < hi)
        out.append(row)
    return out
