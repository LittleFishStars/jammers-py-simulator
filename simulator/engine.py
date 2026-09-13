# -*- coding: utf-8 -*-
"""机器狗 enter/measure/clear/exit 的物理判定与虚拟时钟推进"""
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
        """微秒整数对应的秒，只给展示用；响应体请走 render.virtual_seconds"""
        return self.virtual_time_us / 1_000_000
    svd_deg: float | None = None       # HTTP 响应字段，只有 direction 才返回
    has_position: bool = False
    x: float = 0.0
    y: float = 0.0
    has_channel: bool = False
    channel: int = 0
    has_bearing: bool = False
    bearing_hundredths: int = 0        # 行为日志字段，百分之一度的整数
    cleared_jammer_channel: int | None = None
    detected_jammer_channels: list[int] = field(default_factory=list)


def _normalize_bearing(deg: float) -> float:
    """把角度绕回 [0, 360)"""
    d = deg % 360.0
    return d if d >= 0 else d + 360.0


def _angle_diff(a: float, b: float) -> float:
    """两个方位角之间的最小夹角，落在 0..180"""
    d = abs(a - b) % 360.0
    return d if d <= 180.0 else 360.0 - d


def _noise_seed(scenario: Scenario) -> int:
    """由场景 noise_seed_hex 派生出 64 位噪声种子，官方那边是 uint64

    noise_seed_hex 是 16 位十六进制。缺失或者不合法时，退化成对场景标识做一次
    BLAKE2b-64 摘要，这样同一个场景的误差场照样可复现
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
        self.virtual_time_us = 0    # 官方 int64 微秒，每个动作按整数累加
        self.x = 0.0
        self.y = 0.0
        self.channel = 1           # 测向机当前频道
        self.entered = False
        self.last_x = 0.0          # 上一次合法动作的位置
        self.last_y = 0.0

        # 统计，对齐官方统计表的列
        self.entered = False
        self.cleared_jammer_count = 0
        self.measure_accepted_count = 0
        self.channel_switch_count = 0
        self.clear_failure_count = 0
        self.jammer_count = len(scenario.jammers)

        # 示向度噪声种子，对应官方 Engine+0x80 的场景级种子
        self.seed = _noise_seed(scenario)

    @property
    def virtual_time_s(self) -> float:
        """微秒整数对应的秒，只给展示和日志用；响应体请用 render.virtual_seconds"""
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
        """秒转成微秒整数，官方规则字段本身就是整数微秒"""
        return int(round(seconds * 1_000_000))

    def _move_duration_us(self, x: float, y: float) -> int:
        """移动耗时，单位微秒

        官方 simcore.(*Engine).moveTo 的算法是 int64(1e12 * 距离m / speed_um_per_s)：
        先乘 1e12，再除以以微米每秒表示的速度，结果向零截断成整数微秒。浮点运算的
        先后顺序必须原样保留，换个顺序末位微秒就可能不一样
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
        """检测点是否落在定向干扰源的有效覆盖角度之内，边界算在内

        对应官方 simcore.directionalCoverage。全向源直接算覆盖；定向源判
        |Δ| <= 90° + 1e-9，半角 90°、全角 180°，Δ 是干扰源指向检测点的方位角
        与干扰源朝向之差。注意这个函数只管角度，距离由接收半径另外判
        """
        if jammer.kind != KIND_DIRECTIONAL or jammer.direction_deg is None:
            return True
        bearing = _normalize_bearing(math.degrees(math.atan2(y - jammer.y_m, x - jammer.x_m)))
        half = self.rules.directional_beam_width_deg / 2.0
        return _angle_diff(bearing, jammer.direction_deg) <= half + 1e-9

    def _svd_deg(self, jammer: Jammer, x: float, y: float,
                 channel: int) -> tuple[float, int]:
        """检测点指向干扰源的方位角，再加一层确定性空间噪声

        返回 (svd_deg 保留两位小数, bearing_hundredths 百分之一度的整数)。
        误差只跟噪声种子、频道、测量位置有关，同一位置同一频道重复测误差一模一样，
        取平均压不下去
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