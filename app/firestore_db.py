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

"""Firestore database client and helper functions for CarDex."""

import datetime
import math
from typing import Any
from google.cloud import firestore

# Geospatial Indexing & Privacy Quantization Constants
_GEOHASH_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
_GEOHASH_DECODE_MAP = {c: i for i, c in enumerate(_GEOHASH_BASE32)}


def encode_geohash(latitude: float, longitude: float, precision: int = 6) -> str:
    """Encodes latitude and longitude into a standard 32-character base32 geohash."""
    lat_interval = [-90.0, 90.0]
    lng_interval = [-180.0, 180.0]
    geohash = []
    bits = [16, 8, 4, 2, 1]
    bit = 0
    ch = 0
    even = True

    while len(geohash) < precision:
        if even:
            mid = (lng_interval[0] + lng_interval[1]) / 2.0
            if longitude > mid:
                ch |= bits[bit]
                lng_interval[0] = mid
            else:
                lng_interval[1] = mid
        else:
            mid = (lat_interval[0] + lat_interval[1]) / 2.0
            if latitude > mid:
                ch |= bits[bit]
                lat_interval[0] = mid
            else:
                lat_interval[1] = mid
        even = not even
        if bit < 4:
            bit += 1
        else:
            geohash.append(_GEOHASH_BASE32[ch])
            bit = 0
            ch = 0
    return "".join(geohash)


def decode_geohash(geohash: str) -> tuple[float, float]:
    """Decodes a geohash string into its center (latitude, longitude)."""
    lat_interval = [-90.0, 90.0]
    lng_interval = [-180.0, 180.0]
    even = True
    for c in geohash.lower():
        cd = _GEOHASH_DECODE_MAP.get(c, 0)
        for mask in [16, 8, 4, 2, 1]:
            if even:
                mid = (lng_interval[0] + lng_interval[1]) / 2.0
                if cd & mask:
                    lng_interval[0] = mid
                else:
                    lng_interval[1] = mid
            else:
                mid = (lat_interval[0] + lat_interval[1]) / 2.0
                if cd & mask:
                    lat_interval[0] = mid
                else:
                    lat_interval[1] = mid
            even = not even
    center_lat = (lat_interval[0] + lat_interval[1]) / 2.0
    center_lng = (lng_interval[0] + lng_interval[1]) / 2.0
    return center_lat, center_lng


def quantize_coordinates(
    latitude: float, longitude: float, precision: int = 6
) -> tuple[str, float, float]:
    """Applies spatial quantization to protect spotter privacy (~500m radius).

    Never stores raw high-precision GPS to prevent doxxing residential garages or private homes.
    Returns (geohash, fuzzy_lat, fuzzy_lng).
    """
    gh = encode_geohash(latitude, longitude, precision=precision)
    center_lat, center_lng = decode_geohash(gh)
    fuzzy_lat = round(center_lat, 4)
    fuzzy_lng = round(center_lng, 4)
    return gh, fuzzy_lat, fuzzy_lng


