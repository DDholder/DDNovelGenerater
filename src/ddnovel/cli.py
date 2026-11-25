"""``ddnovel`` 命令行入口 (T12: Typer + Rich 全指令集封装)。

提供覆盖小说创作全生命周期的 11 组 CLI 子命令，作为 ``ddnovel.core`` 与
``ddnovel.templates`` 的薄封装终端交互层。
"""

from __future__ import annotations

import json
import os
import re
import shlex
from collections.abc import Callable, Sequence
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, TypeVar

import typer
import typer.rich_utils as _typer_rich_utils
import yaml
from pydantic import TypeAdapter, ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ddnovel import __version__
from ddnovel.core import (
    ForeshadowRadar,
    ImpactAnalyzer,
    LifecycleManager,
    PacketBuilder,
    ProgressiveParser,
    ReaderExporter,
    SqliteIndexer,
    build_reader_patch,
    estimate_tokens,
    lint_model_setting_doc,
    pack_reader_book,
    split_frontmatter,
    validate_reader_target,
)
from ddnovel.core.parser import CATEGORY_SYNONYMS
from ddnovel.core.paths import (
    find_existing_blueprint_file,
    find_existing_setting_file,
    resolve_active_characters_file,
    resolve_active_factions_file,
    resolve_arc_file,
    resolve_archive_characters_file,
    resolve_archive_factions_file,
    resolve_blueprint_file,
    resolve_blueprints_dir,
    resolve_config_path,
    resolve_draft_file,
    resolve_facts_file,
    resolve_packet_file,
    resolve_reader_pack_dir,
    resolve_relations_file,
    resolve_setting_file,
    resolve_settings_root,
    resolve_threads_file,
    resolve_volume_file,
    resolve_world_state_file,
)
from ddnovel.core.schemas import (
    ID_PREFIX_CATEGORY,
    VOLUME_ID_PATTERN,
    ArcDoc,
    Beat,
    BlueprintCast,
    BlueprintDoc,
    ChapterFactRecord,
    ChapterWindow,
    CharacterState,
    CharacterStateFile,
    EntityId,
    EntityStatus,
    FactionState,
    FactionStateFile,
    FtsScope,
    ImpactScope,
    IssueLevel,
    NovelConfig,
    RadarReason,
    RelationsFile,
    SettingCategory,
    SettingDoc,
    TemporalRelation,
    Thread,
    ThreadAction,
    ThreadRef,
    ThreadsFile,
    ThreadStatus,
    TopicRef,
    VolumeDoc,
    WorldState,
)
from ddnovel.templates import ProjectInitializer
from ddnovel.web import (
    build_dashboard_snapshot,
    render_standalone_html,
    serve_web_ui,
)

# 汉化 Typer/Rich 帮助与错误面板标题
_typer_rich_utils.ARGUMENTS_PANEL_TITLE = "参数"
_typer_rich_utils.OPTIONS_PANEL_TITLE = "选项"
_typer_rich_utils.COMMANDS_PANEL_TITLE = "子命令"
_typer_rich_utils.ERRORS_PANEL_TITLE = "错误"
_typer_rich_utils.ABORTED_TEXT = "已中止。"
_typer_rich_utils.DEFAULT_STRING = "[默认: {}]"
_typer_rich_utils.DEPRECATED_STRING = "（已弃用）"
_typer_rich_utils.ENVVAR_STRING = "[环境变量: {}]"
_typer_rich_utils.REQUIRED_LONG_STRING = "[必填]"

_F = TypeVar("_F", bound=Callable[..., Any])

console = Console()

_CONTEXT_SETTINGS: dict[str, Any] = {"help_option_names": ["-h", "--help"]}

app = typer.Typer(
    name="ddnovel",
    help="DDNovelGenerater 长篇小说创作工程引擎 CLI（支持 -h / --help 查看帮助）",
    epilog=(
        "常用子命令速查:\n"
        "  ddnovel init <目录> -t <书名> -g cultivation_rpg -H antigravity 初始化新书库\n"
        "  ddnovel host install antigravity                               安装本机全局 Antigravity 插件\n"
        "  ddnovel setting upsert char_linfan -n 林凡 -c character        新增/更新设定\n"
        "  ddnovel structure blueprint 1 -t 第一章 -p char_linfan         编写单章蓝图\n"
        "  ddnovel radar -c 10                                            扫描伏笔预警\n"
        "  ddnovel packet build 10 -b 6000                                编译单章写作包\n"
        "  ddnovel draft save 10 -f ./ch10.md && ddnovel audit 10         存草稿并一致性体检\n"
        "  ddnovel chapter promote 10 -v vol_01                           审校定稿入库\n"
        "  ddnovel state update -c 10 -s '本章摘要' -F '事实1' -F '事实2'   结算章节状态与事实\n"
        "  ddnovel reader plan 1 -d .                                     单章伴读设计与资源需求提案\n"
        "  ddnovel reader export -d .                                     生成适配 DDNovelReader 的作品包 (.ddbook)\n"
        "  ddnovel web -d . -p 8765                                       启动可视化状态与关系看板 Web UI\n\n"
        "提示: 任意子命令均可加 -h 或 --help 查看详细参数与示例，例如: ddnovel state update -h"
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings=_CONTEXT_SETTINGS,
)

setting_app = typer.Typer(
    name="setting",
    help="设定维护、五维防吃书影响面扫描与废案剪枝 (01_设定集/)",
    epilog="示例: ddnovel setting upsert -h  |  ddnovel setting impact -h  |  ddnovel setting prune -h",
    no_args_is_help=True,
    context_settings=_CONTEXT_SETTINGS,
)
structure_app = typer.Typer(
    name="structure",
    help="分卷、单元与单章分场节拍蓝图维护 (02_结构/)",
    epilog=(
        "示例: ddnovel structure volume -h  |  "
        "ddnovel structure arc -h  |  "
        "ddnovel structure blueprint -h"
    ),
    no_args_is_help=True,
    context_settings=_CONTEXT_SETTINGS,
)
packet_app = typer.Typer(
    name="packet",
    help="单章 U 型注意力写作包一步直出编译与查阅 (04_创作/写作包/)",
    epilog="示例: ddnovel packet build 10 -b 6000  |  ddnovel packet show 10",
    no_args_is_help=True,
    context_settings=_CONTEXT_SETTINGS,
)
draft_app = typer.Typer(
    name="draft",
    help="单章初稿保存与查阅 (04_创作/草稿/)",
    epilog="示例: ddnovel draft save 10 -f ./ch10.md  |  ddnovel draft show 10",
    no_args_is_help=True,
    context_settings=_CONTEXT_SETTINGS,
)
chapter_app = typer.Typer(
    name="chapter",
    help="章节审校定稿入库与过程产物自动清理 (04_创作/定稿/)",
    epilog="示例: ddnovel chapter promote 10 -v vol_01  |  ddnovel chapter promote 10 -n",
    no_args_is_help=True,
    context_settings=_CONTEXT_SETTINGS,
)
state_app = typer.Typer(
    name="state",
    help="动态状态截面、历史原子事实结算、实体状态检视与冷归档 (03_状态/)",
    epilog="示例: ddnovel state update -h  |  ddnovel state inspect char_linfan  |  ddnovel state gc -n",
    no_args_is_help=True,
    context_settings=_CONTEXT_SETTINGS,
)
index_app = typer.Typer(
    name="index",
    help="SQLite 派生索引增量热同步与零污染全量重建 (.ddnovel/index.sqlite)",
    epilog="示例: ddnovel index sync -d .  |  ddnovel index rebuild -d .",
    no_args_is_help=True,
    context_settings=_CONTEXT_SETTINGS,
)
thread_app = typer.Typer(
    name="thread",
    help="伏笔悬线台账查阅与主动弃坑硬删除 (03_状态/伏笔台账.yaml)",
    epilog="示例: ddnovel thread list -s active  |  ddnovel thread drop <thread_id> -n",
    no_args_is_help=True,
    context_settings=_CONTEXT_SETTINGS,
)
history_app = typer.Typer(
    name="history",
    help="远期完结卷原子事实蒸馏浓缩与历史台账维护 (03_状态/历史/)",
    epilog="示例: ddnovel history compact vol_01 -n  |  ddnovel history compact vol_01 -f milestones.jsonl",
    no_args_is_help=True,
    context_settings=_CONTEXT_SETTINGS,
)
host_app = typer.Typer(
    name="host",
    help="AI 宿主配置同步与全局插件安装 (opencode / dsh / claude / antigravity / codex | chatgpt)",
    epilog=(
        "示例: ddnovel host install antigravity  |  "
        "ddnovel host install codex  |  "
        "ddnovel host sync -d . -H codex"
    ),
    no_args_is_help=True,
    context_settings=_CONTEXT_SETTINGS,
)
reader_app = typer.Typer(
    name="reader",
    help="DDNovelReader (.ddbook / .ddpatch) 伴读作品包导出、单章资源规划、校验与打包",
    epilog=(
        "示例: ddnovel reader plan 1 -d .  |  "
        "ddnovel reader export -d .  |  "
        "ddnovel reader export -d . -c 1  |  "
        "ddnovel reader validate -d ."
    ),
    no_args_is_help=True,
    context_settings=_CONTEXT_SETTINGS,
)

app.add_typer(setting_app, name="setting")
app.add_typer(structure_app, name="structure")
app.add_typer(packet_app, name="packet")
app.add_typer(draft_app, name="draft")
app.add_typer(chapter_app, name="chapter")
app.add_typer(state_app, name="state")
app.add_typer(index_app, name="index")
app.add_typer(thread_app, name="thread")
app.add_typer(history_app, name="history")
app.add_typer(host_app, name="host")
app.add_typer(reader_app, name="reader")

_ENTITY_ID_ADAPTER: TypeAdapter[str] = TypeAdapter(EntityId)
_VOLUME_DIR_RE: re.Pattern[str] = re.compile(VOLUME_ID_PATTERN)

_CATEGORY_DIR_MAP: dict[SettingCategory, str] = {
    SettingCategory.WORLD: "01_settings/world",
    SettingCategory.RULE: "01_settings/world",
    SettingCategory.GEOGRAPHY: "01_settings/geography",
    SettingCategory.FACTION: "01_settings/factions",
    SettingCategory.CHARACTER: "01_settings/characters",
    SettingCategory.ITEM: "01_settings/items",
    SettingCategory.OTHER: "01_settings/other",
}

BookDirOption = Annotated[
    Path | None,
    typer.Option(
        "--book-dir",
        "-d",
        help="小说书库根目录路径（默认读取 DDNOVEL_BOOK_DIR 环境变量或当前工作目录 '.'）",
    ),
]


# ---------------------------------------------------------------------------
# 通用辅助工具与错误拦截装饰器
# ---------------------------------------------------------------------------


def _store_ctx_book_dir(ctx: typer.Context, book_dir: Path | None) -> None:
    ctx.ensure_object(dict)
    if book_dir is not None:
        ctx.obj["book_dir"] = Path(book_dir).resolve()


def _resolve_book_dir(
    book_dir: Path | None = None,
    ctx: typer.Context | None = None,
) -> Path:
    """按 显式参数 -> 父级上下文参数 -> DDNOVEL_BOOK_DIR 环境变量 -> 当前目录 顺序解析书库路径。"""
    if book_dir is not None:
        return Path(book_dir).resolve()

    cur_ctx = ctx
    while cur_ctx is not None:
        if isinstance(cur_ctx.obj, dict) and cur_ctx.obj.get("book_dir") is not None:
            return Path(cur_ctx.obj["book_dir"]).resolve()
        cur_ctx = cur_ctx.parent

    env_val = os.environ.get("DDNOVEL_BOOK_DIR", "").strip()
    if env_val:
        return Path(env_val).resolve()

    return Path.cwd().resolve()


def _load_book_config(book_dir: Path) -> NovelConfig:
    cfg_path = resolve_config_path(book_dir)
    if cfg_path.is_file():
        return NovelConfig.load(book_dir)
    return NovelConfig(title=book_dir.name or "未命名小说")


def _dump_yaml(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _render_frontmatter_md(frontmatter: dict[str, Any], body: str) -> str:
    fm_yaml = _dump_yaml(frontmatter).rstrip("\n")
    clean_body = body.strip("\n")
    if clean_body:
        return f"---\n{fm_yaml}\n---\n\n{clean_body}\n"
    return f"---\n{fm_yaml}\n---\n"


def _split_multi_opts(items: Sequence[str] | None) -> list[str]:
    """展开可重复选项及逗号分隔项，去重保序。"""
    if not items:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in items:
        for part in re.split(r"[,，]", str(raw)):
            cleaned = part.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                result.append(cleaned)
    return result


def _parse_mcp_command_opt(items: Sequence[str] | None) -> list[str] | None:
    """解析 --mcp-command 选项：单字符串非现有文件路径时按 Shell 规则拆分，多参数保留序列。"""
    if not items:
        return None
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    if not cleaned:
        return None
    if len(cleaned) == 1:
        single = cleaned[0]
        if Path(single).expanduser().exists():
            return [single]
        if any(ch.isspace() for ch in single):
            tokens = [
                tok.strip("\"'")
                for tok in shlex.split(single, posix=(os.name != "nt"))
                if tok.strip("\"'")
            ]
            return tokens or [single]
    return cleaned


def _parse_chapter_range(
    range_str: str | None,
    start_ch: int | None = None,
    end_ch: int | None = None,
) -> ChapterWindow | None:
    if start_ch is not None or end_ch is not None:
        if start_ch is None or end_ch is None:
            raise ValueError("指定章节范围时必须同时提供 --start-ch 与 --end-ch，或使用 --range '起-止'")
        return TypeAdapter(ChapterWindow).validate_python((start_ch, end_ch))
    if not range_str or not range_str.strip():
        return None
    cleaned = range_str.strip().strip("[]()")
    parts = [p.strip() for p in re.split(r"[-~:,，\s]+", cleaned) if p.strip()]
    if len(parts) != 2:
        raise ValueError(f"无效的章节区间格式 '{range_str}'，请使用如 '1-30' 或 '1,30'")
    return TypeAdapter(ChapterWindow).validate_python((int(parts[0]), int(parts[1])))


def _cli_guard(func: _F) -> _F:
    """捕获领域异常并输出清晰中文错误提示，返回非零退出码 1。"""

    @wraps(func)
    def _wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except typer.Exit:
            raise
        except ValidationError as exc:
            msgs = "; ".join(err.get("msg", str(err)) for err in exc.errors())
            console.print(f"[bold red]参数或数据校验错误：[/]{msgs}")
            raise typer.Exit(code=1) from None
        except (
            ValueError,
            TypeError,
            KeyError,
            FileNotFoundError,
            FileExistsError,
            yaml.YAMLError,
            json.JSONDecodeError,
        ) as exc:
            err_text = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
            console.print(f"[bold red]执行失败：[/]{err_text}")
            raise typer.Exit(code=1) from None

    return _wrapper  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Root Callback & 子命令组 Callback
# ---------------------------------------------------------------------------


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"ddnovel [bold cyan]{__version__}[/]")
        raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    book_dir: BookDirOption = None,
    version_flag: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="显示引擎版本号并退出",
        ),
    ] = False,
) -> None:
    """DDNovelGenerater 长篇小说创作工程引擎 CLI。"""
    _store_ctx_book_dir(ctx, book_dir)


@setting_app.callback()
def _setting_cb(ctx: typer.Context, book_dir: BookDirOption = None) -> None:
    _store_ctx_book_dir(ctx, book_dir)


@structure_app.callback()
def _structure_cb(ctx: typer.Context, book_dir: BookDirOption = None) -> None:
    _store_ctx_book_dir(ctx, book_dir)


@packet_app.callback()
def _packet_cb(ctx: typer.Context, book_dir: BookDirOption = None) -> None:
    _store_ctx_book_dir(ctx, book_dir)


@draft_app.callback()
def _draft_cb(ctx: typer.Context, book_dir: BookDirOption = None) -> None:
    _store_ctx_book_dir(ctx, book_dir)


@chapter_app.callback()
def _chapter_cb(ctx: typer.Context, book_dir: BookDirOption = None) -> None:
    _store_ctx_book_dir(ctx, book_dir)


@state_app.callback()
def _state_cb(ctx: typer.Context, book_dir: BookDirOption = None) -> None:
    _store_ctx_book_dir(ctx, book_dir)


@index_app.callback()
def _index_cb(ctx: typer.Context, book_dir: BookDirOption = None) -> None:
    _store_ctx_book_dir(ctx, book_dir)


@thread_app.callback()
def _thread_cb(ctx: typer.Context, book_dir: BookDirOption = None) -> None:
    _store_ctx_book_dir(ctx, book_dir)


@history_app.callback()
def _history_cb(ctx: typer.Context, book_dir: BookDirOption = None) -> None:
    _store_ctx_book_dir(ctx, book_dir)


@host_app.callback()
def _host_cb(ctx: typer.Context, book_dir: BookDirOption = None) -> None:
    _store_ctx_book_dir(ctx, book_dir)


@app.command()
def version() -> None:
    """显示引擎版本号。"""
    console.print(f"ddnovel [bold cyan]{__version__}[/]")


# ---------------------------------------------------------------------------
# Group 1: 工程管理 (init)
# ---------------------------------------------------------------------------


