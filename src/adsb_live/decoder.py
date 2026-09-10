"""Supervise a ``dump1090`` child process that reads raw I/Q from stdin.

``adsb-live`` keeps exclusive ownership of the RTL-SDR dongle. To also
demodulate ADS-B messages, this module spawns ``dump1090`` with

    dump1090 --ifile - --iformat UC8 --write-json <tmpdir>

and pipes the raw UC8 byte blocks (produced by :class:`~adsb_live.sdr.RtlSdrSource`
via its ``iq_sink``) into the child's stdin. The child periodically writes
``aircraft.json`` into the temp directory, which the existing
:class:`~adsb_live.dump1090.Dump1090JsonSource` polls and normalises.

The supervisor is intentionally hardware-free and can be exercised in tests
by pointing ``binary_path`` at a small Python script that reads stdin and
writes an ``aircraft.json`` into ``output_dir``.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from .dump1090 import Dump1090JsonSource, FileSnapshotReader
from .sdr import DropOldestByteQueue
from .tracks import TrackStore


class Dump1090BinaryError(RuntimeError):
    """Raised when the ``dump1090`` binary cannot be located."""


@dataclass(frozen=True, slots=True)
class Dump1090ProcessHealth:
    """Thread-safe snapshot of the child decoder's supervisor state."""

    running: bool = False
    pid: int | None = None
    started_at: float | None = None
    exits: int = 0
    restarts: int = 0
    last_exit_code: int | None = None
    last_stderr_line: str | None = None
    bytes_written: int = 0
    blocks_dropped_on_write: int = 0


def _resolve_binary(path: str | None) -> str:
    if path:
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            raise Dump1090BinaryError(f"dump1090 binary not found at {candidate}")
        return str(candidate)
    found = shutil.which("dump1090") or shutil.which("dump1090-fa")
    if not found:
        raise Dump1090BinaryError(
            "dump1090 was not found on PATH. Install dump1090-fa or "
            "readsb-compatible dump1090, or pass --dump1090-path."
        )
    return found


