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

"""Authentication and Firestore user persistence helper for CarDex."""

import hashlib
import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Optional
from google.cloud import firestore

FIRESTORE_PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "qwiklabs-gcp-04-6f324b699fdd")

_db: Optional[firestore.Client] = None
# In-memory session token store: token -> dict(user_id, created_at, expires_at)
_sessions: dict[str, dict[str, Any]] = {}
SESSION_TTL_SECONDS = 86400 * 7  # 7 days


def get_firestore_client() -> firestore.Client:
    """Returns the singleton Firestore client."""
    global _db
    if _db is None:
        _db = firestore.Client(project=FIRESTORE_PROJECT_ID)
    return _db


def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    """Hashes a password with PBKDF2-HMAC-SHA256 (100,000 iterations)."""
    if salt is None:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100_000,
    )
    return key.hex(), salt


def verify_password(password: str, hashed_key: str, salt: str) -> bool:
    """Verifies a password against the stored hash and salt."""
    key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100_000,
    )
    return secrets.compare_digest(key.hex(), hashed_key)


def calculate_rank(points: int) -> str:
    """Calculates spotter prestige rank based on total points."""
    if points >= 100_000:
        return "Apex Legend"
    elif points >= 50_000:
        return "Master Spotter"
    elif points >= 15_000:
        return "Senior Spotter"
    elif points >= 5_000:
        return "Pro Spotter"
    elif points >= 1_000:
        return "Amateur Spotter"
    return "Rookie Spotter"


