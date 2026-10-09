"""Multimodal Acoustic Engine Note Classifier for CarDex.

Analyzes vehicle exhaust audio recordings (5-10s WAV, MP3, AAC, WebM, OGG) to:
1. Measure peak exhaust loudness in dBFS and detect rev-limiter bouncing.
2. Extract acoustic cylinder firing cadence (f_fire = RPM * N_cyl / 120) and harmonic orders.
3. Classify engine configuration: Flat-6, Flat-Plane V8, Cross-Plane V8, V10, V12, or Turbo-4.
4. Verify acoustic authenticity against vehicle engine specs in vehicles.sqlite / CarDex catalog.
5. Award +25% Acoustic Verification bonus multiplier (1.25x) upon matching.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import math
import os
import subprocess
import wave
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

VERTEX_PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "qwiklabs-gcp-04-6f324b699fdd")
VERTEX_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")

# Supported engine configurations
ENGINE_CONFIGS = [
    "Flat-6",
    "Flat-Plane V8",
    "Cross-Plane V8",
    "V10",
    "V12",
    "Turbo-4",
]

# Vehicle model keyword mapping for instant high-confidence engine spec lookup
KNOWN_ENGINE_MAP: dict[str, str] = {
    # Flat-6 (Porsche boxer engines)
    "gt3": "Flat-6",
    "gt3 rs": "Flat-6",
    "gt3rs": "Flat-6",
    "911": "Flat-6",
    "carrera": "Flat-6",
    "turbo s": "Flat-6",
    "992": "Flat-6",
    "991": "Flat-6",
    "gt4": "Flat-6",
    "cayman gt4": "Flat-6",
    "boxster spyder": "Flat-6",

    # Flat-Plane V8 (180-deg crank, screaming high-RPM wail)
    "458": "Flat-Plane V8",
    "488": "Flat-Plane V8",
    "f8": "Flat-Plane V8",
    "sf90": "Flat-Plane V8",
    "720s": "Flat-Plane V8",
    "765lt": "Flat-Plane V8",
    "senna": "Flat-Plane V8",
    "p1": "Flat-Plane V8",
    "artura": "Flat-Plane V8",
    "z06": "Flat-Plane V8",
    "gt350": "Flat-Plane V8",
    "918 spyder": "Flat-Plane V8",

    # Cross-Plane V8 (90-deg crank, deep bass burble/rumble)
    "c63": "Cross-Plane V8",
    "e63": "Cross-Plane V8",
    "amg gt": "Cross-Plane V8",
    "g63": "Cross-Plane V8",
    "mustang gt": "Cross-Plane V8",
    "mustang": "Cross-Plane V8",
    "corvette stingray": "Cross-Plane V8",
    "corvette": "Cross-Plane V8",
    "hellcat": "Cross-Plane V8",
    "demon": "Cross-Plane V8",
    "charger": "Cross-Plane V8",
    "challenger": "Cross-Plane V8",
    "m5": "Cross-Plane V8",
    "m8": "Cross-Plane V8",
    "rs6": "Cross-Plane V8",
    "rs7": "Cross-Plane V8",

    # V10 (odd-order musical howl)
    "huracan": "V10",
    "huracán": "V10",
    "gallardo": "V10",
    "r8": "V10",
    "lfa": "V10",
    "carrera gt": "V10",
    "viper": "V10",
    "e60 m5": "V10",

    # V12 (ultra-smooth high-frequency soprano drone)
    "daytona sp3": "V12",
    "812": "V12",
    "812 superfast": "V12",
    "812 competizione": "V12",
    "laferrari": "V12",
    "enzo": "V12",
    "f12": "V12",
    "aventador": "V12",
    "revuelto": "V12",
    "murcielago": "V12",
    "dbs": "V12",
    "vanquish": "V12",
    "valkyrie": "V12",
    "zonda": "V12",
    "huayra": "V12",
    "utopia": "V12",
    "chiron": "V12",

    # Turbo-4 (inline/flat 4-cylinder, punchy exhaust overrun crackles)
    "a45": "Turbo-4",
    "cla 45": "Turbo-4",
    "civic type r": "Turbo-4",
    "type r": "Turbo-4",
    "golf r": "Turbo-4",
    "gti": "Turbo-4",
    "s3": "Turbo-4",
    "wrx": "Turbo-4",
    "sti": "Turbo-4",
    "gr corolla": "Turbo-4",
    "gr yaris": "Turbo-4",
    "718 cayman": "Turbo-4",
    "718 boxster": "Turbo-4",
}


def _convert_to_pcm_wav(audio_bytes: bytes, mime_type: str = "audio/wav") -> tuple[np.ndarray, int]:
    """Decodes raw audio bytes (WAV, MP3, AAC, WebM, OGG) to a mono float32 numpy array.

    Returns:
        (samples_array, sample_rate)
    """
    if not audio_bytes:
        raise ValueError("Audio bytes payload is empty")

    # If it is already a valid WAV container, decode directly with python wave
    if audio_bytes.startswith(b"RIFF") and b"WAVE" in audio_bytes[:16]:
        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
                sr = wf.getframerate()
                n_channels = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                n_frames = wf.getnframes()
                raw_frames = wf.readframes(n_frames)

                if sampwidth == 2:
                    samples = np.frombuffer(raw_frames, dtype=np.int16).astype(np.float32) / 32768.0
                elif sampwidth == 1:
                    samples = (np.frombuffer(raw_frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
                elif sampwidth == 4:
                    samples = np.frombuffer(raw_frames, dtype=np.int32).astype(np.float32) / 2147483648.0
                else:
                    samples = np.frombuffer(raw_frames, dtype=np.int16).astype(np.float32) / 32768.0

                if n_channels > 1:
                    samples = samples.reshape(-1, n_channels).mean(axis=1)

                return samples, sr
        except Exception as e:
            logger.debug("Direct wave reading failed, falling back to ffmpeg: %s", e)

    # Use ffmpeg for compressed audio formats (WebM, Opus, MP3, AAC, OGG)
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-f",
                "wav",
                "-ac",
                "1",
                "-ar",
                "44100",
                "pipe:1",
            ],
            input=audio_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=10,
        )
        with wave.open(io.BytesIO(proc.stdout), "rb") as wf:
            sr = wf.getframerate()
            raw_frames = wf.readframes(wf.getnframes())
            samples = np.frombuffer(raw_frames, dtype=np.int16).astype(np.float32) / 32768.0
            return samples, sr
    except Exception as e:
        logger.warning("ffmpeg audio transcoding failed: %s. Generating fallback envelope.", e)

    # Final fallback if neither succeeds: synthesize synthetic samples from byte variance
    sr = 44100
    raw_arr = np.frombuffer(audio_bytes[: min(len(audio_bytes), 88200)], dtype=np.uint8).astype(np.float32)
    norm = (raw_arr - 128.0) / 128.0
    return norm, sr


def get_expected_engine_config(claimed_model: str) -> str | None:
    """Resolves the expected engine configuration from model name and vehicles.sqlite specs.

    Returns:
        One of 'Flat-6', 'Flat-Plane V8', 'Cross-Plane V8', 'V10', 'V12', 'Turbo-4', or None.
    """
    if not claimed_model:
        return None

    model_lower = claimed_model.strip().lower()

    # 1. Match known high-performance model keywords
    for keyword, config in KNOWN_ENGINE_MAP.items():
        if keyword in model_lower:
            return config

    # 2. Query vehicles.sqlite database for cylinder count and powertrain details
    try:
        from app import vehicle_db

        records = vehicle_db.search_local_vehicle_database(claimed_model, limit=3)
        if records:
            cyl = records[0].get("cylinders")
            make = (records[0].get("make") or "").lower()
            eng_label = (records[0].get("engine") or "").lower()

            if cyl == 6:
                if "porsche" in make or "boxer" in eng_label or "flat" in eng_label:
                    return "Flat-6"
                return "Flat-6" if "911" in model_lower else "V6"
            elif cyl == 8:
                if any(k in model_lower for k in ["ferrari", "mclaren", "z06", "gt350"]):
                    return "Flat-Plane V8"
                return "Cross-Plane V8"
            elif cyl == 10:
                return "V10"
            elif cyl in (12, 16):
                return "V12"
            elif cyl == 4:
                return "Turbo-4"
    except Exception as e:
        logger.debug("Local vehicle db lookup failed in audio classifier: %s", e)

    return None


def _measure_dbfs(samples: np.ndarray) -> float:
    """Measures peak exhaust loudness in dBFS relative to full digital scale."""
    if len(samples) == 0:
        return -96.0
    peak = np.max(np.abs(samples))
    if peak <= 1e-6:
        return -96.0
    dbfs = 20.0 * math.log10(min(peak, 1.0))
    return max(-96.0, min(0.0, dbfs))


def _detect_rev_limiter(samples: np.ndarray, sample_rate: int) -> bool:
    """Detects periodic 5Hz-25Hz amplitude modulation characteristic of fuel/ignition cut rev-limiters."""
    if len(samples) < sample_rate * 0.3:
        return False

    frame_len = int(sample_rate * 0.04)
    hop_len = int(sample_rate * 0.02)
    n_frames = (len(samples) - frame_len) // hop_len

    if n_frames < 15:
        return False

    rms_env = np.zeros(n_frames, dtype=np.float32)
    for i in range(n_frames):
        start = i * hop_len
        chunk = samples[start : start + frame_len]
        rms_env[i] = np.sqrt(np.mean(chunk**2) + 1e-9)

    rms_detrend = rms_env - np.mean(rms_env)
    env_max = np.max(np.abs(rms_detrend))
    if env_max < 0.03:
        return False

    env_sr = sample_rate / hop_len
    env_fft = np.abs(np.fft.rfft(rms_detrend))
    env_freqs = np.fft.rfftfreq(len(rms_detrend), 1.0 / env_sr)

    limiter_mask = (env_freqs >= 4.0) & (env_freqs <= 25.0)
    if not np.any(limiter_mask):
        return False

    limiter_peak = np.max(env_fft[limiter_mask])
    total_energy = np.mean(env_fft) + 1e-6

    return (limiter_peak / total_energy) > 2.8


def _classify_spectral_cadence(samples: np.ndarray, sample_rate: int) -> tuple[str, float, float, dict[str, Any]]:
    """Analyzes FFT spectral harmonics to determine cylinder firing cadence and tone.

    Returns:
        (detected_config, confidence, peak_firing_freq, spectral_meta)
    """
    if len(samples) == 0:
        return "Turbo-4", 0.50, 200.0, {}

    window = np.hanning(len(samples))
    windowed = samples * window

    fft_magnitudes = np.abs(np.fft.rfft(windowed))
    freqs = np.fft.rfftfreq(len(samples), 1.0 / sample_rate)

    valid_mask = (freqs >= 30.0) & (freqs <= 4000.0)
    if not np.any(valid_mask):
        return "Turbo-4", 0.50, 200.0, {}

    sub_freqs = freqs[valid_mask]
    sub_mag = fft_magnitudes[valid_mask]

    peak_idx = np.argmax(sub_mag)
    f0 = float(sub_freqs[peak_idx])

    bass_energy = float(np.sum(fft_magnitudes[(freqs >= 30.0) & (freqs < 240.0)] ** 2))
    mid_energy = float(np.sum(fft_magnitudes[(freqs >= 240.0) & (freqs < 800.0)] ** 2))
    high_energy = float(np.sum(fft_magnitudes[(freqs >= 800.0) & (freqs < 3500.0)] ** 2))
    total_energy = bass_energy + mid_energy + high_energy + 1e-9

    bass_ratio = bass_energy / total_energy
    mid_ratio = mid_energy / total_energy
    high_ratio = high_energy / total_energy

    # Firing cadence classification based on physical acoustics
    v8_firing_energy = float(np.sum(fft_magnitudes[(freqs >= 280.0) & (freqs <= 480.0)] ** 2))
    if f0 < 130.0 and (v8_firing_energy / total_energy) > 0.08:
        config = "Cross-Plane V8"
        conf = min(0.95, 0.85 + (bass_ratio * 0.10))
    elif f0 <= 270.0:
        config = "Turbo-4"
        conf = min(0.95, 0.80 + (bass_ratio * 0.15))
    elif f0 >= 820.0 or (f0 >= 720.0 and high_ratio >= 0.40):
        config = "V12"
        conf = min(0.96, 0.82 + (high_ratio * 0.15))
    elif f0 >= 600.0:
        config = "V10"
        conf = min(0.95, 0.80 + (mid_ratio * 0.15))
    elif f0 >= 490.0 and bass_ratio < 0.35:
        config = "Flat-Plane V8"
        conf = min(0.94, 0.82 + (mid_ratio * 0.15))
    elif (270.0 < f0 <= 480.0 and bass_ratio >= 0.35) or (280.0 <= f0 <= 470.0 and bass_ratio > mid_ratio):
        config = "Cross-Plane V8"
        conf = min(0.95, 0.82 + (bass_ratio * 0.15))
    elif 380.0 <= f0 <= 550.0:
        config = "Flat-6"
        conf = min(0.95, 0.84 + (mid_ratio * 0.12))
    else:
        config = "Turbo-4"
        conf = min(0.92, 0.78 + (bass_ratio * 0.15))

    meta = {
        "f0_hz": round(f0, 1),
        "bass_ratio": round(float(bass_ratio), 3),
        "mid_ratio": round(float(mid_ratio), 3),
        "high_ratio": round(float(high_ratio), 3),
    }

    return config, conf, f0, meta


def _gemini_multimodal_audio_verify(
    audio_bytes: bytes,
    claimed_model: str,
    mime_type: str = "audio/wav",
) -> dict[str, Any] | None:
    """Optionally queries Gemini Multimodal Audio to cross-verify engine sound."""
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(enterprise=True, project=VERTEX_PROJECT_ID, location=VERTEX_LOCATION)
        prompt = f"""You are CarDex's master automotive acoustic engineer. Analyze this vehicle exhaust audio clip.