@app.command(
    "init",
    epilog=(
        "示例:\n"
        "  ddnovel init ./my_book -t 青云问道 -a 作者 -g cultivation_rpg -H antigravity\n"
        "  ddnovel init ./blank_book -S -w 4 -f"
    ),
)
@_cli_guard
def init_cmd(
    ctx: typer.Context,
    target_dir: Annotated[
        Path | None,
        typer.Argument(help="目标小说书库目录路径（默认使用 --book-dir 或当前目录 '.'）"),
    ] = None,
    title: Annotated[
        str | None,
        typer.Option("--title", "-t", help="小说书名（默认取目录名）"),
    ] = None,
    author: Annotated[
        str,
        typer.Option("--author", "-a", help="作者署名"),
    ] = "",
    genre: Annotated[
        str,
        typer.Option(
            "--genre",
            "-g",
            help="题材扩展配置 (general | cultivation_rpg | mystery_intrigue)",
        ),
    ] = "general",
    host: Annotated[
        list[str] | None,
        typer.Option(
            "--host",
            "-H",
            help="生成指定 AI 宿主配置与 Skills 目录 (opencode | dsh | claude | antigravity | codex | chatgpt)，可多次指定",
        ),
    ] = None,
    mcp_command: Annotated[
        list[str] | None,
        typer.Option(
            "--mcp-command",
            "-m",
            help="自定义 MCP Server 启动命令，可传入完整命令串或多次指定参数",
        ),
    ] = None,
    no_samples: Annotated[
        bool,
        typer.Option("--no-samples", "-S", help="仅生成空容器骨架，不写入起始示例设定与首章蓝图"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="若目标目录已存在小说配置文件则强制覆盖初始化"),
    ] = False,
    chapter_id_width: Annotated[
        int,
        typer.Option("--chapter-id-width", "-w", help="章节文件编号零填充宽度"),
    ] = 4,
    book_dir: BookDirOption = None,
) -> None:
    """初始化四维小说书库骨架、题材预设与多宿主 (opencode / dsh / claude / antigravity) 配置。"""
    resolved_dir = (
        Path(target_dir).resolve()
        if target_dir is not None
        else _resolve_book_dir(book_dir, ctx)
    )
    hosts = _split_multi_opts(host)
    initializer = ProjectInitializer(mcp_command=_parse_mcp_command_opt(mcp_command))
    res = initializer.init_book(
        resolved_dir,
        title=title,
        author=author,
        genre_profile=genre,
        hosts=hosts,
        chapter_id_width=chapter_id_width,
        include_samples=not no_samples,
        force=force,
    )

    table = Table(title="新书四维工程初始化摘要", show_header=True, header_style="bold cyan")
    table.add_column("项目", style="bold")
    table.add_column("详情")
    table.add_row("书库目录", res.book_dir.as_posix())
    table.add_row("配置文件", res.config_path.as_posix())
    table.add_row("书名 / 作者", f"{res.config.title} / {res.config.author or '（未署名）'}")
    table.add_row("题材模板", res.config.genre_profile)
    table.add_row("已创建目录数", str(len(res.created_dirs)))
    table.add_row("已创建真源文件数", str(len(res.created_files)))
    table.add_row(
        "宿主配置与技能文件",
        f"{len(res.host_files)} 个 ({', '.join(h.value for h in res.config.hosts) or '未启用'})",
    )
    console.print(table)


# ---------------------------------------------------------------------------
# Group 1.5: AI 宿主配置同步与全局插件安装 (host)
# ---------------------------------------------------------------------------


def _run_host_install(
    *,
    host: str,
    plugin_dir: Path | None,
    mcp_command: list[str] | None,
    reader_dir: Path | None,
    no_overwrite: bool,
) -> None:
    initializer = ProjectInitializer(
        mcp_command=_parse_mcp_command_opt(mcp_command),
        reader_dir=reader_dir,
    )
    res = initializer.install_global_plugin(
        host,
        plugin_dir=plugin_dir,
        overwrite=not no_overwrite,
    )

    table = Table(title="全局 AI 宿主插件安装摘要", show_header=True, header_style="bold cyan")
    table.add_column("项目", style="bold")
    table.add_column("详情")
    table.add_row("宿主类型", res.host.value)
    table.add_row("插件安装目录", res.plugin_dir.as_posix())
    table.add_row("MCP 启动命令", " ".join(res.mcp_command))
    if initializer.reader_mcp_command is not None:
        table.add_row("DDNovelReader MCP", " ".join(initializer.reader_mcp_command))
    table.add_row("写入/更新文件数", str(len(res.written_files)))
    if res.host.value == "codex":
        table.add_row("适用桌面客户端", "Codex Desktop / ChatGPT Desktop (Codex & Work 模式) / Codex CLI")
    else:
        table.add_row("Antigravity 管理页", f"customization://{res.plugin_dir.name}")
    console.print(table)


@host_app.command(
    "install",
    epilog=(
        "示例:\n"
        "  ddnovel host install antigravity\n"
        "  ddnovel host install antigravity --reader-dir D:/work/AI/proj/DDNovelReader\n"
        "  ddnovel host install codex\n"
        "  ddnovel host install chatgpt\n"
        "  ddnovel host install codex -m 'uv run --directory D:/repo ddnovel-mcp'"
    ),
)
@_cli_guard
def host_install_cmd(
    host: Annotated[
        str,
        typer.Argument(help="目标全局插件宿主类型（支持 antigravity / codex / chatgpt）"),
    ] = "antigravity",
    plugin_dir: Annotated[
        Path | None,
        typer.Option(
            "--plugin-dir",
            "-p",
            help="自定义安装目录（antigravity 默认 ~/.gemini/config/plugins/ddnovel，codex/chatgpt 默认 ~/.codex）",
        ),
    ] = None,
    mcp_command: Annotated[
        list[str] | None,
        typer.Option(
            "--mcp-command",
            "-m",
            help="自定义 MCP Server 启动命令，可传入完整命令串或多次指定参数",
        ),
    ] = None,
    reader_dir: Annotated[
        Path | None,
        typer.Option(
            "--reader-dir",
            "-R",
            help="可选 DDNovelReader 仓库根目录；提供时一并写入 ddnovel-reader MCP 服务配置",
        ),
    ] = None,
    no_overwrite: Annotated[
        bool,
        typer.Option("--no-overwrite", help="保留插件目录中已存在的自定义文件，不覆盖写入"),
    ] = False,
) -> None:
    """在本机全局配置目录安装/升级 AI 宿主插件（支持一引擎驱动多本小说书库）。"""
    _run_host_install(
        host=host,
        plugin_dir=plugin_dir,
        mcp_command=mcp_command,
        reader_dir=reader_dir,
        no_overwrite=no_overwrite,
    )


@host_app.command(
    "install-global",
    epilog="示例: ddnovel host install-global antigravity  |  ddnovel host install-global codex",
)
@_cli_guard
def host_install_global_cmd(
    host: Annotated[
        str,
        typer.Argument(help="目标全局插件宿主类型（支持 antigravity / codex / chatgpt）"),
    ] = "antigravity",
    plugin_dir: Annotated[
        Path | None,
        typer.Option(
            "--plugin-dir",
            "-p",
            help="自定义安装目录（antigravity 默认 ~/.gemini/config/plugins/ddnovel，codex/chatgpt 默认 ~/.codex）",
        ),
    ] = None,
    mcp_command: Annotated[
        list[str] | None,
        typer.Option(
            "--mcp-command",
            "-m",
            help="自定义 MCP Server 启动命令，可传入完整命令串或多次指定参数",
        ),
    ] = None,
    reader_dir: Annotated[
        Path | None,
        typer.Option(
            "--reader-dir",
            "-R",
            help="可选 DDNovelReader 仓库根目录；提供时一并写入 ddnovel-reader MCP 服务配置",
        ),
    ] = None,
    no_overwrite: Annotated[
        bool,
        typer.Option("--no-overwrite", help="保留插件目录中已存在的自定义文件，不覆盖写入"),
    ] = False,
) -> None:
    """安装/升级本机全局 AI 宿主插件（host install 的同义命令）。"""
    _run_host_install(
        host=host,
        plugin_dir=plugin_dir,
        mcp_command=mcp_command,
        reader_dir=reader_dir,
        no_overwrite=no_overwrite,
    )


