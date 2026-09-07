import argparse

import pytest

from adsb_live import cli


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("max", "max"),
        ("AUTO", "auto"),
        ("agc", "agc"),
        ("42.1", 42.1),
    ],
)
def test_parse_gain_accepts_named_and_numeric_values(
    value: str, expected: object
) -> None:
    assert cli._parse_gain(value) == expected


def test_parse_gain_rejects_unknown_value() -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="gain must be"):
        cli._parse_gain("loud")


@pytest.mark.parametrize("value", ["0", "-1"])
def test_parse_positive_float_rejects_non_positive_values(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="positive number"):
        cli._parse_positive_float(value)


@pytest.mark.parametrize("value", ["0", "-2", "3", "300"])
def test_parse_pow2_int_rejects_invalid_values(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="positive power of two"):
        cli._parse_pow2_int(value)


def test_parser_uses_documented_defaults() -> None:
    args = cli._build_parser().parse_args([])

    assert args.freq == 1090e6
    assert args.rate == 2.4e6
    assert args.gain == "max"
    assert args.fft_size == 256
    assert args.demo is False


def test_main_rejects_incomplete_fixed_levels_without_importing_qt(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--vmin", "-90"])

    assert exit_code == 2
    assert "--vmin and --vmax must be provided together." in capsys.readouterr().err


def test_main_rejects_reversed_fixed_levels_without_importing_qt(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--vmin", "-20", "--vmax", "-80"])

    assert exit_code == 2
    assert "--vmax must be greater than --vmin." in capsys.readouterr().err
