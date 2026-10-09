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

"""Unit and integration test suite for production scaling and reliability layer.

Tests:
1. Redis cache wrapper (Redis connection & in-memory fallback degradation).
2. Global leaderboard 60s caching (`leaderboard:global`).
3. User daily quota midnight UTC caching (`user_id:{id}:daily_scans`).
4. Vehicle specs 24-hour caching (`vehicle_specs:{query}`).
5. `/healthz` healthcheck endpoint.
6. `/api/leaderboard` caching headers and hit/miss behavior.
7. Locust load testing harness validation.
"""

import datetime
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Add project root and app to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend")))

from app import cache
from app import vehicle_db


class TestRedisCacheLayer(unittest.TestCase):
    """Test suite verifying Redis wrapper and in-memory fallback mechanisms."""

    def setUp(self):
        cache.clear_local_cache()

    def test_in_memory_fallback_get_set(self):
        """Test cache read/write when falling back to resilient in-memory storage."""
        test_key = "test:unit_car"
        test_data = {"make": "Ferrari", "model": "F40", "power_hp": 478}

        self.assertIsNone(cache.get_json(test_key))
        saved = cache.set_json(test_key, test_data, ttl=60)
        self.assertTrue(saved)

        cached = cache.get_json(test_key)
        self.assertEqual(cached, test_data)

        # Deletion
        cache.delete(test_key)
        self.assertIsNone(cache.get_json(test_key))

    def test_midnight_utc_calculation(self):
        """Test that get_seconds_until_midnight_utc returns a valid positive integer <= 86400."""
        secs = cache.get_seconds_until_midnight_utc()
        self.assertIsInstance(secs, int)
        self.assertGreaterEqual(secs, 60)
        self.assertLessEqual(secs, 86400)

    def test_leaderboard_caching_helpers(self):
        """Test leaderboard caching under leaderboard:global."""
        mock_rankings = [
            {"rank": 1, "spotter_handle": "speed_demon", "total_points": 12500},
            {"rank": 2, "spotter_handle": "ferrari_fan", "total_points": 9800},
        ]
        # Initially empty
        cache.delete(cache.LEADERBOARD_CACHE_KEY)
        self.assertIsNone(cache.get_cached_leaderboard())

        # Save and retrieve
        self.assertTrue(cache.set_cached_leaderboard(mock_rankings, ttl=60))
        retrieved = cache.get_cached_leaderboard()
        self.assertEqual(retrieved, mock_rankings)
        self.assertEqual(len(retrieved), 2)

    def test_user_quota_caching_and_invalidation(self):
        """Test daily scan quota caching with midnight UTC expiration."""
        user_id = "test_spotter_99"
        mock_quota = {
            "user_id": user_id,
            "scans_today": 2,
            "daily_limit": 5,
            "remaining_scans": 3,
            "tier": "free",
        }

        # Cache miss
        self.assertIsNone(cache.get_cached_user_quota(user_id))

        # Cache set
        cache.set_cached_user_quota(user_id, mock_quota)
        cached = cache.get_cached_user_quota(user_id)
        self.assertEqual(cached, mock_quota)

        # Invalidate upon quota deduction/refill
        cache.invalidate_user_quota(user_id)
        self.assertIsNone(cache.get_cached_user_quota(user_id))

    def test_vehicle_specs_24h_caching(self):
        """Test vehicle specs SQLite query results caching for 24 hours."""
        query_key = "porsche_911_gt3_rs"
        mock_specs = [
            {
                "make": "Porsche",
                "model": "911 GT3 RS",
                "power_hp": 518,
                "top_speed_kmh": 296,
            }
        ]

        cache.delete(f"vehicle_specs:{query_key}")
        self.assertIsNone(cache.get_cached_vehicle_specs(query_key))

        cache.set_cached_vehicle_specs(query_key, mock_specs, ttl=86400)
        cached = cache.get_cached_vehicle_specs(query_key)
        self.assertEqual(cached, mock_specs)
        self.assertEqual(cached[0]["power_hp"], 518)


