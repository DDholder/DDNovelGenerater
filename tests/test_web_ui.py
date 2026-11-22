"""Web UI 状态与关系可视化看板 (`ddnovel.web`) 单元测试与集成测试。"""

from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

from typer.testing import CliRunner

from ddnovel.cli import app
from ddnovel.web import (
    build_dashboard_snapshot,
    create_web_server,
    render_standalone_html,
)

FIXTURE_MYSTERY = Path(__file__).resolve().parent / "fixtures" / "books" / "mystery_intrigue"
runner = CliRunner()


def test_build_dashboard_snapshot_on_fixture_is_complete_and_readonly(tmp_path: Path) -> None:
    book_dir = tmp_path / "mystery_copy"
    shutil.copytree(FIXTURE_MYSTERY, book_dir)
    sqlite_cache = book_dir / ".ddnovel" / "index.sqlite"
    if sqlite_cache.exists():
        sqlite_cache.unlink()

    snap = build_dashboard_snapshot(book_dir)

    assert not sqlite_cache.exists()
    assert snap["warnings"] == []
    assert snap["book"]["title"] == "雾港暗桩"
    assert snap["book"]["effective_chapter"] == 24
    assert snap["book"]["next_chapter"] == 25
    assert "三号码头走私惨案" in snap["world_state"]["macro_situation"]

    # 人物状态（含认知边界与 profile_ext 线索/伪装）
    chars = {c["id"]: c for c in snap["characters"]}
    shenmo = chars["char_shenmo"]
    assert shenmo["name"] == "沈默"
    assert shenmo["is_archived"] is False
    assert len(shenmo["epistemic_bounds"]["knows"]) >= 1
    assert len(shenmo["epistemic_bounds"]["unaware_of"]) >= 1
    assert len(shenmo["epistemic_bounds"]["misbelieves"]) >= 1
    assert len(shenmo["profile_ext"]["disguises"]) >= 1
    assert len(shenmo["profile_ext"]["clues_held"]) >= 1

    # 势力状态
    factions = {f["id"]: f for f in snap["factions"]}
    assert "fac_haiguan_jisi" in factions
    assert factions["fac_haiguan_jisi"]["leader_ref"]["name"] == "罗震"

    # 双时态关系（区分第 24 章生效边与第 18 章失效边）
    shenmo_luozhen_rels = [
        r
        for r in snap["relations"]
        if r["source"] == "char_shenmo" and r["target"] == "char_luozhen"
    ]
    assert any(r["valid_from_ch"] == 1 and not r["is_currently_valid"] for r in shenmo_luozhen_rels)
    assert any(r["valid_from_ch"] == 19 and r["is_currently_valid"] for r in shenmo_luozhen_rels)

    # 伏笔台账与第 25 章雷达命中（thread_watchmaker_will 窗口 [25, 36]）
    threads = {t["id"]: t for t in snap["threads"]}
    assert "window" in threads["thread_watchmaker_will"]["radar_reasons"]
    assert snap["stats"]["radar_alerts"] >= 1

    # 历史 JSONL 事实按章降序
    assert [h["ch"] for h in snap["history"]] == [24, 18, 8]


def test_snapshot_preserves_extra_fields_and_isolates_author_notes(tmp_path: Path) -> None:
    book_dir = tmp_path / "zh_book"
    (book_dir / "00_作者设定" / "人物").mkdir(parents=True)
    (book_dir / "01_设定集" / "人物").mkdir(parents=True)
    (book_dir / "01_设定集" / "势力").mkdir(parents=True)
    (book_dir / "03_状态" / "当前").mkdir(parents=True)
    (book_dir / "03_状态" / "归档").mkdir(parents=True)

    (book_dir / "小说配置.yaml").write_text(
        "schema_version: 1\ntitle: 霜月前线\nauthor: 沐风\ncurrent_chapter: 12\n",
        encoding="utf-8",
    )
    (book_dir / "00_作者设定" / "人物" / "机密草稿.md").write_text(
        "# AUTHOR_SECRET_SHOULD_NOT_LEAK\n作者随笔灵感禁区\n",
        encoding="utf-8",
    )
    (book_dir / "01_设定集" / "人物" / "char_0001-苏玥.md").write_text(
        "---\nid: char_0001\nname: 苏玥\nstatus: active\n---\n\n## [核心]\n冷静的战术指挥官。\n",
        encoding="utf-8",
    )
    (book_dir / "01_设定集" / "势力" / "fac_mengyue-梦月军务.md").write_text(
        "---\nid: fac_mengyue\nname: 梦月军务\nstatus: active\n---\n\n## [核心]\n外环安保承包商。\n",
        encoding="utf-8",
    )
    (book_dir / "03_状态" / "世界状态.yaml").write_text(
        "current_chapter: 12\nin_world_time: 新历四十年夏\nmacro_situation: 局势稳中向好。\n",
        encoding="utf-8",
    )
    (book_dir / "03_状态" / "当前" / "人物状态.yaml").write_text(
        "char_0001:\n  updated_at_ch: 12\n  physical_mental: 精力充沛\n  current_goals:\n    - 扩编小队\n",
        encoding="utf-8",
    )
    (book_dir / "03_状态" / "当前" / "势力状态.yaml").write_text(
        "fac_mengyue:\n"
        "  updated_at_ch: 12\n"
        "  status: active\n"
        "  funds_kr: 20000\n"
        "  members:\n"
        "    - char_0001\n"
        "  notes: 首笔赏金已到账。\n",
        encoding="utf-8",
    )
    (book_dir / "03_状态" / "伏笔台账.yaml").write_text(
        "threads:\n"
        "  - id: thread_secret_fund\n"
        "    title: 神秘资助来源\n"
        "    status: active\n"
        "    planted_at: 2\n"
        "    last_progressed_at: 12\n"
        "    notes: 窗口职员暗中核销了补贴。\n",
        encoding="utf-8",
    )
    (book_dir / "03_状态" / "归档" / "人物状态.yaml").write_text(
        "char_0099:\n  updated_at_ch: 3\n  physical_mental: 已退场龙套\n",
        encoding="utf-8",
    )

    snap = build_dashboard_snapshot(book_dir)
    serialized = json.dumps(snap, ensure_ascii=False)

    assert "AUTHOR_SECRET_SHOULD_NOT_LEAK" not in serialized
    assert "作者随笔灵感禁区" not in serialized

    factions = {f["id"]: f for f in snap["factions"]}
    assert factions["fac_mengyue"]["extra_fields"]["funds_kr"] == 20000
    assert factions["fac_mengyue"]["extra_fields"]["members"] == ["char_0001"]
    assert factions["fac_mengyue"]["extra_fields"]["notes"] == "首笔赏金已到账。"

    threads = {t["id"]: t for t in snap["threads"]}
    assert threads["thread_secret_fund"]["extra_fields"]["notes"] == "窗口职员暗中核销了补贴。"

    chars = {c["id"]: c for c in snap["characters"]}
    assert chars["char_0099"]["is_archived"] is True


