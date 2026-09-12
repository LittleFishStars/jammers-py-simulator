# -*- coding: utf-8 -*-
"""配置管理：jammers-simulator.config.json

物理规则对齐官方《模拟器使用说明》/《通信接口说明及编程指南》（附件1、附件2）：
  - 目标区域：半径 1800 米圆形，圆心 (0,0)，X 东 Y 北，坐标单位米
  - 移动速度 5 m/s；检测 5s；清除未发现 3s/成功 5s；切换频道 1s
  - 虚拟限时 360000s；现实限时 1200s；窗口 1500s；倒计时 5s
  - 频道 1..20；干扰源 10..16；接收半径 1000..1500m；近距 5m；清除 20m
  - 定向覆盖角 60°、示向度误差 ±0.5°（文档中该两数值被格式丢失，采用官方
    二进制 simulation-rules 默认值 directional_beam_width_udeg / bearing_error_max_udeg）
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

CONFIG_FILE_NAME = "jammers-simulator.config.json"
ROBOT_PROTOCOL_VERSION = "robot-protocol-v1"
SCENARIO_SCHEMA_VERSION = "scenario-v1"
RULESET_VERSION = "rules-v1"

# 官方常量
MAX_VIRTUAL_DURATION_S = 360_000      # 虚拟限时 100 小时
MAX_PROGRAM_DURATION_S = 1_200        # 现实限时 20 分钟
WINDOW_SECONDS = 1_500                # 测试窗口 25 分钟
COUNTDOWN_SECONDS = 5                 # 准备倒计时
COORD_ABS_MAX_M = 2_000_000           # 坐标分量绝对值上限（米）
BODY_MAX_BYTES = 65_536               # 请求体上限
CHANNEL_MIN, CHANNEL_MAX = 1, 20      # 有效频道范围
JAMMER_COUNT_MIN, JAMMER_COUNT_MAX = 10, 16


def _default_data_dir() -> Path:
    here = Path(__file__).resolve().parent.parent
    return here / "data"


@dataclass
class SimulationRules:
    """物理规则（对应官方题目设定，可配置以便联调）。"""
    # 目标区域
    arena_radius_m: float = 1_800.0
    coord_abs_max_m: float = 2_000_000.0
    # 生成圆盘在目标区域基础上内缩的边距（官方 1800-30=1770 m）
    generation_disk_margin_m: float = 30.0
    # 干扰源
    jammer_count_min: int = 10
    jammer_count_max: int = 16
    channel_min: int = 1
    channel_max: int = 20
    receive_radius_min_m: float = 1_000.0   # 有效接收半径下限
    receive_radius_max_m: float = 1_500.0   # 上限
    # 定向有效覆盖角：全角 180°（半角 90°，含边界）。
    # 依据 simcore.directionalCoverage 反汇编：硬编码比较 |Δ| <= 90.000000001，
    # 对应规则字段 directional_beam_width_udeg = 180000000。
    directional_beam_width_deg: float = 180.0
    # 示向度误差上限（±1°）：由 bearingnoise 噪声值域 U(-1,1) 直接决定，
    # 对应规则字段 bearing_error_max_udeg = 1000000。
    bearing_error_max_deg: float = 1.0
    # 噪声网格间距（米）：对应规则字段 bearing_noise_grid_um = 150000000。
    bearing_noise_grid_m: float = 150.0
    # 判定阈值
    near_distance_m: float = 5.0            # 近距离阈值
    clear_radius_m: float = 20.0            # 清除半径
    # 时间推进
    move_speed_m_per_s: float = 5.0
    measure_duration_s: float = 5.0
    clear_no_target_duration_s: float = 3.0
    clear_success_duration_s: float = 5.0
    channel_switch_duration_s: float = 1.0
    # 时限
    max_virtual_duration_s: float = MAX_VIRTUAL_DURATION_S
    max_program_duration_s: float = MAX_PROGRAM_DURATION_S

    @property
    def generation_disk_radius_m(self) -> float:
        """干扰源采样/校验圆盘半径 = 目标区域半径 - 内缩边距（官方 1770 m）。"""
        return self.arena_radius_m - self.generation_disk_margin_m

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "SimulationRules":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        out = {}
        for k, v in d.items():
            if k in known and isinstance(v, (int, float)):
                out[k] = float(v) if k in ("arena_radius_m", "coord_abs_max_m",
                                           "receive_radius_min_m", "receive_radius_max_m",
                                           "directional_beam_width_deg", "bearing_error_max_deg",
                                           "bearing_noise_grid_m",
                                           "near_distance_m", "clear_radius_m",
                                           "move_speed_m_per_s", "measure_duration_s",
                                           "clear_no_target_duration_s", "clear_success_duration_s",
                                           "channel_switch_duration_s", "max_virtual_duration_s",
                                           "max_program_duration_s") else int(v)
        return cls(**out)


@dataclass
class Config:
    # 服务
    robot_host: str = "127.0.0.1"
    robot_port: int = 2026
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    # 数据
    data_dir: str = ""            # 留空则用默认（项目内 ./data）
    # 测试窗口 / 准备倒计时
    window_seconds: int = WINDOW_SECONDS
    countdown_seconds: int = COUNTDOWN_SECONDS
    # 身份（本地标识，作为 robot_id 校验基准）
    team_no: str = "local-team"
    # 服务器（保留字段，本地版不连接）
    server_origin: str = ""
    robot_protocol_version: str = ROBOT_PROTOCOL_VERSION
    # 物理规则
    rules: SimulationRules = field(default_factory=SimulationRules)

    def to_json(self) -> dict:
        d = asdict(self)
        d["rules"] = self.rules.to_json()
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Config":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        kwargs = {k: v for k, v in d.items() if k in known}
        if "rules" in d and isinstance(d["rules"], dict):
            kwargs["rules"] = SimulationRules.from_json(d["rules"])
        return cls(**kwargs)

    @property
    def resolved_data_dir(self) -> Path:
        if self.data_dir:
            return Path(self.data_dir).expanduser().resolve()
        return _default_data_dir()


def load_config(path: Path | str | None = None) -> Config:
    cfg_path = Path(path) if path else _default_data_dir() / CONFIG_FILE_NAME
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                return Config.from_json(json.load(f))
        except Exception:
            pass
    return Config()


def save_config(cfg: Config, path: Path | str | None = None) -> Path:
    cfg_path = Path(path) if path else cfg.resolved_data_dir / CONFIG_FILE_NAME
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg_path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg.to_json(), f, ensure_ascii=False, indent=2)
    tmp.replace(cfg_path)
    return cfg_path