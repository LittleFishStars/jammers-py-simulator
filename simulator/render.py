# -*- coding: utf-8 -*-
"""响应体渲染：手工拼 JSON，数字的文本形式有约定，移植自官方 renderResult"""
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
    """微秒转成秒的数字字面量，去掉尾随零，负数按向零取整"""
    us = int(virtual_time_us)
    sign = "-" if us < 0 else ""
    us = abs(us)
    whole, frac = divmod(us, 1_000_000)
    if frac == 0:
        return f"{sign}{whole}"
    # 有小数部分就走 "%d.%06d"，再去掉尾随零
    text = f"{sign}{whole}.{frac:06d}".rstrip("0")
    return text


def _quote(value: str) -> str:
    """把字符串转义成 JSON 字面量"""
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
