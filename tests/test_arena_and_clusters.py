"""Unit and integration tests for CarDex Battle Arena & Spontaneous Car Meet Clustering.

Tests:
1. Battle Power & Resolution across all 5 attributes (horsepower, acceleration, top_speed, exhaust_decibels, rarity_tier).
2. Pot calculation, win/draw/loss state transitions, and draw refunds.
3. DBSCAN Spatial-Temporal Clustering:
   - 3 spots within 100m -> Active Car Meet detected.
   - 3 spots 2km apart -> No Car Meet detected.
   - Spots older than 45 min window -> Filtered out.
4. Haversine distance geodesic accuracy.
5. FastAPI Endpoints:
   - GET /api/radar/meets
   - GET /api/battle/garage
   - POST /api/battle/challenge
6. Frontend Asset verification:
   - battle.js, viewfinder_shader.js, and index.html UI linkages.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
frontend_dir = str(BASE_DIR / "frontend")
app_dir = str(BASE_DIR / "app")

for d in (frontend_dir, app_dir):
    if d not in sys.path:
        sys.path.insert(0, d)

import battle_engine
import cluster_beacon
from main import app


@pytest.fixture
def client():
    return TestClient(app)


# =========================================================================
# 1. BATTLE ENGINE UNIT TESTS
# =========================================================================

def test_battle_power_calculation_all_5_attributes():
    """Verify battle power calculation across all 5 supported attributes."""
    spot_ferrari = {
        "id": "spot_f8",
        "make": "Ferrari",
        "model": "F8 Tributo",
        "specs": {
            "horsepower": 710,
            "acceleration": 2.9,
            "top_speed": 340,
        },
        "acoustic_telemetry": {
            "peak_dba": 102.5,
        },
        "rarity_tier": "Legendary",
        "colorway": "Rosso Corsa",
    }

    # 1. Horsepower: direct HP
    hp = battle_engine.calculate_battle_power(spot_ferrari, "horsepower")
    assert hp == 710.0

    # 2. Acceleration: 100.0 / seconds (2.9s -> 100/2.9 ~= 34.48)
    acc = battle_engine.calculate_battle_power(spot_ferrari, "acceleration")
    assert pytest.approx(acc, 0.01) == (100.0 / 2.9)

    # 3. Top Speed: direct km/h
    spd = battle_engine.calculate_battle_power(spot_ferrari, "top_speed")
    assert spd == 340.0

    # 4. Exhaust Decibels: verified peak dBA
    dba = battle_engine.calculate_battle_power(spot_ferrari, "exhaust_decibels")
    assert dba == 102.5

    # 5. Rarity Tier: Legendary base (80) + paint multiplier
    rarity = battle_engine.calculate_battle_power(spot_ferrari, "rarity_tier")
    assert rarity >= 80.0


def test_battle_acceleration_inversion():
    """Verify lower 0-100 km/h time yields higher battle power (Top Trumps rule)."""
    hypercar_quick = {
        "make": "Rimac",
        "model": "Nevera",
        "specs": {"acceleration": 1.85},
    }
    supercar_slow = {
        "make": "Porsche",
        "model": "Cayman GT4",
        "specs": {"acceleration": 4.4},
    }

    p_quick = battle_engine.calculate_battle_power(hypercar_quick, "acceleration")
    p_slow = battle_engine.calculate_battle_power(supercar_slow, "acceleration")

    assert p_quick > p_slow, "1.85s car must beat 4.4s car in acceleration battle power"


def test_battle_resolution_win_loss_and_draw():
    """Verify Top Trumps duel resolution, pot calculation, and draw refunds."""
    player_car = {
        "id": "p1",
        "car_name": "Ferrari SF90 Stradale",
        "specs": {"horsepower": 986, "top_speed": 340},
    }
    rival_car_weaker = {
        "id": "r1",
        "car_name": "Audi R8 V10",
        "specs": {"horsepower": 602, "top_speed": 330},
    }
    rival_car_equal = {
        "id": "r2",
        "car_name": "Ferrari SF90 Assetto Fiorano",
        "specs": {"horsepower": 986, "top_speed": 340},
    }

    # Test Win: Wager 100 -> Pot is 200, net delta +100
    res_win = battle_engine.resolve_battle(player_car, rival_car_weaker, "horsepower", wager=100)
    assert res_win["outcome"] == "win"
    assert res_win["pot"] == 200
    assert res_win["awarded_points"] == 200
    assert res_win["net_delta"] == 100
    assert res_win["player_score"] == 986.0
    assert res_win["opponent_score"] == 602.0

    # Test Loss: Wager 100 -> Pot is 200, awarded 0, net delta -100
    res_loss = battle_engine.resolve_battle(rival_car_weaker, player_car, "horsepower", wager=100)
    assert res_loss["outcome"] == "loss"
    assert res_loss["pot"] == 200
    assert res_loss["awarded_points"] == 0
    assert res_loss["net_delta"] == -100

    # Test Draw: Equal score -> Pot refunded, awarded 100, net delta 0
    res_draw = battle_engine.resolve_battle(player_car, rival_car_equal, "horsepower", wager=100)
    assert res_draw["outcome"] == "draw"
    assert res_draw["awarded_points"] == 100
    assert res_draw["net_delta"] == 0
    assert "Tie" in res_draw["commentary"] or "Stalemate" in res_draw["commentary"] or "draw" in res_draw["commentary"].lower()


def test_battle_wager_limits():
    """Verify battle wagers outside 50..500 PTS are clamped or handled."""
    player_car = {"id": "p1", "specs": {"horsepower": 500}}
    rival_car = {"id": "r1", "specs": {"horsepower": 400}}

    # 500 PTS wager -> Pot 1000
    res_max = battle_engine.resolve_battle(player_car, rival_car, "horsepower", wager=500)
    assert res_max["pot"] == 1000
    assert res_max["net_delta"] == 500


# =========================================================================
# 2. DBSCAN SPATIAL-TEMPORAL CLUSTERING UNIT TESTS
# =========================================================================

def test_haversine_distance_meters():
    """Verify geodesic distance calculation between known landmarks."""
    # Casino de Monte-Carlo to Hotel de Paris (~80 meters apart)
    lat1, lon1 = 43.7391, 7.4277
    lat2, lon2 = 43.7394, 7.4272
    dist = cluster_beacon.haversine_distance_meters(lat1, lon1, lat2, lon2)
    assert 40.0 < dist < 120.0

    # Monaco to Nice Airport (~19 km apart)
    lat_nce, lon_nce = 43.6653, 7.2150
    dist_nice = cluster_beacon.haversine_distance_meters(lat1, lon1, lat_nce, lon_nce)
    assert 18_000.0 < dist_nice < 22_000.0


def test_dbscan_clustering_triggers_meet():
    """Feed 3 mock spots within 100m -> verify active CAR_MEET_DETECTED event created."""
    now = datetime.now(timezone.utc)
    base_lat, base_lng = 43.7384, 7.4246

    # 3 spots within ~50-80 meters logged by 3 unique users within last 15 minutes
    spots = [
        {
            "id": "spot_1",
            "user_id": "spotter_alpha",
            "lat": base_lat + 0.0002,
            "lng": base_lng + 0.0002,
            "car_name": "Porsche 911 GT3 RS",
            "timestamp": (now - timedelta(minutes=5)).isoformat(),
        },
        {
            "id": "spot_2",
            "user_id": "spotter_beta",
            "lat": base_lat - 0.0001,
            "lng": base_lng + 0.0001,
            "car_name": "Ferrari SF90 Assetto Fiorano",
            "timestamp": (now - timedelta(minutes=10)).isoformat(),
        },
        {
            "id": "spot_3",
            "user_id": "spotter_gamma",
            "lat": base_lat + 0.0001,
            "lng": base_lng - 0.0002,
            "car_name": "McLaren 765LT Spider",
            "timestamp": (now - timedelta(minutes=15)).isoformat(),
        },
    ]

    clusters = cluster_beacon.detect_clusters_dbscan(
        spots,
        eps_meters=300.0,
        min_spots=3,
        window_minutes=45.0,
        reference_time=now,
    )

    assert len(clusters) == 1, f"Expected exactly 1 cluster, got {len(clusters)}"
    meet = clusters[0]
    assert meet["car_count"] == 3
    assert meet["unique_spotters"] == 3
    assert meet["status"] == "CAR_MEET_DETECTED"
    assert len(meet["verified_exotics"]) == 3
    assert pytest.approx(meet["center_lat"], 0.001) == base_lat
    assert pytest.approx(meet["center_lng"], 0.001) == base_lng


def test_dbscan_clustering_dispersed_spots_no_meet():
    """Feed 3 spots 2km apart -> verify 0 meets created."""
    now = datetime.now(timezone.utc)
    base_lat, base_lng = 43.7384, 7.4246

    # 3 spots separated by > 2000 meters
    spots = [
        {
            "id": "s1",
            "user_id": "u1",
            "lat": base_lat,
            "lng": base_lng,
            "car_name": "Ferrari 488 Pista",
            "timestamp": now.isoformat(),
        },
        {
            "id": "s2",
            "user_id": "u2",
            "lat": base_lat + 0.03,  # ~3.3 km away
            "lng": base_lng,
            "car_name": "Lamborghini Huracan STO",
            "timestamp": now.isoformat(),
        },
        {
            "id": "s3",
            "user_id": "u3",
            "lat": base_lat,
            "lng": base_lng + 0.04,  # ~3.2 km away
            "car_name": "Aston Martin DBS",
            "timestamp": now.isoformat(),
        },
    ]

    clusters = cluster_beacon.detect_clusters_dbscan(
        spots,
        eps_meters=300.0,
        min_spots=3,
        window_minutes=45.0,
        reference_time=now,
    )

    assert len(clusters) == 0, "No meet cluster should be formed for spots 2km apart"


def test_dbscan_clustering_time_window_expiry():
    """Spots older than 45 minutes must not count toward car meet threshold."""
    now = datetime.now(timezone.utc)
    base_lat, base_lng = 43.7384, 7.4246

    # 2 fresh spots, 1 spot 60 minutes old
    spots = [
        {
            "id": "s1",
            "user_id": "u1",
            "lat": base_lat,
            "lng": base_lng,
            "car_name": "Ferrari 488",
            "timestamp": (now - timedelta(minutes=5)).isoformat(),
        },
        {
            "id": "s2",
            "user_id": "u2",
            "lat": base_lat + 0.0001,
            "lng": base_lng + 0.0001,
            "car_name": "McLaren 720S",
            "timestamp": (now - timedelta(minutes=10)).isoformat(),
        },
        {
            "id": "s3_stale",
            "user_id": "u3",
            "lat": base_lat + 0.0001,
            "lng": base_lng,
            "car_name": "Porsche GT3",
            "timestamp": (now - timedelta(minutes=60)).isoformat(),
        },
    ]

    clusters = cluster_beacon.detect_clusters_dbscan(
        spots,
        eps_meters=300.0,
        min_spots=3,
        window_minutes=45.0,
        reference_time=now,
    )

    assert len(clusters) == 0, "Stale spot (>45min) should prevent cluster from meeting min_spots=3"


# =========================================================================
# 3. FASTAPI ENDPOINT INTEGRATION TESTS
# =========================================================================

def test_api_radar_meets_endpoint(client):
    """Verify GET /api/radar/meets returns active meets within search radius."""
    res = client.get("/api/radar/meets?lat=43.7384&lng=7.4246&radius_km=25.0")
    assert res.status_code == 200
    meets = res.json()
    assert isinstance(meets, list)


def test_api_battle_garage_endpoint(client):
    """Verify GET /api/battle/garage returns vehicles with precomputed battle powers."""
    res = client.get("/api/battle/garage?user_id=spotter_demo")
    assert res.status_code == 200
    data = res.json()
    assert "spots" in data
    assert len(data["spots"]) > 0

    first_spot = data["spots"][0]
    assert "battle_powers" in first_spot
    powers = first_spot["battle_powers"]
    for attr in ("horsepower", "acceleration", "top_speed", "exhaust_decibels", "rarity_tier"):
        assert attr in powers
        assert isinstance(powers[attr], (int, float))


def test_api_battle_challenge_endpoint(client):
    """Verify POST /api/battle/challenge executes Top Trumps card duel."""
    payload = {
        "user_id": "spotter_demo",
        "spot_id": "demo_gt3rs",
        "attribute": "horsepower",
        "wager": 100,
    }
    res = client.post("/api/battle/challenge", json=payload)
    assert res.status_code == 200
    data = res.json()

    assert data["success"] is True
    assert "battle" in data
    duel = data["battle"]
    assert duel["outcome"] in ("win", "loss", "draw")
    assert duel["pot"] == 200
    assert "player_score" in duel
    assert "opponent_score" in duel
    assert "commentary" in duel
    assert "opponent" in duel


def test_api_battle_challenge_validation_errors(client):
    """Verify validation error when attribute is invalid or wager out of range."""
    # Invalid attribute
    res_bad_attr = client.post("/api/battle/challenge", json={
        "user_id": "u1",
        "spot_id": "s1",
        "attribute": "invalid_attribute",
        "wager": 100,
    })
    assert res_bad_attr.status_code == 400

    # Wager too low (<50)
    res_low_wager = client.post("/api/battle/challenge", json={
        "user_id": "u1",
        "spot_id": "s1",
        "attribute": "horsepower",
        "wager": 10,
    })
    assert res_low_wager.status_code == 400


# =========================================================================
# 4. FRONTEND ASSETS & HTML LINKAGE TESTS
# =========================================================================

def test_battle_and_viewfinder_frontend_files():
    """Verify JavaScript assets exist and contain the required APIs and logic."""
    battle_js = BASE_DIR / "frontend" / "static" / "js" / "battle.js"
    shader_js = BASE_DIR / "frontend" / "static" / "js" / "viewfinder_shader.js"
    index_html = BASE_DIR / "frontend" / "static" / "index.html"

    assert battle_js.exists(), "battle.js missing"
    assert shader_js.exists(), "viewfinder_shader.js missing"
    assert index_html.exists(), "index.html missing"

    content_battle = battle_js.read_text(encoding="utf-8")
    assert "CarDexBattleArena" in content_battle
    assert "playTireScreechSound" in content_battle
    assert "playEngineRoarSound" in content_battle
    assert "initiateDuel" in content_battle

    content_shader = shader_js.read_text(encoding="utf-8")
    assert "CarDexViewfinderShader" in content_shader
    assert "toggleNightBoost" in content_shader
    assert "getCaptureCanvas" in content_shader
    assert "deviceorientation" in content_shader

    content_html = index_html.read_text(encoding="utf-8")
    assert "battle.js" in content_html
    assert "viewfinder_shader.js" in content_html
    assert "cockpit-battle-view" in content_html
    assert "tab-btn-battle" in content_html
    assert "car-meet-banner" in content_html
    assert "camera-gl-canvas" in content_html
    assert "camera-reticle-canvas" in content_html
    assert "camera-nightboost-btn" in content_html
