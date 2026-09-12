#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端自测：官方机器人协议对齐验证。

覆盖官方《通信接口说明及编程指南》：
  接口未开放直接断连、enter 五字段响应、measure 三结果与 svd_deg、
  clear 两结果、exit、幂等（重放/409）、未知字段、robot_id 不匹配、
  坐标/频道校验、Content-Type 415、方法 405、路径 404、统计落库、中止。

用法：python3 test_api.py    （在 jammers-py/ 目录下运行）
"""
from __future__ import annotations

import http.client
import json
import tempfile
import time
import urllib.request
from pathlib import Path

ROBOT_PORT = 18100
WEB_PORT = 18101
ROBOT_BASE = f"127.0.0.1:{ROBOT_PORT}"
WEB_BASE = f"http://127.0.0.1:{WEB_PORT}"


def robot_req(method: str, path: str, body=None, headers=None):
    """发机器人接口请求。返回 (status, parsed) 或 (None, None) 表示连接被直接关闭。"""
    conn = http.client.HTTPConnection(ROBOT_BASE, timeout=5)
    try:
        h = {}
        if body is not None:
            h["Content-Type"] = "application/json"
        if headers:
            h.update(headers)
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        parsed = json.loads(data) if data else None
        return resp.status, parsed
    except (http.client.RemoteDisconnected, ConnectionResetError,
            ConnectionAbortedError, http.client.HTTPException):
        return None, None
    finally:
        conn.close()


def robot_post(path: str, payload: dict, headers=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    return robot_req("POST", path, body, headers)


def robot_post_raw(path: str, payload: dict):
    """返回 (status, 原始响应文本)，用于逐字节校验官方 JSON 文本格式。"""
    conn = http.client.HTTPConnection(ROBOT_BASE, timeout=5)
    try:
        conn.request("POST", path, body=json.dumps(payload).encode("utf-8"),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8")
    finally:
        conn.close()


def web_get(path: str) -> dict:
    with urllib.request.urlopen(WEB_BASE + path, timeout=5) as r:
        return json.loads(r.read().decode())


def web_post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(WEB_BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def wait_state(expect: str, timeout_s: float = 10.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        snap = web_get("/api/state")
        if snap["state"] == expect:
            return snap
        time.sleep(0.15)
    raise AssertionError(f"状态未在 {timeout_s}s 内变为 {expect}")


def base(request_id: str) -> dict:
    return {"arena_id": "default", "robot_id": "t-test-01", "request_id": request_id}


def action(request_id: str, x: float, y: float, channel) -> dict:
    p = base(request_id)
    p["position"] = {"x": x, "y": y}
    p["channel"] = channel
    return p


PASS = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS
    assert cond, f"{name} 失败: {detail}"
    PASS += 1
    print(f"[PASS] {name}")


def main() -> int:
    from simulator.config import Config
    from simulator.session import SessionManager
    from simulator.store import PracticeStatsStore
    from simulator.robot_api import RobotAPI
    from simulator.webui import WebUI
    from simulator.scenario import demo_scenario

    tmp = Path(tempfile.mkdtemp(prefix="jammers-test-"))
    try:
        cfg = Config(data_dir=str(tmp))
        cfg.robot_port = ROBOT_PORT
        cfg.web_port = WEB_PORT
        cfg.window_seconds = 60
        cfg.countdown_seconds = 1
        cfg.team_no = "t-test-01"

        store = PracticeStatsStore(cfg.resolved_data_dir)
        manager = SessionManager(cfg, store)
        robot = RobotAPI(cfg, manager)
        web = WebUI(cfg, manager, store)
        ok_r, _ = robot.start()
        ok_w, _ = web.start()
        assert ok_r and ok_w, "服务器启动失败"

        # 1) 空闲状态接口未开放 → 连接直接关闭（无 JSON）
        st, body = robot_post("/measure", action("m-idle", 0, 0, 1))
        check("空闲时接口未开放直接断连", st is None, f"st={st} body={body}")

        # 2) 启动问题4（demo 场景 10 干扰源，含 1 个定向 ch2）
        r = web_post("/api/start", {"problem_no": 4, "scenario": demo_scenario(4).to_json()})
        check("启动测试(问题4)", r.get("ok") is True, str(r))

        # 3) 倒计时中接口未开放
        st, body = robot_post("/enter", base("e-prep"))
        check("倒计时中接口未开放直接断连", st is None, f"st={st}")

        wait_state("window_open")
        check("进入测试窗口(接口开放)", True)

        # 4) 未 enter 就 measure → 200 accepted=false（仅三字段）
        st, body = robot_post("/measure", action("m-noenter", 300, 400, 1))
        check("未进入就测量被拒绝", st == 200 and body["accepted"] is False and
              set(body.keys()) == {"accepted", "real_timestamp_ms", "virtual_time_s"}, str(body))

        # 5) enter 成功，响应含剩余时长等字段（并校验原始文本数字格式）
        st, raw_enter = robot_post_raw("/enter", base("enter-1"))
        body = json.loads(raw_enter)
        check("enter 成功", st == 200 and body["accepted"] is True and
              body["virtual_time_s"] == 0 and
              {"max_virtual_duration_s", "max_real_duration_s", "remaining_real_duration_s"} <= set(body.keys()),
              str(body))
        check("enter 原始文本为官方五字段整数格式（无 .0）",
              raw_enter.startswith('{"accepted":true,"real_timestamp_ms":') and
              '"virtual_time_s":0,"max_virtual_duration_s":360000,' in raw_enter and
              '"max_real_duration_s":1200,"remaining_real_duration_s":' in raw_enter and
              raw_enter.endswith("}"), raw_enter)

        # enter 原始文本：官方 renderResult 的数字格式（无 .0 尾巴）
        st, raw_enter = robot_post_raw("/enter", base("e-dup"))
        check("真实 rejected 响应为固定 3 字段",
              raw_enter.startswith('{"accepted":false,"real_timestamp_ms":') and
              raw_enter.endswith(',"virtual_time_s":0}'), raw_enter)
        remaining = body["remaining_real_duration_s"]
        check("remaining_real_duration_s 合理", 0 <= remaining <= 60, str(remaining))

        # 6) 重复 enter → 200 accepted=false
        st, body = robot_post("/enter", base("enter-2"))
        check("重复 enter 被拒绝", st == 200 and body["accepted"] is False, str(body))

        # 7) measure：全向 ch1 @(500,0)，检测点(500,300) → direction + svd_deg
        st, body = robot_post("/measure", action("m-1", 500, 300, 1))
        check("measure direction + svd_deg", st == 200 and body["accepted"] is True and
              body["measure_result"] == "direction" and isinstance(body["svd_deg"], float),
              str(body))

        # 8) measure 不存在频道 → no_signal（无 svd_deg）
        st, body = robot_post("/measure", action("m-2", 500, 300, 20))
        check("measure no_signal（无 svd_deg）", body["measure_result"] == "no_signal" and
              "svd_deg" not in body, str(body))

        # 9) measure 定向覆盖范围外 → no_signal（ch2 @(-800,0) 朝向90°，半角90°
        #    即覆盖 0..180°；检测点(-800,-500) 位于 270° → 覆盖外）
        st, body = robot_post("/measure", action("m-3", -800, -500, 2))
        check("measure 定向覆盖范围外 no_signal", body["measure_result"] == "no_signal", str(body))

        # 10) measure 定向覆盖范围内 → direction
        #     覆盖判定用 干扰源→检测点 方位（(-800,500) 为 90°，在 0..180° 内）
        #     而 svd_deg 是 检测点→干扰源 方位（270°），两者相差 180°
        st, body = robot_post("/measure", action("m-4", -800, 500, 2))
        check("measure 定向覆盖内 direction", body["measure_result"] == "direction" and
              abs(body["svd_deg"] - 270) <= 1.0 + 1e-6, str(body))

        # 10b) 示向度误差为确定性空间噪声：同一位置同一频道重复测量结果完全相同
        st, again = robot_post("/measure", action("m2b-repeat", -800, 500, 2))
        check("同位置同频道误差可复现（确定性噪声）",
              again["svd_deg"] == body["svd_deg"], f'{body["svd_deg"]} vs {again["svd_deg"]}')

        # 10c) 误差严格落在 ±1° 内，且不同网格位置误差不同
        import math as _m
        st, far = robot_post("/measure", action("m2c-far", -800, 760, 2))
        # 检测点(-800,760)：真方位 = atan2(干扰源y-检测点y, 干扰源x-检测点x) = 270°
        true_bearing = _m.degrees(_m.atan2(0 - 760, -800 - (-800))) % 360.0
        check("dir 误差在 ±1° 内", abs(far["svd_deg"] - true_bearing) <= 1.0 + 1e-6,
              f'{far["svd_deg"]} vs {true_bearing}')
        check("不同位置误差场不同（150m 相关长度）",
              far["svd_deg"] != body["svd_deg"], f'{far["svd_deg"]} == {body["svd_deg"]}')

        # 11) 近距离 near：检测点(500,0) 即 ch1 位置
        st, body = robot_post("/measure", action("m-5", 500, 0, 1))
        check("measure near（无 svd_deg）", body["measure_result"] == "near" and
              "svd_deg" not in body, str(body))

        # 12) clear 成功：ch1 附近 10m
        st, body = robot_post("/clear", action("c-1", 510, 0, 1))
        check("clear success", body["clear_result"] == "success", str(body))

        # 13) 重复 clear 已清除干扰源 → no_target_in_range
        st, body = robot_post("/clear", action("c-2", 510, 0, 1))
        check("重复 clear no_target_in_range", body["clear_result"] == "no_target_in_range", str(body))

        # 14) clear 不存在频道 → no_target_in_range
        st, body = robot_post("/clear", action("c-3", 510, 0, 20))
        check("clear 不存在频道 no_target_in_range", body["clear_result"] == "no_target_in_range", str(body))

        # 15) 幂等：重放相同 request_id+内容 → 完全一致；不同内容 → 409
        st1, b1 = robot_post("/measure", action("m-idem", 600, 100, 3))
        st2, b2 = robot_post("/measure", action("m-idem", 600, 100, 3))
        check("幂等重放返回一致", st1 == st2 and b1 == b2, f"{b1} vs {b2}")
        st3, b3 = robot_post("/measure", action("m-idem", 601, 100, 3))
        check("同 request_id 不同内容 → 409", st3 == 409 and b3["accepted"] is False, str(b3))

        # 16) 未知字段 → 200 accepted=false
        p = action("m-unk", 700, 100, 4)
        p["extra"] = 1
        st, body = robot_post("/measure", p)
        check("未知字段 200 accepted=false", st == 200 and body["accepted"] is False, str(body))

        # 17) robot_id 不匹配 → 200 accepted=false
        p = action("m-rid", 700, 100, 4)
        p["robot_id"] = "other-team"
        st, body = robot_post("/measure", p)
        check("robot_id 不匹配 200 accepted=false", st == 200 and body["accepted"] is False, str(body))

        # 18) arena_id 不匹配 → 200 accepted=false
        p = action("m-aid", 700, 100, 4)
        p["arena_id"] = "other"
        st, body = robot_post("/measure", p)
        check("arena_id 不匹配 200 accepted=false", st == 200 and body["accepted"] is False, str(body))

        # 19) 坐标超范围 → 400
        p = action("m-coord", 3_000_000, 0, 4)
        st, body = robot_post("/measure", p)
        check("坐标超范围 → 400", st == 400 and body["accepted"] is False, str(body))

        # 20) channel 1.5 → 400；channel 1.0 → 接受
        st, body = robot_post("/measure", action("m-ch15", 700, 100, 1.5))
        check("channel=1.5 → 400", st == 400, str(body))
        st, body = robot_post("/measure", action("m-ch10", 700, 100, 1.0))
        check("channel=1.0 数值整数 → 接受", st == 200 and body["accepted"] is True, str(body))

        # 21) channel=0 → 400
        st, body = robot_post("/measure", action("m-ch0", 700, 100, 0))
        check("channel=0 → 400", st == 400, str(body))

        # 22) Content-Type 错误 → 415
        st, body = robot_post("/measure", action("m-ct", 700, 100, 5),
                              headers={"Content-Type": "text/plain"})
        check("Content-Type 错误 → 415", st == 415, str(body))

        # 23) GET /enter → 405
        st, body = robot_req("GET", "/enter")
        check("GET /enter → 405", st == 405, str(body))

        # 24) 未知路径 → 404
        st, body = robot_req("POST", "/nope", b"{}")
        check("未知路径 → 404", st == 404, str(body))

        # 25) 重复键 JSON → 400
        raw = b'{"arena_id":"default","arena_id":"default","robot_id":"t-test-01","request_id":"dup"}'
        st, body = robot_req("POST", "/enter", raw)
        check("重复键 JSON → 400", st == 400, str(body))

        # 26) exit → user_exit，终态
        st, body = robot_post("/exit", base("exit-1"))
        check("exit user_exit", st == 200 and body["accepted"] is True and
              body["exit_reason"] == "user_exit", str(body))
        snap = wait_state("finished")
        check("终态 user_exit", snap["end_reason"] == "user_exit", snap["end_reason"])

        # 27) 统计落库（官方 schema）
        rows = web_get("/api/history").get("rows", [])
        check("统计落库", len(rows) == 1 and rows[0]["problem_no"] == 4, str(rows[:1]))
        rec = rows[0]
        check("统计字段", rec["schema_version"] == "practice-run-statistics-v1" and
              rec["state"] == "confirmed" and rec["jammer_count"] == 10 and
              rec["measure_accepted_count"] == 9 and
              rec["cleared_jammer_count"] == 1 and
              rec["clear_failure_count"] == 2 and
              rec["channel_switch_count"] == 5, str(rec))

        # 28) 中止（未 enter）→ manual_abort_before_enter；问题3场景纯全向
        web_post("/api/clear", {})
        web_post("/api/start", {"problem_no": 3})
        time.sleep(1.3)
        snap = wait_state("window_open")
        check("问题3场景纯全向（0 定向）",
              all(j["kind"] == "omni" for j in snap["scenario"]["jammers"]), str(snap["scenario"]["jammers"]))
        r = web_post("/api/abort", {})
        check("中止成功", r.get("ok") is True, str(r))
        snap = wait_state("finished")
        check("中止终态 manual_abort_before_enter",
              snap["end_reason"] == "manual_abort_before_enter", snap["end_reason"])

        # 29) 场景生成校验：12 个、频道唯一、问题4含定向、问题3纯全向
        web_post("/api/clear", {})
        r = web_post("/api/scenario", {"jammer_count_min": 12, "jammer_count_max": 12,
                                       "problem_no": 4})
        check("场景生成(问题4,12个,含定向)", r.get("ok") and r["scenario"]["jammer_count"] == 12 and
              any(j["kind"] == "directional" for j in r["scenario"]["jammers"]), str(r.get("error")))
        r = web_post("/api/scenario", {"jammer_count_min": 12, "jammer_count_max": 12,
                                       "problem_no": 3})
        check("场景生成(问题3,12个,纯全向)", r.get("ok") and r["scenario"]["jammer_count"] == 12 and
              all(j["kind"] == "omni" for j in r["scenario"]["jammers"]), str(r.get("error")))
        # 问题3 强行带定向场景 → 校验拒绝
        bad = r["scenario"]
        bad["jammers"][0]["kind"] = "directional"
        bad["jammers"][0]["direction"] = 45.0
        r2 = web_post("/api/start", {"problem_no": 3, "scenario": bad})
        check("问题3含定向场景被拒绝", r2.get("ok") is False, str(r2))

        # 30) 位置分布：圆盘面积均匀（r = R·sqrt(u)），生成圆盘半径 1770m
        import math as _mm
        from simulator.scenario import generate_scenario
        from simulator.config import SimulationRules as _R
        _rules = _R()
        check("生成圆盘半径 = 1800-30 = 1770m",
              abs(_rules.generation_disk_radius_m - 1770.0) < 1e-9,
              str(_rules.generation_disk_radius_m))
        radii = []
        for _seed in range(40):
            sc = generate_scenario(_rules, 3, seed=_seed)
            for j in sc.jammers:
                radii.append(_mm.hypot(j.x_m, j.y_m))
        check("所有干扰源位于生成圆盘内", max(radii) <= 1770.0 + 1e-6, f"max={max(radii):.2f}")
        # 面积均匀 ⇒ r 的 CDF 为 (r/R)^2 ⇒ 中位数 ≈ R/√2，均值 ≈ 2R/3
        rs = sorted(radii)
        median = rs[len(rs) // 2]
        mean = sum(rs) / len(rs)
        R = 1770.0
        check("距离中位数 ≈ R/√2（面积均匀）", abs(median - R / _mm.sqrt(2)) <= 0.05 * R,
              f"median={median:.1f} 期望≈{R/_mm.sqrt(2):.1f}")
        check("距离均值 ≈ 2R/3（面积均匀）", abs(mean - 2 * R / 3) <= 0.05 * R,
              f"mean={mean:.1f} 期望≈{2*R/3:.1f}")
        check("距离非均匀分布（排除按半径均匀的错误实现）",
              abs(mean - R / 2) > 0.05 * R, f"mean={mean:.1f} 若为 R/2 则实现错误")
        # 超出生成圆盘位置被拒绝
        sc_bad = generate_scenario(_rules, 3, seed=1)
        sc_bad.jammers[0].x_m, sc_bad.jammers[0].y_m = 1780.0, 0.0
        try:
            sc_bad.validate(3, 1770.0)
            check("生成圆盘外位置被拒绝", False, "未报错")
        except ValueError as e:
            check("生成圆盘外位置被拒绝", "generation disk" in str(e), str(e))

        # 31) 官方 JSON 文本格式：数字字面量逐字节对齐（json.dumps 做不到）
        # 32) render 模块单元校验（官方 renderResult / virtualSeconds）
        from simulator.render import (render_accepted, render_rejected,
                                      virtual_seconds)
        check("virtualSeconds 整数无小数点（官方 %d）",
              virtual_seconds(0) == "0" and virtual_seconds(105000000) == "105",
              f'{virtual_seconds(0)} / {virtual_seconds(105000000)}')
        check("virtualSeconds 微秒 6 位去尾随零（官方 %d.%06d）",
              virtual_seconds(105500001) == "105.500001" and
              virtual_seconds(100100000) == "100.1",
              f'{virtual_seconds(105500001)} / {virtual_seconds(100100000)}')
        check("enter 响应为官方五字段文本",
              render_accepted("/enter", 1, 0, remaining_real_duration_s=1200) ==
              '{"accepted":true,"real_timestamp_ms":1,"virtual_time_s":0,'
              '"max_virtual_duration_s":360000,"max_real_duration_s":1200,'
              '"remaining_real_duration_s":1200}',
              render_accepted("/enter", 1, 0, remaining_real_duration_s=1200))
        check("svd_deg 恒两位小数（官方 %d.%02d）",
              ',"svd_deg":270.02}' in render_accepted("/measure", 1, 105000000, "direction", 27002) and
              ',"svd_deg":270.00}' in render_accepted("/measure", 1, 105000000, "direction", 27000),
              render_accepted("/measure", 1, 105000000, "direction", 27000))
        check("measure 非 direction 无 svd_deg",
              "svd_deg" not in render_accepted("/measure", 1, 111000000, "near"),
              render_accepted("/measure", 1, 111000000, "near"))
        check("clear/exit 后缀与官方一致",
              ',"clear_result":"success"}' in render_accepted("/clear", 1, 194000000, "success") and
              ',"exit_reason":"user_exit"}' in render_accepted("/exit", 1, 199000000),
              render_accepted("/exit", 1, 199000000))
        check("rejected 恒为固定 3 字段且 virtual_time_s 为 0",
              render_rejected(1789054472973) ==
              '{"accepted":false,"real_timestamp_ms":1789054472973,"virtual_time_s":0}',
              render_rejected(1789054472973))

        # 33) 移动耗时：官方 int64(1e12*距离/speed_um_per_s) 截断到微秒
        from simulator.engine import Engine as _Engine
        from simulator.scenario import Scenario as _Sc, Jammer as _J
        _rules2 = _R(); _rules2.move_speed_m_per_s = 5.0
        _sc0 = _Sc(source="t", generation_rules="t", generator_seed_hex="00" * 32,
                   jammers=[_J(channel=1, kind="omni", x_m=0.0, y_m=0.0,
                                               receive_m=1500.0, radius_m=1500.0,
                                               direction_deg=None)])
        _e = _Engine(_rules2, _sc0); _e.enter()
        check("移动 500m → 100s 整（100000000µs）",
              _e._move_duration_us(300.0, 400.0) == 100_000_000,
              str(_e._move_duration_us(300.0, 400.0)))
        check("非整数距离向下截断到微秒",
              _e._move_duration_us(0.0, 0.0000031) == 0 and
              _e._move_duration_us(0.0, 0.000001) == int(1e12 * 1e-6 / 5e6),
              str(_e._move_duration_us(0.0, 0.000001)))

        robot.stop(); web.stop(); store.close()
        print(f"\n全部 {PASS} 项测试通过 ✅")
        return 0
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())