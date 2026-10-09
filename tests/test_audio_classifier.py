"""Tests for Multimodal Acoustic Engine Note Classifier and Spotting Integration.
Validates spectral cadence analysis, rev-limiter detection, dBFS loudness,
and the +25% acoustic verification bonus in the spotting pipeline.
"""
import io
import math
import struct
import wave
import base64
import pytest
from unittest.mock import MagicMock, patch

from app.audio_classifier import (
    classify_engine_note,
    get_expected_engine_config,
    _measure_dbfs,
    _detect_rev_limiter,
    _classify_spectral_cadence,
)
import app.car_tools as car_tools


def _generate_synthetic_engine_wav(
    firing_freq: float,
    duration: float = 3.0,
    sample_rate: int = 22050,
    rpm_pulsing: bool = False,
    low_freq_rumble: float = 0.0,
    harmonics: list = None,
) -> bytes:
    """Generate synthetic PCM 16-bit WAV audio mimicking vehicle exhaust notes."""
    total_samples = int(duration * sample_rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        
        frames = bytearray()
        for i in range(total_samples):
            t = i / sample_rate
            
            # Amplitude modulation for rev limiter (e.g. 10 Hz cut)
            mod = 1.0
            if rpm_pulsing:
                mod = 0.5 + 0.5 * math.sin(2 * math.pi * 10.0 * t)
                if mod < 0.3:
                    mod = 0.05
            
            # Fundamental firing tone
            sample_val = math.sin(2 * math.pi * firing_freq * t)
            
            # Low rumble sub-harmonic (cross-plane V8 crankshaft pulse)
            if low_freq_rumble > 0:
                sample_val += low_freq_rumble * math.sin(2 * math.pi * 80.0 * t)
            
            # Additional harmonics
            if harmonics:
                for h_freq, h_amp in harmonics:
                    sample_val += h_amp * math.sin(2 * math.pi * h_freq * t)
            
            # Normalize and scale to 16-bit signed integer
            scaled = int(max(-32767, min(32767, sample_val * mod * 18000)))
            frames.extend(struct.pack("<h", scaled))
            
        wf.writeframes(frames)
    return buffer.getvalue()


class TestAcousticClassifierUnit:
    """Unit tests for the audio classifier core functions."""

    def test_flat_6_classification(self):
        """Flat-6 engine (450 Hz boxer tone) on Porsche 911 GT3 RS."""
        # GT3 RS at ~9,000 RPM -> f_fire = 9000 * 6 / 120 = 450 Hz
        audio = _generate_synthetic_engine_wav(
            firing_freq=450.0,
            harmonics=[(900.0, 0.4), (1350.0, 0.3)],
        )
        res = classify_engine_note(audio, claimed_model="Porsche 911 GT3 RS", use_gemini=False)
        assert res["engine_config"] == "Flat-6"
        assert res["confidence"] >= 0.70
        assert res["acoustic_match"] is True
        assert res["bonus_multiplier"] == 1.25
        assert res["peak_loudness_dbfs"] < 0.0
        assert res["peak_loudness_dbfs"] > -40.0

    def test_cross_plane_v8_classification(self):
        """Cross-Plane V8 engine (AMG rumble + sub-100Hz bass) on Mercedes-AMG C63."""
        # AMG C63 at ~6,500 RPM -> f_fire = 6500 * 8 / 120 = 433 Hz with heavy low rumble
        audio = _generate_synthetic_engine_wav(
            firing_freq=430.0,
            low_freq_rumble=1.8, # Heavy sub-100Hz rumble
            harmonics=[(215.0, 0.7), (860.0, 0.3)],
        )
        res = classify_engine_note(audio, claimed_model="Mercedes-AMG C63", use_gemini=False)
        assert res["engine_config"] == "Cross-Plane V8"
        assert "rumble" in res["acoustic_signature"].lower()

    def test_flat_plane_v8_classification(self):
        """Flat-Plane V8 engine (screaming 560 Hz wail) on Ferrari 458 Italia."""
        # 458 Italia at ~8,500 RPM -> f_fire = 8500 * 8 / 120 = 566 Hz
        audio = _generate_synthetic_engine_wav(
            firing_freq=560.0,
            low_freq_rumble=0.0, # Crisp, no low bass rumble
            harmonics=[(1120.0, 0.5), (1680.0, 0.25)],
        )
        res = classify_engine_note(audio, claimed_model="Ferrari 458 Italia", use_gemini=False)
        assert res["engine_config"] == "Flat-Plane V8"
        assert res["acoustic_match"] is True
        assert res["bonus_multiplier"] == 1.25

    def test_v10_classification(self):
        """V10 engine (720 Hz howl) on Lamborghini Huracan."""
        # Huracan at ~8,600 RPM -> f_fire = 8600 * 10 / 120 = 716 Hz
        audio = _generate_synthetic_engine_wav(
            firing_freq=720.0,
            harmonics=[(1440.0, 0.4), (2160.0, 0.2)],
        )
        res = classify_engine_note(audio, claimed_model="Lamborghini Huracan", use_gemini=False)
        assert res["engine_config"] == "V10"
        assert res["acoustic_match"] is True
        assert res["bonus_multiplier"] == 1.25

    def test_v12_classification(self):
        """V12 engine (950 Hz soprano drone) on Ferrari Daytona SP3."""
        # Daytona SP3 at ~9,500 RPM -> f_fire = 9500 * 12 / 120 = 950 Hz
        audio = _generate_synthetic_engine_wav(
            firing_freq=950.0,
            harmonics=[(1900.0, 0.3)],
        )
        res = classify_engine_note(audio, claimed_model="Ferrari Daytona SP3", use_gemini=False)
        assert res["engine_config"] == "V12"
        assert res["acoustic_match"] is True
        assert res["bonus_multiplier"] == 1.25

    def test_turbo_4_classification(self):
        """Turbo-4 engine (200 Hz tone) on Honda Civic Type R."""
        # Civic Type R at ~6,000 RPM -> f_fire = 6000 * 4 / 120 = 200 Hz
        audio = _generate_synthetic_engine_wav(
            firing_freq=200.0,
            harmonics=[(400.0, 0.3)],
        )
        res = classify_engine_note(audio, claimed_model="Honda Civic Type R", use_gemini=False)
        assert res["engine_config"] == "Turbo-4"
        assert res["acoustic_match"] is True
        assert res["bonus_multiplier"] == 1.25

    def test_rev_limiter_detection(self):
        """Detects high-frequency amplitude bouncing from rev limiter cut."""
        audio = _generate_synthetic_engine_wav(
            firing_freq=450.0,
            duration=3.0,
            rpm_pulsing=True,
        )
        res = classify_engine_note(audio, claimed_model="Porsche 911 GT3 RS", use_gemini=False)
        assert res["rev_limiter_detected"] is True

    def test_acoustic_mismatch_no_bonus(self):
        """Claimed V12 vehicle with a 4-cylinder engine note awards 1.0x (no bonus)."""
        turbo4_audio = _generate_synthetic_engine_wav(
            firing_freq=200.0,
            harmonics=[(400.0, 0.2)],
        )
        res = classify_engine_note(turbo4_audio, claimed_model="Ferrari Daytona SP3", use_gemini=False)
        assert res["engine_config"] == "Turbo-4"
        assert res["acoustic_match"] is False
        assert res["bonus_multiplier"] == 1.0


class TestSpottingPipelineAcousticBonus:
    """Integration test for car_tools.identify_and_spot_car with acoustic bonus."""

    @patch("app.car_tools.verify_image_integrity")
    @patch("app.car_tools.storage.Client")
    @patch("app.car_tools.genai.Client")
    @patch("app.car_tools.check_and_reserve_scan")
    @patch("app.car_tools.commit_scan_deduction")
    @patch("app.car_tools.record_car_spot_entry")
    def test_spotting_with_matching_audio_bonus(
        self, mock_record, mock_commit, mock_reserve, mock_genai, mock_storage, mock_integrity
    ):
        """Spotting a Porsche 911 GT3 RS with Flat-6 audio yields +25% acoustic bonus."""
        import json
        mock_integrity.return_value = {"passed": True, "phash": "1234567890abcdef"}
        mock_reserve.return_value = (True, "OK", {"tier": "free", "daily_limit": 5, "used_today": 0, "remaining": 5})
        mock_commit.return_value = {"allowed": True, "remaining": 4}
        mock_record.return_value = {"spot_id": "spot_123", "points_awarded": 1500}

        mock_blob = MagicMock()
        mock_storage.return_value.bucket.return_value.blob.return_value = mock_blob

        mock_resp = MagicMock()
        mock_resp.text = json.dumps({
            "make": "Porsche",
            "model": "911 GT3 RS",
            "generation_or_year": "2024",
            "trim": "Weissach Package",
            "observed_colorway": "Guards Red",
            "is_special_edition_or_one_of_one": False,
            "special_edition_name": None,
            "confidence": "High",
            "key_identifying_features": ["Swan neck wing", "Fender louvers"],
            "estimated_rarity_tier": "Legendary",
            "estimated_points": 1000,
        })
        mock_genai.return_value.models.generate_content.return_value = mock_resp

        # Generate Flat-6 audio
        gt3_audio = _generate_synthetic_engine_wav(
            firing_freq=450.0,
            harmonics=[(900.0, 0.4)],
        )
        audio_b64 = "data:audio/wav;base64," + base64.b64encode(gt3_audio).decode("ascii")

        # Run spotting with audio
        result = car_tools.identify_and_spot_car(
            image_input="data:image/jpeg;base64,dGVzdA==",
            audio_input=audio_b64,
            user_id="test_spotter",
            latitude=43.7384,
            longitude=7.4246,
        )

        assert result["success"] is True
        score_info = result["dynamic_scoring"]
        # Check acoustic verification bonus in breakdown
        assert "acoustic_verification" in score_info["breakdown"]
        acoustic_entry = score_info["breakdown"]["acoustic_verification"]
        assert acoustic_entry["engine_config"] == "Flat-6"
        assert acoustic_entry["multiplier"] == 1.25
        assert acoustic_entry["applied"] is True

        # Check return fields
        assert "acoustic_analysis" in result
        assert result["acoustic_analysis"]["acoustic_match"] is True

        # Check A2UI cards include acoustic_verification
        card_types = [c.get("type") for c in result["a2ui_cards"]]
        assert "acoustic_verification" in card_types

    @patch("app.car_tools.verify_image_integrity")
    @patch("app.car_tools.storage.Client")
    @patch("app.car_tools.genai.Client")
    @patch("app.car_tools.check_and_reserve_scan")
    @patch("app.car_tools.commit_scan_deduction")
    @patch("app.car_tools.record_car_spot_entry")
    def test_spotting_without_audio_no_acoustic_bonus(
        self, mock_record, mock_commit, mock_reserve, mock_genai, mock_storage, mock_integrity
    ):
        """Spotting a Porsche 911 GT3 RS with NO audio awards standard points (no 1.25x)."""
        import json
        mock_integrity.return_value = {"passed": True, "phash": "1234567890abcdef"}
        mock_reserve.return_value = (True, "OK", {"tier": "free", "daily_limit": 5, "used_today": 0, "remaining": 5})
        mock_commit.return_value = {"allowed": True, "remaining": 4}
        mock_record.return_value = {"spot_id": "spot_123", "points_awarded": 1200}

        mock_blob = MagicMock()
        mock_storage.return_value.bucket.return_value.blob.return_value = mock_blob

        mock_resp = MagicMock()
        mock_resp.text = json.dumps({
            "make": "Porsche",
            "model": "911 GT3 RS",
            "generation_or_year": "2024",
            "trim": "Weissach Package",
            "observed_colorway": "Guards Red",
            "is_special_edition_or_one_of_one": False,
            "special_edition_name": None,
            "confidence": "High",
            "key_identifying_features": ["Swan neck wing"],
            "estimated_rarity_tier": "Legendary",
            "estimated_points": 1000,
        })
        mock_genai.return_value.models.generate_content.return_value = mock_resp

        result = car_tools.identify_and_spot_car(
            image_input="data:image/jpeg;base64,dGVzdA==",
            audio_input=None,
            user_id="test_spotter",
        )

        assert result["success"] is True
        score_info = result["dynamic_scoring"]
        assert "acoustic_verification" not in score_info["breakdown"]
        assert result.get("acoustic_analysis") is None
