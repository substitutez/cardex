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
from typing import Any
from google.cloud import firestore

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
) -> dict[str, Any]:
    """Record a car spotting event in Firestore.

    Increments the car's global_spotted_count (Shazam-style count),
    writes a record to 'spots', and updates the user's leaderboard score.
    """
    db = get_db()

    # Find the car
    car = get_car_by_id_or_name(car_id)
    if not car:
        # Create an unverified/custom entry if car not in catalog yet
        car_doc_id = car_id.strip().lower().replace(" ", "-")
        points = 250
        rarity = "Unverified"
        car_name = car_id
    else:
        car_doc_id = car["id"]
        points = car.get("points", 250)
        rarity = car.get("rarity_tier", "Common")
        car_name = f"{car.get('make', '')} {car.get('model', '')} ({car.get('trim', '')})"

        # Increment global Shazam-style spotted count in cars collection
        car_ref = db.collection("cars").document(car_doc_id)
        car_ref.update({"global_spotted_count": firestore.Increment(1)})

    # Log to 'spots' collection
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    spot_data = {
        "user_id": user_id,
        "car_id": car_doc_id,
        "car_name": car_name,
        "colorway": colorway,
        "rarity_tier": rarity,
        "points_awarded": points,
        "location": location,
        "notes": notes,
        "image_url": image_url,
        "spotted_at": now,
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
) -> dict[str, Any]:
    """Submit a vehicle review / dispute to 'reviews' collection when classification needs fixing."""
    db = get_db()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    review_data = {
        "user_id": user_id,
        "car_name": car_name,
        "issue_description": issue_description,
        "proposed_correction": proposed_correction,
        "status": "pending_review",
        "submitted_at": now,
    }
    _, ref = db.collection("reviews").add(review_data)
    review_data["review_id"] = ref.id
    return review_data
