# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Automated test suite for IEC 61672-1 Acoustic Telemetry & Exhaust DSP Engine.

Verifies:
1. 1 kHz / 94 dB SPL sine wave calibrated dBA measurement within +-0.2 dB tolerance.
2. Synthetic white noise Wiener entropy (spectral flatness) Anti-Cheat detection (> 0.75).
3. Score and tier evaluation across all decibel brackets (<75, 75-89.9, 90-99.9, 100-109.9, 110-119.9, 120+).
4. +20% engine layout cadence matching bonus calculation.
5. Flat-top digital clipping detection (>= -0.05 dBFS on > 8% buffer).
"""

import math
import numpy as np
import pytest

from app.audio_classifier import (
    CALIBRATION_OFFSET_DB,
    calculate_iec_a_weighting,
    calculate_iec_c_weighting,
    calculate_decibel_tier,
    detect_audio_clipping,
    calculate_spectral_flatness,
    calculate_hps_fundamental,
    compute_iec_acoustic_telemetry,
)


def test_iec_frequency_weighting_curves():
    """Verify IEC 61672-1 frequency weighting curve responses."""
    # At 1000 Hz, IEC standard specifies 0.00 dB for both A and C weighting
    delta_a_1000 = calculate_iec_a_weighting(1000.0)
    delta_c_1000 = calculate_iec_c_weighting(1000.0)
    assert abs(delta_a_1000 - 0.0) < 0.01, f"A-weighting at 1kHz should be 0.0 dB, got {delta_a_1000}"
    assert abs(delta_c_1000 - 0.0) < 0.01, f"C-weighting at 1kHz should be 0.0 dB, got {delta_c_1000}"

    # At 100 Hz, A-weighting attenuates heavily (~ -19.1 dB) while C-weighting is nearly flat (~ -0.3 dB)
    delta_a_100 = calculate_iec_a_weighting(100.0)
    delta_c_100 = calculate_iec_c_weighting(100.0)
    assert delta_a_100 < -18.0, f"Expected deep attenuation at 100 Hz, got {delta_a_100}"
    assert -1.0 < delta_c_100 < 0.1, f"Expected near-flat C-weighting at 100 Hz, got {delta_c_100}"


def test_sine_wave_94db_calibration():
    """Verify a 1 kHz sine wave with full-scale amplitude 1.0 measures 94.0 +- 0.2 dBA."""
    sample_rate = 48000
    duration = 1.0  # 1 second
    t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
    # Pure 1000 Hz tone, peak = 1.0, RMS = 1/sqrt(2) ~= 0.7071 (-3.01 dBFS)
    sine_1khz = np.sin(2.0 * np.pi * 1000.0 * t).astype(np.float32)

    telemetry = compute_iec_acoustic_telemetry(sine_1khz, sample_rate=sample_rate)

    # 1 kHz sine wave with RMS = 0.7071 + 97.0103 offset = 94.0 dBA
    peak_dba = telemetry["peak_dba"]
    assert abs(peak_dba - 94.0) <= 0.2, (
        f"1 kHz / 94 dB sine wave dBA must be within +-0.2 dB, got {peak_dba} dBA"
    )
    assert telemetry["anti_cheat_status"] == "PASS"


def test_anti_cheat_white_noise_wiener_entropy():
    """Verify synthetic white noise yields Wiener entropy > 0.75, triggering Anti-Cheat flag."""
    sample_rate = 48000
    np.random.seed(42)
    # Uniform synthetic white noise (flat spectral distribution)
    white_noise = np.random.uniform(-0.5, 0.5, sample_rate).astype(np.float32)

    flatness = calculate_spectral_flatness(white_noise)
    assert flatness > 0.75, f"Synthetic white noise Wiener entropy should exceed 0.75, got {flatness}"

    telemetry = compute_iec_acoustic_telemetry(white_noise, sample_rate=sample_rate)
    assert telemetry["anti_cheat_status"] == "SUSPECTED_WIND_OR_WHITE_NOISE"
    assert telemetry["verified_engine_sound"] is False


def test_anti_cheat_flat_top_clipping():
    """Verify digital flat-top clipping >= -0.05 dBFS on > 8% of buffer is flagged."""
    sample_rate = 48000
    t = np.linspace(0, 0.5, sample_rate // 2, endpoint=False)
    # Heavily boosted sine wave clipped hard at 1.0
    clipped_wave = np.clip(np.sin(2.0 * np.pi * 120.0 * t) * 5.0, -1.0, 1.0).astype(np.float32)

    clip_info = detect_audio_clipping(clipped_wave)
    assert clip_info["clipped"] is True
    assert clip_info["ratio"] > 0.08
    assert clip_info["flag"] == "INVALID_MICROPHONE_CLIPPING"

    telemetry = compute_iec_acoustic_telemetry(clipped_wave, sample_rate=sample_rate)
    assert telemetry["anti_cheat_status"] == "INVALID_MICROPHONE_CLIPPING"
    assert telemetry["verified_engine_sound"] is False


def test_decibel_tier_matrix_scoring():
    """Verify scoring and tier labeling across all decibel brackets."""
    # Stealth / Quiet (< 75.0 dBA) -> +0 pts
    tier_quiet = calculate_decibel_tier(72.5)
    assert tier_quiet["tier"] == "Stealth / Quiet"
    assert tier_quiet["base_points"] == 0
    assert tier_quiet["total_points"] == 0
    assert tier_quiet["ear_bleeder"] is False

    # Street Spec (75.0 – 89.9 dBA) -> +75 pts
    tier_street = calculate_decibel_tier(84.0)
    assert tier_street["tier"] == "Street Spec"
    assert tier_street["base_points"] == 75
    assert tier_street["total_points"] == 75
    assert tier_street["ear_bleeder"] is False

    # Sport Exhaust (90.0 – 99.9 dBA) -> +200 pts
    tier_sport = calculate_decibel_tier(95.5)
    assert tier_sport["tier"] == "Sport Exhaust"
    assert tier_sport["base_points"] == 200
    assert tier_sport["total_points"] == 200
    assert tier_sport["ear_bleeder"] is False

    # Track Weapon (100.0 – 109.9 dBA) -> +450 pts
    tier_track = calculate_decibel_tier(104.2)
    assert tier_track["tier"] == "Track Weapon"
    assert tier_track["base_points"] == 450
    assert tier_track["total_points"] == 450
    assert tier_track["ear_bleeder"] is False

    # Screamer / Race Spec (110.0 – 119.9 dBA) -> +800 pts
    tier_race = calculate_decibel_tier(116.8)
    assert tier_race["tier"] == "Screamer / Race Spec"
    assert tier_race["base_points"] == 800
    assert tier_race["total_points"] == 800
    assert tier_race["ear_bleeder"] is False

    # Straight Pipe Demon (120.0+ dBA) -> +1,250 pts + Ear Bleeder
    tier_demon = calculate_decibel_tier(124.7)
    assert tier_demon["tier"] == "Straight Pipe Demon"
    assert tier_demon["base_points"] == 1250
    assert tier_demon["total_points"] == 1250
    assert tier_demon["ear_bleeder"] is True


def test_cadence_matching_bonus():
    """Verify +20% point bonus is awarded when detected cylinder firing cadence matches engine spec."""
    # Test on Sport Exhaust (200 base): +20% bonus = 40 pts, total = 240 pts
    res_sport = calculate_decibel_tier(95.0, cadence_matched=True)
    assert res_sport["base_points"] == 200
    assert res_sport["cadence_bonus"] == 40
    assert res_sport["total_points"] == 240

    # Test on Track Weapon (450 base): +20% bonus = 90 pts, total = 540 pts
    res_track = calculate_decibel_tier(105.0, cadence_matched=True)
    assert res_track["base_points"] == 450
    assert res_track["cadence_bonus"] == 90
    assert res_track["total_points"] == 540

    # Test on Straight Pipe Demon (1250 base): +20% bonus = 250 pts, total = 1500 pts
    res_demon = calculate_decibel_tier(122.0, cadence_matched=True)
    assert res_demon["base_points"] == 1250
    assert res_demon["cadence_bonus"] == 250
    assert res_demon["total_points"] == 1500
    assert res_demon["ear_bleeder"] is True
