"""T02.2 全局配置模型 (`config.py`) 单元测试。"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import TypeAdapter, ValidationError

import ddnovel.core.schemas.config as config_mod
from ddnovel.core.schemas._base import HostKind
from ddnovel.core.schemas.config import (
    NOVEL_CONFIG_FILENAME,
    TRIM_ALLOWED_SLOTS,
    EmbeddingConfig,
    LifecycleConfig,
    NovelConfig,
    SlotBudget,
    TokenBudgetConfig,
)
from ddnovel.core.schemas.genres import _GENRE_REGISTRY, GenreExtBase, register_genre


@pytest.fixture
def clean_genre_registry():
    """在测试自定义题材注册前后备份并还原全局题材注册表，防止污染其他测试。"""
    backup = dict(_GENRE_REGISTRY)
    try:
        yield
    finally:
        _GENRE_REGISTRY.clear()
        _GENRE_REGISTRY.update(backup)


# ---------------------------------------------------------------------------
# 1. 默认构造、未知字段拒绝与 validate_assignment 校验
# ---------------------------------------------------------------------------


def test_default_novel_config_construction():
    """仅提供必填 title 即可构造完整默认配置，各子模型默认值与文档一致。"""
    cfg = NovelConfig(title="  凡人修仙录  ")
    assert cfg.schema_version == 1
    assert cfg.title == "凡人修仙录"
    assert cfg.author == ""
    assert cfg.genre_profile == "general"
    assert cfg.current_chapter == 0
    assert cfg.chapter_id_width == 4
    assert cfg.hosts == []

    # 预算默认配置
    assert cfg.budget.total == 10000
    assert cfg.budget.slots.guardrails == 1200
    assert cfg.budget.slots.world_and_facts == 2200
    assert cfg.budget.slots.entities == 2500
    assert cfg.budget.slots.continuity == 2000
    assert cfg.budget.slots.beats == 1800
    assert cfg.budget.slots.total_allocated() == 9700
    assert cfg.budget.tail_excerpt_min_chars == 800
    assert cfg.budget.tail_excerpt_max_chars == 1200
    assert cfg.budget.recent_summary_count == 3
    assert cfg.budget.trim_order == ["world_and_facts", "continuity", "entities"]

    # 生命周期默认配置
    assert cfg.lifecycle.ttl_keep_chapters == 5
    assert cfg.lifecycle.cold_after_chapters == 40
    assert cfg.lifecycle.stale_thread_chapters == 20
    assert cfg.lifecycle.compact_min_milestones == 15
    assert cfg.lifecycle.compact_max_milestones == 20

    # 向量插件默认配置
    assert cfg.embedding.enabled is False
    assert cfg.embedding.provider == ""
    assert cfg.embedding.model == ""
    assert cfg.embedding.dim is None
    assert cfg.embedding.api_key_env == ""
    assert cfg.embedding.rrf_k == 60


def test_empty_or_whitespace_title_rejected():
    """title 为空字符串或纯空白字符时报错。"""
    with pytest.raises(ValidationError):
        NovelConfig(title="")

    with pytest.raises(ValidationError):
        NovelConfig(title="    ")


def test_unknown_fields_forbidden_across_all_models():
    """StrictModel 严禁未知拼写错误字段（如 budgte）。"""
    with pytest.raises(ValidationError, match="budgte"):
        NovelConfig.model_validate({"title": "测试书", "budgte": {"total": 8000}})

    with pytest.raises(ValidationError):
        SlotBudget.model_validate({"unknown_slot": 500})

    with pytest.raises(ValidationError):
        TokenBudgetConfig.model_validate({"max_tokens": 10000})

    with pytest.raises(ValidationError):
        LifecycleConfig.model_validate({"archive_chapters": 50})

    with pytest.raises(ValidationError):
        EmbeddingConfig.model_validate({"api_key": "sk-secret"})


def test_validate_assignment_enforced():
    """修改已有实例属性时同样触发完整校验（validate_assignment=True）。"""
    cfg = NovelConfig(title="长夜余火")

    # 顶层属性赋值校验
    with pytest.raises(ValidationError):
        cfg.title = "   "

    with pytest.raises(ValidationError):
        cfg.genre_profile = "non_existent_genre"

    with pytest.raises(ValidationError):
        cfg.current_chapter = -1

    # 子模型属性赋值触发跨字段 model_validator
    with pytest.raises(ValidationError, match="超出差额: 4700"):
        cfg.budget.total = 5000

    with pytest.raises(ValidationError):
        cfg.budget.tail_excerpt_min_chars = 1500

    with pytest.raises(ValidationError):
        cfg.lifecycle.compact_min_milestones = 25

    with pytest.raises(ValidationError):
        cfg.embedding.enabled = True


def test_hosts_deduplication_preserves_order():
    """hosts 列表自动去重并保持首次出现顺序，同时拒绝非法宿主名。"""
    cfg = NovelConfig(
        title="多宿主测试",
        hosts=["claude", "opencode", "claude", HostKind.DSH, "antigravity", "opencode"],  # type: ignore[list-item]
    )
    assert cfg.hosts == [HostKind.CLAUDE, HostKind.OPENCODE, HostKind.DSH, HostKind.ANTIGRAVITY]

    # 属性赋值同样去重保序
    cfg.hosts = [HostKind.ANTIGRAVITY, HostKind.DSH, HostKind.DSH, HostKind.CLAUDE]
    assert cfg.hosts == [HostKind.ANTIGRAVITY, HostKind.DSH, HostKind.CLAUDE]

    with pytest.raises(ValidationError):
        NovelConfig(title="非法宿主", hosts=["cursor"])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# 2. 预算槽位求和、trim_order 与区间上下界校验
# ---------------------------------------------------------------------------


def test_slot_budget_sum_boundary_and_overflow_diff():
    """五槽之和等于 total 时通过；超出 total 时报错并明确给出差额。"""
    # 恰好相等（9700 == 9700）允许通过
    exact_budget = TokenBudgetConfig(total=9700)
    assert exact_budget.slots.total_allocated() == 9700

    # 超出预算：1200 + 2200 + 3000 + 2000 + 1800 = 10200 > 10000，差额 200
    with pytest.raises(ValidationError) as exc_info:
        TokenBudgetConfig(
            total=10000,
            slots=SlotBudget(entities=3000),
        )
    msg = str(exc_info.value)
    assert "10200" in msg
    assert "10000" in msg
    assert "200" in msg


@pytest.mark.parametrize(
    "bad_trim_order,expected_fragment",
    [
        (["guardrails", "entities"], "guardrails"),
        (["world_and_facts", "beats"], "beats"),
        (["world_and_facts", "unknown_slot"], "unknown_slot"),
        (["entities", "continuity", "entities"], "重复"),
    ],
)
def test_trim_order_rejects_protected_unknown_and_duplicate_slots(
    bad_trim_order: list[str],
    expected_fragment: str,
):
    """trim_order 含 guardrails / beats / 未知槽位 / 重复项时均报错。"""
    with pytest.raises(ValidationError) as exc_info:
        TokenBudgetConfig(trim_order=bad_trim_order)
    assert expected_fragment in str(exc_info.value)


def test_trim_order_valid_subsets_and_constant():
    """TRIM_ALLOWED_SLOTS 仅含中间三槽，允许空列表或任意无重复子集排列。"""
    assert TRIM_ALLOWED_SLOTS == frozenset({"world_and_facts", "entities", "continuity"})

    b_empty = TokenBudgetConfig(trim_order=[])
    assert b_empty.trim_order == []

    b_sub = TokenBudgetConfig(trim_order=["entities", "world_and_facts"])
    assert b_sub.trim_order == ["entities", "world_and_facts"]


def test_min_max_range_validations():
    """tail_excerpt_min_chars > max 与 compact_min_milestones > max 均报错，相等可过。"""
    # 相等边界合法
    b_eq = TokenBudgetConfig(tail_excerpt_min_chars=1000, tail_excerpt_max_chars=1000)
    assert b_eq.tail_excerpt_min_chars == b_eq.tail_excerpt_max_chars == 1000

    lc_eq = LifecycleConfig(compact_min_milestones=18, compact_max_milestones=18)
    assert lc_eq.compact_min_milestones == lc_eq.compact_max_milestones == 18

    # min > max 报错
    with pytest.raises(ValidationError, match="tail_excerpt_min_chars"):
        TokenBudgetConfig(tail_excerpt_min_chars=1201, tail_excerpt_max_chars=1200)

    with pytest.raises(ValidationError, match="compact_min_milestones"):
        LifecycleConfig(compact_min_milestones=21, compact_max_milestones=20)


# ---------------------------------------------------------------------------
# 3. EmbeddingConfig 启用校验与环境变量名安全规则
# ---------------------------------------------------------------------------


def test_embedding_disabled_zero_args_and_enabled_valid():
    """enabled=False 零参数可用；enabled=True 且填写完整参数正常通过。"""
    disabled = EmbeddingConfig()
    assert disabled.enabled is False
    assert disabled.dim is None

    enabled = EmbeddingConfig(
        enabled=True,
        provider="openai",
        model="text-embedding-3-small",
        dim=1536,
        api_key_env="OPENAI_API_KEY",
        rrf_k=50,
    )
    assert enabled.enabled is True
    assert enabled.provider == "openai"
    assert enabled.model == "text-embedding-3-small"
    assert enabled.dim == 1536
    assert enabled.api_key_env == "OPENAI_API_KEY"
    assert enabled.rrf_k == 50


@pytest.mark.parametrize(
    "kwargs,missing_field",
    [
        ({"enabled": True, "model": "bge-m3", "dim": 1024}, "provider"),
        ({"enabled": True, "provider": "   ", "model": "bge-m3", "dim": 1024}, "provider"),
        ({"enabled": True, "provider": "ollama", "dim": 1024}, "model"),
        ({"enabled": True, "provider": "ollama", "model": "bge-m3"}, "dim"),
        ({"enabled": True}, "provider"),
    ],
)
def test_embedding_enabled_missing_required_fields_raises(
    kwargs: dict[str, object],
    missing_field: str,
):
    """enabled=True 时缺少 provider / model / dim 任一项即报错并指出缺失字段。"""
    with pytest.raises(ValidationError) as exc_info:
        EmbeddingConfig.model_validate(kwargs)
    assert missing_field in str(exc_info.value)


@pytest.mark.parametrize(
    "bad_env_name",
    [
        "sk-12345",
        "sk-proj-abcdefg123456",
        "123_INVALID_START",
        "OPENAI-API-KEY",
        "API_KEY=secret",
        "Bearer token",
        "OPENAI_API_KEY\nINJECTED",
    ],
)
def test_embedding_api_key_env_rejects_plaintext_keys_and_illegal_names(bad_env_name: str):
    """api_key_env 传入 sk-... 等明文密钥或非法变量名时立即报错（无论 enabled 与否）。"""
    with pytest.raises(ValidationError, match="api_key_env"):
        EmbeddingConfig(api_key_env=bad_env_name)


@pytest.mark.parametrize("valid_env_name", ["", "OPENAI_API_KEY", "_EMBED_KEY_01", " dashscope_key "])
def test_embedding_api_key_env_accepts_valid_identifiers(valid_env_name: str):
    """合法的环境变量名（或空字符串）正常通过并自动去除首尾空白。"""
    cfg = EmbeddingConfig(api_key_env=valid_env_name)
    assert cfg.api_key_env == valid_env_name.strip()


# ---------------------------------------------------------------------------
# 4. genre_profile 注册表联动校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("genre", ["general", "cultivation_rpg", "mystery_intrigue"])
def test_builtin_genre_profiles_accepted(genre: str):
    """内置的三个题材标识均可正常通过校验。"""
    cfg = NovelConfig(title="题材校验测试", genre_profile=genre)
    assert cfg.genre_profile == genre


def test_unregistered_genre_profile_raises_with_available_options():
    """未注册的 genre_profile 抛错并在消息中提示当前所有已注册题材。"""
    with pytest.raises(ValidationError) as exc_info:
        NovelConfig(title="未知题材书", genre_profile="mecha_warfare")

    msg = str(exc_info.value)
    assert "mecha_warfare" in msg
    assert "cultivation_rpg" in msg
    assert "general" in msg
    assert "mystery_intrigue" in msg


def test_custom_registered_genre_profile_accepted(clean_genre_registry):
    """通过 register_genre 动态注册新题材后，NovelConfig 可直接选用该题材。"""

    @register_genre
    class SpaceOperaExt(GenreExtBase):
        genre_name: ClassVar[str] = "space_opera"
        display_name: ClassVar[str] = "太空歌剧"
        ship_class: str = "cruiser"

    cfg = NovelConfig(title="群星彼岸", genre_profile="space_opera")
    assert cfg.genre_profile == "space_opera"


# ---------------------------------------------------------------------------
# 5. NovelConfig.load / dump 文件读写与异常测试
# ---------------------------------------------------------------------------


def test_load_dump_roundtrip_and_chinese_readability(tmp_path: Path):
    """dump 自动建目录、中文不转义、省略 None 字段，且 load 往返完全一致。"""
    book_dir = tmp_path / "books" / "cultivation_demo"
    original = NovelConfig(
        title="大道争锋：青冥卷",
        author="卧牛真人",
        genre_profile="cultivation_rpg",
        current_chapter=42,
        chapter_id_width=4,
        budget=TokenBudgetConfig(
            total=12000,
            slots=SlotBudget(
                guardrails=1500,
                world_and_facts=2500,
                entities=3000,
                continuity=2200,
                beats=2000,
            ),
            tail_excerpt_min_chars=900,
            tail_excerpt_max_chars=1400,
            recent_summary_count=4,
            trim_order=["continuity", "world_and_facts"],
        ),
        lifecycle=LifecycleConfig(
            ttl_keep_chapters=3,
            cold_after_chapters=50,
            stale_thread_chapters=25,
            compact_min_milestones=12,
            compact_max_milestones=18,
        ),
        embedding=EmbeddingConfig(enabled=False, api_key_env="DASHSCOPE_API_KEY"),
        hosts=[HostKind.CLAUDE, HostKind.OPENCODE],
    )

    written_path = original.dump(book_dir)
    assert written_path == book_dir / NOVEL_CONFIG_FILENAME
    assert written_path.is_file()

    raw_yaml = written_path.read_text(encoding="utf-8")
    # 中文直接可读，未被转义为 \uXXXX
    assert "大道争锋：青冥卷" in raw_yaml
    assert "卧牛真人" in raw_yaml
    assert "\\u" not in raw_yaml
    # exclude_none=True：未启用的 dim=None 不写入 YAML
    assert "dim:" not in raw_yaml
    # sort_keys=False：schema_version 与 title 保持在最前
    lines = [line for line in raw_yaml.splitlines() if line.strip()]
    assert lines[0].startswith("schema_version:")
    assert lines[1].startswith("title:")

    # Path 与 str 两种参数形式加载均与原对象完全相等
    loaded_from_path = NovelConfig.load(book_dir)
    loaded_from_str = NovelConfig.load(str(book_dir))
    assert loaded_from_path == original
    assert loaded_from_str == original


def test_load_missing_file_raises_file_not_found(tmp_path: Path):
    """目标目录或 novel.yaml 不存在时抛 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        NovelConfig.load(tmp_path / "does_not_exist")

    empty_dir = tmp_path / "empty_book"
    empty_dir.mkdir()
    with pytest.raises(FileNotFoundError):
        NovelConfig.load(empty_dir)


