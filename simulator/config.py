# -*- coding: utf-8 -*-
"""配置读写，对应 data/jammers-simulator.config.json"""
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
COORD_ABS_MAX_M = 2_000_000           # 单个坐标分量的绝对值上限，单位米
BODY_MAX_BYTES = 65_536               # 请求体上限
CHANNEL_MIN, CHANNEL_MAX = 1, 20      # 有效频道范围
JAMMER_COUNT_MIN, JAMMER_COUNT_MAX = 10, 16


def _default_data_dir() -> Path:
    """返回项目根目录下的 data 目录，配置和数据都默认放这儿"""
    here = Path(__file__).resolve().parent.parent
    return here / "data"


@dataclass
class SimulationRules:
    """物理规则，对应官方题目设定，做成可配置是为了方便联调"""
    # 目标区域
    arena_radius_m: float = 1_800.0
    coord_abs_max_m: float = 2_000_000.0
    # 生成圆盘在目标区域基础上内缩的边距，官方是 1800-30=1770 m
    generation_disk_margin_m: float = 30.0
    # 干扰源
    jammer_count_min: int = 10
    jammer_count_max: int = 16
    channel_min: int = 1
    channel_max: int = 20
    receive_radius_min_m: float = 1_000.0   # 有效接收半径下限
    receive_radius_max_m: float = 1_500.0   # 上限
    # 定向有效覆盖角：全角 180°，半角 90°，边界算在内。
    # 依据 simcore.directionalCoverage 反汇编：硬编码比较 |Δ| <= 90.000000001，
    # 对应规则字段 directional_beam_width_udeg = 180000000。
    directional_beam_width_deg: float = 180.0
    # 示向度误差上限 ±1°：由 bearingnoise 噪声值域 U(-1,1) 直接决定，
    # 对应规则字段 bearing_error_max_udeg = 1000000。
    bearing_error_max_deg: float = 1.0
    # 噪声网格间距，单位米：对应规则字段 bearing_noise_grid_um = 150000000。
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
        """干扰源采样和校验用的圆盘半径，等于目标区域半径减去内缩边距，官方是 1770 m"""
        return self.arena_radius_m - self.generation_disk_margin_m

    def to_json(self) -> dict:
        """把物理规则导成普通字典，方便写进配置文件"""
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "SimulationRules":
        """从字典里挑出认得的规则字段，按字段类型转成 float 或 int 再建对象"""
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
    """模拟器的整体配置，服务地址、数据目录、测试窗口和物理规则都在这里"""
    # 服务
    robot_host: str = "127.0.0.1"
    robot_port: int = 2026
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    # 数据
    data_dir: str = ""            # 留空就用默认的，项目内 ./data
    # 测试窗口 / 准备倒计时
    window_seconds: int = WINDOW_SECONDS
    countdown_seconds: int = COUNTDOWN_SECONDS
    # 身份，本地标识，拿来当 robot_id 的校验基准
    team_no: str = "local-team"
    # 服务器，保留字段，本地版不连
    server_origin: str = ""
    robot_protocol_version: str = ROBOT_PROTOCOL_VERSION
    # 物理规则
    rules: SimulationRules = field(default_factory=SimulationRules)

    def to_json(self) -> dict:
        """导出成可写的字典，嵌套的规则单独转一遍"""
        d = asdict(self)
        d["rules"] = self.rules.to_json()
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Config":
        """丢掉不认识的键，把 rules 那一层还原成 SimulationRules 对象"""
        known = {f.name for f in cls.__dataclass_fields__.values()}
        kwargs = {k: v for k, v in d.items() if k in known}
        if "rules" in d and isinstance(d["rules"], dict):
            kwargs["rules"] = SimulationRules.from_json(d["rules"])
        return cls(**kwargs)

    @property
    def resolved_data_dir(self) -> Path:
        """给出实际使用的数据目录，配置里留空就退回默认目录"""
        if self.data_dir:
            return Path(self.data_dir).expanduser().resolve()
        return _default_data_dir()


def load_config(path: Path | str | None = None) -> Config:
    """读配置文件，不给路径就用默认目录下的那份，读不到或解析失败都退回默认配置"""
    cfg_path = Path(path) if path else _default_data_dir() / CONFIG_FILE_NAME
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                return Config.from_json(json.load(f))
        except Exception:
            pass
    return Config()


def save_config(cfg: Config, path: Path | str | None = None) -> Path:
    """先写临时文件再改名，避免写一半留下坏配置，返回最终路径"""
    cfg_path = Path(path) if path else cfg.resolved_data_dir / CONFIG_FILE_NAME
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg_path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg.to_json(), f, ensure_ascii=False, indent=2)
    tmp.replace(cfg_path)
    return cfg_path