class Dump1090Process(threading.Thread):
    """Own the ``dump1090`` child process end-to-end.

    Responsibilities:

    * spawn ``dump1090`` with the right ``--ifile -``/``--iformat UC8``
      arguments and a per-process ``--write-json`` directory;
    * feed raw I/Q byte blocks from ``iq_sink`` into the child's stdin;
    * drain the child's stderr into a bounded ring buffer (an undrained
      pipe eventually blocks the child);
    * restart the child with linear backoff if it dies while we're still
      supposed to be running.

    ``stop()`` is the intended shutdown path. During shutdown a non-zero
    exit code is expected (dump1090 prints ``Abnormal exit`` and returns 1
    on stdin EOF) and is not surfaced as an error.
    """

    def __init__(
        self,
        *,
        iq_sink: DropOldestByteQueue,
        output_dir: Path,
        binary_path: str | None = None,
        receiver_lat: float | None = None,
        receiver_lon: float | None = None,
        write_json_every_s: float = 1.0,
        restart_backoff_s: float = 1.0,
        max_restart_backoff_s: float = 10.0,
        stderr_history: int = 32,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(name="Dump1090Process", daemon=True)
        if not math.isfinite(write_json_every_s) or write_json_every_s <= 0.0:
            raise ValueError("write_json_every_s must be positive and finite")
        if not math.isfinite(restart_backoff_s) or restart_backoff_s <= 0.0:
            raise ValueError("restart_backoff_s must be positive and finite")
        if max_restart_backoff_s < restart_backoff_s:
            raise ValueError(
                "max_restart_backoff_s must be >= restart_backoff_s"
            )
        if receiver_lat is not None and not -90.0 <= float(receiver_lat) <= 90.0:
            raise ValueError("receiver_lat must be between -90 and 90 degrees")
        if receiver_lon is not None and not -180.0 <= float(receiver_lon) <= 180.0:
            raise ValueError("receiver_lon must be between -180 and 180 degrees")

        self.binary = _resolve_binary(binary_path)
        self.iq_sink = iq_sink
        self.output_dir = Path(output_dir)
        self.receiver_lat = None if receiver_lat is None else float(receiver_lat)
        self.receiver_lon = None if receiver_lon is None else float(receiver_lon)
        self.write_json_every_s = float(write_json_every_s)
        self.restart_backoff_s = float(restart_backoff_s)
        self.max_restart_backoff_s = float(max_restart_backoff_s)

        self._clock = clock
        self._stop_event = threading.Event()
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._proc_lock = threading.Lock()
        self._writer_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stderr_lines: deque[str] = deque(maxlen=max(1, stderr_history))
        self._stderr_lock = threading.Lock()

        self._health_lock = threading.Lock()
        self._health = Dump1090ProcessHealth()

    # ------------------------------------------------------------------
    # public API

    @property
    def health(self) -> Dump1090ProcessHealth:
        with self._health_lock:
            return self._health

    def stderr_snapshot(self) -> tuple[str, ...]:
        with self._stderr_lock:
            return tuple(self._stderr_lines)

    def build_argv(self) -> list[str]:
        argv = [
            self.binary,
            "--ifile",
            "-",
            "--iformat",
            "UC8",
            "--quiet",
            "--write-json",
            str(self.output_dir),
            "--write-json-every",
            _format_number(self.write_json_every_s),
        ]
        if self.receiver_lat is not None and self.receiver_lon is not None:
            argv += [
                "--lat",
                _format_number(self.receiver_lat),
                "--lon",
                _format_number(self.receiver_lon),
                "--json-location-accuracy",
                "1",
            ]
        return argv

    def stop(self, timeout: float = 3.0) -> None:
        """Signal shutdown and wait for the supervisor thread to exit."""
        self._stop_event.set()
        self._terminate_current_process()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=timeout)

    # ------------------------------------------------------------------
    # thread body

    def run(self) -> None:  # noqa: D401
        self.output_dir.mkdir(parents=True, exist_ok=True)
        backoff = self.restart_backoff_s
        first_iteration = True
        try:
            while not self._stop_event.is_set():
                if not first_iteration:
                    with self._health_lock:
                        self._health = replace(
                            self._health, restarts=self._health.restarts + 1
                        )
                first_iteration = False

                try:
                    self._run_one_child()
                except Exception as exc:  # noqa: BLE001
                    self._record_stderr(
                        f"supervisor error: {type(exc).__name__}: {exc}"
                    )

                if self._stop_event.is_set():
                    break

                self._stop_event.wait(backoff)
                backoff = min(backoff * 2.0, self.max_restart_backoff_s)
        finally:
            with self._health_lock:
                self._health = replace(self._health, running=False, pid=None)

    # ------------------------------------------------------------------
    # child process lifecycle

    def _run_one_child(self) -> None:
        argv = self.build_argv()
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv is trusted
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError as exc:
            raise Dump1090BinaryError(
                f"failed to spawn dump1090: {exc}"
            ) from exc

        with self._proc_lock:
            self._proc = proc
        started_at = self._clock()
        with self._health_lock:
            self._health = replace(
                self._health,
                running=True,
                pid=proc.pid,
                started_at=started_at,
                last_exit_code=None,
            )

        writer = threading.Thread(
            target=self._writer_loop,
            args=(proc,),
            name="Dump1090ProcessWriter",
            daemon=True,
        )
        stderr_reader = threading.Thread(
            target=self._stderr_loop,
            args=(proc,),
            name="Dump1090ProcessStderr",
            daemon=True,
        )
        self._writer_thread = writer
        self._stderr_thread = stderr_reader
        writer.start()
        stderr_reader.start()

        exit_code = proc.wait()

        # Best-effort: close stdin so the writer thread wakes up.
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:  # pragma: no cover - defensive
            pass

        writer.join(timeout=2.0)
        stderr_reader.join(timeout=2.0)

        with self._proc_lock:
            self._proc = None

        with self._health_lock:
            self._health = replace(
                self._health,
                running=False,
                pid=None,
                exits=self._health.exits + 1,
                last_exit_code=exit_code,
            )

    def _terminate_current_process(self) -> None:
        with self._proc_lock:
            proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:  # pragma: no cover - already closed
                    pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:  # pragma: no cover - hung child
                    proc.kill()
                    proc.wait(timeout=1.0)
        except Exception:  # pragma: no cover - defensive
            pass

    # ------------------------------------------------------------------
    # writer / stderr loops

    def _writer_loop(self, proc: subprocess.Popen[bytes]) -> None:
        stdin = proc.stdin
        if stdin is None:  # pragma: no cover - always PIPE
            return
        try:
            while not self._stop_event.is_set() and proc.poll() is None:
                block = self.iq_sink.get(timeout=0.25)
                if block is None:
                    continue
                try:
                    stdin.write(block)
                    stdin.flush()
                except (BrokenPipeError, ValueError, OSError):
                    # ValueError: I/O on closed file. OSError covers EPIPE
                    # variants. All three mean "child is gone, exit quietly".
                    with self._health_lock:
                        self._health = replace(
                            self._health,
                            blocks_dropped_on_write=(
                                self._health.blocks_dropped_on_write + 1
                            ),
                        )
                    return
                with self._health_lock:
                    self._health = replace(
                        self._health,
                        bytes_written=self._health.bytes_written + len(block),
                    )
        finally:
            try:
                stdin.close()
            except Exception:  # pragma: no cover - already closed
                pass

    def _stderr_loop(self, proc: subprocess.Popen[bytes]) -> None:
        stream = proc.stderr
        if stream is None:  # pragma: no cover - always PIPE
            return
        try:
            for raw_line in iter(stream.readline, b""):
                try:
                    line = raw_line.decode("utf-8", errors="replace").rstrip()
                except Exception:  # pragma: no cover - decode never raises here
                    continue
                if line:
                    self._record_stderr(line)
        finally:
            try:
                stream.close()
            except Exception:  # pragma: no cover - already closed
                pass

    def _record_stderr(self, line: str) -> None:
        with self._stderr_lock:
            self._stderr_lines.append(line)
        with self._health_lock:
            self._health = replace(self._health, last_stderr_line=line)


