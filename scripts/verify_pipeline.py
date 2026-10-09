#!/usr/bin/env python3
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

"""CarDex Production Pipeline End-to-End Verification Runner.

Executes comprehensive verification across the CarDex stack without shell wrappers:
1. Module imports and schema verification across app/ and frontend/
2. Mock billing fallback and quota reconciliation (/api/billing/mock-success)
3. Mandatory legal endpoints (/privacy, /terms) and HTML compliance
4. Acoustic DSP 100 dB SPL calibration (+-0.5 dBA) and Anti-Cheat Wiener entropy
5. Spectrometric CIEDE2000 Delta E <= 2.0 OEM paint matching
6. Offline sync payload schema reconciliation against /api/spot
"""

import math
import os
import sys
import traceback
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import numpy as np

# Ensure repository root and subdirectories are on PYTHONPATH
REPO_ROOT = Path(__file__).resolve().parent.parent
frontend_dir = str(REPO_ROOT / "frontend")
app_dir = str(REPO_ROOT / "app")

for d in (str(REPO_ROOT), frontend_dir, app_dir):
    if d not in sys.path:
        sys.path.insert(0, d)


def log_step(name: str, status: str = "RUNNING"):
    print(f"[{status:^7}] {name}")


def verify_module_imports_and_schemas():
    """Verify clean imports and core schemas across all backend modules."""
    log_step("Verifying core module imports and schemas...", "START")

    import app.agent as agent_mod
    import app.car_tools as car_tools_mod
    import app.firestore_db as firestore_db_mod
    import app.vehicle_db as vehicle_db_mod
    import main as frontend_mod

    assert hasattr(agent_mod, "root_agent"), "app.agent must define root_agent"
    assert hasattr(car_tools_mod, "record_car_spot"), "app.car_tools must define record_car_spot"
    assert hasattr(firestore_db_mod, "get_db"), "app.firestore_db must define get_db"
    assert hasattr(vehicle_db_mod, "search_local_vehicle_database"), "app.vehicle_db must define search_local_vehicle_database"
    assert hasattr(frontend_mod, "app"), "frontend.main must define FastAPI app"

    log_step("Module imports and schemas OK", "PASS")


def verify_mock_billing_fallback():
    """Verify that when billing keys are unset, mock sandbox redirects work and update quota."""
    log_step("Verifying mock billing fallback and quota upgrade...", "START")
    from fastapi.testclient import TestClient
    import main as frontend_mod
    import quota_limiter

    # Ensure keys are unset or mock mode is active
    frontend_mod.STRIPE_SECRET_KEY = ""
    frontend_mod.IS_BILLING_MOCK = True

    client = TestClient(frontend_mod.app, follow_redirects=False)

    test_user_id = "test_verify_user_billing"
    # Reset quota to free tier with 0 refill
    quota_limiter.set_user_tier(test_user_id, "free")

    res = client.post(
        "/api/billing/stripe/create-checkout-session",
        json={"user_id": test_user_id, "type": "subscription"},
    )
    assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
    data = res.json()
    assert data.get("mock") is True, "Expected mock: True in billing fallback"
    checkout_url = data.get("checkout_url", "")
    assert "/api/billing/mock-success" in checkout_url, f"Expected mock-success URL, got {checkout_url}"

    # Hit mock success route
    mock_res = client.get(checkout_url, follow_redirects=False)
    assert mock_res.status_code in (302, 303, 307), f"Expected redirect, got {mock_res.status_code}"

    # Verify user quota updated to pro
    profile_quota = quota_limiter.get_user_quota(test_user_id)
    assert profile_quota.get("tier") == "pro", f"User tier must be 'pro', got {profile_quota.get('tier')}"

    # Test refill pack
    res_refill = client.post(
        "/api/billing/stripe/create-checkout-session",
        json={"user_id": test_user_id, "type": "refill_5"},
    )
    assert res_refill.status_code == 200
    refill_url = res_refill.json().get("checkout_url", "")
    mock_res2 = client.get(refill_url, follow_redirects=False)
    assert mock_res2.status_code in (302, 303, 307)

    log_step("Mock billing fallback and quota updates OK", "PASS")


