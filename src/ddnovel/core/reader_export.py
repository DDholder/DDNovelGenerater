"""DDNovelReader (.ddbook / .ddpatch Protocol v1.0) 适配桥接、单章资源规划与打包引擎。

核心能力：
1. 自动发现本机 DDNovelReader 仓库（支持显式路径、`DDNOVEL_READER_DIR` 环境变量或同级目录探测），
   复用其 `tools/ddnovel_tool.py` 严格校验器与 `scripts/ddnovel-mcp.ts` MCP 入口，并在未安装时提供零依赖兜底；
2. 单章伴读导演与视觉资源需求提案 (`plan_chapter_resources` / `plan_book_resources`)：
   读取指定章节正文、蓝图、非机密设定与 `04_创作/插画/` 已有素材，提出所需角色立绘/表情差分、
   角色微徽章、场景背景图、典藏剧情 CG 与封面/横幅清单（明确区分已有可复用与缺失待绘）；
3. 全书一键适配导出 (`export_book`) 与单章增量导出/精修 (`export_chapter`)：
   严格遵守 DDNovelReader Protocol v1.0 五条底层红线（零 null `manifest.json`/`patch.json`、
   安全 ID、POSIX 相对路径、5 处角色 ID 统一、防剧透水位线 `codex/entities.json`、
   全局唯一 `unlock_gallery_id` 与无压缩 ZIP Store Method 0 `.ddbook` / `.ddpatch` 封装）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

import yaml

from ddnovel.core.indexer import SqliteIndexer
from ddnovel.core.parser import ProgressiveParser, split_frontmatter
from ddnovel.core.paths import (
    ZH_CHARACTER_AVATARS_SUBDIR,
    ZH_CHARACTER_PORTRAITS_SUBDIR,
    ZH_COVER_BANNER_SUBDIR,
    ZH_SCENE_BACKGROUNDS_SUBDIR,
    ZH_SCENE_CGS_SUBDIR,
    find_existing_blueprint_file,
    format_volume_stem,
    infer_volume_id_from_stem,
    list_facts_files,
    resolve_asset_catalog_file,
    resolve_character_avatars_dir,
    resolve_character_portraits_dir,
    resolve_cover_banner_dir,
    resolve_drafts_dir,
    resolve_dynamic_media_dir,
    resolve_illustrations_dir,
    resolve_manuscript_dir,
    resolve_master_outline,
    resolve_reader_pack_dir,
    resolve_relations_file,
    resolve_scene_backgrounds_dir,
    resolve_scene_cgs_dir,
    resolve_volumes_dir,
    sanitize_filename_label,
)
from ddnovel.core.schemas import (
    BlueprintDoc,
    ChapterFactRecord,
    NovelConfig,
    RelationsFile,
    SettingCategory,
    SettingDoc,
    TemporalRelation,
    VolumeDoc,
)

DDNOVEL_READER_DIR_ENV_VAR: Final[str] = "DDNOVEL_READER_DIR"
PROTOCOL_VERSION: Final[str] = "1.0"
SCHEMA_VERSION: Final[str] = "1.0"

_IMAGE_EXTS: Final[frozenset[str]] = frozenset(
    {".png", ".webp", ".avif", ".jpg", ".jpeg", ".gif", ".svg"}
)
_MEDIA_EXTS: Final[frozenset[str]] = _IMAGE_EXTS | frozenset(
    {".mp4", ".m4v", ".webm", ".json", ".atlas", ".moc3"}
)
_PNG_PLACEHOLDER_KEY: Final[bytes] = b"DDNovelPlaceholder"
_IGNORED_PACK_NAMES: Final[frozenset[str]] = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})

_SAFE_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9_-]+")
_CHAPTER_NUM_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:ch_?0*(\d+)|第\s*0*(\d+)\s*[章回节])",
    re.IGNORECASE,
)
_CHAPTER_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*#{1,6}\s*(.+?)\s*$"
)
_SCENE_DIVIDER_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:[-*_—─═]{3,}|——\s*.{1,30}\s*——|\*\s*\*\s*\*)\s*$"
)
_CARD_OPEN_RE: Final[re.Pattern[str]] = re.compile(
    r"^:::\s*(system_hud|parchment_letter|terminal_log|stone_tablet)(?:\[(.*?)\])?\s*$"
)
_CARD_CLOSE_RE: Final[re.Pattern[str]] = re.compile(r"^:::\s*$")
_MARKDOWN_INLINE_RE: Final[re.Pattern[str]] = re.compile(
    r"\*\*(.+?)\*\*|__(.+?)__|`([^`\n]+)`|</?(?:p|div|span|br|b|i|strong|em|h[1-6]|ul|ol|li)\b[^>]*>",
    re.IGNORECASE,
)
_EXPLICIT_DIALOGUE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*([^\s:：，。！？「」『』“”\"']{1,16})\s*[:：]\s*([「『“\"].+[」』”\"])\s*$"
)
_PURE_QUOTE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:「[^」]+」|『[^』]+』|“[^”]+”|\"[^\"]+\")\s*$"
)

_CHARACTER_ACCENT_PALETTE: Final[tuple[str, ...]] = (
    "#6CE5E8",
    "#F5C451",
    "#A78BFA",
    "#34D399",
    "#F472B6",
    "#60A5FA",
    "#FB923C",
    "#E879F9",
)

_CATEGORY_TO_CODEX_TYPE: Final[dict[SettingCategory, str]] = {
    SettingCategory.CHARACTER: "character",
    SettingCategory.FACTION: "faction",
    SettingCategory.GEOGRAPHY: "location",
    SettingCategory.ITEM: "item",
    SettingCategory.WORLD: "lore",
    SettingCategory.RULE: "lore",
    SettingCategory.OTHER: "lore",
}

_CODEX_TYPE_DEFAULT_COLOR: Final[dict[str, str]] = {
    "character": "#6CE5E8",
    "faction": "#F5C451",
    "location": "#8BA4B8",
    "item": "#34D399",
    "lore": "#A78BFA",
}

_EXPRESSION_RULES: Final[tuple[tuple[str, str, re.Pattern[str]], ...]] = (
    ("blush", "脸红娇羞", re.compile(r"脸红|泛红|羞涩|羞怯|耳根发红|红着脸|娇羞|微红|不好意思|羞恼|心跳加速")),
    ("tease", "狡黠调皮", re.compile(r"狡黠|俏皮|调侃|戏谑|挑眉|眨眼|坏笑|得意|偷笑")),
    ("surprise", "惊愕惊讶", re.compile(r"惊讶|惊愕|怔住|瞪大|一愣|吃惊|愕然|不可置信|愣住")),
    ("tender", "温柔含情", re.compile(r"温柔|轻柔|柔声|深情|宠溺|怜惜|眷恋|柔情|含情")),
    ("alert", "警觉", re.compile(r"警觉|警惕|戒备|瞳孔|不好|小心|(?<![别不])紧张|拔剑|突袭|危机|喝止")),
    ("smile", "微笑", re.compile(r"微笑|轻笑|笑道|笑意|勾唇|嘴角|温和|欣慰|开心")),
    ("angry", "愤怒", re.compile(r"怒道|冷哼|厉声|咬牙|怒喝|寒声|怒意|斥道|怒目|冷笑")),
    ("sad", "低落", re.compile(r"叹息|垂眸|哽咽|苦笑|黯然|悲伤|泪|低沉|惋惜|失落|委屈")),
    ("determined", "坚定", re.compile(r"沉声|坚定|决意|肃然|凝声|斩钉截铁|目光如炬|正色")),
)

_POSE_RULES: Final[tuple[tuple[str, str, re.Pattern[str]], ...]] = (
    ("lean_close", "倾身靠近", re.compile(r"俯身|凑近|贴近|倾身|靠过来|拉近距离|探身")),
    ("hold_item", "持物执伞", re.compile(r"执伞|撑伞|手持|握着|递出|捧着|举起|提灯")),
    ("turn_back", "回眸转身", re.compile(r"回眸|回头|转身|侧首|望向身后|回首")),
    ("combat_stance", "备战持刃", re.compile(r"拔剑|举枪|架势|摆开|蓄势|亮出武器|戒备姿态")),
    ("sit_relax", "坐姿休憩", re.compile(r"坐下|靠在|倚着|坐在|蜷坐|托腮")),
)

_OUTFIT_RULES: Final[tuple[tuple[str, str, re.Pattern[str]], ...]] = (
    ("formal", "礼服", re.compile(r"礼服|晚礼服|舞裙|华服|盛装|正装")),
    ("homewear", "居家服", re.compile(r"睡衣|居家服|浴袍|家居服|宽松衬衫|寝衣")),
    ("uniform", "制服", re.compile(r"制服|校服|院服|学园装|官服|军装")),
    ("combat", "作战服", re.compile(r"作战服|战斗服|战甲|劲装|武装|战术服")),
    ("cloak", "斗篷装", re.compile(r"斗篷|雨衣|风衣|披风|大衣")),
    ("damaged", "战损装", re.compile(r"战损|衣衫破损|衣襟凌乱|沾血|破碎衣袖")),
)

_WEATHER_RULES: Final[tuple[tuple[str, float, str, re.Pattern[str]], ...]] = (
    ("rain", 0.65, "雨幕氛围", re.compile(r"暴雨|细雨|雨夜|冷雨|大雨|雨丝|雷雨|淋雨")),
    ("snow", 0.6, "飘雪氛围", re.compile(r"大雪|风雪|飘雪|雪夜|落雪|暴雪|积雪")),
    ("embers", 0.65, "余烬火星", re.compile(r"烈焰|余烬|火海|燃烧|火星|大火|炙焰|焚毁")),
    ("fog", 0.55, "薄雾氛围", re.compile(r"迷雾|浓雾|雾气|灰雾|薄雾|瘴气|云雾")),
    ("memory_sepia", 0.5, "回忆滤镜", re.compile(r"回忆|多年前|旧事|往昔|梦境之中")),
)

_INTIMACY_CUE_RE: Final[re.Pattern[str]] = re.compile(
    r"贴贴|依偎|靠在肩|牵手|十指|拥抱|抱住|耳语|靠近|贴近|拂去|擦拭|共伞|并肩|吻|轻抚|怀里|靠过来|拉住手|指尖相触|环住"
)
_CHARM_CUE_RE: Final[re.Pattern[str]] = re.compile(
    r"回眸|逆光|发丝|眼眸|裙摆|修长|长腿|丝袜|锁骨|笑靥|微风|窗边|月光下|灯光下|换上|亮相|侧颜|红唇|风姿|惊艳"
)
_EMOTION_CUE_RE: Final[re.Pattern[str]] = re.compile(
    r"脸红|心跳|含泪|微笑|哽咽|柔声|凝视|注视|心软|愧疚|安心|告白|誓言|温柔|羞怯|眼眶|低语|倾诉"
)
_CLIMAX_CUE_RE: Final[re.Pattern[str]] = re.compile(
    r"决战|觉醒|真相|揭示|崩塌|破晓|爆发|对峙|重逢|告别|高潮|名场面|剑光|轰鸣|震颤|共鸣|撕裂"
)

_CARD_HINT_RULES: Final[tuple[tuple[str, str, re.Pattern[str]], ...]] = (
    ("parchment_letter", "信件/密函", re.compile(r"信封|密信|手书|信笺|来信|亲启|羊皮纸")),
    ("stone_tablet", "碑文/古铭", re.compile(r"石碑|铭文|古碑|刻字|碑文|壁画")),
    ("terminal_log", "终端/日志", re.compile(r"日志|终端|记录仪|档案编号|系统记录")),
    ("system_hud", "系统/状态面板", re.compile(r"系统提示|面板|遥测|警告|任务触发|属性")),
)

_FILENAME_EXPR_HINTS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("blush", re.compile(r"blush|shy|脸红|害羞|娇羞", re.IGNORECASE)),
    ("tease", re.compile(r"tease|smirk|俏皮|狡黠|调皮", re.IGNORECASE)),
    ("surprise", re.compile(r"surprise|shock|惊讶|惊愕", re.IGNORECASE)),
    ("tender", re.compile(r"tender|gentle|温柔|含情", re.IGNORECASE)),
    ("alert", re.compile(r"alert|警觉|戒备|战斗|战损", re.IGNORECASE)),
    ("smile", re.compile(r"smile|happy|微笑|笑|喜", re.IGNORECASE)),
    ("angry", re.compile(r"angry|glare|愤怒|怒|冷冽", re.IGNORECASE)),
    ("sad", re.compile(r"sad|cry|悲伤|低落|泪|委屈", re.IGNORECASE)),
    ("determined", re.compile(r"determined|serious|坚定|认真|肃然", re.IGNORECASE)),
    ("neutral", re.compile(r"neutral|calm|立绘|常服|平静|默认", re.IGNORECASE)),
)


# ---------------------------------------------------------------------------
# 1. DDNovelReader 路径发现与外部工具定位
# ---------------------------------------------------------------------------


def discover_reader_dir(explicit: Path | str | None = None) -> Path | None:
    """定位本机 DDNovelReader 仓库根目录（优先级：显式参数 > 环境变量 > 同级仓库探测）。"""
    candidates: list[Path] = []
    if explicit is not None and str(explicit).strip():
        candidates.append(Path(explicit).expanduser())
    env_val = os.environ.get(DDNOVEL_READER_DIR_ENV_VAR, "").strip()
    if env_val:
        candidates.append(Path(env_val).expanduser())

    # 引擎源码位于 <workspace>/DDNovelGenerater/src/ddnovel/core/reader_export.py
    repo_root = Path(__file__).resolve().parents[3]
    candidates.append(repo_root.parent / "DDNovelReader")
    candidates.append(Path.cwd().resolve().parent / "DDNovelReader")

    for cand in candidates:
        try:
            resolved = cand.resolve()
        except OSError:
            continue
        if (resolved / "tools" / "ddnovel_tool.py").is_file() or (
            resolved / "scripts" / "ddnovel-mcp.ts"
        ).is_file():
            return resolved
    return None


def resolve_reader_tool_script(reader_dir: Path | str | None = None) -> Path | None:
    """返回 DDNovelReader 的零依赖 Python 校验/打包脚本路径 `tools/ddnovel_tool.py`。"""
    root = discover_reader_dir(reader_dir)
    if root is None:
        return None
    script = root / "tools" / "ddnovel_tool.py"
    return script if script.is_file() else None


def resolve_reader_mcp_script(reader_dir: Path | str | None = None) -> Path | None:
    """返回 DDNovelReader 的 stdio MCP 入口脚本路径 `scripts/ddnovel-mcp.ts`。"""
    root = discover_reader_dir(reader_dir)
    if root is None:
        return None
    script = root / "scripts" / "ddnovel-mcp.ts"
    return script if script.is_file() else None


def resolve_reader_mcp_command(reader_dir: Path | str | None = None) -> tuple[str, ...] | None:
    """构建 DDNovelReader stdio MCP 服务启动命令 `("node", "<reader_dir>/scripts/ddnovel-mcp.ts")`."""
    script = resolve_reader_mcp_script(reader_dir)
    if script is None:
        return None
    return ("node", script.as_posix())


# ---------------------------------------------------------------------------
# 2. 数据结构模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReaderResourceRequirement:
    """单条 DDNovelReader 视觉资源需求描述。"""

    asset_path: str
    category: Literal["cover", "banner", "background", "sprite", "portrait", "cg"]
    title: str
    status: Literal["ready", "reuse_base", "missing"]
    recommended_size: str
    reason: str
    entity_id: str | None = None
    expression: str | None = None
    outfit: str | None = None
    pose: str | None = None
    cg_kind: str | None = None
    unlock_gallery_id: str | None = None
    existing_source_path: str | None = None
    archive_target_path: str | None = None
    recommended_skill: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ArchivedAssetResult:
    """单条视觉/媒体资产归档至项目 `04_创作/插画/` 目录的交付摘要。"""

    category: str
    archive_path: str
    archive_rel_path: str
    reader_asset_path: str | None
    synced_to_source_path: str | None
    catalog_path: str
    entity_id: str | None = None
    chapter: int | None = None
    outfit: str | None = None
    pose: str | None = None
    expression: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SyncArchiveResult:
    """反向巡检 `source/assets/` 并归档至项目 `04_创作/插画/` 的结果摘要。"""

    source_dir: str
    archived: list[dict[str, Any]]
    skipped_placeholders: list[str]
    already_archived: list[str]
    catalog_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ChapterCastProfile:
    """单章出场角色统计与差分线索。"""

    entity_id: str
    name: str
    aliases: list[str]
    accent_color: str
    role: str
    dialogue_count: int
    detected_expressions: list[str]
    existing_portraits: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ChapterResourcePlan:
    """单章 DDNovelReader 伴读演出构想与视觉资源需求清单。"""

    chapter: int
    chapter_id: str
    volume_id: str
    title: str
    source_text_path: str
    word_count: int
    cast: list[ChapterCastProfile]
    resources: list[ReaderResourceRequirement]
    stage_outline: dict[str, Any]
    summary_markdown: str
    saved_plan_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter": self.chapter,
            "chapter_id": self.chapter_id,
            "volume_id": self.volume_id,
            "title": self.title,
            "source_text_path": self.source_text_path,
            "word_count": self.word_count,
            "cast": [c.to_dict() for c in self.cast],
            "resources": [r.to_dict() for r in self.resources],
            "stage_outline": self.stage_outline,
            "summary_markdown": self.summary_markdown,
            "saved_plan_path": self.saved_plan_path,
            "counts": {
                "total": len(self.resources),
                "ready": sum(1 for r in self.resources if r.status == "ready"),
                "reuse_base": sum(1 for r in self.resources if r.status == "reuse_base"),
                "missing": sum(1 for r in self.resources if r.status == "missing"),
            },
        }


@dataclass(frozen=True)
class ReaderValidationResult:
    """DDNovelReader Protocol v1.0 校验报告。"""

    ok: bool
    strict: bool
    tool: str
    target: str
    target_kind: str
    errors: int
    warnings: int
    infos: int
    summary: dict[str, Any]
    diagnostics: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReaderExportResult:
    """全书或单章 DDNovelReader 作品包导出与打包结果。"""

    mode: Literal["full", "chapter"]
    book_id: str
    revision: int
    source_dir: str
    ddbook_path: str | None
    ddpatch_path: str | None
    exported_chapters: list[int]
    copied_assets: list[str]
    generated_placeholders: list[str]
    missing_resources: list[dict[str, Any]]
    ready_resources: list[dict[str, Any]]
    validation: dict[str, Any] | None
    reader_tool_used: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _ChapterSource:
    chapter: int
    chapter_id: str
    volume_id: str
    title: str
    text: str
    source_path: Path
    is_draft: bool
    blueprint: BlueprintDoc | None
    summary: str | None


# ---------------------------------------------------------------------------
# 3. 通用清洗、零 null 与纯 Python 占位 PNG 生成工具
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_ascii_id(raw: str, *, prefix: str = "id") -> str:
    cleaned = _SAFE_SLUG_RE.sub("_", raw.strip().lower()).strip("_-")
    if cleaned and cleaned[0].isalnum():
        return cleaned[:48]
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{digest}"


def _strip_none_recursive(obj: Any) -> Any:
    """递归移除 dict 中的 None 值（确保 manifest.json / patch.json / gallery / codex 零 null）。"""
    if isinstance(obj, dict):
        return {k: _strip_none_recursive(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_none_recursive(v) for v in obj if v is not None]
    return obj


def _write_json_strict(path: Path, data: Any, *, allow_stage_patch_nulls: bool = False) -> None:
    payload = data if allow_stage_patch_nulls else _strip_none_recursive(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _strip_markdown_plain(text: str) -> str:
    """剥离 Markdown 强调/行内代码/HTML 标签，输出 DDNovelReader 纯文本段落。"""
    def _repl(match: re.Match[str]) -> str:
        for grp in match.groups():
            if grp is not None:
                return grp
        return ""

    out = _MARKDOWN_INLINE_RE.sub(_repl, text)
    out = re.sub(r"^[ \t]*#{1,6}[ \t]+", "", out, flags=re.MULTILINE)
    out = re.sub(r"^[ \t]*>[ \t]?", "", out, flags=re.MULTILINE)
    return out.strip()


def _parse_hex_rgb(hex_color: str) -> tuple[int, int, int]:
    cleaned = hex_color.strip().lstrip("#")
    if len(cleaned) == 3:
        cleaned = "".join(ch * 2 for ch in cleaned)
    if len(cleaned) >= 6:
        try:
            return int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16)
        except ValueError:
            pass
    return (108, 229, 232)


def _encode_png_rgba(
    width: int,
    height: int,
    rgba_rows: Sequence[bytes],
    *,
    is_placeholder: bool = False,
) -> bytes:
    """零依赖编码合法 8-bit RGBA PNG 文件字节流（可选写入占位图标识 `tEXt` 块）。"""
    def _chunk(chunk_type: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", crc)

    raw_scanlines = b"".join(b"\x00" + row for row in rgba_rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    meta_chunk = _chunk(b"tEXt", _PNG_PLACEHOLDER_KEY + b"\x00true") if is_placeholder else b""
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + meta_chunk
        + _chunk(b"IDAT", zlib.compress(raw_scanlines, level=6))
        + _chunk(b"IEND", b"")
    )


def is_placeholder_png(path: Path | str) -> bool:
    """判断指定文件是否为 DDNovelGenerater / DDNovelReader 自动生成的占位 PNG。"""
    file_path = Path(path)
    if not file_path.is_file() or file_path.suffix.lower() != ".png":
        return False
    try:
        data = file_path.read_bytes()
    except OSError:
        return False
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    pos = 8
    while pos + 12 <= len(data):
        length = struct.unpack_from(">I", data, pos)[0]
        ctype = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        if ctype == b"tEXt" and payload.startswith(_PNG_PLACEHOLDER_KEY + b"\x00"):
            return True
        if ctype == b"IEND":
            break
        pos += 12 + length
    return False


def _generate_placeholder_png(asset_path: str, accent_color: str = "#6CE5E8") -> bytes:
    """根据资源相对路径类别生成轻量合法 RGBA PNG 占位图。

    - `assets/sprites/*`: 透明底、底部贴边 `(0.5, 1.0)` 的角色剪影立绘；
    - `assets/portraits/*`: 1:1 圆形徽章头像；
    - `assets/cover*`: 3:4 封面渐变压纹；
    - `assets/bg/*` / `assets/cgs/*` / `assets/banner*`: 16:9 宽屏渐变图。
    """
    ar, ag, ab = _parse_hex_rgb(accent_color)
    low = asset_path.lower()

    if low.startswith("assets/sprites/"):
        w, h = 160, 250
        rows: list[bytes] = []
        cx = w / 2.0
        head_cy = 56.0
        head_r2 = 26.0 * 26.0
        for y in range(h):
            row = bytearray(w * 4)
            for x in range(w):
                dx = x - cx
                in_head = (dx * dx + (y - head_cy) ** 2) <= head_r2
                in_neck = 78 <= y <= 94 and abs(dx) <= 10
                # 梯形躯干与裙摆/腿部轮廓，严格贴合画布底边 y = h - 1
                if 92 <= y < h:
                    t = (y - 92) / max(1.0, (h - 92))
                    half_w = 24.0 + 22.0 * (1.0 - abs(t - 0.35))
                    in_body = abs(dx) <= half_w
                else:
                    in_body = False
                if in_head or in_neck or in_body:
                    # 柔和垂直明暗渐变
                    shade = 0.65 + 0.35 * (1.0 - y / h)
                    idx = x * 4
                    row[idx] = min(255, int(ar * shade + 35))
                    row[idx + 1] = min(255, int(ag * shade + 35))
                    row[idx + 2] = min(255, int(ab * shade + 40))
                    row[idx + 3] = 240
            rows.append(bytes(row))
        return _encode_png_rgba(w, h, rows, is_placeholder=True)

    if low.startswith("assets/portraits/"):
        w, h = 96, 96
        cx, cy = w / 2.0, h / 2.0
        r_outer2 = 44.0 * 44.0
        r_inner2 = 39.0 * 39.0
        rows = []
        for y in range(h):
            row = bytearray(w * 4)
            for x in range(w):
                d2 = (x - cx) ** 2 + (y - cy) ** 2
                idx = x * 4
                if d2 <= r_inner2:
                    t = y / h
                    row[idx] = int(24 + ar * 0.35 * (1.0 - t * 0.4))
                    row[idx + 1] = int(30 + ag * 0.35 * (1.0 - t * 0.4))
                    row[idx + 2] = int(42 + ab * 0.35 * (1.0 - t * 0.4))
                    row[idx + 3] = 255
                elif d2 <= r_outer2:
                    row[idx], row[idx + 1], row[idx + 2], row[idx + 3] = ar, ag, ab, 255
            rows.append(bytes(row))
        return _encode_png_rgba(w, h, rows, is_placeholder=True)

    if "cover" in Path(low).stem:
        w, h = 180, 240
    else:
        w, h = 256, 144

    is_cg = low.startswith("assets/cgs/")
    rows = []
    for y in range(h):
        t = y / max(1, h - 1)
        row = bytearray(w * 4)
        border_y = y < 3 or y >= h - 3
        for x in range(w):
            border = border_y or x < 3 or x >= w - 3
            idx = x * 4
            if border:
                row[idx], row[idx + 1], row[idx + 2], row[idx + 3] = ar, ag, ab, 255
            else:
                base_r = 28 if is_cg else 18
                base_g = 24 if is_cg else 24
                base_b = 42 if is_cg else 38
                glow = 0.28 * (1.0 - abs(x / max(1, w - 1) - 0.5) * 1.4) * (1.0 - t * 0.6)
                glow = max(0.0, glow)
                row[idx] = min(255, int(base_r + ar * glow + 18 * (1.0 - t)))
                row[idx + 1] = min(255, int(base_g + ag * glow + 16 * (1.0 - t)))
                row[idx + 2] = min(255, int(base_b + ab * glow + 24 * (1.0 - t)))
                row[idx + 3] = 255
        rows.append(bytes(row))
    return _encode_png_rgba(w, h, rows, is_placeholder=True)


# ---------------------------------------------------------------------------
# 4. 校验、ZIP Store 打包与增量补丁构建
# ---------------------------------------------------------------------------


def _validate_rel_posix_path(rel_posix: str) -> None:
    if not rel_posix or rel_posix != rel_posix.strip():
        raise ValueError(f"非法相对路径（含空白）: {rel_posix!r}")
    if "\\" in rel_posix or rel_posix.startswith("/") or rel_posix.endswith("/"):
        raise ValueError(f"非法相对路径分隔符: {rel_posix!r}")
    if any(ch in rel_posix for ch in "?#:") or any(ord(ch) < 32 or ord(ch) == 127 for ch in rel_posix):
        raise ValueError(f"相对路径包含禁用字符: {rel_posix!r}")
    if any(seg in ("", ".", "..") for seg in rel_posix.split("/")):
        raise ValueError(f"相对路径包含非法路径段: {rel_posix!r}")


def _is_ignored_rel(rel_parts: tuple[str, ...]) -> bool:
    return any(part.startswith(".") or part in _IGNORED_PACK_NAMES for part in rel_parts)


def _iter_source_files(src_dir: Path) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(src_dir):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fname in sorted(filenames):
            full = Path(dirpath) / fname
            rel_parts = full.relative_to(src_dir).parts
            if _is_ignored_rel(rel_parts):
                continue
            rel_posix = "/".join(rel_parts)
            _validate_rel_posix_path(rel_posix)
            files.append((full, rel_posix))
    return files


def _pack_zip_store(src_dir: Path, out_file: Path, *, mode: Literal["ddbook", "ddpatch"]) -> int:
    """将源目录打包为合规无压缩 ZIP Store (method=0、无加密、无 Data Descriptor 0x0008)。"""
    if not src_dir.is_dir():
        raise FileNotFoundError(f"源目录不存在: {src_dir}")
    required_root = "manifest.json" if mode == "ddbook" else "patch.json"
    root_json = src_dir / required_root
    if not root_json.is_file():
        raise FileNotFoundError(f"源目录根目录缺少必需文件: {required_root}")
    root_data = json.loads(root_json.read_text(encoding="utf-8"))
    if not isinstance(root_data, dict):
        raise ValueError(f"{required_root} 根节点必须是 JSON 对象")

    files_to_pack = _iter_source_files(src_dir)
    if mode == "ddpatch":
        packed_set = {arc for _, arc in files_to_pack}
        for up in root_data.get("upserted_files", []):
            if up not in packed_set:
                raise ValueError(f"patch.json 声明的 upserted_files 缺失: {up}")

    out_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = out_file.with_suffix(out_file.suffix + ".tmp")
    with zipfile.ZipFile(tmp_file, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for full, rel_posix in files_to_pack:
            zf.write(full, arcname=rel_posix)
    tmp_file.replace(out_file)
    return len(files_to_pack)


def _builtin_validate_target(
    target: Path,
    *,
    strict: bool = True,
) -> ReaderValidationResult:
    """当本机未检出 DDNovelReader 仓库时的零依赖内置协议基础校验兜底。"""
    diagnostics: list[dict[str, Any]] = []

    def _add(severity: str, code: str, file: str, path: str, message: str) -> None:
        diagnostics.append(
            {
                "severity": severity,
                "code": code,
                "file": file,
                "path": path,
                "message": message,
            }
        )

    def _scan_nulls(val: Any, file: str, path: str) -> None:
        if val is None:
            _add("error", "FORBIDDEN_NULL", file, path, f"禁止在 {file} 中出现 null 字段: {path}")
        elif isinstance(val, dict):
            for k, v in val.items():
                _scan_nulls(v, file, f"{path}.{k}" if path else k)
        elif isinstance(val, list):
            for idx, v in enumerate(val):
                _scan_nulls(v, file, f"{path}[{idx}]")

    target_kind = "book_dir"
    if not target.exists():
        _add("error", "TARGET_NOT_FOUND", str(target), "", f"目标不存在: {target}")
    elif target.is_file() and target.suffix.lower() in {".ddbook", ".ddpatch"}:
        target_kind = target.suffix.lower().lstrip(".")
        try:
            with zipfile.ZipFile(target, "r") as zf:
                infos = zf.infolist()
                names = {info.filename for info in infos}
                required_root = "manifest.json" if target_kind == "ddbook" else "patch.json"
                if required_root not in names:
                    _add("error", "MISSING_ROOT_MANIFEST", required_root, "", f"归档根目录缺少 {required_root}")
                for info in infos:
                    if info.compress_type != zipfile.ZIP_STORED:
                        _add("error", "UNSUPPORTED_COMPRESSION", info.filename, "", "必须使用 ZIP Store (method=0)")
                    if info.flag_bits & 0x0001 or info.flag_bits & 0x0040:
                        _add("error", "ENCRYPTED_ENTRY", info.filename, "", "严禁加密 ZIP 条目")
                    if info.flag_bits & 0x0008:
                        _add("error", "FORBIDDEN_DATA_DESCRIPTOR", info.filename, "", "严禁使用 Data Descriptor (0x0008)")
                if required_root in names:
                    parsed = json.loads(zf.read(required_root).decode("utf-8"))
                    _scan_nulls(parsed, required_root, "")
        except Exception as exc:
            _add("error", "INVALID_ARCHIVE", str(target), "", f"归档解析失败: {exc}")
    elif target.is_dir():
        is_patch = (target / "patch.json").is_file()
        target_kind = "ddpatch" if is_patch else "book_dir"
        root_name = "patch.json" if is_patch else "manifest.json"
        root_path = target / root_name
        if not root_path.is_file():
            _add("error", "MISSING_MANIFEST", root_name, "", f"缺少根目录 {root_name}")
        else:
            raw = json.loads(root_path.read_text(encoding="utf-8"))
            _scan_nulls(raw, root_name, "")
            if not is_patch:
                if raw.get("protocol_version") != PROTOCOL_VERSION:
                    _add("error", "UNSUPPORTED_VERSION", root_name, "protocol_version", "protocol_version 必须为 1.0")
                for key in ("cover_asset", "banner_asset"):
                    rel = raw.get(key)
                    if isinstance(rel, str) and not (target / rel).is_file():
                        _add("error", "MISSING_ASSET_FILE", rel, key, f"引用资源不存在: {rel}")
                for vol in raw.get("volumes", []):
                    for ch in vol.get("chapters", []):
                        cfile = ch.get("file")
                        if not isinstance(cfile, str) or not (target / cfile).is_file():
                            _add("error", "MISSING_CHAPTER_FILE", str(cfile), "volumes", f"章节文件不存在: {cfile}")
    else:
        target_kind = "single_json"
        try:
            json.loads(target.read_text(encoding="utf-8"))
        except Exception as exc:
            _add("error", "INVALID_JSON", str(target), "", f"JSON 解析失败: {exc}")

    errors = sum(1 for d in diagnostics if d["severity"] == "error")
    warnings = sum(1 for d in diagnostics if d["severity"] == "warning")
    infos = sum(1 for d in diagnostics if d["severity"] == "info")
    return ReaderValidationResult(
        ok=(errors == 0 and (not strict or warnings == 0)),
        strict=strict,
        tool="ddnovel-builtin-validator",
        target=str(target),
        target_kind=target_kind,
        errors=errors,
        warnings=warnings,
        infos=infos,
        summary={"errors": errors, "warnings": warnings, "infos": infos},
        diagnostics=diagnostics,
    )


def validate_reader_target(
    target: Path | str,
    *,
    strict: bool = True,
    target_type: str | None = None,
    reader_dir: Path | str | None = None,
) -> ReaderValidationResult:
    """校验 DDNovelReader 源目录、`.ddbook`、`.ddpatch` 或单个协议 JSON。

    优先调用 `DDNovelReader/tools/ddnovel_tool.py validate --json` 以实现 100% 字节级与归约器一致校验。
    """
    target_path = Path(target).resolve()
    tool_script = resolve_reader_tool_script(reader_dir)
    if tool_script is None:
        return _builtin_validate_target(target_path, strict=strict)

    cmd = [sys.executable, str(tool_script), "validate", str(target_path), "--json"]
    if strict:
        cmd.append("--strict")
    if target_type:
        cmd.extend(["--type", target_type])

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    stdout = proc.stdout.strip()
    if not stdout:
        raise RuntimeError(
            f"ddnovel_tool.py 未输出校验报告 (exit={proc.returncode}): {proc.stderr.strip()}"
        )
    report = json.loads(stdout)
    summary = report.get("summary", {}) if isinstance(report, dict) else {}
    return ReaderValidationResult(
        ok=bool(report.get("ok", False)),
        strict=bool(report.get("strict", strict)),
        tool=str(report.get("tool", "ddnovel-tool-py")),
        target=str(report.get("target", target_path)),
        target_kind=str(report.get("target_kind", "book_dir")),
        errors=int(summary.get("errors", 0)),
        warnings=int(summary.get("warnings", 0)),
        infos=int(summary.get("infos", 0)),
        summary=summary if isinstance(summary, dict) else {},
        diagnostics=list(report.get("diagnostics", [])),
    )


def pack_reader_book(
    source_dir: Path | str,
    output_file: Path | str,
    *,
    strict: bool = True,
    skip_validate: bool = False,
    reader_dir: Path | str | None = None,
) -> tuple[Path, ReaderValidationResult | None]:
    """预校验并将作品源目录打包为合规 `.ddbook`（ZIP Store Method 0）。"""
    src = Path(source_dir).resolve()
    out = Path(output_file).resolve()
    if not skip_validate:
        pre_report = validate_reader_target(src, strict=strict, reader_dir=reader_dir)
        if not pre_report.ok:
            raise ValueError(
                f"作品目录未通过 DDNovelReader 校验 (errors={pre_report.errors}, warnings={pre_report.warnings}): "
                + "; ".join(
                    f"[{d.get('code')}] {d.get('file')}:{d.get('path')} {d.get('message')}"
                    for d in pre_report.diagnostics[:5]
                )
            )
    _pack_zip_store(src, out, mode="ddbook")
    post_report = (
        None
        if skip_validate
        else validate_reader_target(out, strict=strict, target_type="ddbook", reader_dir=reader_dir)
    )
    if post_report is not None and not post_report.ok:
        raise ValueError(
            f"打包产物 .ddbook 回读校验失败 (errors={post_report.errors}, warnings={post_report.warnings})"
        )
    return out, post_report


def _read_archive_or_dir_files(source: Path) -> dict[str, bytes]:
    if source.is_dir():
        return {rel: full.read_bytes() for full, rel in _iter_source_files(source)}
    if source.is_file():
        files: dict[str, bytes] = {}
        with zipfile.ZipFile(source, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                files[info.filename] = zf.read(info.filename)
        return files
    raise FileNotFoundError(f"旧版本作品源不存在: {source}")


def build_reader_patch(
    old_source: Path | str,
    new_book_dir: Path | str,
    output_file: Path | str,
    *,
    patch_id: str | None = None,
    summary: str = "",
    auto_bump_revision: bool = True,
    strict: bool = True,
    reader_dir: Path | str | None = None,
) -> tuple[Path, ReaderValidationResult]:
    """比对旧版作品目录/`.ddbook` 与新版作品目录，生成合规增量补丁 `.ddpatch`。"""
    old_path = Path(old_source).resolve()
    new_dir = Path(new_book_dir).resolve()
    out_path = Path(output_file).resolve()

    old_files = _read_archive_or_dir_files(old_path)
    new_files = _read_archive_or_dir_files(new_dir)
    if "manifest.json" not in old_files or "manifest.json" not in new_files:
        raise ValueError("旧版与新版作品源均必须包含根目录 manifest.json")

    old_manifest = json.loads(old_files["manifest.json"].decode("utf-8"))
    new_manifest = json.loads(new_files["manifest.json"].decode("utf-8"))
    book_id = str(new_manifest["book_id"])
    if str(old_manifest["book_id"]) != book_id:
        raise ValueError(
            f"book_id 不匹配：旧版={old_manifest['book_id']!r}，新版={book_id!r}"
        )

    base_rev = int(old_manifest["revision"])
    target_rev = int(new_manifest["revision"])
    if target_rev <= base_rev:
        if not auto_bump_revision:
            raise ValueError(
                f"新版 revision ({target_rev}) 必须严格大于旧版 revision ({base_rev})"
            )
        target_rev = base_rev + 1
        new_manifest["revision"] = target_rev
        new_manifest["updated_at"] = _utc_now_iso()
        new_files["manifest.json"] = (
            json.dumps(_strip_none_recursive(new_manifest), ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")

    upserted: list[str] = []
    deleted: list[str] = []
    file_hashes: dict[str, str] = {}

    for rel in sorted(new_files):
        new_bytes = new_files[rel]
        if rel not in old_files or old_files[rel] != new_bytes:
            upserted.append(rel)
            file_hashes[rel] = f"sha256:{hashlib.sha256(new_bytes).hexdigest()}"

    for rel in sorted(old_files):
        if rel not in new_files and rel != "manifest.json":
            deleted.append(rel)

    if "manifest.json" not in upserted:
        upserted.insert(0, "manifest.json")
        file_hashes["manifest.json"] = (
            f"sha256:{hashlib.sha256(new_files['manifest.json']).hexdigest()}"
        )

    resolved_patch_id = patch_id or f"patch_rev{base_rev}_to_{target_rev}"
    patch_doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "patch_id": _safe_ascii_id(resolved_patch_id, prefix="patch"),
        "book_id": book_id,
        "base_revision": base_rev,
        "target_revision": target_rev,
        "created_at": _utc_now_iso(),
        "upserted_files": upserted,
        "deleted_files": deleted,
        "file_hashes": file_hashes,
    }
    if summary.strip():
        patch_doc["summary"] = summary.strip()

    with tempfile.TemporaryDirectory(prefix="ddnovel_patch_") as tmp_dir_str:
        stage_dir = Path(tmp_dir_str)
        _write_json_strict(stage_dir / "patch.json", patch_doc)
        for rel in upserted:
            dest = stage_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(new_files[rel])

        pre = validate_reader_target(stage_dir, strict=strict, target_type="ddpatch", reader_dir=reader_dir)
        if not pre.ok:
            raise ValueError(
                f"补丁预校验失败 (errors={pre.errors}, warnings={pre.warnings}): "
                + "; ".join(d.get("message", "") for d in pre.diagnostics[:5])
            )
        _pack_zip_store(stage_dir, out_path, mode="ddpatch")

    post = validate_reader_target(out_path, strict=strict, target_type="ddpatch", reader_dir=reader_dir)
    if not post.ok:
        raise ValueError(
            f"补丁产物 .ddpatch 回读校验失败 (errors={post.errors}, warnings={post.warnings})"
        )
    return out_path, post


# ---------------------------------------------------------------------------
# 5. DDNovelGenerater -> DDNovelReader 核心导出与单章资源规划器
# ---------------------------------------------------------------------------


class ReaderExporter:
    """将 DDNovelGenerater 四维小说书库适配导出为 DDNovelReader 作品包与单章设计提案。"""

    def __init__(self, book_dir: Path | str) -> None:
        self._book_dir = Path(book_dir).resolve()
        self._config = NovelConfig.load(self._book_dir)
        self._parser = ProgressiveParser(book_dir=self._book_dir)

    @property
    def book_dir(self) -> Path:
        return self._book_dir

    @property
    def config(self) -> NovelConfig:
        return self._config

    # ------------------------------------------------------------------
    # 数据读取辅助（书库真源 -> 内存视图）
    # ------------------------------------------------------------------

    def _load_settings(self) -> dict[str, SettingDoc]:
        with SqliteIndexer(self._book_dir) as indexer:
            indexer.sync()
            settings = indexer.list_entities(include_archived=True)
        return {doc.id: doc for doc in settings}

    def _load_volumes(self) -> dict[str, VolumeDoc]:
        volumes_dir = resolve_volumes_dir(self._book_dir)
        result: dict[str, VolumeDoc] = {}
        if not volumes_dir.is_dir():
            return result
        for md_path in sorted(volumes_dir.glob("*.md")):
            if md_path.name.lower() == "readme.md":
                continue
            try:
                vol = self._parser.parse_volume_md(md_path)
                result[vol.id] = vol
            except Exception:
                continue
        return result

    def _load_chapter_summaries(self) -> dict[int, str]:
        summaries: dict[int, str] = {}
        for facts_path in list_facts_files(self._book_dir):
            if not facts_path.is_file():
                continue
            for line in facts_path.read_text(encoding="utf-8", errors="replace").splitlines():
                raw = line.strip()
                if not raw:
                    continue
                try:
                    rec = ChapterFactRecord.model_validate_json(raw)
                    if rec.summary.strip():
                        summaries[rec.ch] = rec.summary.strip()
                except Exception:
                    continue
        return summaries

    def _load_relations(self) -> list[TemporalRelation]:
        rel_path = resolve_relations_file(self._book_dir)
        if not rel_path.is_file():
            return []
        try:
            loaded = yaml.safe_load(rel_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return RelationsFile.model_validate(loaded).relations
        except Exception:
            return []
        return []

    def _load_book_description(self) -> str:
        outline_path = resolve_master_outline(self._book_dir)
        if outline_path.is_file():
            raw = outline_path.read_text(encoding="utf-8", errors="replace")
            _, body = split_frontmatter(raw)
            lines = [
                _strip_markdown_plain(line)
                for line in body.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            joined = " ".join(ln for ln in lines if ln)
            if joined:
                return joined[:220]
        return f"《{self._config.title}》DDNovelReader 伴读轻剧场作品包。"

    @staticmethod
    def _extract_chapter_num(path: Path, fm_dict: Mapping[str, Any], body: str) -> int | None:
        raw_ch = fm_dict.get("chapter")
        if isinstance(raw_ch, int) and not isinstance(raw_ch, bool) and raw_ch >= 1:
            return raw_ch
        if isinstance(raw_ch, str) and raw_ch.strip().isdigit() and int(raw_ch.strip()) >= 1:
            return int(raw_ch.strip())
        m = _CHAPTER_NUM_RE.search(path.stem)
        if m:
            return int(m.group(1) or m.group(2))
        first_line = body.strip().splitlines()[0] if body.strip() else ""
        m2 = _CHAPTER_NUM_RE.search(first_line)
        if m2:
            return int(m2.group(1) or m2.group(2))
        return None

    @staticmethod
    def _extract_chapter_title(
        ch_num: int,
        fm_dict: Mapping[str, Any],
        body: str,
        blueprint: BlueprintDoc | None,
        path: Path,
    ) -> str:
        for candidate in (
            fm_dict.get("title"),
            blueprint.title if blueprint is not None else None,
        ):
            if isinstance(candidate, str) and candidate.strip():
                title = candidate.strip()
                if not title.startswith("第") and not title.lower().startswith("ch"):
                    return f"第{ch_num}章 {title}"
                return title
        for line in body.splitlines():
            m = _CHAPTER_HEADING_RE.match(line)
            if m:
                heading = _strip_markdown_plain(m.group(1))
                if heading:
                    return heading
            elif line.strip():
                break
        stem_clean = re.sub(r"^(?:第\s*0*\d+\s*章|ch_?0*\d+)[-_:\s]*", "", path.stem, flags=re.IGNORECASE)
        stem_clean = re.sub(r"(?:_草稿|_draft)$", "", stem_clean, flags=re.IGNORECASE).strip("_- ")
        if stem_clean:
            return f"第{ch_num}章 {stem_clean}"
        return f"第{ch_num}章"

    def _collect_chapters(
        self,
        *,
        include_drafts: bool = False,
        target_chapter: int | None = None,
        volume_id: str | None = None,
        start_chapter: int | None = None,
        end_chapter: int | None = None,
    ) -> list[_ChapterSource]:
        summaries = self._load_chapter_summaries()
        by_num: dict[int, _ChapterSource] = {}

        # 1. 定稿优先
        manuscript_dir = resolve_manuscript_dir(self._book_dir)
        if manuscript_dir.is_dir():
            for md_path in sorted(manuscript_dir.rglob("*.md")):
                if not md_path.is_file() or md_path.name.lower() == "readme.md":
                    continue
                raw = md_path.read_text(encoding="utf-8", errors="replace")
                fm, body = split_frontmatter(raw)
                ch_num = self._extract_chapter_num(md_path, fm, body)
                if ch_num is None:
                    continue
                bp_path = find_existing_blueprint_file(self._book_dir, ch_num)
                bp = self._parser.parse_blueprint_md(bp_path) if bp_path is not None else None
                inferred_vol = (
                    (str(fm["volume_id"]).strip() if isinstance(fm.get("volume_id"), str) and fm["volume_id"].strip() else None)
                    or (bp.volume_id if bp is not None and bp.volume_id else None)
                    or (
                        infer_volume_id_from_stem(md_path.parent.name)
                        if md_path.parent != manuscript_dir
                        else None
                    )
                    or "vol_01"
                )
                title = self._extract_chapter_title(ch_num, fm, body, bp, md_path)
                by_num[ch_num] = _ChapterSource(
                    chapter=ch_num,
                    chapter_id=f"ch_{ch_num:04d}",
                    volume_id=inferred_vol,
                    title=title,
                    text=body,
                    source_path=md_path,
                    is_draft=False,
                    blueprint=bp,
                    summary=summaries.get(ch_num),
                )

        # 2. 按需补齐草稿（单章设计默认允许读取尚未定稿的草稿）
        if include_drafts:
            drafts_dir = resolve_drafts_dir(self._book_dir)
            if drafts_dir.is_dir():
                for md_path in sorted(drafts_dir.glob("*.md")):
                    if not md_path.is_file() or md_path.name.lower() == "readme.md":
                        continue
                    raw = md_path.read_text(encoding="utf-8", errors="replace")
                    fm, body = split_frontmatter(raw)
                    ch_num = self._extract_chapter_num(md_path, fm, body)
                    if ch_num is None or ch_num in by_num:
                        continue
                    bp_path = find_existing_blueprint_file(self._book_dir, ch_num)
                    bp = self._parser.parse_blueprint_md(bp_path) if bp_path is not None else None
                    inferred_vol = (
                        (str(fm["volume_id"]).strip() if isinstance(fm.get("volume_id"), str) and fm["volume_id"].strip() else None)
                        or (bp.volume_id if bp is not None and bp.volume_id else None)
                        or "vol_01"
                    )
                    title = self._extract_chapter_title(ch_num, fm, body, bp, md_path)
                    by_num[ch_num] = _ChapterSource(
                        chapter=ch_num,
                        chapter_id=f"ch_{ch_num:04d}",
                        volume_id=inferred_vol,
                        title=title,
                        text=body,
                        source_path=md_path,
                        is_draft=True,
                        blueprint=bp,
                        summary=summaries.get(ch_num),
                    )

        selected: list[_ChapterSource] = []
        for ch_num in sorted(by_num):
            item = by_num[ch_num]
            if target_chapter is not None and item.chapter != target_chapter:
                continue
            if volume_id is not None and item.volume_id != volume_id:
                continue
            if start_chapter is not None and item.chapter < start_chapter:
                continue
            if end_chapter is not None and item.chapter > end_chapter:
                continue
            selected.append(item)
        return selected

    def _load_asset_catalog(self) -> list[dict[str, Any]]:
        catalog_path = resolve_asset_catalog_file(self._book_dir)
        if not catalog_path.is_file():
            return []
        try:
            loaded = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if isinstance(loaded, dict) and isinstance(loaded.get("assets"), list):
            return [dict(item) for item in loaded["assets"] if isinstance(item, dict)]
        return []

    def _save_asset_catalog(self, entries: Sequence[Mapping[str, Any]]) -> Path:
        catalog_path = resolve_asset_catalog_file(self._book_dir, for_write=True)
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _utc_now_iso(),
            "assets": [dict(e) for e in entries],
        }
        catalog_path.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        return catalog_path

    def compute_archive_path(
        self,
        category: str,
        *,
        entity_id: str | None = None,
        chapter: int | None = None,
        volume_id: str | None = None,
        outfit: str = "常服",
        pose: str = "",
        expression: str = "neutral",
        label: str = "",
        ext: str = ".png",
        settings: Mapping[str, SettingDoc] | None = None,
    ) -> Path:
        """计算某类视觉/媒体资产在当前小说项目 `04_创作/插画/` 下的标准归档绝对路径。"""
        cat = category.strip().lower()
        suffix = ext if ext.startswith(".") else f".{ext}"
        loaded_settings = settings if settings is not None else self._load_settings()

        if cat == "sprite":
            if not entity_id:
                raise ValueError("归档角色立绘 (sprite) 必须提供 entity_id")
            doc = loaded_settings.get(entity_id)
            name_lbl = sanitize_filename_label(doc.name if doc else label)
            folder_name = f"{entity_id}-{name_lbl}" if name_lbl else entity_id
            outfit_lbl = sanitize_filename_label(outfit) or "常服"
            pose_lbl = sanitize_filename_label(pose) if pose and pose.strip().lower() not in {"stand", "default"} else ""
            expr_lbl = sanitize_filename_label(expression) or "neutral"
            stem_parts = [folder_name, outfit_lbl]
            if pose_lbl:
                stem_parts.append(pose_lbl)
            stem_parts.append(expr_lbl)
            stem = "_".join(stem_parts)
            return resolve_character_portraits_dir(self._book_dir, for_write=True) / folder_name / f"{stem}{suffix}"

        if cat == "portrait":
            if not entity_id:
                raise ValueError("归档角色微徽章/头像 (portrait) 必须提供 entity_id")
            doc = loaded_settings.get(entity_id)
            name_lbl = sanitize_filename_label(doc.name if doc else label)
            base = f"{entity_id}-{name_lbl}" if name_lbl else entity_id
            return resolve_character_avatars_dir(self._book_dir, for_write=True) / f"{base}_头像{suffix}"

        if cat == "background":
            doc = loaded_settings.get(entity_id) if entity_id else None
            parts: list[str] = []
            if entity_id:
                parts.append(entity_id)
            if doc is not None:
                parts.append(sanitize_filename_label(doc.name))
            clean_label = sanitize_filename_label(label)
            if clean_label and clean_label not in parts:
                parts.append(clean_label)
            if not parts:
                parts.append(f"ch_{chapter:04d}_bg" if chapter is not None else "scene_bg")
            stem = "-".join(p for p in parts if p)
            return resolve_scene_backgrounds_dir(self._book_dir, for_write=True) / f"{stem}{suffix}"

        if cat == "cg":
            vol_stem = format_volume_stem(volume_id or "vol_01")
            clean_label = sanitize_filename_label(label) or "剧情CG"
            ch_prefix = f"第{chapter:04d}章_" if chapter is not None else ""
            stem = f"{ch_prefix}{clean_label}"
            return resolve_scene_cgs_dir(self._book_dir, for_write=True) / vol_stem / f"{stem}{suffix}"

        if cat == "cover":
            return resolve_cover_banner_dir(self._book_dir, for_write=True) / f"cover{suffix}"

        if cat == "banner":
            return resolve_cover_banner_dir(self._book_dir, for_write=True) / f"banner{suffix}"

        if cat in {"video", "rig"}:
            clean_label = sanitize_filename_label(label or entity_id or "media") or "media"
            return resolve_dynamic_media_dir(self._book_dir, for_write=True) / f"{clean_label}{suffix}"

        raise ValueError(
            f"不支持的资产类别 {category!r}，可选: sprite | portrait | background | cg | cover | banner | video | rig"
        )

    def _infer_reader_asset_path(
        self,
        category: str,
        *,
        entity_id: str | None = None,
        chapter: int | None = None,
        outfit: str = "常服",
        pose: str = "",
        expression: str = "neutral",
        label: str = "",
        ext: str = ".png",
    ) -> str | None:
        cat = category.strip().lower()
        suffix = ext if ext.startswith(".") else f".{ext}"
        if cat == "cover":
            return f"assets/cover{suffix}"
        if cat == "banner":
            return f"assets/banner{suffix}"
        if cat == "sprite" and entity_id:
            outfit_clean = (outfit or "").strip()
            outfit_slug = "default"
            if outfit_clean and outfit_clean.lower() not in {"default", "常服", "经典常服"}:
                matched_outfit = next(
                    (oid for oid, olbl, _ in _OUTFIT_RULES if outfit_clean == olbl or outfit_clean.lower() == oid),
                    None,
                )
                outfit_slug = matched_outfit or _safe_ascii_id(outfit_clean, prefix="outfit")
            pose_clean = (pose or "").strip()
            pose_slug = ""
            if pose_clean and pose_clean.lower() not in {"stand", "default"}:
                matched_pose = next(
                    (pid for pid, plbl, _ in _POSE_RULES if pose_clean == plbl or pose_clean.lower() == pid),
                    None,
                )
                pose_slug = matched_pose or _safe_ascii_id(pose_clean, prefix="pose")
            expr_slug = _safe_ascii_id(expression or "neutral", prefix="expr")
            if pose_slug:
                return f"assets/sprites/{entity_id}_{outfit_slug}_{pose_slug}_{expr_slug}{suffix}"
            return f"assets/sprites/{entity_id}_{outfit_slug}_{expr_slug}{suffix}"
        if cat == "portrait" and entity_id:
            return f"assets/portraits/{entity_id}_badge{suffix}"
        if cat == "background":
            slug = _safe_ascii_id(entity_id or label or (f"ch_{chapter:04d}_main" if chapter else "scene"), prefix="bg")
            return f"assets/bg/{slug}{suffix}"
        if cat == "cg" and chapter is not None:
            return f"assets/cgs/cg_ch_{chapter:04d}_01{suffix}"
        if cat == "video":
            slug = _safe_ascii_id(label or entity_id or "loop", prefix="vid")
            return f"assets/videos/{slug}{suffix}"
        if cat == "rig":
            slug = _safe_ascii_id(entity_id or label or "rig", prefix="rig")
            return f"assets/rigs/{slug}{suffix}"
        return None

    def archive_asset(
        self,
        source_file: Path | str,
        *,
        category: str,
        entity_id: str | None = None,
        chapter: int | None = None,
        volume_id: str | None = None,
        outfit: str = "常服",
        pose: str = "",
        expression: str = "neutral",
        label: str = "",
        reader_asset_path: str | None = None,
        output_dir: Path | str | None = None,
        sync_to_reader_source: bool = True,
    ) -> ArchivedAssetResult:
        """将任意生成/外部图片或媒体文件归档至当前项目 `04_创作/插画/`，写入 `资产清单.yaml` 并可选同步至作品包 `source/`。"""
        src = Path(source_file).expanduser().resolve()
        if not src.is_file():
            raise FileNotFoundError(f"待归档的资产文件不存在: {src}")
        if src.stat().st_size == 0:
            raise ValueError(f"待归档的资产文件为空（0 字节）: {src}")

        settings = self._load_settings()
        ext = src.suffix.lower() or ".png"
        dest_archive = self.compute_archive_path(
            category,
            entity_id=entity_id,
            chapter=chapter,
            volume_id=volume_id,
            outfit=outfit,
            pose=pose,
            expression=expression,
            label=label or src.stem,
            ext=ext,
            settings=settings,
        )
        dest_archive.parent.mkdir(parents=True, exist_ok=True)
        if src != dest_archive.resolve():
            shutil.copy2(src, dest_archive)

        archive_rel = dest_archive.relative_to(self._book_dir).as_posix()
        resolved_reader_rel = reader_asset_path.strip() if reader_asset_path and reader_asset_path.strip() else (
            self._infer_reader_asset_path(
                category,
                entity_id=entity_id,
                chapter=chapter,
                outfit=outfit,
                pose=pose,
                expression=expression,
                label=label or src.stem,
                ext=ext,
            )
        )
        if resolved_reader_rel is not None:
            _validate_rel_posix_path(resolved_reader_rel)

        # 维护项目内 04_创作/插画/资产清单.yaml 台账
        entries = self._load_asset_catalog()
        new_entry: dict[str, Any] = {
            "archive_path": archive_rel,
            "category": category.strip().lower(),
            "updated_at": _utc_now_iso(),
        }
        if resolved_reader_rel is not None:
            new_entry["reader_asset_path"] = resolved_reader_rel
        if entity_id:
            new_entry["entity_id"] = entity_id
        if chapter is not None:
            new_entry["chapter"] = int(chapter)
        if volume_id:
            new_entry["volume_id"] = volume_id
        if category.strip().lower() == "sprite":
            new_entry["outfit"] = outfit
            if pose.strip():
                new_entry["pose"] = pose.strip()
            new_entry["expression"] = expression
        if label.strip():
            new_entry["label"] = label.strip()

        updated_entries: list[dict[str, Any]] = []
        replaced = False
        for item in entries:
            if item.get("archive_path") == archive_rel or (
                resolved_reader_rel and item.get("reader_asset_path") == resolved_reader_rel
            ):
                if not replaced:
                    updated_entries.append(new_entry)
                    replaced = True
            else:
                updated_entries.append(item)
        if not replaced:
            updated_entries.append(new_entry)
        catalog_path = self._save_asset_catalog(updated_entries)

        synced_to: str | None = None
        if sync_to_reader_source and resolved_reader_rel is not None:
            out_root = (
                Path(output_dir).resolve()
                if output_dir is not None
                else resolve_reader_pack_dir(self._book_dir, for_write=True)
            )
            source_dir = out_root / "source"
            if source_dir.is_dir() or output_dir is not None:
                target_in_source = source_dir / resolved_reader_rel
                target_in_source.parent.mkdir(parents=True, exist_ok=True)
                if dest_archive.resolve() != target_in_source.resolve():
                    shutil.copy2(dest_archive, target_in_source)
                synced_to = target_in_source.as_posix()

        return ArchivedAssetResult(
            category=category.strip().lower(),
            archive_path=dest_archive.as_posix(),
            archive_rel_path=archive_rel,
            reader_asset_path=resolved_reader_rel,
            synced_to_source_path=synced_to,
            catalog_path=catalog_path.as_posix(),
            entity_id=entity_id,
            chapter=chapter,
            outfit=outfit if category.strip().lower() == "sprite" else None,
            pose=(pose.strip() or None) if category.strip().lower() == "sprite" else None,
            expression=expression if category.strip().lower() == "sprite" else None,
        )

    def sync_assets_to_archive(
        self,
        *,
        output_dir: Path | str | None = None,
        source_dir: Path | str | None = None,
    ) -> SyncArchiveResult:
        """扫描作品包 `source/assets/` 下全部真实素材（跳过自动占位图），将其反向归档至项目 `04_创作/插画/`。"""
        if source_dir is not None:
            resolved_src = Path(source_dir).resolve()
        else:
            out_root = (
                Path(output_dir).resolve()
                if output_dir is not None
                else resolve_reader_pack_dir(self._book_dir)
            )
            resolved_src = out_root / "source"

        assets_dir = resolved_src / "assets"
        catalog_path = resolve_asset_catalog_file(self._book_dir, for_write=True)
        if not assets_dir.is_dir():
            return SyncArchiveResult(
                source_dir=resolved_src.as_posix(),
                archived=[],
                skipped_placeholders=[],
                already_archived=[],
                catalog_path=catalog_path.as_posix(),
            )

        settings = self._load_settings()
        existing_catalog = {
            str(e["reader_asset_path"]): str(e["archive_path"])
            for e in self._load_asset_catalog()
            if e.get("reader_asset_path") and e.get("archive_path")
        }

        archived: list[dict[str, Any]] = []
        skipped_placeholders: list[str] = []
        already_archived: list[str] = []

        for full in sorted(assets_dir.rglob("*")):
            if not full.is_file() or full.suffix.lower() not in _MEDIA_EXTS:
                continue
            rel_posix = full.relative_to(resolved_src).as_posix()
            if is_placeholder_png(full):
                skipped_placeholders.append(rel_posix)
                continue

            # 若台账已记录且磁盘内容完全一致，跳过重复拷贝
            if rel_posix in existing_catalog:
                arch_file = self._book_dir / existing_catalog[rel_posix]
                if arch_file.is_file() and arch_file.read_bytes() == full.read_bytes():
                    already_archived.append(rel_posix)
                    continue

            low = rel_posix.lower()
            stem = full.stem
            category = "background"
            entity_id: str | None = None
            chapter_num: int | None = None
            expression = "neutral"
            label = stem

            if low.startswith("assets/cover"):
                category = "cover"
            elif low.startswith("assets/banner"):
                category = "banner"
            elif low.startswith("assets/sprites/"):
                category = "sprite"
                for eid in settings:
                    if stem.startswith(f"{eid}_") or stem == eid:
                        entity_id = eid
                        rest = stem[len(eid) :].lstrip("_")
                        parts = [p for p in rest.split("_") if p]
                        if parts:
                            expression = parts[-1]
                        break
                if entity_id is None:
                    entity_id = stem.split("_")[0]
            elif low.startswith("assets/portraits/"):
                category = "portrait"
                for eid in settings:
                    if stem.startswith(f"{eid}_") or stem == eid:
                        entity_id = eid
                        break
                if entity_id is None:
                    entity_id = re.sub(r"_badge$", "", stem)
            elif low.startswith("assets/cgs/"):
                category = "cg"
                m = _CHAPTER_NUM_RE.search(stem)
                if m:
                    chapter_num = int(m.group(1) or m.group(2))
            elif low.startswith("assets/bg/"):
                category = "background"
                for eid in settings:
                    if stem == _safe_ascii_id(eid, prefix="bg") or stem.startswith(eid):
                        entity_id = eid
                        break
            elif low.startswith("assets/videos/"):
                category = "video"
            elif low.startswith("assets/rigs/"):
                category = "rig"

            res = self.archive_asset(
                full,
                category=category,
                entity_id=entity_id,
                chapter=chapter_num,
                expression=expression,
                label=label,
                reader_asset_path=rel_posix,
                output_dir=resolved_src.parent,
                sync_to_reader_source=False,
            )
            archived.append(res.to_dict())

        return SyncArchiveResult(
            source_dir=resolved_src.as_posix(),
            archived=archived,
            skipped_placeholders=skipped_placeholders,
            already_archived=already_archived,
            catalog_path=catalog_path.as_posix(),
        )

    def _scan_existing_illustrations(
        self,
        settings: Mapping[str, SettingDoc],
    ) -> dict[str, Any]:
        """扫描项目 `04_创作/插画/` 下的全部已归档资产（立绘/头像/背景/CG/封面横幅与资产台账）。"""
        illus_root = resolve_illustrations_dir(self._book_dir)
        char_sprites: dict[str, dict[str, Path]] = {}
        char_all_files: dict[str, list[Path]] = {}
        char_avatars: dict[str, Path] = {}
        chapter_cgs: dict[int, list[Path]] = {}
        backgrounds: dict[str, Path] = {}
        catalog_map: dict[str, Path] = {}
        cover_file: Path | None = None
        banner_file: Path | None = None

        # 0. 读取资产归档台账（最高优先级精确映射 reader_asset_path -> 项目归档文件）
        for entry in self._load_asset_catalog():
            arch_rel = entry.get("archive_path")
            reader_rel = entry.get("reader_asset_path")
            if isinstance(arch_rel, str):
                arch_full = self._book_dir / arch_rel
                if arch_full.is_file() and arch_full.stat().st_size > 0:
                    if isinstance(reader_rel, str):
                        catalog_map[reader_rel] = arch_full
                    cat = str(entry.get("category", "")).lower()
                    eid = entry.get("entity_id")
                    if cat == "cover" and cover_file is None:
                        cover_file = arch_full
                    elif cat == "banner" and banner_file is None:
                        banner_file = arch_full
                    elif cat == "portrait" and isinstance(eid, str):
                        char_avatars.setdefault(eid, arch_full)
                    elif cat == "sprite" and isinstance(eid, str):
                        expr = str(entry.get("expression") or "neutral")
                        char_sprites.setdefault(eid, {}).setdefault(expr, arch_full)
                        if arch_full not in char_all_files.get(eid, []):
                            char_all_files.setdefault(eid, []).append(arch_full)
                    elif cat == "background":
                        if isinstance(eid, str):
                            backgrounds.setdefault(_safe_ascii_id(eid, prefix="bg"), arch_full)
                        if isinstance(reader_rel, str):
                            backgrounds.setdefault(Path(reader_rel).stem.lower(), arch_full)

        if not illus_root.is_dir():
            return {
                "char_sprites": char_sprites,
                "char_all_files": char_all_files,
                "char_avatars": char_avatars,
                "chapter_cgs": chapter_cgs,
                "backgrounds": backgrounds,
                "catalog_map": catalog_map,
                "cover": cover_file,
                "banner": banner_file,
            }

        char_docs = [doc for doc in settings.values() if doc.category == SettingCategory.CHARACTER]
        loc_docs = [doc for doc in settings.values() if doc.category == SettingCategory.GEOGRAPHY]

        # 1. 角色立绘扫描（同时识别放在角色目录下的 *_头像.* / *_badge.*）
        portrait_dirs = [
            illus_root / ZH_CHARACTER_PORTRAITS_SUBDIR,
            illus_root / "sprites",
            illus_root / "characters",
        ]
        for pdir in portrait_dirs:
            if not pdir.is_dir():
                continue
            for img in sorted(pdir.rglob("*")):
                if not img.is_file() or img.suffix.lower() not in _IMAGE_EXTS:
                    continue
                rel_str = img.relative_to(pdir).as_posix()
                matched_id: str | None = None
                for cdoc in char_docs:
                    if cdoc.id in rel_str or cdoc.name in rel_str:
                        matched_id = cdoc.id
                        break
                if matched_id is None:
                    continue
                if "头像" in img.stem or "badge" in img.stem.lower() or "avatar" in img.stem.lower():
                    char_avatars.setdefault(matched_id, img)
                    continue
                if img not in char_all_files.get(matched_id, []):
                    char_all_files.setdefault(matched_id, []).append(img)
                expr = "neutral"
                for expr_key, pat in _FILENAME_EXPR_HINTS:
                    if pat.search(img.stem):
                        expr = expr_key
                        break
                char_sprites.setdefault(matched_id, {})
                char_sprites[matched_id].setdefault(expr, img)

        # 2. 角色头像/微徽章扫描（04_创作/插画/角色头像/）
        avatar_dirs = [
            illus_root / ZH_CHARACTER_AVATARS_SUBDIR,
            illus_root / "头像",
            illus_root / "portraits",
        ]
        for adir in avatar_dirs:
            if not adir.is_dir():
                continue
            for img in sorted(adir.rglob("*")):
                if not img.is_file() or img.suffix.lower() not in _IMAGE_EXTS:
                    continue
                rel_str = img.relative_to(adir).as_posix()
                for cdoc in char_docs:
                    if cdoc.id in rel_str or cdoc.name in rel_str:
                        char_avatars.setdefault(cdoc.id, img)
                        break

        # 3. 场景 CG 扫描
        cg_dirs = [illus_root / ZH_SCENE_CGS_SUBDIR, illus_root / "cgs"]
        for cdir in cg_dirs:
            if not cdir.is_dir():
                continue
            for img in sorted(cdir.rglob("*")):
                if not img.is_file() or img.suffix.lower() not in _IMAGE_EXTS:
                    continue
                m = _CHAPTER_NUM_RE.search(img.stem)
                if m:
                    ch_num = int(m.group(1) or m.group(2))
                    if img not in chapter_cgs.get(ch_num, []):
                        chapter_cgs.setdefault(ch_num, []).append(img)

        # 4. 场景背景扫描（支持 场景背景/、背景/、bg/，并按地点 ID / 中文名 / slug 匹配）
        for bg_dir in (illus_root / ZH_SCENE_BACKGROUNDS_SUBDIR, illus_root / "背景", illus_root / "bg"):
            if not bg_dir.is_dir():
                continue
            for img in sorted(bg_dir.rglob("*")):
                if not img.is_file() or img.suffix.lower() not in _IMAGE_EXTS:
                    continue
                backgrounds.setdefault(img.stem.lower(), img)
                rel_str = img.relative_to(bg_dir).as_posix()
                for ldoc in loc_docs:
                    if ldoc.id in rel_str or any(len(t) >= 2 and t in rel_str for t in ldoc.all_names()):
                        backgrounds.setdefault(_safe_ascii_id(ldoc.id, prefix="bg"), img)

        # 5. 封面与横幅扫描（优先 封面与横幅/，兼容 04_创作/插画/ 根目录）
        cover_scan_roots = [illus_root / ZH_COVER_BANNER_SUBDIR, illus_root]
        for croot in cover_scan_roots:
            if not croot.is_dir():
                continue
            for img in sorted(croot.glob("*")):
                if not img.is_file() or img.suffix.lower() not in _IMAGE_EXTS:
                    continue
                low = img.stem.lower()
                if cover_file is None and ("cover" in low or "封面" in img.stem):
                    cover_file = img
                elif banner_file is None and ("banner" in low or "横幅" in img.stem):
                    banner_file = img

        return {
            "char_sprites": char_sprites,
            "char_all_files": char_all_files,
            "char_avatars": char_avatars,
            "chapter_cgs": chapter_cgs,
            "backgrounds": backgrounds,
            "catalog_map": catalog_map,
            "cover": cover_file,
            "banner": banner_file,
        }

    # ------------------------------------------------------------------
    # 正文解析、角色/情绪/场景识别与章节编排
    # ------------------------------------------------------------------

    @staticmethod
    def _char_accent_map(settings: Mapping[str, SettingDoc]) -> dict[str, str]:
        char_ids = sorted(
            doc.id for doc in settings.values() if doc.category == SettingCategory.CHARACTER
        )
        return {
            cid: _CHARACTER_ACCENT_PALETTE[idx % len(_CHARACTER_ACCENT_PALETTE)]
            for idx, cid in enumerate(char_ids)
        }

    @staticmethod
    def _detect_weather(text: str) -> tuple[str, float, str]:
        for wtype, intensity, label, pat in _WEATHER_RULES:
            if pat.search(text):
                return wtype, intensity, label
        return "none", 0.0, "无特效"

    @staticmethod
    def _detect_expression(text: str) -> tuple[str, str]:
        # 优先根据引号外的动作/神态描写判定角色真实表情，再回退到台词文本
        outside_quotes = re.sub(r"[「『“\"][^」』”\"]*[」』”\"]", " ", text)
        for zone in (outside_quotes, text):
            for expr_id, expr_label, pat in _EXPRESSION_RULES:
                if pat.search(zone):
                    return expr_id, expr_label
        return "neutral", "常态"

    @staticmethod
    def _match_entity_ids_in_text(
        text: str,
        settings: Mapping[str, SettingDoc],
        *,
        allowed_ids: set[str] | None = None,
    ) -> list[str]:
        matched: list[str] = []
        for eid, doc in settings.items():
            if allowed_ids is not None and eid not in allowed_ids:
                continue
            terms = [t.strip() for t in doc.all_names() if len(t.strip()) >= 2]
            if any(term in text for term in terms):
                matched.append(eid)
        return matched

    @staticmethod
    def _find_character_by_name(
        speaker_name: str,
        char_docs: Sequence[SettingDoc],
    ) -> SettingDoc | None:
        cleaned = speaker_name.strip()
        for doc in char_docs:
            if cleaned == doc.name or cleaned in doc.aliases:
                return doc
        return None

    def _detect_dialogue_speaker(
        self,
        paragraph: str,
        char_docs: Sequence[SettingDoc],
        *,
        last_speaker: SettingDoc | None,
        pov_doc: SettingDoc | None,
    ) -> tuple[SettingDoc | None, str]:
        """识别段落是否为对白并返回 `(说话人 SettingDoc, 清洗后的纯文本)`。"""
        # 模式 1: 显式 "角色名：「台词」"
        m = _EXPLICIT_DIALOGUE_RE.match(paragraph)
        if m:
            found = self._find_character_by_name(m.group(1), char_docs)
            if found is not None:
                return found, m.group(2).strip()

        # 模式 2: 段内包含引号且点名了某角色（如：林岚低声道：“……”）
        has_quote = any(pair[0] in paragraph and pair[1] in paragraph for pair in (("「", "」"), ("『", "』"), ("“", "”"), ('"', '"')))
        if has_quote:
            # 寻找引号前或引号后紧邻的已知角色名
            quote_pos = min(
                (paragraph.find(ch) for ch in ("「", "『", "“", '"') if ch in paragraph),
                default=-1,
            )
            prefix = paragraph[:quote_pos] if quote_pos > 0 else ""
            suffix_start = max(
                (paragraph.rfind(ch) for ch in ("」", "』", "”", '"') if ch in paragraph),
                default=-1,
            )
            suffix = paragraph[suffix_start + 1 :] if suffix_start != -1 else ""
            attribution_zone = f"{prefix} {suffix}"

            mentioned_in_zone: list[tuple[int, SettingDoc]] = []
            for doc in char_docs:
                for term in doc.all_names():
                    if len(term) >= 2 and term in attribution_zone:
                        mentioned_in_zone.append((attribution_zone.find(term), doc))
                        break
            if len(mentioned_in_zone) == 1:
                return mentioned_in_zone[0][1], paragraph
            if len(mentioned_in_zone) > 1:
                mentioned_in_zone.sort(key=lambda pair: pair[0])
                return mentioned_in_zone[0][1], paragraph

            # 模式 3: 纯引号对白段，优先归属上一句说话人或本章 POV（仅当确定存在角色时）
            if _PURE_QUOTE_RE.match(paragraph):
                fallback = last_speaker or pov_doc
                if fallback is not None:
                    return fallback, paragraph

        return None, paragraph

    def _split_prose_segments(self, body: str) -> list[dict[str, Any]]:
        """将 Markdown 章节正文切分为原始语义段（narration/card/divider）。"""
        lines = body.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        segments: list[dict[str, Any]] = []
        buf: list[str] = []
        idx = 0
        skipped_title_heading = False

        def _flush_buf() -> None:
            nonlocal buf
            if not buf:
                return
            joined = "\n".join(buf).strip()
            buf = []
            if not joined:
                return
            cleaned = _strip_markdown_plain(joined)
            if cleaned:
                segments.append({"kind": "paragraph", "text": cleaned})

        while idx < len(lines):
            line = lines[idx]
            stripped = line.strip()

            # 跳过章节首部的主标题行（如 "# 第1章 ..."）
            if not skipped_title_heading and not segments and not buf and _CHAPTER_HEADING_RE.match(line):
                skipped_title_heading = True
                idx += 1
                continue

            # 叙事道具卡围栏 :::system_hud[标题|注脚] ... :::
            card_open = _CARD_OPEN_RE.match(stripped)
            if card_open:
                _flush_buf()
                style = card_open.group(1)
                bracket = (card_open.group(2) or "").strip()
                card_title = ""
                card_footer = ""
                if bracket:
                    if "|" in bracket:
                        t_part, f_part = bracket.split("|", maxsplit=1)
                        card_title, card_footer = t_part.strip(), f_part.strip()
                    else:
                        card_title = bracket
                idx += 1
                card_lines: list[str] = []
                while idx < len(lines) and not _CARD_CLOSE_RE.match(lines[idx].strip()):
                    card_lines.append(lines[idx])
                    idx += 1
                if idx < len(lines) and _CARD_CLOSE_RE.match(lines[idx].strip()):
                    idx += 1
                card_text = _strip_markdown_plain("\n".join(card_lines))
                if card_text:
                    segments.append(
                        {
                            "kind": "diegetic_card",
                            "style": style,
                            "title": card_title,
                            "footer": card_footer,
                            "text": card_text,
                        }
                    )
                continue

            # 场景分割线 --- / *** / —— ... ——
            if _SCENE_DIVIDER_RE.match(stripped):
                _flush_buf()
                div_text = ""
                if stripped.startswith("——") and stripped.endswith("——") and len(stripped) > 4:
                    div_text = stripped
                segments.append({"kind": "scene_divider", "text": div_text})
                idx += 1
                continue

            # 空行切段
            if not stripped:
                _flush_buf()
                idx += 1
                continue

            # 常规中文小说常按单行一段排版：每行非空文本作为独立块，避免多段黏连
            _flush_buf()
            cleaned_line = _strip_markdown_plain(stripped)
            if cleaned_line:
                segments.append({"kind": "paragraph", "text": cleaned_line})
            idx += 1

        _flush_buf()
        if not segments:
            segments.append({"kind": "paragraph", "text": "（本章正文待补充）"})
        return segments

    def _choose_chapter_locations(
        self,
        ch_src: _ChapterSource,
        settings: Mapping[str, SettingDoc],
    ) -> list[SettingDoc]:
        loc_docs = [doc for doc in settings.values() if doc.category == SettingCategory.GEOGRAPHY]
        chosen: list[SettingDoc] = []
        seen: set[str] = set()

        if ch_src.blueprint is not None:
            for lid in ch_src.blueprint.cast.locations:
                doc = settings.get(lid)
                if doc is not None and lid not in seen:
                    seen.add(lid)
                    chosen.append(doc)

        for doc in loc_docs:
            if doc.id in seen:
                continue
            if any(len(term) >= 2 and term in ch_src.text for term in doc.all_names()):
                seen.add(doc.id)
                chosen.append(doc)

        return chosen

    def _build_chapter_document(
        self,
        ch_src: _ChapterSource,
        *,
        settings: Mapping[str, SettingDoc],
        accent_map: Mapping[str, str],
        illus: Mapping[str, Any],
        volume_title: str,
        existing_source_dir: Path | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """编译单章 `ChapterDocument` 与该章使用的素材/元数据注册表。"""
        char_docs = [doc for doc in settings.values() if doc.category == SettingCategory.CHARACTER]
        pov_doc = (
            settings.get(ch_src.blueprint.pov)
            if ch_src.blueprint is not None and ch_src.blueprint.pov
            else None
        )
        if pov_doc is not None and pov_doc.category != SettingCategory.CHARACTER:
            pov_doc = None

        segments = self._split_prose_segments(ch_src.text)
        locations = self._choose_chapter_locations(ch_src, settings)
        weather_type, weather_intensity, weather_label = self._detect_weather(ch_src.text)

        # 确定主背景资源路径
        if locations:
            primary_loc = locations[0]
            bg_slug = _safe_ascii_id(primary_loc.id, prefix="bg")
            primary_bg_asset = f"assets/bg/{bg_slug}.png"
            primary_bg_title = primary_loc.name
            primary_bg_desc = (primary_loc.core_text.strip() or primary_loc.name)[:160]
        else:
            bg_slug = f"{ch_src.chapter_id}_main"
            primary_bg_asset = f"assets/bg/{bg_slug}.png"
            primary_bg_title = f"{ch_src.title} · 主场景"
            primary_bg_desc = f"{ch_src.title} 开篇背景场景。"

        # 预扫一遍段落，收集本章说话人与表情差分
        parsed_entries: list[dict[str, Any]] = []
        last_speaker: SettingDoc | None = None
        speaker_order: list[str] = []
        speaker_exprs: dict[str, set[str]] = {}
        dialogue_counts: dict[str, int] = {}

        for seg in segments:
            if seg["kind"] == "paragraph":
                sp_doc, clean_text = self._detect_dialogue_speaker(
                    seg["text"],
                    char_docs,
                    last_speaker=last_speaker,
                    pov_doc=pov_doc,
                )
                if sp_doc is not None:
                    last_speaker = sp_doc
                    if sp_doc.id not in speaker_order:
                        speaker_order.append(sp_doc.id)
                    dialogue_counts[sp_doc.id] = dialogue_counts.get(sp_doc.id, 0) + 1
                    expr_id, expr_label = self._detect_expression(clean_text)
                    speaker_exprs.setdefault(sp_doc.id, set()).add(expr_id)
                    parsed_entries.append(
                        {
                            "type": "dialogue",
                            "text": clean_text,
                            "speaker": sp_doc,
                            "expression": expr_id,
                            "expression_label": expr_label,
                        }
                    )
                else:
                    # 更新叙述中提到的最后角色以便后续纯引号对白归属
                    for cdoc in char_docs:
                        if any(len(t) >= 2 and t in clean_text for t in cdoc.all_names()):
                            last_speaker = cdoc
                            break
                    parsed_entries.append({"type": "narration", "text": clean_text})
            elif seg["kind"] == "diegetic_card":
                parsed_entries.append(
                    {
                        "type": "diegetic_card",
                        "text": seg["text"],
                        "style": seg["style"],
                        "title": seg["title"],
                        "footer": seg["footer"],
                    }
                )
            elif seg["kind"] == "scene_divider":
                parsed_entries.append({"type": "scene_divider", "text": seg["text"]})

        # 确定初始在场角色（优先蓝图 POV/主要角色或前两位说话人，最多 2 名，左右分布）
        initial_cast_ids: list[str] = []
        for cid in (
            ([ch_src.blueprint.pov] if ch_src.blueprint and ch_src.blueprint.pov else [])
            + (ch_src.blueprint.cast.characters if ch_src.blueprint else [])
            + speaker_order
        ):
            if cid and cid in settings and settings[cid].category == SettingCategory.CHARACTER:
                if cid not in initial_cast_ids:
                    initial_cast_ids.append(cid)
            if len(initial_cast_ids) >= 2:
                break

        def _sprite_asset_for(cid: str, expr: str) -> str:
            return f"assets/sprites/{cid}_default_{expr}.png"

        def _sprite_unlock_id(cid: str, expr: str) -> str:
            return f"gal_{cid}_default_{expr}"

        def _portrait_asset_for(cid: str) -> str:
            return f"assets/portraits/{cid}_badge.png"

        slots_two = ("left", "right")
        initial_sprites: list[dict[str, Any]] = []
        active_on_stage: dict[str, str] = {}
        used_sprites: dict[str, dict[str, dict[str, str]]] = {}
        used_portraits: dict[str, str] = {}

        for idx, cid in enumerate(initial_cast_ids):
            slot = "center" if len(initial_cast_ids) == 1 else slots_two[idx]
            asset = _sprite_asset_for(cid, "neutral")
            ugid = _sprite_unlock_id(cid, "neutral")
            initial_sprites.append(
                {
                    "id": cid,
                    "render_kind": "static_diff",
                    "asset": asset,
                    "expression": "neutral",
                    "slot": slot,
                    "scale": 1.0,
                    "flip_x": False,
                    "z_index": idx + 1,
                    "unlock_gallery_id": ugid,
                }
            )
            active_on_stage[cid] = "neutral"
            used_sprites.setdefault(cid, {})["neutral"] = {
                "asset": asset,
                "label": "常态",
                "unlock_gallery_id": ugid,
            }

        # 检查本章是否有已存在的场景 CG（来自 04_创作/插画/场景CG/）
        existing_ch_cgs: list[Path] = list(illus.get("chapter_cgs", {}).get(ch_src.chapter, []))
        cg_assets_used: list[dict[str, Any]] = []
        cg_insert_block_idx: int | None = None
        if existing_ch_cgs:
            # 选在章节后半段最具张力的旁白块作为破框 CG 展示点
            narration_indices = [
                i for i, e in enumerate(parsed_entries) if e["type"] == "narration"
            ]
            if narration_indices:
                climax_matches = [
                    i
                    for i in narration_indices
                    if _CLIMAX_CUE_RE.search(parsed_entries[i]["text"])
                ]
                cg_insert_block_idx = climax_matches[-1] if climax_matches else narration_indices[-1]
            else:
                cg_insert_block_idx = len(parsed_entries) - 1

        # 构建 blocks[]
        blocks: list[dict[str, Any]] = []
        used_backgrounds: dict[str, dict[str, Any]] = {
            primary_bg_asset: {
                "id": f"scene_{bg_slug}",
                "title": primary_bg_title,
                "description": primary_bg_desc,
                "asset": primary_bg_asset,
                "chapter_id": ch_src.chapter_id,
            }
        }
        next_loc_idx = 1
        extra_slot_cycle = ("right", "left", "far_right", "far_left", "center")
        extra_slot_cursor = 0
        cg_shown = False

        for bi, entry in enumerate(parsed_entries):
            block_id = f"ch{ch_src.chapter}_b{bi + 1:03d}"
            btype = entry["type"]
            btext = entry["text"]
            block: dict[str, Any] = {
                "id": block_id,
                "type": btype,
                "break_policy": "hard_before" if btype == "scene_divider" else "auto",
                "text": btext,
            }
            stage_patch: dict[str, Any] = {}

            if btype == "dialogue":
                sp_doc: SettingDoc = entry["speaker"]
                cid = sp_doc.id
                expr_id = entry["expression"]
                expr_label = entry["expression_label"]
                accent = accent_map.get(cid, "#6CE5E8")
                badge = _portrait_asset_for(cid)
                used_portraits[cid] = badge
                block["speaker"] = {
                    "id": cid,
                    "name": sp_doc.name,
                    "accent_color": accent,
                    "badge_asset": badge,
                }
                asset = _sprite_asset_for(cid, expr_id)
                ugid = _sprite_unlock_id(cid, expr_id)
                used_sprites.setdefault(cid, {})[expr_id] = {
                    "asset": asset,
                    "label": expr_label,
                    "unlock_gallery_id": ugid,
                }
                if cid not in active_on_stage:
                    slot = extra_slot_cycle[extra_slot_cursor % len(extra_slot_cycle)]
                    extra_slot_cursor += 1
                    stage_patch["sprites_upsert"] = [
                        {
                            "id": cid,
                            "render_kind": "static_diff",
                            "asset": asset,
                            "expression": expr_id,
                            "slot": slot,
                            "unlock_gallery_id": ugid,
                        }
                    ]
                    active_on_stage[cid] = expr_id
                elif active_on_stage[cid] != expr_id:
                    stage_patch["sprites_upsert"] = [
                        {
                            "id": cid,
                            "asset": asset,
                            "expression": expr_id,
                            "unlock_gallery_id": ugid,
                        }
                    ]
                    active_on_stage[cid] = expr_id
            elif btype == "diegetic_card":
                meta: dict[str, Any] = {"style": entry["style"]}
                if entry.get("title"):
                    meta["title"] = entry["title"]
                if entry.get("footer"):
                    meta["footer"] = entry["footer"]
                block["card_meta"] = meta
            elif btype == "scene_divider" and next_loc_idx < len(locations):
                next_loc = locations[next_loc_idx]
                next_loc_idx += 1
                nbg_slug = _safe_ascii_id(next_loc.id, prefix="bg")
                nbg_asset = f"assets/bg/{nbg_slug}.png"
                used_backgrounds[nbg_asset] = {
                    "id": f"scene_{nbg_slug}",
                    "title": next_loc.name,
                    "description": (next_loc.core_text.strip() or next_loc.name)[:160],
                    "asset": nbg_asset,
                    "chapter_id": ch_src.chapter_id,
                }
                stage_patch["background"] = {
                    "asset": nbg_asset,
                    "media_type": "image",
                    "focal_point": [0.5, 0.35],
                    "transition": "dip_to_black",
                }
                if cg_shown:
                    stage_patch["mode"] = "composite"
                    stage_patch["break_frame"] = False
                    stage_patch["cg"] = None
                    cg_shown = False

            # 如果本章存在匹配好的剧情 CG，在高潮块切换破框 CG 演出
            if cg_insert_block_idx is not None and bi == cg_insert_block_idx and existing_ch_cgs:
                cg_src_path = existing_ch_cgs[0]
                cg_id = f"cg_{ch_src.chapter_id}_01"
                cg_ext = cg_src_path.suffix.lower() if cg_src_path.suffix.lower() in _IMAGE_EXTS else ".png"
                cg_asset = f"assets/cgs/{cg_id}{cg_ext}"
                cg_title_label = re.sub(
                    r"^(?:第\s*0*\d+\s*章|ch_?0*\d+)(?:_S\d+)?[-_\s]*",
                    "",
                    cg_src_path.stem,
                    flags=re.IGNORECASE,
                ).strip("_- ") or f"{ch_src.title} · 典藏 CG"
                block["break_policy"] = "standalone_page"
                stage_patch["mode"] = "cg_showcase"
                stage_patch["break_frame"] = True
                stage_patch["clear_sprites"] = True
                stage_patch["background"] = None
                stage_patch["cg"] = {
                    "asset": cg_asset,
                    "media_type": "image",
                    "transition": "crossfade",
                    "unlock_gallery_id": cg_id,
                }
                active_on_stage.clear()
                cg_shown = True
                cg_assets_used.append(
                    {
                        "id": cg_id,
                        "title": cg_title_label,
                        "description": f"《{ch_src.title}》剧情典藏 CG。",
                        "asset": cg_asset,
                        "chapter_id": ch_src.chapter_id,
                        "source_file": cg_src_path,
                    }
                )
            elif cg_shown and btype != "scene_divider":
                # 破框 CG 后的下一段自然切回常规合成舞台
                stage_patch["mode"] = "composite"
                stage_patch["break_frame"] = False
                stage_patch["cg"] = None
                stage_patch["background"] = {
                    "asset": primary_bg_asset,
                    "media_type": "image",
                    "focal_point": [0.5, 0.35],
                    "transition": "crossfade",
                }
                cg_shown = False

            entity_refs = self._match_entity_ids_in_text(btext, settings)
            if entity_refs:
                block["entity_refs"] = entity_refs
            if stage_patch:
                block["stage_patch"] = stage_patch

            blocks.append(block)

        chapter_doc: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "chapter_id": ch_src.chapter_id,
            "chapter_index": ch_src.chapter,
            "volume_id": ch_src.volume_id,
            "volume_title": volume_title,
            "title": ch_src.title,
            "initial_stage": {
                "mode": "composite",
                "break_frame": False,
                "split_ratio_hint": 0.45,
                "background": {
                    "asset": primary_bg_asset,
                    "media_type": "image",
                    "focal_point": [0.5, 0.35],
                    "transition": "crossfade",
                },
                "sprites": initial_sprites,
                "weather_fx": {
                    "type": weather_type,
                    "intensity": weather_intensity,
                },
                "camera": {
                    "zoom": 1.0,
                    "pan": [0, 0],
                    "motion": "slow_drift",
                    "shake": "none",
                    "duration_ms": 450,
                },
            },
            "blocks": blocks,
        }

        meta_bundle = {
            "used_backgrounds": used_backgrounds,
            "used_sprites": used_sprites,
            "used_portraits": used_portraits,
            "cg_assets_used": cg_assets_used,
            "dialogue_counts": dialogue_counts,
            "speaker_exprs": {k: sorted(v) for k, v in speaker_exprs.items()},
            "weather": {"type": weather_type, "intensity": weather_intensity, "label": weather_label},
            "locations": locations,
            "initial_cast_ids": initial_cast_ids,
        }
        return chapter_doc, meta_bundle

    # ------------------------------------------------------------------
    # 单章视觉资源需求与演出提案（支持单章单独设计）
    # ------------------------------------------------------------------

    def plan_chapter_resources(
        self,
        chapter: int,
        *,
        output_dir: Path | str | None = None,
        include_drafts: bool = True,
        save_plan: bool = False,
    ) -> ChapterResourcePlan:
        """分析指定章节的正文、蓝图、设定与已有插画，提出完整视觉资源需求与伴读演出方案。"""
        chapters = self._collect_chapters(
            include_drafts=include_drafts,
            target_chapter=chapter,
        )
        if not chapters:
            raise FileNotFoundError(
                f"未找到第 {chapter} 章的定稿或草稿正文（请检查 04_创作/定稿/ 或 04_创作/草稿/）"
            )
        ch_src = chapters[0]
        settings = self._load_settings()
        volumes = self._load_volumes()
        accent_map = self._char_accent_map(settings)
        illus = self._scan_existing_illustrations(settings)

        out_root = (
            Path(output_dir).resolve()
            if output_dir is not None
            else resolve_reader_pack_dir(self._book_dir, for_write=True)
        )
        source_dir = out_root / "source"
        vol_doc = volumes.get(ch_src.volume_id)
        vol_title = vol_doc.title if vol_doc and vol_doc.title else f"第{ch_src.volume_id.split('_')[-1]}卷"

        chapter_doc, meta = self._build_chapter_document(
            ch_src,
            settings=settings,
            accent_map=accent_map,
            illus=illus,
            volume_title=vol_title,
            existing_source_dir=source_dir if source_dir.is_dir() else None,
        )

        # 汇总登场角色档案
        cast_ids: list[str] = []
        for cid in (
            ([ch_src.blueprint.pov] if ch_src.blueprint and ch_src.blueprint.pov else [])
            + (ch_src.blueprint.cast.characters if ch_src.blueprint else [])
            + list(meta["used_sprites"].keys())
            + list(meta["dialogue_counts"].keys())
        ):
            if cid and cid in settings and settings[cid].category == SettingCategory.CHARACTER and cid not in cast_ids:
                cast_ids.append(cid)

        # 补充正文中直接提及但未在蓝图/对白出现的角色
        for eid in self._match_entity_ids_in_text(ch_src.text, settings):
            if settings[eid].category == SettingCategory.CHARACTER and eid not in cast_ids:
                cast_ids.append(eid)

        cast_profiles: list[ChapterCastProfile] = []
        for cid in cast_ids:
            doc = settings[cid]
            role = "mentioned"
            if ch_src.blueprint and ch_src.blueprint.pov == cid:
                role = "pov"
            elif cid in meta["initial_cast_ids"] or (ch_src.blueprint and cid in ch_src.blueprint.cast.characters):
                role = "featured"
            elif meta["dialogue_counts"].get(cid, 0) > 0:
                role = "speaker"
            exprs = sorted(
                set(meta["speaker_exprs"].get(cid, []))
                | set(meta["used_sprites"].get(cid, {}).keys())
                | ({"neutral"} if role in ("pov", "featured", "speaker") else set())
            )
            existing_files = [
                p.relative_to(self._book_dir).as_posix()
                for p in illus["char_all_files"].get(cid, [])
            ]
            cast_profiles.append(
                ChapterCastProfile(
                    entity_id=cid,
                    name=doc.name,
                    aliases=list(doc.aliases),
                    accent_color=accent_map.get(cid, "#6CE5E8"),
                    role=role,
                    dialogue_count=meta["dialogue_counts"].get(cid, 0),
                    detected_expressions=exprs,
                    existing_portraits=existing_files,
                )
            )

        resources: list[ReaderResourceRequirement] = []

        # 1. 封面与横幅检查（整书基础门面）
        def _real_in_source(rel: str) -> bool:
            cand = source_dir / rel
            return cand.is_file() and cand.stat().st_size > 0 and not is_placeholder_png(cand)

        cover_archive_rel = self.compute_archive_path("cover", settings=settings).relative_to(self._book_dir).as_posix()
        cover_ready = _real_in_source("assets/cover.png") or illus["cover"] is not None
        resources.append(
            ReaderResourceRequirement(
                asset_path="assets/cover.png",
                category="cover",
                title=f"《{self._config.title}》作品封面",
                status="ready" if cover_ready else "missing",
                recommended_size="900x1200 或 1200x1600 (3:4 竖版)",
                reason="书架卡片主视觉（manifest.cover_asset 必填）",
                existing_source_path=(
                    illus["cover"].relative_to(self._book_dir).as_posix()
                    if illus["cover"] is not None
                    else ("source/assets/cover.png" if _real_in_source("assets/cover.png") else None)
                ),
                archive_target_path=cover_archive_rel,
                recommended_skill="dd-scene-cg-illustrator",
            )
        )
        banner_archive_rel = self.compute_archive_path("banner", settings=settings).relative_to(self._book_dir).as_posix()
        banner_ready = _real_in_source("assets/banner.png") or illus["banner"] is not None
        resources.append(
            ReaderResourceRequirement(
                asset_path="assets/banner.png",
                category="banner",
                title=f"《{self._config.title}》详情页横幅",
                status="ready" if banner_ready else "missing",
                recommended_size="1600x900 或 1920x800 (16:9 宽幅)",
                reason="作品详情页顶部横幅（manifest.banner_asset）",
                existing_source_path=(
                    illus["banner"].relative_to(self._book_dir).as_posix()
                    if illus["banner"] is not None
                    else ("source/assets/banner.png" if _real_in_source("assets/banner.png") else None)
                ),
                archive_target_path=banner_archive_rel,
                recommended_skill="dd-scene-cg-illustrator",
            )
        )

        # 2. 场景背景图需求
        loc_by_slug = {
            _safe_ascii_id(loc.id, prefix="bg"): loc for loc in meta.get("locations", [])
        }
        for bg_asset, bg_info in meta["used_backgrounds"].items():
            bg_slug = Path(bg_asset).stem.lower()
            loc_doc = loc_by_slug.get(bg_slug)
            matched_bg = illus.get("catalog_map", {}).get(bg_asset) or illus["backgrounds"].get(bg_slug)
            bg_ready = _real_in_source(bg_asset) or matched_bg is not None
            bg_archive_rel = self.compute_archive_path(
                "background",
                entity_id=loc_doc.id if loc_doc is not None else None,
                chapter=chapter,
                label=bg_info["title"],
                settings=settings,
            ).relative_to(self._book_dir).as_posix()
            resources.append(
                ReaderResourceRequirement(
                    asset_path=bg_asset,
                    category="background",
                    title=f"场景背景 · {bg_info['title']}",
                    status="ready" if bg_ready else "missing",
                    recommended_size="1920x1080 (16:9) 或 1080x1920 (9:16)，配合 focal_point=[0.5, 0.35]",
                    reason=f"第 {chapter} 章舞台背景（氛围：{meta['weather']['label']}）",
                    entity_id=loc_doc.id if loc_doc is not None else None,
                    unlock_gallery_id=bg_info["id"],
                    existing_source_path=(
                        matched_bg.relative_to(self._book_dir).as_posix()
                        if matched_bg is not None
                        else (f"source/{bg_asset}" if _real_in_source(bg_asset) else None)
                    ),
                    archive_target_path=bg_archive_rel,
                    recommended_skill="dd-scene-cg-illustrator",
                )
            )

        # 3. 角色立绘、表情差分、动作差分、不同装扮与说话人微徽章
        expr_labels = {k: lbl for k, lbl, _ in _EXPRESSION_RULES}
        expr_labels["neutral"] = "常态"
        pose_labels = {k: lbl for k, lbl, _ in _POSE_RULES}
        outfit_labels = {k: lbl for k, lbl, _ in _OUTFIT_RULES}

        # 按角色采集本章提及的动作与换装段落文本
        char_context_text: dict[str, str] = {}
        for cp in cast_profiles:
            doc = settings[cp.entity_id]
            terms = [t.strip() for t in doc.all_names() if len(t.strip()) >= 2]
            matched_blocks = [
                blk["text"]
                for blk in chapter_doc["blocks"]
                if any(t in blk["text"] for t in terms)
                or (isinstance(blk.get("speaker"), dict) and blk["speaker"].get("id") == cp.entity_id)
            ]
            char_context_text[cp.entity_id] = "\n".join(matched_blocks)

        for cp in cast_profiles:
            if cp.role == "mentioned" and not cp.detected_expressions:
                continue
            char_map = illus["char_sprites"].get(cp.entity_id, {})
            has_any_base = bool(cp.existing_portraits)
            base_path_str = cp.existing_portraits[0] if cp.existing_portraits else None
            c_text = char_context_text.get(cp.entity_id, "")

            # 3.1 常规装扮 + 丰富表情差分（整合对白与叙述中的神态描写；主角/核心出场角色至少规划 3 个表情差分）
            expr_list = list(cp.detected_expressions or ["neutral"])
            if "neutral" not in expr_list:
                expr_list.insert(0, "neutral")
            for eid_rule, _elbl_rule, epat_rule in _EXPRESSION_RULES:
                if eid_rule not in expr_list and epat_rule.search(c_text):
                    expr_list.append(eid_rule)
            if cp.role in ("pov", "featured") and len(expr_list) < 3:
                for extra_expr in ("smile", "blush", "tender", "determined"):
                    if extra_expr not in expr_list:
                        expr_list.append(extra_expr)
                    if len(expr_list) >= 3:
                        break

            for expr in expr_list:
                sp_asset = f"assets/sprites/{cp.entity_id}_default_{expr}.png"
                ugid = f"gal_{cp.entity_id}_default_{expr}"
                exact_file = illus.get("catalog_map", {}).get(sp_asset) or char_map.get(expr)
                sp_archive_rel = self.compute_archive_path(
                    "sprite",
                    entity_id=cp.entity_id,
                    outfit="常服",
                    expression=expr,
                    settings=settings,
                ).relative_to(self._book_dir).as_posix()
                if _real_in_source(sp_asset):
                    status: Literal["ready", "reuse_base", "missing"] = "ready"
                    ex_path: str | None = f"source/{sp_asset}"
                elif exact_file is not None:
                    status = "ready"
                    ex_path = exact_file.relative_to(self._book_dir).as_posix()
                elif has_any_base:
                    status = "reuse_base"
                    ex_path = base_path_str
                else:
                    status = "missing"
                    ex_path = None
                resources.append(
                    ReaderResourceRequirement(
                        asset_path=sp_asset,
                        category="sprite",
                        title=f"角色立绘 · {cp.name}（常服·{expr_labels.get(expr, expr)}表情差分）",
                        status=status,
                        recommended_size="1024x1600 或 1200x2000 透明底 PNG（脚底贴底 0.5,1.0，同角色统一尺寸）",
                        reason=(
                            f"本章{cp.role}角色（对白 {cp.dialogue_count} 句），用于对白软切展现「{expr_labels.get(expr, expr)}」微表情"
                        ),
                        entity_id=cp.entity_id,
                        expression=expr,
                        outfit="default",
                        unlock_gallery_id=ugid,
                        existing_source_path=ex_path,
                        archive_target_path=sp_archive_rel,
                        recommended_skill="dd-character-illustrator",
                    )
                )

            # 3.2 动作差分立绘（根据本章人物互动/动作描写自动提议，增强 2.5D 舞台灵动感）
            detected_poses = [
                pid for pid, _plbl, pat in _POSE_RULES if pat.search(c_text)
            ]
            for pose_id in detected_poses[:2]:
                pose_lbl = pose_labels.get(pose_id, pose_id)
                pose_expr = expr_list[1] if len(expr_list) > 1 else "neutral"
                pose_asset = f"assets/sprites/{cp.entity_id}_default_{pose_id}_{pose_expr}.png"
                pose_ugid = f"gal_{cp.entity_id}_default_{pose_id}_{pose_expr}"
                matched_pose = illus.get("catalog_map", {}).get(pose_asset)
                pose_archive_rel = self.compute_archive_path(
                    "sprite",
                    entity_id=cp.entity_id,
                    outfit="常服",
                    pose=pose_id,
                    expression=pose_expr,
                    settings=settings,
                ).relative_to(self._book_dir).as_posix()
                if _real_in_source(pose_asset) or matched_pose is not None:
                    p_status: Literal["ready", "reuse_base", "missing"] = "ready"
                    p_ex = (
                        matched_pose.relative_to(self._book_dir).as_posix()
                        if matched_pose is not None
                        else f"source/{pose_asset}"
                    )
                elif has_any_base:
                    p_status = "reuse_base"
                    p_ex = base_path_str
                else:
                    p_status = "missing"
                    p_ex = None
                resources.append(
                    ReaderResourceRequirement(
                        asset_path=pose_asset,
                        category="sprite",
                        title=f"动作差分立绘 · {cp.name}（{pose_lbl}·{expr_labels.get(pose_expr, pose_expr)}）",
                        status=p_status,
                        recommended_size="1024x1600 或 1200x2000 透明底 PNG（传基础立绘参考锁脸与服装，仅换动态姿态）",
                        reason=f"正文检出「{pose_lbl}」动作描写，增加动作差分可大幅提升阅读器舞台生动度",
                        entity_id=cp.entity_id,
                        expression=pose_expr,
                        outfit="default",
                        pose=pose_id,
                        unlock_gallery_id=pose_ugid,
                        existing_source_path=p_ex,
                        archive_target_path=pose_archive_rel,
                        recommended_skill="dd-character-illustrator",
                    )
                )

            # 3.3 不同装扮差分（若正文检出换装/礼服/居家服/制服/作战服/斗篷/战损）
            detected_outfits = [
                oid for oid, _olbl, pat in _OUTFIT_RULES if pat.search(c_text)
            ]
            for outfit_id in detected_outfits[:2]:
                outfit_lbl = outfit_labels.get(outfit_id, outfit_id)
                outfit_asset = f"assets/sprites/{cp.entity_id}_{outfit_id}_neutral.png"
                outfit_ugid = f"gal_{cp.entity_id}_{outfit_id}_neutral"
                matched_outfit = illus.get("catalog_map", {}).get(outfit_asset)
                outfit_archive_rel = self.compute_archive_path(
                    "sprite",
                    entity_id=cp.entity_id,
                    outfit=outfit_lbl,
                    expression="neutral",
                    settings=settings,
                ).relative_to(self._book_dir).as_posix()
                if _real_in_source(outfit_asset) or matched_outfit is not None:
                    o_status: Literal["ready", "reuse_base", "missing"] = "ready"
                    o_ex = (
                        matched_outfit.relative_to(self._book_dir).as_posix()
                        if matched_outfit is not None
                        else f"source/{outfit_asset}"
                    )
                elif has_any_base:
                    o_status = "reuse_base"
                    o_ex = base_path_str
                else:
                    o_status = "missing"
                    o_ex = None
                resources.append(
                    ReaderResourceRequirement(
                        asset_path=outfit_asset,
                        category="sprite",
                        title=f"换装立绘 · {cp.name}（{outfit_lbl}装扮）",
                        status=o_status,
                        recommended_size="1024x1600 或 1200x2000 透明底 PNG（保持脸/发型/身材一致，收录画廊换装组）",
                        reason=f"正文检出「{outfit_lbl}」装扮场景，可在阅读器鉴赏室与百科解锁新换装条目",
                        entity_id=cp.entity_id,
                        expression="neutral",
                        outfit=outfit_id,
                        unlock_gallery_id=outfit_ugid,
                        existing_source_path=o_ex,
                        archive_target_path=outfit_archive_rel,
                        recommended_skill="dd-character-illustrator",
                    )
                )

            portrait_asset = f"assets/portraits/{cp.entity_id}_badge.png"
            matched_avatar = illus.get("catalog_map", {}).get(portrait_asset) or illus.get("char_avatars", {}).get(cp.entity_id)
            portrait_ready = _real_in_source(portrait_asset) or matched_avatar is not None
            portrait_archive_rel = self.compute_archive_path(
                "portrait",
                entity_id=cp.entity_id,
                settings=settings,
            ).relative_to(self._book_dir).as_posix()
            resources.append(
                ReaderResourceRequirement(
                    asset_path=portrait_asset,
                    category="portrait",
                    title=f"微徽章/百科头像 · {cp.name}",
                    status="ready" if portrait_ready else ("reuse_base" if has_any_base else "missing"),
                    recommended_size="256x256 (1:1 方形，可从立绘头部裁切)",
                    reason="对白说话人圆形微徽章（speaker.badge_asset）与时空百科档案头像（portrait_asset）",
                    entity_id=cp.entity_id,
                    existing_source_path=(
                        matched_avatar.relative_to(self._book_dir).as_posix()
                        if matched_avatar is not None
                        else (f"source/{portrait_asset}" if _real_in_source(portrait_asset) else base_path_str)
                    ),
                    archive_target_path=portrait_archive_rel,
                    recommended_skill="dd-character-illustrator",
                )
            )

        # 4. 典藏剧情 CG / 魅力特写 / 情感瞬间 / 主角贴贴提案（多维度高赏心悦目 CG 矩阵）
        cg_moments: list[dict[str, Any]] = []
        used_cg_assets: set[str] = set()
        next_cg_num = 1
        if meta["cg_assets_used"]:
            for cg_item in meta["cg_assets_used"]:
                src_p = cg_item["source_file"]
                src_rel = src_p.relative_to(self._book_dir).as_posix()
                used_cg_assets.add(cg_item["asset"])
                next_cg_num += 1
                resources.append(
                    ReaderResourceRequirement(
                        asset_path=cg_item["asset"],
                        category="cg",
                        title=f"典藏 CG · {cg_item['title']}",
                        status="ready",
                        recommended_size="1920x1080 (16:9 横幅宽屏，破框全屏演出)",
                        reason="已在 04_创作/插画/场景CG/ 检出本章对应剧情 CG，可直接用于破框高潮页",
                        cg_kind="climax",
                        unlock_gallery_id=cg_item["id"],
                        existing_source_path=src_rel,
                        archive_target_path=src_rel,
                        recommended_skill="dd-scene-cg-illustrator",
                    )
                )
                cg_moments.append({"title": cg_item["title"], "asset": cg_item["asset"], "cg_kind": "climax", "status": "ready"})

        # 围绕四大高魅力维度挖掘本章可追加的精选 CG 提案（主角贴贴 / 角色魅力特写 / 情感瞬间 / 剧情高潮）
        cg_proposals: list[tuple[str, str, str, str]] = []  # (cg_kind, kind_tag, label, snippet)
        seen_cg_kinds: set[str] = set()
        text_blocks = [blk["text"] for blk in chapter_doc["blocks"] if blk["type"] != "scene_divider"]

        # 4.1 [主角贴贴] 亲密互动/双人羁绊
        intimacy_hit = next((t for t in text_blocks if _INTIMACY_CUE_RE.search(t)), None)
        if intimacy_hit:
            cg_proposals.append(("intimacy", "主角贴贴", "双人亲密贴贴互动", intimacy_hit[:80]))
            seen_cg_kinds.add("intimacy")
        elif len(cast_profiles) >= 2:
            lead_names = f"{cast_profiles[0].name}与{cast_profiles[1].name}"
            cg_proposals.append(
                (
                    "intimacy",
                    "主角贴贴",
                    f"{lead_names}近距离互动贴贴",
                    f"捕捉{lead_names}在本章中的近身交流、并肩靠拢或微妙肢体互动瞬间（可配 2 张表情差分）",
                )
            )
            seen_cg_kinds.add("intimacy")

        # 4.2 [角色魅力特写] 高光颜值/身材/逆光/换装特写
        charm_hit = next((t for t in text_blocks if _CHARM_CUE_RE.search(t)), None)
        lead_char_name = cast_profiles[0].name if cast_profiles else "核心角色"
        if charm_hit:
            cg_proposals.append(("charm_closeup", "角色魅力特写", f"{lead_char_name}魅力高光特写", charm_hit[:80]))
            seen_cg_kinds.add("charm_closeup")
        elif cast_profiles:
            cg_proposals.append(
                (
                    "charm_closeup",
                    "角色魅力特写",
                    f"{lead_char_name}光影魅力特写",
                    f"以清亮逆光与电影景深展现{lead_char_name}的精致五官、发丝与修长身材曲线",
                )
            )
            seen_cg_kinds.add("charm_closeup")

        # 4.3 [情感瞬间] 眼神交汇/心动/柔情/情绪波动
        emotion_hit = next((t for t in text_blocks if _EMOTION_CUE_RE.search(t)), None)
        if emotion_hit:
            cg_proposals.append(("emotion", "情感瞬间", "角色细腻情感特写", emotion_hit[:80]))
            seen_cg_kinds.add("emotion")

        # 4.4 [剧情高潮] 蓝图高潮节拍或剧烈剧情转折（若尚无已有高潮 CG 则补充）
        if not meta["cg_assets_used"]:
            climax_label: str | None = None
            climax_snippet: str | None = None
            if ch_src.blueprint is not None and ch_src.blueprint.beats:
                last_beat = ch_src.blueprint.beats[-1]
                climax_label = last_beat.title.strip() or f"节拍 {last_beat.index}"
                climax_snippet = last_beat.content.strip()[:80] or climax_label
            if climax_label is None:
                climax_hit = next((t for t in reversed(text_blocks) if _CLIMAX_CUE_RE.search(t)), None)
                if climax_hit:
                    climax_label = f"{ch_src.title} 高潮名场面"
                    climax_snippet = climax_hit[:80]
            if climax_label is None and text_blocks:
                climax_label = f"{ch_src.title} 核心场景"
                climax_snippet = text_blocks[-1][:80]
            if climax_label and climax_snippet:
                cg_proposals.append(("climax", "剧情高潮", climax_label, climax_snippet))
                seen_cg_kinds.add("climax")

        for cg_kind, kind_tag, label, snippet in cg_proposals[:4]:
            cg_id = f"cg_{ch_src.chapter_id}_{next_cg_num:02d}"
            cg_asset = f"assets/cgs/{cg_id}.png"
            while cg_asset in used_cg_assets:
                next_cg_num += 1
                cg_id = f"cg_{ch_src.chapter_id}_{next_cg_num:02d}"
                cg_asset = f"assets/cgs/{cg_id}.png"
            used_cg_assets.add(cg_asset)
            next_cg_num += 1

            matched_cg = illus.get("catalog_map", {}).get(cg_asset)
            cg_ready = _real_in_source(cg_asset) or matched_cg is not None
            cg_archive_rel = self.compute_archive_path(
                "cg",
                chapter=chapter,
                volume_id=ch_src.volume_id,
                label=f"S{next_cg_num - 1:02d}_{kind_tag}_{label}",
                settings=settings,
            ).relative_to(self._book_dir).as_posix()
            resources.append(
                ReaderResourceRequirement(
                    asset_path=cg_asset,
                    category="cg",
                    title=f"推荐 CG [{kind_tag}] · {label}",
                    status="ready" if cg_ready else "missing",
                    recommended_size="1920x1080 (16:9 横幅宽屏，破框全屏演出；可选 2~3 张表情差分)",
                    reason=f"[{kind_tag}] 推荐作为 break_frame=true + standalone_page 收藏级插图（线索：{snippet}）",
                    cg_kind=cg_kind,
                    unlock_gallery_id=cg_id,
                    existing_source_path=(
                        matched_cg.relative_to(self._book_dir).as_posix()
                        if matched_cg is not None
                        else (f"source/{cg_asset}" if _real_in_source(cg_asset) else None)
                    ),
                    archive_target_path=cg_archive_rel,
                    recommended_skill="dd-scene-cg-illustrator",
                )
            )
            cg_moments.append(
                {
                    "title": f"[{kind_tag}] {label}",
                    "asset": cg_asset,
                    "cg_kind": cg_kind,
                    "snippet": snippet,
                    "status": "ready" if cg_ready else "missing",
                }
            )

        # 5. 识别潜在叙事道具卡线索
        card_candidates: list[dict[str, str]] = []
        for style, style_label, pat in _CARD_HINT_RULES:
            if pat.search(ch_src.text):
                card_candidates.append({"style": style, "label": style_label})

        stage_outline = {
            "initial_background": chapter_doc["initial_stage"]["background"]["asset"],
            "initial_weather_fx": meta["weather"],
            "initial_sprites": [s["id"] for s in chapter_doc["initial_stage"]["sprites"]],
            "total_blocks": len(chapter_doc["blocks"]),
            "dialogue_blocks": sum(1 for b in chapter_doc["blocks"] if b["type"] == "dialogue"),
            "narration_blocks": sum(1 for b in chapter_doc["blocks"] if b["type"] == "narration"),
            "diegetic_card_blocks": sum(1 for b in chapter_doc["blocks"] if b["type"] == "diegetic_card"),
            "scene_dividers": sum(1 for b in chapter_doc["blocks"] if b["type"] == "scene_divider"),
            "cg_moments": cg_moments,
            "diegetic_card_candidates": card_candidates,
        }

        summary_md = self._render_chapter_plan_markdown(
            ch_src=ch_src,
            cast_profiles=cast_profiles,
            resources=resources,
            stage_outline=stage_outline,
        )

        saved_plan_path: str | None = None
        if save_plan:
            plan_dir = out_root / "设计方案"
            plan_dir.mkdir(parents=True, exist_ok=True)
            plan_file = plan_dir / f"第{chapter:04d}章_伴读设计与资源需求.md"
            plan_file.write_text(summary_md, encoding="utf-8")
            saved_plan_path = plan_file.as_posix()

        return ChapterResourcePlan(
            chapter=ch_src.chapter,
            chapter_id=ch_src.chapter_id,
            volume_id=ch_src.volume_id,
            title=ch_src.title,
            source_text_path=ch_src.source_path.relative_to(self._book_dir).as_posix(),
            word_count=len(re.sub(r"\s+", "", ch_src.text)),
            cast=cast_profiles,
            resources=resources,
            stage_outline=stage_outline,
            summary_markdown=summary_md,
            saved_plan_path=saved_plan_path,
        )

    def plan_book_resources(
        self,
        *,
        output_dir: Path | str | None = None,
        include_drafts: bool = False,
        save_plan: bool = False,
    ) -> list[ChapterResourcePlan]:
        """为当前书库全部可用章节生成资源需求规划列表。"""
        chapters = self._collect_chapters(include_drafts=include_drafts)
        if not chapters:
            raise FileNotFoundError(
                "未找到可规划的章节正文（请确认 04_创作/定稿/ 有定稿，或传入 include_drafts=True）"
            )
        return [
            self.plan_chapter_resources(
                ch.chapter,
                output_dir=output_dir,
                include_drafts=include_drafts,
                save_plan=save_plan,
            )
            for ch in chapters
        ]

    @staticmethod
    def _render_chapter_plan_markdown(
        *,
        ch_src: _ChapterSource,
        cast_profiles: Sequence[ChapterCastProfile],
        resources: Sequence[ReaderResourceRequirement],
        stage_outline: Mapping[str, Any],
    ) -> str:
        status_badge = {
            "ready": "✅ 已有可复用",
            "reuse_base": "🔄 已有基础立绘（待绘差分/裁切）",
            "missing": "❌ 缺失待绘",
        }
        lines = [
            f"# {ch_src.title} · DDNovelReader 伴读演出与资源需求清单",
            "",
            f"- **章节 ID**: `{ch_src.chapter_id}`（第 {ch_src.chapter} 章 / 分卷 `{ch_src.volume_id}`）",
            f"- **正文来源**: `{ch_src.source_path.name}`（{'草稿' if ch_src.is_draft else '定稿'}）",
            f"- **块结构预估**: 共 {stage_outline['total_blocks']} 块（旁白 {stage_outline['narration_blocks']} / 对白 {stage_outline['dialogue_blocks']} / 道具卡 {stage_outline['diegetic_card_blocks']} / 切场 {stage_outline['scene_dividers']}）",
            f"- **建议开篇氛围**: 背景 `{stage_outline['initial_background']}` · 特效 `{stage_outline['initial_weather_fx']['type']}` ({stage_outline['initial_weather_fx']['label']})",
            "",
            "## 1. 出场角色与神态差分分析",
            "",
        ]
        if cast_profiles:
            lines.append("| 角色 ID | 名称 | 定位 | 对白句数 | 建议表情差分 | 已有立绘 |")
            lines.append("| :--- | :--- | :---: | :---: | :--- | :--- |")
            for cp in cast_profiles:
                expr_str = ", ".join(cp.detected_expressions) or "neutral"
                ex_str = f"{len(cp.existing_portraits)} 张" if cp.existing_portraits else "无"
                lines.append(
                    f"| `{cp.entity_id}` | {cp.name} | {cp.role} | {cp.dialogue_count} | `{expr_str}` | {ex_str} |"
                )
        else:
            lines.append("（本章未检出已建卡出场角色，可按纯场景与旁白方式编排。）")

        lines.extend(
            [
                "",
                "## 2. 视觉资源需求清单（立绘 / 场景 / CG / 头像 / 封面）",
                "",
                "| 类别 | 资源名称 | 项目归档真源路径 (`04_创作/插画/`) | 作品包内路径 | 当前状态 | 推荐规格 | 建议协同技能 | 说明 |",
                "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
            ]
        )
        for req in resources:
            lines.append(
                f"| `{req.category}` | {req.title} | `{req.archive_target_path or '-'}` | `{req.asset_path}` "
                f"| {status_badge[req.status]} | {req.recommended_size} | `{req.recommended_skill or '-'}` | {req.reason} |"
            )

        if stage_outline.get("diegetic_card_candidates"):
            lines.extend(["", "## 3. 可选叙事道具卡建议", ""])
            for cand in stage_outline["diegetic_card_candidates"]:
                lines.append(f"- 检出 **{cand['label']}** 线索，可将对应段落精修为 `diegetic_card` (`style: \"{cand['style']}\"`)。")

        lines.extend(
            [
                "",
                "## 4. 建议下一步执行路径",
                "",
                "1. **优先补齐并项目内归档**：所有生成的立绘、头像、场景背景、剧情 CG、封面与横幅，必须先通过 `novel_reader_archive_asset`（或 `ddnovel reader archive`）归档落盘至上表对应的项目目录 `04_创作/插画/...` 并登记 `资产清单.yaml`，严禁仅留在临时缓存目录；",
                "2. **先占位预演**：如需先看分页与 2.5D 舞台节奏，可执行 `ddnovel reader export -c "
                f"{ch_src.chapter}` 或调用 `ddbook_generate_placeholder_assets` 生成轻量合法 RGBA 占位图；",
                "3. **精修与真机验证**：通过 `ddnovel-reader` MCP (`ddbook_upsert_chapter` / `ddbook_preview_stage` / `ddbook_preview_codex` / `ddbook_validate_target(strict=true)`) 精修镜头与防剧透锚点，最后打包 `.ddbook` 或增量 `.ddpatch`。",
                "",
            ]
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Gallery & Codex 构建（严格防撞键 + 零剧透水位线锚点）
    # ------------------------------------------------------------------

    def _build_gallery(
        self,
        *,
        chapter_docs: Mapping[int, dict[str, Any]],
        settings: Mapping[str, SettingDoc],
        accent_map: Mapping[str, str],
        chapter_metas: Mapping[int, dict[str, Any]],
        existing_gallery: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        cgs_by_id: dict[str, dict[str, Any]] = {}
        scenes_by_id: dict[str, dict[str, Any]] = {}
        chars_by_id: dict[str, dict[str, Any]] = {}

        # 先保留已有画廊元数据（单章增量模式下不丢失其他章节自定义条目）
        if isinstance(existing_gallery, Mapping):
            for cg in existing_gallery.get("cgs", []):
                if isinstance(cg, dict) and cg.get("id"):
                    cgs_by_id[str(cg["id"])] = dict(cg)
            for sc in existing_gallery.get("scenes", []):
                if isinstance(sc, dict) and sc.get("id"):
                    scenes_by_id[str(sc["id"])] = dict(sc)
            for ch_entry in existing_gallery.get("characters", []):
                if isinstance(ch_entry, dict) and ch_entry.get("id"):
                    chars_by_id[str(ch_entry["id"])] = json.loads(json.dumps(ch_entry))

        # 合并本次导出的全部章节元数据
        for ch_num in sorted(chapter_metas):
            meta = chapter_metas[ch_num]
            ch_doc = chapter_docs[ch_num]
            ch_id = ch_doc["chapter_id"]

            for cg_item in meta["cg_assets_used"]:
                cgs_by_id[cg_item["id"]] = {
                    "id": cg_item["id"],
                    "title": cg_item["title"],
                    "description": cg_item["description"],
                    "asset": cg_item["asset"],
                    "thumbnail_asset": cg_item["asset"],
                    "media_type": "image",
                    "chapter_id": ch_id,
                    "unlock_hint": f"阅读「{ch_doc['title']}」破框高潮页解锁",
                    "tags": ["CG", ch_doc["title"]],
                }

            for bg_info in meta["used_backgrounds"].values():
                sid = bg_info["id"]
                if sid not in scenes_by_id:
                    scenes_by_id[sid] = {
                        "id": sid,
                        "title": bg_info["title"],
                        "description": bg_info["description"],
                        "asset": bg_info["asset"],
                        "thumbnail_asset": bg_info["asset"],
                        "media_type": "image",
                        "chapter_id": ch_id,
                        "unlock_hint": f"阅读「{ch_doc['title']}」解锁",
                        "variants": [],
                    }

            for cid, expr_map in meta["used_sprites"].items():
                sdoc = settings.get(cid)
                cname = sdoc.name if sdoc else cid
                cdesc = (sdoc.core_text.strip() if sdoc else "")[:160] or f"{cname}角色立绘。"
                if cid not in chars_by_id:
                    chars_by_id[cid] = {
                        "id": cid,
                        "name": cname,
                        "accent_color": accent_map.get(cid, "#6CE5E8"),
                        "description": cdesc,
                        "unlock_hint": f"在「{ch_doc['title']}」首次登场解锁",
                        "outfits": [],
                    }
                char_entry = chars_by_id[cid]
                outfits = char_entry.setdefault("outfits", [])
                outfit_id = f"{cid}_default"
                outfit = next((o for o in outfits if o.get("id") == outfit_id), None)
                if outfit is None:
                    outfit = {
                        "id": outfit_id,
                        "label": "经典常服",
                        "unlock_gallery_id": f"gal_{cid}_default",
                        "unlock_hint": f"{cname}常服立绘",
                        "expressions": [],
                    }
                    outfits.append(outfit)
                existing_exprs = {e.get("id"): e for e in outfit.setdefault("expressions", []) if isinstance(e, dict)}
                for expr_id, info in expr_map.items():
                    if expr_id not in existing_exprs:
                        outfit["expressions"].append(
                            {
                                "id": expr_id,
                                "label": info["label"],
                                "asset": info["asset"],
                                "render_kind": "static_diff",
                                "unlock_gallery_id": info["unlock_gallery_id"],
                            }
                        )

        return {
            "schema_version": SCHEMA_VERSION,
            "cgs": list(cgs_by_id.values()),
            "scenes": list(scenes_by_id.values()),
            "characters": list(chars_by_id.values()),
        }

    def _build_codex(
        self,
        *,
        all_chapters: Mapping[int, dict[str, Any]],
        settings: Mapping[str, SettingDoc],
        accent_map: Mapping[str, str],
        relations: Sequence[TemporalRelation],
        portraits_map: Mapping[str, str],
    ) -> dict[str, Any]:
        """构建严格防剧透时空百科：仅收录在已导出章节中出现过的实体，并精确绑定 0-based 解锁锚点。"""
        # 1. 扫描所有已导出章节，计算每个实体的最早出场水位线 (chapter_index, block_index, chapter_id, block_id)
        first_seen: dict[str, tuple[int, int, str, str]] = {}
        for ch_idx in sorted(all_chapters):
            ch_doc = all_chapters[ch_idx]
            ch_id = str(ch_doc["chapter_id"])
            blocks = ch_doc.get("blocks", [])
            if not blocks:
                continue
            first_block_id = str(blocks[0]["id"])
            # initial_stage 登场立绘在 block 0 即解锁
            for sp in ch_doc.get("initial_stage", {}).get("sprites", []):
                sid = sp.get("id")
                if sid in settings and sid not in first_seen:
                    first_seen[sid] = (ch_idx, 0, ch_id, first_block_id)
            for bi, blk in enumerate(blocks):
                bid = str(blk["id"])
                cand_ids: list[str] = []
                if isinstance(blk.get("speaker"), dict) and blk["speaker"].get("id"):
                    cand_ids.append(str(blk["speaker"]["id"]))
                for ref in blk.get("entity_refs", []):
                    cand_ids.append(str(ref))
                spatch = blk.get("stage_patch")
                if isinstance(spatch, dict):
                    for key in ("sprites", "sprites_upsert"):
                        for sp in spatch.get(key, []):
                            if isinstance(sp, dict) and sp.get("id"):
                                cand_ids.append(str(sp["id"]))
                for eid in cand_ids:
                    if eid in settings and eid not in first_seen:
                        first_seen[eid] = (ch_idx, bi, ch_id, bid)

        # 2. 构建 CodexEntity（绝不泄露 secret_text / ## [机密]！）
        entities: list[dict[str, Any]] = []
        included_ids: set[str] = set()
        for eid in sorted(first_seen):
            doc = settings[eid]
            ch_idx, b_idx, ch_id, b_id = first_seen[eid]
            ctype = _CATEGORY_TO_CODEX_TYPE.get(doc.category, "lore")
            color = (
                accent_map.get(eid, _CODEX_TYPE_DEFAULT_COLOR["character"])
                if ctype == "character"
                else _CODEX_TYPE_DEFAULT_COLOR.get(ctype, "#A78BFA")
            )
            summary = _strip_markdown_plain(doc.core_text) or f"{doc.name}档案。"
            snapshot: dict[str, Any] = {
                "unlock_at": {
                    "chapter_index": ch_idx,
                    "block_index": b_idx,
                    "chapter_id": ch_id,
                    "block_id": b_id,
                },
                "title": doc.name,
                "summary": summary[:420],
                "attributes": {
                    "分类": doc.category.value,
                    "状态": doc.status.value,
                },
            }
            if doc.tags:
                snapshot["tags"] = [t for t in doc.tags if t.strip()]
            if eid in portraits_map:
                snapshot["portrait_asset"] = portraits_map[eid]

            entity_entry: dict[str, Any] = {
                "id": eid,
                "type": ctype,
                "name": doc.name,
                "accent_color": color,
                "snapshots": [snapshot],
            }
            valid_aliases = [a for a in doc.aliases if a.strip()]
            if valid_aliases:
                entity_entry["aliases"] = valid_aliases
            entities.append(entity_entry)
            included_ids.add(eid)

        # 3. 构建防剧透双时态关系边（仅当两端实体均已解锁且有效章号存在）
        codex_relations: list[dict[str, Any]] = []
        sorted_ch_indices = sorted(all_chapters)
        seen_rel_ids: set[str] = set()
        for r_idx, rel in enumerate(relations):
            if rel.source not in included_ids or rel.target not in included_ids:
                continue
            src_seen = first_seen[rel.source]
            tgt_seen = first_seen[rel.target]
            min_ch = max(rel.valid_from_ch, src_seen[0], tgt_seen[0])
            # 找到 >= min_ch 的首个已导出章节作为解锁锚点（避免指向尚未导出章节触发告警）
            anchor_ch = next((c for c in sorted_ch_indices if c >= min_ch), None)
            if anchor_ch is None:
                continue
            anchor_doc = all_chapters[anchor_ch]
            anchor_blocks = anchor_doc.get("blocks", [])
            if not anchor_blocks:
                continue
            anchor_b_idx = 0
            if anchor_ch == src_seen[0]:
                anchor_b_idx = max(anchor_b_idx, src_seen[1])
            if anchor_ch == tgt_seen[0]:
                anchor_b_idx = max(anchor_b_idx, tgt_seen[1])
            anchor_b_idx = min(anchor_b_idx, len(anchor_blocks) - 1)

            attitude = "neutral"
            rlabel = rel.relation.strip()
            if re.search(r"敌|仇|杀|对手|死敌|宿敌|背叛", rlabel):
                attitude = "hostile"
            elif re.search(r"亲|父|母|兄|弟|姐|妹|师|徒|挚友|恋人|道侣", rlabel):
                attitude = "kin"
            elif re.search(r"盟|同伴|伙伴|朋友|搭档|护卫|信任|部下", rlabel):
                attitude = "ally"
            elif re.search(r"未知|试探|神秘|疑|暗中", rlabel):
                attitude = "mystery"

            rel_id = f"rel_{rel.source}_{rel.target}"
            if rel_id in seen_rel_ids:
                rel_id = f"{rel_id}_{r_idx + 1}"
            seen_rel_ids.add(rel_id)

            codex_relations.append(
                {
                    "id": rel_id,
                    "from_entity_id": rel.source,
                    "to_entity_id": rel.target,
                    "directed": True,
                    "snapshots": [
                        {
                            "unlock_at": {
                                "chapter_index": anchor_ch,
                                "block_index": anchor_b_idx,
                                "chapter_id": str(anchor_doc["chapter_id"]),
                                "block_id": str(anchor_blocks[anchor_b_idx]["id"]),
                            },
                            "label": rlabel,
                            "attitude": attitude,
                            "description": f"{settings[rel.source].name}与{settings[rel.target].name}的关系：{rlabel}。",
                        }
                    ],
                }
            )

        return {
            "schema_version": SCHEMA_VERSION,
            "entities": entities,
            "relations": codex_relations,
        }

    # ------------------------------------------------------------------
    # 资产同步与占位图补齐
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_all_referenced_assets(
        manifest: Mapping[str, Any],
        chapters: Mapping[int, Mapping[str, Any]],
        gallery: Mapping[str, Any],
        codex: Mapping[str, Any],
    ) -> set[str]:
        refs: set[str] = set()
        for key in ("cover_asset", "banner_asset"):
            if isinstance(manifest.get(key), str):
                refs.add(manifest[key])
        for vol in manifest.get("volumes", []):
            if isinstance(vol.get("cover_asset"), str):
                refs.add(vol["cover_asset"])

        for ch in chapters.values():
            init = ch.get("initial_stage", {})
            if isinstance(init.get("background"), dict) and init["background"].get("asset"):
                refs.add(init["background"]["asset"])
            if isinstance(init.get("cg"), dict) and init["cg"].get("asset"):
                refs.add(init["cg"]["asset"])
            for sp in init.get("sprites", []):
                if isinstance(sp, dict) and sp.get("asset"):
                    refs.add(sp["asset"])
            for blk in ch.get("blocks", []):
                if isinstance(blk.get("speaker"), dict) and blk["speaker"].get("badge_asset"):
                    refs.add(blk["speaker"]["badge_asset"])
                patch = blk.get("stage_patch")
                if isinstance(patch, dict):
                    if isinstance(patch.get("background"), dict) and patch["background"].get("asset"):
                        refs.add(patch["background"]["asset"])
                    if isinstance(patch.get("cg"), dict) and patch["cg"].get("asset"):
                        refs.add(patch["cg"]["asset"])
                    for k in ("sprites", "sprites_upsert"):
                        for sp in patch.get(k, []):
                            if isinstance(sp, dict) and sp.get("asset"):
                                refs.add(sp["asset"])

        for cg in gallery.get("cgs", []):
            for k in ("asset", "thumbnail_asset"):
                if isinstance(cg.get(k), str):
                    refs.add(cg[k])
        for sc in gallery.get("scenes", []):
            for k in ("asset", "thumbnail_asset"):
                if isinstance(sc.get(k), str):
                    refs.add(sc[k])
            for var in sc.get("variants", []):
                if isinstance(var.get("asset"), str):
                    refs.add(var["asset"])
        for ch_entry in gallery.get("characters", []):
            for out in ch_entry.get("outfits", []):
                for expr in out.get("expressions", []):
                    if isinstance(expr.get("asset"), str):
                        refs.add(expr["asset"])

        for ent in codex.get("entities", []):
            for snap in ent.get("snapshots", []):
                if isinstance(snap.get("portrait_asset"), str):
                    refs.add(snap["portrait_asset"])
        return refs

    def _materialize_assets(
        self,
        source_dir: Path,
        *,
        referenced_assets: set[str],
        illus: Mapping[str, Any],
        chapter_metas: Mapping[int, Mapping[str, Any]],
        accent_map: Mapping[str, str],
        default_accent: str,
        generate_placeholders: bool,
    ) -> tuple[list[str], list[str], list[dict[str, Any]], list[dict[str, Any]]]:
        copied_assets: list[str] = []
        generated_placeholders: list[str] = []
        ready_resources: list[dict[str, Any]] = []
        missing_resources: list[dict[str, Any]] = []

        # 建立可自动拷贝的真实插画映射表（资产台账最高优先，其次各分类归档目录）
        real_source_map: dict[str, Path] = dict(illus.get("catalog_map", {}))
        if illus.get("cover") is not None:
            real_source_map.setdefault("assets/cover.png", illus["cover"])
        if illus.get("banner") is not None:
            real_source_map.setdefault("assets/banner.png", illus["banner"])
        for cid, avatar_path in illus.get("char_avatars", {}).items():
            real_source_map.setdefault(f"assets/portraits/{cid}_badge.png", avatar_path)

        for meta in chapter_metas.values():
            for cg_item in meta.get("cg_assets_used", []):
                real_source_map.setdefault(cg_item["asset"], cg_item["source_file"])
            for cid, expr_map in meta.get("used_sprites", {}).items():
                char_map = illus.get("char_sprites", {}).get(cid, {})
                fallback_base = (
                    illus.get("char_all_files", {}).get(cid, [None])[0]
                    if illus.get("char_all_files", {}).get(cid)
                    else None
                )
                for expr_id, info in expr_map.items():
                    matched = char_map.get(expr_id) or (char_map.get("neutral") if expr_id == "neutral" else None) or fallback_base
                    if matched is not None:
                        real_source_map.setdefault(info["asset"], matched)
            for bg_asset in meta.get("used_backgrounds", {}):
                slug = Path(bg_asset).stem.lower()
                if slug in illus.get("backgrounds", {}):
                    real_source_map.setdefault(bg_asset, illus["backgrounds"][slug])

        for asset_rel in sorted(referenced_assets):
            dest = source_dir / asset_rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if asset_rel in real_source_map:
                src_img = real_source_map[asset_rel]
                shutil.copy2(src_img, dest)
                copied_assets.append(asset_rel)
                ready_resources.append(
                    {
                        "asset_path": asset_rel,
                        "source": src_img.relative_to(self._book_dir).as_posix(),
                        "placeholder": False,
                    }
                )
            elif dest.is_file() and dest.stat().st_size > 0 and not is_placeholder_png(dest):
                ready_resources.append(
                    {
                        "asset_path": asset_rel,
                        "source": f"source/{asset_rel}",
                        "placeholder": False,
                    }
                )
            else:
                missing_info = {
                    "asset_path": asset_rel,
                    "placeholder_generated": False,
                }
                if generate_placeholders and asset_rel.lower().endswith(".png"):
                    # 匹配角色主题色
                    tint = default_accent
                    for cid, color in accent_map.items():
                        if cid in asset_rel:
                            tint = color
                            break
                    dest.write_bytes(_generate_placeholder_png(asset_rel, tint))
                    generated_placeholders.append(asset_rel)
                    missing_info["placeholder_generated"] = True
                missing_resources.append(missing_info)

        return copied_assets, generated_placeholders, ready_resources, missing_resources

    # ------------------------------------------------------------------
    # 对外导出入口：全书导出 & 单章导出/增量合并
    # ------------------------------------------------------------------

    def export_book(
        self,
        *,
        output_dir: Path | str | None = None,
        include_drafts: bool = False,
        pack_ddbook: bool = True,
        patch_from: Path | str | None = None,
        generate_placeholders: bool = True,
        strict_validate: bool = True,
        reader_dir: Path | str | None = None,
        book_id: str | None = None,
        revision: int | None = None,
        accent_color: str = "#6CE5E8",
        volume_id: str | None = None,
        start_chapter: int | None = None,
        end_chapter: int | None = None,
    ) -> ReaderExportResult:
        """将当前书库全部可用章节一键导出为 DDNovelReader 作品源目录及 `.ddbook` / `.ddpatch`。"""
        chapters = self._collect_chapters(
            include_drafts=include_drafts,
            volume_id=volume_id,
            start_chapter=start_chapter,
            end_chapter=end_chapter,
        )
        if not chapters:
            raise FileNotFoundError(
                "未找到可导出的章节正文（请确认 04_创作/定稿/ 有定稿，或指定 include_drafts=True 包含草稿）"
            )

        out_root = (
            Path(output_dir).resolve()
            if output_dir is not None
            else resolve_reader_pack_dir(self._book_dir, for_write=True)
        )
        source_dir = out_root / "source"
        source_dir.mkdir(parents=True, exist_ok=True)

        # 若 source/assets/ 中已放入手工/模型直写的真实图片（非占位图），自动反向归档至项目 04_创作/插画/
        if (source_dir / "assets").is_dir():
            self.sync_assets_to_archive(source_dir=source_dir)

        settings = self._load_settings()
        volumes = self._load_volumes()
        relations = self._load_relations()
        accent_map = self._char_accent_map(settings)
        illus = self._scan_existing_illustrations(settings)

        resolved_book_id = _safe_ascii_id(book_id or self._config.title or self._book_dir.name, prefix="book")
        existing_manifest_path = source_dir / "manifest.json"
        existing_rev = 0
        if existing_manifest_path.is_file():
            try:
                old_m = json.loads(existing_manifest_path.read_text(encoding="utf-8"))
                existing_rev = int(old_m.get("revision", 0))
            except Exception:
                existing_rev = 0
        resolved_revision = revision if revision is not None else max(1, existing_rev)

        chapter_docs: dict[int, dict[str, Any]] = {}
        chapter_metas: dict[int, dict[str, Any]] = {}
        portraits_map: dict[str, str] = {}
        volumes_entries: dict[str, dict[str, Any]] = {}

        for ch_src in chapters:
            vdoc = volumes.get(ch_src.volume_id)
            vol_title = (
                vdoc.title.strip()
                if vdoc is not None and vdoc.title.strip()
                else f"第{ch_src.volume_id.split('_')[-1]}卷"
            )
            ch_doc, meta = self._build_chapter_document(
                ch_src,
                settings=settings,
                accent_map=accent_map,
                illus=illus,
                volume_title=vol_title,
                existing_source_dir=source_dir,
            )
            chapter_docs[ch_src.chapter] = ch_doc
            chapter_metas[ch_src.chapter] = meta
            portraits_map.update(meta["used_portraits"])

            ch_rel_file = f"chapters/{ch_src.chapter_id}.json"
            _write_json_strict(source_dir / ch_rel_file, ch_doc, allow_stage_patch_nulls=True)

            vol_bucket = volumes_entries.setdefault(
                ch_src.volume_id,
                {
                    "volume_id": ch_src.volume_id,
                    "title": vol_title,
                    "chapters": [],
                },
            )
            ch_entry: dict[str, Any] = {
                "chapter_id": ch_src.chapter_id,
                "index": ch_src.chapter,
                "title": ch_src.title,
                "file": ch_rel_file,
                "word_count": len(re.sub(r"\s+", "", ch_src.text)),
            }
            if ch_src.summary:
                ch_entry["summary"] = ch_src.summary
            vol_bucket["chapters"].append(ch_entry)

        # 清理旧的未列在 manifest 中的孤儿章节 JSON，防止触发 UNLISTED_CHAPTER_FILE 警告
        declared_ch_files = {f"{ch_doc['chapter_id']}.json" for ch_doc in chapter_docs.values()}
        chapters_dir = source_dir / "chapters"
        if chapters_dir.is_dir():
            for existing_ch_file in chapters_dir.glob("*.json"):
                if existing_ch_file.name not in declared_ch_files:
                    existing_ch_file.unlink()

        now_iso = _utc_now_iso()
        manifest: dict[str, Any] = {
            "protocol_version": PROTOCOL_VERSION,
            "book_id": resolved_book_id,
            "revision": resolved_revision,
            "title": self._config.title,
            "author": self._config.author.strip() or "匿名作者",
            "description": self._load_book_description(),
            "cover_asset": "assets/cover.png",
            "banner_asset": "assets/banner.png",
            "accent_color": accent_color,
            "volumes": [volumes_entries[vid] for vid in sorted(volumes_entries)],
            "gallery_manifest": "gallery/index.json",
            "codex_manifest": "codex/entities.json",
            "tags": [self._config.genre_profile, "DDNovelGenerater"],
            "language": "zh-CN",
            "reading_direction": "ltr",
            "created_at": now_iso,
            "updated_at": now_iso,
        }

        gallery = self._build_gallery(
            chapter_docs=chapter_docs,
            settings=settings,
            accent_map=accent_map,
            chapter_metas=chapter_metas,
        )
        codex = self._build_codex(
            all_chapters=chapter_docs,
            settings=settings,
            accent_map=accent_map,
            relations=relations,
            portraits_map=portraits_map,
        )

        _write_json_strict(source_dir / "manifest.json", manifest)
        _write_json_strict(source_dir / "gallery" / "index.json", gallery)
        _write_json_strict(source_dir / "codex" / "entities.json", codex)

        referenced_assets = self._collect_all_referenced_assets(manifest, chapter_docs, gallery, codex)
        copied, placeholders, ready_res, missing_res = self._materialize_assets(
            source_dir,
            referenced_assets=referenced_assets,
            illus=illus,
            chapter_metas=chapter_metas,
            accent_map=accent_map,
            default_accent=accent_color,
            generate_placeholders=generate_placeholders,
        )

        validation = validate_reader_target(
            source_dir,
            strict=strict_validate,
            reader_dir=reader_dir,
        )
        ddbook_path_str: str | None = None
        ddpatch_path_str: str | None = None

        if pack_ddbook:
            out_ddbook = out_root / f"{resolved_book_id}.ddbook"
            packed_file, post_val = pack_reader_book(
                source_dir,
                out_ddbook,
                strict=strict_validate,
                skip_validate=not strict_validate and not validation.ok,
                reader_dir=reader_dir,
            )
            ddbook_path_str = packed_file.as_posix()
            if post_val is not None:
                validation = post_val

        if patch_from is not None:
            out_patch = out_root / f"{resolved_book_id}_rev{resolved_revision + 1}.ddpatch"
            patch_file, _ = build_reader_patch(
                patch_from,
                source_dir,
                out_patch,
                summary=f"全书同步更新（共 {len(chapter_docs)} 章）",
                auto_bump_revision=True,
                strict=strict_validate,
                reader_dir=reader_dir,
            )
            ddpatch_path_str = patch_file.as_posix()

        return ReaderExportResult(
            mode="full",
            book_id=resolved_book_id,
            revision=resolved_revision,
            source_dir=source_dir.as_posix(),
            ddbook_path=ddbook_path_str,
            ddpatch_path=ddpatch_path_str,
            exported_chapters=sorted(chapter_docs),
            copied_assets=copied,
            generated_placeholders=placeholders,
            missing_resources=missing_res,
            ready_resources=ready_res,
            validation=validation.to_dict(),
            reader_tool_used=validation.tool,
        )

    def export_chapter(
        self,
        chapter: int,
        *,
        output_dir: Path | str | None = None,
        include_drafts: bool = True,
        pack_ddbook: bool = False,
        patch_from: Path | str | None = None,
        generate_placeholders: bool = True,
        strict_validate: bool = True,
        reader_dir: Path | str | None = None,
        chapter_override: Mapping[str, Any] | None = None,
        book_id: str | None = None,
        accent_color: str = "#6CE5E8",
    ) -> ReaderExportResult:
        """单独导出或增量更新指定一章至 DDNovelReader 作品目录（保留已有其他章节）。"""
        chapters = self._collect_chapters(
            include_drafts=include_drafts,
            target_chapter=chapter,
        )
        if not chapters:
            raise FileNotFoundError(
                f"未找到第 {chapter} 章的定稿或草稿正文（请检查 04_创作/定稿/ 或 04_创作/草稿/）"
            )
        ch_src = chapters[0]

        out_root = (
            Path(output_dir).resolve()
            if output_dir is not None
            else resolve_reader_pack_dir(self._book_dir, for_write=True)
        )
        source_dir = out_root / "source"
        source_dir.mkdir(parents=True, exist_ok=True)

        # 若 source/assets/ 中已放入手工/模型直写的真实图片（非占位图），自动反向归档至项目 04_创作/插画/
        if (source_dir / "assets").is_dir():
            self.sync_assets_to_archive(source_dir=source_dir)

        settings = self._load_settings()
        volumes = self._load_volumes()
        relations = self._load_relations()
        accent_map = self._char_accent_map(settings)
        illus = self._scan_existing_illustrations(settings)

        vdoc = volumes.get(ch_src.volume_id)
        vol_title = (
            vdoc.title.strip()
            if vdoc is not None and vdoc.title.strip()
            else f"第{ch_src.volume_id.split('_')[-1]}卷"
        )

        # 生成或采用覆盖章节 JSON
        ch_doc, meta = self._build_chapter_document(
            ch_src,
            settings=settings,
            accent_map=accent_map,
            illus=illus,
            volume_title=vol_title,
            existing_source_dir=source_dir,
        )
        if chapter_override is not None:
            ch_doc = dict(chapter_override)

        ch_rel_file = f"chapters/{ch_src.chapter_id}.json"
        _write_json_strict(source_dir / ch_rel_file, ch_doc, allow_stage_patch_nulls=True)

        # 加载或初始化 manifest.json（增量合并本章节，不覆盖其他已存在的章节）
        manifest_path = source_dir / "manifest.json"
        now_iso = _utc_now_iso()
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            resolved_book_id = str(manifest.get("book_id") or _safe_ascii_id(book_id or self._config.title, prefix="book"))
            resolved_revision = int(manifest.get("revision", 1))
            manifest["updated_at"] = now_iso
        else:
            resolved_book_id = _safe_ascii_id(book_id or self._config.title or self._book_dir.name, prefix="book")
            resolved_revision = 1
            manifest = {
                "protocol_version": PROTOCOL_VERSION,
                "book_id": resolved_book_id,
                "revision": resolved_revision,
                "title": self._config.title,
                "author": self._config.author.strip() or "匿名作者",
                "description": self._load_book_description(),
                "cover_asset": "assets/cover.png",
                "banner_asset": "assets/banner.png",
                "accent_color": accent_color,
                "volumes": [],
                "gallery_manifest": "gallery/index.json",
                "codex_manifest": "codex/entities.json",
                "tags": [self._config.genre_profile, "DDNovelGenerater"],
                "language": "zh-CN",
                "reading_direction": "ltr",
                "created_at": now_iso,
                "updated_at": now_iso,
            }

        # 更新 manifest.volumes[] 中目标章条目
        volumes_list = list(manifest.get("volumes", []))
        target_vol = next((v for v in volumes_list if v.get("volume_id") == ch_src.volume_id), None)
        if target_vol is None:
            target_vol = {
                "volume_id": ch_src.volume_id,
                "title": vol_title,
                "chapters": [],
            }
            volumes_list.append(target_vol)

        # 移除同一章号/chapter_id 的旧条目后重新插入并按 index 排序
        for vol in volumes_list:
            vol["chapters"] = [
                c
                for c in vol.get("chapters", [])
                if c.get("chapter_id") != ch_src.chapter_id and c.get("index") != ch_src.chapter
            ]

        ch_entry: dict[str, Any] = {
            "chapter_id": ch_src.chapter_id,
            "index": ch_src.chapter,
            "title": str(ch_doc.get("title") or ch_src.title),
            "file": ch_rel_file,
            "word_count": len(re.sub(r"\s+", "", ch_src.text)),
        }
        if ch_src.summary:
            ch_entry["summary"] = ch_src.summary
        target_vol["chapters"].append(ch_entry)
        target_vol["chapters"].sort(key=lambda c: int(c.get("index", 0)))
        manifest["volumes"] = sorted(
            [v for v in volumes_list if v.get("chapters")],
            key=lambda v: str(v.get("volume_id", "")),
        )

        # 读取全部已注册章节以保持 gallery & codex 跨章一致
        all_chapter_docs: dict[int, dict[str, Any]] = {}
        portraits_map: dict[str, str] = dict(meta["used_portraits"])
        for vol in manifest["volumes"]:
            for c_item in vol["chapters"]:
                c_path = source_dir / c_item["file"]
                if c_path.is_file():
                    loaded_ch = json.loads(c_path.read_text(encoding="utf-8"))
                    c_idx = int(loaded_ch["chapter_index"])
                    all_chapter_docs[c_idx] = loaded_ch
                    for blk in loaded_ch.get("blocks", []):
                        sp = blk.get("speaker")
                        if isinstance(sp, dict) and sp.get("id") and sp.get("badge_asset"):
                            portraits_map[str(sp["id"])] = str(sp["badge_asset"])

        existing_gallery_path = source_dir / "gallery" / "index.json"
        existing_gallery = (
            json.loads(existing_gallery_path.read_text(encoding="utf-8"))
            if existing_gallery_path.is_file()
            else None
        )
        gallery = self._build_gallery(
            chapter_docs={ch_src.chapter: ch_doc},
            settings=settings,
            accent_map=accent_map,
            chapter_metas={ch_src.chapter: meta},
            existing_gallery=existing_gallery,
        )
        codex = self._build_codex(
            all_chapters=all_chapter_docs,
            settings=settings,
            accent_map=accent_map,
            relations=relations,
            portraits_map=portraits_map,
        )

        _write_json_strict(manifest_path, manifest)
        _write_json_strict(source_dir / "gallery" / "index.json", gallery)
        _write_json_strict(source_dir / "codex" / "entities.json", codex)

        referenced_assets = self._collect_all_referenced_assets(manifest, all_chapter_docs, gallery, codex)
        copied, placeholders, ready_res, missing_res = self._materialize_assets(
            source_dir,
            referenced_assets=referenced_assets,
            illus=illus,
            chapter_metas={ch_src.chapter: meta},
            accent_map=accent_map,
            default_accent=accent_color,
            generate_placeholders=generate_placeholders,
        )

        validation = validate_reader_target(
            source_dir,
            strict=strict_validate,
            reader_dir=reader_dir,
        )
        ddbook_path_str: str | None = None
        ddpatch_path_str: str | None = None

        if pack_ddbook:
            out_ddbook = out_root / f"{resolved_book_id}.ddbook"
            packed_file, post_val = pack_reader_book(
                source_dir,
                out_ddbook,
                strict=strict_validate,
                skip_validate=not strict_validate and not validation.ok,
                reader_dir=reader_dir,
            )
            ddbook_path_str = packed_file.as_posix()
            if post_val is not None:
                validation = post_val

        if patch_from is not None:
            out_patch = out_root / f"{resolved_book_id}_{ch_src.chapter_id}.ddpatch"
            patch_file, _ = build_reader_patch(
                patch_from,
                source_dir,
                out_patch,
                summary=f"单章设计更新：{ch_src.title}",
                auto_bump_revision=True,
                strict=strict_validate,
                reader_dir=reader_dir,
            )
            ddpatch_path_str = patch_file.as_posix()

        return ReaderExportResult(
            mode="chapter",
            book_id=resolved_book_id,
            revision=resolved_revision,
            source_dir=source_dir.as_posix(),
            ddbook_path=ddbook_path_str,
            ddpatch_path=ddpatch_path_str,
            exported_chapters=[ch_src.chapter],
            copied_assets=copied,
            generated_placeholders=placeholders,
            missing_resources=missing_res,
            ready_resources=ready_res,
            validation=validation.to_dict(),
            reader_tool_used=validation.tool,
        )


__all__ = [
    "DDNOVEL_READER_DIR_ENV_VAR",
    "ArchivedAssetResult",
    "ChapterCastProfile",
    "ChapterResourcePlan",
    "ReaderExportResult",
    "ReaderExporter",
    "ReaderResourceRequirement",
    "ReaderValidationResult",
    "SyncArchiveResult",
    "build_reader_patch",
    "discover_reader_dir",
    "is_placeholder_png",
    "pack_reader_book",
    "resolve_reader_mcp_command",
    "resolve_reader_mcp_script",
    "resolve_reader_tool_script",
    "validate_reader_target",
]
