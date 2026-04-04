"""Tests for top-level CLI flags."""

from click.testing import CliRunner

from observational_memory import __version__
from observational_memory.cli import cli


def test_version_flag_outputs_package_version():
    runner = CliRunner()

    result = runner.invoke(cli, ["--version"])

    assert result.exit_code == 0, result.output
    assert __version__ in result.output
