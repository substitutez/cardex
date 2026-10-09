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

"""Spontaneous Car Meet Radar Beacon & Algorithmic Spatial Clustering Engine.

Uses DBSCAN (Density-Based Spatial Clustering of Applications with Noise)
to automatically detect live gatherings and spontaneous car meets:
- Proximity threshold: eps = 300 meters
- Density threshold: min_spots = 3 spots (or unique spotters)
- Temporal recency window: Delta t = 45 minutes
- Alerts nearby spotters within 25 km of the epicenter
"""

import datetime
import hashlib
import logging
import math
from typing import Any

try:
    from .firestore_db import get_db, encode_geohash
except ImportError:
    try:
        from app.firestore_db import get_db, encode_geohash
    except ImportError:
        import firestore_db  # type: ignore
        get_db = firestore_db.get_db
        encode_geohash = firestore_db.encode_geohash

logger = logging.getLogger("cardex.cluster_beacon")

EPS_METERS = 300.0
MIN_SPOTS = 3
WINDOW_MINUTES = 45.0
ALERT_RADIUS_KM = 25.0


def haversine_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Computes the great-circle distance between two GPS coordinates in meters."""
    r_earth = 6371000.0  # Earth radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2)
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r_earth * c


def parse_spot_timestamp(spot: dict[str, Any]) -> datetime.datetime:
    """Extracts a UTC datetime from spot metadata."""
    ts = (
        spot.get("spot_timestamp")
        or spot.get("spotted_at")
        or spot.get("timestamp")
        or spot.get("created_at")
    )
    if isinstance(ts, datetime.datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=datetime.timezone.utc)
        return ts.astimezone(datetime.timezone.utc)
    if isinstance(ts, str):
        try:
            return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            pass
    return datetime.datetime.now(datetime.timezone.utc)


def detect_clusters_dbscan(
    spots: list[dict[str, Any]],
    eps_meters: float = EPS_METERS,
    min_spots: int = MIN_SPOTS,
    window_minutes: float = WINDOW_MINUTES,
    reference_time: datetime.datetime | None = None,
) -> list[dict[str, Any]]:
    """DBSCAN spatial clustering on vehicle spots within a temporal window.

    Args:
        spots: List of spot dictionaries containing latitude and longitude.
        eps_meters: Maximum spatial distance in meters to connect points (default 300m).
        min_spots: Minimum spots/users to form a dense cluster (default 3).
        window_minutes: Time window in minutes; spots older than this are ignored.
        reference_time: Reference time for windowing (defaults to UTC now).

    Returns:
        List of detected car meet cluster dictionaries.
    """
    ref_time = reference_time or datetime.datetime.now(datetime.timezone.utc)
    max_age_seconds = window_minutes * 60.0

    # 1. Filter spots with valid coordinates and within temporal window
    valid_spots = []
    for s in spots:
        lat = s.get("latitude") if s.get("latitude") is not None else s.get("lat") or s.get("fuzzy_lat")
        lng = s.get("longitude") if s.get("longitude") is not None else s.get("lng") or s.get("fuzzy_lng")
        if lat is None or lng is None:
            continue
        try:
            lat = float(lat)
            lng = float(lng)
        except (ValueError, TypeError):
            continue

        spot_time = parse_spot_timestamp(s)
        age = (ref_time - spot_time).total_seconds()
        if age > max_age_seconds or age < -300:  # Allow slight future clock skew
            continue

        valid_spots.append({
            "spot": s,
            "lat": lat,
            "lng": lng,
            "user_id": s.get("user_id") or "anonymous",
            "car_name": s.get("car_name") or s.get("make_model") or "Supercar",
            "time": spot_time,
        })

    n = len(valid_spots)
    if n < min_spots:
        return []

    # 2. Build neighbor adjacency list
    neighbors: list[list[int]] = [[] for _ in range(n)]
    for i in range(n):
        for j in range(n):
            dist = haversine_distance_meters(
                valid_spots[i]["lat"], valid_spots[i]["lng"],
                valid_spots[j]["lat"], valid_spots[j]["lng"],
            )
            if dist <= eps_meters:
                neighbors[i].append(j)

    # 3. DBSCAN clustering
    visited = [False] * n
    clusters: list[list[int]] = []

    for i in range(n):
        if visited[i]:
            continue
        visited[i] = True

        if len(neighbors[i]) < min_spots:
            continue  # Noise or border point initially

        current_cluster = [i]
        queue = list(neighbors[i])

        while queue:
            pt = queue.pop(0)
            if not visited[pt]:
                visited[pt] = True
                if len(neighbors[pt]) >= min_spots:
                    for nbr in neighbors[pt]:
                        if nbr not in queue:
                            queue.append(nbr)
            if pt not in current_cluster:
                current_cluster.append(pt)

        if len(current_cluster) >= min_spots:
            clusters.append(current_cluster)

    # 4. Format cluster metadata
    detected_meets = []
    for cluster_indices in clusters:
        cluster_pts = [valid_spots[idx] for idx in cluster_indices]
        avg_lat = sum(p["lat"] for p in cluster_pts) / len(cluster_pts)
        avg_lng = sum(p["lng"] for p in cluster_pts) / len(cluster_pts)

        unique_users = list({p["user_id"] for p in cluster_pts})
        exotics = list({p["car_name"] for p in cluster_pts})
        earliest_time = min(p["time"] for p in cluster_pts)
        expires_time = earliest_time + datetime.timedelta(minutes=window_minutes)

        # Geohash at precision 6 (~610m x 610m)
        geohash_sector = encode_geohash(avg_lat, avg_lng, precision=6)
        cluster_id = f"meet_{geohash_sector}_{int(earliest_time.timestamp())}"

        meet_entry = {
            "id": cluster_id,
            "center_lat": round(avg_lat, 6),
            "center_lng": round(avg_lng, 6),
            "geohash": geohash_sector,
            "car_count": len(cluster_pts),
            "unique_spotters": len(unique_users),
            "verified_exotics": exotics,
            "started_at": earliest_time.isoformat(),
            "expires_at": expires_time.isoformat(),
            "status": "CAR_MEET_DETECTED",
            "radius_meters": eps_meters,
        }
        detected_meets.append(meet_entry)

    return detected_meets


def record_spot_cluster_check(spot_data: dict[str, Any]) -> dict[str, Any] | None:
    """Ingestion hook: checks if newly spotted car forms or joins an active meet cluster.

    Args:
        spot_data: Spot dictionary containing latitude and longitude.

    Returns:
        Meet cluster dictionary if meet was formed/updated, else None.
    """
    lat = spot_data.get("latitude") if spot_data.get("latitude") is not None else spot_data.get("lat")
    lng = spot_data.get("longitude") if spot_data.get("longitude") is not None else spot_data.get("lng")
    if lat is None or lng is None:
        return None

    try:
        lat = float(lat)
        lng = float(lng)
    except (ValueError, TypeError):
        return None

    db = get_db()
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    min_time = now_dt - datetime.timedelta(minutes=WINDOW_MINUTES)

    # Fetch recent spots in the vicinity
    recent_spots = [spot_data]
    try:
        spots_ref = db.collection("spots").limit(50)
        for doc in spots_ref.stream():
            d = doc.to_dict() | {"id": doc.id}
            # Skip duplicate of current spot if already inserted
            if d.get("id") != spot_data.get("id"):
                recent_spots.append(d)
    except Exception as e:
        logger.warning("Error loading recent spots for meet clustering: %s", e)

    meets = detect_clusters_dbscan(
        spots=recent_spots,
        eps_meters=EPS_METERS,
        min_spots=MIN_SPOTS,
        window_minutes=WINDOW_MINUTES,
        reference_time=now_dt,
    )

    if not meets:
        return None

    # Find the meet that includes the current spot's coordinates
    matching_meet = None
    for m in meets:
        dist = haversine_distance_meters(lat, lng, m["center_lat"], m["center_lng"])
        if dist <= EPS_METERS * 1.5:
            matching_meet = m
            break

    if not matching_meet:
        matching_meet = meets[0]

    # Persist or update in Firestore 'active_meets' collection
    try:
        meet_ref = db.collection("active_meets").document(matching_meet["id"])
        meet_ref.set(matching_meet, merge=True)
        logger.info(
            "🔥 Active Car Meet Detected! %s at (%.4f, %.4f) with %d exotics",
            matching_meet["id"], matching_meet["center_lat"], matching_meet["center_lng"],
            matching_meet["car_count"]
        )
    except Exception as e:
        logger.warning("Failed to persist active meet to Firestore: %s", e)

    return matching_meet


def get_active_meets_near(
    lat: float,
    lng: float,
    radius_km: float = ALERT_RADIUS_KM,
) -> list[dict[str, Any]]:
    """Retrieves active, unexpired car meets within radius_km of the user's position."""
    db = get_db()
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    nearby_meets = []

    try:
        meets_ref = db.collection("active_meets").stream()
        for doc in meets_ref:
            m = doc.to_dict() | {"id": doc.id}
            # Check expiry
            exp_str = m.get("expires_at")
            if exp_str:
                try:
                    exp_dt = datetime.datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                    if exp_dt < now_dt:
                        continue
                except Exception:
                    pass

            c_lat = float(m.get("center_lat", 0.0))
            c_lng = float(m.get("center_lng", 0.0))
            dist_meters = haversine_distance_meters(lat, lng, c_lat, c_lng)
            dist_km = dist_meters / 1000.0

            if dist_km <= radius_km:
                m_copy = dict(m)
                m_copy["distance_km"] = round(dist_km, 2)
                nearby_meets.append(m_copy)
    except Exception as e:
        logger.warning("Error querying active meets: %s", e)

    nearby_meets.sort(key=lambda m: m.get("distance_km", 999.0))
    return nearby_meets
