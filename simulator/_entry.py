# -*- coding: utf-8 -*-
"""命令行入口，run.py 与 python3 -m simulator 共用"""
from __future__ import annotations

import argparse
import signal
import sys

from .config import Config, load_config, save_config
from .session import SessionManager
from .store import PracticeStatsStore
from .robot_api import RobotAPI
from .webui import WebUI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="无线电干扰源环境模拟器（本地演练版，Python）")
    p.add_argument("--robot-host", default=None, help="机器狗接口监听地址（默认 127.0.0.1）")
    p.add_argument("--robot-port", type=int, default=None, help="机器狗接口端口（默认 2026）")
    p.add_argument("--web-host", default=None, help="控制台监听地址（默认 127.0.0.1）")
    p.add_argument("--web-port", type=int, default=None, help="控制台端口（默认 8080）")
    p.add_argument("--window", type=int, default=None, help="测试窗口秒数（默认 1500）")
    p.add_argument("--countdown", type=int, default=None, help="准备倒计时秒数（缺省取配置，默认 5）")
    p.add_argument("--team", default=None, help="参赛队号（本地标识）")
    p.add_argument("--config", default=None, help="配置文件路径（默认 data/jammers-simulator.config.json）")
    p.add_argument("--demo", action="store_true", help="使用固定演示场景（便于确定性联调）")
    return p


def apply_overrides(cfg: Config, args) -> None:
    if args.robot_host: cfg.robot_host = args.robot_host
    if args.robot_port: cfg.robot_port = args.robot_port
    if args.web_host: cfg.web_host = args.web_host
    if args.web_port: cfg.web_port = args.web_port
    if args.window: cfg.window_seconds = args.window
    if args.countdown: cfg.countdown_seconds = args.countdown
    if args.team: cfg.team_no = args.team


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    apply_overrides(cfg, args)
    cfg.resolved_data_dir.mkdir(parents=True, exist_ok=True)
    save_config(cfg)

    demo_scenario = None
    if args.demo:
        from .scenario import demo_scenario
        demo_scenario = demo_scenario(4)   # 演示场景含定向，对应问题4

    store = PracticeStatsStore(cfg.resolved_data_dir)
    manager = SessionManager(cfg, store)

    if demo_scenario is not None:
        ok, msg = manager.start(4, scenario=demo_scenario)
        print(f"[demo] 启动演示会话(问题4): {msg}")

    robot = RobotAPI(cfg, manager)
    web = WebUI(cfg, manager, store)

    ok_r, msg_r = robot.start()
    ok_w, msg_w = web.start()
    print("=" * 60)
    print("无线电干扰源环境模拟器 · 本地演练版")
    print("=" * 60)
    print(f"机器狗接口 : {msg_r}")
    print(f"Web 控制台 : {msg_w}")
    print(f"数据目录   : {cfg.resolved_data_dir}")
    print("按 Ctrl+C 退出")
    print("=" * 60)

    if not (ok_r and ok_w):
        robot.stop(); web.stop()
        print("机器狗接口启动失败" if not ok_r else "控制台启动失败", file=sys.stderr)
        return 1

    stop_evt = _install_signal_handlers()
    try:
        while not stop_evt.is_set():
            stop_evt.wait(timeout=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n正在关闭…")
        robot.stop(); web.stop(); store.close()
    return 0


def _install_signal_handlers():
    import threading
    stop_evt = threading.Event()

    def _sig(*_):
        stop_evt.set()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    return stop_evt


if __name__ == "__main__":
    sys.exit(main())