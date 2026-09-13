# -*- coding: utf-8 -*-
"""测试会话状态机，机器狗请求的字段校验、幂等与动作调度"""
from __future__ import annotations

import math
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field

from . import render
from .config import Config, SimulationRules
from .engine import Engine, EngineResult
from .scenario import Scenario, generate_scenario
from .store import PracticeStatsStore

# 终态原因，对齐官方枚举
END_USER_EXIT = "user_exit"
END_MANUAL_ABORT_BEFORE_ENTER = "manual_abort_before_enter"
END_MANUAL_ABORT_RUNNING = "manual_abort_running"
END_WINDOW_TIMEOUT_BEFORE_ENTER = "window_timeout_before_enter"
END_WINDOW_TIMEOUT_RUNNING = "window_timeout_running"
END_PROGRAM_TIMEOUT = "program_timeout"
END_VIRTUAL_TIMEOUT = "virtual_timeout"
END_INTERNAL_FAILURE = "internal_failure"

_STATE_LABELS = {
    "idle": "空闲",
    "preparing": "准备中",
    "window_open": "等待机器狗进入",
    "running": "运行中",
    "finished": "已结束",
}

_HTTP_OK = 200
_HTTP_BAD_REQUEST = 400
_HTTP_CONFLICT = 409
_HTTP_TOO_MANY = 429

_IDEMPOTENCY_LIMIT = 100_000


def _has_invalid_char(s: str) -> bool:
    for ch in s:
        o = ord(ch)
        if o < 32 or o == 127:
            return True
        if unicodedata.category(ch) == "Cf":
            return True
    return False


def _is_int_number(v) -> bool:
    """判断是不是数值上恰好等于整数的 number，bool 要排掉"""
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    if isinstance(v, float):
        return math.isfinite(v) and v == int(v)
    return False


@dataclass
class _IdemRecord:
    path: str
    canonical: str
    status: int
    body: str          # 已经渲染好的 JSON 文本，格式同官方 renderResult