def haversine_distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Calculates great-circle distance between two points on Earth in kilometers."""
    r = 6371.0  # Earth radius in kilometers
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c


# IMPORTANT: Hardcode the GCP Project ID as a literal string.
# On Agent Platform, google.auth.default() and GOOGLE_CLOUD_PROJECT return
# the project NUMBER, which breaks Firestore resolution of the (default) database.
FIRESTORE_PROJECT_ID = "qwiklabs-gcp-04-6f324b699fdd"

_client: firestore.Client | None = None


def get_db() -> firestore.Client:
    """Returns the singleton Firestore Client configured with the hardcoded project ID."""
    global _client
    if _client is None:
        _client = firestore.Client(project=FIRESTORE_PROJECT_ID)
    return _client


def get_car_by_id_or_name(query: str) -> dict[str, Any] | None:
    """Retrieve car document from the 'cars' collection by doc ID or matching make/model."""
    db = get_db()
    slug = query.strip().lower().replace(" ", "-")

    # 1. Try exact document ID
    doc_ref = db.collection("cars").document(slug)
    doc = doc_ref.get()
    if doc.exists:
        data = doc.to_dict() or {}
        data["id"] = doc.id
        return data

    # 2. Try prefix/case-insensitive search across cars collection
    cars_ref = db.collection("cars")
    docs = cars_ref.stream()
    query_lower = query.strip().lower()

    for d in docs:
        car = d.to_dict()
        car["id"] = d.id
        full_name = f"{car.get('make', '')} {car.get('model', '')}".lower()
        if (
            query_lower in full_name
            or query_lower in d.id.lower()
            or query_lower == car.get("model", "").lower()
            or query_lower == car.get("make", "").lower()
        ):
            return car

    return None


def list_catalog_cars(
    make: str | None = None,
    rarity_tier: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List cars from the 'cars' collection, optionally filtered by make or rarity tier."""
    db = get_db()
    cars_ref = db.collection("cars")

    results = []
    for d in cars_ref.stream():
        car = d.to_dict()
        car["id"] = d.id
        if make and car.get("make", "").lower() != make.strip().lower():
            continue
        if rarity_tier and car.get("rarity_tier", "").lower() != rarity_tier.strip().lower():
            continue
        results.append(car)
        if len(results) >= limit:
            break

    # Sort results by points descending
    results.sort(key=lambda c: c.get("points", 0), reverse=True)
    return results


