#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无线电干扰源环境模拟器（本地演练版）一键启动。

用法：
    python3 run.py                      # 默认配置启动
    python3 run.py --robot-port 2026 --web-port 8080
    python3 run.py --window 300 --countdown 5   # 缩短窗口便于联调
    python3 run.py --demo               # 使用固定演示场景

数据落在 ./data/（SQLite 统计 + behavior-logs/ 行为日志），
配置写在 data/jammers-simulator.config.json。
"""
from __future__ import annotations

import sys

from simulator._entry import main

if __name__ == "__main__":
    sys.exit(main())