"""新书四维目录骨架与多宿主配置渲染器 (T05)。

负责初始化与具体引擎仓库解耦的独立小说书库目录树（`novel.yaml` + 四维真源目录 +
`.ddnovel/` 派生缓存目录 + `.gitignore`），按需渲染 `opencode`、`dsh`、`claude`、
`antigravity` 四类 AI 宿主的 MCP 配置与四角色 Skills 目录结构，并支持安装全局 Antigravity 插件。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from ddnovel import __version__
from ddnovel.core.paths import (
    ZH_ACTIVE_SUBDIR,
    ZH_ARCHIVE_SUBDIR,
    ZH_ARCS_SUBDIR,
    ZH_AUTHOR_NOTES_DIR,
    ZH_AUTHOR_NOTES_README_FILENAME,
    ZH_BLUEPRINTS_SUBDIR,
    ZH_CHARACTERS_STATE_FILENAME,
    ZH_CREATION_DIR,
    ZH_DRAFTS_SUBDIR,
    ZH_FACTIONS_STATE_FILENAME,
    ZH_HISTORY_SUBDIR,
    ZH_MANUSCRIPT_SUBDIR,
    ZH_MASTER_OUTLINE_FILENAME,
    ZH_PACKETS_SUBDIR,
    ZH_RELATIONS_FILENAME,
    ZH_SETTINGS_DIR,
    ZH_STATE_DIR,
    ZH_STRUCTURE_DIR,
    ZH_STYLE_GUIDE_FILENAME,
    ZH_THREADS_FILENAME,
    ZH_VOLUMES_SUBDIR,
    ZH_WORLD_STATE_FILENAME,
    format_arc_stem,
    format_chapter_stem,
    format_volume_stem,
    resolve_config_path,
)
from ddnovel.core.schemas import (
    HostKind,
    NovelConfig,
)

SKILL_ROLES: Final[tuple[str, ...]] = (
    "dd-setting-architect",
    "dd-structure-planner",
    "dd-chapter-writer",
    "dd-state-curator",
)

BOOK_SKELETON_DIRS: Final[tuple[str, ...]] = (
    ".ddnovel",
    ZH_AUTHOR_NOTES_DIR,
    f"{ZH_SETTINGS_DIR}/世界观",
    f"{ZH_SETTINGS_DIR}/地理",
    f"{ZH_SETTINGS_DIR}/势力",
    f"{ZH_SETTINGS_DIR}/人物",
    f"{ZH_SETTINGS_DIR}/器物",
    f"{ZH_STRUCTURE_DIR}/{ZH_VOLUMES_SUBDIR}",
    f"{ZH_STRUCTURE_DIR}/{ZH_ARCS_SUBDIR}",
    f"{ZH_STRUCTURE_DIR}/{ZH_BLUEPRINTS_SUBDIR}/第01卷",
    f"{ZH_STATE_DIR}/{ZH_ACTIVE_SUBDIR}",
    f"{ZH_STATE_DIR}/{ZH_HISTORY_SUBDIR}",
    f"{ZH_STATE_DIR}/{ZH_ARCHIVE_SUBDIR}",
    f"{ZH_CREATION_DIR}/{ZH_PACKETS_SUBDIR}",
    f"{ZH_CREATION_DIR}/{ZH_DRAFTS_SUBDIR}",
    f"{ZH_CREATION_DIR}/{ZH_MANUSCRIPT_SUBDIR}/第01卷",
)

_DEFAULT_TEMPLATES_DIR: Final[Path] = Path(__file__).resolve().parent
ANTIGRAVITY_PLUGIN_NAME: Final[str] = "ddnovel"
DEFAULT_ANTIGRAVITY_PLUGINS_DIR: Final[Path] = Path("~/.gemini/config/plugins")


@dataclass(frozen=True, slots=True)
class InitBookResult:
    """新书初始化完成后的交付物摘要。"""

    book_dir: Path
    config_path: Path
    config: NovelConfig
    created_dirs: tuple[Path, ...]
    created_files: tuple[Path, ...]
    host_files: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class GlobalPluginInstallResult:
    """全局 AI 宿主插件安装/同步完成后的交付物摘要。"""

    host: HostKind
    plugin_dir: Path
    mcp_command: tuple[str, ...]
    written_files: tuple[Path, ...]


def _dump_yaml(data: Any) -> str:
    """将字典/列表序列化为保留中文且字段顺序稳定的 YAML 字符串。"""
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _render_frontmatter_md(frontmatter: dict[str, Any], body: str) -> str:
    """将 Frontmatter 字典安全序列化后与 Markdown 正文拼接。"""
    fm_yaml = _dump_yaml(frontmatter).rstrip("\n")
    clean_body = body.strip("\n")
    if clean_body:
        return f"---\n{fm_yaml}\n---\n\n{clean_body}\n"
    return f"---\n{fm_yaml}\n---\n"


class ProjectInitializer:
    """新书四维真源目录与多宿主配置初始化渲染器。"""

    def __init__(
        self,
        *,
        templates_dir: Path | str | None = None,
        skills_source_dir: Path | str | None = None,
        mcp_command: Sequence[str] | str | None = None,
    ) -> None:
        self.templates_dir: Path = (
            Path(templates_dir).resolve()
            if templates_dir is not None
            else _DEFAULT_TEMPLATES_DIR
        )
        self.skeleton_dir: Path = self.templates_dir / "book_skeleton"
        self.host_configs_dir: Path = self.templates_dir / "host_configs"
        self.skills_source_dir: Path = (
            Path(skills_source_dir).resolve()
            if skills_source_dir is not None
            else self.templates_dir / "skills"
        )
        self._has_explicit_mcp_command: bool = mcp_command is not None
        self.mcp_command: tuple[str, ...] = self._normalize_mcp_command(mcp_command)

    @staticmethod
    def _normalize_mcp_command(mcp_command: Sequence[str] | str | None) -> tuple[str, ...]:
        if mcp_command is None:
            return ("ddnovel-mcp",)
        if isinstance(mcp_command, str):
            cleaned = mcp_command.strip()
            if not cleaned:
                raise ValueError("mcp_command 不能为空字符串")
            return (cleaned,)
        parts = tuple(str(part).strip() for part in mcp_command if str(part).strip())
        if not parts:
            raise ValueError("mcp_command 不能为空序列")
        return parts

    @staticmethod
    def _normalize_hosts(
        hosts: Iterable[HostKind | str] | HostKind | str | None,
    ) -> list[HostKind]:
        if hosts is None:
            return []
        raw_items: Iterable[HostKind | str]
        if isinstance(hosts, (str, HostKind)):
            raw_items = (hosts,)
        else:
            raw_items = hosts

        valid_values = [kind.value for kind in HostKind]
        normalized: list[HostKind] = []
        seen: set[HostKind] = set()
        for item in raw_items:
            if isinstance(item, HostKind):
                kind = item
            else:
                raw_str = str(item).strip().lower()
                try:
                    kind = HostKind(raw_str)
                except ValueError as exc:
                    raise ValueError(
                        f"不支持的宿主类型 {item!r}，可选值: {valid_values}"
                    ) from exc
            if kind not in seen:
                seen.add(kind)
                normalized.append(kind)
        return normalized

    def _read_skeleton_text(self, rel_name: str) -> str:
        return (self.skeleton_dir / rel_name).read_text(encoding="utf-8")

    def _load_genre_preset(self, genre_profile: str) -> dict[str, Any]:
        presets_path = self.skeleton_dir / "genre_presets.yaml"
        raw_presets = yaml.safe_load(presets_path.read_text(encoding="utf-8"))
        if not isinstance(raw_presets, dict):
            raise ValueError(f"题材预设文件格式异常: {presets_path}")

        if genre_profile in raw_presets:
            return dict(raw_presets[genre_profile])

        # 自定义第三方题材回退使用 general 骨架，并清空题材专属 profile_ext 避免校验冲突
        fallback = yaml.safe_load(_dump_yaml(raw_presets["general"]))
        active_chars = fallback.get("active_characters")
        if isinstance(active_chars, dict):
            for char_state in active_chars.values():
                if isinstance(char_state, dict):
                    char_state["profile_ext"] = {}
        return fallback

    def init_book(
        self,
        book_dir: Path | str,
        *,
        title: str | None = None,
        author: str = "",
        genre_profile: str = "general",
        hosts: Iterable[HostKind | str] | HostKind | str | None = None,
        chapter_id_width: int = 4,
        include_samples: bool = True,
        force: bool = False,
    ) -> InitBookResult:
        """在指定目录初始化完整四维小说书库及可选 AI 宿主配置。"""
        target_dir = Path(book_dir).resolve()
        existing_cfg_path = resolve_config_path(target_dir)
        if existing_cfg_path.exists() and not force:
            raise FileExistsError(
                f"目标目录已存在配置文件 {existing_cfg_path.name}: {existing_cfg_path}，"
                "如需覆盖请指定 force=True"
            )

        resolved_title = (
            title.strip()
            if title is not None and title.strip()
            else (target_dir.name.strip() or "未命名长篇小说")
        )
        host_list = self._normalize_hosts(hosts)

        # 在落盘任何目录与文件之前先通过配置 Schema 校验，防止产生半成品脏目录
        config = NovelConfig(
            title=resolved_title,
            author=author,
            genre_profile=genre_profile,
            current_chapter=0,
            chapter_id_width=chapter_id_width,
            hosts=host_list,
        )

        created_dirs: list[Path] = []
        target_dir.mkdir(parents=True, exist_ok=True)
        for rel_dir in BOOK_SKELETON_DIRS:
            dir_path = target_dir / rel_dir
            dir_path.mkdir(parents=True, exist_ok=True)
            created_dirs.append(dir_path)

        created_files: list[Path] = []

        def _write(rel_path: str, content: str) -> Path:
            file_path = target_dir / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            created_files.append(file_path)
            return file_path

        config_path = config.dump(target_dir)
        created_files.append(config_path)

        gitignore_path = target_dir / ".gitignore"
        if not gitignore_path.exists() or force:
            _write(".gitignore", self._read_skeleton_text("gitignore.tmpl"))

        # 冷库空容器与历史事实台账（无论是否生成样例均创建）
        _write(f"{ZH_STATE_DIR}/{ZH_ARCHIVE_SUBDIR}/{ZH_CHARACTERS_STATE_FILENAME}", "{}\n")
        _write(f"{ZH_STATE_DIR}/{ZH_ARCHIVE_SUBDIR}/{ZH_FACTIONS_STATE_FILENAME}", "{}\n")
        _write(f"{ZH_STATE_DIR}/{ZH_ARCHIVE_SUBDIR}/{ZH_RELATIONS_FILENAME}", _dump_yaml({"relations": []}))
        _write(f"{ZH_STATE_DIR}/{ZH_ARCHIVE_SUBDIR}/{ZH_THREADS_FILENAME}", _dump_yaml({"threads": []}))
        _write(f"{ZH_STATE_DIR}/{ZH_HISTORY_SUBDIR}/第01卷_事实.jsonl", "")

        if include_samples:
            self._write_sample_files(
                write_fn=_write,
                config=config,
            )
        else:
            self._write_minimal_empty_files(
                write_fn=_write,
                config=config,
            )

        host_files: list[Path] = []
        if host_list:
            host_files = self.render_host_configs(
                target_dir,
                host_list,
                overwrite=True,
                update_novel_yaml=False,
            )

        return InitBookResult(
            book_dir=target_dir,
            config_path=config_path,
            config=config,
            created_dirs=tuple(created_dirs),
            created_files=tuple(created_files),
            host_files=tuple(host_files),
        )

    def initialize(
        self,
        book_dir: Path | str,
        *,
        title: str | None = None,
        author: str = "",
        genre_profile: str = "general",
        hosts: Iterable[HostKind | str] | HostKind | str | None = None,
        chapter_id_width: int = 4,
        include_samples: bool = True,
        force: bool = False,
    ) -> InitBookResult:
        """``init_book`` 的语义别名方法。"""
        return self.init_book(
            book_dir,
            title=title,
            author=author,
            genre_profile=genre_profile,
            hosts=hosts,
            chapter_id_width=chapter_id_width,
            include_samples=include_samples,
            force=force,
        )

    def _write_sample_files(
        self,
        *,
        write_fn: Any,
        config: NovelConfig,
    ) -> None:
        preset = self._load_genre_preset(config.genre_profile)
        author_readme_tmpl = self._read_skeleton_text("author_notes_readme.md.tmpl")
        setting_tmpl = self._read_skeleton_text("setting_card.md.tmpl")
        outline_tmpl = self._read_skeleton_text("master_outline.md.tmpl")
        style_tmpl = self._read_skeleton_text("style_guide.md.tmpl")

        # 0. 00_作者设定/ 自由设定说明
        write_fn(f"{ZH_AUTHOR_NOTES_DIR}/{ZH_AUTHOR_NOTES_README_FILENAME}", author_readme_tmpl)

        # 1. 01_settings/ 五类起始设定卡
        for card in preset["settings"]:
            fm = {
                "id": card["id"],
                "name": card["name"],
                "aliases": card["aliases"],
                "category": card["category"],
                "status": card["status"],
                "tags": card["tags"],
            }
            body = setting_tmpl.format(
                core_text=card["core_text"],
                topic_name=card["topic_name"],
                topic_text=card["topic_text"],
                secret_text=card["secret_text"],
            )
            write_fn(card["path"], _render_frontmatter_md(fm, body))

        # 2. 02_设定/结构 总纲 -> 分卷 -> 单元 -> 首章蓝图
        vol = preset["volume"]
        write_fn(
            f"{ZH_STRUCTURE_DIR}/{ZH_MASTER_OUTLINE_FILENAME}",
            outline_tmpl.format(
                title=config.title,
                premise=preset["premise"],
                vol_title=vol["title"],
                vol_conflict=vol["core_conflict"],
            ),
        )
        vol_fm = {
            "id": vol["id"],
            "title": vol["title"],
            "chapter_range": vol["chapter_range"],
            "core_conflict": vol["core_conflict"],
            "folded_summary": None,
        }
        vol_stem = format_volume_stem(vol["id"], title=vol.get("title"))
        write_fn(
            f"{ZH_STRUCTURE_DIR}/{ZH_VOLUMES_SUBDIR}/{vol_stem}.md",
            _render_frontmatter_md(vol_fm, vol["body"]),
        )

        arc = preset["arc"]
        arc_fm = {
            "id": arc["id"],
            "volume_id": arc["volume_id"],
            "title": arc["title"],
            "chapter_range": arc["chapter_range"],
            "folded_summary": None,
        }
        arc_stem = format_arc_stem(arc["id"], title=arc.get("title"))
        write_fn(
            f"{ZH_STRUCTURE_DIR}/{ZH_ARCS_SUBDIR}/{arc_stem}.md",
            _render_frontmatter_md(arc_fm, arc["body"]),
        )

        bp = preset["blueprint"]
        bp_fm = {
            "chapter": bp["chapter"],
            "volume_id": bp["volume_id"],
            "arc_id": bp["arc_id"],
            "title": bp["title"],
            "pov": bp["pov"],
            "cast": bp["cast"],
            "threads": bp["threads"],
            "unlock_secrets": bp["unlock_secrets"],
            "topics": bp["topics"],
            "forbidden": bp["forbidden"],
            "beats": bp["beats"],
        }
        ch_stem = format_chapter_stem(
            int(bp["chapter"]),
            width=config.chapter_id_width,
            title=bp.get("title"),
        )
        ch_filename = f"{ch_stem}.md"
        bp_vol_stem = format_volume_stem(bp["volume_id"])
        write_fn(
            f"{ZH_STRUCTURE_DIR}/{ZH_BLUEPRINTS_SUBDIR}/{bp_vol_stem}/{ch_filename}",
            _render_frontmatter_md(bp_fm, bp["body"]),
        )

        # 3. 03_状态/ 状态维截面文件
        write_fn(f"{ZH_STATE_DIR}/{ZH_WORLD_STATE_FILENAME}", _dump_yaml(preset["world_state"]))
        write_fn(
            f"{ZH_STATE_DIR}/{ZH_ACTIVE_SUBDIR}/{ZH_CHARACTERS_STATE_FILENAME}",
            _dump_yaml(preset["active_characters"]),
        )
        write_fn(
            f"{ZH_STATE_DIR}/{ZH_ACTIVE_SUBDIR}/{ZH_FACTIONS_STATE_FILENAME}",
            _dump_yaml(preset["active_factions"]),
        )
        write_fn(f"{ZH_STATE_DIR}/{ZH_RELATIONS_FILENAME}", _dump_yaml({"relations": preset["relations"]}))
        write_fn(f"{ZH_STATE_DIR}/{ZH_THREADS_FILENAME}", _dump_yaml({"threads": preset["threads"]}))

        # 4. 04_创作/文风指南.md
        sg = preset["style_guide"]
        sg_fm = {
            "tone": sg["tone"],
            "pov_rules": sg["pov_rules"],
            "banned_words": sg["banned_words"],
        }
        sg_body = style_tmpl.format(style_note=sg["style_note"])
        write_fn(f"{ZH_CREATION_DIR}/{ZH_STYLE_GUIDE_FILENAME}", _render_frontmatter_md(sg_fm, sg_body))

    def _write_minimal_empty_files(
        self,
        *,
        write_fn: Any,
        config: NovelConfig,
    ) -> None:
        author_readme_tmpl = self._read_skeleton_text("author_notes_readme.md.tmpl")
        outline_tmpl = self._read_skeleton_text("master_outline.md.tmpl")
        style_tmpl = self._read_skeleton_text("style_guide.md.tmpl")

        write_fn(f"{ZH_AUTHOR_NOTES_DIR}/{ZH_AUTHOR_NOTES_README_FILENAME}", author_readme_tmpl)

        write_fn(
            f"{ZH_STRUCTURE_DIR}/{ZH_MASTER_OUTLINE_FILENAME}",
            outline_tmpl.format(
                title=config.title,
                premise="（待编写全书核心立意与主线冲突）",
                vol_title="第一卷（待定）",
                vol_conflict="（待编写第一卷核心冲突）",
            ),
        )
        write_fn(
            f"{ZH_STATE_DIR}/{ZH_WORLD_STATE_FILENAME}",
            _dump_yaml({"current_chapter": 0, "in_world_time": "", "macro_situation": ""}),
        )
        write_fn(f"{ZH_STATE_DIR}/{ZH_ACTIVE_SUBDIR}/{ZH_CHARACTERS_STATE_FILENAME}", "{}\n")
        write_fn(f"{ZH_STATE_DIR}/{ZH_ACTIVE_SUBDIR}/{ZH_FACTIONS_STATE_FILENAME}", "{}\n")
        write_fn(f"{ZH_STATE_DIR}/{ZH_RELATIONS_FILENAME}", _dump_yaml({"relations": []}))
        write_fn(f"{ZH_STATE_DIR}/{ZH_THREADS_FILENAME}", _dump_yaml({"threads": []}))

        sg_fm = {
            "tone": "",
            "pov_rules": ["严格遵守单章 POV 角色认知边界，禁止未解锁剧透。"],
            "banned_words": ["震惊全场", "恐怖如斯"],
        }
        sg_body = style_tmpl.format(style_note="保持叙事节奏凝练，突出动作与细节。")
        write_fn(f"{ZH_CREATION_DIR}/{ZH_STYLE_GUIDE_FILENAME}", _render_frontmatter_md(sg_fm, sg_body))

    def render_host_configs(
        self,
        book_dir: Path | str,
        hosts: Iterable[HostKind | str] | HostKind | str,
        *,
        overwrite: bool = True,
        update_novel_yaml: bool = True,
    ) -> list[Path]:
        """为目标书库生成或增量更新指定 AI 宿主的 MCP 配置与 Skills 目录。"""
        target_dir = Path(book_dir).resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        host_list = self._normalize_hosts(hosts)
        book_dir_posix = target_dir.as_posix()

        written_files: list[Path] = []
        for host in host_list:
            if host == HostKind.OPENCODE:
                cfg_path = self._render_opencode_config(
                    target_dir,
                    book_dir_posix=book_dir_posix,
                    overwrite=overwrite,
                )
                if cfg_path is not None:
                    written_files.append(cfg_path)
                written_files.extend(
                    self._sync_host_skills(target_dir / ".opencode" / "skills", overwrite=overwrite)
                )
            elif host == HostKind.DSH:
                cfg_path = self._render_dsh_config(
                    target_dir,
                    book_dir_posix=book_dir_posix,
                    overwrite=overwrite,
                )
                if cfg_path is not None:
                    written_files.append(cfg_path)
                written_files.extend(
                    self._sync_host_skills(target_dir / ".dsh" / "skills", overwrite=overwrite)
                )
            elif host == HostKind.CLAUDE:
                cfg_path = self._render_claude_config(
                    target_dir,
                    book_dir_posix=book_dir_posix,
                    overwrite=overwrite,
                )
                if cfg_path is not None:
                    written_files.append(cfg_path)
                written_files.extend(
                    self._sync_host_skills(target_dir / ".claude" / "skills", overwrite=overwrite)
                )
            elif host == HostKind.ANTIGRAVITY:
                written_files.extend(
                    self._render_antigravity_bundle(
                        target_dir / ".agents" / "plugins" / ANTIGRAVITY_PLUGIN_NAME,
                        mcp_command=self.mcp_command,
                        book_dir_posix=book_dir_posix,
                        overwrite=overwrite,
                    )
                )

        if update_novel_yaml and resolve_config_path(target_dir).is_file():
            cfg = NovelConfig.load(target_dir)
            merged_hosts = list(dict.fromkeys([*cfg.hosts, *host_list]))
            if merged_hosts != cfg.hosts:
                updated_cfg = cfg.model_copy(update={"hosts": merged_hosts})
                updated_cfg.dump(target_dir)

        return written_files

    def _render_opencode_config(
        self,
        book_dir: Path,
        *,
        book_dir_posix: str,
        overwrite: bool,
    ) -> Path | None:
        target_path = book_dir / "opencode.json"
        template = json.loads((self.host_configs_dir / "opencode.json").read_text(encoding="utf-8"))

        if target_path.is_file():
            try:
                existing = json.loads(target_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = None
            if isinstance(existing, dict):
                mcp_block = existing.get("mcp")
                if not overwrite and isinstance(mcp_block, dict) and "ddnovel" in mcp_block:
                    return None
                data = existing
            else:
                if not overwrite:
                    return None
                data = template
        else:
            data = template

        data.setdefault("$schema", template.get("$schema", "https://opencode.ai/config.json"))
        mcp_section = data.get("mcp")
        if not isinstance(mcp_section, dict):
            mcp_section = {}
            data["mcp"] = mcp_section

        mcp_section["ddnovel"] = {
            "type": "local",
            "command": list(self.mcp_command),
            "environment": {"DDNOVEL_BOOK_DIR": book_dir_posix},
            "enabled": True,
        }
        target_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return target_path

    def _render_dsh_config(
        self,
        book_dir: Path,
        *,
        book_dir_posix: str,
        overwrite: bool,
    ) -> Path | None:
        dsh_dir = book_dir / ".dsh"
        dsh_dir.mkdir(parents=True, exist_ok=True)
        target_path = dsh_dir / "cordis.mcp.patch.yml"

        if target_path.exists() and not overwrite:
            return None

        raw_template = (self.host_configs_dir / "cordis.mcp.patch.yml").read_text(encoding="utf-8")
        patch_data = yaml.safe_load(raw_template)
        if not isinstance(patch_data, list):
            raise ValueError("host_configs/cordis.mcp.patch.yml 顶层必须为列表")

        cmd_exe = self.mcp_command[0]
        cmd_args = list(self.mcp_command[1:])
        for op in patch_data:
            if not isinstance(op, dict):
                continue
            inserts = op.get("insert")
            if not isinstance(inserts, list):
                continue
            for entry in inserts:
                if isinstance(entry, dict) and entry.get("id") == "mcp-ddnovel":
                    cfg = entry.setdefault("config", {})
                    cfg["command"] = cmd_exe
                    cfg["args"] = cmd_args
                    cfg["cwd"] = book_dir_posix
                    cfg["env"] = {"DDNOVEL_BOOK_DIR": book_dir_posix}

        header = "# DDNovelGenerater MCP patch for DeepSeek Harness (DSH / Cordis)\n"
        target_path.write_text(header + _dump_yaml(patch_data), encoding="utf-8")
        return target_path

    def _render_claude_config(
        self,
        book_dir: Path,
        *,
        book_dir_posix: str,
        overwrite: bool,
    ) -> Path | None:
        target_path = book_dir / ".mcp.json"
        template = json.loads((self.host_configs_dir / "claude.mcp.json").read_text(encoding="utf-8"))

        if target_path.is_file():
            try:
                existing = json.loads(target_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = None
            if isinstance(existing, dict):
                servers_block = existing.get("mcpServers")
                if not overwrite and isinstance(servers_block, dict) and "ddnovel" in servers_block:
                    return None
                data = existing
            else:
                if not overwrite:
                    return None
                data = template
        else:
            data = template

        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
            data["mcpServers"] = servers

        servers["ddnovel"] = {
            "type": "stdio",
            "command": self.mcp_command[0],
            "args": list(self.mcp_command[1:]),
            "env": {"DDNOVEL_BOOK_DIR": book_dir_posix},
        }
        target_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return target_path

    def _sync_host_skills(self, skills_root: Path, *, overwrite: bool) -> list[Path]:
        """创建四角色技能目录，若引擎内置技能模板存在则同步复制到宿主目录。"""
        skills_root.mkdir(parents=True, exist_ok=True)
        copied: list[Path] = []
        for role in SKILL_ROLES:
            dst_role_dir = skills_root / role
            dst_role_dir.mkdir(parents=True, exist_ok=True)

            src_role_dir = self.skills_source_dir / role
            if not src_role_dir.is_dir():
                continue

            for src_file in sorted(src_role_dir.rglob("*")):
                if not src_file.is_file():
                    continue
                rel_file = src_file.relative_to(src_role_dir)
                dst_file = dst_role_dir / rel_file
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                if not dst_file.exists() or overwrite:
                    shutil.copy2(src_file, dst_file)
                    copied.append(dst_file)
        return copied

    def install_global_plugin(
        self,
        host: HostKind | str = HostKind.ANTIGRAVITY,
        *,
        plugin_dir: Path | str | None = None,
        overwrite: bool = True,
    ) -> GlobalPluginInstallResult:
        """安装或升级机器级全局 AI 宿主插件（当前支持 ``antigravity``）。"""
        normalized = self._normalize_hosts([host])
        kind = normalized[0]
        if kind != HostKind.ANTIGRAVITY:
            raise ValueError(
                "全局插件安装目前仅支持 'antigravity' 宿主；"
                "书库级配置请使用 ddnovel init -H <host> 或 ddnovel host sync"
            )

        target_plugin_dir = (
            Path(plugin_dir).expanduser().resolve()
            if plugin_dir is not None
            else (DEFAULT_ANTIGRAVITY_PLUGINS_DIR.expanduser().resolve() / ANTIGRAVITY_PLUGIN_NAME)
        )
        effective_cmd = (
            self.mcp_command
            if self._has_explicit_mcp_command
            else resolve_global_mcp_command()
        )
        written_files = self._render_antigravity_bundle(
            target_plugin_dir,
            mcp_command=effective_cmd,
            book_dir_posix=None,
            overwrite=overwrite,
        )
        return GlobalPluginInstallResult(
            host=kind,
            plugin_dir=target_plugin_dir,
            mcp_command=effective_cmd,
            written_files=tuple(written_files),
        )

    def _render_antigravity_bundle(
        self,
        plugin_dir: Path,
        *,
        mcp_command: tuple[str, ...],
        book_dir_posix: str | None,
        overwrite: bool,
    ) -> list[Path]:
        """渲染 Antigravity 插件包（plugin.json / mcp_config.json / agents / rules / skills）。"""
        plugin_dir.mkdir(parents=True, exist_ok=True)
        written_files: list[Path] = []

        # 1. plugin.json
        manifest_path = plugin_dir / "plugin.json"
        if not manifest_path.exists() or overwrite:
            manifest_data = json.loads(
                (self.host_configs_dir / "antigravity.plugin.json").read_text(encoding="utf-8")
            )
            manifest_data["version"] = __version__
            manifest_path.write_text(
                json.dumps(manifest_data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            written_files.append(manifest_path)

        # 2. README.md
        readme_path = plugin_dir / "README.md"
        if not readme_path.exists() or overwrite:
            readme_text = (self.host_configs_dir / "antigravity.readme.md.tmpl").read_text(encoding="utf-8")
            readme_path.write_text(readme_text, encoding="utf-8")
            written_files.append(readme_path)

        # 3. mcp_config.json（保留同目录其他已配置 MCP 服务）
        mcp_path = plugin_dir / "mcp_config.json"
        mcp_template = json.loads(
            (self.host_configs_dir / "antigravity.mcp.json").read_text(encoding="utf-8")
        )
        should_write_mcp = True
        if mcp_path.is_file():
            try:
                existing = json.loads(mcp_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = None
            if isinstance(existing, dict):
                servers_block = existing.get("mcpServers")
                if (
                    not overwrite
                    and isinstance(servers_block, dict)
                    and ANTIGRAVITY_PLUGIN_NAME in servers_block
                ):
                    should_write_mcp = False
                    mcp_data = existing
                else:
                    mcp_data = existing
            else:
                if not overwrite:
                    should_write_mcp = False
                mcp_data = mcp_template
        else:
            mcp_data = mcp_template

        if should_write_mcp:
            servers = mcp_data.get("mcpServers")
            if not isinstance(servers, dict):
                servers = {}
                mcp_data["mcpServers"] = servers

            server_entry: dict[str, Any] = {
                "command": mcp_command[0],
                "args": list(mcp_command[1:]),
            }
            if book_dir_posix is not None:
                server_entry["cwd"] = book_dir_posix
                server_entry["env"] = {"DDNOVEL_BOOK_DIR": book_dir_posix}

            servers[ANTIGRAVITY_PLUGIN_NAME] = server_entry
            mcp_path.write_text(
                json.dumps(mcp_data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            written_files.append(mcp_path)

        # 4. agents/dd-chapter-writer.md（严格空白上下文执笔子代理）
        agent_path = plugin_dir / "agents" / "dd-chapter-writer.md"
        if not agent_path.exists() or overwrite:
            agent_path.parent.mkdir(parents=True, exist_ok=True)
            agent_text = (self.host_configs_dir / "antigravity.chapter_writer.agent.md").read_text(
                encoding="utf-8"
            )
            agent_path.write_text(agent_text, encoding="utf-8")
            written_files.append(agent_path)

        # 5. rules/AGENTS.md（纯 Markdown 无 Frontmatter）
        rules_path = plugin_dir / "rules" / "AGENTS.md"
        if not rules_path.exists() or overwrite:
            rules_path.parent.mkdir(parents=True, exist_ok=True)
            if book_dir_posix is not None:
                routing_section = (
                    f"- 当前工作区插件已绑定固定小说书库根目录：`{book_dir_posix}`"
                    "（已注入 `DDNOVEL_BOOK_DIR`）。\n"
                    f"- 调用 `novel_*` MCP 工具时可省略 `book_dir` 或显式传入 `{book_dir_posix}`；"
                    f"执行 `ddnovel` CLI 命令时在书库根目录运行或传入 `-d {book_dir_posix}`。"
                )
            else:
                routing_section = (
                    "- 当前插件以**全局多书模式**运行（未硬编码 `DDNOVEL_BOOK_DIR`），"
                    "支持一引擎驱动多本小说书库。\n"
                    "- 调用任何 `novel_*` MCP 工具时，**必须显式传入目标书库根目录路径 `book_dir`**"
                    "（例如当前打开的小说工作区路径）；\n"
                    "- 执行 `ddnovel` CLI 命令时，**必须传入 `-d <book_dir>`**（或在目标书库目录下运行），"
                    "确保不会读写错误书库。"
                )
            rules_tmpl = (self.host_configs_dir / "antigravity.rules.md.tmpl").read_text(encoding="utf-8")
            rules_path.write_text(rules_tmpl.format(routing_section=routing_section), encoding="utf-8")
            written_files.append(rules_path)

        # 6. skills/<role>/SKILL.md
        written_files.extend(self._sync_host_skills(plugin_dir / "skills", overwrite=overwrite))
        return written_files


def resolve_global_mcp_command(
    explicit_mcp_command: Sequence[str] | str | None = None,
) -> tuple[str, ...]:
    """解析全局插件使用的绝对 MCP Server 启动命令，避免因未激活虚拟环境导致无法启动。"""
    if explicit_mcp_command is not None:
        return ProjectInitializer._normalize_mcp_command(explicit_mcp_command)

    exe_dir = Path(sys.executable).resolve().parent
    candidate_names = ("ddnovel-mcp.exe", "ddnovel-mcp") if os.name == "nt" else ("ddnovel-mcp",)
    for name in candidate_names:
        candidate = exe_dir / name
        if candidate.is_file():
            return (candidate.as_posix(),)

    which_cmd = shutil.which("ddnovel-mcp")
    if which_cmd:
        return (Path(which_cmd).resolve().as_posix(),)

    return (Path(sys.executable).resolve().as_posix(), "-m", "ddnovel.mcp_server")


def install_antigravity_plugin(
    *,
    plugin_dir: Path | str | None = None,
    mcp_command: Sequence[str] | str | None = None,
    overwrite: bool = True,
) -> GlobalPluginInstallResult:
    """快捷安装或更新本机全局 Antigravity ``ddnovel`` 插件。"""
    return ProjectInitializer(mcp_command=mcp_command).install_global_plugin(
        HostKind.ANTIGRAVITY,
        plugin_dir=plugin_dir,
        overwrite=overwrite,
    )


def init_book(
    book_dir: Path | str,
    *,
    title: str | None = None,
    author: str = "",
    genre_profile: str = "general",
    hosts: Iterable[HostKind | str] | HostKind | str | None = None,
    chapter_id_width: int = 4,
    include_samples: bool = True,
    force: bool = False,
) -> InitBookResult:
    """快捷初始化新小说书库目录与宿主配置。"""
    return ProjectInitializer().init_book(
        book_dir,
        title=title,
        author=author,
        genre_profile=genre_profile,
        hosts=hosts,
        chapter_id_width=chapter_id_width,
        include_samples=include_samples,
        force=force,
    )


__all__ = [
    "ANTIGRAVITY_PLUGIN_NAME",
    "BOOK_SKELETON_DIRS",
    "DEFAULT_ANTIGRAVITY_PLUGINS_DIR",
    "GlobalPluginInstallResult",
    "InitBookResult",
    "ProjectInitializer",
    "SKILL_ROLES",
    "init_book",
    "install_antigravity_plugin",
    "resolve_global_mcp_command",
]
