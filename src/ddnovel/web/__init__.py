"""DDNovelGenerater 状态、悬线与关系网交互式 Web UI 看板子包。"""

from __future__ import annotations

from .server import (
    DashboardHTTPServer,
    create_web_server,
    render_standalone_html,
    serve_web_ui,
)
from .snapshot import build_dashboard_snapshot

__all__ = [
    "DashboardHTTPServer",
    "build_dashboard_snapshot",
    "create_web_server",
    "render_standalone_html",
    "serve_web_ui",
]