def create_session(user_id: str) -> str:
    """Generates a secure session token and caches it."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    _sessions[token] = {
        "user_id": user_id,
        "created_at": now,
        "expires_at": now + SESSION_TTL_SECONDS,
    }
    return token


def validate_session(token: str) -> Optional[str]:
    """Validates session token and returns user_id if valid."""
    if not token or token not in _sessions:
        return None
    sess = _sessions[token]
    if time.time() > sess.get("expires_at", 0):
        del _sessions[token]
        return None
    return sess.get("user_id")


def revoke_session(token: str) -> None:
    """Removes a session token."""
    if token in _sessions:
        del _sessions[token]


def register_user(username: str, email: str, password: str) -> dict[str, Any]:
    """Registers a new user in Firestore 'users' and 'leaderboard' collections."""
    username = username.strip()
    email = email.strip().lower()

    if len(username) < 3 or len(username) > 30:
        raise ValueError("Username must be between 3 and 30 characters.")
    if not re.match(r"^[a-zA-Z0-9_-]+$", username):
        raise ValueError("Username may only contain letters, numbers, underscores, and dashes.")
    if len(password) < 4:
        raise ValueError("Password must be at least 4 characters long.")
    if not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
        raise ValueError("Please provide a valid email address.")

    db = get_firestore_client()
    users_col = db.collection("users")

    user_id = username.lower()

    # Check if username/user_id exists
    if users_col.document(user_id).get().exists:
        raise ValueError(f"Username '{username}' is already taken. Please choose another.")

    # Check if email is already registered
    from google.cloud.firestore_v1.base_query import FieldFilter
    existing_emails = list(users_col.where(filter=FieldFilter("email", "==", email)).limit(1).stream())
    if existing_emails:
        raise ValueError(f"Email '{email}' is already registered. Please sign in.")

    key_hex, salt = hash_password(password)
    now = datetime.now(timezone.utc).isoformat()

    user_doc = {
        "user_id": user_id,
        "username": username,
        "email": email,
        "password_hash": key_hex,
        "salt": salt,
        "created_at": now,
        "total_points": 0,
        "total_spots": 0,
        "rank": "Rookie Spotter",
    }
    users_col.document(user_id).set(user_doc)

    # Initialize leaderboard record so points increment smoothly
    lb_doc = db.collection("leaderboard").document(user_id)
    if not lb_doc.get().exists:
        lb_doc.set({
            "user_id": user_id,
            "username": username,
            "total_points": 0,
            "total_spots": 0,
            "rank": "Rookie Spotter",
            "last_spotted_at": now,
        })

    token = create_session(user_id)
    return {
        "token": token,
        "user": {
            "user_id": user_id,
            "username": username,
            "email": email,
            "total_points": 0,
            "points": 0,
            "total_spots": 0,
            "rank": "Rookie Spotter",
            "created_at": now,
        },
    }


def authenticate_user(username_or_email: str, password: str) -> dict[str, Any]:
    """Authenticates a user and returns session token and profile data."""
    query_str = username_or_email.strip()
    db = get_firestore_client()
    users_col = db.collection("users")

    user_data = None
    user_id = None

    # 1. Try by username / user_id
    doc = users_col.document(query_str.lower()).get()
    if doc.exists:
        user_data = doc.to_dict()
        user_id = doc.id
    else:
        # 2. Try by email
        from google.cloud.firestore_v1.base_query import FieldFilter
        email_matches = list(users_col.where(filter=FieldFilter("email", "==", query_str.lower())).limit(1).stream())
        if email_matches:
            user_data = email_matches[0].to_dict()
            user_id = email_matches[0].id

    if not user_data:
        # Fallback check: If spotter exists in leaderboard (e.g. demo account or spotter_1)
        lb_doc = db.collection("leaderboard").document(query_str.lower()).get()
        if lb_doc.exists:
            lb_data = lb_doc.to_dict()
            key_hex, salt = hash_password(password)
            now = datetime.now(timezone.utc).isoformat()
            pts = lb_data.get("total_points", 0)
            user_data = {
                "user_id": lb_doc.id,
                "username": lb_data.get("username", lb_doc.id),
                "email": f"{lb_doc.id}@cardex.internal",
                "password_hash": key_hex,
                "salt": salt,
                "created_at": now,
                "total_points": pts,
                "total_spots": lb_data.get("total_spots", 0),
                "rank": calculate_rank(pts),
            }
            users_col.document(lb_doc.id).set(user_data)
            user_id = lb_doc.id
        else:
            raise ValueError("Invalid username/email or password.")

    # Verify password hash
    if not verify_password(password, user_data.get("password_hash", ""), user_data.get("salt", "")):
        raise ValueError("Invalid username/email or password.")

    # Fetch live score from leaderboard
    lb_doc = db.collection("leaderboard").document(user_id).get()
    if lb_doc.exists:
        lb_info = lb_doc.to_dict()
        points = lb_info.get("total_points", 0)
        spots = lb_info.get("total_spots", 0)
        rarest = lb_info.get("rarest_spot", "")
    else:
        points = user_data.get("total_points", 0)
        spots = user_data.get("total_spots", 0)
        rarest = ""

    rank = calculate_rank(points)
    token = create_session(user_id)

    return {
        "token": token,
        "user": {
            "user_id": user_id,
            "username": user_data.get("username", user_id),
            "email": user_data.get("email", ""),
            "total_points": points,
            "points": points,
            "total_spots": spots,
            "rarest_spot": rarest,
            "rank": rank,
        },
    }


def get_user_profile(user_id: str) -> dict[str, Any]:
    """Retrieves full profile and live stats for user_id."""
    db = get_firestore_client()
    users_col = db.collection("users")
    doc = users_col.document(user_id).get()
    user_data = doc.to_dict() if doc.exists else {}

    lb_doc = db.collection("leaderboard").document(user_id).get()
    if lb_doc.exists:
        lb_info = lb_doc.to_dict()
        points = lb_info.get("total_points", 0)
        spots = lb_info.get("total_spots", 0)
        rarest = lb_info.get("rarest_spot", "")
        username = lb_info.get("username", user_data.get("username", user_id))
    else:
        points = user_data.get("total_points", 0)
        spots = user_data.get("total_spots", 0)
        rarest = user_data.get("rarest_spot", "")
        username = user_data.get("username", user_id)

    rank = calculate_rank(points)
    return {
        "user_id": user_id,
        "username": username,
        "email": user_data.get("email", ""),
        "total_points": points,
        "points": points,
        "total_spots": spots,
        "rarest_spot": rarest,
        "rank": rank,
        "created_at": user_data.get("created_at"),
    }


def get_user_submissions(user_id: str) -> list[dict[str, Any]]:
    """Retrieves all vehicle sightings and submissions submitted by user_id."""
    db = get_firestore_client()
    from google.cloud.firestore_v1.base_query import FieldFilter
    spots_ref = db.collection("spots").where(filter=FieldFilter("user_id", "==", user_id))
    spots = []
    for d in spots_ref.stream():
        data = d.to_dict()
        data["id"] = d.id
        spots.append(data)
    spots.sort(key=lambda s: s.get("spotted_at", ""), reverse=True)
    return spots


def get_user_stats(user_id: str) -> dict[str, Any]:
    """Retrieves detailed spot statistics and rarity tier breakdown."""
    profile = get_user_profile(user_id)
    submissions = get_user_submissions(user_id)

    rarity_counts: dict[str, int] = {}
    for s in submissions:
        tier = s.get("rarity_tier", "Common")
        rarity_counts[tier] = rarity_counts.get(tier, 0) + 1

    profile["rarity_counts"] = rarity_counts
    profile["submissions_count"] = len(submissions)
    return profile


def update_user_quota_tier(user_id: str, tier: str = "pro") -> dict[str, Any]:
    """Updates the user quota tier in Firestore."""
    db = get_firestore_client()
    user_ref = db.collection("users").document(user_id)
    user_ref.set({"quota": {"tier": tier.lower()}}, merge=True)
    return {"user_id": user_id, "tier": tier.lower()}


def increment_user_refill_credits(user_id: str, count: int = 5) -> dict[str, Any]:
    """Atomically increments the user refill_credits in Firestore."""
    db = get_firestore_client()
    user_ref = db.collection("users").document(user_id)
    user_ref.set({"quota": {"refill_credits": firestore.Increment(count)}}, merge=True)
    return {"user_id": user_id, "refill_credits_added": count}