@pytest.mark.parametrize(
    "invalid_yaml_content",
    [
        "",
        "   \n# 仅有注释的空文件\n",
        "- list_item_1\n- list_item_2\n",
        "just_a_scalar_string\n",
        "42\n",
        "title: [unclosed_bracket\n  bad: :\tindent",
    ],
)
def test_load_empty_non_mapping_or_corrupted_yaml_raises_validation_error(
    tmp_path: Path,
    invalid_yaml_content: str,
):
    """空文件、顶层非映射或语法损坏的 YAML 均统一抛出 ValidationError。"""
    yaml_file = tmp_path / NOVEL_CONFIG_FILENAME
    yaml_file.write_text(invalid_yaml_content, encoding="utf-8")

    with pytest.raises(ValidationError):
        NovelConfig.load(tmp_path)


# ---------------------------------------------------------------------------
# 6. 布尔值伪装整数拒绝测试（覆盖全部模型全部 int 字段）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bool_val", [True, False])
@pytest.mark.parametrize(
    "model_cls,base_kwargs,field_name",
    [
        # SlotBudget (5 个 int 字段)
        (SlotBudget, {}, "guardrails"),
        (SlotBudget, {}, "world_and_facts"),
        (SlotBudget, {}, "entities"),
        (SlotBudget, {}, "continuity"),
        (SlotBudget, {}, "beats"),
        # TokenBudgetConfig (4 个 int 字段)
        (TokenBudgetConfig, {}, "total"),
        (TokenBudgetConfig, {}, "tail_excerpt_min_chars"),
        (TokenBudgetConfig, {}, "tail_excerpt_max_chars"),
        (TokenBudgetConfig, {}, "recent_summary_count"),
        # LifecycleConfig (5 个 int 字段)
        (LifecycleConfig, {}, "ttl_keep_chapters"),
        (LifecycleConfig, {}, "cold_after_chapters"),
        (LifecycleConfig, {}, "stale_thread_chapters"),
        (LifecycleConfig, {}, "compact_min_milestones"),
        (LifecycleConfig, {}, "compact_max_milestones"),
        # EmbeddingConfig (2 个 int 字段：dim 与 rrf_k)
        (EmbeddingConfig, {}, "dim"),
        (EmbeddingConfig, {}, "rrf_k"),
        # NovelConfig (3 个 int 字段)
        (NovelConfig, {"title": "测试书"}, "schema_version"),
        (NovelConfig, {"title": "测试书"}, "current_chapter"),
        (NovelConfig, {"title": "测试书"}, "chapter_id_width"),
    ],
)
def test_bool_rejected_for_all_int_fields(
    model_cls: type,
    base_kwargs: dict[str, object],
    field_name: str,
    bool_val: bool,
):
    """所有整数配置字段（含可选整数 dim）在构造和属性赋值时均拒绝 True/False。"""
    init_kwargs = {**base_kwargs, field_name: bool_val}
    with pytest.raises(ValidationError):
        model_cls.model_validate(init_kwargs)

    instance = model_cls.model_validate(base_kwargs)
    with pytest.raises(ValidationError):
        setattr(instance, field_name, bool_val)


# ---------------------------------------------------------------------------
# 7. JSON Schema 兼容性与模块公开导出检查
# ---------------------------------------------------------------------------


def test_json_schema_export_and_public_all():
    """验证 NovelConfig 可正常生成序列化 JSON Schema，且 __all__ 完整准确。"""
    schema = TypeAdapter(NovelConfig).json_schema(mode="serialization")
    assert schema["type"] == "object"
    assert "title" in schema["properties"]
    assert "budget" in schema["properties"]

    expected_exports = {
        "ENV_VAR_NAME_PATTERN",
        "NOVEL_CONFIG_FILENAME",
        "TRIM_ALLOWED_SLOTS",
        "EmbeddingConfig",
        "LifecycleConfig",
        "NovelConfig",
        "SlotBudget",
        "TokenBudgetConfig",
    }
    assert set(config_mod.__all__) == expected_exports
    for name in config_mod.__all__:
        assert hasattr(config_mod, name)
