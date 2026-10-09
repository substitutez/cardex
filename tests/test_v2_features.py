"""End-to-End Test Suite for CarDex v2 Features.

Covers:
1. Daily Quota Limiter & Enforcement (5/5 scans, 6th scan rejection, refill bypass)
2. Dynamic Rarity & Deflationary Scoring Engine (production_units, velocity decay, 1-of-1 scarcity)
3. CIEDE2000 Spectrometric Color Matching (exact LAB match ΔE=0.0, distant color separation)
4. Anti-Cheat & Forensic Integrity (authentic camera pass, moiré screen-spoof detection)
"""

import os
import uuid
import pytest

from app.quota_limiter import (
    DEFAULT_DAILY_LIMIT,
    check_and_reserve_scan,
    commit_scan_deduction,
    add_refill_credits,
    get_user_quota,
    set_user_tier,
)
from app.scoring_engine import (
    compute_dynamic_spot_score,
    lookup_production_run,
    calculate_base_points,
    calculate_encounter_decay,
    get_paint_multiplier,
)
from app.paint_matcher import (
    ciede2000,
    match_cielab_coordinates,
    rgb_to_cielab,
)
from app.anti_cheat import (
    verify_image_integrity,
    detect_screen_moire_pattern,
)
from app.vehicle_db import get_vehicle_production_units


# --- 1. Daily Quota Tests ---

def test_daily_quota_initial_scans_allowed():
    """Verify that a new user starts with full 5-scan quota allowance."""
    user_id = f"test_quota_init_{uuid.uuid4().hex[:8]}"
    allowed, reason, quota = check_and_reserve_scan(user_id)
    assert allowed is True
    assert quota["daily_limit"] == DEFAULT_DAILY_LIMIT
    assert quota["scans_today"] == 0
    assert quota["remaining_free"] == DEFAULT_DAILY_LIMIT


def test_daily_quota_exhaustion_rejects_sixth_scan():
    """Verify that exhausting 5 scans causes the 6th scan to be rejected with quota limit reason."""
    user_id = f"test_quota_exhaust_{uuid.uuid4().hex[:8]}"

    # Perform 5 valid scans
    for i in range(5):
        allowed, reason, quota = check_and_reserve_scan(user_id)
        assert allowed is True, f"Scan {i+1} should be permitted"
        commit_scan_deduction(user_id)

    # 6th scan must be rejected
    allowed, reason, quota = check_and_reserve_scan(user_id)
    assert allowed is False
    assert "Daily scan limit reached" in reason or "quota" in reason.lower()
    assert quota["scans_today"] == 5
    assert quota["remaining_free"] == 0


def test_daily_quota_refill_credits_bypass_limit():
    """Verify that purchasing or adding refill credits permits scans even after daily quota is exhausted."""
    user_id = f"test_quota_refill_{uuid.uuid4().hex[:8]}"

    # Exhaust all 5 daily free scans
    for _ in range(5):
        check_and_reserve_scan(user_id)
        commit_scan_deduction(user_id)

    # Verify exhausted
    allowed, _, _ = check_and_reserve_scan(user_id)
    assert allowed is False

    # Add 1 refill pack (+5 scans)
    add_refill_credits(user_id, count=5)

    # Now scan must be allowed using refill credit
    allowed, reason, quota = check_and_reserve_scan(user_id)
    assert allowed is True
    assert quota["refill_credits"] >= 5

    # Commit deduction and verify refill credit decremented
    updated_quota = commit_scan_deduction(user_id)
    assert updated_quota["refill_credits"] == 4


def test_pro_tier_unlimited_quota():
    """Verify that Pro tier members have infinite scan allowance."""
    user_id = f"test_pro_user_{uuid.uuid4().hex[:8]}"
    set_user_tier(user_id, "pro")

    for _ in range(7):
        allowed, _, quota = check_and_reserve_scan(user_id)
        assert allowed is True
        assert quota["tier"] == "pro"
        assert quota["is_pro"] is True
        commit_scan_deduction(user_id)


# --- 2. Dynamic Scoring Engine Tests ---

def test_dynamic_scoring_incorporates_production_units():
    """Verify that dynamic scoring queries production_units from SQLite database."""
    # Carrera GT has 1,270 units in SQLite
    cgt_units = get_vehicle_production_units("Porsche", "Carrera GT")
    assert cgt_units == 1270

    score = compute_dynamic_spot_score("Porsche", "Carrera GT")
    assert score["production_run"] == 1270
    assert score["base_points"] > 2000
    assert score["final_points"] > 0


