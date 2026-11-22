"""T01 脚手架冒烟测试。"""

from typer.testing import CliRunner


def test_import_package():
    import ddnovel

    assert ddnovel.__version__


def test_subpackages_importable():
    import ddnovel.core
    import ddnovel.core.schemas
    import ddnovel.templates  # noqa: F401
    import ddnovel.web  # noqa: F401


def test_cli_version():
    from ddnovel import __version__
    from ddnovel.cli import app

    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_mcp_server_factory():
    from ddnovel.mcp_server import create_server

    assert create_server().name == "ddnovel"
