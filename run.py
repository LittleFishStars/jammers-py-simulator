#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无线电干扰源环境模拟器一键启动，跑本地演练版"""
from __future__ import annotations

import sys

from simulator._entry import main

if __name__ == "__main__":
    sys.exit(main())