"""``ddnovel`` 命令行入口 (占位骨架，完整子命令在 T12 实现)。"""

from __future__ import annotations

import typer
from rich.console import Console

from ddnovel import __version__

app = typer.Typer(
    name="ddnovel",
    help="DDNovelGenerater 长篇小说创作工程引擎 CLI",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.command()
def version() -> None:
    """显示引擎版本号。"""
    console.print(f"ddnovel [bold cyan]{__version__}[/]")


@app.callback()
def _root() -> None:
    """DDNovelGenerater 长篇小说创作工程引擎 CLI。"""


def main() -> None:
    app()


if __name__ == "__main__":
    main()
