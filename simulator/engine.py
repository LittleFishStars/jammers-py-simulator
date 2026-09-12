# -*- coding: utf-8 -*-
"""模拟引擎：机器狗 enter/measure/clear/exit 的物理判定与虚拟时钟推进。

对齐官方《通信接口说明及编程指南》：
  - /enter：初始位置 (0,0)，测向机初始频道 1，不推进虚拟时钟
  - 移动由 /measure 或 /clear 的位置参数自动推断，耗时 = 直线距离 / 5 m/s
  - /measure：移动耗时 + 频道切换耗时(不同频道 +1s) + 检测 5s
      no_signal（无未清除干扰源/超出接收半径/定向覆盖范围外）
      near（距离 ≤5m 且在覆盖范围内）
      direction（返回 svd_deg = 方位角 + 确定性空间噪声，两位小数；±1°）
  - /clear：移动耗时 + 3s（未发现）/ 5s（成功）；清除半径 20m，不切换频道
  - 坐标单位：米；角度：正东 0°，逆时针为正
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

from . import bearingnoise
from .config import SimulationRules
from .scenario import KIND_DIRECTIONAL, Jammer, Scenario


@dataclass
class EngineResult:
    accepted: bool = False
    outcome: str = "rejected"          # entered/no_signal/near/direction/success/no_target_in_range/user_exit/rejected
    diagnostic: str = ""
    virtual_time_us: int = 0            # 官方为 int64 微秒

    @property
    def virtual_time_s(self) -> float:
        """微秒整数对应的秒（仅用于展示；响应体请用 render.virtual_seconds）。"""
        return self.virtual_time_us / 1_000_000
    svd_deg: float | None = None       # HTTP 响应字段（direction 时返回）
    has_position: bool = False
    x: float = 0.0
    y: float = 0.0
    has_channel: bool = False
    channel: int = 0
    has_bearing: bool = False
    bearing_hundredths: int = 0        # 行为日志字段（百分之一度，整数）
    cleared_jammer_channel: int | None = None
    detected_jammer_channels: list[int] = field(default_factory=list)


def _normalize_bearing(deg: float) -> float:
    """归一化到 [0, 360)。"""
    d = deg % 360.0
    return d if d >= 0 else d + 360.0


def _angle_diff(a: float, b: float) -> float:
    """两个方位角的最小角度差（0..180）。"""
    d = abs(a - b) % 360.0
    return d if d <= 180.0 else 360.0 - d


def _noise_seed(scenario: Scenario) -> int:
    """由场景 noise_seed_hex 派生 64 位噪声种子（官方为 uint64）。

    noise_seed_hex 为 16 位十六进制；缺失或非法时退化为对场景标识做
    BLAKE2b-64 摘要，保证同一场景误差场仍然可复现。
    """
    raw = (scenario.noise_seed_hex or "").strip()
    if raw:
        try:
            return int(raw, 16) & 0xFFFFFFFFFFFFFFFF
        except ValueError:
            pass
    basis = f"{scenario.source}|{scenario.generator_seed_hex}|{scenario.generation_rules}"
    digest = hashlib.blake2b(basis.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


class Engine:
    def __init__(self, rules: SimulationRules, scenario: Scenario):
        self.rules = rules
        self.scenario = scenario
        self.virtual_time_us = 0    # 官方 int64 微秒，逐动作整数累加
        self.x = 0.0
        self.y = 0.0
        self.channel = 1           # 测向机当前频道
        self.entered = False
        self.last_x = 0.0          # 上一次合法动作位置
        self.last_y = 0.0

        # 统计（对齐官方统计表列）
        self.entered = False
        self.cleared_jammer_count = 0
        self.measure_accepted_count = 0
        self.channel_switch_count = 0
        self.clear_failure_count = 0
        self.jammer_count = len(scenario.jammers)

        # 示向度噪声种子（对应官方 Engine+0x80 的场景级种子）
        self.seed = _noise_seed(scenario)

    @property
    def virtual_time_s(self) -> float:
        """微秒整数对应的秒（仅用于展示/日志；响应体请用 render.virtual_seconds）。"""
        return self.virtual_time_us / 1_000_000

    # ---- 快照 ----
    def snapshot(self) -> dict:
        return {
            "virtual_time_us": self.virtual_time_us,
            "virtual_time_s": self.virtual_time_s,
            "entered": self.entered,
            "x": self.x,
            "y": self.y,
            "channel": self.channel,
            "cleared_jammer_count": self.cleared_jammer_count,
            "measure_accepted_count": self.measure_accepted_count,
            "channel_switch_count": self.channel_switch_count,
            "clear_failure_count": self.clear_failure_count,
            "jammer_count": self.jammer_count,
            "jammers": [j.to_json() for j in self.scenario.jammers],
        }

    # ---- 工具 ----
    def _us(self, seconds: float) -> int:
        """秒 → 微秒整数（官方规则字段本身即整数微秒）。"""
        return int(round(seconds * 1_000_000))

    def _move_duration_us(self, x: float, y: float) -> int:
        """移动耗时（微秒）。

        官方 simcore.(*Engine).moveTo：int64(1e12 * 距离m / speed_um_per_s)，
        先乘 1e12 再除以「微米每秒」的速度，结果向零截断为整数微秒。
        浮点运算顺序必须保持一致，否则末位微秒可能有差异。
        """
        distance_m = math.hypot(x - self.last_x, y - self.last_y)
        speed_um_per_s = self._us(self.rules.move_speed_m_per_s)
        return int(1e12 * distance_m / speed_um_per_s)

    def _jammer_on_channel(self, channel: int) -> Jammer | None:
        for j in self.scenario.jammers:
            if j.channel == channel and not j.cleared:
                return j
        return None

    def _in_directional_coverage(self, jammer: Jammer, x: float, y: float) -> bool:
        """检测点是否在定向干扰源有效覆盖角度范围内（含边界）。

        官方 simcore.directionalCoverage：全向源直接覆盖；否则
        |Δ| <= 90° + 1e-9（半角 90°，全角 180°），Δ 为干扰源→检测点方位角
        与干扰源朝向之差。注意该函数只判角度，距离由接收半径另行判定。
        """
        if jammer.kind != KIND_DIRECTIONAL or jammer.direction_deg is None:
            return True
        bearing = _normalize_bearing(math.degrees(math.atan2(y - jammer.y_m, x - jammer.x_m)))
        half = self.rules.directional_beam_width_deg / 2.0
        return _angle_diff(bearing, jammer.direction_deg) <= half + 1e-9

    def _svd_deg(self, jammer: Jammer, x: float, y: float,
                 channel: int) -> tuple[float, int]:
        """检测点指向干扰源的方位角 + 确定性空间噪声。

        返回 (svd_deg 保留两位小数, bearing_hundredths 百分之一度整数)。
        误差只取决于 (噪声种子, 频道, 测量位置)，同一位置同一频道重复测量
        误差完全相同，取平均无法减小误差。
        """
        true_bearing = _normalize_bearing(
            math.degrees(math.atan2(jammer.y_m - y, jammer.x_m - x)))
        err = bearingnoise.error_degrees(self.seed, channel, x, y,
                                         self.rules.bearing_noise_grid_m)
        hundredths = bearingnoise.quantize_bearing_hundredths(true_bearing, err)
        return round(_normalize_bearing(true_bearing + err), 2), hundredths

    # ---- 动作 ----
    def enter(self) -> EngineResult:
        if self.entered:
            return EngineResult(outcome="rejected", diagnostic="already_entered")
        self.entered = True
        self.x = self.y = 0.0
        self.last_x = self.last_y = 0.0
        self.channel = 1
        return EngineResult(accepted=True, outcome="entered",
                            virtual_time_us=self.virtual_time_us,
                            has_position=True, x=self.x, y=self.y)

    def measure(self, x: float, y: float, channel: int) -> EngineResult:
        move = self._move_duration_us(x, y)
        switch = 0
        if channel != self.channel:
            switch = self._us(self.rules.channel_switch_duration_s)
            self.channel_switch_count += 1
            self.channel = channel
        self.virtual_time_us += move + switch + self._us(self.rules.measure_duration_s)
        self.last_x, self.last_y = x, y
        self.x, self.y = x, y
        self.measure_accepted_count += 1

        res = EngineResult(
            accepted=True, virtual_time_us=self.virtual_time_us,
            has_position=True, x=x, y=y, has_channel=True, channel=channel,
        )
        jammer = self._jammer_on_channel(channel)
        if jammer is None:
            res.outcome = "no_signal"
            return res
        dist = math.hypot(x - jammer.x_m, y - jammer.y_m)
        if dist > jammer.receive_m:
            res.outcome = "no_signal"
            return res
        if not self._in_directional_coverage(jammer, x, y):
            res.outcome = "no_signal"
            return res
        if dist <= self.rules.near_distance_m:
            res.outcome = "near"
            return res
        svd, hundredths = self._svd_deg(jammer, x, y, channel)
        res.outcome = "direction"
        res.svd_deg = svd
        res.has_bearing = True
        res.bearing_hundredths = hundredths
        return res

    def clear(self, x: float, y: float, channel: int) -> EngineResult:
        move = self._move_duration_us(x, y)
        self.last_x, self.last_y = x, y
        self.x, self.y = x, y

        jammer = self._jammer_on_channel(channel)
        if jammer is not None and math.hypot(x - jammer.x_m, y - jammer.y_m) <= self.rules.clear_radius_m:
            jammer.cleared = True
            self.cleared_jammer_count += 1
            dur = self._us(self.rules.clear_success_duration_s)
            outcome = "success"
        else:
            self.clear_failure_count += 1
            dur = self._us(self.rules.clear_no_target_duration_s)
            outcome = "no_target_in_range"
        self.virtual_time_us += move + dur
        return EngineResult(
            accepted=True, outcome=outcome, virtual_time_us=self.virtual_time_us,
            has_position=True, x=x, y=y, has_channel=True, channel=channel,
            cleared_jammer_channel=(channel if outcome == "success" else None),
        )

    def exit(self) -> EngineResult:
        if not self.entered:
            return EngineResult(outcome="rejected", diagnostic="not_entered")
        return EngineResult(accepted=True, outcome="user_exit",
                            virtual_time_us=self.virtual_time_us)