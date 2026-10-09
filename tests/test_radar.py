"""Tests for Location-Aware Sighting Radar system.

Validates:
1. Geohash encoding and decoding.
2. Coordinate privacy quantization (~500m bounding box, raw GPS never stored).
3. Haversine spherical distance calculations.
4. Firestore DB spot recording with quantized spatial fields.
5. Radar sighting queries with trailing 7-day cutoff.
6. GET /api/radar FastAPI endpoint with radius validation.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

frontend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))
if frontend_dir not in sys.path:
    sys.path.insert(0, frontend_dir)

import firestore_db
from main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_geohash_encoding_and_decoding():
    """Verify 6-character geohash correctly encodes and decodes coordinates."""
    # Monaco Casino coordinates
    lat, lng = 43.7391, 7.4277
    gh = firestore_db.encode_geohash(lat, lng, precision=6)
    assert isinstance(gh, str)
    assert len(gh) == 6
    assert gh == "spv2c4"

    # Decode and check error margin is within ~1km
    dec_lat, dec_lng = firestore_db.decode_geohash(gh)
    assert abs(dec_lat - lat) < 0.01
    assert abs(dec_lng - lng) < 0.01


def test_privacy_quantization_never_stores_raw_gps():
    """Verify spatial quantization fuzzes coordinates to ~500m and strips exact precision."""
    # Exact residential driveway coordinates
    raw_lat = 34.073612345
    raw_lng = -118.400356789

    gh, fuzzy_lat, fuzzy_lng = firestore_db.quantize_coordinates(raw_lat, raw_lng, precision=6)

    assert len(gh) == 6
    # Coordinates must be rounded and fuzzed (not matching raw precision)
    assert fuzzy_lat != raw_lat
    assert fuzzy_lng != raw_lng
    assert len(str(fuzzy_lat).split(".")[1]) <= 4
    assert len(str(fuzzy_lng).split(".")[1]) <= 4

    # Distance between raw GPS and fuzzy centroid should be under ~1km
    dist = firestore_db.haversine_distance_km(raw_lat, raw_lng, fuzzy_lat, fuzzy_lng)
    assert 0.0 < dist < 1.2


def test_haversine_distance_km():
    """Verify haversine spherical distance calculations."""
    # Monaco Port Hercule to Casino Square (~500m)
    d = firestore_db.haversine_distance_km(43.7352, 7.4231, 43.7391, 7.4277)
    assert 0.4 < d < 0.8

    # Same location should be 0.0km
    assert firestore_db.haversine_distance_km(43.7384, 7.4246, 43.7384, 7.4246) == 0.0


def test_record_car_spot_entry_with_geospatial_privacy():
    """Verify record_car_spot_entry stores quantized geohash & fuzzy coordinates."""
    mock_db = MagicMock()
    mock_doc = MagicMock()
    mock_doc.id = "test_radar_spot_id"
    mock_db.collection.return_value.add.return_value = (None, mock_doc)

    fake_car = {
        "id": "porsche_gt3rs",
        "make": "Porsche",
        "model": "911 GT3 RS",
        "trim": "Weissach",
        "points": 1500,
        "rarity_tier": "Legendary",
    }

    with patch("firestore_db.get_db", return_value=mock_db), \
         patch("firestore_db.get_car_by_id_or_name", return_value=fake_car):
        res = firestore_db.record_car_spot_entry(
            car_id="porsche_gt3rs",
            colorway="Pyro Red",
            user_id="spotter_test",
            location="Monaco Casino Square",
            notes="Weissach package wing spotted",
            latitude=43.7391823,
            longitude=7.4277123,
            points_override=1500,
            rarity_override="Legendary",
        )

        assert res["spot_id"] == "test_radar_spot_id"
        assert res["points_awarded"] == 1500

        # Inspect what document was written to Firestore
        call_args = mock_db.collection.return_value.add.call_args[0][0]
        assert "geohash" in call_args
        assert call_args["geohash"] == "spv2c4"
        assert "fuzzy_lat" in call_args
        assert "fuzzy_lng" in call_args
        assert "spot_timestamp" in call_args
        assert "rarity_tier" in call_args
        assert "make_model" in call_args

        # Crucial privacy assertion: raw high-precision GPS is NEVER in the dict keys!
        assert "latitude" not in call_args
        assert "longitude" not in call_args
        assert call_args["fuzzy_lat"] != 43.7391823


def test_query_radar_sightings_filters_trailing_7_days():
    """Verify query_radar_sightings returns nearby contacts within 7 days and excludes older ones."""
    now = datetime.now(timezone.utc)
    recent_ts = (now - timedelta(hours=3)).isoformat()
    old_ts = (now - timedelta(days=12)).isoformat()

    doc_recent = MagicMock()
    doc_recent.id = "recent_spot"
    doc_recent.to_dict.return_value = {
        "make_model": "Ferrari Daytona SP3",
        "rarity_tier": "Mythic 1-of-1",
        "fuzzy_lat": 43.7385,
        "fuzzy_lng": 7.4250,
        "geohash": "spv2bf",
        "spot_timestamp": recent_ts,
        "image_url": "https://storage.googleapis.com/test/sp3.jpg",
        "colorway": "Rosso Corsa",
        "user_id": "spotter_monaco",
        "points_awarded": 15000,
    }

    doc_old = MagicMock()
    doc_old.id = "old_spot"
    doc_old.to_dict.return_value = {
        "make_model": "Bugatti Chiron",
        "rarity_tier": "Legendary",
        "fuzzy_lat": 43.7385,
        "fuzzy_lng": 7.4250,
        "geohash": "spv2bf",
        "spot_timestamp": old_ts,
        "user_id": "spotter_old",
    }

    mock_db = MagicMock()
    mock_db.collection.return_value.stream.return_value = [doc_recent, doc_old]

    with patch("firestore_db.get_db", return_value=mock_db):
        sightings = firestore_db.query_radar_sightings(lat=43.7384, lng=7.4246, radius_km=10.0, max_age_days=7)

        assert len(sightings) == 1
        contact = sightings[0]
        assert contact["id"] == "recent_spot"
        assert contact["make_model"] == "Ferrari Daytona SP3"
        assert contact["rarity_tier"] == "Mythic 1-of-1"
        assert contact["lat"] == 43.7385
        assert contact["lng"] == 7.4250
        assert contact["age_hours"] < 5.0
        assert contact["distance_km"] < 1.0


def test_api_radar_endpoint(client):
    """Verify GET /api/radar endpoint returns JSON contacts within radius."""
    res = client.get("/api/radar?lat=43.7384&lng=7.4246&radius_km=10")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    if data:
        contact = data[0]
        for field in ["id", "make_model", "rarity_tier", "lat", "lng", "age_hours", "distance_km"]:
            assert field in contact, f"Missing required field {field} in contact"


def test_api_radar_radius_capping_and_bad_params(client):
    """Verify radius_km is capped at 50km and handles invalid input gracefully."""
    # Invalid lat/lng
    res_bad = client.get("/api/radar?lat=invalid&lng=7.4246")
    assert res_bad.status_code == 400

    # Max radius capping
    res_huge = client.get("/api/radar?lat=43.7384&lng=7.4246&radius_km=100")
    assert res_huge.status_code == 200