class SessionManager:
    def __init__(self, cfg: Config, store: PracticeStatsStore):
        self.cfg = cfg
        self.store = store
        self.rules: SimulationRules = cfg.rules
        self.state = "idle"
        self.end_reason = ""
        self.problem_no: int | None = None
        self.run_no = 0
        self.case_code = ""
        self.scenario: Scenario | None = None
        self.engine: Engine | None = None

        self._lock = threading.Lock()
        self._monitor = None
        self._monitor_stop = threading.Event()
        self._prepare_deadline = 0.0
        self._window_deadline = 0.0
        self._program_deadline = 0.0
        self._enter_real_ms = 0
        self._started_real_ms = 0

        self._idem: dict[str, _IdemRecord] = {}
        self._history: list[dict] = []      # 行为日志事件，内存态，落盘交给 store

    # ================= 状态机 =================
    def start(self, problem_no: int, scenario: Scenario | None = None) -> tuple[bool, str]:
        with self._lock:
            if self.state not in ("idle", "finished"):
                return False, "已有测试在进行中"
            if problem_no not in (3, 4):
                return False, "problem_no 必须为 3 或 4"
            if scenario is None:
                scenario = generate_scenario(self.rules, problem_no)
            try:
                scenario.validate(problem_no, self.rules.generation_disk_radius_m)
            except ValueError as e:
                return False, f"场景无效: {e}"

            self.problem_no = problem_no
            self.scenario = scenario
            self.engine = Engine(self.rules, scenario)
            self.run_no = self.store.next_run_no(problem_no)
            self.case_code = self.store.new_case_code()
            self.end_reason = ""
            self._idem.clear()
            self._history.clear()

            now = time.time()
            self._prepare_deadline = now + self.cfg.countdown_seconds
            self._window_deadline = now + self.cfg.countdown_seconds + self.cfg.window_seconds
            self._started_real_ms = int(now * 1000)
            self.state = "preparing"

            self._record("prepare_started",
                         f"problem={problem_no} run_no={self.run_no} jammer_count={scenario.jammer_count}")
            self._start_monitor()
            return True, "已开始准备"

    def _start_monitor(self):
        if self._monitor and self._monitor.is_alive():
            return
        self._monitor_stop.clear()
        self._monitor = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor.start()

    def _monitor_loop(self):
        while not self._monitor_stop.is_set():
            time.sleep(0.2)
            with self._lock:
                if self.state == "preparing":
                    if time.time() >= self._prepare_deadline:
                        self.state = "window_open"
                        self._record("window_opened", "机器狗接口已开放，测试窗口开始")
                elif self.state == "window_open":
                    if time.time() >= self._window_deadline:
                        self._finish(END_WINDOW_TIMEOUT_BEFORE_ENTER)
                elif self.state == "running":
                    vt_us = self.engine.virtual_time_us if self.engine else 0
                    if vt_us >= self._us(self.rules.max_virtual_duration_s):
                        self._finish(END_VIRTUAL_TIMEOUT)
                    elif time.time() >= self._program_deadline:
                        self._finish(END_PROGRAM_TIMEOUT)
                    elif time.time() >= self._window_deadline:
                        self._finish(END_WINDOW_TIMEOUT_RUNNING)

    def _finish(self, reason: str):
        if self.state == "finished":
            return
        self.state = "finished"
        self.end_reason = reason
        self._record("test_ended", f"{reason}: 测试结束")
        self._persist_statistics()

    def abort(self) -> tuple[bool, str]:
        with self._lock:
            if self.state not in ("preparing", "window_open", "running"):
                return False, "当前无进行中的测试"
            if self.state in ("preparing", "window_open"):
                reason = END_MANUAL_ABORT_BEFORE_ENTER
            else:
                reason = END_MANUAL_ABORT_RUNNING
            self._finish(reason)
            return True, "已中止"

    def clear_finished(self) -> bool:
        with self._lock:
            if self.state != "finished":
                return False
            self.state = "idle"
            self.engine = None
            self.scenario = None
            self._idem.clear()
            self._history.clear()
            self.end_reason = ""
            self.problem_no = None
            return True

    def stop(self):
        self._monitor_stop.set()
        if self._monitor and self._monitor.is_alive():
            self._monitor.join(timeout=1)

    # ================= 统计 =================
    def _persist_statistics(self):
        if self.engine is None or self.problem_no is None:
            return
        e = self.engine
        entered = 1 if e.entered else 0
        dur_ms = int(time.time() * 1000) - self._enter_real_ms if entered else None
        self.store.record_result(
            team_no=self.cfg.team_no,
            client_request_id=str(uuid.uuid4()),
            schema_version="practice-run-statistics-v1",
            practice_ticket_sha256=self.store.new_sha256(),
            problem_no=self.problem_no,
            practice_run_no=self.run_no,
            case_code=self.case_code,
            entered=entered,
            end_reason=self.end_reason,
            cleared_jammer_count=e.cleared_jammer_count,
            measure_accepted_count=e.measure_accepted_count,
            virtual_time_us=e.virtual_time_us,
            program_run_duration_ms=dur_ms,
            channel_switch_count=e.channel_switch_count,
            clear_failure_count=e.clear_failure_count,
            jammer_count=e.jammer_count,
        )

    # ================= 行为日志 =================
    def _record(self, event: str, detail: str = ""):
        seq = len(self._history) + 1
        now_ms = int(time.time() * 1000)
        rec = {
            "type": "lifecycle", "event": event, "detail": detail,
            "seq": seq, "ts_ms": now_ms, "real_timestamp_ms": now_ms,
        }
        if self.engine is not None:
            rec["virtual_time_us"] = self.engine.virtual_time_us
        self._history.append(rec)
        # 顺手落盘，按追加行的写法
        self.store.append_behavior_log(self.problem_no, self.run_no, rec)

    def _record_robot(self, action: str, req: dict, res: EngineResult, http_status: int,
                      diagnostic: str = ""):
        seq = len(self._history) + 1
        now_ms = int(time.time() * 1000)
        rec = {
            "type": "robot", "action": action, "outcome": res.outcome,
            "accepted": res.accepted, "http_status": http_status, "diagnostic": diagnostic,
            "virtual_time_us": res.virtual_time_us,
            "has_position": res.has_position, "x": res.x, "y": res.y,
            "has_channel": res.has_channel, "channel": res.channel,
            "has_bearing": res.has_bearing,
            "bearing_hundredths": res.bearing_hundredths if res.has_bearing else 0,
            "request": req, "seq": seq, "ts_ms": now_ms, "real_timestamp_ms": now_ms,
        }
        self._history.append(rec)
        self.store.append_behavior_log(self.problem_no, self.run_no, rec)

    # ================= 请求处理 =================
    def interface_open(self) -> bool:
        return self.state in ("window_open", "running")

    def handle(self, path: str, req: dict) -> tuple[int, str] | None:
        """处理一个已通过 HTTP 层校验的请求

        返回 (status, json_text)；接口未开放或者测试已结束时返回 None，那种情况直接关连接，
        不给 JSON。响应体是官方 renderResult 同款的手工拼装文本，数字格式逐字节对齐。
        """
        if not self.interface_open():
            return None  # 接口未开放/已结束：直接关闭连接，无 JSON

        if not self._lock.acquire(blocking=False):
            return (_HTTP_CONFLICT, self._err_text("concurrent actions are not allowed"))
        try:
            if not self.interface_open():
                return None
            return self._handle_locked(path, req)
        finally:
            self._lock.release()

    def _us(self, seconds: float) -> int:
        """秒 → 微秒整数，官方规则字段本身就是整数微秒"""
        return int(round(seconds * 1_000_000))

    def _err_text(self, diagnostic: str = "") -> str:
        """accepted=false：官方恒为固定 3 字段，且 virtual_time_s 是字面量 0"""
        self._last_diagnostic = diagnostic
        return render.render_rejected(int(time.time() * 1000))

    def _handle_locked(self, path: str, req: dict) -> tuple[int, str]:
        # ---- 公共标识字段校验 ----
        arena_id = req.get("arena_id")
        robot_id = req.get("robot_id")
        request_id = req.get("request_id")

        # 缺失或类型错误 → 400
        for name, val, maxbytes in (("arena_id", arena_id, 64), ("robot_id", robot_id, 64),
                                    ("request_id", request_id, 128)):
            if val is None:
                return (_HTTP_BAD_REQUEST, self._err_text(f"missing field {name}"))
            if not isinstance(val, str):
                return (_HTTP_BAD_REQUEST, self._err_text(f"field {name} must be a string"))
            if len(val.encode("utf-8")) < 1 or len(val.encode("utf-8")) > maxbytes:
                return (_HTTP_BAD_REQUEST, self._err_text(f"field {name} length is invalid"))
            if _has_invalid_char(val):
                return (_HTTP_BAD_REQUEST, self._err_text(f"field {name} contains invalid characters"))

        # arena_id / robot_id 不匹配 → 200 accepted=false，这一档不占用 request_id
        if arena_id != "default":
            return (_HTTP_OK, self._err_text("arena_id mismatch"))
        if robot_id != self.cfg.team_no:
            return (_HTTP_OK, self._err_text("robot_id mismatch"))

        # ---- 未知字段校验，这一档也不占用 request_id ----
        allowed = {"arena_id", "robot_id", "request_id"}
        if path in ("/measure", "/clear"):
            allowed |= {"position", "channel"}
        unknown = set(req.keys()) - allowed
        if unknown:
            return (_HTTP_OK, self._err_text(f"unknown field(s): {', '.join(sorted(unknown))}"))

        # ---- position / channel 字段校验 ----
        position = None
        channel = None
        if path in ("/measure", "/clear"):
            position = req.get("position")
            if not isinstance(position, dict):
                return (_HTTP_BAD_REQUEST, self._err_text("missing field position"))
            p_unknown = set(position.keys()) - {"x", "y"}
            if p_unknown:
                return (_HTTP_OK, self._err_text(
                    f"unknown field(s) in position: {', '.join(sorted(p_unknown))}"))
            x, y = position.get("x"), position.get("y")
            if not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x):
                return (_HTTP_BAD_REQUEST, self._err_text("position.x is invalid"))
            if not isinstance(y, (int, float)) or isinstance(y, bool) or not math.isfinite(y):
                return (_HTTP_BAD_REQUEST, self._err_text("position.y is invalid"))
            if abs(x) > self.rules.coord_abs_max_m or abs(y) > self.rules.coord_abs_max_m:
                return (_HTTP_BAD_REQUEST, self._err_text("coordinate out of range"))
            channel = req.get("channel")
            if channel is None:
                return (_HTTP_BAD_REQUEST, self._err_text("missing field channel"))
            if not _is_int_number(channel):
                return (_HTTP_BAD_REQUEST, self._err_text("channel must be an integer"))
            channel = int(channel)
            if not (self.rules.channel_min <= channel <= self.rules.channel_max):
                return (_HTTP_BAD_REQUEST, self._err_text("channel out of range"))

        # ---- 幂等 ----
        canonical = (path, repr(req))
        if request_id in self._idem:
            rec = self._idem[request_id]
            if rec.canonical == canonical:
                return (rec.status, rec.body)
            return (_HTTP_CONFLICT, self._err_text("request_id reused with different content"))
        if len(self._idem) >= _IDEMPOTENCY_LIMIT:
            return (_HTTP_TOO_MANY, self._err_text("idempotency records exhausted"))

        # ---- 动作调度 ----
        status, body = self._dispatch(path, req, position, channel)

        # 只有业务接受，也就是 accepted=true，才占用 request_id
        # 官方 rejected 响应恒以 {"accepted":false 开头，据此判定即可
        if not body.startswith('{"accepted":false'):
            self._idem[request_id] = _IdemRecord(path, canonical, status, body)
        return (status, body)

    def _dispatch(self, path: str, req: dict, position, channel) -> tuple[int, str]:
        e = self.engine
        if e is None:
            return (_HTTP_OK, self._err_text("no test run exists"))

        if path == "/enter":
            if self.state == "running" or e.entered:
                return (_HTTP_OK, self._err_text("already_entered"))
            res = e.enter()
            if res.accepted:
                self.state = "running"
                now = time.time()
                self._enter_real_ms = int(now * 1000)
                # 实际现实截止 = min(窗口截止, /enter 成功后 20 分钟)
                self._program_deadline = min(self._window_deadline,
                                             now + float(self.rules.max_program_duration_s))
                remaining = self._remaining_real_duration()
                self._record_robot("enter", req, res, _HTTP_OK)
                body = self._enter_body(res, remaining)
                return (_HTTP_OK, body)
            return (_HTTP_OK, self._err_text(res.diagnostic))

        if path in ("/measure", "/clear"):
            if self.state != "running" or not e.entered:
                return (_HTTP_OK, self._err_text("not_entered"))
            if path == "/measure":
                res = e.measure(float(position["x"]), float(position["y"]), channel)
            else:
                res = e.clear(float(position["x"]), float(position["y"]), channel)
            self._record_robot("measure" if path == "/measure" else "clear", req, res, _HTTP_OK)
            return (_HTTP_OK, self._action_body(path, res))

        if path == "/exit":
            if self.state != "running" or not e.entered:
                return (_HTTP_OK, self._err_text("not_entered"))
            res = e.exit()
            if res.accepted:
                self._record_robot("exit", req, res, _HTTP_OK)
                self._finish(END_USER_EXIT)
                return (_HTTP_OK, self._action_body(path, res))
            return (_HTTP_OK, self._err_text(res.diagnostic))

        return (_HTTP_BAD_REQUEST, self._err_text("unknown action"))

    def _remaining_real_duration(self) -> int:
        now = time.time()
        left_window = self._window_deadline - now
        left_program = (self._program_deadline - now) if self._program_deadline \
            else self.rules.max_program_duration_s
        return max(0, int(min(left_window, left_program)))

    def _enter_body(self, res: EngineResult, remaining: int) -> str:
        return render.render_accepted(
            "/enter", int(time.time() * 1000), res.virtual_time_us,
            remaining_real_duration_s=remaining)

    def _action_body(self, path: str, res: EngineResult) -> str:
        """measure/clear/exit 的响应体，就是官方 renderResult 的那段文本"""
        return render.render_accepted(
            path, int(time.time() * 1000), res.virtual_time_us,
            result=res.outcome,
            svd_hundredths=(res.bearing_hundredths if res.outcome == "direction" else None))

    # ================= 快照 =================
    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            prepare_remaining = max(0.0, self._prepare_deadline - now)
            window_remaining = max(0.0, self._window_deadline - now)
            program_remaining = max(0.0, self._program_deadline - now) if self._program_deadline else 0
            e = self.engine
            log_tail = [r for r in self._history[-200:]]
            return {
                "state": self.state,
                "state_label": _STATE_LABELS.get(self.state, self.state),
                "problem_no": self.problem_no,
                "run_no": self.run_no,
                "case_code": self.case_code,
                "end_reason": self.end_reason,
                "prepare_remaining_s": prepare_remaining,
                "window_remaining_s": window_remaining,
                "program_remaining_s": program_remaining,
                "scenario": self.scenario.to_json() if self.scenario else None,
                "engine": e.snapshot() if e else None,
                "log_tail": log_tail,
            }