def verify_legal_endpoints():
    """Verify that GET /privacy and GET /terms return HTTP 200 with styled HTML content."""
    log_step("Verifying legal endpoints (/privacy and /terms)...", "START")
    from fastapi.testclient import TestClient
    import main as frontend_mod

    client = TestClient(frontend_mod.app)

    res_privacy = client.get("/privacy")
    assert res_privacy.status_code == 200, f"Privacy policy returned {res_privacy.status_code}"
    assert "privacy" in res_privacy.text.lower()
    assert "<html" in res_privacy.text.lower()
    assert "camera" in res_privacy.text.lower() or "location" in res_privacy.text.lower()

    res_terms = client.get("/terms")
    assert res_terms.status_code == 200, f"Terms returned {res_terms.status_code}"
    assert "terms" in res_terms.text.lower()
    assert "<html" in res_terms.text.lower()

    log_step("Legal endpoints compliant OK", "PASS")


def verify_acoustic_dsp_and_anticheat():
    """Verify 1 kHz / 100 dB SPL sine wave accuracy (+-0.5 dBA) and white noise anti-cheat flag."""
    log_step("Verifying acoustic DSP calibration and Wiener entropy anti-cheat...", "START")
    from audio_classifier import (
        CALIBRATION_OFFSET_DB,
        compute_iec_acoustic_telemetry,
        calculate_spectral_flatness,
    )

    sample_rate = 48000
    duration = 1.0
    t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)

    # 1 kHz sine wave representing 100 dB SPL with calibrated sensitivity offset
    # Full scale amplitude 1.0 (-3.01 dBFS) with calibration offset 103.0103 yields exactly 100.0 dBA
    sine_1khz_100db = (np.sin(2.0 * np.pi * 1000.0 * t)).astype(np.float32)

    telemetry = compute_iec_acoustic_telemetry(
        sine_1khz_100db,
        sample_rate=sample_rate,
        calibration_offset_db=103.0103,
    )
    measured_dba = telemetry["peak_dba"]
    assert abs(measured_dba - 100.0) <= 0.5, (
        f"Synthetic 1 kHz / 100 dB SPL sine wave should measure 100.0 +- 0.5 dBA, got {measured_dba:.2f} dBA"
    )
    assert telemetry["anti_cheat_status"] == "PASS"

    # Anti-cheat check: synthetic white noise (flat spectral distribution)
    np.random.seed(1337)
    white_noise = np.random.uniform(-0.5, 0.5, sample_rate).astype(np.float32)
    flatness = calculate_spectral_flatness(white_noise)
    assert flatness > 0.75, f"Expected Wiener entropy > 0.75 for white noise, got {flatness}"

    telemetry_noise = compute_iec_acoustic_telemetry(white_noise, sample_rate=sample_rate)
    assert telemetry_noise["anti_cheat_status"] == "SUSPECTED_WIND_OR_WHITE_NOISE"
    assert telemetry_noise["verified_engine_sound"] is False

    log_step("Acoustic DSP 100 dBA and Anti-Cheat OK", "PASS")


def verify_ciede2000_paint_matching():
    """Verify CIEDE2000 matching against app/paint_db.json achieves Delta E <= 2.0 for known finishes."""
    log_step("Verifying CIEDE2000 OEM colorway matching...", "START")
    from paint_matcher import match_cielab_coordinates, load_paint_database

    paint_db = load_paint_database()
    assert len(paint_db) > 0, "Paint database must contain factory finishes"

    # Known finish 1: Porsche Viola Metallic [16.24, 22.53, -19.28]
    viola_match = match_cielab_coordinates([16.24, 22.53, -19.28], make_hint="Porsche")
    assert viola_match["commercial_name"] == "Viola Metallic"
    de_viola = viola_match.get("delta_e", viola_match.get("delta_e00"))
    assert de_viola <= 2.0, f"Expected Delta E <= 2.0, got {de_viola}"
    assert viola_match["multiplier"] >= 1.25

    # Known finish 2: Porsche Ruby Star Neo [41.48, 60.71, -1.21]
    ruby_match = match_cielab_coordinates([41.48, 60.71, -1.21], make_hint="Porsche")
    assert ruby_match["commercial_name"] == "Ruby Star Neo"
    de_ruby = ruby_match.get("delta_e", ruby_match.get("delta_e00"))
    assert de_ruby <= 2.0, f"Expected Delta E <= 2.0, got {de_ruby}"

    log_step("CIEDE2000 OEM Paint matching OK", "PASS")