@host_app.command(
    "sync",
    epilog=(
        "示例:\n"
        "  ddnovel host sync -d ./my_book -H antigravity\n"
        "  ddnovel host sync -d ./my_book --no-overwrite"
    ),
)
@_cli_guard
def host_sync_cmd(
    ctx: typer.Context,
    host: Annotated[
        list[str] | None,
        typer.Option(
            "--host",
            "-H",
            help="要同步的宿主类型 (opencode | dsh | claude | antigravity | codex | chatgpt)，省略则读取小说配置中的 hosts",
        ),
    ] = None,
    mcp_command: Annotated[
        list[str] | None,
        typer.Option(
            "--mcp-command",
            "-m",
            help="自定义 MCP Server 启动命令，可传入完整命令串或多次指定参数",
        ),
    ] = None,
    reader_dir: Annotated[
        Path | None,
        typer.Option(
            "--reader-dir",
            "-R",
            help="可选 DDNovelReader 仓库根目录；提供时一并写入 ddnovel-reader MCP 服务配置",
        ),
    ] = None,
    no_overwrite: Annotated[
        bool,
        typer.Option("--no-overwrite", help="保留书库中已存在的宿主配置与自定义 SKILL.md"),
    ] = False,
    book_dir: BookDirOption = None,
) -> None:
    """为现有小说书库增量生成或刷新工作区级 AI 宿主配置与技能目录。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    hosts = _split_multi_opts(host)
    if not hosts:
        cfg = _load_book_config(bdir)
        hosts = [h.value for h in cfg.hosts]
    if not hosts:
        raise ValueError(
            "未指定 --host，且当前书库配置中也未记录 hosts；请至少传入一个宿主如 -H antigravity"
        )

    initializer = ProjectInitializer(
        mcp_command=_parse_mcp_command_opt(mcp_command),
        reader_dir=reader_dir,
    )
    written = initializer.render_host_configs(
        bdir,
        hosts,
        overwrite=not no_overwrite,
        update_novel_yaml=True,
    )

    table = Table(title="工作区 AI 宿主配置同步摘要", show_header=True, header_style="bold cyan")
    table.add_column("项目", style="bold")
    table.add_column("详情")
    table.add_row("书库目录", bdir.as_posix())
    table.add_row("同步宿主", ", ".join(hosts))
    table.add_row("覆盖模式", "否 (--no-overwrite)" if no_overwrite else "是")
    table.add_row("写入/更新文件数", str(len(written)))
    console.print(table)


# ---------------------------------------------------------------------------
# Group 2 & 3: 设定维护与设定影响面 (setting)
# ---------------------------------------------------------------------------


def _find_existing_setting_path(book_dir: Path, entity_id: str) -> Path | None:
    direct = find_existing_setting_file(book_dir, entity_id)
    if direct is not None:
        return direct
    settings_root = resolve_settings_root(book_dir)
    if not settings_root.is_dir():
        return None
    parser = ProgressiveParser(book_dir=book_dir)
    for md_path in sorted(settings_root.rglob("*.md")):
        if not md_path.is_file():
            continue
        doc = parser.parse_setting_md(md_path, book_dir=book_dir)
        if doc.id == entity_id:
            return md_path
    return None


def _infer_category_from_id(entity_id: str) -> SettingCategory:
    for prefix, cat in ID_PREFIX_CATEGORY.items():
        if entity_id.startswith(prefix):
            return cat
    return SettingCategory.CHARACTER


def _parse_topic_entries(raw_topics: Sequence[str] | None) -> dict[str, str]:
    topics: dict[str, str] = {}
    if not raw_topics:
        return topics
    for item in raw_topics:
        text = str(item).strip()
        if not text:
            continue
        sep_match = re.search(r"[:：=]", text)
        if not sep_match:
            raise ValueError(
                f"专题参数格式错误 '{text}'，请使用 '专题名:专题内容' 或 '专题名=专题内容' 格式"
            )
        idx = sep_match.start()
        t_name = text[:idx].strip()
        t_body = text[idx + 1 :].strip()
        if not t_name or not t_body:
            raise ValueError(f"专题名称与专题内容均不能为空: '{text}'")
        topics[t_name] = t_body
    return topics


def _compose_setting_body(
    core_text: str,
    topics: dict[str, str],
    secret_text: str | None,
) -> str:
    sections: list[str] = [f"## [核心]\n{core_text.strip()}"]
    for t_name, t_content in topics.items():
        sections.append(f"## [专题: {t_name.strip()}]\n{t_content.strip()}")
    if secret_text is not None and secret_text.strip():
        sections.append(f"## [机密]\n{secret_text.strip()}")
    return "\n\n".join(sections)


@setting_app.command(
    "upsert",
    epilog=(
        "示例:\n"
        "  ddnovel setting upsert char_linfan -n 林凡 -c character -a 凡哥 -t 主角 -C '青云门外门弟子'\n"
        "  ddnovel setting upsert char_linfan -T '功法:青云诀第三重' -S '上古剑尊转世'\n"
        "  ddnovel setting upsert --id item_gujing -f ./item_gujing.md"
    ),
)
@_cli_guard
def setting_upsert_cmd(
    ctx: typer.Context,
    entity_id: Annotated[
        str | None,
        typer.Argument(help="设定实体唯一 ID（如 char_linfan / loc_zhuixinggu）"),
    ] = None,
    id_opt: Annotated[
        str | None,
        typer.Option("--id", help="设定实体唯一 ID（与位置参数二选一）"),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", "-n", help="设定标准名称（如 林凡）"),
    ] = None,
    category: Annotated[
        str | None,
        typer.Option(
            "--category",
            "-c",
            help="设定分类 (world | rule | geography | faction | character | item | other)",
        ),
    ] = None,
    status: Annotated[
        str | None,
        typer.Option("--status", "-s", help="实体状态 (active | deceased | retired)"),
    ] = None,
    alias: Annotated[
        list[str] | None,
        typer.Option("--alias", "--aliases", "-a", help="实体外号/简称（可多次指定或逗号分隔）"),
    ] = None,
    tag: Annotated[
        list[str] | None,
        typer.Option("--tag", "--tags", "-t", help="实体标签（可多次指定或逗号分隔）"),
    ] = None,
    core: Annotated[
        str | None,
        typer.Option("--core", "-C", help="## [核心] 切片正文（默认入包）"),
    ] = None,
    topic: Annotated[
        list[str] | None,
        typer.Option("--topic", "-T", help="按需专题切片，格式 '专题名:内容'（可多次指定）"),
    ] = None,
    secret: Annotated[
        str | None,
        typer.Option("--secret", "-S", help="## [机密] 切片正文（默认物理隔离）"),
    ] = None,
    from_file: Annotated[
        Path | None,
        typer.Option("--from-file", "-f", help="从指定 Markdown 文件读取设定内容"),
    ] = None,
    book_dir: BookDirOption = None,
) -> None:
    """新建或更新设定卡片 (01_设定集/<分类>/<id>-<对象名>.md)，并自动增量同步索引。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    parser = ProgressiveParser(book_dir=bdir)

    file_doc: SettingDoc | None = None
    if from_file is not None:
        src_path = Path(from_file).resolve()
        if not src_path.is_file():
            raise FileNotFoundError(f"未找到设定输入文件: {src_path}")
        file_doc = parser.parse_setting_md(src_path, book_dir=bdir)

    raw_id = entity_id or id_opt or (file_doc.id if file_doc is not None else None)
    if not raw_id or not str(raw_id).strip():
        raise ValueError("必须提供设定实体 ID（例如: ddnovel setting upsert char_linfan --name 林凡）")
    valid_id = _ENTITY_ID_ADAPTER.validate_python(str(raw_id).strip())

    existing_path = _find_existing_setting_path(bdir, valid_id)
    existing_doc = (
        parser.parse_setting_md(existing_path, book_dir=bdir)
        if existing_path is not None
        else None
    )

    base_doc = file_doc or existing_doc
    resolved_cat = (
        SettingCategory(category.strip().lower())
        if category is not None
        else (base_doc.category if base_doc is not None else _infer_category_from_id(valid_id))
    )
    resolved_status = (
        EntityStatus(status.strip().lower())
        if status is not None
        else (base_doc.status if base_doc is not None else EntityStatus.ACTIVE)
    )
    resolved_name = (
        name.strip()
        if name is not None and name.strip()
        else (base_doc.name if base_doc is not None else valid_id)
    )
    resolved_aliases = (
        _split_multi_opts(alias)
        if alias is not None
        else (list(base_doc.aliases) if base_doc is not None else [])
    )
    resolved_tags = (
        _split_multi_opts(tag)
        if tag is not None
        else (list(base_doc.tags) if base_doc is not None else [])
    )
    resolved_core = (
        core
        if core is not None
        else (base_doc.core_text if base_doc is not None else f"{resolved_name}的核心设定描述。")
    )
    merged_topics: dict[str, str] = dict(base_doc.topics) if base_doc is not None else {}
    if topic is not None:
        merged_topics.update(_parse_topic_entries(topic))
    resolved_secret = (
        secret
        if secret is not None
        else (base_doc.secret_text if base_doc is not None else None)
    )

    target_path = resolve_setting_file(
        bdir,
        valid_id,
        resolved_cat,
        name=resolved_name,
        for_write=True,
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    rel_file_path = target_path.relative_to(bdir).as_posix()

    validated_doc = SettingDoc(
        id=valid_id,
        name=resolved_name,
        category=resolved_cat,
        aliases=resolved_aliases,
        status=resolved_status,
        tags=resolved_tags,
        file_path=rel_file_path,
        core_text=resolved_core,
        topics=merged_topics,
        secret_text=resolved_secret,
    )

    fm_dict = {
        "id": validated_doc.id,
        "name": validated_doc.name,
        "aliases": validated_doc.aliases,
        "category": validated_doc.category.value,
        "status": validated_doc.status.value,
        "tags": validated_doc.tags,
    }
    body_md = _compose_setting_body(
        validated_doc.core_text,
        validated_doc.topics,
        validated_doc.secret_text,
    )
    target_path.write_text(_render_frontmatter_md(fm_dict, body_md), encoding="utf-8")
    if existing_path is not None and existing_path.is_file() and not existing_path.samefile(target_path):
        existing_path.unlink(missing_ok=True)

    with SqliteIndexer(bdir) as indexer:
        sync_rep = indexer.sync()

    abstraction_warnings = lint_model_setting_doc(validated_doc)

    console.print(
        Panel(
            f"[bold green]已保存设定卡：[/]{rel_file_path}\n"
            f"ID: [cyan]{validated_doc.id}[/] | 名称: [bold]{validated_doc.name}[/] | "
            f"分类: {validated_doc.category.value} | 状态: {validated_doc.status.value}\n"
            f"别名: {', '.join(validated_doc.aliases) or '无'} | "
            f"专题数: {len(validated_doc.topics)} | "
            f"含机密: {'是' if validated_doc.secret_text else '否'} | "
            f"索引同步: +{len(sync_rep.added)} ~{len(sync_rep.updated)}",
            title="设定维护 (setting upsert)",
            border_style="green",
        )
    )
    for w in abstraction_warnings:
        console.print(f"[yellow]⚠️ 抽象层检查告警: {w}[/]")


@setting_app.command(
    "list",
    epilog="示例:\n  ddnovel setting list -c character\n  ddnovel setting list --no-archived",
)
@_cli_guard
def setting_list_cmd(
    ctx: typer.Context,
    category: Annotated[
        str | None,
        typer.Option(
            "--category",
            "-c",
            help="按设定分类过滤 (world | rule | geography | faction | character | item | other)",
        ),
    ] = None,
    include_archived: Annotated[
        bool,
        typer.Option(
            "--include-archived/--no-archived",
            "-A",
            help="是否包含冷库归档/已故/退隐设定",
        ),
    ] = True,
    book_dir: BookDirOption = None,
) -> None:
    """以终端表格列出当前书库中的设定卡片。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    cat_enum = SettingCategory(category.strip().lower()) if category is not None else None

    with SqliteIndexer(bdir) as indexer:
        indexer.sync()
        docs = indexer.list_entities(category=cat_enum, include_archived=include_archived)

    table = Table(
        title=f"设定卡片列表（共 {len(docs)} 项）",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("名称", style="bold")
    table.add_column("分类")
    table.add_column("状态")
    table.add_column("外号/别名")
    table.add_column("专题", justify="right")
    table.add_column("机密", justify="center")
    table.add_column("文件路径", style="dim")

    for doc in docs:
        table.add_row(
            doc.id,
            doc.name,
            doc.category.value,
            doc.status.value,
            "、".join(doc.aliases) if doc.aliases else "-",
            str(len(doc.topics)),
            "有" if (doc.secret_text and doc.secret_text.strip()) else "-",
            doc.file_path,
        )
    console.print(table)


@setting_app.command(
    "show",
    epilog="示例:\n  ddnovel setting show char_linfan\n  ddnovel setting show 林凡 --hide-secret",
)
@_cli_guard
def setting_show_cmd(
    ctx: typer.Context,
    entity_id: Annotated[
        str,
        typer.Argument(help="要查阅的设定实体 ID 或名称"),
    ],
    show_secret: Annotated[
        bool,
        typer.Option(
            "--show-secret/--hide-secret",
            "-S",
            help="是否在终端展开显示 ## [机密] 切片内容",
        ),
    ] = True,
    book_dir: BookDirOption = None,
) -> None:
    """查看单个设定卡片的完整元信息与 [核心]/[专题]/[机密] 渐进切片。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    target = entity_id.strip()

    with SqliteIndexer(bdir) as indexer:
        indexer.sync()
        doc = indexer.get_entity(target)
        if doc is None:
            row = indexer.conn.execute(
                """
                SELECT entity_id FROM entity_aliases
                WHERE alias = ?
                ORDER BY is_archived ASC, entity_id ASC
                LIMIT 1
                """,
                (target,),
            ).fetchone()
            if row is not None:
                doc = indexer.get_entity(str(row[0]))

    if doc is None:
        raise KeyError(f"未找到设定卡片 '{target}'")

    lines: list[str] = [
        f"[bold]ID:[/] [cyan]{doc.id}[/]    [bold]名称:[/] {doc.name}    "
        f"[bold]分类:[/] {doc.category.value}    [bold]状态:[/] {doc.status.value}",
        f"[bold]别名:[/] {'、'.join(doc.aliases) if doc.aliases else '无'}    "
        f"[bold]标签:[/] {'、'.join(doc.tags) if doc.tags else '无'}",
        f"[bold]路径:[/] {doc.file_path}",
        "",
        f"[bold cyan]## [核心] ({estimate_tokens(doc.core_text)} Tokens)[/]",
        doc.core_text or "（空）",
    ]
    for t_name, t_body in doc.topics.items():
        lines.extend(
            [
                "",
                f"[bold yellow]## [专题: {t_name}] ({estimate_tokens(t_body)} Tokens)[/]",
                t_body,
            ]
        )
    if doc.secret_text:
        lines.append("")
        if show_secret:
            lines.append(
                f"[bold magenta]## [机密] ({estimate_tokens(doc.secret_text)} Tokens)[/]"
            )
            lines.append(doc.secret_text)
        else:
            lines.append("[bold magenta]## [机密][/] （已隐藏，传入 --show-secret 查看）")

    console.print(
        Panel(
            "\n".join(lines),
            title=f"设定详情 · {doc.name} ({doc.id})",
            border_style="cyan",
        )
    )


@setting_app.command(
    "impact",
    epilog="示例:\n  ddnovel setting impact char_linfan -c 50\n  ddnovel setting impact 青云诀 -A",
)
@_cli_guard
def setting_impact_cmd(
    ctx: typer.Context,
    target: Annotated[
        str,
        typer.Argument(help="拟修改或删除的设定实体 ID、角色名/外号或自由检索关键词"),
    ],
    after_chapter: Annotated[
        int | None,
        typer.Option("--after-chapter", "-c", help="后续未定稿蓝图的分界章号"),
    ] = None,
    include_archived: Annotated[
        bool,
        typer.Option("--include-archived", "-A", help="是否同时扫描 archive/ 冷库状态文件"),
    ] = False,
    book_dir: BookDirOption = None,
) -> None:
    """中途改/删设定五维防吃书影响面扫描（正文/历史事实/活跃状态/未完结伏笔/后续蓝图）。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    with ImpactAnalyzer(bdir) as analyzer:
        report = analyzer.scan_impact(
            target,
            after_chapter=after_chapter,
            include_archived_state=include_archived,
        )

    safe_badge = (
        "[bold green]是（无历史引用冲突，可直接修改/删除）[/]"
        if report.is_safe_to_change
        else f"[bold yellow]否（共发现 {len(report.hits)} 处关联引用，请核对前文后再修改）[/]"
    )
    scope_counts = {s: 0 for s in ImpactScope}
    for hit in report.hits:
        scope_counts[hit.scope] += 1

    summary_lines = [
        f"[bold]扫描目标:[/] {report.target}    "
        f"[bold]解析实体 ID:[/] [cyan]{report.entity_id or '（自由关键词/伏笔）'}[/]",
        f"[bold]展开别名词表:[/] {', '.join(report.expanded_terms)}",
        f"[bold]可安全直接改动:[/] {safe_badge}",
        (
            "[bold]五维命中分布:[/] "
            f"manuscript={scope_counts[ImpactScope.MANUSCRIPT]} | "
            f"fact={scope_counts[ImpactScope.FACT]} | "
            f"state={scope_counts[ImpactScope.STATE]} | "
            f"thread={scope_counts[ImpactScope.THREAD]} | "
            f"blueprint={scope_counts[ImpactScope.BLUEPRINT]}"
        ),
    ]
    console.print(
        Panel(
            "\n".join(summary_lines),
            title="防吃书五维影响面扫描报告 (setting impact)",
            border_style="green" if report.is_safe_to_change else "yellow",
        )
    )

    if report.hits:
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("作用域 (Scope)", style="bold")
        table.add_column("章号", justify="right")
        table.add_column("命中词元", style="cyan")
        table.add_column("文件位置", style="dim")
        table.add_column("上下文摘录")
        for hit in report.hits:
            table.add_row(
                hit.scope.value,
                str(hit.chapter_num) if hit.chapter_num is not None else "-",
                hit.matched_term or "-",
                hit.location,
                hit.excerpt,
            )
        console.print(table)


@setting_app.command(
    "prune",
    epilog=(
        "示例:\n"
        "  ddnovel setting prune -c 80 -m 2 -a 30        # 仅 dry-run 预览候选废案\n"
        "  ddnovel setting prune -c 80 -y               # 确认物理删除零引用/龙套设定\n"
        "  ddnovel setting prune char_temp_01 -y -f     # 强制剪枝指定实体"
    ),
)
@_cli_guard
def setting_prune_cmd(
    ctx: typer.Context,
    target_ids: Annotated[
        list[str] | None,
        typer.Argument(help="可选指定待剪枝的实体 ID 列表（不传则自动扫描全部孤儿与低频龙套）"),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option("--apply", "-y", help="执行物理文件删除与索引级联清理（默认仅 dry-run 预览）"),
    ] = False,
    current_chapter: Annotated[
        int | None,
        typer.Option("--current-chapter", "-c", help="指定当前基准章号"),
    ] = None,
    max_appearances: Annotated[
        int,
        typer.Option("--max-appearances", "-m", help="一次性龙套最多历史出现章数阈值"),
    ] = 2,
    min_age: Annotated[
        int | None,
        typer.Option("--min-age", "-a", help="距离最后出场最小冷却章数阈值"),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="对显式传入的 target_ids 强制物理剪枝"),
    ] = False,
    book_dir: BookDirOption = None,
) -> None:
    """扫描零引用废案孤儿设定与远期一次性龙套卡片，按需执行物理删除瘦身。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    targets = _split_multi_opts(target_ids) if target_ids else None
    with LifecycleManager(bdir) as lm:
        rep = lm.prune_settings(
            target_ids=targets,
            apply=apply,
            current_ch=current_chapter,
            max_appearances=max_appearances,
            min_age_chapters=min_age,
            force=force,
        )

    mode_str = (
        "[bold red]物理删除已执行 (applied)[/]"
        if not rep.dry_run
        else "[bold cyan]预览模式 (dry-run)[/]"
    )
    table = Table(title=f"设定剪枝报告 ({mode_str})", show_header=True, header_style="bold cyan")
    table.add_column("类别", style="bold")
    table.add_column("数量", justify="right")
    table.add_column("明细")
    table.add_row(
        "零引用孤儿废案 (orphan_ids)",
        str(len(rep.orphan_ids)),
        ", ".join(rep.orphan_ids) or "-",
    )
    table.add_row(
        "远期低频龙套 (low_frequency_ids)",
        str(len(rep.low_frequency_ids)),
        ", ".join(rep.low_frequency_ids) or "-",
    )
    table.add_row(
        "已物理删除文件 (deleted_paths)",
        str(len(rep.deleted_paths)),
        ", ".join(rep.deleted_paths) or "-",
    )
    table.add_row(
        "已跳过保护实体 (skipped_ids)",
        str(len(rep.skipped_ids)),
        ", ".join(rep.skipped_ids) or "-",
    )
    console.print(table)


@setting_app.command(
    "lint",
    epilog=(
        "示例:\n"
        "  ddnovel setting lint\n"
        "  ddnovel setting lint -c character\n"
        "  ddnovel setting lint -A"
    ),
)
@_cli_guard
def setting_lint_cmd(
    ctx: typer.Context,
    category: Annotated[
        str | None,
        typer.Option(
            "--category",
            "-c",
            help="按分类过滤 (world/rule/geography/faction/character/item/other 或中文)",
        ),
    ] = None,
    include_archived: Annotated[
        bool,
        typer.Option(
            "--include-archived",
            "-A",
            help="是否同时检查已归档/退场设定卡（默认仅检查 active）",
        ),
    ] = False,
    book_dir: BookDirOption = None,
) -> None:
    """检查模型侧设定集（01_设定集/）是否存在具象场景、示范台词、动作指令清单或作者备忘泄露风险。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    settings_root = resolve_settings_root(bdir)

    cat_filter: SettingCategory | None = None
    if category is not None and category.strip():
        raw_cat = category.strip().lower()
        cat_filter = CATEGORY_SYNONYMS.get(raw_cat) or SettingCategory(raw_cat)

    parser = ProgressiveParser(book_dir=bdir)
    scanned_count = 0
    flagged_rows: list[tuple[SettingDoc, list[str]]] = []

    if settings_root.is_dir():
        for md_path in sorted(settings_root.rglob("*.md")):
            doc = parser.parse_setting_md(md_path, book_dir=bdir)
            if not include_archived and doc.status != EntityStatus.ACTIVE:
                continue
            if cat_filter is not None and doc.category != cat_filter:
                continue
            scanned_count += 1
            warnings = lint_model_setting_doc(doc)
            if warnings:
                flagged_rows.append((doc, warnings))

    total_warnings = sum(len(ws) for _, ws in flagged_rows)
    if total_warnings == 0:
        console.print(
            Panel(
                f"[bold green]✅ 抽象层体检通过：[/]共扫描 {scanned_count} 份模型侧设定卡（`01_设定集/`），"
                "未发现具象场景、台词例句、预设动作清单或作者备忘泄露风险。",
                title="设定抽象层体检 (setting lint)",
                border_style="green",
            )
        )
        return

    console.print(
        Panel(
            f"[bold yellow]⚠️ 检测到模型侧设定抽象层泄露风险：[/]共扫描 {scanned_count} 份设定卡，"
            f"[bold]{len(flagged_rows)}[/] 份命中 [bold red]{total_warnings}[/] 项告警。\n"
            "建议将具象示范场景、台词例句、动作清单及灵感随笔移回 `00_作者设定/`，"
            "并在 `01_设定集/` 仅保留“客观属性 + 抽象性格机制 + 规则边界”。",
            title="设定抽象层体检 (setting lint)",
            border_style="yellow",
        )
    )

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("实体 ID", style="cyan", no_wrap=True)
    table.add_column("名称", style="bold")
    table.add_column("分类")
    table.add_column("文件路径", style="dim")
    table.add_column("告警明细")
    for doc, ws in flagged_rows:
        table.add_row(
            doc.id,
            doc.name,
            doc.category.value,
            doc.file_path or "-",
            "\n".join(f"- {w}" for w in ws),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Group 4: 结构与蓝图 (structure)
# ---------------------------------------------------------------------------


@structure_app.command(
    "volume",
    epilog=(
        "示例:\n"
        "  ddnovel structure volume vol_01 -t 第一卷·青云初试 -r 1-60 -c 宗门大比危机\n"
        "  ddnovel structure volume vol_01 -F '林凡入宗门夺魁并揭开暗线'"
    ),
)
@_cli_guard
def structure_volume_cmd(
    ctx: typer.Context,
    volume_id: Annotated[
        str | None,
        typer.Argument(help="分卷 ID（如 vol_01）"),
    ] = None,
    id_opt: Annotated[
        str | None,
        typer.Option("--id", help="分卷 ID（与位置参数二选一）"),
    ] = None,
    title: Annotated[
        str | None,
        typer.Option("--title", "-t", help="分卷标题"),
    ] = None,
    chapter_range: Annotated[
        str | None,
        typer.Option("--range", "--chapter-range", "-r", help="章节范围，如 '1-100'"),
    ] = None,
    start_ch: Annotated[
        int | None,
        typer.Option("--start-ch", "-s", help="起始章号"),
    ] = None,
    end_ch: Annotated[
        int | None,
        typer.Option("--end-ch", "-e", help="结束章号"),
    ] = None,
    conflict: Annotated[
        str | None,
        typer.Option("--conflict", "--core-conflict", "-c", help="本卷核心矛盾冲突一句话概述"),
    ] = None,
    folded_summary: Annotated[
        str | None,
        typer.Option("--folded-summary", "-F", help="完结卷折叠摘要（替代百章明细供后期宏观定位）"),
    ] = None,
    body: Annotated[
        str | None,
        typer.Option("--body", "-b", help="分卷规划 Markdown 正文"),
    ] = None,
    from_file: Annotated[
        Path | None,
        typer.Option("--from-file", "-f", help="从指定 Markdown 文件加载分卷内容"),
    ] = None,
    book_dir: BookDirOption = None,
) -> None:
    """创建或更新分卷纲要文档 (02_结构/分卷/第XX卷-<卷名>.md)，支持写入卷级折叠摘要。"""
    bdir = _resolve_book_dir(book_dir, ctx)

    file_fm: dict[str, Any] = {}
    file_body = ""
    if from_file is not None:
        src_path = Path(from_file).resolve()
        if not src_path.is_file():
            raise FileNotFoundError(f"未找到分卷输入文件: {src_path}")
        file_fm, file_body = split_frontmatter(src_path.read_text(encoding="utf-8"))

    raw_vid = volume_id or id_opt or file_fm.get("id")
    if not raw_vid or not str(raw_vid).strip():
        raise ValueError("必须指定分卷 ID（例如: ddnovel structure volume vol_01 --title 破局）")
    vid = str(raw_vid).strip()

    existing_path = resolve_volume_file(bdir, vid, for_write=False)
    existing_fm: dict[str, Any] = {}
    existing_body = ""
    if existing_path.is_file():
        existing_fm, existing_body = split_frontmatter(existing_path.read_text(encoding="utf-8"))

    merged_fm = {**existing_fm, **file_fm, "id": vid}
    if title is not None:
        merged_fm["title"] = title
    parsed_range = _parse_chapter_range(chapter_range, start_ch, end_ch)
    if parsed_range is not None:
        merged_fm["chapter_range"] = [parsed_range.start, parsed_range.end]
    if conflict is not None:
        merged_fm["core_conflict"] = conflict
    if folded_summary is not None:
        merged_fm["folded_summary"] = folded_summary

    resolved_body = (
        body
        if body is not None
        else (file_body if from_file is not None else existing_body)
    )
    doc = VolumeDoc.model_validate({**merged_fm, "body": resolved_body})

    fm_out = {
        "id": doc.id,
        "title": doc.title,
        "chapter_range": (
            [doc.chapter_range.start, doc.chapter_range.end]
            if doc.chapter_range is not None
            else None
        ),
        "core_conflict": doc.core_conflict,
        "folded_summary": doc.folded_summary,
    }
    target_path = resolve_volume_file(
        bdir,
        doc.id,
        title=doc.title or "",
        for_write=True,
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(_render_frontmatter_md(fm_out, doc.body), encoding="utf-8")
    if existing_path.is_file() and not existing_path.samefile(target_path):
        existing_path.unlink(missing_ok=True)

    with SqliteIndexer(bdir) as indexer:
        indexer.sync()

    rel_path = target_path.relative_to(bdir).as_posix()
    console.print(
        Panel(
            f"[bold green]已保存分卷纲要：[/]{rel_path}\n"
            f"分卷 ID: [cyan]{doc.id}[/] | 标题: [bold]{doc.title or '（未命名）'}[/] | "
            f"章节区间: {fm_out['chapter_range'] or '未指定'}\n"
            f"核心冲突: {doc.core_conflict or '无'} | "
            f"折叠摘要: {'已设置' if doc.folded_summary else '未设置'}",
            title="分卷结构规划 (structure volume)",
            border_style="green",
        )
    )


@structure_app.command(
    "arc",
    epilog=(
        "示例:\n"
        "  ddnovel structure arc vol_01_arc_01 -v vol_01 -t 外门试炼 -r 1-25\n"
        "  ddnovel structure arc vol_01_arc_01 -F '外门试炼结束，林凡进入内门'"
    ),
)
@_cli_guard
def structure_arc_cmd(
    ctx: typer.Context,
    arc_id: Annotated[
        str | None,
        typer.Argument(help="剧情单元 ID（如 vol_01_arc_01）"),
    ] = None,
    id_opt: Annotated[
        str | None,
        typer.Option("--id", help="剧情单元 ID（与位置参数二选一）"),
    ] = None,
    volume_id: Annotated[
        str | None,
        typer.Option("--volume-id", "-v", help="所属分卷 ID（默认从单元 ID 前缀推断）"),
    ] = None,
    title: Annotated[
        str | None,
        typer.Option("--title", "-t", help="剧情单元标题"),
    ] = None,
    chapter_range: Annotated[
        str | None,
        typer.Option("--range", "--chapter-range", "-r", help="单元章节范围，如 '1-25'"),
    ] = None,
    start_ch: Annotated[
        int | None,
        typer.Option("--start-ch", "-s", help="起始章号"),
    ] = None,
    end_ch: Annotated[
        int | None,
        typer.Option("--end-ch", "-e", help="结束章号"),
    ] = None,
    folded_summary: Annotated[
        str | None,
        typer.Option("--folded-summary", "-F", help="完结单元折叠摘要"),
    ] = None,
    body: Annotated[
        str | None,
        typer.Option("--body", "-b", help="单元规划 Markdown 正文"),
    ] = None,
    from_file: Annotated[
        Path | None,
        typer.Option("--from-file", "-f", help="从指定 Markdown 文件加载单元内容"),
    ] = None,
    book_dir: BookDirOption = None,
) -> None:
    """创建或更新剧情单元设计文档 (02_结构/单元/第XX卷_第YY单元-<单元名>.md)。"""
    bdir = _resolve_book_dir(book_dir, ctx)

    file_fm: dict[str, Any] = {}
    file_body = ""
    if from_file is not None:
        src_path = Path(from_file).resolve()
        if not src_path.is_file():
            raise FileNotFoundError(f"未找到单元输入文件: {src_path}")
        file_fm, file_body = split_frontmatter(src_path.read_text(encoding="utf-8"))

    raw_aid = arc_id or id_opt or file_fm.get("id")
    if not raw_aid or not str(raw_aid).strip():
        raise ValueError("必须指定单元 ID（例如: ddnovel structure arc vol_01_arc_01 --title 潜龙初醒）")
    aid = str(raw_aid).strip()

    existing_path = resolve_arc_file(bdir, aid, for_write=False)
    existing_fm: dict[str, Any] = {}
    existing_body = ""
    if existing_path.is_file():
        existing_fm, existing_body = split_frontmatter(existing_path.read_text(encoding="utf-8"))

    merged_fm = {**existing_fm, **file_fm, "id": aid}
    if volume_id is not None:
        merged_fm["volume_id"] = volume_id
    if title is not None:
        merged_fm["title"] = title
    parsed_range = _parse_chapter_range(chapter_range, start_ch, end_ch)
    if parsed_range is not None:
        merged_fm["chapter_range"] = [parsed_range.start, parsed_range.end]
    if folded_summary is not None:
        merged_fm["folded_summary"] = folded_summary

    resolved_body = (
        body
        if body is not None
        else (file_body if from_file is not None else existing_body)
    )
    doc = ArcDoc.model_validate({**merged_fm, "body": resolved_body})

    fm_out = {
        "id": doc.id,
        "volume_id": doc.volume_id,
        "title": doc.title,
        "chapter_range": (
            [doc.chapter_range.start, doc.chapter_range.end]
            if doc.chapter_range is not None
            else None
        ),
        "folded_summary": doc.folded_summary,
    }
    target_path = resolve_arc_file(
        bdir,
        doc.id,
        title=doc.title or "",
        for_write=True,
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(_render_frontmatter_md(fm_out, doc.body), encoding="utf-8")
    if existing_path.is_file() and not existing_path.samefile(target_path):
        existing_path.unlink(missing_ok=True)

    with SqliteIndexer(bdir) as indexer:
        indexer.sync()

    rel_path = target_path.relative_to(bdir).as_posix()
    console.print(
        Panel(
            f"[bold green]已保存剧情单元：[/]{rel_path}\n"
            f"单元 ID: [cyan]{doc.id}[/] | 所属卷: {doc.volume_id} | "
            f"标题: [bold]{doc.title or '（未命名）'}[/]\n"
            f"章节区间: {fm_out['chapter_range'] or '未指定'} | "
            f"折叠摘要: {'已设置' if doc.folded_summary else '未设置'}",
            title="剧情单元规划 (structure arc)",
            border_style="green",
        )
    )


def _parse_thread_refs(raw_items: Sequence[str] | None) -> list[ThreadRef]:
    refs: list[ThreadRef] = []
    if not raw_items:
        return refs
    for item in raw_items:
        text = str(item).strip()
        if not text:
            continue
        if ":" in text or "：" in text or "=" in text:
            parts = re.split(r"[:：=]", text, maxsplit=1)
            tid = parts[0].strip()
            act = ThreadAction(parts[1].strip().lower())
        else:
            tid = text
            act = ThreadAction.PROGRESS
        refs.append(ThreadRef(thread_id=tid, action=act))
    return refs


def _parse_topic_refs(raw_items: Sequence[str] | None) -> list[TopicRef]:
    refs: list[TopicRef] = []
    if not raw_items:
        return refs
    for item in raw_items:
        text = str(item).strip()
        if not text:
            continue
        parts = re.split(r"[:：=]", text, maxsplit=1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            raise ValueError(f"蓝图专题挂载格式错误 '{text}'，请使用 'entity_id:专题名' 格式")
        refs.append(TopicRef(entity_id=parts[0].strip(), topic=parts[1].strip()))
    return refs


def _parse_beat_opts(raw_items: Sequence[str] | None) -> list[Beat]:
    beats: list[Beat] = []
    if not raw_items:
        return beats
    for idx, item in enumerate(raw_items, start=1):
        text = str(item).strip()
        if not text:
            continue
        if ":" in text or "：" in text:
            parts = re.split(r"[:：]", text, maxsplit=1)
            b_title = parts[0].strip()
            b_content = parts[1].strip()
        else:
            b_title = f"节拍 {idx}"
            b_content = text
        beats.append(Beat(index=idx, title=b_title, content=b_content))
    return beats


@structure_app.command(
    "blueprint",
    epilog=(
        "示例:\n"
        "  ddnovel structure blueprint 10 -v vol_01 -t 夜探禁地 -p char_linfan "
        "-C char_linfan,char_elder\n"
        "  ddnovel structure blueprint 10 -l loc_forbidden -T 'thread_01:progress' "
        "-U char_elder -B '入阵:潜入禁地'"
    ),
)
@_cli_guard
def structure_blueprint_cmd(
    ctx: typer.Context,
    chapter: Annotated[
        int | None,
        typer.Argument(help="章节号（正整数，如 1 或 142）"),
    ] = None,
    chapter_opt: Annotated[
        int | None,
        typer.Option("--chapter", "-c", help="章节号（与位置参数二选一）"),
    ] = None,
    volume_id: Annotated[
        str | None,
        typer.Option("--volume-id", "-v", help="所属分卷 ID（默认 vol_01 或从 arc_id 推断）"),
    ] = None,
    arc_id: Annotated[
        str | None,
        typer.Option("--arc-id", "-a", help="所属剧情单元 ID（如 vol_01_arc_01）"),
    ] = None,
    title: Annotated[
        str | None,
        typer.Option("--title", "-t", help="本章标题"),
    ] = None,
    pov: Annotated[
        str | None,
        typer.Option("--pov", "-p", help="本章叙事视角角色 ID（如 char_linfan）"),
    ] = None,
    character: Annotated[
        list[str] | None,
        typer.Option("--character", "--cast", "-C", help="本章出场角色 ID（可多次指定或逗号分隔）"),
    ] = None,
    location: Annotated[
        list[str] | None,
        typer.Option("--location", "-l", help="本章涉及地点 ID（可多次指定或逗号分隔）"),
    ] = None,
    faction: Annotated[
        list[str] | None,
        typer.Option("--faction", "-F", help="本章涉及势力 ID（可多次指定或逗号分隔）"),
    ] = None,
    item: Annotated[
        list[str] | None,
        typer.Option("--item", "-i", help="本章涉及物品 ID（可多次指定或逗号分隔）"),
    ] = None,
    thread: Annotated[
        list[str] | None,
        typer.Option("--thread", "-T", help="本章指定伏笔动作，格式 'thread_id:plant|progress|resolve'"),
    ] = None,
    unlock_secret: Annotated[
        list[str] | None,
        typer.Option("--unlock-secret", "-U", help="本章解锁 [机密] 的实体 ID"),
    ] = None,
    topic: Annotated[
        list[str] | None,
        typer.Option("--topic", "-O", help="本章挂载的实体专题，格式 'entity_id:专题名'"),
    ] = None,
    forbidden: Annotated[
        list[str] | None,
        typer.Option("--forbidden", "-X", help="本章负向禁止事项红线（可多次指定）"),
    ] = None,
    beat: Annotated[
        list[str] | None,
        typer.Option("--beat", "-B", help="本章分场节拍，格式 '节拍标题:节拍内容'（可多次指定）"),
    ] = None,
    body: Annotated[
        str | None,
        typer.Option("--body", "-b", help="蓝图补充正文说明"),
    ] = None,
    from_file: Annotated[
        Path | None,
        typer.Option("--from-file", "-f", help="从指定 Markdown 文件加载章节蓝图"),
    ] = None,
    book_dir: BookDirOption = None,
) -> None:
    """创建或更新单章分场节拍蓝图 (02_结构/章节蓝图/第XX卷/第XXXX章-<章节标题>.md)。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    cfg = _load_book_config(bdir)
    parser = ProgressiveParser(book_dir=bdir)

    file_bp: BlueprintDoc | None = None
    if from_file is not None:
        src_path = Path(from_file).resolve()
        if not src_path.is_file():
            raise FileNotFoundError(f"未找到蓝图输入文件: {src_path}")
        file_bp = parser.parse_blueprint_md(src_path, book_dir=bdir)

    resolved_ch = chapter or chapter_opt or (file_bp.chapter if file_bp is not None else None)
    if resolved_ch is None or isinstance(resolved_ch, bool) or resolved_ch < 1:
        raise ValueError("必须提供 >= 1 的合法章号（例如: ddnovel structure blueprint 1 --title 首章）")

    existing_path = find_existing_blueprint_file(
        bdir,
        resolved_ch,
        width=cfg.chapter_id_width,
    )
    existing_bp = (
        parser.parse_blueprint_md(existing_path, book_dir=bdir)
        if existing_path is not None
        else None
    )

    base = file_bp or existing_bp
    resolved_arc = arc_id if arc_id is not None else (base.arc_id if base is not None else None)
    resolved_vol = (
        volume_id
        if volume_id is not None
        else (
            base.volume_id
            if base is not None and base.volume_id is not None
            else (
                resolved_arc.split("_arc_", maxsplit=1)[0]
                if resolved_arc and "_arc_" in resolved_arc
                else "vol_01"
            )
        )
    )
    resolved_title = (
        title
        if title is not None
        else (base.title if base is not None and base.title else f"第{resolved_ch}章")
    )
    resolved_pov = pov if pov is not None else (base.pov if base is not None else None)

    cast_obj = BlueprintCast(
        characters=(
            _split_multi_opts(character)
            if character is not None
            else (list(base.cast.characters) if base is not None else [])
        ),
        locations=(
            _split_multi_opts(location)
            if location is not None
            else (list(base.cast.locations) if base is not None else [])
        ),
        factions=(
            _split_multi_opts(faction)
            if faction is not None
            else (list(base.cast.factions) if base is not None else [])
        ),
        items=(
            _split_multi_opts(item)
            if item is not None
            else (list(base.cast.items) if base is not None else [])
        ),
    )
    resolved_threads = (
        _parse_thread_refs(thread)
        if thread is not None
        else (list(base.threads) if base is not None else [])
    )
    resolved_secrets = (
        _split_multi_opts(unlock_secret)
        if unlock_secret is not None
        else (list(base.unlock_secrets) if base is not None else [])
    )
    resolved_topics = (
        _parse_topic_refs(topic)
        if topic is not None
        else (list(base.topics) if base is not None else [])
    )
    resolved_forbidden = (
        [f.strip() for f in forbidden if f.strip()]
        if forbidden is not None
        else (list(base.forbidden) if base is not None else [])
    )
    resolved_beats = (
        _parse_beat_opts(beat)
        if beat is not None
        else (list(base.beats) if base is not None else [])
    )
    resolved_body = body if body is not None else (base.body if base is not None else "")

    bp_doc = BlueprintDoc(
        chapter=resolved_ch,
        volume_id=resolved_vol,
        arc_id=resolved_arc,
        title=resolved_title,
        pov=resolved_pov,
        cast=cast_obj,
        threads=resolved_threads,
        unlock_secrets=resolved_secrets,
        topics=resolved_topics,
        forbidden=resolved_forbidden,
        beats=resolved_beats,
        body=resolved_body,
    )

    fm_out = {
        "chapter": bp_doc.chapter,
        "volume_id": bp_doc.volume_id,
        "arc_id": bp_doc.arc_id,
        "title": bp_doc.title,
        "pov": bp_doc.pov,
        "cast": bp_doc.cast.model_dump(mode="json"),
        "threads": [t.model_dump(mode="json") for t in bp_doc.threads],
        "unlock_secrets": list(bp_doc.unlock_secrets),
        "topics": [tp.model_dump(mode="json") for tp in bp_doc.topics],
        "forbidden": list(bp_doc.forbidden),
        "beats": [b.model_dump(mode="json") for b in bp_doc.beats],
    }

    target_path = resolve_blueprint_file(
        bdir,
        bp_doc.volume_id or "vol_01",
        bp_doc.chapter,
        width=cfg.chapter_id_width,
        title=bp_doc.title or "",
        for_write=True,
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(_render_frontmatter_md(fm_out, bp_doc.body), encoding="utf-8")
    if (
        existing_path is not None
        and existing_path.is_file()
        and not existing_path.samefile(target_path)
    ):
        existing_path.unlink(missing_ok=True)

    with SqliteIndexer(bdir) as indexer:
        indexer.sync()

    rel_path = target_path.relative_to(bdir).as_posix()
    console.print(
        Panel(
            f"[bold green]已保存单章蓝图：[/]{rel_path}\n"
            f"章节: 第 [cyan]{bp_doc.chapter}[/] 章 ({bp_doc.title}) | "
            f"分卷/单元: {bp_doc.volume_id} / {bp_doc.arc_id or '-'}\n"
            f"POV: [bold]{bp_doc.pov or '未指定'}[/] | "
            f"关联实体数: {len(bp_doc.all_entity_ids())} | "
            f"伏笔动作数: {len(bp_doc.threads)} | "
            f"节拍数: {len(bp_doc.beats)}",
            title="章节蓝图规划 (structure blueprint)",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# Group 5: 伏笔雷达 (radar)
# ---------------------------------------------------------------------------


@app.command(
    "radar",
    epilog="示例:\n  ddnovel radar -c 10\n  ddnovel radar -c 10 -e char_linfan,item_gujing -s 15",
)
@_cli_guard
def radar_cmd(
    ctx: typer.Context,
    chapter: Annotated[
        int | None,
        typer.Option("--chapter", "-c", help="目标规划章号（默认取 current_chapter + 1）"),
    ] = None,
    entity: Annotated[
        list[str] | None,
        typer.Option("--entity", "-e", help="本章计划出场实体 ID 或名称（可多次指定或逗号分隔）"),
    ] = None,
    stale_threshold: Annotated[
        int | None,
        typer.Option("--stale-threshold", "-s", help="超期未推进断线告警章数阈值"),
    ] = None,
    include_blueprint: Annotated[
        bool,
        typer.Option(
            "--include-blueprint/--no-blueprint",
            "-B",
            help="未传 --entity 时是否自动提取目标章蓝图出场实体",
        ),
    ] = True,
    book_dir: BookDirOption = None,
) -> None:
    """扫描全书活跃伏笔台账，输出窗口到期、超期断线、出场实体关联三类候选伏笔雷达报告。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    planned = _split_multi_opts(entity) if entity is not None else None

    with ForeshadowRadar(bdir) as radar:
        report = radar.scan(
            target_chapter=chapter,
            planned_entities=planned,
            stale_threshold=stale_threshold,
            include_blueprint_entities=include_blueprint,
        )

    win_hits = [h for h in report.hits if RadarReason.WINDOW in h.reasons]
    stale_hits = [h for h in report.hits if RadarReason.STALE in h.reasons]
    entity_hits = [h for h in report.hits if RadarReason.ENTITY in h.reasons]

    console.print(
        Panel(
            f"[bold]目标章节:[/] 第 [cyan]{report.target_chapter}[/] 章    "
            f"[bold]活跃悬线总数:[/] {report.total_active_threads}    "
            f"[bold]雷达命中总数:[/] [bold yellow]{len(report.hits)}[/]\n"
            f"[bold]计划出场实体:[/] {', '.join(report.planned_entities) or '（无）'}\n"
            f"[bold]三类分布:[/] 窗口到期(window)={len(win_hits)} | "
            f"超期断线(stale)={len(stale_hits)} | "
            f"实体联动(entity)={len(entity_hits)}",
            title="规划期悬线伏笔三类雷达巡检 (radar)",
            border_style="cyan",
        )
    )

    if not report.hits:
        console.print("[dim]当前章节无触发告警的候选伏笔。[/]")
        return

    table = Table(
        title=f"第 {report.target_chapter} 章候选伏笔巡检表",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("伏笔 ID", style="cyan", no_wrap=True)
    table.add_column("标题", style="bold")
    table.add_column("状态")
    table.add_column("触发类别")
    table.add_column("埋设/推进章", justify="center")
    table.add_column("预计窗口", justify="center")
    table.add_column("表面线索与告警详情")

    reason_labels = {
        RadarReason.WINDOW: "窗口到期(window)",
        RadarReason.STALE: "超期断线(stale)",
        RadarReason.ENTITY: "实体联动(entity)",
    }
    for hit in report.hits:
        thr = hit.thread
        win_str = (
            f"[{thr.expected_window.start}, {thr.expected_window.end}]"
            if thr.expected_window is not None
            else "-"
        )
        r_str = ", ".join(reason_labels[r] for r in hit.reasons)
        table.add_row(
            thr.id,
            thr.title,
            thr.status.value,
            r_str,
            f"{thr.planted_at} / {thr.last_progressed_at}",
            win_str,
            f"{hit.detail}\n[dim]线索: {thr.public_clue}[/]" if thr.public_clue else hit.detail,
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Group 6: 写作包输出 (packet)
# ---------------------------------------------------------------------------


@packet_app.command("build")
@_cli_guard
def packet_build_cmd(
    ctx: typer.Context,
    chapter: Annotated[
        int,
        typer.Argument(help="要编译写作包的目标章号（如 1 或 142）"),
    ],
    budget: Annotated[
        int | None,
        typer.Option("--budget", "-b", help="自定义单章总 Token 预算上限"),
    ] = None,
    book_dir: BookDirOption = None,
) -> None:
    """按 U 型注意力曲线与三重物理隔离一步编译单章独立写作包 (04_创作/写作包/第XXXX章_写作包.md)。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    with PacketBuilder(bdir) as builder:
        res = builder.build(chapter_num=chapter, custom_budget=budget)

    table = Table(
        title=f"第 {res.chapter} 章 U 型五槽位 Token 预算统计",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("槽位名称 (Slot)", style="bold")
    table.add_column("预算上限", justify="right")
    table.add_column("实际占用", justify="right", style="cyan")
    table.add_column("是否裁剪", justify="center")
    table.add_column("已裁剪/降级条目")

    for slot in res.slots:
        table.add_row(
            slot.name.value,
            str(slot.budget),
            str(slot.used),
            "[yellow]是[/]" if slot.trimmed else "否",
            "、".join(slot.dropped_items) if slot.dropped_items else "-",
        )
    console.print(table)

    warn_text = "\n".join(f"  - [yellow]{w}[/]" for w in res.warnings) if res.warnings else "  无"
    secret_list_str = ", ".join(res.blocked_secrets) or "无"
    console.print(
        Panel(
            f"[bold green]写作包输出路径:[/] {res.packet_path}\n"
            f"[bold]总 Token 占用:[/] [cyan]{res.total_tokens}[/] / {res.budget_total or '-'} Tokens\n"
            f"[bold]物理隔离机密实体数:[/] {len(res.blocked_secrets)} ({secret_list_str})\n"
            f"[bold]物理屏蔽非本章伏笔数:[/] {len(res.blocked_threads)}\n"
            f"[bold]编译提示/告警:[/]\n{warn_text}",
            title=f"单章独立写作包编译完成 · 第 {res.chapter} 章",
            border_style="green",
        )
    )


def _locate_packet_file(book_dir: Path, chapter: int) -> Path:
    if isinstance(chapter, bool) or chapter < 1:
        raise ValueError(f"章号必须为 >= 1 的正整数，收到: {chapter!r}")
    cfg = _load_book_config(book_dir)
    pkt_path = resolve_packet_file(book_dir, chapter, width=cfg.chapter_id_width)
    if pkt_path.is_file():
        return pkt_path
    raise FileNotFoundError(
        f"未找到第 {chapter} 章写作包文件 ({pkt_path})，"
        f"请先执行 'ddnovel packet build {chapter}'"
    )


@packet_app.command("show")
@_cli_guard
def packet_show_cmd(
    ctx: typer.Context,
    chapter: Annotated[
        int,
        typer.Argument(help="要查阅写作包的章号"),
    ],
    book_dir: BookDirOption = None,
) -> None:
    """查阅已编译的单章独立写作包内容。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    pkt_path = _locate_packet_file(bdir, chapter)
    content = pkt_path.read_text(encoding="utf-8")
    rel_path = pkt_path.relative_to(bdir).as_posix()
    console.print(
        Panel(
            content,
            title=f"单章写作包 · {rel_path} (~{estimate_tokens(content)} Tokens)",
            border_style="cyan",
        )
    )


@packet_app.command("read")
@_cli_guard
def packet_read_cmd(
    ctx: typer.Context,
    chapter: Annotated[
        int,
        typer.Argument(help="要读取写作包的章号"),
    ],
    book_dir: BookDirOption = None,
) -> None:
    """读取并显示已编译的单章独立写作包（packet show 的同义命令）。"""
    packet_show_cmd(ctx=ctx, chapter=chapter, book_dir=book_dir)


# ---------------------------------------------------------------------------
# Group 7: 草稿创作 (draft)
# ---------------------------------------------------------------------------


@draft_app.command("save")
@_cli_guard
def draft_save_cmd(
    ctx: typer.Context,
    chapter: Annotated[
        int,
        typer.Argument(help="草稿所属章号（正整数）"),
    ],
    content: Annotated[
        str | None,
        typer.Option("--content", "-c", help="整章草稿 Markdown 正文文本"),
    ] = None,
    from_file: Annotated[
        Path | None,
        typer.Option("--from-file", "-f", help="从指定文件读取整章草稿内容"),
    ] = None,
    book_dir: BookDirOption = None,
) -> None:
    """保存单章初稿至 04_创作/草稿/第XXXX章_草稿.md 供作者通篇审改。"""
    if isinstance(chapter, bool) or chapter < 1:
        raise ValueError(f"章号必须为 >= 1 的正整数，收到: {chapter!r}")
    bdir = _resolve_book_dir(book_dir, ctx)
    cfg = _load_book_config(bdir)

    resolved_text: str | None = content
    if from_file is not None:
        src_path = Path(from_file).resolve()
        if not src_path.is_file():
            raise FileNotFoundError(f"未找到草稿来源文件: {src_path}")
        resolved_text = src_path.read_text(encoding="utf-8")

    if resolved_text is None or not resolved_text.strip():
        raise ValueError("草稿内容不能为空，请通过 --content 或 --from-file 提供章节草稿正文")

    draft_path = resolve_draft_file(bdir, chapter, width=cfg.chapter_id_width, for_write=True)
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    clean_text = resolved_text.rstrip() + "\n"
    draft_path.write_text(clean_text, encoding="utf-8")

    rel_path = draft_path.relative_to(bdir).as_posix()
    console.print(
        Panel(
            f"[bold green]已保存章节初稿:[/] {rel_path}\n"
            f"章节: 第 [cyan]{chapter}[/] 章 | 字符数: {len(clean_text)} | "
            f"估算 Token: ~{estimate_tokens(clean_text)}",
            title="草稿保存 (draft save)",
            border_style="green",
        )
    )


def _locate_draft_path(book_dir: Path, chapter: int) -> Path:
    if isinstance(chapter, bool) or chapter < 1:
        raise ValueError(f"章号必须为 >= 1 的正整数，收到: {chapter!r}")
    cfg = _load_book_config(book_dir)
    draft_path = resolve_draft_file(book_dir, chapter, width=cfg.chapter_id_width)
    if draft_path.is_file():
        return draft_path
    raise FileNotFoundError(f"未找到第 {chapter} 章草稿文件: {draft_path}")


@draft_app.command("show")
@_cli_guard
def draft_show_cmd(
    ctx: typer.Context,
    chapter: Annotated[
        int,
        typer.Argument(help="要查阅草稿的章号"),
    ],
    book_dir: BookDirOption = None,
) -> None:
    """查阅 04_创作/草稿/第XXXX章_草稿.md 中的整章草稿。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    draft_path = _locate_draft_path(bdir, chapter)
    text = draft_path.read_text(encoding="utf-8")
    rel_path = draft_path.relative_to(bdir).as_posix()
    console.print(
        Panel(
            text,
            title=f"第 {chapter} 章初稿 · {rel_path} ({len(text)} 字符)",
            border_style="cyan",
        )
    )


@draft_app.command("read")
@_cli_guard
def draft_read_cmd(
    ctx: typer.Context,
    chapter: Annotated[
        int,
        typer.Argument(help="要读取草稿的章号"),
    ],
    book_dir: BookDirOption = None,
) -> None:
    """读取并显示指定章节初稿（draft show 的同义命令）。"""
    draft_show_cmd(ctx=ctx, chapter=chapter, book_dir=book_dir)


# ---------------------------------------------------------------------------
# Group 8: 定稿与结算 (chapter & state)
# ---------------------------------------------------------------------------


@chapter_app.command(
    "promote",
    epilog=(
        "示例:\n"
        "  ddnovel chapter promote 10 -v vol_01          # 审校通过并定稿归档到 vol_01\n"
        "  ddnovel chapter promote 10 -v vol_01 -S       # 严格模式（有 WARNING 也阻止定稿）\n"
        "  ddnovel chapter promote 10 -n                 # 仅预览归档路径与过期清理，不写盘"
    ),
)
@_cli_guard
def chapter_promote_cmd(
    ctx: typer.Context,
    chapter: Annotated[
        int,
        typer.Argument(help="要审校定稿入库的章号"),
    ],
    volume_id: Annotated[
        str | None,
        typer.Option("--volume-id", "-v", help="显式指定归档分卷 ID（默认自动推断）"),
    ] = None,
    no_audit: Annotated[
        bool,
        typer.Option("--no-audit", "-N", help="跳过定稿前静态一致性体检"),
    ] = False,
    strict: Annotated[
        bool,
        typer.Option("--strict", "-S", help="严格模式：存在任何 WARNING 或 ERROR 均阻止定稿"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="仅预览归档路径与待清理超期过程文件，不执行实际写盘"),
    ] = False,
    book_dir: BookDirOption = None,
) -> None:
    """将审校通过的草稿正式定稿归入 manuscript/，并自动触发超期旧包/旧稿 TTL 清理。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    with LifecycleManager(bdir) as lm:
        if not no_audit:
            audit_rep = lm.audit_chapter(chapter)
            has_blocking = (not audit_rep.passed) or (strict and len(audit_rep.issues) > 0)
            if has_blocking:
                _render_audit_report(audit_rep)
                raise ValueError(f"第 {chapter} 章定稿前一致性体检未通过，请修复上述问题或使用 --no-audit")

        res = lm.promote_chapter(
            chapter,
            volume_id=volume_id,
            dry_run=dry_run,
            run_audit=False,
        )

    cleaned_str = (
        "\n".join(f"  - {p}" for p in res.cleaned_paths)
        if res.cleaned_paths
        else "  无超期过程文件"
    )
    console.print(
        Panel(
            f"[bold green]定稿章节:[/] 第 [cyan]{res.chapter}[/] 章 "
            f"({'预览模式 dry-run' if res.dry_run else '已正式入库'})\n"
            f"[bold]来源草稿:[/] {res.draft_path}\n"
            f"[bold]正式正文:[/] [bold cyan]{res.manuscript_path}[/]\n"
            f"[bold]TTL 自动清理过程文件 ({len(res.cleaned_paths)} 个):[/]\n{cleaned_str}",
            title="章节定稿入库 (chapter promote)",
            border_style="green",
        )
    )


@chapter_app.command(
    "export",
    epilog=(
        "示例:\n"
        "  ddnovel chapter export -d .                    # 将全部定稿合并导出到 04_创作/<书名>.txt\n"
        "  ddnovel chapter export -d . -o ./全本.txt       # 指定导出路径\n"
        "  ddnovel chapter export -d . -v vol_01          # 仅导出第 1 卷定稿\n"
        "  ddnovel chapter export -d . --start 1 --end 10 # 仅导出第 1~10 章\n"
        "  ddnovel chapter export -d . -n                 # 仅预览章节列表与输出路径，不写盘"
    ),
)
@_cli_guard
def chapter_export_cmd(
    ctx: typer.Context,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="导出 TXT 文件路径（默认写入 04_创作/<书名>.txt）"),
    ] = None,
    volume_id: Annotated[
        str | None,
        typer.Option("--volume-id", "-v", help="仅导出指定分卷 ID（如 vol_01）"),
    ] = None,
    start: Annotated[
        int | None,
        typer.Option("--start", help="起始章号（含）"),
    ] = None,
    end: Annotated[
        int | None,
        typer.Option("--end", help="结束章号（含）"),
    ] = None,
    raw_markdown: Annotated[
        bool,
        typer.Option("--raw-markdown", help="保留 Markdown 标题前缀（默认剥离 # 以适配纯文本阅读）"),
    ] = False,
    no_title: Annotated[
        bool,
        typer.Option("--no-title", help="不在文件开头添加《书名》标题头"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="仅预览将合并的章节与输出路径，不执行写盘"),
    ] = False,
    book_dir: BookDirOption = None,
) -> None:
    """将所有定稿正文按章序合并打包为单个 UTF-8 TXT 文件。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    resolved_out = (
        (output if output.is_absolute() else (Path.cwd() / output).resolve())
        if output is not None
        else None
    )
    with LifecycleManager(bdir) as lm:
        res = lm.export_manuscript_txt(
            output_path=resolved_out,
            volume_id=volume_id,
            start_chapter=start,
            end_chapter=end,
            include_book_title=not no_title,
            strip_markdown=not raw_markdown,
            dry_run=dry_run,
        )

    preview_list = "\n".join(f"  - {p}" for p in res.source_paths[:10])
    if len(res.source_paths) > 10:
        preview_list += f"\n  ... 另有 {len(res.source_paths) - 10} 章"
    console.print(
        Panel(
            f"[bold green]导出状态:[/] {'预览模式 dry-run' if res.dry_run else '已合并导出'}\n"
            f"[bold]输出文件:[/] [bold cyan]{res.output_path}[/]\n"
            f"[bold]合并章节:[/] {res.chapter_count} 章 | [bold]总字数:[/] {res.total_chars:,} 字符\n"
            f"[bold]源文件列表:[/]\n{preview_list}",
            title="定稿打包导出 (chapter export)",
            border_style="green",
        )
    )


def _infer_volume_for_chapter(book_dir: Path, chapter: int) -> str:
    bp_root = resolve_blueprints_dir(book_dir)
    if bp_root.is_dir():
        parser = ProgressiveParser(book_dir=book_dir)
        for p in sorted(bp_root.rglob("*.md")):
            if not p.is_file():
                continue
            bp = parser.parse_blueprint_md(p, book_dir=book_dir)
            if bp.chapter == chapter:
                if bp.volume_id:
                    return bp.volume_id
                for part in reversed(p.parts):
                    if _VOLUME_DIR_RE.match(part):
                        return part
    return "vol_01"


def _upsert_chapter_fact_jsonl(history_file: Path, record: ChapterFactRecord) -> None:
    history_file.parent.mkdir(parents=True, exist_ok=True)
    existing: list[ChapterFactRecord] = []
    if history_file.is_file():
        for raw_line in history_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line:
                existing.append(ChapterFactRecord.model_validate_json(line))
    replaced = False
    updated_records: list[ChapterFactRecord] = []
    for item in existing:
        if item.ch == record.ch:
            updated_records.append(record)
            replaced = True
        else:
            updated_records.append(item)
    if not replaced:
        updated_records.append(record)
    updated_records.sort(key=lambda r: r.ch)
    out_lines = [r.model_dump_json(by_alias=True, exclude_none=True) for r in updated_records]
    history_file.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def _parse_structured_kv_or_mapping(
    raw_items: Sequence[str] | None,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not raw_items:
        return result
    for raw in raw_items:
        text = str(raw).strip()
        if not text:
            continue
        loaded = yaml.safe_load(text)
        if isinstance(loaded, dict):
            # 支持 {"char_linfan": {"updated_at_ch": 1, ...}} 或单实体 {"id": "char_linfan", ...}
            if "id" in loaded and isinstance(loaded["id"], str):
                eid = loaded["id"].strip()
                payload = {k: v for k, v in loaded.items() if k != "id"}
                result[eid] = payload
            else:
                for k, v in loaded.items():
                    if isinstance(v, dict):
                        result[str(k).strip()] = v
            continue
        raise ValueError(f"状态更新参数格式无效: '{text}'，请传入 JSON/YAML 字典字符串")
    return result


def _parse_relation_specs(
    raw_items: Sequence[str] | None,
    default_ch: int,
) -> list[TemporalRelation]:
    rels: list[TemporalRelation] = []
    if not raw_items:
        return rels
    for raw in raw_items:
        text = str(raw).strip()
        if not text:
            continue
        if text.startswith("{"):
            loaded = yaml.safe_load(text)
            rels.append(TemporalRelation.model_validate(loaded))
            continue
        parts = [p.strip() for p in re.split(r"[:：]", text)]
        if len(parts) < 3:
            raise ValueError(
                f"关系参数格式无效 '{text}'，请使用 'source:target:关系描述[:生效章[:失效章]]'"
            )
        src, dst, desc = parts[0], parts[1], parts[2]
        v_from = int(parts[3]) if len(parts) >= 4 and parts[3] else max(1, default_ch)
        v_to = int(parts[4]) if len(parts) >= 5 and parts[4] else None
        rels.append(
            TemporalRelation(
                source=src,
                target=dst,
                relation=desc,
                valid_from_ch=v_from,
                invalid_at_ch=v_to,
            )
        )
    return rels


@state_app.command(
    "update",
    epilog=(
        "示例:\n"
        "  ddnovel state update -c 10 -v vol_01 -s '突破炼气三层' "
        "-F '击败执事' -F '获得玉简' -F '潜入禁地'\n"
        "  ddnovel state update -c 10 "
        "-C '{\"char_linfan\": {\"location\": \"loc_sect\", \"physical_mental\": \"突破\"}}'\n"
        "  ddnovel state update -c 10 -R 'char_linfan:char_elder:师徒:10' "
        "-T '{\"id\":\"th_01\",\"title\":\"玉简秘密\",\"status\":\"active\"}'"
    ),
)
@_cli_guard
def state_update_cmd(
    ctx: typer.Context,
    chapter: Annotated[
        int | None,
        typer.Option("--chapter", "-c", help="结算章节号（正整数）"),
    ] = None,
    summary: Annotated[
        str | None,
        typer.Option("--summary", "-s", help="本章百字摘要（写入 03_状态/历史/第XX卷_事实.jsonl）"),
    ] = None,
    fact: Annotated[
        list[str] | None,
        typer.Option("--fact", "-F", help="本章沉淀的原子事实条目（推荐 3~5 条，可多次指定）"),
    ] = None,
    entity: Annotated[
        list[str] | None,
        typer.Option("--entity", "-e", help="本章事实涉及的实体 ID（可多次指定或逗号分隔）"),
    ] = None,
    volume_id: Annotated[
        str | None,
        typer.Option("--volume-id", "-v", help="所属分卷 ID（如 vol_01）"),
    ] = None,
    arc_id: Annotated[
        str | None,
        typer.Option("--arc-id", "-a", help="所属剧情单元 ID（如 vol_01_arc_01）"),
    ] = None,
    in_world_time: Annotated[
        str | None,
        typer.Option("--time", "--in-world-time", "-t", help="书内时间日历坐标（如 天历三年秋）"),
    ] = None,
    macro_situation: Annotated[
        str | None,
        typer.Option("--macro-situation", "-m", help="更新 世界状态.yaml 全局天下大势简述"),
    ] = None,
    char_state: Annotated[
        list[str] | None,
        typer.Option("--char-state", "-C", help="更新活跃角色状态 JSON/YAML 片段"),
    ] = None,
    faction_state: Annotated[
        list[str] | None,
        typer.Option("--faction-state", "-G", help="更新活跃势力状态 JSON/YAML 片段"),
    ] = None,
    relation: Annotated[
        list[str] | None,
        typer.Option(
            "--relation",
            "-R",
            help="新增或更新双时态关系，格式 'source:target:relation[:from[:to]]'",
        ),
    ] = None,
    thread: Annotated[
        list[str] | None,
        typer.Option("--thread", "-T", help="新增或更新伏笔条目 JSON/YAML 片段"),
    ] = None,
    from_file: Annotated[
        Path | None,
        typer.Option("--from-file", "-f", help="从 JSON/YAML 结算文件批量导入本章状态与事实更新"),
    ] = None,
    book_dir: BookDirOption = None,
) -> None:
    """沉淀单章百字摘要与原子事实 (第XX卷_事实.jsonl)，并覆写最新角色/势力/世界/关系/伏笔截面状态。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    cfg = _load_book_config(bdir)
    val_ctx = {"genre_profile": cfg.genre_profile}

    payload: dict[str, Any] = {}
    if from_file is not None:
        src_path = Path(from_file).resolve()
        if not src_path.is_file():
            raise FileNotFoundError(f"未找到状态结算文件: {src_path}")
        loaded = yaml.safe_load(src_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            payload = loaded

    resolved_ch = chapter or payload.get("ch") or payload.get("chapter")
    resolved_summary = summary if summary is not None else payload.get("summary")
    resolved_facts = (
        [f.strip() for f in fact if f.strip()]
        if fact is not None
        else payload.get("facts")
    )
    resolved_entities = (
        _split_multi_opts(entity)
        if entity is not None
        else payload.get("entities", [])
    )
    resolved_time = (
        in_world_time
        if in_world_time is not None
        else (payload.get("time") or payload.get("in_world_time"))
    )
    resolved_macro = (
        macro_situation if macro_situation is not None else payload.get("macro_situation")
    )

    updated_files: list[str] = []

    # 1. 写入单章摘要与原子事实 (03_state/history/vol_XX_facts.jsonl)
    if resolved_summary is not None or resolved_facts is not None:
        if resolved_ch is None:
            raise ValueError("记录章节事实必须提供 --chapter / -c 章号")
        if not resolved_summary or not resolved_facts:
            raise ValueError("记录章节事实必须同时提供 --summary 摘要与至少 1 条 --fact 原子事实")
        vol_id = (
            volume_id
            or payload.get("volume_id")
            or (
                arc_id.split("_arc_", maxsplit=1)[0]
                if arc_id and "_arc_" in arc_id
                else _infer_volume_for_chapter(bdir, int(resolved_ch))
            )
        )
        fact_rec = ChapterFactRecord(
            ch=int(resolved_ch),
            volume_id=vol_id,
            arc=arc_id or payload.get("arc") or payload.get("arc_id"),
            time=resolved_time,
            summary=str(resolved_summary),
            facts=list(resolved_facts),
            entities=list(resolved_entities or []),
        )
        hist_path = resolve_facts_file(bdir, vol_id, for_write=True)
        _upsert_chapter_fact_jsonl(hist_path, fact_rec)
        updated_files.append(hist_path.relative_to(bdir).as_posix())

    # 2. 更新 世界状态.yaml (兼容 world_state.yaml)
    if resolved_ch is not None or resolved_time is not None or resolved_macro is not None:
        ws_path = resolve_world_state_file(bdir, for_write=True)
        raw_ws = yaml.safe_load(ws_path.read_text(encoding="utf-8")) if ws_path.is_file() else {}
        ws_obj = WorldState.model_validate(raw_ws if isinstance(raw_ws, dict) else {})
        ws_updates: dict[str, Any] = {}
        if resolved_ch is not None and int(resolved_ch) >= ws_obj.current_chapter:
            ws_updates["current_chapter"] = int(resolved_ch)
        if resolved_time is not None:
            ws_updates["in_world_time"] = str(resolved_time)
        if resolved_macro is not None:
            ws_updates["macro_situation"] = str(resolved_macro)
        if ws_updates:
            ws_new = ws_obj.model_copy(update=ws_updates)
            ws_path.parent.mkdir(parents=True, exist_ok=True)
            ws_path.write_text(
                _dump_yaml(ws_new.model_dump(mode="json", by_alias=True, exclude_none=True)),
                encoding="utf-8",
            )
            updated_files.append(ws_path.relative_to(bdir).as_posix())

    # 3. 更新 人物状态.yaml（含冷库唤醒）
    char_updates = _parse_structured_kv_or_mapping(char_state)
    if isinstance(payload.get("characters"), dict):
        char_updates.update(payload["characters"])
    if char_updates:
        chars_path = resolve_active_characters_file(bdir, for_write=True)
        raw_chars = (
            yaml.safe_load(chars_path.read_text(encoding="utf-8"))
            if chars_path.is_file()
            else {}
        )
        cf = CharacterStateFile.model_validate(
            raw_chars if isinstance(raw_chars, dict) else {},
            context=val_ctx,
        )
        merged_chars = dict(cf.root)

        arch_chars_path = resolve_archive_characters_file(bdir, for_write=True)
        raw_arch_chars = (
            yaml.safe_load(arch_chars_path.read_text(encoding="utf-8"))
            if arch_chars_path.is_file()
            else {}
        )
        arch_cf = CharacterStateFile.model_validate(
            raw_arch_chars if isinstance(raw_arch_chars, dict) else {},
            context=val_ctx,
        )
        arch_chars = dict(arch_cf.root)
        arch_chars_modified = False

        for cid, c_dict in char_updates.items():
            merged_input = dict(c_dict)
            if cid in arch_chars:
                arch_state = arch_chars.pop(cid)
                arch_chars_modified = True
                if cid not in merged_chars:
                    merged_input = {
                        **arch_state.model_dump(mode="json", by_alias=True, exclude_none=True),
                        **merged_input,
                    }
            elif cid in merged_chars:
                merged_input = {
                    **merged_chars[cid].model_dump(mode="json", by_alias=True, exclude_none=True),
                    **merged_input,
                }
            if "updated_at_ch" not in c_dict and resolved_ch is not None:
                merged_input["updated_at_ch"] = int(resolved_ch)
            merged_chars[cid] = CharacterState.model_validate(merged_input, context=val_ctx)
        chars_path.parent.mkdir(parents=True, exist_ok=True)
        chars_path.write_text(
            _dump_yaml(
                CharacterStateFile(root=merged_chars).model_dump(
                    mode="json", by_alias=True, exclude_none=True
                )
            ),
            encoding="utf-8",
        )
        updated_files.append(chars_path.relative_to(bdir).as_posix())

        if arch_chars_modified:
            arch_chars_path.write_text(
                _dump_yaml(
                    CharacterStateFile(root=arch_chars).model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    )
                ),
                encoding="utf-8",
            )
            updated_files.append(arch_chars_path.relative_to(bdir).as_posix())

    # 4. 更新 势力状态.yaml（含冷库唤醒）
    fac_updates = _parse_structured_kv_or_mapping(faction_state)
    if isinstance(payload.get("factions"), dict):
        fac_updates.update(payload["factions"])
    if fac_updates:
        facs_path = resolve_active_factions_file(bdir, for_write=True)
        raw_facs = (
            yaml.safe_load(facs_path.read_text(encoding="utf-8"))
            if facs_path.is_file()
            else {}
        )
        ff = FactionStateFile.model_validate(
            raw_facs if isinstance(raw_facs, dict) else {},
            context=val_ctx,
        )
        merged_facs = dict(ff.root)

        arch_facs_path = resolve_archive_factions_file(bdir, for_write=True)
        raw_arch_facs = (
            yaml.safe_load(arch_facs_path.read_text(encoding="utf-8"))
            if arch_facs_path.is_file()
            else {}
        )
        arch_ff = FactionStateFile.model_validate(
            raw_arch_facs if isinstance(raw_arch_facs, dict) else {},
            context=val_ctx,
        )
        arch_facs = dict(arch_ff.root)
        arch_facs_modified = False

        for fid, f_dict in fac_updates.items():
            merged_input = dict(f_dict)
            if fid in arch_facs:
                arch_f_state = arch_facs.pop(fid)
                arch_facs_modified = True
                if fid not in merged_facs:
                    merged_input = {
                        **arch_f_state.model_dump(mode="json", by_alias=True, exclude_none=True),
                        **merged_input,
                    }
            elif fid in merged_facs:
                merged_input = {
                    **merged_facs[fid].model_dump(mode="json", by_alias=True, exclude_none=True),
                    **merged_input,
                }
            if "updated_at_ch" not in f_dict and resolved_ch is not None:
                merged_input["updated_at_ch"] = int(resolved_ch)
            merged_facs[fid] = FactionState.model_validate(merged_input, context=val_ctx)
        facs_path.parent.mkdir(parents=True, exist_ok=True)
        facs_path.write_text(
            _dump_yaml(
                FactionStateFile(root=merged_facs).model_dump(
                    mode="json", by_alias=True, exclude_none=True
                )
            ),
            encoding="utf-8",
        )
        updated_files.append(facs_path.relative_to(bdir).as_posix())

        if arch_facs_modified:
            arch_facs_path.write_text(
                _dump_yaml(
                    FactionStateFile(root=arch_facs).model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    )
                ),
                encoding="utf-8",
            )
            updated_files.append(arch_facs_path.relative_to(bdir).as_posix())

    # 5. 更新 relations.yaml
    eff_ch = int(resolved_ch) if resolved_ch is not None else max(1, cfg.current_chapter)
    rel_updates = _parse_relation_specs(relation, default_ch=eff_ch)
    if isinstance(payload.get("relations"), list):
        rel_updates.extend(TemporalRelation.model_validate(r) for r in payload["relations"])
    if rel_updates:
        rels_path = resolve_relations_file(bdir, for_write=True)
        raw_rels = (
            yaml.safe_load(rels_path.read_text(encoding="utf-8"))
            if rels_path.is_file()
            else {}
        )
        rf = RelationsFile.model_validate(raw_rels if isinstance(raw_rels, dict) else {})
        merged_rels = list(rf.relations)
        for new_r in rel_updates:
            replaced_rel = False
            for idx, old_r in enumerate(merged_rels):
                if (
                    old_r.source == new_r.source
                    and old_r.target == new_r.target
                    and old_r.valid_from_ch == new_r.valid_from_ch
                ):
                    merged_rels[idx] = new_r
                    replaced_rel = True
                    break
            if not replaced_rel:
                merged_rels.append(new_r)
        rels_path.parent.mkdir(parents=True, exist_ok=True)
        rels_path.write_text(
            _dump_yaml(
                RelationsFile(relations=merged_rels).model_dump(
                    mode="json", by_alias=True, exclude_none=True
                )
            ),
            encoding="utf-8",
        )
        updated_files.append(rels_path.relative_to(bdir).as_posix())

    # 6. 更新 threads.yaml
    thr_updates: list[Thread] = []
    if thread:
        for raw_t in thread:
            loaded_t = yaml.safe_load(raw_t)
            if isinstance(loaded_t, dict):
                thr_updates.append(Thread.model_validate(loaded_t))
            else:
                raise ValueError(f"伏笔更新参数必须为 JSON/YAML 对象字符串: {raw_t}")
    if isinstance(payload.get("threads"), list):
        thr_updates.extend(Thread.model_validate(t) for t in payload["threads"])
    if thr_updates:
        thrs_path = resolve_threads_file(bdir, for_write=True)
        raw_thrs = (
            yaml.safe_load(thrs_path.read_text(encoding="utf-8"))
            if thrs_path.is_file()
            else {}
        )
        tf = ThreadsFile.model_validate(raw_thrs if isinstance(raw_thrs, dict) else {})
        by_id = {t.id: t for t in tf.threads}
        for t_item in thr_updates:
            by_id[t_item.id] = t_item
        thrs_path.parent.mkdir(parents=True, exist_ok=True)
        thrs_path.write_text(
            _dump_yaml(
                ThreadsFile(threads=list(by_id.values())).model_dump(
                    mode="json", by_alias=True, exclude_none=True
                )
            ),
            encoding="utf-8",
        )
        updated_files.append(thrs_path.relative_to(bdir).as_posix())

    if not updated_files:
        raise ValueError(
            "未提供任何状态或事实更新项，请指定 --summary/--fact、--char-state、--relation 或 --from-file"
        )

    with SqliteIndexer(bdir) as indexer:
        sync_rep = indexer.sync()

    sync_summary = f"+{len(sync_rep.added)} ~{len(sync_rep.updated)} ({sync_rep.elapsed_ms:.1f} ms)"
    console.print(
        Panel(
            f"[bold green]已完成状态与事实结算（共更新 {len(updated_files)} 个文件）：[/]\n"
            + "\n".join(f"  - [cyan]{p}[/]" for p in updated_files)
            + f"\n[bold]索引同步:[/] {sync_summary}",
            title="定稿后状态结算 (state update)",
            border_style="green",
        )
    )


@state_app.command(
    "record-outcome",
    epilog=(
        "提示: 本命令为 state update 的同义命令，参数与用法完全一致；"
        "可运行 ddnovel state update -h 查看完整示例。"
    ),
)
@_cli_guard
def state_record_outcome_cmd(
    ctx: typer.Context,
    chapter: Annotated[int | None, typer.Option("--chapter", "-c", help="结算章节号（正整数）")] = None,
    summary: Annotated[
        str | None,
        typer.Option("--summary", "-s", help="本章百字摘要（写入 03_状态/历史/第XX卷_事实.jsonl）"),
    ] = None,
    fact: Annotated[
        list[str] | None,
        typer.Option("--fact", "-F", help="本章沉淀的原子事实条目（推荐 3~5 条，可多次指定）"),
    ] = None,
    entity: Annotated[
        list[str] | None,
        typer.Option("--entity", "-e", help="本章事实涉及的实体 ID（可多次指定或逗号分隔）"),
    ] = None,
    volume_id: Annotated[
        str | None,
        typer.Option("--volume-id", "-v", help="所属分卷 ID（如 vol_01）"),
    ] = None,
    arc_id: Annotated[
        str | None,
        typer.Option("--arc-id", "-a", help="所属剧情单元 ID（如 vol_01_arc_01）"),
    ] = None,
    in_world_time: Annotated[
        str | None,
        typer.Option("--time", "--in-world-time", "-t", help="书内时间日历坐标（如 天历三年秋）"),
    ] = None,
    macro_situation: Annotated[
        str | None,
        typer.Option("--macro-situation", "-m", help="更新 世界状态.yaml 全局天下大势简述"),
    ] = None,
    char_state: Annotated[
        list[str] | None,
        typer.Option("--char-state", "-C", help="更新活跃角色状态 JSON/YAML 片段"),
    ] = None,
    faction_state: Annotated[
        list[str] | None,
        typer.Option("--faction-state", "-G", help="更新活跃势力状态 JSON/YAML 片段"),
    ] = None,
    relation: Annotated[
        list[str] | None,
        typer.Option(
            "--relation",
            "-R",
            help="新增或更新双时态关系，格式 'source:target:relation[:from[:to]]'",
        ),
    ] = None,
    thread: Annotated[
        list[str] | None,
        typer.Option("--thread", "-T", help="新增或更新伏笔条目 JSON/YAML 片段"),
    ] = None,
    from_file: Annotated[
        Path | None,
        typer.Option("--from-file", "-f", help="从 JSON/YAML 结算文件批量导入本章状态与事实更新"),
    ] = None,
    book_dir: BookDirOption = None,
) -> None:
    """沉淀单章摘要与原子事实并更新状态截面（state update 的同义命令）。"""
    state_update_cmd(
        ctx=ctx,
        chapter=chapter,
        summary=summary,
        fact=fact,
        entity=entity,
        volume_id=volume_id,
        arc_id=arc_id,
        in_world_time=in_world_time,
        macro_situation=macro_situation,
        char_state=char_state,
        faction_state=faction_state,
        relation=relation,
        thread=thread,
        from_file=from_file,
        book_dir=book_dir,
    )


@state_app.command("inspect")
@_cli_guard
def state_inspect_cmd(
    ctx: typer.Context,
    entity_id: Annotated[
        str,
        typer.Argument(help="要检视的实体 ID 或名称（如 char_linfan）"),
    ],
    chapter: Annotated[
        int | None,
        typer.Option("--chapter", "-c", help="按指定章号查询当时生效的双时态关系边"),
    ] = None,
    book_dir: BookDirOption = None,
) -> None:
    """检视指定实体的设定概要、当前动态状态截面与生效双时态关系边。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    target = entity_id.strip()

    with SqliteIndexer(bdir) as indexer:
        indexer.sync()
        try:
            bundle = indexer.get_entity_bundle(target, at_chapter=chapter)
        except KeyError:
            row = indexer.conn.execute(
                """
                SELECT entity_id FROM entity_aliases
                WHERE alias = ?
                ORDER BY is_archived ASC, entity_id ASC
                LIMIT 1
                """,
                (target,),
            ).fetchone()
            if row is None:
                raise
            bundle = indexer.get_entity_bundle(str(row[0]), at_chapter=chapter)

    s_doc = bundle.setting
    state_yaml = (
        _dump_yaml(bundle.state.model_dump(mode="json", exclude_none=True)).strip()
        if bundle.state is not None
        else "（该实体无动态状态截面）"
    )
    arch_badge = "[yellow]冷库归档 (archived)[/]" if bundle.is_archived else "[green]热池活跃 (active)[/]"

    console.print(
        Panel(
            f"[bold]实体 ID:[/] [cyan]{s_doc.id}[/]    [bold]名称:[/] {s_doc.name}    "
            f"[bold]分类:[/] {s_doc.category.value}    [bold]状态:[/] {s_doc.status.value} ({arch_badge})\n"
            f"[bold]别名:[/] {'、'.join(s_doc.aliases) or '无'}\n"
            f"[bold cyan]核心设定速写:[/] {s_doc.core_text or '（空）'}\n\n"
            f"[bold yellow]当前动态状态截面:[/]\n{state_yaml}",
            title=f"实体全维状态检视 · {s_doc.name} ({s_doc.id})",
            border_style="cyan",
        )
    )

    rel_table = Table(
        title=f"有效双时态关系边 ({f'第 {chapter} 章截面' if chapter else '当前未失效'})",
        show_header=True,
        header_style="bold cyan",
    )
    rel_table.add_column("起点 (Source)", style="cyan")
    rel_table.add_column("终点 (Target)", style="cyan")
    rel_table.add_column("关系描述", style="bold")
    rel_table.add_column("生效区间 [valid_from, invalid_at)", justify="center")
    for rel in bundle.relations:
        rel_table.add_row(
            rel.source,
            rel.target,
            rel.relation,
            f"[{rel.valid_from_ch}, {rel.invalid_at_ch if rel.invalid_at_ch is not None else '∞'})",
        )
    console.print(rel_table)


@state_app.command(
    "gc",
    epilog=(
        "示例:\n"
        "  ddnovel state gc -c 80 -n        # 仅预览待移入 archive/ 冷库的角色/势力/关系/伏笔\n"
        "  ddnovel state gc -c 80 -y -a 30   # 确认执行归档（超过 30 章未出场转冷库）"
    ),
)
@_cli_guard
def state_gc_cmd(
    ctx: typer.Context,
    chapter: Annotated[
        int | None,
        typer.Option("--chapter", "-c", help="当前结算章号（默认取小说配置文件中的 current_chapter）"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="仅预览待移入 archive/ 冷库的条目，不修改磁盘文件"),
    ] = False,
    apply: Annotated[
        bool,
        typer.Option("--apply", "-y", help="确认执行冷库归档写盘并刷新索引"),
    ] = False,
    cold_after: Annotated[
        int | None,
        typer.Option("--cold-after", "-a", help="连续未出场章数超期归档阈值"),
    ] = None,
    book_dir: BookDirOption = None,
) -> None:
    """将已死亡/退场/超期未出场实体、已失效关系边与已填完伏笔移入 03_状态/归档/ 冷库。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    effective_dry_run = bool(dry_run and not apply)
    with LifecycleManager(bdir) as lm:
        rep = lm.cold_archive_gc(
            current_ch=chapter,
            dry_run=effective_dry_run,
            cold_after_chapters=cold_after,
        )

    mode_label = "预览模式 (dry-run)" if rep.dry_run else "已执行归档 (applied)"
    table = Table(
        title=f"状态冷归档报告 (基准第 {rep.current_chapter} 章 · {mode_label})",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("归档维度", style="bold")
    table.add_column("数量", justify="right", style="cyan")
    table.add_column("详情")
    table.add_row(
        "角色冷归档 (characters)",
        str(len(rep.archived_characters)),
        ", ".join(rep.archived_characters) or "-",
    )
    table.add_row(
        "势力冷归档 (factions)",
        str(len(rep.archived_factions)),
        ", ".join(rep.archived_factions) or "-",
    )
    table.add_row(
        "失效关系边归档 (relations)",
        str(rep.archived_relations_count),
        f"{rep.archived_relations_count} 条已失效关系边",
    )
    table.add_row(
        "完结伏笔归档 (threads)",
        str(len(rep.archived_threads)),
        ", ".join(rep.archived_threads) or "-",
    )
    table.add_row(
        "涉及状态文件",
        str(len(rep.modified_paths)),
        ", ".join(rep.modified_paths) or "-",
    )
    console.print(table)


# ---------------------------------------------------------------------------
# Group 9: 索引与检索 (index & search)
# ---------------------------------------------------------------------------


@index_app.command("sync")
@_cli_guard
def index_sync_cmd(
    ctx: typer.Context,
    book_dir: BookDirOption = None,
) -> None:
    """增量对比 SHA-256 文件哈希并热同步 .ddnovel/index.sqlite 派生索引。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    with SqliteIndexer(bdir) as indexer:
        rep = indexer.sync(force_rebuild=False)

    console.print(
        Panel(
            f"[bold green]增量索引同步完成[/] (耗时 [cyan]{rep.elapsed_ms:.2f} ms[/])\n"
            f"新增文件: [bold]{len(rep.added)}[/] | "
            f"更新文件: [bold]{len(rep.updated)}[/] | "
            f"级联清理已删文件: [bold]{len(rep.deleted)}[/]",
            title="索引增量同步 (index sync)",
            border_style="green",
        )
    )


@index_app.command("rebuild")
@_cli_guard
def index_rebuild_cmd(
    ctx: typer.Context,
    book_dir: BookDirOption = None,
) -> None:
    """清空并从四维磁盘真源执行零污染全量重建 .ddnovel/index.sqlite。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    with SqliteIndexer(bdir) as indexer:
        rep = indexer.sync(force_rebuild=True)

    console.print(
        Panel(
            f"[bold green]零污染全量索引重建完成[/] "
            f"(rebuilt={rep.rebuilt}, 耗时 [cyan]{rep.elapsed_ms:.2f} ms[/])\n"
            f"共扫描入库真源文件: [bold]{len(rep.added)}[/] 个",
            title="索引全量重建 (index rebuild)",
            border_style="green",
        )
    )


@app.command(
    "search",
    epilog="示例:\n  ddnovel search 林凡 -s setting -k 10\n  ddnovel search 密钥线索 -H",
)
@_cli_guard
def search_cmd(
    ctx: typer.Context,
    query: Annotated[
        str,
        typer.Argument(help="检索关键词（支持中文二字人名、单字、专有名词及复合词）"),
    ],
    scope: Annotated[
        str | None,
        typer.Option(
            "--scope",
            "-s",
            help="限定检索作用域 (setting | fact | summary | manuscript | blueprint)",
        ),
    ] = None,
    top_k: Annotated[
        int,
        typer.Option("--top-k", "-k", help="返回最大命中条数"),
    ] = 20,
    hybrid: Annotated[
        bool,
        typer.Option("--hybrid", "-H", help="若已启用向量插件则执行 BM25 + Cosine RRF 混合重排"),
    ] = False,
    book_dir: BookDirOption = None,
) -> None:
    """基于 CJK 双模 FTS5 全文索引（及可选混合向量 RRF）检索设定、事实、摘要、蓝图与正文。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    scope_enum = FtsScope(scope.strip().lower()) if scope is not None else None

    with SqliteIndexer(bdir) as indexer:
        indexer.sync()
        hits = indexer.search_fts(query, scope=scope_enum, top_k=top_k, hybrid=hybrid)

    table = Table(
        title=f"全文检索结果 · '{query}'（共命中 {len(hits)} 条）",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("文档切片 ID (doc_id)", style="cyan")
    table.add_column("作用域", style="bold")
    table.add_column("章号", justify="right")
    table.add_column("实体 ID")
    table.add_column("得分", justify="right")
    table.add_column("内容摘录")

    for hit in hits:
        table.add_row(
            hit.doc_id,
            hit.scope.value,
            str(hit.chapter_num) if hit.chapter_num > 0 else "-",
            hit.entity_id or "-",
            f"{hit.score:.3f}",
            hit.excerpt,
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Group 10: 遗忘与瘦身 (thread & history)
# ---------------------------------------------------------------------------


@thread_app.command(
    "drop",
    epilog=(
        "示例:\n"
        "  ddnovel thread drop thread_side_01 -n   # 预览前文引用\n"
        "  ddnovel thread drop thread_side_01      # 确认弃坑并从台账移除"
    ),
)
@_cli_guard
def thread_drop_cmd(
    ctx: typer.Context,
    thread_id: Annotated[
        str,
        typer.Argument(help="要主动弃坑移除的伏笔 ID（如 thread_opening_mystery）"),
    ],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="仅预检前文提及影响面，不实际从伏笔台账中删除"),
    ] = False,
    book_dir: BookDirOption = None,
) -> None:
    """主动弃坑：扫描前文提及提示并从伏笔台账物理移除指定伏笔，永不再触发雷达告警。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    with LifecycleManager(bdir) as lm:
        res = lm.drop_thread(thread_id, dry_run=dry_run)

    console.print(
        Panel(
            f"[bold green]弃坑伏笔 ID:[/] [cyan]{res.thread_id}[/] "
            f"({'预览模式 dry-run' if res.dry_run else '已物理移除'})\n"
            f"[bold]所在台账文件:[/] {res.removed_from}\n"
            f"[bold]前文/蓝图关联提及数:[/] {len(res.prior_mentions)} 处",
            title="主动弃坑物理移除 (thread drop)",
            border_style="green",
        )
    )
    if res.prior_mentions:
        table = Table(
            title="弃坑伏笔关联提及清单（供顺手圆场参考）",
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("作用域")
        table.add_column("章号", justify="right")
        table.add_column("位置", style="dim")
        table.add_column("摘录")
        for hit in res.prior_mentions:
            table.add_row(
                hit.scope.value,
                str(hit.chapter_num) if hit.chapter_num is not None else "-",
                hit.location,
                hit.excerpt,
            )
        console.print(table)


@thread_app.command("list")
@_cli_guard
def thread_list_cmd(
    ctx: typer.Context,
    status: Annotated[
        str | None,
        typer.Option("--status", "-s", help="按伏笔状态过滤 (planted | dormant | active | resolved)"),
    ] = None,
    chapter: Annotated[
        int | None,
        typer.Option("--chapter", "-c", help="仅显示在指定章号及之前已埋设的伏笔"),
    ] = None,
    book_dir: BookDirOption = None,
) -> None:
    """列出伏笔悬线台账。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    stat_enum = ThreadStatus(status.strip().lower()) if status is not None else None
    with SqliteIndexer(bdir) as indexer:
        indexer.sync()
        threads = indexer.get_threads(status=stat_enum, at_chapter=chapter)

    table = Table(title=f"伏笔悬线台账（共 {len(threads)} 条）", show_header=True, header_style="bold cyan")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("标题", style="bold")
    table.add_column("状态")
    table.add_column("埋设/推进章", justify="center")
    table.add_column("预计窗口", justify="center")
    table.add_column("触发实体")
    table.add_column("表面线索")
    for t in threads:
        win_str = f"[{t.expected_window.start}, {t.expected_window.end}]" if t.expected_window else "-"
        table.add_row(
            t.id,
            t.title,
            t.status.value,
            f"{t.planted_at} / {t.last_progressed_at}",
            win_str,
            ", ".join(t.trigger_entities) or "-",
            t.public_clue,
        )
    console.print(table)


@history_app.command(
    "compact",
    epilog="示例:\n  ddnovel history compact vol_01 -n\n  ddnovel history compact vol_01 -f milestones.jsonl",
)
@_cli_guard
def history_compact_cmd(
    ctx: typer.Context,
    volume_id: Annotated[
        str,
        typer.Argument(help="要蒸馏浓缩的完结分卷 ID（如 vol_01）"),
    ],
    milestone: Annotated[
        list[str] | None,
        typer.Option(
            "--milestone",
            "-m",
            help="显式传入的里程碑事实 JSON 行（可多次指定；不传则按密度算法自动蒸馏）",
        ),
    ] = None,
    from_file: Annotated[
        Path | None,
        typer.Option("--from-file", "-f", help="从 JSONL/YAML 文件加载自定义里程碑事实记录列表"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="仅预览浓缩前后条数对比，不覆写 jsonl 文件"),
    ] = False,
    book_dir: BookDirOption = None,
) -> None:
    """将已完结老卷的逐章细碎事实蒸馏浓缩为高密度卷级核心里程碑事实。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    milestones_list: list[ChapterFactRecord] | None = None

    if from_file is not None:
        src_path = Path(from_file).resolve()
        if not src_path.is_file():
            raise FileNotFoundError(f"未找到里程碑输入文件: {src_path}")
        raw_text = src_path.read_text(encoding="utf-8").strip()
        if src_path.suffix.lower() == ".jsonl":
            milestones_list = [
                ChapterFactRecord.model_validate_json(line)
                for line in raw_text.splitlines()
                if line.strip()
            ]
        else:
            loaded = yaml.safe_load(raw_text)
            if not isinstance(loaded, list):
                raise ValueError("里程碑文件顶层必须为列表")
            milestones_list = [ChapterFactRecord.model_validate(item) for item in loaded]
    elif milestone:
        milestones_list = [
            ChapterFactRecord.model_validate(yaml.safe_load(m))
            for m in milestone
            if m.strip()
        ]

    with LifecycleManager(bdir) as lm:
        res = lm.compact_volume_facts(
            volume_id,
            milestones=milestones_list,
            dry_run=dry_run,
        )

    console.print(
        Panel(
            f"[bold green]分卷事实蒸馏完成:[/] [cyan]{res.volume_id}[/] "
            f"({'预览模式 dry-run' if res.dry_run else '已写盘生效'})\n"
            f"[bold]事实台账路径:[/] {res.history_path}\n"
            f"[bold]记录条数变化:[/] 原始 [bold]{res.original_count}[/] 章记录 -> "
            f"浓缩后 [bold cyan]{res.compacted_count}[/] 条核心里程碑",
            title="远期老卷事实蒸馏浓缩 (history compact)",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# Group 11: 一致性体检 (audit)
# ---------------------------------------------------------------------------


def _render_audit_report(report: Any) -> None:
    scope_title = f"第 {report.chapter} 章" if report.chapter is not None else "全书"
    status_badge = (
        "[bold green]PASSED（通过）[/]"
        if report.passed
        else "[bold red]FAILED（存在阻断级错误 ERROR）[/]"
    )
    err_cnt = sum(1 for i in report.issues if i.level == IssueLevel.ERROR)
    warn_cnt = sum(1 for i in report.issues if i.level == IssueLevel.WARNING)
    info_cnt = sum(1 for i in report.issues if i.level == IssueLevel.INFO)

    console.print(
        Panel(
            f"[bold]体检范围:[/] {scope_title}    [bold]体检结论:[/] {status_badge}\n"
            f"[bold]问题统计:[/] ERROR={err_cnt} | WARNING={warn_cnt} | INFO={info_cnt}",
            title="静态一致性体检报告 (audit)",
            border_style="green" if report.passed else "red",
        )
    )

    if report.issues:
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("级别", style="bold", no_wrap=True)
        table.add_column("规则代码 (Code)", style="cyan", no_wrap=True)
        table.add_column("文件位置", style="dim")
        table.add_column("问题描述")
        level_styles = {
            IssueLevel.ERROR: "[bold red]ERROR[/]",
            IssueLevel.WARNING: "[bold yellow]WARNING[/]",
            IssueLevel.INFO: "[cyan]INFO[/]",
        }
        for iss in report.issues:
            table.add_row(
                level_styles.get(iss.level, iss.level.value),
                iss.code,
                iss.location or "-",
                iss.message,
            )
        console.print(table)


@app.command(
    "audit",
    epilog=(
        "示例:\n"
        "  ddnovel audit 10 -S   # 体检第 10 章（严格模式，有 WARNING 也返回退出码 1）\n"
        "  ddnovel audit         # 执行全书静态一致性体检"
    ),
)
@_cli_guard
def audit_cmd(
    ctx: typer.Context,
    chapter: Annotated[
        int | None,
        typer.Argument(help="要体检的章号（省略则执行全书静态一致性体检）"),
    ] = None,
    chapter_opt: Annotated[
        int | None,
        typer.Option("--chapter", "-c", help="要体检的章号（与位置参数二选一）"),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option("--strict", "-S", help="严格门禁：若体检未通过或存在告警则返回退出码 1"),
    ] = False,
    book_dir: BookDirOption = None,
) -> None:
    """静态规则一致性体检（检查蓝图失效引用、已故/冷库角色未唤醒出场、禁用词红线与扩展槽格式）。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    target_ch = chapter if chapter is not None else chapter_opt

    with LifecycleManager(bdir) as lm:
        report = lm.audit_chapter(chapter_num=target_ch)

    _render_audit_report(report)
    if not report.passed or (strict and len(report.issues) > 0):
        raise typer.Exit(code=1)


@app.command(
    "web",
    epilog=(
        "示例:\n"
        "  ddnovel web -d .                              # 启动本地 Web UI 看板并自动打开浏览器\n"
        "  ddnovel web -d . -p 9000 --no-open            # 指定端口启动服务但不自动弹窗\n"
        "  ddnovel web -d . --export-html ./看板.html    # 导出单文件离线交互式 HTML 看板"
    ),
)
@_cli_guard
def web_cmd(
    ctx: typer.Context,
    host: Annotated[
        str,
        typer.Option("--host", "-H", help="Web 服务监听地址"),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="Web 服务监听端口（传 0 自动分配空闲端口）"),
    ] = 8765,
    open_browser: Annotated[
        bool,
        typer.Option("--open/--no-open", help="启动后是否自动在默认浏览器中打开看板"),
    ] = True,
    export_html: Annotated[
        Path | None,
        typer.Option(
            "--export-html",
            "-o",
            help="不启动常驻服务，直接导出单文件离线交互式 HTML 看板并退出",
        ),
    ] = None,
    book_dir: BookDirOption = None,
) -> None:
    """启动本地 Web UI 可视化看板（或导出离线 HTML），查阅状态、悬线、人物与双时态关系网。"""
    bdir = _resolve_book_dir(book_dir, ctx)

    if export_html is not None:
        out_path = export_html if export_html.is_absolute() else (Path.cwd() / export_html).resolve()
        snap = build_dashboard_snapshot(bdir)
        html = render_standalone_html(snap, live_server=False)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")
        stats = snap["stats"]
        console.print(
            Panel(
                f"[bold green]导出模式:[/] 单文件离线交互式 HTML 看板\n"
                f"[bold]输出文件:[/] [bold cyan]{out_path}[/]\n"
                f"[bold]书库标题:[/] 《{snap['book']['title']}》 (第 {stats['effective_chapter']} 章)\n"
                f"[bold]数据摘要:[/] 角色 {stats['characters_total']} | "
                f"势力 {stats['factions_total']} | "
                f"关系 {stats['relations_total']} | "
                f"悬线 {stats['threads_total']}",
                title="Web UI 离线看板导出 (ddnovel web)",
                border_style="green",
            )
        )
        return

    def _announce_ready(url: str) -> None:
        console.print(
            Panel(
                f"[bold green]访问地址:[/] [bold cyan]{url}[/]\n"
                f"[bold]目标书库:[/] {bdir}\n"
                f"[dim]页面内点击「🔄 刷新数据」可实时读取最新文件状态；按 Ctrl+C 停止服务。[/]",
                title="DDNovel Web UI 状态看板已启动",
                border_style="green",
            )
        )

    serve_web_ui(
        bdir,
        host=host,
        port=port,
        open_browser=open_browser,
        on_ready=_announce_ready,
    )


# ---------------------------------------------------------------------------
# Group 12: DDNovelReader 作品包适配、单章资源规划与打包 (reader)
# ---------------------------------------------------------------------------


@reader_app.command(
    "plan",
    epilog=(
        "示例:\n"
        "  ddnovel reader plan 1 -d .              # 单独设计第 1 章并列出所需立绘/场景/CG 资源清单\n"
        "  ddnovel reader plan 1 -d . --save       # 生成并保存设计方案 Markdown 至 04_创作/阅读器作品包/设计方案/\n"
        "  ddnovel reader plan --all -d . --json   # 输出当前全部章节的结构化 JSON 资源清单"
    ),
)
@_cli_guard
def reader_plan_cmd(
    ctx: typer.Context,
    chapter: Annotated[
        int | None,
        typer.Argument(help="要单独设计的章号（省略时需指定 --all 规划全部章节）"),
    ] = None,
    all_chapters: Annotated[
        bool,
        typer.Option("--all", "-a", help="规划当前书库全部章节的伴读资源需求"),
    ] = False,
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", "-o", help="DDNovelReader 作品包输出目录（默认 04_创作/阅读器作品包/）"),
    ] = None,
    include_drafts: Annotated[
        bool,
        typer.Option("--include-drafts/--manuscript-only", help="当定稿尚未入库时是否允许读取草稿正文"),
    ] = True,
    save: Annotated[
        bool,
        typer.Option("--save", "-s", help="将伴读演出与资源需求清单写入 Markdown 设计文件"),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="以 JSON 格式输出结构化资源需求清单"),
    ] = False,
    book_dir: BookDirOption = None,
) -> None:
    """为单章（或全书）分析并提出适配 DDNovelReader 的视觉资源需求清单（立绘/差分/场景/CG/头像/封面）。"""
    if chapter is None and not all_chapters:
        raise ValueError("请指定章号（如 ddnovel reader plan 1），或传入 --all 规划当前全部章节")

    bdir = _resolve_book_dir(book_dir, ctx)
    exporter = ReaderExporter(bdir)
    if chapter is not None:
        plan = exporter.plan_chapter_resources(
            chapter,
            output_dir=output_dir,
            include_drafts=include_drafts,
            save_plan=save,
        )
        if as_json:
            typer.echo(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
            return

        payload = plan.to_dict()
        counts = payload["counts"]
        console.print(
            Panel(
                f"[bold cyan]{plan.title}[/] (`{plan.chapter_id}` / {plan.volume_id})\n"
                f"[bold]正文来源:[/] {plan.source_text_path} ({plan.word_count:,} 字)\n"
                f"[bold]资源统计:[/] 共 {counts['total']} 项 | "
                f"[green]已有可复用 {counts['ready']}[/] | "
                f"[yellow]已有基础待绘差分 {counts['reuse_base']}[/] | "
                f"[red]缺失待绘 {counts['missing']}[/]"
                + (f"\n[bold]已保存方案:[/] {plan.saved_plan_path}" if plan.saved_plan_path else ""),
                title="DDNovelReader 单章伴读设计与资源需求提案 (reader plan)",
                border_style="cyan",
            )
        )
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("类别", no_wrap=True)
        table.add_column("资源名称", style="bold")
        table.add_column("项目归档路径", style="cyan")
        table.add_column("作品包路径", style="dim")
        table.add_column("状态", no_wrap=True)
        table.add_column("建议技能", no_wrap=True)
        status_map = {
            "ready": "[green]已有[/]",
            "reuse_base": "[yellow]待差分[/]",
            "missing": "[red]缺失[/]",
        }
        for req in plan.resources:
            table.add_row(
                req.category,
                req.title,
                req.archive_target_path or "-",
                req.asset_path,
                status_map.get(req.status, req.status),
                req.recommended_skill or "-",
            )
        console.print(table)
        return

    plans = exporter.plan_book_resources(
        output_dir=output_dir,
        include_drafts=include_drafts,
        save_plan=save,
    )
    if as_json:
        typer.echo(json.dumps([p.to_dict() for p in plans], ensure_ascii=False, indent=2))
        return

    table = Table(title=f"全书伴读资源需求总览（共 {len(plans)} 章）", show_header=True, header_style="bold cyan")
    table.add_column("章号", justify="right")
    table.add_column("章节标题", style="bold")
    table.add_column("总资源数", justify="right")
    table.add_column("已有", justify="right", style="green")
    table.add_column("待差分", justify="right", style="yellow")
    table.add_column("缺失", justify="right", style="red")
    for p in plans:
        c = p.to_dict()["counts"]
        table.add_row(
            str(p.chapter),
            p.title,
            str(c["total"]),
            str(c["ready"]),
            str(c["reuse_base"]),
            str(c["missing"]),
        )
    console.print(table)


@reader_app.command(
    "export",
    epilog=(
        "示例:\n"
        "  ddnovel reader export -d .                                    # 导出当前全部定稿并打包 .ddbook\n"
        "  ddnovel reader export -d . --include-drafts                   # 包含草稿一并导出当前全部章节\n"
        "  ddnovel reader export -d . -c 1 --no-pack                     # 单独导出/更新第 1 章到源目录供热重载预览\n"
        "  ddnovel reader export -d . -c 2 --patch-from ./old.ddbook     # 增量更新第 2 章并生成 .ddpatch 补丁"
    ),
)
@_cli_guard
def reader_export_cmd(
    ctx: typer.Context,
    chapter: Annotated[
        int | None,
        typer.Option("--chapter", "-c", help="仅导出/增量更新指定单章（省略则导出当前全书）"),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", "-o", help="作品包输出目录（默认 04_创作/阅读器作品包/）"),
    ] = None,
    include_drafts: Annotated[
        bool,
        typer.Option("--include-drafts", help="当定稿缺失时自动回退读取草稿正文"),
    ] = False,
    pack: Annotated[
        bool,
        typer.Option("--pack/--no-pack", help="导出后是否自动封装合规 ZIP Store .ddbook 作品包"),
    ] = True,
    placeholders: Annotated[
        bool,
        typer.Option("--placeholders/--no-placeholders", help="是否为缺失的 .png 素材自动生成合法 RGBA 占位图"),
    ] = True,
    patch_from: Annotated[
        Path | None,
        typer.Option("--patch-from", help="传入旧版源目录或 .ddbook，同时生成增量补丁 .ddpatch"),
    ] = None,
    book_id: Annotated[
        str | None,
        typer.Option("--book-id", help="自定义 DDNovelReader book_id（默认按书名生成安全 ASCII ID）"),
    ] = None,
    revision: Annotated[
        int | None,
        typer.Option("--revision", help="指定 manifest.revision 版本号"),
    ] = None,
    accent_color: Annotated[
        str,
        typer.Option("--accent-color", help="作品 HUD 主题强调色 Hex（默认 #6CE5E8）"),
    ] = "#6CE5E8",
    volume_id: Annotated[
        str | None,
        typer.Option("--volume-id", "-v", help="全书模式下仅导出指定分卷（如 vol_01）"),
    ] = None,
    start: Annotated[
        int | None,
        typer.Option("--start", help="全书模式下起始章号（含）"),
    ] = None,
    end: Annotated[
        int | None,
        typer.Option("--end", help="全书模式下结束章号（含）"),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option("--strict/--no-strict", help="是否启用严格模式（将 warning 视为阻断）"),
    ] = True,
    reader_dir: Annotated[
        Path | None,
        typer.Option("--reader-dir", "-R", help="指定本机 DDNovelReader 仓库根目录（用于调用 ddnovel_tool.py）"),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="输出结构化 JSON 结果"),
    ] = False,
    book_dir: BookDirOption = None,
) -> None:
    """将当前全书或单独一章导出为适配 DDNovelReader 的源目录、`.ddbook` 与 `.ddpatch`。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    exporter = ReaderExporter(bdir)
    if chapter is not None:
        res = exporter.export_chapter(
            chapter,
            output_dir=output_dir,
            include_drafts=True if not include_drafts else include_drafts,
            pack_ddbook=pack,
            patch_from=patch_from,
            generate_placeholders=placeholders,
            strict_validate=strict,
            reader_dir=reader_dir,
            book_id=book_id,
            accent_color=accent_color,
        )
    else:
        res = exporter.export_book(
            output_dir=output_dir,
            include_drafts=include_drafts,
            pack_ddbook=pack,
            patch_from=patch_from,
            generate_placeholders=placeholders,
            strict_validate=strict,
            reader_dir=reader_dir,
            book_id=book_id,
            revision=revision,
            accent_color=accent_color,
            volume_id=volume_id,
            start_chapter=start,
            end_chapter=end,
        )

    if as_json:
        typer.echo(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
        return

    val = res.validation or {}
    val_ok = bool(val.get("ok", False))
    lines = [
        f"[bold]导出模式:[/] {'单章增量' if res.mode == 'chapter' else '全书打包'} | "
        f"[bold]book_id:[/] [cyan]{res.book_id}[/] (rev {res.revision})",
        f"[bold]导出章节:[/] {', '.join(str(c) for c in res.exported_chapters)}（共 {len(res.exported_chapters)} 章）",
        f"[bold]源目录（可热重载挂载）:[/] [bold cyan]{res.source_dir}[/]",
        f"[bold]素材统计:[/] 真实复用 {len(res.copied_assets)} | 自动占位图 {len(res.generated_placeholders)} | "
        f"待补绘 {len(res.missing_resources)}",
        f"[bold]协议校验:[/] {'[green]PASSED[/]' if val_ok else '[red]FAILED[/]'} "
        f"({res.reader_tool_used}: errors={val.get('errors', 0)}, warnings={val.get('warnings', 0)})",
    ]
    if res.ddbook_path:
        lines.append(f"[bold]已打包 .ddbook:[/] [bold green]{res.ddbook_path}[/]")
    if res.ddpatch_path:
        lines.append(f"[bold]已打包 .ddpatch:[/] [bold green]{res.ddpatch_path}[/]")
    console.print(
        Panel(
            "\n".join(lines),
            title="DDNovelReader 作品包导出完成 (reader export)",
            border_style="green" if val_ok else "yellow",
        )
    )


@reader_app.command(
    "validate",
    epilog="示例: ddnovel reader validate ./04_创作/阅读器作品包/source --strict",
)
@_cli_guard
def reader_validate_cmd(
    ctx: typer.Context,
    target: Annotated[
        Path | None,
        typer.Argument(help="待校验的源目录、.ddbook、.ddpatch 或 JSON（省略默认检查 04_创作/阅读器作品包/source）"),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option("--strict/--no-strict", help="严格模式：存在 warning 即返回非零退出码"),
    ] = True,
    target_type: Annotated[
        str | None,
        typer.Option("--type", help="强制指定目标类型 (book_dir|ddbook|ddpatch|manifest|chapter|gallery|codex|patch)"),
    ] = None,
    reader_dir: Annotated[
        Path | None,
        typer.Option("--reader-dir", "-R", help="指定本机 DDNovelReader 仓库根目录"),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="输出结构化 JSON 校验报告"),
    ] = False,
    book_dir: BookDirOption = None,
) -> None:
    """调用 DDNovelReader Protocol v1.0 校验器体检作品源目录或 `.ddbook` / `.ddpatch`。"""
    if target is None:
        bdir = _resolve_book_dir(book_dir, ctx)
        resolved_target = resolve_reader_pack_dir(bdir) / "source"
    else:
        resolved_target = target.resolve()

    report = validate_reader_target(
        resolved_target,
        strict=strict,
        target_type=target_type,
        reader_dir=reader_dir,
    )
    if as_json:
        typer.echo(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        console.print(
            Panel(
                f"[bold]目标:[/] {report.target} (`{report.target_kind}`)\n"
                f"[bold]校验器:[/] {report.tool}\n"
                f"[bold]结论:[/] {'[green]PASSED[/]' if report.ok else '[red]FAILED[/]'} "
                f"(errors={report.errors}, warnings={report.warnings}, infos={report.infos})",
                title="DDNovelReader 协议校验报告 (reader validate)",
                border_style="green" if report.ok else "red",
            )
        )
        if report.diagnostics:
            table = Table(show_header=True, header_style="bold cyan")
            table.add_column("级别", no_wrap=True)
            table.add_column("Code", style="cyan", no_wrap=True)
            table.add_column("文件/路径", style="dim")
            table.add_column("说明")
            for d in report.diagnostics[:30]:
                loc = f"{d.get('file', '')}:{d.get('path', '')}".strip(":")
                table.add_row(str(d.get("severity", "")), str(d.get("code", "")), loc, str(d.get("message", "")))
            console.print(table)

    if not report.ok:
        raise typer.Exit(code=1 if report.errors > 0 else 2)


@reader_app.command(
    "pack",
    epilog=(
        "示例:\n"
        "  ddnovel reader pack ./source -o ./my_book.ddbook\n"
        "  ddnovel reader pack ./source --patch-from ./old.ddbook -o ./v2.ddpatch"
    ),
)
@_cli_guard
def reader_pack_cmd(
    ctx: typer.Context,
    source_dir: Annotated[
        Path | None,
        typer.Argument(help="作品源目录（省略默认 04_创作/阅读器作品包/source）"),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="输出 .ddbook 或 .ddpatch 文件路径"),
    ] = None,
    patch_from: Annotated[
        Path | None,
        typer.Option("--patch-from", help="传入旧版源目录或 .ddbook，改为生成增量 .ddpatch"),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option("--strict/--no-strict", help="打包前是否执行严格预校验"),
    ] = True,
    reader_dir: Annotated[
        Path | None,
        typer.Option("--reader-dir", "-R", help="指定本机 DDNovelReader 仓库根目录"),
    ] = None,
    book_dir: BookDirOption = None,
) -> None:
    """将 DDNovelReader 源目录封装为合规 ZIP Store `.ddbook` 或增量 `.ddpatch`。"""
    bdir = _resolve_book_dir(book_dir, ctx) if source_dir is None else None
    default_root = resolve_reader_pack_dir(bdir) if bdir is not None else Path.cwd()
    src = source_dir.resolve() if source_dir is not None else (default_root / "source")

    if patch_from is not None:
        out_path = output.resolve() if output is not None else (src.parent / f"{src.parent.name}.ddpatch")
        packed, val = build_reader_patch(
            patch_from,
            src,
            out_path,
            auto_bump_revision=True,
            strict=strict,
            reader_dir=reader_dir,
        )
        kind = ".ddpatch"
    else:
        out_path = output.resolve() if output is not None else (src.parent / f"{src.parent.name}.ddbook")
        packed, val = pack_reader_book(
            src,
            out_path,
            strict=strict,
            reader_dir=reader_dir,
        )
        kind = ".ddbook"

    console.print(
        Panel(
            f"[bold green]已成功封装 {kind}:[/] [bold cyan]{packed.as_posix()}[/]\n"
            f"[bold]校验器:[/] {val.tool if val else '-'} (errors={val.errors if val else 0}, warnings={val.warnings if val else 0})",
            title="DDNovelReader 容器打包完成 (reader pack)",
            border_style="green",
        )
    )


@reader_app.command(
    "archive",
    epilog=(
        "示例:\n"
        "  ddnovel reader archive ./gen.png -d . -C sprite -e char_lin --expression alert\n"
        "  ddnovel reader archive ./bg.png -d . -C background -e loc_berth --label 雨夜栈桥\n"
        "  ddnovel reader archive -d . --sync-from-source   # 将作品包 source/assets/ 中全部真实图片反向归档至 04_创作/插画/"
    ),
)
@_cli_guard
def reader_archive_cmd(
    ctx: typer.Context,
    source_file: Annotated[
        Path | None,
        typer.Argument(help="待归档的源图片/媒体文件路径（使用 --sync-from-source 时可省略）"),
    ] = None,
    category: Annotated[
        str | None,
        typer.Option(
            "--category",
            "-C",
            help="资产分类 (sprite | portrait | background | cg | cover | banner | video | rig)",
        ),
    ] = None,
    entity_id: Annotated[
        str | None,
        typer.Option("--entity-id", "-e", help="关联实体 ID（如 char_lin / loc_berth）"),
    ] = None,
    chapter: Annotated[
        int | None,
        typer.Option("--chapter", "-c", help="关联章号（CG 或单章背景）"),
    ] = None,
    volume_id: Annotated[
        str | None,
        typer.Option("--volume-id", "-v", help="所属分卷 ID（如 vol_01）"),
    ] = None,
    outfit: Annotated[
        str,
        typer.Option("--outfit", help="立绘服装名称（默认 常服，如 礼服 / 居家服 / 制服 / 作战服）"),
    ] = "常服",
    pose: Annotated[
        str,
        typer.Option("--pose", help="可选动作差分姿态标识（如 lean_close / hold_item / turn_back / sit_relax）"),
    ] = "",
    expression: Annotated[
        str,
        typer.Option("--expression", help="立绘表情名（如 neutral / smile / blush / tease / alert / tender）"),
    ] = "neutral",
    label: Annotated[
        str,
        typer.Option("--label", "-l", help="场景短名或素材标签"),
    ] = "",
    reader_asset_path: Annotated[
        str | None,
        typer.Option("--reader-asset-path", help="显式指定作品包内相对路径（如 assets/sprites/char_lin_default_alert.png）"),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", "-o", help="作品包目录（默认 04_创作/阅读器作品包/）"),
    ] = None,
    sync_source: Annotated[
        bool,
        typer.Option("--sync-source/--no-sync-source", help="归档后是否同步复制进作品包 source/assets/"),
    ] = True,
    sync_from_source: Annotated[
        bool,
        typer.Option("--sync-from-source", help="扫描作品包 source/assets/ 并将全部非占位真实资产反向归档至 04_创作/插画/"),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="输出结构化 JSON 结果"),
    ] = False,
    book_dir: BookDirOption = None,
) -> None:
    """将生图/设计过程中产生的图片与媒体资产规范归档至小说项目 `04_创作/插画/` 并维护 `资产清单.yaml`。"""
    bdir = _resolve_book_dir(book_dir, ctx)
    exporter = ReaderExporter(bdir)

    if sync_from_source:
        sync_res = exporter.sync_assets_to_archive(output_dir=output_dir)
        if as_json:
            typer.echo(json.dumps(sync_res.to_dict(), ensure_ascii=False, indent=2))
            return
        console.print(
            Panel(
                f"[bold]扫描源目录:[/] {sync_res.source_dir}\n"
                f"[bold green]新增归档:[/] {len(sync_res.archived)} 项 | "
                f"[bold]已有跳过:[/] {len(sync_res.already_archived)} 项 | "
                f"[dim]忽略占位图:[/] {len(sync_res.skipped_placeholders)} 项\n"
                f"[bold]资产台账:[/] [cyan]{sync_res.catalog_path}[/]",
                title="项目资产反向归档完成 (reader archive --sync-from-source)",
                border_style="green",
            )
        )
        return

    if source_file is None:
        raise ValueError("请提供待归档文件路径，或使用 --sync-from-source 扫描作品包反向归档")
    if not category:
        raise ValueError("单项归档必须指定 --category / -C (sprite|portrait|background|cg|cover|banner|video|rig)")

    res = exporter.archive_asset(
        source_file,
        category=category,
        entity_id=entity_id,
        chapter=chapter,
        volume_id=volume_id,
        outfit=outfit,
        pose=pose,
        expression=expression,
        label=label,
        reader_asset_path=reader_asset_path,
        output_dir=output_dir,
        sync_to_reader_source=sync_source,
    )
    if as_json:
        typer.echo(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
        return
    console.print(
        Panel(
            f"[bold]资产分类:[/] [cyan]{res.category}[/]\n"
            f"[bold green]项目归档路径:[/] [bold cyan]{res.archive_rel_path}[/]\n"
            f"[bold]作品包映射:[/] {res.reader_asset_path or '-'}\n"
            f"[bold]同步入包:[/] {res.synced_to_source_path or '未同步'}\n"
            f"[bold]资产台账:[/] {res.catalog_path}",
            title="项目视觉资产归档完成 (reader archive)",
            border_style="green",
        )
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
