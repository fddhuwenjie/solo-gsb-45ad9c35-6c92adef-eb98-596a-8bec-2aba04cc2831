"""SQLite 持久化层：样本、阶梯、互斥关系、占用段、最近一次排程快照。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT NOT NULL UNIQUE,
    rh_min       REAL NOT NULL,
    rh_max       REAL NOT NULL,
    eq_rh        REAL,
    eq_duration  INTEGER NOT NULL DEFAULT 30,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS steps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id  INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    position   INTEGER NOT NULL,
    target     REAL NOT NULL,
    dwell      INTEGER NOT NULL,
    rate       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS excludes (
    sample_id  INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    other_id   INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    PRIMARY KEY (sample_id, other_id)
);
CREATE TABLE IF NOT EXISTS occupancies (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    start   INTEGER NOT NULL,
    end     INTEGER NOT NULL,
    rh      REAL NOT NULL,
    slots   INTEGER NOT NULL DEFAULT 1,
    note    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    feasible   INTEGER NOT NULL,
    payload    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    conclusion TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    payload    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS redo_schedules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    review_id   INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    base_minute INTEGER NOT NULL,
    end_minute  INTEGER NOT NULL,
    payload     TEXT NOT NULL
);
"""


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ---------------- 样本 ---------------- #
    def list_samples(self) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM samples ORDER BY id").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                rid = r['id']
                d['steps'] = [dict(x) for x in c.execute(
                    "SELECT position,target,dwell,rate FROM steps "
                    "WHERE sample_id=? ORDER BY position", (rid,))]
                d['excludes'] = [x['other_id'] for x in c.execute(
                    "SELECT other_id FROM excludes WHERE sample_id=?"
                    " ORDER BY other_id", (rid,))]
                out.append(d)
            return out

    def get_sample(self, sid: int) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM samples WHERE id=?", (sid,)).fetchone()
            if not r:
                return None
            d = dict(r)
            d['steps'] = [dict(x) for x in c.execute(
                "SELECT position,target,dwell,rate FROM steps "
                "WHERE sample_id=? ORDER BY position", (sid,))]
            d['excludes'] = [x['other_id'] for x in c.execute(
                "SELECT other_id FROM excludes WHERE sample_id=?", (sid,))]
            return d

    def upsert_sample(self, data: dict) -> int:
        with self.conn() as c:
            sid = data.get('id')
            vals = (data['code'], data['rh_min'], data['rh_max'],
                    data.get('eq_rh'), int(data.get('eq_duration', 30)))
            if sid:
                c.execute("UPDATE samples SET code=?,rh_min=?,rh_max=?,eq_rh=?,"
                          "eq_duration=? WHERE id=?", vals + (sid,))
                if c.total_changes == 0:
                    sid = None
            if not sid:
                cur = c.execute(
                    "INSERT INTO samples(code,rh_min,rh_max,eq_rh,eq_duration)"
                    " VALUES (?,?,?,?,?)", vals)
                sid = cur.lastrowid
            c.execute("DELETE FROM steps WHERE sample_id=?", (sid,))
            c.execute("DELETE FROM excludes WHERE sample_id=? OR other_id=?",
                      (sid, sid))
            for i, st in enumerate(data['steps']):
                c.execute(
                    "INSERT INTO steps(sample_id,position,target,dwell,rate)"
                    " VALUES (?,?,?,?,?)",
                    (sid, i, st['target'], int(st['dwell']), st['rate']))
            for other in set(data.get('excludes') or []):
                if int(other) != int(sid):
                    c.execute("INSERT OR IGNORE INTO excludes(sample_id,other_id)"
                              " VALUES (?,?)", (sid, int(other)))
            return int(sid)

    def delete_sample(self, sid: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM excludes WHERE sample_id=? OR other_id=?",
                      (sid, sid))
            c.execute("DELETE FROM steps WHERE sample_id=?", (sid,))
            c.execute("DELETE FROM samples WHERE id=?", (sid,))

    # ---------------- 占用 ---------------- #
    def list_occ(self) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM occupancies ORDER BY start,id")]

    def add_occ(self, d: dict) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO occupancies(start,end,rh,slots,note)"
                " VALUES (?,?,?,?,?)",
                (int(d['start']), int(d['end']), d['rh'],
                 int(d.get('slots', 1)), d.get('note', '')))
            return int(cur.lastrowid)

    def delete_occ(self, oid: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM occupancies WHERE id=?", (oid,))

    # ---------------- 设置/快照 ---------------- #
    def get_capacity(self) -> int:
        with self.conn() as c:
            r = c.execute("SELECT value FROM settings WHERE key='capacity'").fetchone()
            return int(r['value']) if r else 3

    def set_capacity(self, cap: int) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO settings(key,value) VALUES('capacity',?)"
                      " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (str(int(cap)),))

    def get_max_shift_delta(self) -> float | None:
        """每次换档最大目标湿度差值；旧库没有该设置时返回 None（不限制）。"""
        with self.conn() as c:
            r = c.execute(
                "SELECT value FROM settings WHERE key='max_shift_delta'"
            ).fetchone()
            return float(r['value']) if r else None

    def set_max_shift_delta(self, value: float | None) -> None:
        """None = 清除该设置（不限制），兼容旧数据。"""
        with self.conn() as c:
            if value is None:
                c.execute("DELETE FROM settings WHERE key='max_shift_delta'")
            else:
                c.execute(
                    "INSERT INTO settings(key,value) VALUES('max_shift_delta',?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(float(value)),))

    def save_schedule(self, payload: dict) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO schedules(feasible,payload) VALUES (?,?)",
                (1 if payload.get('feasible') else 0,
                 json.dumps(payload, ensure_ascii=False)))
            return int(cur.lastrowid)

    def latest_schedule(self) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT payload FROM schedules ORDER BY id DESC LIMIT 1"
                          ).fetchone()
            return json.loads(r['payload']) if r else None

    # ---------------- 实测复核记录 ---------------- #
    def save_review(self, payload: dict, conclusion: str,
                    note: str = '') -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO reviews(conclusion,note,payload) VALUES (?,?,?)",
                (str(conclusion), str(note or ''),
                 json.dumps(payload, ensure_ascii=False)))
            return int(cur.lastrowid)

    def list_reviews(self, limit: int = 50) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT id,created_at,conclusion,note,payload FROM reviews"
                " ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
            out = []
            for r in rows:
                d = json.loads(r['payload'])
                # 列表视图不回传嵌入的原排程快照（体积大），仅标记其存在；
                # 单条 GET /api/reviews/{id} 仍返回完整 payload 供补做使用
                has_source = bool(d.get('source_schedule'))
                out.append({'id': r['id'], 'created_at': r['created_at'],
                            'conclusion': r['conclusion'], 'note': r['note'],
                            'summary': d.get('summary'),
                            'parameters': d.get('parameters'),
                            'has_source_schedule': has_source,
                            'payload': d})
            return out

    def get_review(self, rid: int) -> dict | None:
        with self.conn() as c:
            r = c.execute(
                "SELECT id,created_at,conclusion,note,payload FROM reviews"
                " WHERE id=?", (int(rid),)).fetchone()
            if not r:
                return None
            d = json.loads(r['payload'])
            return {'id': r['id'], 'created_at': r['created_at'],
                    'conclusion': r['conclusion'], 'note': r['note'],
                    'summary': d.get('summary'),
                    'parameters': d.get('parameters'),
                    'payload': d}

    def delete_review(self, rid: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM redo_schedules WHERE review_id=?", (int(rid),))
            c.execute("DELETE FROM reviews WHERE id=?", (int(rid),))

    # ---------------- 补做排程 ---------------- #
    def save_redo(self, payload: dict, review_id: int) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO redo_schedules(review_id,base_minute,end_minute,"
                "payload) VALUES (?,?,?,?)",
                (int(review_id), int(payload.get('base_minute', 0)),
                 int(payload.get('end_minute', 0)),
                 json.dumps(payload, ensure_ascii=False)))
            return int(cur.lastrowid)

    def list_redo(self, limit: int = 50) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT id,created_at,review_id,base_minute,end_minute,payload"
                " FROM redo_schedules ORDER BY id DESC LIMIT ?",
                (int(limit),)).fetchall()
            out = []
            for r in rows:
                d = json.loads(r['payload'])
                out.append({'id': r['id'], 'created_at': r['created_at'],
                            'review_id': r['review_id'],
                            'base_minute': r['base_minute'],
                            'end_minute': r['end_minute'],
                            'summary': {
                                'failed': len(d.get('failed_samples') or []),
                                'passed': len(d.get('passed_samples') or []),
                            },
                            'junction': d.get('junction'),
                            'payload': d})
            return out

    def get_redo(self, redo_id: int) -> dict | None:
        with self.conn() as c:
            r = c.execute(
                "SELECT id,created_at,review_id,base_minute,end_minute,payload"
                " FROM redo_schedules WHERE id=?", (int(redo_id),)).fetchone()
            if not r:
                return None
            d = json.loads(r['payload'])
            return {'id': r['id'], 'created_at': r['created_at'],
                    'review_id': r['review_id'],
                    'base_minute': r['base_minute'],
                    'end_minute': r['end_minute'], 'payload': d}

    def list_redo_for_review(self, review_id: int) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT id,created_at,review_id,base_minute,end_minute,payload"
                " FROM redo_schedules WHERE review_id=? ORDER BY id",
                (int(review_id),)).fetchall()
            return [{'id': r['id'], 'created_at': r['created_at'],
                     'review_id': r['review_id'],
                     'base_minute': r['base_minute'],
                     'end_minute': r['end_minute'],
                     'payload': json.loads(r['payload'])} for r in rows]

    def delete_redo(self, redo_id: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM redo_schedules WHERE id=?", (int(redo_id),))
