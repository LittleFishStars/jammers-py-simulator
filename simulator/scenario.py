# -*- coding: utf-8 -*-
"""干扰源场景模型与生成器，schema 版本 scenario-v1

对齐官方《通信接口说明及编程指南》：

  - 目标区域是半径 1800 米的圆，圆心 (0,0)；干扰源都落在 1770 米的生成圆盘里，内缩了 30m
  - 干扰源 10..16 个；频道 1..20 且唯一
  - 有效接收半径 1000..1500 米
  - 分全向 omni 与定向 directional 两类；定向覆盖角由 rules 提供，见 config.directional_beam_width_deg，默认 180°
  - 位置单位米，x 朝东、y 朝北
"""
from __future__ import annotations

import math
import random
import secrets
from dataclasses import dataclass, field, asdict

from .config import SimulationRules

KIND_OMNI = "omni"
KIND_DIRECTIONAL = "directional"


@dataclass
class Jammer:
    channel: int                 # 1..20，唯一
    kind: str                    # omni / directional
    x_m: float
    y_m: float
    receive_m: float             # 有效接收半径（1000..1500）
    radius_m: float              # 干扰源物理半径；文档没给它与接收半径的关系，本地拿 receive 顶替
    direction_deg: float | None  # 定向源朝向；omni 必须为 None
    theta_deg: float | None = None  # 保留字段，官方 scenario-v1 里有，含义见题目
    cleared: bool = False

    def to_json(self) -> dict:
        d = {
            "channel": self.channel,
            "kind": self.kind,
            "position": {"x": self.x_m, "y": self.y_m},
            "radius": self.radius_m,
            "receive": self.receive_m,
            "direction": self.direction_deg,
            "theta": self.theta_deg,
            "cleared": self.cleared,
        }
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Jammer":
        pos = d.get("position", {})
        return cls(
            channel=int(d["channel"]),
            kind=str(d["kind"]),
            x_m=float(pos.get("x", 0)),
            y_m=float(pos.get("y", 0)),
            receive_m=float(d.get("receive", 0)),
            radius_m=float(d.get("radius", d.get("receive", 0))),
            direction_deg=d.get("direction"),
            theta_deg=d.get("theta"),
            cleared=bool(d.get("cleared", False)),
        )


@dataclass
class Scenario:
    source: str = "practice_generated"
    generation_rules: str = "practice-gen-rules-v1"
    generator_seed_hex: str = ""
    noise_seed_hex: str = ""
    jammers: list[Jammer] = field(default_factory=list)

    @property
    def jammer_count(self) -> int:
        return len(self.jammers)

    def to_json(self) -> dict:
        return {
            "source": self.source,
            "generation_rules": self.generation_rules,
            "generator_seed_hex": self.generator_seed_hex,
            "noise_seed_hex": self.noise_seed_hex,
            "jammer_count": self.jammer_count,
            "jammers": [j.to_json() for j in self.jammers],
        }

    @classmethod
    def from_json(cls, d: dict) -> "Scenario":
        return cls(
            source=str(d.get("source", "practice_generated")),
            generation_rules=str(d.get("generation_rules", "practice-gen-rules-v1")),
            generator_seed_hex=str(d.get("generator_seed_hex", "")),
            noise_seed_hex=str(d.get("noise_seed_hex", "")),
            jammers=[Jammer.from_json(j) for j in d.get("jammers", [])],
        )

    def validate(self, problem_no: int | None = None,
                 disk_radius_m: float = 1770.0) -> None:
        rules_ok = self.generation_rules == "practice-gen-rules-v1"
        if not rules_ok:
            raise ValueError("generation_rules must be practice-gen-rules-v1")
        n = self.jammer_count
        if not (10 <= n <= 16):
            raise ValueError(f"jammer_count must be in 10..16, got {n}")
        chans = []
        directional = 0
        for j in self.jammers:
            if not (1 <= j.channel <= 20):
                raise ValueError(f"jammer {j.channel} channel is invalid")
            chans.append(j.channel)
            if j.kind not in (KIND_OMNI, KIND_DIRECTIONAL):
                raise ValueError(f"jammer {j.channel} kind is invalid")
            if j.kind == KIND_OMNI:
                if j.direction_deg is not None:
                    raise ValueError(f"jammer {j.channel} omni direction must be null")
            else:
                directional += 1
                d = j.direction_deg
                if d is None or not (0 <= d < 360):
                    raise ValueError(f"jammer {j.channel} directional angle is invalid")
            if not (1000.0 <= j.receive_m <= 1500.0):
                raise ValueError(f"jammer {j.channel} max_receive is out of range")
            if math.hypot(j.x_m, j.y_m) > disk_radius_m:
                raise ValueError(f"jammer {j.channel} position is outside the generation disk")
        if len(set(chans)) != len(chans) or chans != sorted(chans):
            raise ValueError("jammer channels must be unique and strictly increasing")
        if problem_no == 3 and directional != 0:
            raise ValueError("problem 3 cannot contain directional jammers")
        if problem_no == 4 and directional == 0:
            raise ValueError("problem 4 requires at least one directional jammer")


