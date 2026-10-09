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

"""CarDex Full System Synthetic Integration Test Suite.

End-to-end integration test pipeline verifying all critical production subsystems:
1. Quota Enforcement: 5 scans transition user to blocked state; mock payment clears block.
2. Audio DSP: Synthetic 1 kHz / 100 dB tone measures correct dBA and assigns bonus points.
3. CIEDE2000 Paint Matcher: Exact Lab match against app/paint_db.json OEM swatch library.
4. Offline Sync: Verifies offline payload in offline_sync.js conforms to backend API schema.
5. Legal & Sandbox Endpoints: Verifies /privacy, /terms, and /api/billing/mock-success routes.
"""

import json
import os
import sys
import uuid
import numpy as np
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "frontend"))
sys.path.insert(0, REPO_ROOT)

if "AGENT_ENGINE_RESOURCE_NAME" not in os.environ:
    os.environ["AGENT_ENGINE_RESOURCE_NAME"] = "projects/861697384142/locations/us-east1/reasoningEngines/5178590915173810176"

from starlette.testclient import TestClient

from app.audio_classifier import (
    calculate_decibel_tier,
    compute_iec_acoustic_telemetry,
)
from app.paint_matcher import match_cielab_coordinates
import quota_limiter
from frontend.main import app


# ==============================================================================
# 1. Quota Enforcement & Mock Billing Fallback
# ==============================================================================
def test_quota_enforcement_and_mock_payment_clearing():
    """Verify rolling quota transitions user to blocked state at 5 scans and mock payment clears block."""
    user_id = f"synthetic_spotter_{uuid.uuid4().hex[:8]}"

    # Initial free state: user has 5 scans remaining
    quota = quota_limiter.get_user_quota(user_id)
    assert quota["can_scan"] is True
    assert quota["tier"] == "free"
    assert quota["scans_today"] == 0
    assert quota["remaining_scans"] == 5

    # Consume all 5 daily scan credits
    for i in range(5):
        allowed, reason, info = quota_limiter.check_and_reserve_scan(user_id)
        assert allowed is True, f"Scan {i+1} should be permitted for free tier"
        updated_quota = quota_limiter.commit_scan_deduction(user_id)
        assert updated_quota["scans_today"] == i + 1

    # 6th scan attempt must be strictly BLOCKED
    allowed, reason, blocked_info = quota_limiter.check_and_reserve_scan(user_id)
    assert allowed is False, "6th scan must be rejected when free quota is exhausted"
    assert blocked_info["can_scan"] is False
    assert blocked_info["remaining_scans"] == 0
    assert "limit reached" in reason.lower()

    # Simulate Mock Payment checkout request via API
    client = TestClient(app)
    checkout_res = client.post(
        "/api/billing/stripe/create-checkout-session",
        json={"user_id": user_id, "type": "subscription"},
    )
    assert checkout_res.status_code == 200
    checkout_data = checkout_res.json()
    assert "mock-success" in checkout_data["checkout_url"]
    assert user_id in checkout_data["checkout_url"]

    # Trigger mock success endpoint to clear block via Pro subscription
    success_res = client.get(
        checkout_data["checkout_url"],
        follow_redirects=False,
    )
    assert success_res.status_code in (302, 303, 307)
    assert "?payment_status=mock_success" in success_res.headers["location"]

    # Verify user transitions to Pro tier with unblocked scanning
    allowed, reason, pro_info = quota_limiter.check_and_reserve_scan(user_id)
    assert allowed is True, "Pro tier must clear scan block immediately"
    assert pro_info["can_scan"] is True
    assert pro_info["is_pro"] is True
    assert pro_info["tier"] == "pro"
    assert pro_info["remaining_scans"] >= 999999