def test_dynamic_scoring_scarcity_bonus_for_1of1():
    """Verify that 1-of-1 and ultra-low production hypercars receive maximum scarcity bonus."""
    score_profilee = compute_dynamic_spot_score("Bugatti", "Chiron Profilée")
    score_f40 = compute_dynamic_spot_score("Ferrari", "F40")
    score_mass = compute_dynamic_spot_score("Toyota", "Camry")

    # 1-of-1 hypercar awards 50,000 base points
    assert score_profilee["production_run"] == 1
    assert score_profilee["base_points"] == 50000
    assert score_profilee["rarity_tier"] == "Mythic 1-of-1"

    # Hierarchy: 1-of-1 > F40 (1315 units) > Mass Market
    assert score_profilee["base_points"] > score_f40["base_points"] > score_mass["base_points"]


def test_dynamic_scoring_velocity_decay():
    """Verify that encounter velocity decay factor (D_30) reduces points as sightings increase."""
    decay_0 = calculate_encounter_decay(0)
    decay_5 = calculate_encounter_decay(5)
    decay_20 = calculate_encounter_decay(20)
    decay_100 = calculate_encounter_decay(100)

    assert decay_0 == 1.0
    assert decay_5 < decay_0
    assert decay_20 < decay_5
    # Asymptotic floor at 0.35
    assert decay_100 == 0.35


# --- 3. CIEDE2000 Spectrometric Color Matching Tests ---

def test_ciede2000_exact_lab_match():
    """Verify that exact CIE L*a*b* coordinates yield ΔE = 0.0 and correct OEM paint code."""
    # Porsche Viola Metallic (Code 3AE): L=16.24, a=22.53, b=-19.28
    match = match_cielab_coordinates((16.24, 22.53, -19.28), make_hint="Porsche")

    assert match["matched"] is True
    assert match["delta_e00"] == 0.0
    assert match["paint_code"] == "3AE"
    assert match["paint_name"] == "Viola Metallic"
    assert match["multiplier"] == 1.35
    assert match["program"] == "Paint to Sample (PTS)"


def test_ciede2000_distant_colors_high_delta_e():
    """Verify that distant colors (e.g. bright yellow vs deep purple) yield high ΔE."""
    # Bright Yellow: L=80.0, a=-10.0, b=85.0
    # Porsche Viola: L=16.24, a=22.53, b=-19.28
    yellow_lab = (80.0, -10.0, 85.0)
    purple_lab = (16.24, 22.53, -19.28)

    de = ciede2000(yellow_lab, purple_lab)
    assert de > 80.0, f"Expected large ΔE between yellow and deep purple, got {de}"


def test_ciede2000_pts_multiplier_assignment():
    """Verify paint finish multipliers: Standard (1.0x), PTS (1.35x), Carbon (1.75x)."""
    assert get_paint_multiplier(is_pts=False, is_bespoke_or_carbon=False) == 1.0
    assert get_paint_multiplier(is_pts=True, is_bespoke_or_carbon=False) == 1.35
    assert get_paint_multiplier(is_pts=False, is_bespoke_or_carbon=True) == 1.75


# --- 4. Anti-Cheat & Forensic Verification Tests ---

def test_anti_cheat_authentic_spot_passes():
    """Verify that an authentic real-world spot photograph passes FFT screen-capture detection."""
    fixture_path = "tests/fixtures/real_spot.jpg"
    assert os.path.exists(fixture_path), f"Missing test fixture: {fixture_path}"

    with open(fixture_path, "rb") as f:
        img_bytes = f.read()

    moire_res = detect_screen_moire_pattern(img_bytes)
    assert moire_res["is_screen_capture"] is False

    integrity = verify_image_integrity(img_bytes)
    assert integrity["passed"] is True
    assert integrity["flag"] == "CLEAN"


def test_anti_cheat_screen_capture_flagged():
    """Verify that a photograph of a computer/phone screen displaying Moiré raster grid is rejected."""
    fixture_path = "tests/fixtures/screen_spoof.jpg"
    assert os.path.exists(fixture_path), f"Missing test fixture: {fixture_path}"

    with open(fixture_path, "rb") as f:
        img_bytes = f.read()

    moire_res = detect_screen_moire_pattern(img_bytes)
    assert moire_res["is_screen_capture"] is True
    assert moire_res["peak_ratio"] > 3.2

    integrity = verify_image_integrity(img_bytes)
    assert integrity["passed"] is False
    assert integrity["flag"] == "SCREEN_SPOOFING_DETECTED"
    assert "Moiré frequency grid detected" in integrity["message"]
