"""Unit test suite for CarDex Competitive & Production Modules.

Covers:
1. Daily Quota Limiter (5 scans/day, rolling 24h reset, refills, pro tier)
2. Dynamic Rarity & Deflationary Scoring Engine (N_prod, D_30, M_paint, B_first)
3. Anti-Screen Spoofing & Duplicate Defense (2D FFT Moiré & 64-bit DCT pHash)
4. Spectrometric Colorway & CIEDE2000 Paint Matcher
5. Community Dispute Adjudication Pipeline
"""

import io
import math
import uuid
import numpy as np
import pytest
from PIL import Image, ImageFilter

from app.quota_limiter import (
    DEFAULT_DAILY_LIMIT,
    get_user_quota,
    check_and_reserve_scan,
    commit_scan_deduction,
    add_refill_credits,
    set_user_tier,
)
from app.scoring_engine import (
    compute_dynamic_spot_score,
    lookup_production_run,
    calculate_base_points,
    calculate_encounter_decay,
    get_paint_multiplier,
)
from app.anti_cheat import (
    compute_perceptual_hash,
    compute_hamming_distance,
    detect_screen_moire_pattern,
    verify_image_integrity,
)
from app.paint_matcher import (
    ciede2000,
    rgb_to_cielab,
    match_oem_paint_color,
    extract_dominant_panel_color,
)
from app.firestore_db import (
    submit_dispute_review,
    get_pending_disputes,
    cast_dispute_vote,
)


# --- 1. Dynamic Scoring Engine Tests ---

def test_production_run_lookup():
    """Verify known exotic production runs and fallback tiers."""
    assert lookup_production_run("Bugatti", "Chiron Profilée") == 1
    assert lookup_production_run("Bugatti", "Chiron Profilee") == 1
    assert lookup_production_run("Pagani", "Zonda HP Barchetta") == 3
    assert lookup_production_run("Ferrari", "F40") == 1315
    assert lookup_production_run("Porsche", "911 GT3 RS") in (600, 4500)
    # Unknown exotic model fallback
    assert lookup_production_run("Generic", "Supercar") == 150000


def test_base_rarity_points_scaling():
    """Verify logarithmic base point scaling: 1-of-1 hypercar gets 50,000 PTS."""
    p_1of1 = calculate_base_points(1)
    p_f40 = calculate_base_points(1315)
    p_mass = calculate_base_points(50000)

    assert p_1of1 == 50000
    assert p_f40 > 2000
    assert p_mass <= 500
    assert p_1of1 > p_f40 > p_mass


def test_encounter_decay_formula():
    """Verify deflationary decay: D_30 decreases with encounter density."""
    d_zero = calculate_encounter_decay(0)
    d_five = calculate_encounter_decay(5)
    d_twenty = calculate_encounter_decay(20)
    d_hundred = calculate_encounter_decay(100)

    assert d_zero == 1.0
    assert d_five < d_zero
    assert d_twenty < d_five
    # Minimum floor at 0.35
    assert d_hundred == 0.35


def test_paint_finish_multiplier():
    """Verify paint finish multipliers (1.0x standard, 1.35x PTS, 1.75x exposed carbon)."""
    assert get_paint_multiplier(is_pts=False, is_bespoke_or_carbon=False) == 1.0
    assert get_paint_multiplier(is_pts=True, is_bespoke_or_carbon=False) == 1.35
    assert get_paint_multiplier(is_pts=False, is_bespoke_or_carbon=True) == 1.75


def test_full_dynamic_score_computation():
    """Verify complete dynamic score breakdown dict generation."""
    score = compute_dynamic_spot_score(
        make="Porsche",
        model="911 GT3 RS",
        trim="Weissach Package",
        is_pts=True,
        is_bespoke_or_carbon=False,
        location="Monaco",
    )
    assert "final_points" in score
    assert "breakdown" in score
    assert score["final_points"] > 0
    assert score["breakdown"]["paint_multiplier"] == 1.35


# --- 2. Anti-Cheat & Image Integrity Tests ---

def test_perceptual_hash_exact_and_similar():
    """Verify 64-bit DCT perceptual hashing and Hamming distance."""
    img1 = Image.new("RGB", (128, 128), color=(255, 100, 50))
    buf1 = io.BytesIO()
    img1.save(buf1, format="JPEG")
    b1 = buf1.getvalue()

    h1 = compute_perceptual_hash(b1)
    assert len(h1) == 16  # 64-bit hash encoded in 16 hex characters

    assert compute_hamming_distance(h1, h1) == 0

    img2 = Image.new("RGB", (128, 128), color=(254, 101, 51))
    buf2 = io.BytesIO()
    img2.save(buf2, format="JPEG")
    b2 = buf2.getvalue()
    h2 = compute_perceptual_hash(b2)
    assert compute_hamming_distance(h1, h2) <= 4


def test_moire_pattern_detection_natural_image():
    """Verify natural photography passes 2D FFT Moiré frequency test."""
    rng = np.random.default_rng(42)
    arr = rng.integers(100, 160, size=(512, 512, 3), dtype=np.uint8)
    img = Image.fromarray(arr).filter(ImageFilter.GaussianBlur(radius=3))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    res = detect_screen_moire_pattern(buf.getvalue())
    assert res["is_screen_capture"] is False