def record_car_spot_entry(
    car_id: str,
    colorway: str = "Standard",
    user_id: str = "spotter_1",
    location: str = "Public Road",
    notes: str = "",
    image_url: str | None = None,
    points_override: int | None = None,
    rarity_override: str | None = None,
    phash: str | None = None,
    points_breakdown: dict[str, Any] | None = None,
    paint_badge: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> dict[str, Any]:
    """Record a car spotting event in Firestore.

    Increments the car's global_spotted_count (Shazam-style count),
    writes a record to 'spots', and updates the user's leaderboard score.
    Fuzzes GPS coordinates into a privacy-safe ~500m geohash to protect residences.
    """
    db = get_db()

    # Find the car
    car = get_car_by_id_or_name(car_id)
    if not car:
        car_doc_id = car_id.strip().lower().replace(" ", "-")
        points = points_override if points_override is not None else 250
        rarity = rarity_override if rarity_override is not None else "Unverified"
        car_name = car_id
    else:
        car_doc_id = car["id"]
        points = points_override if points_override is not None else car.get("points", 250)
        rarity = rarity_override if rarity_override is not None else car.get("rarity_tier", "Common")
        car_name = f"{car.get('make', '')} {car.get('model', '')} ({car.get('trim', '')})"

        # Increment global Shazam-style spotted count in cars collection
        car_ref = db.collection("cars").document(car_doc_id)
        car_ref.update({"global_spotted_count": firestore.Increment(1)})

    # Quantize coordinates for spotter privacy (~500m radius)
    geohash_val = None
    fuzzy_lat = None
    fuzzy_lng = None
    if latitude is not None and longitude is not None:
        try:
            geohash_val, fuzzy_lat, fuzzy_lng = quantize_coordinates(float(latitude), float(longitude), precision=6)
        except Exception:
            pass

    # Log to 'spots' collection
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    spot_data = {
        "user_id": user_id,
        "car_id": car_doc_id,
        "car_name": car_name,
        "make_model": car_name,
        "colorway": colorway,
        "rarity_tier": rarity,
        "points_awarded": points,
        "location": location,
        "notes": notes,
        "image_url": image_url,
        "phash": phash,
        "points_breakdown": points_breakdown or {},
        "paint_badge": paint_badge,
        "status": "VERIFIED",
        "spotted_at": now,
        "spot_timestamp": now,
        "geohash": geohash_val,
        "fuzzy_lat": fuzzy_lat,
        "fuzzy_lng": fuzzy_lng,
    }
    _, spot_ref = db.collection("spots").add(spot_data)
    spot_data["spot_id"] = spot_ref.id

    # Update spotter's leaderboard profile in 'leaderboard' collection
    user_ref = db.collection("leaderboard").document(user_id)
    user_doc = user_ref.get()
    if user_doc.exists:
        user_ref.update({
            "total_points": firestore.Increment(points),
            "total_spots": firestore.Increment(1),
            "last_spotted_at": now,
        })
    else:
        user_ref.set({
            "user_id": user_id,
            "username": user_id.capitalize(),
            "total_points": points,
            "total_spots": 1,
            "rarest_spot": car_name,
            "last_spotted_at": now,
        })

    # Also update 'users' collection if registered account exists
    auth_user_ref = db.collection("users").document(user_id)
    if auth_user_ref.get().exists:
        auth_user_ref.update({
            "total_points": firestore.Increment(points),
            "total_spots": firestore.Increment(1),
            "last_spotted_at": now,
        })

    return spot_data


def get_user_garage_spots(user_id: str = "spotter_1") -> list[dict[str, Any]]:
    """Retrieve all cars spotted by a given user from 'spots' collection."""
    db = get_db()
    from google.cloud.firestore_v1.base_query import FieldFilter
    spots_ref = db.collection("spots").where(filter=FieldFilter("user_id", "==", user_id))
    spots = [d.to_dict() | {"id": d.id} for d in spots_ref.stream()]
    spots.sort(key=lambda s: s.get("spotted_at", ""), reverse=True)
    return spots


def get_leaderboard_rankings(limit: int = 10) -> list[dict[str, Any]]:
    """Retrieve top spotters from 'leaderboard' collection ranked by total points."""
    db = get_db()
    users = [d.to_dict() for d in db.collection("leaderboard").stream()]
    users.sort(key=lambda u: u.get("total_points", 0), reverse=True)
    for idx, u in enumerate(users[:limit], start=1):
        u["rank"] = idx
    return users[:limit]


def submit_dispute_review(
    car_name: str,
    issue_description: str,
    proposed_correction: str,
    user_id: str = "spotter_1",
    spot_id: str | None = None,
    image_url: str | None = None,
    proposed_make: str | None = None,
    proposed_model: str | None = None,
    proposed_trim: str | None = None,
    proposed_color: str | None = None,
) -> dict[str, Any]:
    """Submit a vehicle review / dispute to 'reviews' collection when classification needs fixing."""
    db = get_db()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    review_data = {
        "user_id": user_id,
        "car_name": car_name,
        "spot_id": spot_id,
        "image_url": image_url,
        "issue_description": issue_description,
        "proposed_correction": proposed_correction,
        "proposed_make": proposed_make,
        "proposed_model": proposed_model,
        "proposed_trim": proposed_trim,
        "proposed_color": proposed_color,
        "status": "UNDER_REVIEW",
        "votes": [],
        "consensus_threshold": 3,
        "bounty_points": 500,
        "submitted_at": now,
    }
    _, ref = db.collection("reviews").add(review_data)
    review_data["review_id"] = ref.id

    # If linked to a spot, mark spot as UNDER_REVIEW
    if spot_id:
        try:
            spot_ref = db.collection("spots").document(spot_id)
            if spot_ref.get().exists:
                spot_ref.update({"status": "UNDER_REVIEW", "dispute_id": ref.id})
        except Exception:
            pass

    return review_data


def get_pending_disputes(limit: int = 20) -> list[dict[str, Any]]:
    """Retrieve disputes awaiting Master Spotter consensus review."""
    db = get_db()
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter
        reviews_ref = db.collection("reviews").where(
            filter=FieldFilter("status", "in", ["UNDER_REVIEW", "pending_review"])
        ).limit(limit)
        reviews = [d.to_dict() | {"review_id": d.id} for d in reviews_ref.stream()]
        reviews.sort(key=lambda r: r.get("submitted_at", ""), reverse=True)
        return reviews
    except Exception:
        # Fallback in case composite index is warming up
        all_revs = [d.to_dict() | {"review_id": d.id} for d in db.collection("reviews").stream()]
        pending = [r for r in all_revs if r.get("status") in ["UNDER_REVIEW", "pending_review"]]
        pending.sort(key=lambda r: r.get("submitted_at", ""), reverse=True)
        return pending[:limit]


def cast_dispute_vote(
    review_id: str,
    reviewer_id: str,
    vote_agree: bool,
    reviewer_rank: str = "Master Spotter",
    comment: str = "",
) -> dict[str, Any]:
    """Cast a blind consensus vote on a pending dispute.

    If 3 blind votes are reached and agreement > 66% (2 of 3), the review is RESOLVED,
    the car entry is updated, and a +500 PTS bounty is credited to all participating reviewers.
    """
    db = get_db()
    review_ref = db.collection("reviews").document(review_id)
    doc = review_ref.get()

    if not doc.exists:
        return {"success": False, "error": f"Dispute review '{review_id}' not found."}

    review = doc.to_dict() or {}
    if review.get("status") in ["RESOLVED", "REJECTED"]:
        return {
            "success": False,
            "error": f"Dispute review has already been finalized ({review.get('status')}).",
        }

    votes = review.get("votes", [])

    # Prevent double voting by the same reviewer
    if any(v.get("reviewer_id") == reviewer_id for v in votes):
        return {"success": False, "error": "You have already cast your blind vote on this dispute."}

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    new_vote = {
        "reviewer_id": reviewer_id,
        "reviewer_rank": reviewer_rank,
        "vote_agree": bool(vote_agree),
        "comment": comment,
        "voted_at": now,
    }
    votes.append(new_vote)

    threshold = int(review.get("consensus_threshold", 3))
    bounty = int(review.get("bounty_points", 500))
    resolved = False
    consensus_passed = False

    update_fields: dict[str, Any] = {"votes": votes}

    if len(votes) >= threshold:
        # Tally consensus
        agrees = sum(1 for v in votes if v.get("vote_agree"))
        consensus_ratio = agrees / len(votes)

        if consensus_ratio > 0.66:
            consensus_passed = True
            update_fields["status"] = "RESOLVED"
            update_fields["resolved_at"] = now
            update_fields["consensus_ratio"] = round(consensus_ratio, 2)

            # Update the spot if linked
            spot_id = review.get("spot_id")
            if spot_id:
                try:
                    spot_ref = db.collection("spots").document(spot_id)
                    spot_doc = spot_ref.get()
                    if spot_doc.exists:
                        spot_data = spot_doc.to_dict() or {}
                        corrected_make = review.get("proposed_make") or spot_data.get("make")
                        corrected_model = review.get("proposed_model") or spot_data.get("model")
                        corrected_trim = review.get("proposed_trim") or ""
                        corrected_color = review.get("proposed_color") or spot_data.get("colorway")

                        # Re-score vehicle dynamically
                        try:
                            from .scoring_engine import compute_dynamic_spot_score
                        except ImportError:
                            from scoring_engine import compute_dynamic_spot_score
                        new_score = compute_dynamic_spot_score(
                            make=str(corrected_make),
                            model=str(corrected_model),
                            trim=str(corrected_trim),
                            is_pts=bool(review.get("is_pts", False)),
                            location=spot_data.get("location", "Public Road"),
                        )

                        old_points = spot_data.get("points_awarded", 0)
                        diff_points = new_score["final_points"] - old_points

                        spot_ref.update({
                            "status": "VERIFIED_BY_CONSENSUS",
                            "car_name": f"{corrected_make} {corrected_model} ({corrected_trim})".strip(),
                            "colorway": corrected_color,
                            "points_awarded": new_score["final_points"],
                            "rarity_tier": new_score["rarity_tier"],
                            "points_breakdown": new_score["breakdown"],
                            "adjudicated_at": now,
                        })

                        # Adjust original spotter's points
                        spot_user_id = spot_data.get("user_id")
                        if spot_user_id and diff_points != 0:
                            db.collection("leaderboard").document(spot_user_id).set(
                                {"total_points": firestore.Increment(diff_points)}, merge=True
                            )
                            db.collection("users").document(spot_user_id).set(
                                {"total_points": firestore.Increment(diff_points)}, merge=True
                            )
                except Exception:
                    pass

            # Award +500 PTS bounty to each participating reviewer
            for v in votes:
                v_user_id = v.get("reviewer_id")
                if v_user_id:
                    db.collection("leaderboard").document(v_user_id).set(
                        {"total_points": firestore.Increment(bounty)}, merge=True
                    )
                    db.collection("users").document(v_user_id).set(
                        {"total_points": firestore.Increment(bounty)}, merge=True
                    )

            resolved = True
        else:
            update_fields["status"] = "REJECTED"
            update_fields["resolved_at"] = now
            update_fields["consensus_ratio"] = round(consensus_ratio, 2)
            resolved = True

            # Revert spot status
            spot_id = review.get("spot_id")
            if spot_id:
                try:
                    db.collection("spots").document(spot_id).update({"status": "VERIFIED"})
                except Exception:
                    pass

    review_ref.update(update_fields)

    return {
        "success": True,
        "votes_count": len(votes),
        "threshold": threshold,
        "resolved": resolved,
        "consensus_passed": consensus_passed,
        "status": update_fields.get("status", review.get("status", "UNDER_REVIEW")),
        "bounty_awarded": bounty if (resolved and consensus_passed) else 0,
    }


def query_radar_sightings(
    lat: float,
    lng: float,
    radius_km: float = 10.0,
    max_age_days: int = 7,
) -> list[dict[str, Any]]:
    """Query Firestore for vehicle sightings within radius_km logged in the trailing max_age_days.

    Returns a list of sightings with fuzzy coordinates and age in hours.
    Fuzzy coordinates guarantee that no residential garages or high-precision locations are exposed.
    """
    db = get_db()
    clamped_radius = max(0.5, min(float(radius_km), 50.0))
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=max_age_days)
    cutoff_iso = cutoff.isoformat()

    sightings: list[dict[str, Any]] = []
    docs: list[Any] = []
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter
        query = db.collection("spots").where(filter=FieldFilter("spotted_at", ">=", cutoff_iso))
        docs = list(query.stream())
    except Exception:
        pass

    # If recent stream returned no docs or error, stream all spots and filter in-memory
    if not docs:
        try:
            docs = list(db.collection("spots").stream())
        except Exception as e:
            print("Radar query Firestore error:", e)
            docs = []

    for d in docs:
        spot = d.to_dict()
        spot_id = d.id
        spot_lat = spot.get("fuzzy_lat")
        spot_lng = spot.get("fuzzy_lng")

        if spot_lat is None or spot_lng is None:
            continue

        try:
            dist = haversine_distance_km(lat, lng, float(spot_lat), float(spot_lng))
        except (ValueError, TypeError):
            continue

        if dist > clamped_radius:
            continue

        # Parse age
        spotted_str = spot.get("spot_timestamp") or spot.get("spotted_at", "")
        age_hours = 1.0
        if spotted_str:
            try:
                cleaned_str = spotted_str.replace("Z", "+00:00")
                spotted_dt = datetime.datetime.fromisoformat(cleaned_str)
                if spotted_dt.tzinfo is None:
                    spotted_dt = spotted_dt.replace(tzinfo=datetime.timezone.utc)
                diff = (now - spotted_dt).total_seconds()
                if diff > max_age_days * 86400:
                    continue
                age_hours = round(max(0.1, diff / 3600.0), 1)
            except Exception:
                pass

        paint_code = None
        breakdown = spot.get("points_breakdown")
        if isinstance(breakdown, dict):
            paint_code = breakdown.get("paint_code")

        sightings.append({
            "id": spot_id,
            "make_model": spot.get("make_model") or spot.get("car_name", "Exotic Vehicle"),
            "rarity_tier": spot.get("rarity_tier", "Rare"),
            "lat": float(spot_lat),
            "lng": float(spot_lng),
            "geohash": spot.get("geohash", ""),
            "age_hours": age_hours,
            "distance_km": round(dist, 2),
            "image_url": spot.get("image_url"),
            "colorway": spot.get("colorway", "Standard"),
            "paint_code": paint_code or spot.get("colorway", "OEM Spec"),
            "paint_badge": spot.get("paint_badge"),
            "spotter": spot.get("user_id", "Anonymous Spotter"),
            "points_awarded": spot.get("points_awarded", 500),
            "status": spot.get("status", "VERIFIED"),
        })

    # Sort closest first
    sightings.sort(key=lambda s: s["distance_km"])
    return sightings