def verify_offline_sync_schema():
    """Verify that offline_sync.js dispatch payload conforms to backend /api/spot request schema."""
    log_step("Verifying offline sync schema reconciliation against /api/spot...", "START")
    from fastapi.testclient import TestClient
    import main as frontend_mod

    # Verify key fields from offline_sync.js are present
    js_path = REPO_ROOT / "frontend" / "static" / "js" / "offline_sync.js"
    assert js_path.exists(), "offline_sync.js must exist"
    js_text = js_path.read_text(encoding="utf-8")
    for required_key in ["message", "image", "audio", "user_id", "username", "offline_synced_id"]:
        assert required_key in js_text, f"offline_sync.js must serialize {required_key}"

    client = TestClient(frontend_mod.app)

    # Realistic payload generated by offline_sync.js
    offline_payload = {
        "message": "Ferrari 488 Pista spotted in Monaco",
        "image": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
        "audio": None,
        "user_id": "test_sync_spotter",
        "username": "FieldOperative",
        "offline_synced_id": "stash_offline_uuid_999",
        "latitude": 43.7384,
        "longitude": 7.4246,
    }

    # Mock the A2A remote client to test schema ingestion and end-to-end endpoint routing
    mock_part = MagicMock()
    mock_part.root = MagicMock()
    mock_part.root.text = "Vehicle verified: Ferrari 488 Pista."
    mock_msg = MagicMock()
    mock_msg.parts = [mock_part]

    mock_a2a_client = MagicMock()
    async def mock_send_message(*args, **kwargs):
        yield mock_msg
    mock_a2a_client.send_message = mock_send_message

    with patch("main._get_card", new_callable=AsyncMock) as mock_get_card, \
         patch("main.ClientFactory") as mock_factory_cls:
        mock_factory = MagicMock()
        mock_factory.create.return_value = mock_a2a_client
        mock_factory_cls.return_value = mock_factory

        res = client.post("/api/spot", json=offline_payload)
        assert res.status_code == 200, f"/api/spot rejected offline payload: {res.status_code} {res.text}"
        data = res.json()
        assert "parts" in data or "quota" in data or "user" in data

    log_step("Offline sync schema reconciliation OK", "PASS")


def main():
    print("=" * 70)
    print("🏎️  CARDEX PRODUCTION PIPELINE VERIFICATION SUITE")
    print("=" * 70)

    checks = [
        ("Module Imports & Schemas", verify_module_imports_and_schemas),
        ("Mock Billing Fallback", verify_mock_billing_fallback),
        ("Legal Endpoints (/privacy, /terms)", verify_legal_endpoints),
        ("Acoustic DSP & Anti-Cheat", verify_acoustic_dsp_and_anticheat),
        ("CIEDE2000 OEM Paint Matcher", verify_ciede2000_paint_matching),
        ("Offline Sync Payload Schema", verify_offline_sync_schema),
    ]

    failed = []
    for name, check_fn in checks:
        try:
            check_fn()
        except Exception as e:
            traceback.print_exc()
            log_step(f"{name} FAILED: {e}", "FAIL")
            failed.append((name, str(e)))

    print("=" * 70)
    if failed:
        print(f"❌ PIPELINE VERIFICATION FAILED: {len(failed)} failure(s)")
        for name, err in failed:
            print(f"  - {name}: {err}")
        sys.exit(1)
    else:
        print("✅ ALL PRODUCTION PIPELINE VERIFICATION CHECKS PASSED SUCCESSFULLY!")
        print("=" * 70)
        sys.exit(0)


if __name__ == "__main__":
    main()
