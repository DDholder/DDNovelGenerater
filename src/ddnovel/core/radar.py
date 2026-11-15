"""规划期悬线伏笔三类雷达巡检引擎 (Task T09)。

在编写单元或单章蓝图前扫描全书活跃伏笔台账，基于以下三类规则精准唤醒候选伏笔：
1. ``RadarReason.WINDOW``：预计回收窗口覆盖目标章；
2. ``RadarReason.STALE``：非休眠伏笔连续未推进超过阈值，或任意活跃伏笔已超出预计回收窗口；
3. ``RadarReason.ENTITY``：触发实体与本章计划出场实体产生交集。

同时严格物理隔离 ``archive/`` 冷库伏笔、已完结 (``resolved``) 伏笔与尚未埋设伏笔
（``planted_at > target_chapter``），确保不产生无效噪音。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Self

from .indexer import SqliteIndexer
from .parser import ProgressiveParser
from .paths import format_chapter_stem, is_archive_path, resolve_blueprints_dir, resolve_config_path
from .schemas import (
    ENTITY_ID_PATTERN,
    ChapterWindow,
    NovelConfig,
    RadarHit,
    RadarReason,
    RadarReport,
    Thread,
    ThreadStatus,
    format_chapter,
)

_ENTITY_ID_RE: Final[re.Pattern[str]] = re.compile(ENTITY_ID_PATTERN)
_STALE_GAP_STATUSES: Final[frozenset[ThreadStatus]] = frozenset(
    {ThreadStatus.PLANTED, ThreadStatus.ACTIVE}
)


@dataclass(frozen=True, slots=True)
class _ThreadRow:
    """索引库 threads 表的完整记录视图（包含 source_file）。"""

    thread: Thread
    source_file: str

    @property
    def is_archived(self) -> bool:
        normalized = self.source_file.replace("\\", "/")
        return is_archive_path(normalized)


class ForeshadowRadar:
    """规划期悬线伏笔三类雷达（窗口到期 / 超期断线 / 出场实体联动）。"""

    def __init__(
        self,
        book_dir: Path | str,
        *,
        indexer: SqliteIndexer | None = None,
        parser: ProgressiveParser | None = None,
        config: NovelConfig | None = None,
        auto_sync: bool = True,
    ) -> None:
        self._book_dir: Path = Path(book_dir).resolve()
        self._external_indexer: SqliteIndexer | None = indexer
        self._auto_sync: bool = auto_sync
        self._owned_indexer: SqliteIndexer | None = None

        if config is not None:
            self._config = config
        elif indexer is not None:
            self._config = indexer.config
        else:
            cfg_path = resolve_config_path(self._book_dir)
            if cfg_path.is_file():
                self._config = NovelConfig.load(self._book_dir)
            else:
                self._config = NovelConfig(title=self._book_dir.name or "未命名小说")

        self._parser: ProgressiveParser = (
            parser if parser is not None else ProgressiveParser(book_dir=self._book_dir)
        )

    @property
    def book_dir(self) -> Path:
        """返回解析后的小说书库根目录。"""
        return self._book_dir

    @property
    def config(self) -> NovelConfig:
        """返回当前使用的小说配置对象。"""
        return self._config

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """幂等释放内部创建的 SQLite 连接（绝不关闭外部注入的 indexer）。"""
        if self._owned_indexer is not None:
            try:
                self._owned_indexer.close()
            finally:
                self._owned_indexer = None

    @contextmanager
    def _use_indexer(self) -> Iterator[SqliteIndexer]:
        """提供生命周期安全的 SqliteIndexer 会话上下文。"""
        if self._external_indexer is not None:
            yield self._external_indexer
            return

        if self._owned_indexer is not None:
            yield self._owned_indexer
            return

        temp_indexer = SqliteIndexer(
            self._book_dir,
            config=self._config,
            parser=self._parser,
        )
        self._owned_indexer = temp_indexer
        try:
            yield temp_indexer
        finally:
            self._owned_indexer = None
            temp_indexer.close()

    # ------------------------------------------------------------------
    # 公共雷达巡检入口
    # ------------------------------------------------------------------

    def scan(
        self,
        target_chapter: int | None = None,
        planned_entities: Sequence[str] | None = None,
        *,
        stale_threshold: int | None = None,
        include_blueprint_entities: bool = True,
        sync_index: bool | None = None,
    ) -> RadarReport:
        """执行指定目标章节的规划期伏笔三类雷达巡检并返回结构化报告。"""
        resolved_chapter = self._resolve_target_chapter(target_chapter)
        resolved_threshold = self._resolve_stale_threshold(stale_threshold)
        should_sync = self._auto_sync if sync_index is None else sync_index

        with self._use_indexer() as active_indexer:
            if should_sync:
                active_indexer.sync()

            resolved_entities = self._resolve_planned_entities(
                indexer=active_indexer,
                target_chapter=resolved_chapter,
                planned_entities=planned_entities,
                include_blueprint_entities=include_blueprint_entities,
            )
            thread_rows = self._load_thread_rows(active_indexer)

        planned_set = set(resolved_entities)
        total_active_threads = 0
        hits: list[RadarHit] = []

        for row in thread_rows:
            if row.is_archived:
                continue
            thread = row.thread
            if thread.status == ThreadStatus.RESOLVED:
                continue
            if thread.planted_at > resolved_chapter:
                continue

            total_active_threads += 1
            hit = self._evaluate_thread(
                thread,
                target_chapter=resolved_chapter,
                planned_set=planned_set,
                stale_threshold=resolved_threshold,
            )
            if hit is not None:
                hits.append(hit)

        hits.sort(
            key=lambda h: (
                -len(h.reasons),
                0 if RadarReason.WINDOW in h.reasons else 1,
                h.thread.planted_at,
                h.thread.id,
            )
        )

        return RadarReport(
            target_chapter=resolved_chapter,
            planned_entities=resolved_entities,
            hits=hits,
            total_active_threads=total_active_threads,
        )

    # ------------------------------------------------------------------
    # 参数校验与实体解析
    # ------------------------------------------------------------------

    def _resolve_target_chapter(self, target_chapter: int | None) -> int:
        if target_chapter is None:
            return max(1, self._config.current_chapter + 1)
        if isinstance(target_chapter, bool) or not isinstance(target_chapter, int) or target_chapter < 1:
            raise ValueError(f"目标章号 target_chapter 必须为 >= 1 的整数，收到: {target_chapter!r}")
        return target_chapter

    def _resolve_stale_threshold(self, stale_threshold: int | None) -> int:
        if stale_threshold is None:
            return self._config.lifecycle.stale_thread_chapters
        if isinstance(stale_threshold, bool) or not isinstance(stale_threshold, int) or stale_threshold < 1:
            raise ValueError(f"断线阈值 stale_threshold 必须为 >= 1 的整数，收到: {stale_threshold!r}")
        return stale_threshold

    def _resolve_planned_entities(
        self,
        *,
        indexer: SqliteIndexer,
        target_chapter: int,
        planned_entities: Sequence[str] | None,
        include_blueprint_entities: bool,
    ) -> list[str]:
        if planned_entities is None:
            if not include_blueprint_entities:
                return []
            return self._extract_blueprint_entities(target_chapter)

        if isinstance(planned_entities, (str, bytes)):
            raise TypeError(
                "planned_entities 必须为实体标识字符串序列（如 list[str]），不能直接传入单个字符串"
            )

        resolved: list[str] = []
        seen: set[str] = set()
        for raw_item in planned_entities:
            if not isinstance(raw_item, str):
                raise TypeError(
                    f"planned_entities 中的每一项必须为字符串，实际收到: {type(raw_item).__name__}"
                )
            term = raw_item.strip()
            if not term:
                continue

            if _ENTITY_ID_RE.fullmatch(term):
                eid = term
            else:
                mapped = self._lookup_entity_id_by_name_or_alias(indexer, term)
                if mapped is None:
                    raise ValueError(
                        f"无法将 planned_entities 中的 '{term}' 解析为有效实体 ID"
                        "（既不符合 EntityId 格式，也未在实体名称或别名表中找到精确匹配）"
                    )
                eid = mapped

            if eid not in seen:
                seen.add(eid)
                resolved.append(eid)

        return resolved

    @staticmethod
    def _lookup_entity_id_by_name_or_alias(indexer: SqliteIndexer, term: str) -> str | None:
        conn = indexer.conn
        queries = (
            "SELECT id FROM entities WHERE name = ? AND is_archived = 0 ORDER BY id ASC LIMIT 1",
            "SELECT entity_id FROM entity_aliases WHERE alias = ? AND is_archived = 0 "
            "ORDER BY entity_id ASC LIMIT 1",
            "SELECT id FROM entities WHERE name = ? ORDER BY id ASC LIMIT 1",
            "SELECT entity_id FROM entity_aliases WHERE alias = ? ORDER BY entity_id ASC LIMIT 1",
        )
        for sql in queries:
            row = conn.execute(sql, (term,)).fetchone()
            if row is not None:
                return str(row[0])
        return None

    def _extract_blueprint_entities(self, chapter_num: int) -> list[str]:
        bp_root = resolve_blueprints_dir(self._book_dir)
        if not bp_root.is_dir():
            return []

        all_files = sorted(p for p in bp_root.rglob("*.md") if p.is_file())
        if not all_files:
            return []

        expected_stem = format_chapter(chapter_num, width=self._config.chapter_id_width)
        expected_zh_stem = format_chapter_stem(chapter_num, width=self._config.chapter_id_width, legacy=False)
        ch_stem_re = re.compile(rf"^(?:ch_?|第)0*{chapter_num}(?:$|[^0-9])", re.IGNORECASE)

        prioritized: list[Path] = []
        fallback: list[Path] = []
        for path in all_files:
            if path.stem in (expected_stem, expected_zh_stem) or ch_stem_re.match(path.stem):
                prioritized.append(path)
            else:
                fallback.append(path)

        for path in (*prioritized, *fallback):
            parsed = self._parser.parse_blueprint_md(path, book_dir=self._book_dir)
            if parsed.chapter == chapter_num:
                return list(parsed.all_entity_ids())

        return []

    # ------------------------------------------------------------------
    # 台账读取与三类规则判定
    # ------------------------------------------------------------------

    @staticmethod
    def _load_thread_rows(indexer: SqliteIndexer) -> list[_ThreadRow]:
        cursor = indexer.conn.execute(
            """
            SELECT id, title, category, status, planted_at, last_progressed_at,
                   win_start, win_end, trigger_entities_json, public_clue, truth_secret, source_file
            FROM threads
            ORDER BY planted_at ASC, id ASC
            """
        )
        rows: list[_ThreadRow] = []
        for r in cursor.fetchall():
            exp_win = (
                ChapterWindow(start=int(r[6]), end=int(r[7]))
                if r[6] is not None and r[7] is not None
                else None
            )
            thread = Thread(
                id=str(r[0]),
                title=str(r[1]),
                category=str(r[2]) if r[2] else "other",
                status=ThreadStatus(r[3]),
                planted_at=int(r[4]),
                last_progressed_at=int(r[5]),
                expected_window=exp_win,
                trigger_entities=json.loads(r[8]) if r[8] else [],
                public_clue=str(r[9] or ""),
                truth_secret=str(r[10]) if r[10] else None,
            )
            rows.append(_ThreadRow(thread=thread, source_file=str(r[11] or "")))
        return rows

    @staticmethod
    def _evaluate_thread(
        thread: Thread,
        *,
        target_chapter: int,
        planned_set: set[str],
        stale_threshold: int,
    ) -> RadarHit | None:
        reasons: list[RadarReason] = []
        detail_parts: list[str] = []

        win = thread.expected_window
        if win is not None and win.start <= target_chapter <= win.end:
            reasons.append(RadarReason.WINDOW)
            detail_parts.append(
                f"进入预计回收窗口 [{win.start}, {win.end}]（目标第 {target_chapter} 章）"
            )

        gap = target_chapter - thread.last_progressed_at
        is_gap_stale = thread.status in _STALE_GAP_STATUSES and gap > stale_threshold
        is_window_overdue = win is not None and target_chapter > win.end

        if is_gap_stale or is_window_overdue:
            reasons.append(RadarReason.STALE)
            stale_msgs: list[str] = []
            if is_gap_stale:
                stale_msgs.append(
                    f"距上次推进（第 {thread.last_progressed_at} 章）已间隔 {gap} 章，"
                    f"超过阈值 {stale_threshold} 章"
                )
            if is_window_overdue and win is not None:
                overdue = target_chapter - win.end
                stale_msgs.append(
                    f"已超出预计回收窗口 [{win.start}, {win.end}] {overdue} 章仍未完结"
                )
            detail_parts.append("且".join(stale_msgs))

        if planned_set and thread.trigger_entities:
            matched_entities = [eid for eid in thread.trigger_entities if eid in planned_set]
            if matched_entities:
                reasons.append(RadarReason.ENTITY)
                detail_parts.append(f"命中计划出场实体: {', '.join(matched_entities)}")

        if not reasons:
            return None

        return RadarHit(
            thread=thread,
            reasons=reasons,
            detail="；".join(detail_parts),
        )


def scan_foreshadow_radar(
    book_dir: Path | str,
    target_chapter: int | None = None,
    planned_entities: Sequence[str] | None = None,
    **kwargs: Any,
) -> RadarReport:
    """便捷函数：执行规划期悬线伏笔三类雷达巡检并返回 ``RadarReport``。"""
    indexer = kwargs.pop("indexer", None)
    parser = kwargs.pop("parser", None)
    config = kwargs.pop("config", None)
    auto_sync = kwargs.pop("auto_sync", True)

    with ForeshadowRadar(
        book_dir,
        indexer=indexer,
        parser=parser,
        config=config,
        auto_sync=auto_sync,
    ) as radar:
        return radar.scan(
            target_chapter=target_chapter,
            planned_entities=planned_entities,
            **kwargs,
        )


__all__ = [
    "ForeshadowRadar",
    "scan_foreshadow_radar",
]
