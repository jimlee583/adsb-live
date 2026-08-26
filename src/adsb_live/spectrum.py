"""FFT helpers that turn complex I/Q blocks into dB power rows."""

from __future__ import annotations

import numpy as np


class SpectrumComputer:
    """Windowed FFT that maps a block of complex samples to a dB power row.

    The output is fftshifted so index 0 corresponds to `center_freq - rate/2`
    and the last index corresponds to `center_freq + rate/2 - rate/N`.
    """

    def __init__(self, fft_size: int) -> None:
        if fft_size <= 0 or (fft_size & (fft_size - 1)) != 0:
            raise ValueError("fft_size must be a positive power of two")
        self.fft_size = fft_size
        self._window = np.hanning(fft_size).astype(np.float32)
        # Coherent gain of a Hann window is sum(w)/N = 0.5, so normalizing by
        # sum(w) keeps tone amplitudes independent of the window.
        self._window_gain = float(self._window.sum())

    def compute(self, samples: np.ndarray) -> np.ndarray:
        """Return a dB power row of length ``fft_size`` for one FFT block."""
        if samples.shape[0] != self.fft_size:
            raise ValueError(
                f"expected {self.fft_size} samples, got {samples.shape[0]}"
            )
        block = samples.astype(np.complex64, copy=False) * self._window
        spectrum = np.fft.fftshift(np.fft.fft(block))
        magnitude = np.abs(spectrum) / self._window_gain
        # Small floor to avoid log(0).
        power_db = 20.0 * np.log10(magnitude + 1e-12)
        return power_db.astype(np.float32)

    def frequency_axis(self, center_freq: float, sample_rate: float) -> np.ndarray:
        """Frequency bin centers, in Hz, matching the fftshifted output."""
        bins = np.fft.fftshift(np.fft.fftfreq(self.fft_size, d=1.0 / sample_rate))
        return (bins + center_freq).astype(np.float64)
