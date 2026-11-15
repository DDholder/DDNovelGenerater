"""小说书库中文目录与文件名解析器（兼顾旧版英文路径向后兼容）。

提供四维真源目录、配置文件、设定卡片、结构蓝图、动态状态与创作产物的统一路径解析：
- 新建与初始化默认输出全中文目录与文件名（如 ``小说配置.yaml``、``01_设定集/``、``第0001章.md``）；
- 读取或向已有旧版英文书库写入时自动兼容 ``novel.yaml``、``01_settings/``、``ch_0001.md`` 等路径。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final, Literal

import yaml

from ddnovel.core.schemas._base import SettingCategory

# ---------------------------------------------------------------------------
# 1. 目录与文件名常量（中文主命名 + 旧版英文兼容）
# ---------------------------------------------------------------------------

ZH_CONFIG_FILENAME: Final[str] = "小说配置.yaml"
LEGACY_CONFIG_FILENAME: Final[str] = "novel.yaml"

ZH_AUTHOR_NOTES_DIR: Final[str] = "00_作者设定"
LEGACY_AUTHOR_NOTES_DIR: Final[str] = "00_author_notes"
ZH_AUTHOR_NOTES_README_FILENAME: Final[str] = "创作随笔与原稿说明.md"

ZH_SETTINGS_DIR: Final[str] = "01_设定集"
LEGACY_SETTINGS_DIR: Final[str] = "01_settings"

ZH_STRUCTURE_DIR: Final[str] = "02_结构"
LEGACY_STRUCTURE_DIR: Final[str] = "02_structure"

ZH_STATE_DIR: Final[str] = "03_状态"
LEGACY_STATE_DIR: Final[str] = "03_state"

ZH_CREATION_DIR: Final[str] = "04_创作"
LEGACY_CREATION_DIR: Final[str] = "04_creation"

SETTING_SUBDIR_MAP: Final[dict[SettingCategory, tuple[str, str]]] = {
    SettingCategory.WORLD: ("世界观", "world"),
    SettingCategory.RULE: ("世界观", "world"),
    SettingCategory.GEOGRAPHY: ("地理", "geography"),
    SettingCategory.FACTION: ("势力", "factions"),
    SettingCategory.CHARACTER: ("人物", "characters"),
    SettingCategory.ITEM: ("器物", "items"),
    SettingCategory.OTHER: ("其他", "other"),
}

ZH_MASTER_OUTLINE_FILENAME: Final[str] = "总纲.md"
LEGACY_MASTER_OUTLINE_FILENAME: Final[str] = "master_outline.md"

ZH_VOLUMES_SUBDIR: Final[str] = "分卷"
LEGACY_VOLUMES_SUBDIR: Final[str] = "volumes"

ZH_ARCS_SUBDIR: Final[str] = "单元"
LEGACY_ARCS_SUBDIR: Final[str] = "arcs"

ZH_BLUEPRINTS_SUBDIR: Final[str] = "章节蓝图"
LEGACY_BLUEPRINTS_SUBDIR: Final[str] = "blueprints"

ZH_ACTIVE_SUBDIR: Final[str] = "当前"
LEGACY_ACTIVE_SUBDIR: Final[str] = "active"

ZH_HISTORY_SUBDIR: Final[str] = "历史"
LEGACY_HISTORY_SUBDIR: Final[str] = "history"

ZH_ARCHIVE_SUBDIR: Final[str] = "归档"
LEGACY_ARCHIVE_SUBDIR: Final[str] = "archive"

ZH_CHARACTERS_STATE_FILENAME: Final[str] = "人物状态.yaml"
LEGACY_CHARACTERS_STATE_FILENAME: Final[str] = "characters.yaml"

ZH_FACTIONS_STATE_FILENAME: Final[str] = "势力状态.yaml"
LEGACY_FACTIONS_STATE_FILENAME: Final[str] = "factions.yaml"

ZH_RELATIONS_FILENAME: Final[str] = "人物关系.yaml"
LEGACY_RELATIONS_FILENAME: Final[str] = "relations.yaml"

ZH_THREADS_FILENAME: Final[str] = "伏笔台账.yaml"
LEGACY_THREADS_FILENAME: Final[str] = "threads.yaml"

ZH_WORLD_STATE_FILENAME: Final[str] = "世界状态.yaml"
LEGACY_WORLD_STATE_FILENAME: Final[str] = "world_state.yaml"

ZH_STYLE_GUIDE_FILENAME: Final[str] = "文风指南.md"
LEGACY_STYLE_GUIDE_FILENAME: Final[str] = "style_guide.md"

ZH_PACKETS_SUBDIR: Final[str] = "写作包"
LEGACY_PACKETS_SUBDIR: Final[str] = "packets"

ZH_DRAFTS_SUBDIR: Final[str] = "草稿"
LEGACY_DRAFTS_SUBDIR: Final[str] = "drafts"

ZH_MANUSCRIPT_SUBDIR: Final[str] = "定稿"
LEGACY_MANUSCRIPT_SUBDIR: Final[str] = "manuscript"

_CN_OR_ARABIC_NUM: Final[str] = r"[零〇一二三四五六七八九十百千万两\d]+"
_VOL_ID_RE: Final[re.Pattern[str]] = re.compile(r"^vol_(\d+)$")
_ARC_ID_RE: Final[re.Pattern[str]] = re.compile(r"^vol_(\d+)_arc_(\d+)$")

_VOL_STEM_NUM_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:vol_?0*(\d+)|第\s*0*(\d+)\s*卷)(?:$|[-_\s])",
    re.IGNORECASE,
)
_ARC_STEM_NUM_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:vol_?0*(\d+)_arc_?0*(\d+)|第\s*0*(\d+)\s*卷_第\s*0*(\d+)\s*单元)(?:$|[-_\s])",
    re.IGNORECASE,
)
_CH_STEM_NUM_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:ch_?0*(\d+)|第\s*0*(\d+)\s*[章回节])(?:$|[-_\s])",
    re.IGNORECASE,
)

_VOL_TITLE_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    rf"^(?:vol_?\d+|第\s*{_CN_OR_ARABIC_NUM}\s*卷)(?:$|[:：\-—_\s]+)",
    re.IGNORECASE,
)
_ARC_VOL_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    rf"^(?:vol_?\d+_?|第\s*{_CN_OR_ARABIC_NUM}\s*卷[\s_—\-:：]*)"
    rf"(?=(?:arc_?\d+|第\s*{_CN_OR_ARABIC_NUM}\s*(?:单元|幕|篇)))",
    re.IGNORECASE,
)
_ARC_TITLE_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    rf"^(?:arc_?\d+|第\s*{_CN_OR_ARABIC_NUM}\s*(?:单元|幕|篇))(?:$|[:：\-—_\s]+)",
    re.IGNORECASE,
)
_CH_TITLE_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    rf"^(?:ch_?\d+|第\s*{_CN_OR_ARABIC_NUM}\s*[章回节])(?:$|[:：\-—_\s]+)",
    re.IGNORECASE,
)
_NON_CHAPTER_STEM_SUFFIXES: Final[tuple[str, ...]] = ("_写作包", "_草稿", "_packet", "_draft")


# ---------------------------------------------------------------------------
# 2. 路径判定与兼容解析工具
# ---------------------------------------------------------------------------


def is_legacy_book(book_dir: Path | str) -> bool:
    """判断目标书库是否为尚未迁移的旧版英文目录结构。"""
    root = Path(book_dir)
    has_zh = (
        (root / ZH_CONFIG_FILENAME).exists()
        or (root / ZH_SETTINGS_DIR).exists()
        or (root / ZH_STRUCTURE_DIR).exists()
        or (root / ZH_STATE_DIR).exists()
        or (root / ZH_CREATION_DIR).exists()
    )
    if has_zh:
        return False
    return (
        (root / LEGACY_CONFIG_FILENAME).exists()
        or (root / LEGACY_SETTINGS_DIR).exists()
        or (root / LEGACY_STRUCTURE_DIR).exists()
        or (root / LEGACY_STATE_DIR).exists()
        or (root / LEGACY_CREATION_DIR).exists()
    )


def resolve_config_path(book_dir: Path | str, *, for_write: bool = False) -> Path:
    """解析书籍主配置文件路径（优先 ``小说配置.yaml``，兼容 ``novel.yaml``）。"""
    root = Path(book_dir)
    zh_path = root / ZH_CONFIG_FILENAME
    legacy_path = root / LEGACY_CONFIG_FILENAME
    if zh_path.is_file():
        return zh_path
    if legacy_path.is_file():
        return legacy_path
    if for_write and is_legacy_book(root):
        return legacy_path
    return zh_path


def _resolve_pair(
    book_dir: Path | str,
    zh_parts: tuple[str, ...],
    legacy_parts: tuple[str, ...],
    *,
    for_write: bool = False,
) -> Path:
    root = Path(book_dir)
    zh_path = root.joinpath(*zh_parts)
    if zh_path.exists():
        return zh_path
    legacy_path = root.joinpath(*legacy_parts)
    if legacy_path.exists():
        return legacy_path
    if for_write and is_legacy_book(root):
        return legacy_path
    return zh_path


def resolve_author_notes_root(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(
        book_dir,
        (ZH_AUTHOR_NOTES_DIR,),
        (LEGACY_AUTHOR_NOTES_DIR,),
        for_write=for_write,
    )


def resolve_settings_root(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(book_dir, (ZH_SETTINGS_DIR,), (LEGACY_SETTINGS_DIR,), for_write=for_write)


def resolve_setting_dir(
    book_dir: Path | str,
    category: SettingCategory,
    *,
    for_write: bool = False,
) -> Path:
    zh_sub, legacy_sub = SETTING_SUBDIR_MAP.get(category, ("其他", "other"))
    return _resolve_pair(
        book_dir,
        (ZH_SETTINGS_DIR, zh_sub),
        (LEGACY_SETTINGS_DIR, legacy_sub),
        for_write=for_write,
    )


def resolve_structure_root(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(book_dir, (ZH_STRUCTURE_DIR,), (LEGACY_STRUCTURE_DIR,), for_write=for_write)


def resolve_master_outline(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(
        book_dir,
        (ZH_STRUCTURE_DIR, ZH_MASTER_OUTLINE_FILENAME),
        (LEGACY_STRUCTURE_DIR, LEGACY_MASTER_OUTLINE_FILENAME),
        for_write=for_write,
    )


def resolve_volumes_dir(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(
        book_dir,
        (ZH_STRUCTURE_DIR, ZH_VOLUMES_SUBDIR),
        (LEGACY_STRUCTURE_DIR, LEGACY_VOLUMES_SUBDIR),
        for_write=for_write,
    )


def resolve_arcs_dir(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(
        book_dir,
        (ZH_STRUCTURE_DIR, ZH_ARCS_SUBDIR),
        (LEGACY_STRUCTURE_DIR, LEGACY_ARCS_SUBDIR),
        for_write=for_write,
    )


def resolve_blueprints_dir(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(
        book_dir,
        (ZH_STRUCTURE_DIR, ZH_BLUEPRINTS_SUBDIR),
        (LEGACY_STRUCTURE_DIR, LEGACY_BLUEPRINTS_SUBDIR),
        for_write=for_write,
    )


def resolve_state_root(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(book_dir, (ZH_STATE_DIR,), (LEGACY_STATE_DIR,), for_write=for_write)


def resolve_active_characters_file(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(
        book_dir,
        (ZH_STATE_DIR, ZH_ACTIVE_SUBDIR, ZH_CHARACTERS_STATE_FILENAME),
        (LEGACY_STATE_DIR, LEGACY_ACTIVE_SUBDIR, LEGACY_CHARACTERS_STATE_FILENAME),
        for_write=for_write,
    )


def resolve_active_factions_file(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(
        book_dir,
        (ZH_STATE_DIR, ZH_ACTIVE_SUBDIR, ZH_FACTIONS_STATE_FILENAME),
        (LEGACY_STATE_DIR, LEGACY_ACTIVE_SUBDIR, LEGACY_FACTIONS_STATE_FILENAME),
        for_write=for_write,
    )


def resolve_archive_dir(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(
        book_dir,
        (ZH_STATE_DIR, ZH_ARCHIVE_SUBDIR),
        (LEGACY_STATE_DIR, LEGACY_ARCHIVE_SUBDIR),
        for_write=for_write,
    )


def resolve_archive_characters_file(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(
        book_dir,
        (ZH_STATE_DIR, ZH_ARCHIVE_SUBDIR, ZH_CHARACTERS_STATE_FILENAME),
        (LEGACY_STATE_DIR, LEGACY_ARCHIVE_SUBDIR, LEGACY_CHARACTERS_STATE_FILENAME),
        for_write=for_write,
    )


def resolve_archive_factions_file(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(
        book_dir,
        (ZH_STATE_DIR, ZH_ARCHIVE_SUBDIR, ZH_FACTIONS_STATE_FILENAME),
        (LEGACY_STATE_DIR, LEGACY_ARCHIVE_SUBDIR, LEGACY_FACTIONS_STATE_FILENAME),
        for_write=for_write,
    )


def resolve_archive_relations_file(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(
        book_dir,
        (ZH_STATE_DIR, ZH_ARCHIVE_SUBDIR, ZH_RELATIONS_FILENAME),
        (LEGACY_STATE_DIR, LEGACY_ARCHIVE_SUBDIR, LEGACY_RELATIONS_FILENAME),
        for_write=for_write,
    )


def resolve_archive_threads_file(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(
        book_dir,
        (ZH_STATE_DIR, ZH_ARCHIVE_SUBDIR, ZH_THREADS_FILENAME),
        (LEGACY_STATE_DIR, LEGACY_ARCHIVE_SUBDIR, LEGACY_THREADS_FILENAME),
        for_write=for_write,
    )


def resolve_history_dir(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(
        book_dir,
        (ZH_STATE_DIR, ZH_HISTORY_SUBDIR),
        (LEGACY_STATE_DIR, LEGACY_HISTORY_SUBDIR),
        for_write=for_write,
    )


def resolve_relations_file(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(
        book_dir,
        (ZH_STATE_DIR, ZH_RELATIONS_FILENAME),
        (LEGACY_STATE_DIR, LEGACY_RELATIONS_FILENAME),
        for_write=for_write,
    )


def resolve_threads_file(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(
        book_dir,
        (ZH_STATE_DIR, ZH_THREADS_FILENAME),
        (LEGACY_STATE_DIR, LEGACY_THREADS_FILENAME),
        for_write=for_write,
    )


def resolve_world_state_file(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(
        book_dir,
        (ZH_STATE_DIR, ZH_WORLD_STATE_FILENAME),
        (LEGACY_STATE_DIR, LEGACY_WORLD_STATE_FILENAME),
        for_write=for_write,
    )


def resolve_creation_root(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(book_dir, (ZH_CREATION_DIR,), (LEGACY_CREATION_DIR,), for_write=for_write)


def resolve_style_guide_file(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(
        book_dir,
        (ZH_CREATION_DIR, ZH_STYLE_GUIDE_FILENAME),
        (LEGACY_CREATION_DIR, LEGACY_STYLE_GUIDE_FILENAME),
        for_write=for_write,
    )


def resolve_packets_dir(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(
        book_dir,
        (ZH_CREATION_DIR, ZH_PACKETS_SUBDIR),
        (LEGACY_CREATION_DIR, LEGACY_PACKETS_SUBDIR),
        for_write=for_write,
    )


def resolve_drafts_dir(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(
        book_dir,
        (ZH_CREATION_DIR, ZH_DRAFTS_SUBDIR),
        (LEGACY_CREATION_DIR, LEGACY_DRAFTS_SUBDIR),
        for_write=for_write,
    )


def resolve_manuscript_dir(book_dir: Path | str, *, for_write: bool = False) -> Path:
    return _resolve_pair(
        book_dir,
        (ZH_CREATION_DIR, ZH_MANUSCRIPT_SUBDIR),
        (LEGACY_CREATION_DIR, LEGACY_MANUSCRIPT_SUBDIR),
        for_write=for_write,
    )


# ---------------------------------------------------------------------------
# 3. 分卷 / 单元 / 章节文件名格式化与定位
# ---------------------------------------------------------------------------


def sanitize_filename_label(label: str | None, *, max_len: int = 40) -> str:
    """将对象名或标题清洗为跨平台安全的单段文件名后缀。"""
    if label is None:
        return ""
    text = re.sub(r'[\x00-\x1f\x7f"]', "", str(label))
    text = re.sub(r"[<>:/\\|?*\s]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._- ")
    if not text:
        return ""
    if max_len > 0 and len(text) > max_len:
        text = text[:max_len].rstrip("._- ")
    return text


def extract_clean_title(
    title: str | None,
    kind: Literal["volume", "arc", "chapter"],
) -> str:
    """去除分卷/单元/章节标题中的冗余编号前缀并做安全文件名清洗。"""
    if not title:
        return ""
    text = str(title).strip()
    if kind == "volume":
        text = _VOL_TITLE_PREFIX_RE.sub("", text, count=1)
    elif kind == "arc":
        text = _ARC_VOL_PREFIX_RE.sub("", text, count=1)
        text = _ARC_TITLE_PREFIX_RE.sub("", text, count=1)
    elif kind == "chapter":
        text = _CH_TITLE_PREFIX_RE.sub("", text, count=1)
    return sanitize_filename_label(text)


def format_setting_stem(
    entity_id: str,
    name: str | None = None,
    *,
    legacy: bool = False,
) -> str:
    """生成设定卡文件名 stem（新版为 ``<id>-<对象名>``，旧版英文书库保留 ``<id>``）。"""
    clean_id = entity_id.strip()
    if legacy:
        return clean_id
    clean_name = sanitize_filename_label(name)
    if not clean_name or clean_name.lower() == clean_id.lower():
        return clean_id
    if clean_name.lower().startswith(f"{clean_id.lower()}-"):
        return clean_name
    return f"{clean_id}-{clean_name}"


def format_volume_stem(
    vol_id: str,
    *,
    title: str | None = None,
    legacy: bool = False,
) -> str:
    if legacy:
        return vol_id
    m = _VOL_ID_RE.match(vol_id.strip())
    base = f"第{m.group(1)}卷" if m else vol_id
    clean = extract_clean_title(title, "volume") if title else ""
    return f"{base}-{clean}" if clean else base


def format_arc_stem(
    arc_id: str,
    *,
    title: str | None = None,
    legacy: bool = False,
) -> str:
    if legacy:
        return arc_id
    m = _ARC_ID_RE.match(arc_id.strip())
    base = f"第{m.group(1)}卷_第{m.group(2)}单元" if m else arc_id
    clean = extract_clean_title(title, "arc") if title else ""
    return f"{base}-{clean}" if clean else base


def format_chapter_stem(
    chapter: int,
    width: int = 4,
    *,
    title: str | None = None,
    legacy: bool = False,
) -> str:
    if legacy:
        return f"ch_{chapter:0{width}d}"
    base = f"第{chapter:0{width}d}章"
    clean = extract_clean_title(title, "chapter") if title else ""
    return f"{base}-{clean}" if clean else base


def infer_volume_id_from_stem(stem: str) -> str:
    """从分卷文件名 stem（如 ``第01卷-破局立锚`` 或 ``vol_01``）反推标准 ``vol_XX``。"""
    clean = stem.strip()
    if not _ARC_STEM_NUM_RE.match(clean):
        m = _VOL_STEM_NUM_RE.match(clean)
        if m:
            num = int(m.group(1) or m.group(2))
            if num >= 1:
                return f"vol_{num:02d}"
    return clean


def infer_arc_id_from_stem(stem: str) -> str:
    """从单元文件名 stem（如 ``第01卷_第01单元-暗夜启程``）反推标准 ``vol_XX_arc_YY``。"""
    clean = stem.strip()
    m = _ARC_STEM_NUM_RE.match(clean)
    if m:
        vol_num = int(m.group(1) or m.group(3))
        arc_num = int(m.group(2) or m.group(4))
        if vol_num >= 1 and arc_num >= 1:
            return f"vol_{vol_num:02d}_arc_{arc_num:02d}"
    return clean


def _extract_vol_num(vol_id_or_stem: str) -> int | None:
    clean = vol_id_or_stem.strip()
    m_id = _VOL_ID_RE.match(clean)
    if m_id:
        return int(m_id.group(1))
    if _ARC_STEM_NUM_RE.match(clean):
        return None
    m_stem = _VOL_STEM_NUM_RE.match(clean)
    if m_stem:
        return int(m_stem.group(1) or m_stem.group(2))
    return None


def _extract_arc_nums(arc_id_or_stem: str) -> tuple[int, int] | None:
    clean = arc_id_or_stem.strip()
    m_id = _ARC_ID_RE.match(clean)
    if m_id:
        return int(m_id.group(1)), int(m_id.group(2))
    m_stem = _ARC_STEM_NUM_RE.match(clean)
    if m_stem:
        return int(m_stem.group(1) or m_stem.group(3)), int(m_stem.group(2) or m_stem.group(4))
    return None


def _matches_volume_stem(stem: str, vol_num: int) -> bool:
    if _ARC_STEM_NUM_RE.match(stem):
        return False
    m = _VOL_STEM_NUM_RE.match(stem)
    if not m:
        return False
    return int(m.group(1) or m.group(2)) == vol_num


def _matches_arc_stem(stem: str, vol_num: int, arc_num: int) -> bool:
    m = _ARC_STEM_NUM_RE.match(stem)
    if not m:
        return False
    return int(m.group(1) or m.group(3)) == vol_num and int(m.group(2) or m.group(4)) == arc_num


def _matches_chapter_stem(stem: str, chapter: int) -> bool:
    if stem.endswith(_NON_CHAPTER_STEM_SUFFIXES):
        return False
    m = _CH_STEM_NUM_RE.match(stem)
    if not m:
        return False
    return int(m.group(1) or m.group(2)) == chapter


def _read_frontmatter_id(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    clean = head.lstrip("\ufeff")
    if not clean.startswith("---"):
        return None
    lines = clean.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            end_idx = idx
            break
    if end_idx is None:
        return None
    try:
        parsed = yaml.safe_load("\n".join(lines[1:end_idx]))
    except yaml.YAMLError:
        return None
    if isinstance(parsed, dict) and parsed.get("id") is not None:
        return str(parsed["id"]).strip()
    return None


def find_existing_setting_file(book_dir: Path | str, entity_id: str) -> Path | None:
    """在 ``01_设定集/``（及旧版 ``01_settings/``）中定位既有设定卡片路径。"""
    root = Path(book_dir)
    target = entity_id.strip().lower()
    if not target:
        return None

    candidates: list[Path] = []
    for base in (root / ZH_SETTINGS_DIR, root / LEGACY_SETTINGS_DIR):
        if base.is_dir():
            candidates.extend(sorted(p for p in base.rglob("*.md") if p.is_file()))

    prefix = f"{target}-"
    for cand in candidates:
        stem_lower = cand.stem.lower()
        if stem_lower == target or stem_lower.startswith(prefix):
            return cand

    for cand in candidates:
        fm_id = _read_frontmatter_id(cand)
        if fm_id is not None and fm_id.lower() == target:
            return cand
    return None


def resolve_setting_file(
    book_dir: Path | str,
    entity_id: str,
    category: SettingCategory | str,
    *,
    name: str | None = None,
    for_write: bool = False,
) -> Path:
    """解析设定卡文件路径（读取优先命中既有卡，写入支持 ``<id>-<对象名>.md``）。"""
    root = Path(book_dir)
    cat_enum = category if isinstance(category, SettingCategory) else SettingCategory(category)
    legacy_mode = is_legacy_book(root)
    existing = find_existing_setting_file(root, entity_id)

    if not for_write:
        if existing is not None:
            return existing
        s_dir = resolve_setting_dir(root, cat_enum, for_write=False)
        return s_dir / f"{format_setting_stem(entity_id, name, legacy=legacy_mode)}.md"

    target_dir = resolve_setting_dir(root, cat_enum, for_write=True)
    if not legacy_mode and name is not None and sanitize_filename_label(name):
        return target_dir / f"{format_setting_stem(entity_id, name, legacy=False)}.md"
    if not legacy_mode and name is None and existing is not None:
        return target_dir / existing.name
    return target_dir / f"{format_setting_stem(entity_id, name, legacy=legacy_mode)}.md"


def resolve_volume_file(
    book_dir: Path | str,
    vol_id: str,
    *,
    title: str | None = None,
    for_write: bool = False,
) -> Path:
    root = Path(book_dir)
    zh_bare = f"{format_volume_stem(vol_id, legacy=False)}.md"
    legacy_name = f"{vol_id}.md"
    vol_num = _extract_vol_num(vol_id)

    if not for_write or title is None:
        bases = (
            root / ZH_STRUCTURE_DIR / ZH_VOLUMES_SUBDIR,
            root / LEGACY_STRUCTURE_DIR / LEGACY_VOLUMES_SUBDIR,
        )
        for base in bases:
            for name in (zh_bare, legacy_name):
                cand = base / name
                if cand.is_file():
                    return cand
        if vol_num is not None:
            for base in bases:
                if base.is_dir():
                    for cand in sorted(base.glob("*.md")):
                        if cand.is_file() and _matches_volume_stem(cand.stem, vol_num):
                            return cand

    legacy_mode = is_legacy_book(root)
    vols_dir = resolve_volumes_dir(root, for_write=for_write)
    target_stem = format_volume_stem(vol_id, title=title, legacy=legacy_mode)
    return vols_dir / f"{target_stem}.md"


def resolve_arc_file(
    book_dir: Path | str,
    arc_id: str,
    *,
    title: str | None = None,
    for_write: bool = False,
) -> Path:
    root = Path(book_dir)
    zh_bare = f"{format_arc_stem(arc_id, legacy=False)}.md"
    legacy_name = f"{arc_id}.md"
    arc_nums = _extract_arc_nums(arc_id)

    if not for_write or title is None:
        bases = (
            root / ZH_STRUCTURE_DIR / ZH_ARCS_SUBDIR,
            root / LEGACY_STRUCTURE_DIR / LEGACY_ARCS_SUBDIR,
        )
        for base in bases:
            for name in (zh_bare, legacy_name):
                cand = base / name
                if cand.is_file():
                    return cand
        if arc_nums is not None:
            v_num, a_num = arc_nums
            for base in bases:
                if base.is_dir():
                    for cand in sorted(base.glob("*.md")):
                        if cand.is_file() and _matches_arc_stem(cand.stem, v_num, a_num):
                            return cand

    legacy_mode = is_legacy_book(root)
    arcs_dir = resolve_arcs_dir(root, for_write=for_write)
    target_stem = format_arc_stem(arc_id, title=title, legacy=legacy_mode)
    return arcs_dir / f"{target_stem}.md"


def resolve_volume_subdir(parent_dir: Path, vol_id: str, *, legacy: bool = False) -> Path:
    zh_sub = parent_dir / format_volume_stem(vol_id, legacy=False)
    legacy_sub = parent_dir / vol_id
    if zh_sub.is_dir():
        return zh_sub
    if legacy_sub.is_dir():
        return legacy_sub
    return legacy_sub if legacy else zh_sub


def _iter_volume_subdirs(bp_or_ms_dir: Path, vol_id: str, primary: Path) -> list[Path]:
    seen: list[Path] = []
    for cand in (
        primary,
        bp_or_ms_dir / format_volume_stem(vol_id, legacy=False),
        bp_or_ms_dir / vol_id,
    ):
        if cand not in seen:
            seen.append(cand)
    return seen


def find_existing_blueprint_file(
    book_dir: Path | str,
    chapter: int,
    width: int = 4,
) -> Path | None:
    """在 ``02_结构/章节蓝图/``（及旧版目录）扫描指定章号的既有蓝图文件。"""
    del width  # 兼容可选宽参数签名，匹配任意零填充宽度
    root = Path(book_dir)
    for base in (
        root / ZH_STRUCTURE_DIR / ZH_BLUEPRINTS_SUBDIR,
        root / LEGACY_STRUCTURE_DIR / LEGACY_BLUEPRINTS_SUBDIR,
    ):
        if not base.is_dir():
            continue
        for cand in sorted(base.rglob("*.md")):
            if cand.is_file() and _matches_chapter_stem(cand.stem, chapter):
                return cand
    return None


def resolve_blueprint_file(
    book_dir: Path | str,
    vol_id: str,
    chapter: int,
    width: int = 4,
    *,
    title: str | None = None,
    for_write: bool = False,
) -> Path:
    root = Path(book_dir)
    legacy_mode = is_legacy_book(root)
    bp_dir = resolve_blueprints_dir(root, for_write=for_write)
    vol_dir = resolve_volume_subdir(bp_dir, vol_id, legacy=legacy_mode)
    zh_bare = f"{format_chapter_stem(chapter, width, legacy=False)}.md"
    legacy_bare = f"{format_chapter_stem(chapter, width, legacy=True)}.md"

    if not for_write or title is None:
        for cand_dir in _iter_volume_subdirs(bp_dir, vol_id, vol_dir):
            for name in (zh_bare, legacy_bare):
                if (cand_dir / name).is_file():
                    return cand_dir / name
            if cand_dir.is_dir():
                for cand in sorted(cand_dir.glob("*.md")):
                    if cand.is_file() and _matches_chapter_stem(cand.stem, chapter):
                        return cand
        if not for_write:
            any_found = find_existing_blueprint_file(root, chapter, width=width)
            if any_found is not None:
                return any_found

    target_stem = format_chapter_stem(chapter, width, title=title, legacy=legacy_mode)
    return vol_dir / f"{target_stem}.md"


def resolve_packet_file(
    book_dir: Path | str,
    chapter: int,
    width: int = 4,
    *,
    for_write: bool = False,
) -> Path:
    root = Path(book_dir)
    packets_dir = resolve_packets_dir(root, for_write=for_write)
    zh_candidates = (
        packets_dir / f"{format_chapter_stem(chapter, width, legacy=False)}_写作包.md",
        packets_dir / f"第{chapter:04d}章_写作包.md",
        packets_dir / f"第{chapter}章_写作包.md",
    )
    legacy_candidates = (
        packets_dir / f"ch_{chapter:0{width}d}_packet.md",
        packets_dir / f"ch_{chapter:04d}_packet.md",
        packets_dir / f"ch_{chapter}_packet.md",
    )
    for cand in (*zh_candidates, *legacy_candidates):
        if cand.is_file():
            return cand
    legacy_mode = is_legacy_book(root)
    return legacy_candidates[0] if legacy_mode else zh_candidates[0]


def resolve_draft_file(
    book_dir: Path | str,
    chapter: int,
    width: int = 4,
    *,
    for_write: bool = False,
) -> Path:
    root = Path(book_dir)
    drafts_dir = resolve_drafts_dir(root, for_write=for_write)
    zh_candidates = (
        drafts_dir / f"{format_chapter_stem(chapter, width, legacy=False)}_草稿.md",
        drafts_dir / f"第{chapter:04d}章_草稿.md",
        drafts_dir / f"第{chapter}章_草稿.md",
    )
    legacy_candidates = (
        drafts_dir / f"ch_{chapter:0{width}d}_draft.md",
        drafts_dir / f"ch_{chapter:04d}_draft.md",
        drafts_dir / f"ch_{chapter}_draft.md",
    )
    for cand in (*zh_candidates, *legacy_candidates):
        if cand.is_file():
            return cand
    legacy_mode = is_legacy_book(root)
    return legacy_candidates[0] if legacy_mode else zh_candidates[0]


def find_existing_manuscript_files(
    book_dir: Path | str,
    chapter: int,
    *,
    vol_id: str | None = None,
) -> list[Path]:
    """扫描 ``04_创作/定稿/``（及旧版目录）中指定章号的既有定稿文件。"""
    root = Path(book_dir)
    results: list[Path] = []
    seen: set[Path] = set()
    for base in (
        root / ZH_CREATION_DIR / ZH_MANUSCRIPT_SUBDIR,
        root / LEGACY_CREATION_DIR / LEGACY_MANUSCRIPT_SUBDIR,
    ):
        if not base.is_dir():
            continue
        if vol_id is not None:
            sub_candidates: list[Path] = [
                base / format_volume_stem(vol_id, legacy=False),
                base / vol_id,
            ]
            vol_num = _extract_vol_num(vol_id)
            if vol_num is not None:
                for cand_dir in sorted(base.iterdir()):
                    if cand_dir.is_dir() and _matches_volume_stem(cand_dir.name, vol_num):
                        sub_candidates.append(cand_dir)
            for sub in sub_candidates:
                if not sub.is_dir():
                    continue
                for cand in sorted(sub.glob("*.md")):
                    if cand.is_file() and _matches_chapter_stem(cand.stem, chapter):
                        rp = cand.resolve()
                        if rp not in seen:
                            seen.add(rp)
                            results.append(cand)
        else:
            for cand in sorted(base.rglob("*.md")):
                if cand.is_file() and _matches_chapter_stem(cand.stem, chapter):
                    rp = cand.resolve()
                    if rp not in seen:
                        seen.add(rp)
                        results.append(cand)
    return results


def resolve_manuscript_file(
    book_dir: Path | str,
    vol_id: str,
    chapter: int,
    width: int = 4,
    *,
    title: str | None = None,
    for_write: bool = False,
) -> Path:
    root = Path(book_dir)
    legacy_mode = is_legacy_book(root)
    ms_dir = resolve_manuscript_dir(root, for_write=for_write)
    vol_dir = resolve_volume_subdir(ms_dir, vol_id, legacy=legacy_mode)
    zh_bare = f"{format_chapter_stem(chapter, width, legacy=False)}.md"
    legacy_bare = f"{format_chapter_stem(chapter, width, legacy=True)}.md"

    if not for_write or title is None:
        if (vol_dir / zh_bare).is_file():
            return vol_dir / zh_bare
        if (vol_dir / legacy_bare).is_file():
            return vol_dir / legacy_bare
        existing = find_existing_manuscript_files(root, chapter, vol_id=vol_id)
        if existing:
            return existing[0]
        if not for_write:
            any_vol = find_existing_manuscript_files(root, chapter)
            if any_vol:
                return any_vol[0]

    target_stem = format_chapter_stem(chapter, width, title=title, legacy=legacy_mode)
    return vol_dir / f"{target_stem}.md"


def resolve_facts_file(
    book_dir: Path | str,
    vol_id: str,
    *,
    for_write: bool = False,
) -> Path:
    root = Path(book_dir)
    zh_name = f"{format_volume_stem(vol_id, legacy=False)}_事实.jsonl"
    legacy_name = f"{vol_id}_facts.jsonl"
    for base in (
        root / ZH_STATE_DIR / ZH_HISTORY_SUBDIR,
        root / LEGACY_STATE_DIR / LEGACY_HISTORY_SUBDIR,
    ):
        for name in (zh_name, legacy_name):
            cand = base / name
            if cand.is_file():
                return cand
    legacy_mode = is_legacy_book(root)
    hist_dir = resolve_history_dir(root, for_write=for_write)
    return hist_dir / (legacy_name if legacy_mode else zh_name)


def list_facts_files(book_dir: Path | str) -> list[Path]:
    root = Path(book_dir)
    results: list[Path] = []
    for base in (
        root / ZH_STATE_DIR / ZH_HISTORY_SUBDIR,
        root / LEGACY_STATE_DIR / LEGACY_HISTORY_SUBDIR,
    ):
        if base.is_dir():
            for path in sorted(base.iterdir()):
                if path.is_file() and is_facts_filename(path.name):
                    results.append(path)
    return results


# ---------------------------------------------------------------------------
# 4. 路径/文件名特征判断辅助函数
# ---------------------------------------------------------------------------


def is_archive_path(path: Path | str) -> bool:
    parts = Path(path).parts
    return ZH_ARCHIVE_SUBDIR in parts or LEGACY_ARCHIVE_SUBDIR in parts


def is_active_state_path(path: Path | str) -> bool:
    parts = Path(path).parts
    return ZH_ACTIVE_SUBDIR in parts or LEGACY_ACTIVE_SUBDIR in parts


def is_blueprint_path(path: Path | str) -> bool:
    parts = Path(path).parts
    return ZH_BLUEPRINTS_SUBDIR in parts or LEGACY_BLUEPRINTS_SUBDIR in parts


def is_author_notes_root_part(part: str) -> bool:
    return part in (ZH_AUTHOR_NOTES_DIR, LEGACY_AUTHOR_NOTES_DIR)


def is_author_notes_path(path: Path | str) -> bool:
    if isinstance(path, str):
        if ZH_AUTHOR_NOTES_DIR not in path and LEGACY_AUTHOR_NOTES_DIR not in path:
            return False
        parts = tuple(p for p in path.replace("\\", "/").split("/") if p)
        return any(is_author_notes_root_part(part) for part in parts)
    parts = path.parts
    return any(is_author_notes_root_part(part) for part in parts)


def is_setting_root_part(part: str) -> bool:
    return part in (ZH_SETTINGS_DIR, LEGACY_SETTINGS_DIR)


def is_structure_root_part(part: str) -> bool:
    return part in (ZH_STRUCTURE_DIR, LEGACY_STRUCTURE_DIR)


def is_state_root_part(part: str) -> bool:
    return part in (ZH_STATE_DIR, LEGACY_STATE_DIR)


def is_creation_root_part(part: str) -> bool:
    return part in (ZH_CREATION_DIR, LEGACY_CREATION_DIR)


def is_facts_filename(name: str) -> bool:
    return name.endswith("_事实.jsonl") or name.endswith("_facts.jsonl")


def is_characters_state_filename(name: str) -> bool:
    return name in (ZH_CHARACTERS_STATE_FILENAME, LEGACY_CHARACTERS_STATE_FILENAME)


def is_factions_state_filename(name: str) -> bool:
    return name in (ZH_FACTIONS_STATE_FILENAME, LEGACY_FACTIONS_STATE_FILENAME)


def is_relations_filename(name: str) -> bool:
    return name in (ZH_RELATIONS_FILENAME, LEGACY_RELATIONS_FILENAME)


def is_threads_filename(name: str) -> bool:
    return name in (ZH_THREADS_FILENAME, LEGACY_THREADS_FILENAME)


def is_world_state_filename(name: str) -> bool:
    return name in (ZH_WORLD_STATE_FILENAME, LEGACY_WORLD_STATE_FILENAME)


__all__ = [
    "LEGACY_ACTIVE_SUBDIR",
    "LEGACY_ARCHIVE_SUBDIR",
    "LEGACY_ARCS_SUBDIR",
    "LEGACY_AUTHOR_NOTES_DIR",
    "LEGACY_BLUEPRINTS_SUBDIR",
    "LEGACY_CHARACTERS_STATE_FILENAME",
    "LEGACY_CONFIG_FILENAME",
    "LEGACY_CREATION_DIR",
    "LEGACY_DRAFTS_SUBDIR",
    "LEGACY_FACTIONS_STATE_FILENAME",
    "LEGACY_HISTORY_SUBDIR",
    "LEGACY_MANUSCRIPT_SUBDIR",
    "LEGACY_MASTER_OUTLINE_FILENAME",
    "LEGACY_PACKETS_SUBDIR",
    "LEGACY_RELATIONS_FILENAME",
    "LEGACY_SETTINGS_DIR",
    "LEGACY_STATE_DIR",
    "LEGACY_STRUCTURE_DIR",
    "LEGACY_STYLE_GUIDE_FILENAME",
    "LEGACY_THREADS_FILENAME",
    "LEGACY_VOLUMES_SUBDIR",
    "LEGACY_WORLD_STATE_FILENAME",
    "SETTING_SUBDIR_MAP",
    "ZH_ACTIVE_SUBDIR",
    "ZH_ARCHIVE_SUBDIR",
    "ZH_ARCS_SUBDIR",
    "ZH_AUTHOR_NOTES_DIR",
    "ZH_AUTHOR_NOTES_README_FILENAME",
    "ZH_BLUEPRINTS_SUBDIR",
    "ZH_CHARACTERS_STATE_FILENAME",
    "ZH_CONFIG_FILENAME",
    "ZH_CREATION_DIR",
    "ZH_DRAFTS_SUBDIR",
    "ZH_FACTIONS_STATE_FILENAME",
    "ZH_HISTORY_SUBDIR",
    "ZH_MANUSCRIPT_SUBDIR",
    "ZH_MASTER_OUTLINE_FILENAME",
    "ZH_PACKETS_SUBDIR",
    "ZH_RELATIONS_FILENAME",
    "ZH_SETTINGS_DIR",
    "ZH_STATE_DIR",
    "ZH_STRUCTURE_DIR",
    "ZH_STYLE_GUIDE_FILENAME",
    "ZH_THREADS_FILENAME",
    "ZH_VOLUMES_SUBDIR",
    "ZH_WORLD_STATE_FILENAME",
    "extract_clean_title",
    "find_existing_blueprint_file",
    "find_existing_manuscript_files",
    "find_existing_setting_file",
    "format_arc_stem",
    "format_chapter_stem",
    "format_setting_stem",
    "format_volume_stem",
    "infer_arc_id_from_stem",
    "infer_volume_id_from_stem",
    "is_active_state_path",
    "is_archive_path",
    "is_author_notes_path",
    "is_author_notes_root_part",
    "is_blueprint_path",
    "is_characters_state_filename",
    "is_creation_root_part",
    "is_factions_state_filename",
    "is_facts_filename",
    "is_legacy_book",
    "is_relations_filename",
    "is_setting_root_part",
    "is_state_root_part",
    "is_structure_root_part",
    "is_threads_filename",
    "is_world_state_filename",
    "list_facts_files",
    "resolve_active_characters_file",
    "resolve_active_factions_file",
    "resolve_archive_characters_file",
    "resolve_archive_dir",
    "resolve_archive_factions_file",
    "resolve_archive_relations_file",
    "resolve_archive_threads_file",
    "resolve_arc_file",
    "resolve_arcs_dir",
    "resolve_author_notes_root",
    "resolve_blueprint_file",
    "resolve_blueprints_dir",
    "resolve_config_path",
    "resolve_creation_root",
    "resolve_draft_file",
    "resolve_drafts_dir",
    "resolve_facts_file",
    "resolve_history_dir",
    "resolve_manuscript_dir",
    "resolve_manuscript_file",
    "resolve_master_outline",
    "resolve_packet_file",
    "resolve_packets_dir",
    "resolve_relations_file",
    "resolve_setting_dir",
    "resolve_setting_file",
    "resolve_settings_root",
    "resolve_state_root",
    "resolve_structure_root",
    "resolve_style_guide_file",
    "resolve_threads_file",
    "resolve_volume_file",
    "resolve_volume_subdir",
    "resolve_volumes_dir",
    "resolve_world_state_file",
    "sanitize_filename_label",
]
