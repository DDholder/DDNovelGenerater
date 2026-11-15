"""T02.2 全局配置模型 (`novel.yaml`)：预算分配、生命周期阈值、可选向量配置与读写接口。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Final, Self

import yaml
from pydantic import BeforeValidator, Field, ValidationError, field_validator, model_validator
from pydantic_core import InitErrorDetails, PydanticCustomError

from ._base import HostKind, StrictModel
from .genres import list_genres

NOVEL_CONFIG_FILENAME: Final[str] = "小说配置.yaml"
_LEGACY_NOVEL_CONFIG_FILENAME: Final[str] = "novel.yaml"
TRIM_ALLOWED_SLOTS: Final[frozenset[str]] = frozenset(
    {"world_and_facts", "entities", "continuity"}
)
ENV_VAR_NAME_PATTERN: Final[str] = r"^[A-Za-z_][A-Za-z0-9_]*$"
_ENV_VAR_NAME_RE: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def _reject_bool_int(value: Any) -> Any:
    """拒绝将布尔值 True/False 静默当作整数 1/0。"""
    if isinstance(value, bool):
        raise ValueError("整数配置项不能传入布尔值 True/False")
    return value


NonNegativeInt = Annotated[int, Field(ge=0), BeforeValidator(_reject_bool_int)]
PositiveInt = Annotated[int, Field(ge=1), BeforeValidator(_reject_bool_int)]


# ---------------------------------------------------------------------------
# 1. 写作上下文五槽位预算配置
# ---------------------------------------------------------------------------


class SlotBudget(StrictModel):
    """写作上下文包（Context Packet）五槽位 Token 预算分配。"""

    guardrails: NonNegativeInt = Field(default=1200, description="槽位 1：文风红线与防剧透守则预算")
    world_and_facts: NonNegativeInt = Field(default=2200, description="槽位 2：世界观与近期历史事实预算")
    entities: NonNegativeInt = Field(default=2500, description="槽位 3：出场实体设定与当前截面预算")
    continuity: NonNegativeInt = Field(default=2000, description="槽位 4：上章尾部切片与连续性摘要预算")
    beats: NonNegativeInt = Field(default=1800, description="槽位 5：本章蓝图与分场节拍预算")

    def total_allocated(self) -> int:
        """返回五槽位预算之和。"""
        return (
            self.guardrails
            + self.world_and_facts
            + self.entities
            + self.continuity
            + self.beats
        )


class TokenBudgetConfig(StrictModel):
    """全局 Token 预算与溢出裁剪策略配置。"""

    total: PositiveInt = Field(default=10000, description="单章写作包总 Token 预算上限")
    slots: SlotBudget = Field(default_factory=SlotBudget, description="五槽位各自预算分配")
    tail_excerpt_min_chars: NonNegativeInt = Field(
        default=800, description="上一章尾部原文摘录最小字符数"
    )
    tail_excerpt_max_chars: PositiveInt = Field(
        default=1200, description="上一章尾部原文摘录最大字符数"
    )
    recent_summary_count: NonNegativeInt = Field(
        default=3, description="注入近期章节摘要的章数"
    )
    trim_order: list[str] = Field(
        default_factory=lambda: ["world_and_facts", "continuity", "entities"],
        description="超预算时的裁剪优先级顺序（仅允许中间三槽，首尾槽位永不可裁）",
    )

    @field_validator("trim_order")
    @classmethod
    def _validate_trim_order(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        allowed_display = sorted(TRIM_ALLOWED_SLOTS)
        for slot in value:
            if slot in {"guardrails", "beats"}:
                raise ValueError(
                    f"trim_order 不可包含受保护的首尾槽位 '{slot}'（guardrails 与 beats 永不可裁，"
                    f"仅允许: {allowed_display}）"
                )
            if slot not in TRIM_ALLOWED_SLOTS:
                raise ValueError(
                    f"trim_order 包含未知槽位 '{slot}'，仅允许: {allowed_display}"
                )
            if slot in seen:
                raise ValueError(f"trim_order 中存在重复槽位 '{slot}'")
            seen.add(slot)
        return value

    @model_validator(mode="after")
    def _validate_budget_constraints(self) -> Self:
        slot_sum = self.slots.total_allocated()
        if slot_sum > self.total:
            over_by = slot_sum - self.total
            raise ValueError(
                f"五槽位预算总和 ({slot_sum}) 超出总预算 ({self.total})，超出差额: {over_by}"
            )
        if self.tail_excerpt_min_chars > self.tail_excerpt_max_chars:
            raise ValueError(
                f"tail_excerpt_min_chars ({self.tail_excerpt_min_chars}) "
                f"不能大于 tail_excerpt_max_chars ({self.tail_excerpt_max_chars})"
            )
        return self


# ---------------------------------------------------------------------------
# 2. 记忆生命周期与归档浓缩配置
# ---------------------------------------------------------------------------


class LifecycleConfig(StrictModel):
    """状态生命周期、冷热归档与卷末浓缩阈值配置。"""

    ttl_keep_chapters: NonNegativeInt = Field(
        default=5, description="临时缓存产物（如写作包）保留的最近章数"
    )
    cold_after_chapters: PositiveInt = Field(
        default=40, description="实体连续未出场超过该章数后归入冷库候选"
    )
    stale_thread_chapters: PositiveInt = Field(
        default=20, description="伏笔连续未推进超过该章数后触发断线预警"
    )
    compact_min_milestones: PositiveInt = Field(
        default=15, description="分卷浓缩里程碑最小条数"
    )
    compact_max_milestones: PositiveInt = Field(
        default=20, description="分卷浓缩里程碑最大条数"
    )

    @model_validator(mode="after")
    def _validate_compact_milestone_range(self) -> Self:
        if self.compact_min_milestones > self.compact_max_milestones:
            raise ValueError(
                f"compact_min_milestones ({self.compact_min_milestones}) "
                f"不能大于 compact_max_milestones ({self.compact_max_milestones})"
            )
        return self


# ---------------------------------------------------------------------------
# 3. 可选插拔向量检索配置
# ---------------------------------------------------------------------------


class EmbeddingConfig(StrictModel):
    """可选向量检索插件配置（默认关闭，绝不落盘明文密钥）。"""

    enabled: bool = Field(default=False, description="是否启用向量语义检索插件")
    provider: str = Field(default="", description="向量模型服务提供方标识")
    model: str = Field(default="", description="Embedding 模型名称")
    dim: PositiveInt | None = Field(default=None, description="向量维度（启用时必填正整数）")
    api_key_env: str = Field(
        default="", description="存放 API Key 的环境变量名（严禁直接填写明文密钥）"
    )
    rrf_k: PositiveInt = Field(default=60, description="RRF 混合检索融合常数 k")

    @field_validator("api_key_env")
    @classmethod
    def _validate_api_key_env(cls, value: str) -> str:
        if value and not _ENV_VAR_NAME_RE.fullmatch(value):
            raise ValueError(
                f"api_key_env 必须是合法环境变量名格式 ({ENV_VAR_NAME_PATTERN})，"
                f"严禁写入明文 API Key，收到: {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _validate_enabled_fields(self) -> Self:
        if self.enabled:
            missing: list[str] = []
            if not self.provider:
                missing.append("provider")
            if not self.model:
                missing.append("model")
            if self.dim is None:
                missing.append("dim")
            if missing:
                raise ValueError(
                    f"当 embedding.enabled=True 时，以下字段必填: {', '.join(missing)}"
                )
        return self


# ---------------------------------------------------------------------------
# 4. 顶层书籍全局配置 (`novel.yaml`)
# ---------------------------------------------------------------------------


class NovelConfig(StrictModel):
    """单本书籍真源根目录 `novel.yaml` 顶层配置模型。"""

    schema_version: PositiveInt = Field(default=1, description="Schema 契约版本号")
    title: str = Field(..., min_length=1, description="作品标题")
    author: str = Field(default="", description="作者署名")
    genre_profile: str = Field(default="general", description="题材扩展配置标识")
    current_chapter: NonNegativeInt = Field(default=0, description="当前已定稿最新章号（0 表示尚未开篇）")
    chapter_id_width: PositiveInt = Field(default=4, description="章节文件编号零填充宽度")
    budget: TokenBudgetConfig = Field(default_factory=TokenBudgetConfig, description="Token 预算配置")
    lifecycle: LifecycleConfig = Field(default_factory=LifecycleConfig, description="生命周期阈值配置")
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig, description="可选向量检索配置")
    hosts: list[HostKind] = Field(default_factory=list, description="目标 AI 宿主环境列表（自动去重保序）")

    @field_validator("genre_profile")
    @classmethod
    def _validate_genre_profile(cls, value: str) -> str:
        available = list_genres()
        if value not in available:
            raise ValueError(
                f"未注册的题材配置 '{value}'，当前可选题材列表: {available}"
            )
        return value

    @field_validator("hosts")
    @classmethod
    def _deduplicate_hosts(cls, value: list[HostKind]) -> list[HostKind]:
        return list(dict.fromkeys(value))

    @classmethod
    def load(cls, book_dir: Path | str) -> Self:
        """从 ``小说配置.yaml``（兼容旧版 ``novel.yaml``）读取并校验全局配置。

        Raises:
            FileNotFoundError: 当配置文件不存在时抛出。
            ValidationError: 当 YAML 为空、语法损坏、顶层非映射或字段校验失败时抛出。
        """
        root = Path(book_dir)
        config_path = root / NOVEL_CONFIG_FILENAME
        if not config_path.is_file():
            legacy_path = root / _LEGACY_NOVEL_CONFIG_FILENAME
            if legacy_path.is_file():
                config_path = legacy_path
        if not config_path.is_file():
            raise FileNotFoundError(f"未找到书籍配置文件: {config_path}")

        raw_text = config_path.read_text(encoding="utf-8")
        try:
            data = yaml.safe_load(raw_text)
        except yaml.YAMLError as exc:
            raise ValidationError.from_exception_data(
                title=cls.__name__,
                line_errors=[
                    InitErrorDetails(
                        type=PydanticCustomError(
                            "yaml_parse_error",
                            "novel.yaml 解析失败: {error}",
                            {"error": str(exc)},
                        ),
                        loc=(),
                        input=raw_text,
                    )
                ],
            ) from exc

        if not isinstance(data, dict):
            raise ValidationError.from_exception_data(
                title=cls.__name__,
                line_errors=[
                    InitErrorDetails(
                        type=PydanticCustomError(
                            "yaml_root_not_mapping",
                            "novel.yaml 顶层必须为非空键值映射（dict），实际得到: {actual_type}",
                            {"actual_type": type(data).__name__},
                        ),
                        loc=(),
                        input=data,
                    )
                ],
            )

        return cls.model_validate(data)

    def dump(self, book_dir: Path | str) -> Path:
        """将当前配置序列化写入 ``小说配置.yaml``（若为旧版书库则写回 ``novel.yaml``）并返回写入路径。"""
        target_dir = Path(book_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        config_path = target_dir / NOVEL_CONFIG_FILENAME
        legacy_path = target_dir / _LEGACY_NOVEL_CONFIG_FILENAME
        if not config_path.is_file() and legacy_path.is_file():
            config_path = legacy_path

        payload = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        yaml_text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        config_path.write_text(yaml_text, encoding="utf-8")
        return config_path


__all__ = [
    "ENV_VAR_NAME_PATTERN",
    "NOVEL_CONFIG_FILENAME",
    "TRIM_ALLOWED_SLOTS",
    "EmbeddingConfig",
    "LifecycleConfig",
    "NovelConfig",
    "SlotBudget",
    "TokenBudgetConfig",
]
