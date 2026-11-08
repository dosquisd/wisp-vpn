"""Tests for the --version flag."""

from importlib.metadata import version

import pytest

from wisp.cli.parser import main, parse_args
from wisp.version import get_version


def test_get_version_reads_distribution_metadata() -> None:
    assert get_version() == version("wisp-vpn")


def test_version_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse_args(["--version"])

    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"wisp {get_version()}"


def test_version_flag_ignores_surrounding_arguments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["deploy", "oci", "--version"]) == 0
    assert capsys.readouterr().out.strip() == f"wisp {get_version()}"
