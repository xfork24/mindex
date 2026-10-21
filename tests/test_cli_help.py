"""Help must work throughout the command tree without touching user data."""

import pytest
from typer import rich_utils
from typer.core import TyperGroup
from typer.main import get_command
from typer.testing import CliRunner

from mindex import cli


def command_tree(command, path=()):
    yield path, command
    if isinstance(command, TyperGroup):
        for name, child in command.commands.items():
            yield from command_tree(child, (*path, name))


COMMANDS = list(command_tree(get_command(cli.app)))


@pytest.mark.parametrize("flag", ["-h", "--help"])
@pytest.mark.parametrize("path,command", COMMANDS, ids=[" ".join(path) or "root" for path, _ in COMMANDS])
def test_help_at_every_level_without_configuration(tmp_path, monkeypatch, path, command, flag):
    def forbidden(*args, **kwargs):
        pytest.fail("Help must not load configuration or runtime settings")

    monkeypatch.setattr(cli, "load_config", forbidden)
    # Invalid YAML and a nonexistent data directory must not prevent help or create data.
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("embedding: [invalid YAML")
    data = tmp_path / "untouched"
    result = CliRunner().invoke(cli.app, ["--config", str(invalid), "--data-dir", str(data), *path, flag])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output
    assert "-h" in result.output and "--help" in result.output
    assert "╭─" in result.output
    assert not data.exists()
    # Every user argument/option is documented, including commands registered in submodules.
    assert command.help
    assert "示例" in result.output
    for parameter in command.params:
        if parameter.name not in {"install_completion", "show_completion"}:
            assert parameter.help, (path, parameter.name)


@pytest.mark.parametrize("path,command", [(p, c) for p, c in COMMANDS if isinstance(c, TyperGroup)])
def test_empty_group_shows_menu_without_loading_config(monkeypatch, path, command):
    def forbidden(*args, **kwargs):
        pytest.fail("An empty command group should show help without loading configuration")

    monkeypatch.setattr(cli, "load_config", forbidden)
    args = ["--config", "/nonexistent/config.yaml", *path] if path else []
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 2
    assert "Commands" in result.output
    for name in command.commands:
        assert name in result.output


@pytest.mark.parametrize("args", [["search", "--", "--help"], ["export", "SOURCE", "--output", "--help"]])
def test_literal_help_value_still_loads_configuration(monkeypatch, args):
    class ConfigurationRead(Exception):
        pass

    def load(*args, **kwargs):
        raise ConfigurationRead

    monkeypatch.setattr(cli, "load_config", load)
    result = CliRunner().invoke(cli.app, args)
    assert isinstance(result.exception, ConfigurationRead)


def test_help_is_complete_in_a_narrow_terminal(monkeypatch):
    monkeypatch.setattr(rich_utils, "MAX_WIDTH", 60)
    result = CliRunner().invoke(cli.app, ["backup", "import", "-h"], terminal_width=60)
    assert result.exit_code == 0
    compact = "".join(result.output.replace("│", "").split())
    assert "不发布目标数据或配置，仍需时间与暂存空间。" in compact
    assert "full完整校验；basic省略部分哈希/向量/媒体复核" in compact
    assert "…" not in result.output


def test_missing_config_does_not_block_nested_help(tmp_path):
    result = CliRunner().invoke(
        cli.app, ["--config", str(tmp_path / "missing.yaml"), "persons", "process", "-h"]
    )
    assert result.exit_code == 0, result.output
    assert "--retry" in result.output
    assert not list(tmp_path.iterdir())


def test_clean_examples_remain_separate_commands():
    result = CliRunner().invoke(cli.app, ["clean", "--help"])
    assert result.exit_code == 0
    lines = [line.strip() for line in result.output.splitlines()]
    assert "mindex clean" in lines
    assert "mindex clean --apply" in lines
    assert "mindex --config /路径/config.yaml --data-dir /路径/data clean" in lines