def _rand_seed_hex(n_bytes: int) -> str:
    return secrets.token_hex(n_bytes)


def generate_scenario(rules: SimulationRules, problem_no: int, seed: int | None = None) -> Scenario:
    """在生成圆盘里撒 10..16 个干扰源，圆盘默认 1770m

    位置取圆盘面积均匀分布：r = R·sqrt(u)、theta = 2πu₂，官方那边叫 uniform_disk_area。
    频道从 1..20 里随机抽 n 个再升序排，唯一且严格递增。receive 在 [1000,1500] 米之间随机。
    定向源的数量对齐官方 GeneratePractice：问题3 一个都不给，纯全向；问题4 至少 1 个，
    在 1..n 里随机。
    """
    rng = random.Random(seed)
    n = rng.randint(int(rules.jammer_count_min), int(rules.jammer_count_max))
    channels = sorted(rng.sample(range(int(rules.channel_min), int(rules.channel_max) + 1), n))
    if problem_no == 3:
        n_dir = 0
    else:
        n_dir = rng.randint(1, n)   # 问题4：至少 1 个定向
    kinds = [KIND_DIRECTIONAL] * n_dir + [KIND_OMNI] * (n - n_dir)
    rng.shuffle(kinds)

    jammers = []
    for ch, kind in zip(channels, kinds):
        # 圆盘面积均匀分布：r = R·sqrt(u)，theta 均匀。开方是关键，保证面密度恒定
        r = rules.generation_disk_radius_m * math.sqrt(rng.random())
        ang = rng.uniform(0, 2 * math.pi)
        x = r * math.cos(ang)
        y = r * math.sin(ang)
        receive = rng.uniform(rules.receive_radius_min_m, rules.receive_radius_max_m)
        if kind == KIND_DIRECTIONAL:
            direction = round(rng.uniform(0, 360), 2)
        else:
            direction = None
        jammers.append(Jammer(
            channel=ch, kind=kind, x_m=round(x, 2), y_m=round(y, 2),
            receive_m=round(receive, 2), radius_m=round(receive, 2),
            direction_deg=direction,
        ))
    return Scenario(
        source="practice_generated",
        generation_rules="practice-gen-rules-v1",
        generator_seed_hex=_rand_seed_hex(32),   # 64 hex
        noise_seed_hex=_rand_seed_hex(8),        # 16 hex
        jammers=jammers,
    )


def demo_scenario(problem_no: int = 4) -> Scenario:
    """确定性演示场景：10 个干扰源，便于联调，也满足官方 10..16 的约束

    问题4：ch2 为定向，朝向 90°，覆盖 0°..180°，其余全向；
    问题3：全部全向
    """
    positions = [
        (500.0, 0.0), (-800.0, 0.0), (0.0, 900.0), (-1200.0, 300.0),
        (1500.0, 500.0), (-500.0, -1000.0), (700.0, -1300.0), (100.0, -400.0),
        (-1600.0, -700.0), (300.0, 1600.0),
    ]
    receives = [1200.0, 1100.0, 1050.0, 1300.0, 1400.0, 1150.0, 1250.0, 1350.0, 1000.0, 1450.0]
    jammers = []
    for i, ((x, y), recv) in enumerate(zip(positions, receives), start=1):
        if i == 2 and problem_no == 4:
            jammers.append(Jammer(channel=i, kind=KIND_DIRECTIONAL, x_m=x, y_m=y,
                                  receive_m=recv, radius_m=recv, direction_deg=90.0))
        else:
            jammers.append(Jammer(channel=i, kind=KIND_OMNI, x_m=x, y_m=y,
                                  receive_m=recv, radius_m=recv, direction_deg=None))
    return Scenario(
        source="practice_generated",
        generation_rules="practice-gen-rules-v1",
        generator_seed_hex="d" * 64,
        noise_seed_hex="e" * 16,
        jammers=jammers,
    )