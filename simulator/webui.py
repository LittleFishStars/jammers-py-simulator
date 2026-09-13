# -*- coding: utf-8 -*-
"""Web 控制台服务器，服务 web/ 下的页面并转发 REST API"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Config, save_config
from .scenario import Scenario, generate_scenario
from .session import SessionManager
from .store import PracticeStatsStore

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# 可经 /api/config 与 /api/scenario 调整的规则字段
_RULE_FIELDS = (
    "arena_radius_m", "coord_abs_max_m", "generation_disk_margin_m",
    "jammer_count_min", "jammer_count_max",
    "channel_min", "channel_max",
    "receive_radius_min_m", "receive_radius_max_m",
    "directional_beam_width_deg", "bearing_error_max_deg", "bearing_noise_grid_m",
    "near_distance_m", "clear_radius_m",
    "move_speed_m_per_s", "measure_duration_s",
    "clear_no_target_duration_s", "clear_success_duration_s",
    "channel_switch_duration_s",
    "max_virtual_duration_s", "max_program_duration_s",
)


class WebUI:
    """本地 Web 控制台，起停 HTTP 服务并把请求交给 WebHandler 处理"""
    def __init__(self, cfg: Config, manager: SessionManager, store: PracticeStatsStore):
        """记下配置、会话管理器和统计库，服务对象与线程先留空"""
        self.cfg = cfg
        self.manager = manager
        self.store = store
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> tuple[bool, str]:
        """在配置的端口上起后台线程跑 HTTP 服务，返回是否成功和一句提示"""
        if self._server is not None:
            return False, "已在运行"

        class Handler(WebHandler):
            """把当前 WebUI 实例绑进类属性，供各 API 方法读取"""
            ui = self

        try:
            self._server = ThreadingHTTPServer((self.cfg.web_host, self.cfg.web_port), Handler)
        except OSError as e:
            return False, f"控制台端口不可用: {e}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return True, f"控制台已开启 http://{self.cfg.web_host}:{self.cfg.web_port}"

    def stop(self):
        """停掉监听套接字并等后台线程退出，最多等 2 秒"""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    @property
    def running(self) -> bool:
        """服务是否已经起来"""
        return self._server is not None


class WebHandler(BaseHTTPRequestHandler):
    """控制台页面的请求处理器，管静态文件和 /api 路由"""
    ui: WebUI = None  # 由 WebUI.start 注入

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        """吞掉基类默认打到 stderr 的访问日志，控制台自己不需要"""
        pass

    def _send_json(self, code: int, obj: dict):
        """按给定状态码回一份 JSON，内容以 UTF-8 编码并禁止缓存"""
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, rel: str, ctype: str):
        """发 web/ 目录下的文件，路径越界回 403，读不到回 404"""
        path = (WEB_DIR / rel).resolve()
        if WEB_DIR not in path.parents and path != WEB_DIR:
            self._send_json(403, {"error": "forbidden"})
            return
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(404, {"error": "not_found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self, max_len: int = 262_144) -> dict | None:
        """读请求体解析成字典，长度超限或不是 JSON 对象时返回 None"""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if length <= 0 or length > max_len:
            return None
        raw = self.rfile.read(length)
        try:
            d = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return d if isinstance(d, dict) else None

    # ---- 路由 ----
    def do_GET(self):
        """分发 GET，页面和脚本走 _send_file，状态和历史走各自的 API"""
        path = self.path.split("?")[0]
        if path == "/":
            self._send_file("index.html", "text/html; charset=utf-8")
        elif path == "/app.js":
            self._send_file("app.js", "application/javascript; charset=utf-8")
        elif path == "/style.css":
            self._send_file("style.css", "text/css; charset=utf-8")
        elif path == "/api/state":
            self._api_state()
        elif path == "/api/history":
            self._api_history()
        else:
            self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        """按路径查表分发 POST，查不到的路径回 404"""
        path = self.path.split("?")[0]
        routes = {
            "/api/start": self._api_start,
            "/api/abort": self._api_abort,
            "/api/clear": self._api_clear,
            "/api/scenario": self._api_scenario,
            "/api/config": self._api_config,
            "/api/history/clear": self._api_history_clear,
        }
        handler = routes.get(path)
        if handler is None:
            self._send_json(404, {"error": "not_found"})
            return
        handler()

    # ---- API ----
    def _api_state(self):
        """回会话快照，外带端口、窗口时长等页面要显示的服务端配置"""
        snap = self.ui.manager.snapshot()
        snap["config"] = {
            "robot_port": self.ui.cfg.robot_port,
            "web_port": self.ui.cfg.web_port,
            "window_seconds": self.ui.cfg.window_seconds,
            "countdown_seconds": self.ui.cfg.countdown_seconds,
            "team_no": self.ui.cfg.team_no,
            "rules": self.ui.cfg.rules.to_json(),
        }
        self._send_json(200, snap)

    def _api_start(self):
        """开一次演练，请求里带了场景就先校验再交给会话管理器"""
        req = self._read_json() or {}
        problem_no = int(req.get("problem_no", 3))
        scenario = None
        if req.get("scenario"):
            try:
                scenario = Scenario.from_json(req["scenario"])
                scenario.validate(problem_no, self.ui.cfg.rules.generation_disk_radius_m)
            except Exception as e:
                self._send_json(400, {"ok": False, "error": f"场景无效: {e}"})
                return
        ok, msg = self.ui.manager.start(problem_no, scenario=scenario)
        self._send_json(200 if ok else 409, {"ok": ok, "message": msg})

    def _api_abort(self):
        """中止正在跑的演练，没得中止时回 409"""
        ok, msg = self.ui.manager.abort()
        self._send_json(200 if ok else 409, {"ok": ok, "message": msg})

    def _api_clear(self):
        """清掉已结束的会话，返回清掉了没有"""
        ok = self.ui.manager.clear_finished()
        self._send_json(200, {"ok": ok})

    def _api_scenario(self):
        """把请求里的规则字段存盘，再按新规则生成并校验一个场景"""
        req = self._read_json() or {}
        problem_no = int(req.get("problem_no", 4))
        if problem_no not in (3, 4):
            self._send_json(400, {"ok": False, "error": "problem_no 必须为 3 或 4"})
            return
        rules = self.ui.cfg.rules
        for k in _RULE_FIELDS:
            if k in req:
                try:
                    setattr(rules, k, float(req[k]))
                except (TypeError, ValueError):
                    self._send_json(400, {"ok": False, "error": f"参数 {k} 无效"})
                    return
        save_config(self.ui.cfg)
        try:
            sc = generate_scenario(rules, problem_no, seed=req.get("seed"))
            sc.validate(problem_no, rules.generation_disk_radius_m)
        except Exception as e:
            self._send_json(400, {"ok": False, "error": str(e)})
            return
        self._send_json(200, {"ok": True, "scenario": sc.to_json()})

    def _api_config(self):
        """改端口和窗口时长等运行配置并存盘，越界的值直接拒绝"""
        req = self._read_json() or {}
        changed = []
        for k in ("robot_port", "web_port", "window_seconds", "countdown_seconds", "team_no"):
            if k in req:
                val = int(req[k]) if k != "team_no" else str(req[k])
                if k != "team_no":
                    if "port" in k and not (1 <= val <= 65535):
                        self._send_json(400, {"ok": False, "error": f"参数 {k} 越界"})
                        return
                    if k in ("window_seconds", "countdown_seconds") and not (0 < val <= 3_600_000):
                        self._send_json(400, {"ok": False, "error": f"参数 {k} 越界"})
                        return
                setattr(self.ui.cfg, k, val)
                changed.append(k)
        if "rules" in req and isinstance(req["rules"], dict):
            for k, v in req["rules"].items():
                if k in _RULE_FIELDS:
                    setattr(self.ui.cfg.rules, k, float(v))
                    changed.append(f"rules.{k}")
        try:
            save_config(self.ui.cfg)
        except OSError as e:
            self._send_json(500, {"ok": False, "error": f"设置无法保存: {e}", "changed": changed})
            return
        self._send_json(200, {"ok": True, "changed": changed,
                              "note": "端口等项需重启进程后生效"})

    def _api_history(self):
        """回最近 200 条练习记录给历史表格"""
        self._send_json(200, {"rows": self.ui.store.list_results(200)})

    def _api_history_clear(self):
        """清空练习历史，回删掉的条数"""
        n = self.ui.store.clear_results()
        self._send_json(200, {"ok": True, "deleted": n})