# -*- coding: utf-8 -*-
"""机器狗本地 HTTP 接口，字段与业务规则在 session.SessionManager.handle"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config, BODY_MAX_BYTES
from . import render
from .session import SessionManager

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_NOT_FOUND = 404
HTTP_METHOD_NOT_ALLOWED = 405
HTTP_CONFLICT = 409
HTTP_TOO_LARGE = 413
HTTP_UNSUPPORTED_MEDIA = 415
HTTP_TOO_MANY = 429
HTTP_INTERNAL = 500

KNOWN_PATHS = ("/enter", "/measure", "/clear", "/exit")
MAX_JSON_DEPTH = 16


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise _DuplicateKeyError(k)
        d[k] = v
    return d


def _json_depth(obj, depth: int = 1) -> int:
    if isinstance(obj, dict):
        if not obj:
            return depth
        return max(_json_depth(v, depth + 1) for v in obj.values())
    if isinstance(obj, list):
        if not obj:
            return depth
        return max(_json_depth(v, depth + 1) for v in obj)
    return depth


def strict_loads(raw: bytes):
    """严格解析请求体，不允许 BOM、重复键和过深嵌套，不合法就抛 ValueError"""
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("BOM not allowed")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("not valid UTF-8")
    obj = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    if not isinstance(obj, dict):
        raise ValueError("body must be a JSON object")
    if _json_depth(obj) > MAX_JSON_DEPTH:
        raise ValueError("JSON nesting too deep")
    return obj


class RobotAPI:
    def __init__(self, cfg: Config, manager: SessionManager):
        self.cfg = cfg
        self.manager = manager
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> tuple[bool, str]:
        if self._server is not None:
            return False, "已在运行"

        class Handler(RobotHandler):
            api = self

        try:
            self._server = ThreadingHTTPServer((self.cfg.robot_host, self.cfg.robot_port), Handler)
        except OSError as e:
            return False, f"端口不可用: {e}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return True, f"机器狗接口已开启 http://{self.cfg.robot_host}:{self.cfg.robot_port}"

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._server is not None


class RobotHandler(BaseHTTPRequestHandler):
    api: RobotAPI = None  # 由 RobotAPI.start 注入

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---- 基础 ----
    def _send(self, code: int, text: str):
        """把已经渲染好的 JSON 文本按 UTF-8 字节数发出去"""
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code: int):
        self._send(code, render.render_rejected(self._now_ms()))

    def _now_ms(self) -> int:
        import time
        return int(time.time() * 1000)

    def _close_no_response(self):
        """接口未开放 / 已结束：直接关闭连接，不返回 JSON"""
        self.close_connection = True

    # ---- 路由 ----
    def _exact_path(self) -> str:
        return self.path

    def _check_content_headers(self) -> bool:
        """检查 Content-Type 与 Content-Encoding，不合法时自己写出 415 并返回 False"""
        # Content-Type
        ctype = self.headers.get("Content-Type")
        if ctype is None:
            self._err(HTTP_UNSUPPORTED_MEDIA)
            return False
        parts = [p.strip() for p in ctype.split(";")]
        if parts[0].lower() != "application/json":
            self._err(HTTP_UNSUPPORTED_MEDIA)
            return False
        for param in parts[1:]:
            if not param:
                continue
            if "=" not in param:
                self._err(HTTP_UNSUPPORTED_MEDIA)
                return False
            k, v = param.split("=", 1)
            if k.strip().lower() != "charset" or v.strip().lower() != "utf-8":
                self._err(HTTP_UNSUPPORTED_MEDIA)
                return False
        # Content-Encoding
        enc = self.headers.get("Content-Encoding")
        if enc is not None and enc.strip().lower() != "identity":
            self._err(HTTP_UNSUPPORTED_MEDIA)
            return False
        return True

    def _read_body(self) -> bytes | None:
        """按 Content-Length 读请求体，长度不合法时自己写出响应并返回 None"""
        length_raw = self.headers.get("Content-Length")
        if length_raw is None:
            self._err(HTTP_BAD_REQUEST)
            return None
        try:
            length = int(length_raw)
        except ValueError:
            self._err(HTTP_BAD_REQUEST)
            return None
        if length < 0:
            self._err(HTTP_BAD_REQUEST)
            return None
        if length > BODY_MAX_BYTES:
            self._err(HTTP_TOO_LARGE)
            return None
        try:
            return self.rfile.read(length)
        except Exception:
            self._err(HTTP_BAD_REQUEST)
            return None

    def do_POST(self):
        path = self.path
        if path not in KNOWN_PATHS:
            self._err(HTTP_NOT_FOUND)
            return
        if not self._check_content_headers():
            return
        raw = self._read_body()
        if raw is None:
            return
        try:
            req = strict_loads(raw)
        except _DuplicateKeyError:
            self._err(HTTP_BAD_REQUEST)
            return
        except ValueError:
            self._err(HTTP_BAD_REQUEST)
            return

        result = self.api.manager.handle(path, req)
        if result is None:
            self._close_no_response()
            return
        status, body = result
        self._send(status, body)

    def do_GET(self):
        self._method_not_allowed_or_404()

    def do_HEAD(self):
        self._method_not_allowed_or_404()

    def do_PUT(self):
        self._method_not_allowed_or_404()

    def do_DELETE(self):
        self._method_not_allowed_or_404()

    def _method_not_allowed_or_404(self):
        if self.path in KNOWN_PATHS:
            self._err(HTTP_METHOD_NOT_ALLOWED)
        else:
            self._err(HTTP_NOT_FOUND)