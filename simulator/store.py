# -*- coding: utf-8 -*-
"""本地持久化，存演练统计记录和行为日志"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

STATISTICS_SCHEMA_VERSION = "practice-run-statistics-v1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS practice_statistics_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_no TEXT NOT NULL,
    client_request_id TEXT NOT NULL UNIQUE,
    schema_version TEXT NOT NULL CHECK(schema_version='practice-run-statistics-v1'),
    practice_ticket_sha256 TEXT NOT NULL UNIQUE,
    problem_no INTEGER NOT NULL CHECK(problem_no IN(3,4)),
    practice_run_no INTEGER NOT NULL CHECK(practice_run_no>0),
    case_code TEXT NOT NULL,
    entered INTEGER NOT NULL CHECK(entered IN(0,1)),
    end_reason TEXT NOT NULL,
    cleared_jammer_count INTEGER NOT NULL CHECK(cleared_jammer_count BETWEEN 0 AND 16),
    measure_accepted_count INTEGER NOT NULL CHECK(measure_accepted_count BETWEEN 0 AND 131072),
    virtual_time_us INTEGER NOT NULL CHECK(virtual_time_us BETWEEN 0 AND 360000000000),
    program_run_duration_ms INTEGER,
    channel_switch_count INTEGER NOT NULL,
    clear_failure_count INTEGER NOT NULL CHECK(clear_failure_count BETWEEN 0 AND 131072),
    jammer_count INTEGER NOT NULL CHECK(jammer_count BETWEEN 0 AND 16),
    state TEXT NOT NULL CHECK(state IN('queued','submitting','retry_wait','confirmed','server_rejected')),
    attempt_count INTEGER NOT NULL,
    next_attempt_at_ms INTEGER NOT NULL,
    last_error_code TEXT NOT NULL,
    received_at_ms INTEGER,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    CHECK(channel_switch_count BETWEEN 0 AND measure_accepted_count),
    CHECK((entered=0 AND program_run_duration_ms IS NULL)
          OR (entered=1 AND program_run_duration_ms BETWEEN 0 AND 1200000))
) STRICT;
CREATE INDEX IF NOT EXISTS practice_statistics_due
    ON practice_statistics_tasks(team_no,state,next_attempt_at_ms,created_at_ms);
"""


