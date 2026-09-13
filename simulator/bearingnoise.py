# -*- coding: utf-8 -*-
"""示向度误差噪声：官方 `jammers/client/internal/bearingnoise` 包的忠实移植

官方模型是确定性空间哈希噪声，既不是逐次随机，也不是高斯：

    grid(seed, salt, ix, iy) = 2u - 1,
        u = BE_uint64( BLAKE2b-64("%d:%d:%d:%d" % (seed, salt, ix, iy)) ) / 2^64

每个网格节点的值都服从 U(-1, 1)，节点之间相互独立。误差场由这些节点的值插值而成，
用的是 smoothstep 双线性插值：

    ErrorDegrees(seed, salt, x, y) = 双线性插值( grid 四角 )
        网格间距 150 m，ix = floor(x/150)，iy = floor(y/150)
        插值权重 a = smoothstep(frac(x/150)) = fx^2 (3 - 2 fx)

下面几条性质对建模有直接影响，别把它当普通随机噪声处理：

  * 值域严格为 (-1°, 1°)，与规则字段 bearing_error_max_udeg = 1° 一致
  * 均值 0；标准差 = sqrt(Σw^2 / 3) ∈ [0.289°, 0.577°]，典型 ≈ 0.4°
  * 误差只由噪声种子、频道、测量位置三者决定。同一位置同一频道重复测量，误差完全相同，
    取平均无法减小误差
  * 相关长度 ≈ 150 m，同一网格内的多点误差是同向系统偏差，不能相互抵消
  * salt 取请求频道，所以不同频道是相互独立的误差场

反汇编依据（jammers-simulator.exe）：
  bearingnoise.ErrorDegrees            VA 0x1404ee680
  bearingnoise.QuantizeBearingHundredths VA 0x1404ee900
  bearingnoise.grid                    VA 0x1404eeac0
  常量 2^-53 / 2π / 150.0 / 3.0 / 1.0 / 100.0 均已逐一核对
"""
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
    """官方 bearingnoise.ErrorDegrees：返回示向度误差，单位为度，值域 (-1, 1)

    seed 是场景噪声种子，salt 是请求频道，x_m/y_m 是机器狗本次的测量位置，单位米
    """
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
    """官方 bearingnoise.QuantizeBearingHundredths：量化到百分之一度并回绕

    先把真实示向度加误差归一化到 [0, 360)，再截断到 0.01°，最后回绕到 [0, 36000)。
    加 1e-9 只为抵消二进制浮点的表示误差，比如 359.20 会存成 359.19999…。
    """
    total = (bearing_deg + error_deg) % 360.0
    hundredths = int(total * 100.0 + 1e-9)
    return hundredths % _HUNDREDTHS_PER_TURN
