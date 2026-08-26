"""RTL-SDR reader threads that emit dB spectrum rows.

Two sources are supported:

* :class:`RtlSdrSource` opens a real FlightAware / RTL2832U dongle via
  ``pyrtlsdr`` and streams synchronous reads through :class:`SpectrumComputer`.
* :class:`DemoSource` fabricates noise plus intermittent tones so the UI can
  be exercised without hardware.

Both push :class:`SpectrumRow` instances into a small drop-oldest queue so a
slow UI can never backpressure the radio.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

from .spectrum import SpectrumComputer


@dataclass(frozen=True)
class SpectrumRow:
    """One FFT hop: dB power vs frequency, plus timing metadata."""

    timestamp: float
    center_freq: float
    sample_rate: float
    power_db: np.ndarray  # shape (fft_size,), float32


class DropOldestQueue:
    """Thread-safe queue that discards the oldest item on overflow.

    The UI drains a batch each tick; when the UI stalls, old rows are
    silently dropped so the SDR thread never blocks.
    """

    def __init__(self, maxsize: int = 512) -> None:
        self._q: queue.Queue[SpectrumRow] = queue.Queue(maxsize=maxsize)

    def put(self, item: SpectrumRow) -> None:
        while True:
            try:
                self._q.put_nowait(item)
                return
            except queue.Full:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    continue

    def get_batch(self, max_items: Optional[int] = None) -> list[SpectrumRow]:
        """Return every currently queued row (up to ``max_items``)."""
        items: list[SpectrumRow] = []
        while max_items is None or len(items) < max_items:
            try:
                items.append(self._q.get_nowait())
            except queue.Empty:
                break
        return items


class _BaseSource(threading.Thread):
    def __init__(
        self,
        *,
        center_freq: float,
        sample_rate: float,
        fft_size: int,
        rows: DropOldestQueue,
        name: str,
    ) -> None:
        super().__init__(name=name, daemon=True)
        self.center_freq = float(center_freq)
        self.sample_rate = float(sample_rate)
        self.fft_size = int(fft_size)
        self.rows = rows
        self.spectrum = SpectrumComputer(self.fft_size)
        self._stop = threading.Event()
        self._error: Optional[BaseException] = None

    @property
    def error(self) -> Optional[BaseException]:
        return self._error

    def stop(self) -> None:
        self._stop.set()

    def _emit(self, samples: np.ndarray) -> None:
        power_db = self.spectrum.compute(samples)
        self.rows.put(
            SpectrumRow(
                timestamp=time.monotonic(),
                center_freq=self.center_freq,
                sample_rate=self.sample_rate,
                power_db=power_db,
            )
        )


class RtlSdrSource(_BaseSource):
    """Background thread that reads I/Q from a real RTL-SDR dongle."""

    def __init__(
        self,
        *,
        center_freq: float,
        sample_rate: float,
        gain: object,
        fft_size: int,
        rows: DropOldestQueue,
        device_index: int = 0,
    ) -> None:
        super().__init__(
            center_freq=center_freq,
            sample_rate=sample_rate,
            fft_size=fft_size,
            rows=rows,
            name="RtlSdrSource",
        )
        self.gain = gain
        self.device_index = device_index
        self._configured_gain: Optional[str] = None

    @property
    def configured_gain(self) -> str:
        return self._configured_gain or "unknown"

    def run(self) -> None:  # noqa: D401
        try:
            from rtlsdr import RtlSdr
        except Exception as exc:  # pragma: no cover - import failure path
            self._error = exc
            return

        sdr: Optional[object] = None
        try:
            sdr = RtlSdr(device_index=self.device_index)
            sdr.sample_rate = self.sample_rate
            sdr.center_freq = self.center_freq
            if isinstance(self.gain, str) and self.gain.lower() == "auto":
                sdr.gain = "auto"
                self._configured_gain = "auto"
            else:
                gain_value = float(self.gain)
                sdr.gain = gain_value
                self._configured_gain = f"{gain_value:.1f} dB"

            while not self._stop.is_set():
                samples = sdr.read_samples(self.fft_size)
                if samples is None or len(samples) < self.fft_size:
                    continue
                self._emit(np.asarray(samples[: self.fft_size], dtype=np.complex64))
        except BaseException as exc:  # noqa: BLE001
            self._error = exc
        finally:
            if sdr is not None:
                try:
                    sdr.close()
                except Exception:  # pragma: no cover - best-effort cleanup
                    pass


class DemoSource(_BaseSource):
    """Synthetic source: complex noise plus occasional narrow tones."""

    def __init__(
        self,
        *,
        center_freq: float,
        sample_rate: float,
        fft_size: int,
        rows: DropOldestQueue,
    ) -> None:
        super().__init__(
            center_freq=center_freq,
            sample_rate=sample_rate,
            fft_size=fft_size,
            rows=rows,
            name="DemoSource",
        )
        self._rng = np.random.default_rng(seed=0xAD5B)

    @property
    def configured_gain(self) -> str:
        return "demo"

    def run(self) -> None:  # noqa: D401
        try:
            n = self.fft_size
            t = np.arange(n, dtype=np.float32) / self.sample_rate
            # Pace hops at roughly the real acquisition rate so the waterfall
            # scrolls at a natural speed in demo mode.
            hop_period = n / self.sample_rate
            next_deadline = time.monotonic()
            while not self._stop.is_set():
                noise = (
                    self._rng.standard_normal(n).astype(np.float32)
                    + 1j * self._rng.standard_normal(n).astype(np.float32)
                ) * 0.05

                samples = noise
                # A couple of chirping tones so the waterfall shows structure.
                for base_offset, drift_hz in ((-0.35e6, 4e4), (0.6e6, -3e4)):
                    jitter = float(self._rng.uniform(-2e4, 2e4))
                    offset = base_offset + drift_hz * np.sin(
                        time.monotonic() * 0.3
                    ) + jitter
                    amp = 0.3 + 0.2 * float(self._rng.random())
                    samples = samples + amp * np.exp(
                        1j * 2 * np.pi * offset * t
                    ).astype(np.complex64)

                # Random narrow pulse resembling a 1090 MHz burst.
                if self._rng.random() < 0.15:
                    pulse_offset = float(self._rng.uniform(-1.0e6, 1.0e6))
                    start = int(self._rng.integers(0, n - 128))
                    length = int(self._rng.integers(32, 128))
                    pulse = np.zeros(n, dtype=np.complex64)
                    pulse[start : start + length] = 1.5 * np.exp(
                        1j * 2 * np.pi * pulse_offset * t[:length]
                    ).astype(np.complex64)
                    samples = samples + pulse

                self._emit(samples.astype(np.complex64))

                next_deadline += hop_period
                sleep_for = next_deadline - time.monotonic()
                if sleep_for > 0:
                    self._stop.wait(sleep_for)
                else:
                    next_deadline = time.monotonic()
        except BaseException as exc:  # noqa: BLE001
            self._error = exc
