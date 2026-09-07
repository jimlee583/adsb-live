"""Hardware-free tests for the ``dump1090`` supervisor and ``LiveDecoder``.

The real ``dump1090`` binary is not available on CI. These tests stand in
a tiny Python script that mimics the parts of the ``dump1090`` contract the
supervisor actually depends on (reads stdin, writes ``aircraft.json``,
optionally exits after N seconds, optionally exits early).
"""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest

from adsb_live.decoder import (
    Dump1090BinaryError,
    Dump1090Process,
    LiveDecoder,
    _resolve_binary,
)
from adsb_live.sdr import DropOldestByteQueue


_FAKE_DUMP1090 = textwrap.dedent(
    """
    import json, os, sys, threading, time
    from pathlib import Path

    out_dir = None
    write_every = 1.0
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == '--write-json':
            out_dir = Path(args[i + 1])
        elif a == '--write-json-every':
            write_every = float(args[i + 1])
    exit_after = os.environ.get('FAKE_EXIT_AFTER')
    exit_after = float(exit_after) if exit_after else None
    exit_immediately = os.environ.get('FAKE_EXIT_IMMEDIATELY') == '1'
    if out_dir is None:
        sys.stderr.write('missing --write-json\\n')
        sys.exit(2)
    sys.stderr.write(f'fake dump1090 booted argc={len(args)}\\n')
    sys.stderr.flush()
    if exit_immediately:
        sys.exit(3)
    if exit_after is not None:
        # Force exit even if the parent isn't feeding stdin, so restart
        # accounting can be observed in tests.
        def _timeout():
            time.sleep(exit_after)
            os._exit(0)
        threading.Thread(target=_timeout, daemon=True).start()
    total = 0
    last_write = 0.0
    while True:
        chunk = sys.stdin.buffer.read(4096)
        if not chunk:
            break
        total += len(chunk)
        now = time.monotonic()
        if now - last_write >= write_every:
            (out_dir / 'aircraft.json').write_text(json.dumps({
                'now': now,
                'messages': total,
                'aircraft': [{'hex': 'A12345', 'flight': 'TEST1234',
                              'lat': 40.0, 'lon': -105.0, 'seen': 0.1}],
            }))
            last_write = now
    sys.stderr.write(f'fake dump1090 exit total={total}\\n')
    """
)