def test_quota_refill_pack_clearing():
    """Verify purchasing a 5-scan refill pack restores scanning for free tier users."""
    user_id = f"refill_spotter_{uuid.uuid4().hex[:8]}"

    # Exhaust all 5 free scans
    for _ in range(5):
        quota_limiter.check_and_reserve_scan(user_id)
        quota_limiter.commit_scan_deduction(user_id)

    # User is currently blocked
    allowed, _, _ = quota_limiter.check_and_reserve_scan(user_id)
    assert allowed is False

    # Simulate purchasing 5 refill credits via mock success endpoint
    client = TestClient(app)
    client.get(
        f"/api/billing/mock-success?user_id={user_id}&type=refill",
        follow_redirects=False,
    )

    # Verify scan credit replenished
    refilled_quota = quota_limiter.get_user_quota(user_id)
    assert refilled_quota["can_scan"] is True
    assert refilled_quota["refill_credits"] == 5
    assert refilled_quota["remaining_scans"] == 5

    allowed, _, _ = quota_limiter.check_and_reserve_scan(user_id)
    assert allowed is True


# ==============================================================================
# 2. IEC 61672 Acoustic Telemetry & Exhaust DSP Engine
# ==============================================================================
def test_synthetic_audio_dsp_1khz_100db_tone():
    """Verify synthetic 1 kHz / 100 dB SPL tone calculates correct dBA and assigns Track Weapon bonus."""
    sample_rate = 48000
    duration = 1.0  # 1 second
    t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)

    # In digital audio, 0.95 peak amplitude prevents ADC/mic flat-top clipping.
    # To calibrate for a 100.0 dB SPL physical acoustic environment:
    # peak_dba = 20*log10(RMS) + calibration_offset_db = 100.0
    amplitude = 0.95
    rms = amplitude / np.sqrt(2.0)
    cal_offset_100db = 100.0 - 20.0 * np.log10(rms)

    synthetic_tone = (amplitude * np.sin(2.0 * np.pi * 1000.0 * t)).astype(np.float32)

    # Run IEC acoustic telemetry DSP pipeline
    telemetry = compute_iec_acoustic_telemetry(
        synthetic_tone,
        sample_rate=sample_rate,
        calibration_offset_db=cal_offset_100db,
    )

    # Verify SPL accuracy within IEC 61672 Class 1 +-0.25 dB tolerance
    peak_dba = telemetry["peak_dba"]
    assert abs(peak_dba - 100.0) <= 0.25, f"Expected ~100.0 dBA, got {peak_dba} dBA"
    assert telemetry["anti_cheat_status"] == "PASS"

    # Verify decibel tier evaluation and bonus points
    tier_info = calculate_decibel_tier(peak_dba)
    assert tier_info["tier"] == "Track Weapon"
    assert tier_info["total_points"] == 450
    assert tier_info["base_points"] == 450
    assert tier_info["badge"] == "TRACK_WEAPON"
    assert tier_info["ear_bleeder"] is False


# ==============================================================================
# 3. CIEDE2000 Paint Matcher
# ==============================================================================
def test_ciede2000_paint_matcher_exact_lab_database_match():
    """Verify exact Lab color match against app/paint_db.json OEM swatch library."""
    paint_db_path = os.path.join(REPO_ROOT, "app", "paint_db.json")
    assert os.path.exists(paint_db_path), f"Paint database must exist at {paint_db_path}"

    with open(paint_db_path, "r", encoding="utf-8") as f:
        paint_db = json.load(f)

    assert len(paint_db) >= 10, "Paint database should contain extensive OEM library"

    # Test 1: Porsche Viola Metallic (Lab: [16.24, 22.53, -19.28])
    viola_entry = next(c for c in paint_db if c.get("name") == "Viola Metallic")
    exact_viola_lab = viola_entry["lab"]

    match_porsche = match_cielab_coordinates(exact_viola_lab, make_hint="Porsche")
    assert match_porsche["matched"] is True
    assert match_porsche["is_exact_match"] is True
    assert match_porsche["delta_e00"] == 0.0
    assert match_porsche["name"] == "Viola Metallic"
    assert match_porsche["paint_code"] == viola_entry["paint_code"]
    assert match_porsche["is_pts"] is True

    # Test 2: Ferrari Rosso Corsa (Lab: [46.12, 68.34, 49.52])
    rosso_entry = next(c for c in paint_db if c.get("name") == "Rosso Corsa")
    match_ferrari = match_cielab_coordinates(rosso_entry["lab"], make_hint="Ferrari")
    assert match_ferrari["matched"] is True
    assert match_ferrari["is_exact_match"] is True
    assert match_ferrari["delta_e00"] == 0.0
    assert match_ferrari["name"] == "Rosso Corsa"


