import numpy as np
import pytest

from adsb_live.spectrum import SpectrumComputer


@pytest.mark.parametrize("fft_size", [0, -2, 3, 12])
def test_rejects_invalid_fft_sizes(fft_size: int) -> None:
    with pytest.raises(ValueError, match="positive power of two"):
        SpectrumComputer(fft_size)


def test_compute_places_tone_in_expected_frequency_bin() -> None:
    fft_size = 256
    sample_rate = 2.4e6
    center_freq = 1090e6
    tone_bin = 12
    tone_offset = tone_bin * sample_rate / fft_size
    sample_numbers = np.arange(fft_size)
    samples = np.exp(
        2j * np.pi * tone_offset * sample_numbers / sample_rate
    ).astype(np.complex64)

    computer = SpectrumComputer(fft_size)
    power_db = computer.compute(samples)
    frequencies = computer.frequency_axis(center_freq, sample_rate)
    peak_index = int(np.argmax(power_db))

    assert power_db.shape == (fft_size,)
    assert power_db.dtype == np.float32
    assert frequencies.dtype == np.float64
    assert frequencies[peak_index] == pytest.approx(center_freq + tone_offset)
    assert power_db[peak_index] == pytest.approx(0.0, abs=1e-5)


def test_frequency_axis_covers_shifted_sample_band() -> None:
    computer = SpectrumComputer(8)

    frequencies = computer.frequency_axis(center_freq=100.0, sample_rate=80.0)

    np.testing.assert_array_equal(
        frequencies,
        np.array([60.0, 70.0, 80.0, 90.0, 100.0, 110.0, 120.0, 130.0]),
    )


def test_compute_rejects_wrong_sample_count() -> None:
    computer = SpectrumComputer(256)

    with pytest.raises(ValueError, match="expected 256 samples, got 128"):
        computer.compute(np.zeros(128, dtype=np.complex64))
