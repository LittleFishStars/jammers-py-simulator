# -*- coding: utf-8 -*-
"""响应体渲染：官方 robotapi.renderResult 的忠实移植

官方不用 encoding/json，而是手工拼装 JSON 字符串，所以数字的文本形式有严格约定，
必须逐字节复刻：

  virtual_time_s   → virtualSeconds(µs)：整数秒写成 "%d"，比如 105；
                     否则 "%d.%06d" 再去掉尾随零，比如 105.500001
  svd_deg          → "%d.%02d"：恒为两位小数，比如 270.02、270.00
  max_virtual_duration_s / max_real_duration_s → 整数，360000 / 1200
  remaining_real_duration_s → 整数

这正是 Python 的 json.dumps 做不到的：它会把 105 写成 105.0、把 270.00 写成 270.0，
所以这里照官方做法手工拼接；字符串值仍用 json.dumps 转义。

反汇编依据：
  robotapi.renderResult          VA 0x1404ff7e0
  robotapi.virtualSeconds        VA 0x1404ffc00
  格式串 "%d"/"%d.%06d"/"%d.%02d" @ 0x1407f30fa / 0x1407f4879 / 0x1407fd630
"""
from __future__ import annotations

import json

# 官方 renderResult 的固定骨架，顺序照代码来
_ACCEPTED_PREFIX = '{"accepted":true,"real_timestamp_ms":%d,"virtual_time_s":%s'
_MEASURE_SUFFIX = ',"measure_result":"%s"'
_SVD_SUFFIX = ',"svd_deg":%d.%02d'
_CLEAR_SUFFIX = ',"clear_result":"%s"'
# 官方把这两个常量直接烧进字符串
_ENTER_SUFFIX = (',"max_virtual_duration_s":360000,"max_real_duration_s":1200,'
                 '"remaining_real_duration_s":%d')
_EXIT_SUFFIX = ',"exit_reason":"user_exit"'
# accepted=false 恒为固定 3 字段
_REJECTED = '{"accepted":false,"real_timestamp_ms":%d,"virtual_time_s":0}'


def virtual_seconds(virtual_time_us: int) -> str:
    """官方 robotapi.virtualSeconds：微秒 → 无尾随零的秒数字面量

    整数秒输出 "%d"；否则 "%d.%06d" 后 TrimRight("0")。
    负数按 Go 的整数除法向零取整。
    """
    us = int(virtual_time_us)
    sign = "-" if us < 0 else ""
    us = abs(us)
    whole, frac = divmod(us, 1_000_000)
    if frac == 0:
        return f"{sign}{whole}"
    text = f"{sign}{whole}.{frac:06d}".rstrip("0")
    return text


def _quote(value: str) -> str:
    """字符串值转义，只用于 result 枚举与诊断文本"""
    return json.dumps(value, ensure_ascii=False)


def render_accepted(path: str, real_timestamp_ms: int, virtual_time_us: int,
                    result: str = "", svd_hundredths: int | None = None,
                    remaining_real_duration_s: int = 0) -> str:
    """渲染 accepted=true 的响应体，走的还是官方 renderResult 那套拼接顺序"""
    parts = [_ACCEPTED_PREFIX % (int(real_timestamp_ms), virtual_seconds(virtual_time_us))]

    if path == "/measure":
        parts.append(_MEASURE_SUFFIX % result)
        if result == "direction" and svd_hundredths is not None:
            h = int(svd_hundredths) % 36000
            parts.append(_SVD_SUFFIX % (h // 100, h % 100))
    elif path == "/clear":
        parts.append(_CLEAR_SUFFIX % result)
    elif path == "/enter":
        parts.append(_ENTER_SUFFIX % int(remaining_real_duration_s))
    elif path == "/exit":
        parts.append(_EXIT_SUFFIX)

    parts.append("}")
    return "".join(parts)


def render_rejected(real_timestamp_ms: int) -> str:
    return _REJECTED % int(real_timestamp_ms)
