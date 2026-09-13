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
    """JSON 请求体里出现重复键时抛出的内部异常"""
    pass


def _reject_duplicate_keys(pairs):
    """给 json.loads 当 object_pairs_hook，同一层出现重复键就抛错"""
    d = {}
    for k, v in pairs:
        if k in d:
            raise _DuplicateKeyError(k)
        d[k] = v
    return d


def _json_depth(obj, depth: int = 1) -> int:
    """递归算出 JSON 结构的嵌套层数，空字典和空列表算作当前深度"""
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
    """机器狗 HTTP 接口的服务端，管着监听端口的起停"""
    def __init__(self, cfg: Config, manager: SessionManager):
        """记下配置和会话管理器，服务器与线程留到 start 时再建"""
        self.cfg = cfg
        self.manager = manager
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> tuple[bool, str]:
        """在后台线程里监听机器狗端口，返回是否成功和一句状态说明"""
        if self._server is not None:
            return False, "已在运行"

        class Handler(RobotHandler):
            """把当前 RobotAPI 实例挂到类属性上，处理请求时直接取用"""
            api = self

        try:
            self._server = ThreadingHTTPServer((self.cfg.robot_host, self.cfg.robot_port), Handler)
        except OSError as e:
            return False, f"端口不可用: {e}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return True, f"机器狗接口已开启 http://{self.cfg.robot_host}:{self.cfg.robot_port}"

    def stop(self):
        """关掉服务器并等后台线程退出，没起过就跳过"""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    @property
    def running(self) -> bool:
        """接口是否在监听，看服务器实例有没有建起来"""
        return self._server is not None


class RobotHandler(BaseHTTPRequestHandler):
    """机器狗接口的请求处理器，校验 HTTP 请求后转成会话管理器的调用"""
    api: RobotAPI = None  # 由 RobotAPI.start 注入

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        """覆盖父类的访问日志，请求不打到控制台"""
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
        """按给定状态码回一条统一的拒绝响应"""
        self._send(code, render.render_rejected(self._now_ms()))

    def _now_ms(self) -> int:
        """取当前时间的毫秒时间戳，填进拒绝响应里"""
        import time
        return int(time.time() * 1000)

    def _close_no_response(self):
        """接口未开放 / 已结束：直接关闭连接，不返回 JSON"""
        self.close_connection = True

    # ---- 路由 ----
    def _exact_path(self) -> str:
        """原样返回请求路径，不做归一化也不做百分号解码"""
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
        """处理 POST，路径、请求头和请求体都通过后交给会话管理器"""
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
        """GET 不支持，已知路径回 405，其余路径回 404"""
        self._method_not_allowed_or_404()

    def do_HEAD(self):
        """HEAD 不支持，已知路径回 405，其余路径回 404"""
        self._method_not_allowed_or_404()

    def do_PUT(self):
        """PUT 不支持，已知路径回 405，其余路径回 404"""
        self._method_not_allowed_or_404()

    def do_DELETE(self):
        """DELETE 不支持，已知路径回 405，其余路径回 404"""
        self._method_not_allowed_or_404()

    def _method_not_allowed_or_404(self):
        """已知路径说明方法用错了回 405，其它路径回 404"""
        if self.path in KNOWN_PATHS:
            self._err(HTTP_METHOD_NOT_ALLOWED)
        else:
            self._err(HTTP_NOT_FOUND)