@pytest.fixture
def fake_binary(tmp_path: Path) -> Path:
    script = tmp_path / "fake_dump1090.py"
    script.write_text(_FAKE_DUMP1090)
    shim = tmp_path / "dump1090_shim.sh"
    shim.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def _wait_for(predicate, *, timeout: float = 3.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_resolve_binary_uses_explicit_path(fake_binary: Path) -> None:
    assert _resolve_binary(str(fake_binary)) == str(fake_binary)


def test_resolve_binary_missing_explicit_path_raises(tmp_path: Path) -> None:
    with pytest.raises(Dump1090BinaryError, match="not found"):
        _resolve_binary(str(tmp_path / "nope"))


def test_resolve_binary_uses_path(monkeypatch: pytest.MonkeyPatch, fake_binary: Path) -> None:
    monkeypatch.setattr(
        "shutil.which",
        lambda name: str(fake_binary) if name == "dump1090" else None,
    )
    assert _resolve_binary(None) == str(fake_binary)


def test_resolve_binary_missing_from_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(Dump1090BinaryError, match="not found on PATH"):
        _resolve_binary(None)


def test_build_argv_adds_lat_lon(fake_binary: Path, tmp_path: Path) -> None:
    proc = Dump1090Process(
        iq_sink=DropOldestByteQueue(maxsize=1),
        output_dir=tmp_path / "out",
        binary_path=str(fake_binary),
        receiver_lat=40.5,
        receiver_lon=-105.25,
        write_json_every_s=1.0,
    )
    argv = proc.build_argv()
    assert argv[0] == str(fake_binary)
    assert "--ifile" in argv and "-" in argv
    assert "--iformat" in argv and "UC8" in argv
    assert "--write-json" in argv
    assert argv[argv.index("--write-json") + 1] == str(tmp_path / "out")
    assert "--lat" in argv and "40.5" in argv
    assert "--lon" in argv and "-105.25" in argv


def test_build_argv_omits_lat_lon_when_not_set(
    fake_binary: Path, tmp_path: Path
) -> None:
    proc = Dump1090Process(
        iq_sink=DropOldestByteQueue(maxsize=1),
        output_dir=tmp_path / "out",
        binary_path=str(fake_binary),
    )
    argv = proc.build_argv()
    assert "--lat" not in argv and "--lon" not in argv


def test_process_writes_json_and_shuts_down_cleanly(
    fake_binary: Path, tmp_path: Path
) -> None:
    sink = DropOldestByteQueue(maxsize=8)
    out_dir = tmp_path / "out"
    proc = Dump1090Process(
        iq_sink=sink,
        output_dir=out_dir,
        binary_path=str(fake_binary),
        write_json_every_s=0.1,
        restart_backoff_s=0.2,
    )
    proc.start()
    try:
        for _ in range(5):
            sink.put(os.urandom(4096))
            time.sleep(0.05)

        assert _wait_for(lambda: (out_dir / "aircraft.json").exists())
        payload = json.loads((out_dir / "aircraft.json").read_text())
        assert payload["aircraft"][0]["hex"] == "A12345"

        health = proc.health
        assert health.running is True
        assert health.pid is not None
        assert health.bytes_written > 0
    finally:
        proc.stop(timeout=3.0)

    assert not proc.is_alive()
    final = proc.health
    assert final.running is False
    assert final.exits >= 1
    # SIGTERM path -> exit_code is negative (e.g. -15) or 0/1 depending on OS.


def test_process_restarts_after_early_exit(
    fake_binary: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_EXIT_AFTER", "0.2")
    out_dir = tmp_path / "out"
    proc = Dump1090Process(
        iq_sink=DropOldestByteQueue(maxsize=8),
        output_dir=out_dir,
        binary_path=str(fake_binary),
        write_json_every_s=1.0,
        restart_backoff_s=0.05,
        max_restart_backoff_s=0.1,
    )
    proc.start()
    try:
        assert _wait_for(lambda: proc.health.restarts >= 1, timeout=3.0)
        health = proc.health
        assert health.exits >= 1
        assert health.restarts >= 1
    finally:
        proc.stop(timeout=3.0)

    assert not proc.is_alive()


def test_process_records_stderr(
    fake_binary: Path, tmp_path: Path
) -> None:
    proc = Dump1090Process(
        iq_sink=DropOldestByteQueue(maxsize=4),
        output_dir=tmp_path / "out",
        binary_path=str(fake_binary),
        write_json_every_s=1.0,
    )
    proc.start()
    try:
        assert _wait_for(
            lambda: any("booted" in line for line in proc.stderr_snapshot()),
            timeout=2.0,
        )
    finally:
        proc.stop(timeout=2.0)


def test_live_decoder_cleans_up_temp_output_dir(fake_binary: Path) -> None:
    decoder = LiveDecoder(binary_path=str(fake_binary), poll_interval_s=0.1,
                          write_json_every_s=0.1)
    tmp = decoder.output_dir
    assert tmp.exists()
    decoder.start()
    try:
        for _ in range(3):
            decoder.iq_sink.put(os.urandom(4096))
            time.sleep(0.05)
        assert _wait_for(lambda: (tmp / "aircraft.json").exists(), timeout=3.0)
        assert _wait_for(
            lambda: decoder.poller.health.snapshots_processed >= 1,
            timeout=3.0,
        )
        # Store should have picked up the fake aircraft.
        assert _wait_for(
            lambda: len(decoder.store.snapshot()) >= 1, timeout=3.0
        )
    finally:
        decoder.stop(timeout=3.0)
    assert not tmp.exists()


def test_live_decoder_stop_before_start_is_safe(fake_binary: Path) -> None:
    decoder = LiveDecoder(binary_path=str(fake_binary))
    tmp = decoder.output_dir
    assert tmp.exists()
    decoder.stop()
    assert not tmp.exists()
