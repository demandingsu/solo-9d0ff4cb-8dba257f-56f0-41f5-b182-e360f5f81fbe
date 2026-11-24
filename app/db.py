"""SQLite 持久层：登记读数、复测区间与诊断结果，按班次查询。

写路径均为“先算后写、单事务提交”：诊断先在内存中算完，再把
登记/复测与诊断结果放进同一事务落库；任一步失败整体回滚，
旧的读数与诊断原样保留。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("DATABASE_PATH", os.path.join("data", "app.db"))


class StaleSubmissionError(RuntimeError):
    """合报保存时发现某区已换稿（请求的上报号不再是该区最新版本）。"""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS registrations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    shift      TEXT NOT NULL,
    ports_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS readings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    registration_id INTEGER NOT NULL REFERENCES registrations(id),
    segment_key     TEXT NOT NULL,
    src             TEXT NOT NULL,
    dst             TEXT NOT NULL,
    capacity        INTEGER NOT NULL,
    meter_lo        INTEGER NOT NULL,
    meter_hi        INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS remeasurements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    shift       TEXT NOT NULL,
    segment_key TEXT NOT NULL,
    rlo         INTEGER NOT NULL,
    rhi         INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS diagnoses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    shift           TEXT NOT NULL,
    registration_id INTEGER NOT NULL REFERENCES registrations(id),
    inputs_json     TEXT NOT NULL,
    result_json     TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
-- 甲乙分区上报：每次整份替换生成一个新版本（id 即上报号）
CREATE TABLE IF NOT EXISTS zone_submissions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    zone       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS zone_readings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL REFERENCES zone_submissions(id),
    segment_key   TEXT NOT NULL,
    src           TEXT NOT NULL,
    dst           TEXT NOT NULL,
    meter_lo      INTEGER NOT NULL,
    meter_hi      INTEGER NOT NULL
);
-- 成功合报：底表、诊断与所用两个上报号同一事务入库
CREATE TABLE IF NOT EXISTS combined_reports (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    base_diagnosis_id INTEGER NOT NULL REFERENCES diagnoses(id),
    base_shift        TEXT NOT NULL,
    jia_submission_id INTEGER NOT NULL REFERENCES zone_submissions(id),
    yi_submission_id  INTEGER NOT NULL REFERENCES zone_submissions(id),
    boundary_json     TEXT NOT NULL,
    inputs_json       TEXT NOT NULL,
    result_json       TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reg_shift  ON registrations(shift, id);
CREATE INDEX IF NOT EXISTS idx_rem_shift  ON remeasurements(shift, id);
CREATE INDEX IF NOT EXISTS idx_diag_shift ON diagnoses(shift, id);
CREATE INDEX IF NOT EXISTS idx_zone_sub   ON zone_submissions(zone, id);
CREATE INDEX IF NOT EXISTS idx_comb_base  ON combined_reports(base_diagnosis_id, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with _conn() as conn:
        conn.executescript(_SCHEMA)


# ---------------- 查询 ----------------

def latest_registration(shift: str) -> dict | None:
    with _conn() as conn:
        reg = conn.execute(
            "SELECT * FROM registrations WHERE shift=? ORDER BY id DESC LIMIT 1",
            (shift,)).fetchone()
        if reg is None:
            return None
        segs = conn.execute(
            "SELECT segment_key, src, dst, capacity, meter_lo, meter_hi "
            "FROM readings WHERE registration_id=? ORDER BY id",
            (reg["id"],)).fetchall()
    return {"id": reg["id"], "shift": reg["shift"],
            "ports": json.loads(reg["ports_json"]),
            "created_at": reg["created_at"],
            "segments": [dict(s) for s in segs]}


def remeasurements_for_shift(shift: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT segment_key, rlo, rhi, created_at FROM remeasurements "
            "WHERE shift=? ORDER BY id", (shift,)).fetchall()
    return [dict(r) for r in rows]


def diagnoses_for_shift(shift: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, shift, registration_id, inputs_json, result_json, "
            "created_at FROM diagnoses WHERE shift=? ORDER BY id DESC",
            (shift,)).fetchall()
    return [{"id": r["id"], "shift": r["shift"],
             "registration_id": r["registration_id"],
             "inputs": json.loads(r["inputs_json"]),
             "result": json.loads(r["result_json"]),
             "created_at": r["created_at"]} for r in rows]


def latest_diagnosis(shift: str) -> dict | None:
    rows = diagnoses_for_shift(shift)
    return rows[0] if rows else None


def list_shifts() -> list[str]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT shift FROM registrations "
            "GROUP BY shift ORDER BY MAX(id) DESC").fetchall()
    return [r["shift"] for r in rows]


# ---------------- 写入（单事务，失败整体回滚） ----------------

def save_registration_with_diagnosis(shift: str, ports: list[str],
                                     segments: list[dict], inputs: dict,
                                     result: dict) -> int:
    """登记一班读数并连同诊断结果落库，返回诊断 id。"""
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO registrations(shift, ports_json, created_at) "
            "VALUES (?,?,?)", (shift, json.dumps(ports), _now()))
        reg_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO readings(registration_id, segment_key, src, dst, "
            "capacity, meter_lo, meter_hi) VALUES (?,?,?,?,?,?,?)",
            [(reg_id, s["key"], s["from_node"], s["to_node"], s["capacity"],
              s["meter_lo"], s["meter_hi"]) for s in segments])
        cur = conn.execute(
            "INSERT INTO diagnoses(shift, registration_id, inputs_json, "
            "result_json, created_at) VALUES (?,?,?,?,?)",
            (shift, reg_id, json.dumps(inputs, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False), _now()))
        return cur.lastrowid


def save_remeasures_with_diagnosis(shift: str, remeasures: list[dict],
                                   registration_id: int, inputs: dict,
                                   result: dict) -> int:
    """补登复测区间并与新诊断同事务生效，返回诊断 id。"""
    with _conn() as conn:
        conn.executemany(
            "INSERT INTO remeasurements(shift, segment_key, rlo, rhi, "
            "created_at) VALUES (?,?,?,?,?)",
            [(shift, r["segment_key"], r["rlo"], r["rhi"], _now())
             for r in remeasures])
        cur = conn.execute(
            "INSERT INTO diagnoses(shift, registration_id, inputs_json, "
            "result_json, created_at) VALUES (?,?,?,?,?)",
            (shift, registration_id, json.dumps(inputs, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False), _now()))
        return cur.lastrowid


# ---------------- 分区上报（整份替换，版本即上报号） ----------------

def save_zone_submission(zone: str, readings: list[dict]) -> int:
    """整份保存一区读数并生成新版本，返回上报号（submission id）。"""
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO zone_submissions(zone, created_at) VALUES (?,?)",
            (zone, _now()))
        submission_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO zone_readings(submission_id, segment_key, src, dst, "
            "meter_lo, meter_hi) VALUES (?,?,?,?,?,?)",
            [(submission_id, r["segment_key"], r["from_node"], r["to_node"],
              r["meter_lo"], r["meter_hi"]) for r in readings])
        return submission_id


def _load_submission(conn: sqlite3.Connection, submission_id: int) -> dict | None:
    sub = conn.execute(
        "SELECT id, zone, created_at FROM zone_submissions WHERE id=?",
        (submission_id,)).fetchone()
    if sub is None:
        return None
    rows = conn.execute(
        "SELECT segment_key, src, dst, meter_lo, meter_hi "
        "FROM zone_readings WHERE submission_id=? ORDER BY id",
        (submission_id,)).fetchall()
    return {"id": sub["id"], "zone": sub["zone"],
            "created_at": sub["created_at"],
            "readings": [dict(r) for r in rows]}


def get_zone_submission(submission_id: int) -> dict | None:
    with _conn() as conn:
        return _load_submission(conn, submission_id)


def latest_zone_submission_id(zone: str) -> int | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT MAX(id) AS m FROM zone_submissions WHERE zone=?",
            (zone,)).fetchone()
    return row["m"] if row and row["m"] is not None else None


def latest_zone_submission(zone: str) -> dict | None:
    sid = latest_zone_submission_id(zone)
    if sid is None:
        return None
    with _conn() as conn:
        return _load_submission(conn, sid)


def list_zone_submissions(zone: str) -> list[dict]:
    """该区全部上报版本（新的在前），逐条装载读数。"""
    with _conn() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM zone_submissions WHERE zone=? ORDER BY id DESC",
            (zone,)).fetchall()]
    return [s for s in (get_zone_submission(i) for i in ids) if s]


def get_diagnosis_by_id(diagnosis_id: int) -> dict | None:
    with _conn() as conn:
        r = conn.execute(
            "SELECT id, shift, registration_id, inputs_json, result_json, "
            "created_at FROM diagnoses WHERE id=?", (diagnosis_id,)).fetchone()
    if r is None:
        return None
    return {"id": r["id"], "shift": r["shift"],
            "registration_id": r["registration_id"],
            "inputs": json.loads(r["inputs_json"]),
            "result": json.loads(r["result_json"]),
            "created_at": r["created_at"]}


# ---------------- 合报 ----------------

def save_combined_report(base_diagnosis_id: int, base_shift: str,
                         jia_submission_id: int, yi_submission_id: int,
                         boundary_keys: list[str], inputs: dict,
                         result: dict) -> int:
    """合报结果单事务入库，并在同一事务内复查两区是否已换稿。

    任一区的最新上报号与请求指定的上报号不一致 -> StaleSubmissionError，
    事务回滚，不写入任何合报记录（失败时原合报结果仍可查）。
    """
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for zone, wanted in (("甲", jia_submission_id),
                                 ("乙", yi_submission_id)):
                row = conn.execute(
                    "SELECT MAX(id) AS m FROM zone_submissions WHERE zone=?",
                    (zone,)).fetchone()
                latest = row["m"] if row is not None else None
                if latest != wanted:
                    raise StaleSubmissionError(
                        f"{zone}区已换稿：请求上报号 {wanted}，"
                        f"当前最新上报号 {latest}")
            cur = conn.execute(
                "INSERT INTO combined_reports(base_diagnosis_id, base_shift, "
                "jia_submission_id, yi_submission_id, boundary_json, "
                "inputs_json, result_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (base_diagnosis_id, base_shift, jia_submission_id,
                 yi_submission_id, json.dumps(boundary_keys, ensure_ascii=False),
                 json.dumps(inputs, ensure_ascii=False),
                 json.dumps(result, ensure_ascii=False), _now()))
            report_id = cur.lastrowid
        except Exception:
            conn.rollback()
            raise
        conn.commit()
        return report_id


def _row_to_combined(r: sqlite3.Row) -> dict:
    return {"id": r["id"],
            "base_diagnosis_id": r["base_diagnosis_id"],
            "base_shift": r["base_shift"],
            "jia_submission_id": r["jia_submission_id"],
            "yi_submission_id": r["yi_submission_id"],
            "boundary_segment_keys": json.loads(r["boundary_json"]),
            "inputs": json.loads(r["inputs_json"]),
            "result": json.loads(r["result_json"]),
            "created_at": r["created_at"]}


_COMBINED_COLS = ("id, base_diagnosis_id, base_shift, jia_submission_id, "
                  "yi_submission_id, boundary_json, inputs_json, result_json, "
                  "created_at")


def combined_reports_for_base(base_diagnosis_id: int) -> list[dict]:
    """按底图（诊断号）查全部成功合报，新的在前。"""
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT {_COMBINED_COLS} FROM combined_reports "
            "WHERE base_diagnosis_id=? ORDER BY id DESC",
            (base_diagnosis_id,)).fetchall()
    return [_row_to_combined(r) for r in rows]


def latest_combined_report(base_diagnosis_id: int) -> dict | None:
    """按底图查最近一次成功合报。"""
    with _conn() as conn:
        r = conn.execute(
            f"SELECT {_COMBINED_COLS} FROM combined_reports "
            "WHERE base_diagnosis_id=? ORDER BY id DESC LIMIT 1",
            (base_diagnosis_id,)).fetchone()
    return _row_to_combined(r) if r is not None else None


def get_combined_report(report_id: int) -> dict | None:
    with _conn() as conn:
        r = conn.execute(
            f"SELECT {_COMBINED_COLS} FROM combined_reports WHERE id=?",
            (report_id,)).fetchone()
    return _row_to_combined(r) if r is not None else None
