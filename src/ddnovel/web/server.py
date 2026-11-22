"""基于 Python 标准库的零依赖 Web UI 本地看板服务与离线 HTML 渲染器。"""

from __future__ import annotations

import json
import threading
import webbrowser
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .snapshot import build_dashboard_snapshot

_TEMPLATE_PATH = Path(__file__).resolve().parent / "static" / "dashboard.html"
_INITIAL_DATA_PLACEHOLDER = '<script id="ddnovel-initial-data" type="application/json">null</script>'


def render_standalone_html(snapshot: dict[str, Any], *, live_server: bool = False) -> str:
    """将书库快照安全注入单文件交互式 HTML 看板模板。"""
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    if _INITIAL_DATA_PLACEHOLDER not in template:
        raise RuntimeError(f"看板模板缺少初始数据占位块: {_TEMPLATE_PATH}")

    payload = {**snapshot, "_live_server": live_server}
    raw_json = json.dumps(payload, ensure_ascii=False)
    safe_json = (
        raw_json.replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    injected = f'<script id="ddnovel-initial-data" type="application/json">{safe_json}</script>'
    return template.replace(_INITIAL_DATA_PLACEHOLDER, injected, 1)


class DashboardHTTPServer(ThreadingHTTPServer):
    """绑定指定小说书库目录的多线程本地 HTTP 看板服务。"""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        *,
        book_dir: Path | str,
    ) -> None:
        self.book_dir: Path = Path(book_dir).resolve()
        super().__init__(server_address, request_handler_class)


class _DashboardRequestHandler(BaseHTTPRequestHandler):
    """处理 Web UI 页面、实时 JSON 快照接口与健康检查请求。"""

    server: DashboardHTTPServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _send_bytes(
        self,
        body: bytes,
        *,
        status: int = HTTPStatus.OK,
        content_type: str,
        no_store: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if no_store:
            self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(
        self,
        payload: dict[str, Any],
        *,
        status: int = HTTPStatus.OK,
        no_store: bool = True,
    ) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(
            raw,
            status=status,
            content_type="application/json; charset=utf-8",
            no_store=no_store,
        )

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in {"/", "/index.html"}:
            try:
                snap = build_dashboard_snapshot(self.server.book_dir)
                html = render_standalone_html(snap, live_server=True)
                self._send_bytes(
                    html.encode("utf-8"),
                    status=HTTPStatus.OK,
                    content_type="text/html; charset=utf-8",
                    no_store=True,
                )
            except (FileNotFoundError, ValueError, RuntimeError) as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            return

        if path == "/api/dashboard":
            try:
                snap = build_dashboard_snapshot(self.server.book_dir)
                self._send_json(snap, status=HTTPStatus.OK)
            except (FileNotFoundError, ValueError, RuntimeError) as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            return

        if path == "/api/health":
            self._send_json(
                {"ok": True, "book_dir": self.server.book_dir.as_posix()},
                status=HTTPStatus.OK,
            )
            return

        self._send_json({"ok": False, "error": "Not Found"}, status=HTTPStatus.NOT_FOUND)


def create_web_server(
    book_dir: Path | str,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> DashboardHTTPServer:
    """校验书库有效性并创建已绑定端口的 ``DashboardHTTPServer`` 实例。"""
    if isinstance(port, bool) or not isinstance(port, int) or not (0 <= port <= 65535):
        raise ValueError(f"port 必须为 0~65535 之间的整数，收到: {port!r}")
    build_dashboard_snapshot(book_dir)
    return DashboardHTTPServer((host, port), _DashboardRequestHandler, book_dir=book_dir)


def serve_web_ui(
    book_dir: Path | str,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    on_ready: Callable[[str], None] | None = None,
) -> None:
    """启动本地 Web UI 看板 HTTP 服务并在需要时自动打开默认浏览器。"""
    server = create_web_server(book_dir, host=host, port=port)
    actual_port = int(server.server_address[1])
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    url = f"http://{display_host}:{actual_port}/"

    if on_ready is not None:
        on_ready(url)

    timer: threading.Timer | None = None
    if open_browser:
        timer = threading.Timer(0.15, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if timer is not None:
            timer.cancel()
        server.server_close()


__all__ = [
    "DashboardHTTPServer",
    "create_web_server",
    "render_standalone_html",
    "serve_web_ui",
]
