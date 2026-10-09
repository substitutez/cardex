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

"""Daily Quota Limiter & Monetization Middleware for CarDex.

Enforces a 5 scans/day rolling limiter on free-tier users, tracks consumable refill credits,
and gates Gemini Vision API usage to prevent token exhaustion and preserve economy balance.
"""

import datetime
from typing import Any
from google.cloud import firestore
try:
    from .firestore_db import get_db
except ImportError:
    from firestore_db import get_db

DEFAULT_DAILY_LIMIT = 5


def _parse_iso(timestamp_str: str | None) -> datetime.datetime | None:
    if not timestamp_str:
        return None
    try:
        return datetime.datetime.fromisoformat(timestamp_str)
    except Exception:
        return None


def get_user_quota(user_id: str) -> dict[str, Any]:
    """Retrieve the current scan quota, tier, and refill credits for a spotter."""
    db = get_db()
    user_ref = db.collection("users").document(user_id)
    doc = user_ref.get()

    now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.isoformat()

    if not doc.exists:
        # Default quota structure for guest / fresh user
        return {
            "user_id": user_id,
            "scans_today": 0,
            "daily_limit": DEFAULT_DAILY_LIMIT,
            "tier": "free",
            "refill_credits": 0,
            "remaining_scans": DEFAULT_DAILY_LIMIT,
            "can_scan": True,
            "next_reset_seconds": 86400,
            "last_reset": now_iso,
        }

    data = doc.to_dict() or {}
    quota = data.get("quota") or {}

    scans_today = int(quota.get("scans_today", 0))
    daily_limit = int(quota.get("daily_limit", DEFAULT_DAILY_LIMIT))
    tier = str(quota.get("tier", "free")).lower()
    refill_credits = int(quota.get("refill_credits", 0))
    last_reset_str = quota.get("last_reset")

    last_reset = _parse_iso(last_reset_str)
    needs_reset = False

    if last_reset is None or (now - last_reset).total_seconds() >= 86400:
        scans_today = 0
        last_reset = now
        needs_reset = True

    if needs_reset:
        user_ref.set(
            {
                "quota": {
                    "scans_today": scans_today,
                    "daily_limit": daily_limit,
                    "tier": tier,
                    "refill_credits": refill_credits,
                    "last_reset": now.isoformat(),
                }
            },
            merge=True,
        )

    remaining_free = max(0, daily_limit - scans_today)
    total_remaining = 999999 if tier == "pro" else (remaining_free + refill_credits)
    can_scan = (tier == "pro") or (total_remaining > 0)

    # Calculate seconds until next reset
    seconds_since_reset = (now - last_reset).total_seconds()
    next_reset_seconds = max(0, int(86400 - seconds_since_reset))

    return {
        "user_id": user_id,
        "scans_today": scans_today,
        "daily_limit": daily_limit,
        "tier": tier,
        "refill_credits": refill_credits,
        "remaining_scans": total_remaining,
        "remaining_free": remaining_free,
        "can_scan": can_scan,
        "next_reset_seconds": next_reset_seconds,
        "last_reset": last_reset.isoformat(),
    }


def check_and_reserve_scan(user_id: str) -> tuple[bool, str, dict[str, Any]]:
    """Verify if the user has available scan quota before processing image.

    Returns:
        (allowed: bool, reason: str, quota_info: dict)
    """
    quota = get_user_quota(user_id)
    if quota["tier"] == "pro":
        return True, "Pro tier unlimited access", quota

    if quota["can_scan"]:
        return True, "Scan quota available", quota

    return (
        False,
        f"Daily scan limit reached ({quota['scans_today']}/{quota['daily_limit']}). Upgrade to Pro or get a scan refill to continue spotting today.",
        quota,
    )


def commit_scan_deduction(user_id: str) -> dict[str, Any]:
    """Atomically commit scan deduction upon successful vehicle parsing.

    Deducts 1 free scan, or 1 refill credit if free scans are exhausted. Pro tier is untouched.
    """
    db = get_db()
    user_ref = db.collection("users").document(user_id)

    @firestore.transactional
    def _deduct_txn(transaction: firestore.Transaction) -> dict[str, Any]:
        doc = user_ref.get(transaction=transaction)
        now = datetime.datetime.now(datetime.timezone.utc)
        if not doc.exists:
            transaction.set(
                user_ref,
                {
                    "quota": {
                        "scans_today": 1,
                        "daily_limit": DEFAULT_DAILY_LIMIT,
                        "tier": "free",
                        "refill_credits": 0,
                        "last_reset": now.isoformat(),
                    }
                },
                merge=True,
            )
            return {"scans_today": 1, "tier": "free", "remaining_scans": 4}

        data = doc.to_dict() or {}
        quota = data.get("quota") or {}
        tier = str(quota.get("tier", "free")).lower()
        scans_today = int(quota.get("scans_today", 0))
        daily_limit = int(quota.get("daily_limit", DEFAULT_DAILY_LIMIT))
        refill_credits = int(quota.get("refill_credits", 0))
        last_reset = _parse_iso(quota.get("last_reset"))

        if last_reset is None or (now - last_reset).total_seconds() >= 86400:
            scans_today = 0
            last_reset = now

        if tier == "pro":
            # Pro tier does not consume daily counts
            transaction.set(
                user_ref,
                {"quota": {"last_scanned_at": now.isoformat()}},
                merge=True,
            )
            return {"scans_today": scans_today, "tier": "pro", "remaining_scans": 999999}

        if scans_today < daily_limit:
            scans_today += 1
        elif refill_credits > 0:
            refill_credits -= 1

        transaction.set(
            user_ref,
            {
                "quota": {
                    "scans_today": scans_today,
                    "daily_limit": daily_limit,
                    "tier": tier,
                    "refill_credits": refill_credits,
                    "last_reset": last_reset.isoformat(),
                    "last_scanned_at": now.isoformat(),
                }
            },
            merge=True,
        )

        remaining_free = max(0, daily_limit - scans_today)
        return {
            "scans_today": scans_today,
            "daily_limit": daily_limit,
            "tier": tier,
            "refill_credits": refill_credits,
            "remaining_scans": remaining_free + refill_credits,
        }

    txn = db.transaction()
    return _deduct_txn(txn)


def add_refill_credits(user_id: str, count: int = 5) -> dict[str, Any]:
    """Credit consumable scan refills to a user."""
    db = get_db()
    user_ref = db.collection("users").document(user_id)
    user_ref.set(
        {"quota": {"refill_credits": firestore.Increment(count)}},
        merge=True,
    )
    return get_user_quota(user_id)


def set_user_tier(user_id: str, tier: str = "pro") -> dict[str, Any]:
    """Update user account tier between 'free' and 'pro'."""
    db = get_db()
    user_ref = db.collection("users").document(user_id)
    user_ref.set(
        {"quota": {"tier": tier.lower()}},
        merge=True,
    )
    return get_user_quota(user_id)