def test_render_standalone_html_escapes_script_close_tags(tmp_path: Path) -> None:
    book_dir = tmp_path / "xss_book"
    (book_dir / "03_状态").mkdir(parents=True)
    (book_dir / "小说配置.yaml").write_text("title: 注入测试书库\ncurrent_chapter: 1\n", encoding="utf-8")
    (book_dir / "03_状态" / "世界状态.yaml").write_text(
        'current_chapter: 1\nin_world_time: 第一天\nmacro_situation: "</script><script>alert(1)</script>"\n',
        encoding="utf-8",
    )

    snap = build_dashboard_snapshot(book_dir)
    html = render_standalone_html(snap, live_server=False)

    marker = '<script id="ddnovel-initial-data" type="application/json">'
    start = html.index(marker) + len(marker)
    end = html.index("</script>", start)
    embedded = html[start:end]

    assert "<script>alert(1)</script>" not in embedded
    assert "<\\/script><script>alert(1)<\\/script>" in embedded
    parsed = json.loads(embedded)
    assert parsed["world_state"]["macro_situation"] == "</script><script>alert(1)</script>"
    assert parsed["_live_server"] is False


def test_http_server_serves_live_snapshot_and_hot_reloads(tmp_path: Path) -> None:
    book_dir = tmp_path / "live_book"
    (book_dir / "03_状态").mkdir(parents=True)
    (book_dir / "小说配置.yaml").write_text("title: 实时服务测试\ncurrent_chapter: 5\n", encoding="utf-8")
    ws_file = book_dir / "03_状态" / "世界状态.yaml"
    ws_file.write_text(
        "current_chapter: 5\nin_world_time: 第5日\nmacro_situation: 初始局势\n",
        encoding="utf-8",
    )

    server = create_web_server(book_dir, host="127.0.0.1", port=0)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as resp:
            assert resp.status == 200
            health = json.loads(resp.read().decode("utf-8"))
            assert health["ok"] is True

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as resp:
            assert resp.status == 200
            page_html = resp.read().decode("utf-8")
            assert "实时服务测试" in page_html
            assert '"_live_server": true' in page_html

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/dashboard", timeout=3) as resp:
            data1 = json.loads(resp.read().decode("utf-8"))
            assert data1["book"]["effective_chapter"] == 5
            assert data1["world_state"]["macro_situation"] == "初始局势"

        # 原地修改磁盘状态后立即请求，确认热读取生效
        ws_file.write_text(
            "current_chapter: 6\nin_world_time: 第6日\nmacro_situation: 局势升级\n",
            encoding="utf-8",
        )
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/dashboard", timeout=3) as resp:
            data2 = json.loads(resp.read().decode("utf-8"))
            assert data2["book"]["effective_chapter"] == 6
            assert data2["world_state"]["macro_situation"] == "局势升级"

        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/not-found", timeout=3)
            raise AssertionError("预期 /not-found 返回 404")
        except urllib.error.HTTPError as err:
            assert err.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_cli_web_export_html_and_invalid_book_guard(tmp_path: Path) -> None:
    book_dir = tmp_path / "cli_book"
    init_res = runner.invoke(
        app,
        ["init", str(book_dir), "--title", "看板命令行测试", "--genre", "mystery_intrigue"],
    )
    assert init_res.exit_code == 0, init_res.output

    out_html = tmp_path / "out" / "dashboard.html"
    res = runner.invoke(
        app,
        ["web", "-d", str(book_dir), "--export-html", str(out_html)],
    )
    assert res.exit_code == 0, res.output
    assert out_html.is_file()
    html_text = out_html.read_text(encoding="utf-8")
    assert "看板命令行测试" in html_text
    assert '"_live_server": false' in html_text

    empty_dir = tmp_path / "not_a_book"
    empty_dir.mkdir()
    bad_res = runner.invoke(
        app,
        ["web", "-d", str(empty_dir), "--export-html", str(tmp_path / "bad.html")],
    )
    assert bad_res.exit_code == 1
    assert "执行失败" in bad_res.output