def test_moire_pattern_detection_synthetic_screen_grid():
    """Verify periodic LCD subpixel grid triggers Moiré detection."""
    arr = np.zeros((512, 512, 3), dtype=np.uint8)
    for y in range(512):
        for x in range(512):
            val = 255 if ((x % 4 == 0) and (y % 4 == 0)) else 0
            arr[y, x] = [val, val, val]
    img = Image.fromarray(arr)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    res = detect_screen_moire_pattern(buf.getvalue())
    assert res["is_screen_capture"] is True
    assert res["peak_ratio"] > 4.2


# --- 3. Spectrometric Colorway & CIEDE2000 Paint Matcher Tests ---

def test_ciede2000_identical_colors():
    """Identical Lab coordinates must return Delta E = 0.0."""
    lab = (50.0, 20.0, -10.0)
    assert ciede2000(lab, lab) == 0.0


def test_ciede2000_known_benchmark_distance():
    """Verify CIEDE2000 formula against standard color science benchmark."""
    lab1 = (50.0, 2.6772, -79.7751)
    lab2 = (50.0, 0.0, -82.7485)
    delta_e = ciede2000(lab1, lab2)
    assert 1.9 <= delta_e <= 2.2


def test_rgb_to_lab_d65():
    """Verify D65 Illuminant RGB to CIE L*a*b* conversion."""
    l, a, b = rgb_to_cielab(255, 0, 0)
    assert 50 < l < 60
    assert a > 70
    assert b > 50


def test_oem_paint_matching_porsche_viola():
    """Verify matching Porsche Viola Metallic from panel image bytes."""
    # Porsche Viola Metallic OEM RGB [58, 28, 68]
    img = Image.new("RGB", (200, 200), color=(58, 28, 68))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    match = match_oem_paint_color(buf.getvalue(), make_hint="Porsche")
    assert match is not None
    assert match["matched"] is True
    assert "Viola Metallic" in match["paint_name"]
    assert match["is_pts"] is True
    assert match["delta_e00"] < 3.5


def test_oem_paint_matching_ferrari_rosso():
    """Verify matching Ferrari Rosso Corsa from panel image bytes."""
    # Ferrari Rosso Corsa OEM RGB [212, 0, 0]
    img = Image.new("RGB", (200, 200), color=(212, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    match = match_oem_paint_color(buf.getvalue(), make_hint="Ferrari")
    assert match is not None
    assert match["matched"] is True
    assert "Rosso Corsa" in match["paint_name"]
    assert match["delta_e00"] < 3.5


# --- 4. Daily Quota Limiter Tests ---

def test_quota_initialization_and_deduction():
    """Verify rolling 24h quota: 5 free scans, reservation and atomic commit."""
    test_user = f"pytest_quota_{uuid.uuid4().hex[:8]}"
    quota = get_user_quota(test_user)
    assert quota["user_id"] == test_user
    assert quota["daily_limit"] == 5
    assert quota["can_scan"] is True

    allowed, _, _ = check_and_reserve_scan(test_user)
    assert allowed is True
    new_quota = commit_scan_deduction(test_user)
    assert new_quota["scans_today"] >= 1


def test_quota_refills_and_pro_tier():
    """Verify consumable scan refills and Pro tier unlimited bypass."""
    test_user = f"pytest_pro_{uuid.uuid4().hex[:8]}"
    set_user_tier(test_user, "pro")
    quota = get_user_quota(test_user)
    assert quota["tier"] == "pro"
    assert quota["can_scan"] is True
    assert quota["remaining_scans"] == 999999

    test_free_user = f"pytest_free_{uuid.uuid4().hex[:8]}"
    set_user_tier(test_free_user, "free")
    add_refill_credits(test_free_user, 10)
    refill_quota = get_user_quota(test_free_user)
    assert refill_quota["refill_credits"] >= 10
    assert refill_quota["remaining_scans"] >= 10


# --- 5. Community Dispute Adjudication Pipeline Tests ---

def test_dispute_submission_and_voting():
    """Verify end-to-end dispute submission, consensus voting, and bounty award."""
    review = submit_dispute_review(
        car_name="Porsche 911 GT3 RS",
        issue_description="Listed as standard GT3, but vehicle has Weissach swan-neck carbon wing",
        proposed_correction="Porsche 911 GT3 RS (Weissach Package)",
        user_id="pytest_dispute_submitter",
        proposed_make="Porsche",
        proposed_model="911 GT3 RS",
        proposed_trim="Weissach Package",
    )
    rev_id = review["review_id"]
    assert rev_id is not None
    assert review["status"] == "UNDER_REVIEW"

    r1_id = f"reviewer_{uuid.uuid4().hex[:6]}"
    r2_id = f"reviewer_{uuid.uuid4().hex[:6]}"
    r3_id = f"reviewer_{uuid.uuid4().hex[:6]}"

    v1 = cast_dispute_vote(rev_id, r1_id, True, "Master Spotter")
    assert v1["status"] in ("UNDER_REVIEW", "PENDING_CONSENSUS")

    v2 = cast_dispute_vote(rev_id, r2_id, True, "Master Spotter")
    assert v2["status"] in ("UNDER_REVIEW", "PENDING_CONSENSUS")

    v3 = cast_dispute_vote(rev_id, r3_id, True, "Master Spotter")
    assert v3["resolved"] is True
    assert v3["consensus_passed"] is True
    assert v3["status"] == "RESOLVED"
    assert v3["bounty_awarded"] == 500
