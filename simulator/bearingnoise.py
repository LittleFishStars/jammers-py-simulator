# -*- coding: utf-8 -*-
"""示向度误差噪声：确定性空间哈希噪声，移植自官方 bearingnoise"""
from __future__ import annotations

import hashlib
import math

# 官方常量，都跟反汇编核对过
_NOISE_GRID_M = 150.0          # 噪声网格间距，对应 bearing_noise_grid_um = 150000000
_SMOOTHSTEP_C = 3.0            # fx^2 (3 - 2 fx)
_TWO_POW_64 = 2.0 ** 64
_HUNDREDTHS_PER_TURN = 36000   # 360° × 100


def _grid(seed: int, salt: int, ix: int, iy: int) -> float:
    """官方 bearingnoise.grid：BLAKE2b-64 摘要 → [0,1) → 2u-1 ∈ [-1, 1)"""
    message = f"{seed}:{salt}:{ix}:{iy}".encode("utf-8")
    digest = hashlib.blake2b(message, digest_size=8).digest()
    # 反汇编：读 8 字节后 bswap → 大端解释
    value = int.from_bytes(digest, "big")
    return 2.0 * (value / _TWO_POW_64) - 1.0


def _smoothstep(t: float) -> float:
    return t * t * (_SMOOTHSTEP_C - 2.0 * t)


def _clamp01(t: float) -> float:
    if t < 0.0:
        return 0.0
    if t > 1.0:
        return 1.0
    return t


def error_degrees(seed: int, salt: int, x_m: float, y_m: float,
                  grid_m: float = _NOISE_GRID_M) -> float:
    """返回示向度误差，单位为度，值域 (-1, 1)，同一个位置同一个频道恒定"""
    gx = x_m / grid_m
    gy = y_m / grid_m
    ix = math.floor(gx)
    iy = math.floor(gy)
    fx = _clamp01(gx - ix)
    fy = _clamp01(gy - iy)
    sx = _smoothstep(fx)
    sy = _smoothstep(fy)

    v00 = _grid(seed, salt, ix, iy)
    v10 = _grid(seed, salt, ix + 1, iy)
    v01 = _grid(seed, salt, ix, iy + 1)
    v11 = _grid(seed, salt, ix + 1, iy + 1)

    near = v00 + sx * (v10 - v00)     # iy 行沿 x 插值
    far = v01 + sx * (v11 - v01)      # iy+1 行沿 x 插值
    return near + sy * (far - near)   # 沿 y 插值


def quantize_bearing_hundredths(bearing_deg: float, error_deg: float) -> int:
    """真实示向度加误差后量化到百分之一度并回绕到 [0, 36000)"""
    total = (bearing_deg + error_deg) % 360.0
    # 加 1e-9 抵消二进制浮点误差，否则 359.20 会截断成 359.19
    hundredths = int(total * 100.0 + 1e-9)
    return hundredths % _HUNDREDTHS_PER_TURN
