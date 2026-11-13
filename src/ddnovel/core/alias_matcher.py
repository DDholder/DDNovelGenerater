"""Aho-Corasick 实体别名自动机 (T04)。

基于纯 Python 字典 Trie + BFS 失败指针 + 预合并后缀输出链实现线性时间多模式匹配，
零外部 C 扩展依赖，支持最长优先重叠消歧与冷库实体自动跳过/按需唤醒。
"""

from __future__ import annotations

from bisect import bisect_right
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Self

from pydantic import TypeAdapter

from .schemas import EntityId, EntityStatus, MatchedEntity, SettingDoc

_ENTITY_ID_ADAPTER: Final[TypeAdapter[str]] = TypeAdapter(EntityId)


@dataclass(frozen=True, slots=True)
class _AliasPattern:
    """内部编译后的别名模式条目。"""

    entity_id: str
    alias: str
    length: int
    is_archived: bool
    reg_order: int


class AliasMatcher:
    """Aho-Corasick 实体全名/外号/简称多模式匹配自动机。"""

    def __init__(
        self,
        entity_aliases_map: Mapping[str, Any] | Iterable[SettingDoc] | None = None,
        *,
        archived_ids: Iterable[str] | None = None,
        archive_non_active: bool = True,
    ) -> None:
        self._reset()
        if entity_aliases_map is not None:
            self.build(
                entity_aliases_map,
                archived_ids=archived_ids,
                archive_non_active=archive_non_active,
            )

    def _reset(self) -> None:
        self._trie: dict[str, Any] = {}
        self._children: list[dict[str, int]] = [{}]
        self._fail: list[int] = [0]
        self._outputs: list[tuple[_AliasPattern, ...]] = [()]
        self._entity_aliases: dict[str, list[str]] = {}
        self._archived_entities: set[str] = set()
        self._alias_lookup: dict[str, list[tuple[str, bool, int]]] = {}
        self._alias_count: int = 0

    @property
    def trie(self) -> dict[str, Any]:
        """返回只读字符字典 Trie 视图（对齐架构类图 `-dict trie`）。"""
        return self._trie

    @property
    def entity_count(self) -> int:
        """已注册实体数量。"""
        return len(self._entity_aliases)

    @property
    def alias_count(self) -> int:
        """已注册的 `(entity_id, alias)` 模式总数。"""
        return self._alias_count

    @classmethod
    def from_settings(
        cls,
        settings: Iterable[SettingDoc],
        archived_ids: Iterable[str] | None = None,
        *,
        archive_non_active: bool = True,
    ) -> Self:
        """直接从设定文档列表构造自动机。"""
        return cls(
            settings,
            archived_ids=archived_ids,
            archive_non_active=archive_non_active,
        )

    @classmethod
    def from_alias_rows(cls, rows: Iterable[tuple[str, str, bool | int]]) -> Self:
        """从 `ENTITY_ALIASES (alias, entity_id, is_archived)` 索引行集合构造自动机。"""
        aliases_by_entity: dict[str, list[str]] = {}
        archived_ids: set[str] = set()
        for alias, entity_id, is_arch in rows:
            eid = _ENTITY_ID_ADAPTER.validate_python(entity_id)
            aliases_by_entity.setdefault(eid, []).append(alias)
            if bool(is_arch):
                archived_ids.add(eid)
        return cls(
            aliases_by_entity,
            archived_ids=archived_ids,
            archive_non_active=False,
        )

    def build(
        self,
        entity_aliases_map: Mapping[str, Any] | Iterable[SettingDoc] | None = None,
        *,
        archived_ids: Iterable[str] | None = None,
        archive_non_active: bool = True,
    ) -> None:
        """从实体别名映射或 `SettingDoc` 集合重建自动机（先彻底清空旧状态）。"""
        self._reset()
        if entity_aliases_map is None:
            return

        archived_set: set[str] = set()
        if archived_ids is not None:
            for item in archived_ids:
                if item is None:
                    continue
                cleaned = str(item).strip()
                if cleaned:
                    archived_set.add(cleaned)

        normalized_entries = self._normalize_inputs(
            entity_aliases_map,
            archived_set=archived_set,
            archive_non_active=archive_non_active,
        )

        own_outputs: list[list[_AliasPattern]] = [[]]
        reg_order = 0

        for eid, aliases, is_arch in normalized_entries:
            self._entity_aliases[eid] = aliases
            if is_arch:
                self._archived_entities.add(eid)

            for alias in aliases:
                pat = _AliasPattern(
                    entity_id=eid,
                    alias=alias,
                    length=len(alias),
                    is_archived=is_arch,
                    reg_order=reg_order,
                )
                reg_order += 1
                self._alias_count += 1
                self._alias_lookup.setdefault(alias, []).append((eid, is_arch, pat.reg_order))

                state = 0
                trie_node = self._trie
                for ch in alias:
                    next_state = self._children[state].get(ch)
                    if next_state is None:
                        next_state = len(self._children)
                        self._children[state][ch] = next_state
                        self._children.append({})
                        self._fail.append(0)
                        self._outputs.append(())
                        own_outputs.append([])
                    state = next_state

                    next_trie = trie_node.get(ch)
                    if next_trie is None:
                        next_trie = {}
                        trie_node[ch] = next_trie
                    trie_node = next_trie

                own_outputs[state].append(pat)

        self._build_failure_links(own_outputs)

    def _normalize_inputs(
        self,
        entity_aliases_map: Mapping[str, Any] | Iterable[SettingDoc],
        *,
        archived_set: set[str],
        archive_non_active: bool,
    ) -> list[tuple[str, list[str], bool]]:
        """将多形态输入规范化并按实体 ID 合并去重。"""
        merged_aliases: dict[str, list[str]] = {}
        seen_by_entity: dict[str, set[str]] = {}
        merged_archived: dict[str, bool] = {}

        def _register(eid: str, raw_names: Iterable[Any], explicit_arch: bool) -> None:
            is_arch = (eid in archived_set) or explicit_arch
            merged_archived[eid] = merged_archived.get(eid, False) or is_arch
            bucket = merged_aliases.setdefault(eid, [])
            seen = seen_by_entity.setdefault(eid, set())

            for item in raw_names:
                if item is None:
                    continue
                if not isinstance(item, str):
                    raise TypeError(f"实体 '{eid}' 的别名必须为 str，实际得到 {type(item).__name__}")
                cleaned = item.strip()
                if not cleaned or cleaned in seen:
                    continue
                seen.add(cleaned)
                bucket.append(cleaned)

        if isinstance(entity_aliases_map, Mapping):
            for raw_eid, raw_val in entity_aliases_map.items():
                eid = _ENTITY_ID_ADAPTER.validate_python(raw_eid)
                names, explicit_arch = self._extract_mapping_value(
                    eid,
                    raw_val,
                    archive_non_active=archive_non_active,
                )
                _register(eid, names, explicit_arch)
        elif isinstance(entity_aliases_map, Iterable) and not isinstance(
            entity_aliases_map, (str, bytes, bytearray)
        ):
            for doc in entity_aliases_map:
                if not isinstance(doc, SettingDoc):
                    raise TypeError(
                        f"可迭代输入的元素必须为 SettingDoc，实际得到 {type(doc).__name__}"
                    )
                explicit_arch = archive_non_active and doc.status != EntityStatus.ACTIVE
                _register(doc.id, doc.all_names(), explicit_arch)
        else:
            raise TypeError(
                "entity_aliases_map 必须为 Mapping 或 Iterable[SettingDoc]，"
                f"实际得到 {type(entity_aliases_map).__name__}"
            )

        return [
            (eid, merged_aliases[eid], merged_archived[eid])
            for eid in merged_aliases
        ]

    @staticmethod
    def _extract_mapping_value(
        eid: str,
        raw_val: Any,
        *,
        archive_non_active: bool,
    ) -> tuple[Iterable[Any], bool]:
        """解析 Mapping 形态下单个实体的值。"""
        if isinstance(raw_val, SettingDoc):
            explicit_arch = archive_non_active and raw_val.status != EntityStatus.ACTIVE
            return raw_val.all_names(), explicit_arch

        if isinstance(raw_val, Mapping):
            collected: list[Any] = []
            raw_name = raw_val.get("name")
            if raw_name is not None:
                collected.append(raw_name)
            extra_names = raw_val.get("aliases")
            if extra_names is None:
                extra_names = raw_val.get("names", ())
            if isinstance(extra_names, str):
                collected.append(extra_names)
            elif isinstance(extra_names, Iterable) and not isinstance(
                extra_names, (bytes, bytearray)
            ):
                collected.extend(extra_names)
            elif extra_names is not None:
                raise TypeError(
                    f"实体 '{eid}' 的 aliases 必须为字符串或可迭代对象，实际得到 {type(extra_names).__name__}"
                )
            return collected, bool(raw_val.get("is_archived", False))

        if (
            isinstance(raw_val, tuple)
            and len(raw_val) == 2
            and isinstance(raw_val[1], bool)
            and not isinstance(raw_val[0], (str, bytes, bytearray))
        ):
            names_part, explicit_arch = raw_val
            if not isinstance(names_part, Iterable):
                raise TypeError(
                    f"实体 '{eid}' 的二元组第一项必须为别名可迭代对象，实际得到 {type(names_part).__name__}"
                )
            return names_part, explicit_arch

        if isinstance(raw_val, str):
            return [raw_val], False

        if isinstance(raw_val, Iterable) and not isinstance(raw_val, (bytes, bytearray)):
            return raw_val, False

        raise TypeError(f"实体 '{eid}' 的别名配置类型不支持: {type(raw_val).__name__}")

    def _build_failure_links(self, own_outputs: list[list[_AliasPattern]]) -> None:
        """层序 BFS 构建 Failure Links，并把后缀输出链一次性合并到 `_outputs`。"""
        children = self._children
        fail = self._fail
        outputs = self._outputs
        queue: deque[int] = deque()

        for child_state in children[0].values():
            fail[child_state] = 0
            outputs[child_state] = tuple(own_outputs[child_state])
            queue.append(child_state)

        while queue:
            u = queue.popleft()
            u_fail = fail[u]
            for ch, v in children[u].items():
                f = u_fail
                while f and ch not in children[f]:
                    f = fail[f]
                fail_state = children[f].get(ch, 0)
                fail[v] = fail_state

                fail_outs = outputs[fail_state]
                if own_outputs[v]:
                    outputs[v] = (*own_outputs[v], *fail_outs)
                else:
                    outputs[v] = fail_outs
                queue.append(v)

    def scan_text(self, text: str, include_archived: bool = False) -> list[MatchedEntity]:
        """在文本中扫描命中实体别名，默认跳过冷库实体，重叠命中遵循最长优先。"""
        if not isinstance(text, str):
            raise TypeError(f"scan_text 的 text 参数必须为 str，实际得到 {type(text).__name__}")

        if not text or len(self._children) <= 1:
            return []

        children = self._children
        fail = self._fail
        outputs = self._outputs

        # 候选 tuple: (start, end, is_archived, reg_order, entity_id, alias)
        raw_hits: list[tuple[int, int, bool, int, str, str]] = []
        state = 0

        for idx, ch in enumerate(text):
            while state and ch not in children[state]:
                state = fail[state]
            state = children[state].get(ch, 0)

            state_outs = outputs[state]
            if state_outs:
                end = idx + 1
                for pat in state_outs:
                    if not include_archived and pat.is_archived:
                        continue
                    raw_hits.append(
                        (
                            end - pat.length,
                            end,
                            pat.is_archived,
                            pat.reg_order,
                            pat.entity_id,
                            pat.alias,
                        )
                    )

        if not raw_hits:
            return []

        if len(raw_hits) == 1:
            s, e, is_arch, _, eid, alias = raw_hits[0]
            return [
                MatchedEntity(
                    entity_id=eid,
                    alias=alias,
                    start=s,
                    end=e,
                    is_archived=is_arch,
                )
            ]

        raw_hits.sort(key=lambda h: h[0])
        selected: list[tuple[int, int, bool, int, str, str]] = []

        cluster: list[tuple[int, int, bool, int, str, str]] = [raw_hits[0]]
        cluster_max_end = raw_hits[0][1]

        for hit in raw_hits[1:]:
            if hit[0] < cluster_max_end:
                cluster.append(hit)
                if hit[1] > cluster_max_end:
                    cluster_max_end = hit[1]
            else:
                self._resolve_cluster(cluster, selected)
                cluster = [hit]
                cluster_max_end = hit[1]

        self._resolve_cluster(cluster, selected)

        return [
            MatchedEntity(
                entity_id=h[4],
                alias=h[5],
                start=h[0],
                end=h[1],
                is_archived=h[2],
            )
            for h in selected
        ]

    @staticmethod
    def _resolve_cluster(
        cluster: list[tuple[int, int, bool, int, str, str]],
        selected: list[tuple[int, int, bool, int, str, str]],
    ) -> None:
        """在单个重叠连通簇内按最长优先 + 靠左 + 活跃优先消歧。"""
        if len(cluster) == 1:
            selected.append(cluster[0])
            return

        cluster.sort(key=lambda h: (-(h[1] - h[0]), h[0], h[2], h[3], h[4]))

        acc_starts: list[int] = []
        acc_ends: list[int] = []
        cluster_selected: list[tuple[int, int, bool, int, str, str]] = []

        for hit in cluster:
            s = hit[0]
            e = hit[1]
            pos = bisect_right(acc_starts, s)
            if pos > 0 and acc_ends[pos - 1] > s:
                continue
            if pos < len(acc_starts) and acc_starts[pos] < e:
                continue
            acc_starts.insert(pos, s)
            acc_ends.insert(pos, e)
            cluster_selected.append(hit)

        cluster_selected.sort(key=lambda h: (h[0], h[1]))
        selected.extend(cluster_selected)

    def scan_entity_ids(self, text: str, include_archived: bool = False) -> list[str]:
        """扫描文本并按首次命中顺序返回去重后的实体 ID 列表。"""
        hits = self.scan_text(text, include_archived=include_archived)
        seen: set[str] = set()
        result: list[str] = []
        for hit in hits:
            if hit.entity_id not in seen:
                seen.add(hit.entity_id)
                result.append(hit.entity_id)
        return result

    def get_aliases(self, entity_id: str) -> list[str]:
        """返回指定实体已注册的别名浅拷贝列表（未注册返回空列表）。"""
        aliases = self._entity_aliases.get(entity_id)
        return list(aliases) if aliases is not None else []

    def is_archived(self, entity_id: str) -> bool:
        """返回指定实体是否处于冷库归档状态。"""
        return entity_id in self._archived_entities

    def resolve_alias(self, alias: str, *, include_archived: bool = True) -> str | None:
        """将精确别名反查为首选实体 ID（优先活跃实体）。"""
        if not isinstance(alias, str):
            raise TypeError(f"resolve_alias 的 alias 参数必须为 str，实际得到 {type(alias).__name__}")
        cleaned = alias.strip()
        if not cleaned:
            return None
        candidates = self._alias_lookup.get(cleaned)
        if not candidates:
            return None

        best: tuple[bool, int, str] | None = None
        for eid, is_arch, reg_order in candidates:
            if not include_archived and is_arch:
                continue
            key = (is_arch, reg_order, eid)
            if best is None or key < best:
                best = key
        return best[2] if best is not None else None


__all__ = ["AliasMatcher"]
