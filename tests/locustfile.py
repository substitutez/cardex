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

"""Locust Load Testing Harness for CarDex Production Scaling.

Simulates 500 concurrent spotters under production traffic distribution:
- 70% browsing global leaderboard, user quotas, and vehicle garages.
- 20% polling the location-aware sighting radar map.
- 10% submitting simulated car scan payloads.

Enforces SLA assertions:
- Zero HTTP 500 Internal Server Errors.
- Cached route latency < 800ms (P95 SLA threshold).
"""

import base64
import random
import logging
from locust import HttpUser, task, between, events

logger = logging.getLogger("cardex.loadtest")

# Sample spotter profiles
SPOTTER_IDS = [f"spotter_{i}" for i in range(1, 101)]

# Major global car spotting hot-spots
GEO_LOCATIONS = [
    {"name": "San Francisco", "lat": 37.7749, "lng": -122.4194},
    {"name": "Los Angeles / Beverly Hills", "lat": 34.0736, "lng": -118.4004},
    {"name": "Miami / South Beach", "lat": 25.7617, "lng": -80.1918},
    {"name": "London / Mayfair", "lat": 51.5074, "lng": -0.1278},
    {"name": "Tokyo / Daikoku PA", "lat": 35.4522, "lng": 139.6732},
    {"name": "Monaco / Casino Square", "lat": 43.7384, "lng": 7.4246},
]

# Minimal valid 1x1 JPEG base64 payload for simulated spotting submissions
TINY_JPEG_B64 = (
    "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////"
    "////////////////////////////////////////////////////////////////////wgALCAAB"
    "AAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA="
)

# Latency SLA limit for cached endpoints (ms)
CACHED_ROUTE_MAX_MS = 800.0


class CardexSpotterUser(HttpUser):
    """Simulates an active CarDex supercar spotter interacting with the application."""

    # Pacing between actions simulating active mobile spotters
    wait_time = between(0.5, 2.0)

    def on_start(self):
        """Initialize spotter session with assigned profile and location."""
        self.user_id = random.choice(SPOTTER_IDS)
        self.location = random.choice(GEO_LOCATIONS)

    # --------------------------------------------------------------------------
    # 70% TRAFFIC: Browsing Leaderboard, Garages & Quotas
    # --------------------------------------------------------------------------
    @task(7)
    def browse_leaderboard_and_garages(self):
        """Fetch global leaderboard (Redis 60s cached) and user garage stats."""
        # 1. Global Leaderboard (High-frequency cached route)
        with self.client.get(
            "/api/leaderboard?limit=20",
            name="/api/leaderboard",
            catch_response=True,
        ) as response:
            if response.status_code >= 500:
                response.failure(f"HTTP {response.status_code} server error on /api/leaderboard")
            elif response.status_code == 200:
                is_hit = response.headers.get("X-Cache-Status") == "HIT"
                resp_ms = response.elapsed.total_seconds() * 1000.0
                if is_hit and resp_ms > CACHED_ROUTE_MAX_MS:
                    response.failure(
                        f"SLA violation: Cached /api/leaderboard latency {resp_ms:.1f}ms > {CACHED_ROUTE_MAX_MS}ms"
                    )
                else:
                    response.success()

        # 2. Check User Daily Quota (Redis midnight UTC cached)
        with self.client.get(
            f"/api/user/quota?user_id={self.user_id}",
            name="/api/user/quota",
            catch_response=True,
        ) as response:
            if response.status_code >= 500:
                response.failure(f"HTTP {response.status_code} on /api/user/quota")
            elif response.status_code == 200:
                is_hit = response.headers.get("X-Cache-Status") == "HIT"
                resp_ms = response.elapsed.total_seconds() * 1000.0
                if is_hit and resp_ms > CACHED_ROUTE_MAX_MS:
                    response.failure(
                        f"SLA violation: Cached /api/user/quota latency {resp_ms:.1f}ms > {CACHED_ROUTE_MAX_MS}ms"
                    )
                else:
                    response.success()

        # 3. User Submissions & Garage View
        with self.client.get(
            f"/api/user/submissions?user_id={self.user_id}",
            name="/api/user/submissions",
            catch_response=True,
        ) as response:
            if response.status_code >= 500:
                response.failure(f"HTTP {response.status_code} on /api/user/submissions")
            else:
                response.success()

    # --------------------------------------------------------------------------
    # 20% TRAFFIC: Sighting Radar Map Polling
    # --------------------------------------------------------------------------
    @task(2)
    def poll_radar_map(self):
        """Poll the location-aware sighting radar within a 25km radius."""
        loc = self.location
        url = f"/api/radar?lat={loc['lat']}&lng={loc['lng']}&radius_km=25"

        with self.client.get(url, name="/api/radar", catch_response=True) as response:
            if response.status_code >= 500:
                response.failure(f"HTTP {response.status_code} server error on /api/radar")
            elif response.status_code == 200:
                resp_ms = response.elapsed.total_seconds() * 1000.0
                if resp_ms > CACHED_ROUTE_MAX_MS:
                    response.failure(
                        f"SLA violation: /api/radar latency {resp_ms:.1f}ms > {CACHED_ROUTE_MAX_MS}ms"
                    )
                else:
                    response.success()

    # --------------------------------------------------------------------------
    # 10% TRAFFIC: Submitting Simulated Car Scan Payloads
    # --------------------------------------------------------------------------
    @task(1)
    def submit_car_scan(self):
        """Submit a vehicle sighting payload with location and image input."""
        payload = {
            "image": TINY_JPEG_B64,
            "user_id": self.user_id,
            "latitude": self.location["lat"],
            "longitude": self.location["lng"],
            "location_name": self.location["name"],
        }

        with self.client.post(
            "/api/spot",
            json=payload,
            name="/api/spot",
            catch_response=True,
        ) as response:
            # We assert no 500 crash under concurrent ingestion
            if response.status_code >= 500:
                response.failure(f"HTTP {response.status_code} crash on /api/spot")
            else:
                response.success()


# ------------------------------------------------------------------------------
# Global Locust Event Listeners for SLA & Error Assertions
# ------------------------------------------------------------------------------

@events.request.add_listener
def check_sla_and_errors(request_type, name, response_time, response_length, response, context, exception, **kwargs):
    """Global hook asserting zero HTTP 500s and enforcing cached route SLAs."""
    if exception:
        logger.error("Request %s %s threw exception: %s", request_type, name, exception)
        return

    # Assert no HTTP 500 error
    if response and response.status_code >= 500:
        logger.error(
            "CRITICAL: HTTP %s 500-level error detected on route %s",
            response.status_code,
            name,
        )

    # Assert response times for cached routes remain under 800ms
    if name in ["/api/leaderboard", "/api/user/quota", "/healthz"]:
        if response_time > CACHED_ROUTE_MAX_MS:
            logger.warning(
                "SLA Alert: %s exceeded 800ms threshold: %.1fms",
                name,
                response_time,
            )