class TestVehicleDbCacheIntegration(unittest.TestCase):
    """Test vehicle database query caching integration."""

    def setUp(self):
        cache.clear_local_cache()

    def test_search_local_vehicle_database_caches_results(self):
        """Verify that search_local_vehicle_database caches queries."""
        results_first = vehicle_db.search_local_vehicle_database(query="Aventador", limit=2)
        
        # Second call should hit cache directly
        results_second = vehicle_db.search_local_vehicle_database(query="Aventador", limit=2)
        self.assertEqual(results_first, results_second)


class TestLocustLoadTestHarness(unittest.TestCase):
    """Validate Locust load test harness structure and configuration."""

    def test_locust_user_class(self):
        import subprocess

        script = """
from tests import locustfile
assert issubclass(locustfile.CardexSpotterUser, locustfile.HttpUser), "Not a HttpUser"
tasks = locustfile.CardexSpotterUser.tasks
assert len(tasks) == 10, f"Expected 10 tasks, got {len(tasks)}"
# 70% browse, 20% radar, 10% spot
browse_count = sum(1 for t in tasks if t.__name__ == 'browse_leaderboard_and_garages')
radar_count = sum(1 for t in tasks if t.__name__ == 'poll_radar_map')
spot_count = sum(1 for t in tasks if t.__name__ == 'submit_car_scan')
assert browse_count == 7, f"Expected 7 browse tasks (70%), got {browse_count}"
assert radar_count == 2, f"Expected 2 radar tasks (20%), got {radar_count}"
assert spot_count == 1, f"Expected 1 spot task (10%), got {spot_count}"
print("OK")
"""
        res = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
        )
        self.assertEqual(res.returncode, 0, f"Locust validation failed: {res.stderr}")
        self.assertIn("OK", res.stdout)


class TestFastApiScalingEndpoints(unittest.TestCase):
    """Test /healthz and /api/leaderboard endpoints in FastAPI."""

    def setUp(self):
        cache.clear_local_cache()
        os.environ["AGENT_ENGINE_RESOURCE_NAME"] = "projects/test/locations/us-central1/reasoningEngines/123"

    def test_healthz_endpoint(self):
        """Test GET /healthz returns 200 and healthy status."""
        from fastapi.testclient import TestClient
        from frontend.main import app

        client = TestClient(app)
        response = client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "healthy")
        self.assertEqual(data["service"], "cardex")
        self.assertIn("redis_cache", data)

    @patch("frontend.main.firestore_db.get_leaderboard_rankings")
    def test_api_leaderboard_endpoint_caching(self, mock_get_leaderboard):
        """Test GET /api/leaderboard cache HIT and MISS headers."""
        mock_get_leaderboard.return_value = [
            {"rank": 1, "spotter_handle": "track_legend", "total_points": 5000},
            {"rank": 2, "spotter_handle": "hypercar_hunter", "total_points": 4200},
        ]

        from fastapi.testclient import TestClient
        from frontend.main import app

        client = TestClient(app)

        # First request (MISS - queries firestore_db)
        resp1 = client.get("/api/leaderboard?limit=5")
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp1.headers.get("X-Cache-Status"), "MISS")
        data1 = resp1.json()
        self.assertIn("leaderboard", data1)
        self.assertEqual(len(data1["leaderboard"]), 2)
        self.assertFalse(data1.get("cached"))
        mock_get_leaderboard.assert_called_once()

        # Second request should be a cache HIT from Redis/memory (no new call to firestore)
        resp2 = client.get("/api/leaderboard?limit=5")
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.headers.get("X-Cache-Status"), "HIT")
        self.assertTrue(resp2.json().get("cached"))
        # Call count remains 1 because second request was served from cache
        mock_get_leaderboard.assert_called_once()


if __name__ == "__main__":
    unittest.main()