class PracticeStatsStore:
    """演练统计用的 SQLite 存储，一把锁护住连接，线程安全"""

    def __init__(self, data_dir: Path):
        """在给定的数据目录下开库，目录和数据表缺了就顺手建好"""
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "behavior-logs").mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "practice-statistics-queue.sqlite3"
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._log_path: Path | None = None
        self._log_key: tuple | None = None

    def next_run_no(self, problem_no: int) -> int:
        """取某个题号下的下一个演练序号，查库里的最大值加一"""
        with self._lock:
            cur = self._conn.execute(
                "SELECT COALESCE(MAX(practice_run_no),0)+1 FROM practice_statistics_tasks WHERE problem_no=?",
                (problem_no,),
            )
            return int(cur.fetchone()[0])

    def new_sha256(self) -> str:
        """生成一个随机 UUID 的 sha256 十六进制串，给演练凭据当指纹用"""
        return hashlib.sha256(uuid.uuid4().bytes).hexdigest()

    def new_case_code(self) -> str:
        """生成 XXXX-XXXX-XXXX-XXXX 形式的案例编码"""
        s = uuid.uuid4().hex[:16].upper()
        return "-".join(s[i:i + 4] for i in range(0, 16, 4))

    def record_result(self, **rec) -> int:
        """按关键字参数收一条演练结果，转手交给 insert_result 落库"""
        return self.insert_result(rec)

    def append_behavior_log(self, problem_no: int, run_no: int, rec: dict):
        """向行为日志追加一条事件，文件按需创建并先写一行表头"""
        key = (problem_no, run_no)
        with self._lock:
            if self._log_key != key or self._log_path is None:
                ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
                self._log_path = (self.data_dir / "behavior-logs"
                                  / f"practice-p{problem_no}-{run_no}-{ts}.jlog")
                with open(self._log_path, "w", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "schema_version": "jammers-event-chain/v1",
                        "profile_version": "practice-local-v1",
                        "problem_no": problem_no,
                        "practice_run_no": run_no,
                        "created_at_ms": int(time.time() * 1000),
                    }, ensure_ascii=False) + "\n")
                self._log_key = key
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def insert_result(self, rec: dict) -> int:
        """把一条记录补全默认值后插进统计表，返回自增主键"""
        with self._lock:
            now = int(time.time() * 1000)
            entered = 1 if rec.get("entered", True) else 0
            run_duration = rec.get("program_run_duration_ms")
            if entered == 0:
                run_duration = None
            cur = self._conn.execute(
                """INSERT INTO practice_statistics_tasks
                   (team_no, client_request_id, schema_version, practice_ticket_sha256,
                    problem_no, practice_run_no, case_code, entered, end_reason,
                    cleared_jammer_count, measure_accepted_count, virtual_time_us,
                    program_run_duration_ms, channel_switch_count, clear_failure_count,
                    jammer_count, state, attempt_count, next_attempt_at_ms,
                    last_error_code, received_at_ms, created_at_ms, updated_at_ms)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rec.get("team_no", ""),
                    rec.get("client_request_id", str(uuid.uuid4())),
                    STATISTICS_SCHEMA_VERSION,
                    rec.get("practice_ticket_sha256", _rand_sha256()),
                    int(rec["problem_no"]),
                    int(rec["practice_run_no"]),
                    rec.get("case_code", ""),
                    entered,
                    rec.get("end_reason", ""),
                    int(rec.get("cleared_jammer_count", 0)),
                    int(rec.get("measure_accepted_count", 0)),
                    int(rec.get("virtual_time_us", 0)),
                    run_duration,
                    int(rec.get("channel_switch_count", 0)),
                    int(rec.get("clear_failure_count", 0)),
                    int(rec.get("jammer_count", 0)),
                    "confirmed",  # 本地版没有服务器，落库直接确认
                    0,            # attempt_count
                    0,            # next_attempt_at_ms
                    "",           # last_error_code
                    now,          # received_at_ms
                    now,
                    now,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_results(self, limit: int = 100) -> list[dict]:
        """按创建时间倒序取最近若干条记录，列名从游标描述里读出来配成字典"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM practice_statistics_tasks ORDER BY created_at_ms DESC LIMIT ?",
                (limit,),
            ).fetchall()
            cur = self._conn.execute("SELECT * FROM practice_statistics_tasks LIMIT 0")
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]

    def clear_results(self) -> int:
        """清空统计表，返回删掉的行数"""
        with self._lock:
            cur = self._conn.execute("DELETE FROM practice_statistics_tasks")
            self._conn.commit()
            return cur.rowcount

    def close(self):
        """关掉 SQLite 连接"""
        with self._lock:
            self._conn.close()


def _rand_sha256() -> str:
    """给缺凭据指纹的记录兜一个随机值，插库时用"""
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def new_case_code() -> str:
    """生成 XXXX-XXXX-XXXX-XXXX 形式的案例编码"""
    s = uuid.uuid4().hex[:16].upper()
    return "-".join(s[i:i + 4] for i in range(0, 16, 4))


def open_behavior_log(data_dir: Path, problem_no: int, run_no: int) -> Path:
    """创建行为日志文件并写入表头，返回文件路径"""
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    path = data_dir / "behavior-logs" / f"practice-p{problem_no}-{run_no}-{ts}.jlog"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "schema_version": "jammers-event-chain/v1",
            "profile_version": "practice-local-v1",
            "problem_no": problem_no,
            "practice_run_no": run_no,
            "created_at_ms": int(time.time() * 1000),
        }, ensure_ascii=False) + "\n")
    return path