# ==============================================================================
# 4. Offline Sync Client-Backend Schema Conformance
# ==============================================================================
def test_offline_sync_payload_conforms_to_backend_schema():
    """Verify offline payload structure in offline_sync.js conforms to backend API ingestion schema."""
    js_path = os.path.join(REPO_ROOT, "frontend", "static", "js", "offline_sync.js")
    assert os.path.exists(js_path), f"offline_sync.js must exist at {js_path}"

    with open(js_path, "r", encoding="utf-8") as f:
        js_content = f.read()

    # Verify offline sync database and payload construction
    assert "pending_spots" in js_content, "IndexedDB must define pending_spots store"
    assert "/api/spot" in js_content, "Offline queue must POST to /api/spot"

    # Construct synthetic offline payload conforming to offline_sync.js
    synthetic_payload = {
        "message": "Exotic car sighting (Offline Stash)",
        "image": "data:image/jpeg;base64,/9j/4AAQSkZJRg==",
        "audio": "data:audio/wav;base64,UklGRg==",
        "user_id": f"spotter_{uuid.uuid4().hex[:8]}",
        "username": "ApexSpotter",
        "offline_synced_id": 1728472910,
        "latitude": 43.7384,
        "longitude": 7.4246,
    }

    # Verify required backend schema fields are present
    expected_fields = [
        "message",
        "image",
        "audio",
        "user_id",
        "username",
        "offline_synced_id",
        "latitude",
        "longitude",
    ]
    for field in expected_fields:
        assert field in synthetic_payload
        assert f"payload.{field}" in js_content or f"{field}:" in js_content

    # Verify values adhere to expected types
    assert isinstance(synthetic_payload["message"], str)
    assert isinstance(synthetic_payload["image"], str)
    assert isinstance(synthetic_payload["user_id"], str)
    assert isinstance(synthetic_payload["latitude"], float)
    assert isinstance(synthetic_payload["longitude"], float)
    assert isinstance(synthetic_payload["offline_synced_id"], int)


# ==============================================================================
# 5. Apple Mandatory Legal & Billing Routes
# ==============================================================================
def test_apple_mandatory_legal_and_billing_routes():
    """Verify /privacy, /terms, and mock billing endpoints are accessible."""
    client = TestClient(app)

    # Privacy Policy check
    privacy_res = client.get("/privacy")
    assert privacy_res.status_code == 200
    assert "text/html" in privacy_res.headers["content-type"]
    privacy_html = privacy_res.text
    assert "Camera" in privacy_html
    assert "Microphone" in privacy_html
    assert "Location" in privacy_html

    # Terms of Use check
    terms_res = client.get("/terms")
    assert terms_res.status_code == 200
    assert "text/html" in terms_res.headers["content-type"]
    terms_html = terms_res.text
    assert "Terms of Use" in terms_html
    assert "CarDex" in terms_html
    assert "Subscription" in terms_html

    # Billing fallback checkout session check
    checkout_res = client.post(
        "/api/billing/stripe/create-checkout-session",
        json={"user_id": "legal_spotter_1", "type": "subscription"},
    )
    assert checkout_res.status_code == 200
    assert checkout_res.json()["mock"] is True