def _format_number(value: float) -> str:
    """Format a float compactly for the ``dump1090`` command line."""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


class LiveDecoder:
    """Bundle the child decoder, its JSON poller, and a shared track store.

    The CLI constructs one of these when ``--decode`` is passed, hands the
    embedded ``iq_sink`` to :class:`~adsb_live.sdr.RtlSdrSource`, and gives
    the ``store`` to the UI table. ``start()`` and ``stop()`` bring the
    child process and the poller up and down in the right order and clean
    up the temporary ``aircraft.json`` directory.

    ``kind = "decode"`` disambiguates this class from :class:`ReplaySource`
    when the UI status bar wants to distinguish live decoding from playback.
    """

    kind = "decode"

    def __init__(
        self,
        *,
        store: TrackStore | None = None,
        iq_sink: DropOldestByteQueue | None = None,
        binary_path: str | None = None,
        receiver_lat: float | None = None,
        receiver_lon: float | None = None,
        poll_interval_s: float = 1.0,
        write_json_every_s: float = 1.0,
        iq_sink_maxsize: int = 64,
        stale_after_s: float = 60.0,
        output_dir: Path | None = None,
        on_poll: Callable[[TrackStore], None] | None = None,
    ) -> None:
        self.store = store if store is not None else TrackStore(
            stale_after_s=stale_after_s
        )
        self.iq_sink = iq_sink if iq_sink is not None else DropOldestByteQueue(
            maxsize=iq_sink_maxsize
        )
        self._owns_output_dir = output_dir is None
        if output_dir is None:
            self._tempdir = tempfile.mkdtemp(prefix="adsb-live-dump1090-")
            output_dir_path = Path(self._tempdir)
        else:
            self._tempdir = None
            output_dir_path = Path(output_dir)
            output_dir_path.mkdir(parents=True, exist_ok=True)
        self.output_dir = output_dir_path

        self.process = Dump1090Process(
            iq_sink=self.iq_sink,
            output_dir=self.output_dir,
            binary_path=binary_path,
            receiver_lat=receiver_lat,
            receiver_lon=receiver_lon,
            write_json_every_s=write_json_every_s,
        )
        self.poller = Dump1090JsonSource(
            reader=FileSnapshotReader(self.output_dir / "aircraft.json"),
            store=self.store,
            poll_interval_s=poll_interval_s,
            on_poll=on_poll,
        )
        self._started = False
        self._stopped = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        # Process first so aircraft.json exists by the time the poller wakes.
        self.process.start()
        self.poller.start()

    def stop(self, timeout: float = 3.0) -> None:
        if self._stopped:
            return
        self._stopped = True
        # Poller first: stop trying to read the JSON before it disappears.
        self.poller.stop()
        if self.poller.is_alive():
            self.poller.join(timeout=timeout)
        self.process.stop(timeout=timeout)
        if self._tempdir is not None:
            shutil.rmtree(self._tempdir, ignore_errors=True)