The user claims this vehicle is: '{claimed_model}'.

Classify the engine configuration into EXACTLY ONE of:
- Flat-6 (Porsche boxer howl, 9000 RPM metallic scream)
- Flat-Plane V8 (Ferrari/McLaren crisp 180-deg tenor wail)
- Cross-Plane V8 (AMG/Mustang deep guttural burble/rumble)
- V10 (Lamborghini Huracan/LFA odd-order musical howl)
- V12 (Ferrari Daytona SP3/Aventador ultra-high soprano drone)
- Turbo-4 (hot hatch/inline-4 punch with turbo overrun)

Return ONLY a raw JSON object:
{{
  "engine_config": "Flat-6",
  "confidence": 0.95,
  "acoustic_match": true,
  "signature_description": "Sharp 9,000 RPM naturally aspirated boxer valvetrain scream",
  "rev_limiter": true
}}"""

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                prompt,
            ],
        )

        resp_text = response.text.strip()
        if resp_text.startswith("```"):
            lines = resp_text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            resp_text = "\n".join(lines).strip()

        data = json.loads(resp_text)
        if isinstance(data, dict) and "engine_config" in data:
            return data
    except Exception as e:
        logger.debug("Gemini multimodal audio call skipped or failed: %s", e)

    return None


def classify_engine_note(
    audio_bytes: bytes,
    claimed_model: str = "",
    mime_type: str = "audio/wav",
    use_gemini: bool = False,
) -> dict[str, Any]:
    """Classifies an exhaust audio recording and verifies match with claimed vehicle model.

    Args:
        audio_bytes: Raw 5-10 second audio recording (WAV, MP3, AAC, WebM, OGG).
        claimed_model: Vehicle model name (e.g. 'Porsche 911 GT3 RS', 'Ferrari 458', 'Mercedes-AMG C63').
        mime_type: Audio MIME format.

    Returns:
        dict containing:
        - engine_config: Detected configuration string.
        - confidence: Float confidence score (0.0 to 1.0).
        - acoustic_match: Boolean indicating whether detected audio matches the vehicle spec.
        - bonus_multiplier: 1.25 (+25% point bonus) if match is verified, else 1.0.
        - peak_loudness_dbfs: Measured peak exhaust level in dBFS.
        - rev_limiter_detected: Boolean whether ignition/fuel cut was detected.
        - firing_frequency_hz: Dominant cylinder firing frequency in Hz.
        - expected_config: Expected engine spec for the vehicle model.
    """
    if not audio_bytes:
        return {
            "engine_config": "Unknown",
            "confidence": 0.0,
            "acoustic_match": False,
            "bonus_multiplier": 1.0,
            "peak_loudness_dbfs": -96.0,
            "rev_limiter_detected": False,
            "firing_frequency_hz": 0.0,
            "expected_config": None,
            "acoustic_signature": "No audio signal detected.",
            "sound_level": "Silent",
        }

    # 1. Decode audio to mono PCM samples
    samples, sr = _convert_to_pcm_wav(audio_bytes, mime_type=mime_type)

    # 2. Measure peak exhaust loudness in dBFS
    peak_dbfs = _measure_dbfs(samples)

    # 3. Detect rev limiter stuttering
    rev_limiter = _detect_rev_limiter(samples, sr)

    # 4. Spectral harmonic analysis
    config_fft, conf_fft, f0_hz, meta = _classify_spectral_cadence(samples, sr)

    # 5. Determine expected configuration from vehicle specifications
    expected_config = get_expected_engine_config(claimed_model)

    # 6. Optional Multimodal Gemini verification
    gemini_result = _gemini_multimodal_audio_verify(audio_bytes, claimed_model, mime_type=mime_type) if use_gemini else None

    if gemini_result and gemini_result.get("engine_config") in ENGINE_CONFIGS:
        detected_config = gemini_result["engine_config"]
        confidence = float(gemini_result.get("confidence", conf_fft))
        signature_desc = gemini_result.get("signature_description")
        if "rev_limiter" in gemini_result:
            rev_limiter = rev_limiter or bool(gemini_result["rev_limiter"])
    else:
        detected_config = config_fft
        confidence = conf_fft
        signature_desc = None

    # 7. Evaluate acoustic match
    if expected_config:
        acoustic_match = (detected_config == expected_config)
    else:
        acoustic_match = (confidence >= 0.85)

    # 8. Signature description generator
    if not signature_desc:
        if detected_config == "Flat-6":
            signature_desc = "High-pitched metallic boxer rasp with 9,000 RPM valvetrain scream"
        elif detected_config == "Flat-Plane V8":
            signature_desc = "Crisp 180° crankshaft tenor scream with even 4th-order harmonics"
        elif detected_config == "Cross-Plane V8":
            signature_desc = "Deep guttural sub-200 Hz rumble with asymmetric 90° exhaust pulsing"
        elif detected_config == "V10":
            signature_desc = "Odd-order musical howl with resonant acoustic dissonance"
        elif detected_config == "V12":
            signature_desc = "Ultra-smooth high-frequency soprano drone with harmonic density"
        else:
            signature_desc = "Turbocharged 4-cylinder exhaust punch with rapid wastegate response"

    # Sound level label
    if peak_dbfs >= -3.0:
        sound_level = "Deafening / Open Exhaust Valve (>115 dB)"
    elif peak_dbfs >= -12.0:
        sound_level = "Extremely Loud Track Spec (100–110 dB)"
    elif peak_dbfs >= -24.0:
        sound_level = "Sport Exhaust Active"
    else:
        sound_level = "Muffled / Idle RPM"

    bonus_multiplier = 1.25 if acoustic_match else 1.0

    return {
        "engine_config": detected_config,
        "confidence": round(float(confidence), 2),
        "acoustic_match": bool(acoustic_match),
        "bonus_multiplier": bonus_multiplier,
        "peak_loudness_dbfs": round(float(peak_dbfs), 1),
        "rev_limiter_detected": bool(rev_limiter),
        "firing_frequency_hz": round(float(f0_hz), 1),
        "expected_config": expected_config,
        "acoustic_signature": signature_desc,
        "sound_level": sound_level,
        "spectral_meta": meta,
    }
