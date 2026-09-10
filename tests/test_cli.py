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


def test_main_rejects_decode_with_demo_without_importing_qt(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--demo", "--decode"])

    assert exit_code == 2
    assert "--decode is incompatible with --demo" in capsys.readouterr().err


def test_main_rejects_decode_with_wrong_rate_without_importing_qt(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--decode", "--rate", "1e6"])

    assert exit_code == 2
    assert "--decode requires --rate 2.4e6" in capsys.readouterr().err


def test_main_rejects_lone_lat_without_importing_qt(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--lat", "40.0"])

    assert exit_code == 2
    assert "--lat and --lon must be provided together." in capsys.readouterr().err


def test_main_hints_when_decode_missing_receiver(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--decode without --lat/--lon should print the map-off hint."""

    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from adsb_live.decoder import Dump1090BinaryError

    def _explode(*_args: object, **_kwargs: object) -> object:
        raise Dump1090BinaryError("boom")

    monkeypatch.setattr(cli, "LiveDecoder", _explode)

    exit_code = cli.main(["--decode"])

    stderr = capsys.readouterr().err
    assert exit_code == 2
    assert "aircraft map is disabled without --lat/--lon" in stderr


@pytest.mark.parametrize("value", ["91", "-91", "abc"])
def test_parse_latitude_rejects_out_of_range(value: str) -> None:
    with pytest.raises((argparse.ArgumentTypeError, ValueError)):
        cli._parse_latitude(value)


@pytest.mark.parametrize("value", ["181", "-181", "abc"])
def test_parse_longitude_rejects_out_of_range(value: str) -> None:
    with pytest.raises((argparse.ArgumentTypeError, ValueError)):
        cli._parse_longitude(value)


# -----------------------------------------------------------------------------
# --record / --replay validation. All paths must return 2 without importing Qt.
# -----------------------------------------------------------------------------


def test_main_rejects_record_without_decode(
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--record", str(tmp_path / "s.db")])
    assert exit_code == 2
    assert "--record requires --decode" in capsys.readouterr().err


def test_main_rejects_record_with_existing_path(
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "existing.db"
    path.write_bytes(b"already here")
    exit_code = cli.main(["--decode", "--record", str(path)])
    assert exit_code == 2
    assert "refuses to overwrite" in capsys.readouterr().err


def test_main_rejects_record_with_replay(
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    existing = tmp_path / "replay.db"
    existing.write_bytes(b"stub")
    exit_code = cli.main([
        "--replay", str(existing),
        "--record", str(tmp_path / "out.db"),
    ])
    assert exit_code == 2
    err = capsys.readouterr().err
    # Either the --replay-vs-record check or the --record-vs-replay check
    # will trip first, both are correct rejections.
    assert (
        "--replay is incompatible with --record" in err
        or "--record is incompatible with --replay" in err
    )


def test_main_rejects_replay_with_decode(
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--replay", str(tmp_path / "x.db"), "--decode"])
    assert exit_code == 2
    assert "--replay is incompatible with --decode" in capsys.readouterr().err


def test_main_rejects_replay_with_demo(
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--replay", str(tmp_path / "x.db"), "--demo"])
    assert exit_code == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_main_reports_missing_replay_file(
    tmp_path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    exit_code = cli.main(["--replay", str(tmp_path / "missing.db")])
    assert exit_code == 2
    assert "failed to open replay file" in capsys.readouterr().err
