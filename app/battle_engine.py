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

"""CarDex Battle Arena: Asynchronous Garage PvP Stat Duels.

Enables spotters to challenge rival garages in Top Trumps / Pokemon-style card duels
wagering spotter points across 5 vehicle attributes:
1. Horsepower (raw engine output)
2. Acceleration (0-100 km/h computed as 100 / seconds)
3. Top Speed (km/h)
4. Exhaust Decibels (calibrated IEC 61672 peak dBA)
5. Rarity Tier (weighted production volume & PTS paint multiplier)
"""

import logging
import math
import random
from typing import Any

try:
    from .firestore_db import get_db
    from .vehicle_db import search_local_vehicle_database
except ImportError:
    try:
        from app.firestore_db import get_db
        from app.vehicle_db import search_local_vehicle_database
    except ImportError:
        import firestore_db  # type: ignore
        get_db = firestore_db.get_db
        import vehicle_db  # type: ignore
        search_local_vehicle_database = vehicle_db.search_local_vehicle_database

logger = logging.getLogger("cardex.battle_engine")

SUPPORTED_ATTRIBUTES = {
    "horsepower": "Engine Output (HP)",
    "acceleration": "0-100 km/h Quickness (100 / sec)",
    "top_speed": "Maximum Velocity (km/h)",
    "exhaust_decibels": "Exhaust Loudness (Peak dBA)",
    "rarity_tier": "Collector Rarity & PTS Multiplier",
}

TIER_BASE_SCORES = {
    "mythic": 1000.0,
    "legendary": 800.0,
    "epic": 600.0,
    "rare": 400.0,
    "uncommon": 250.0,
    "common": 100.0,
}

TIER_DEFAULTS = {
    "mythic": {"hp": 950.0, "accel_sec": 2.5, "top_speed": 400.0, "dba": 106.0},
    "legendary": {"hp": 750.0, "accel_sec": 3.0, "top_speed": 345.0, "dba": 102.0},
    "epic": {"hp": 580.0, "accel_sec": 3.6, "top_speed": 315.0, "dba": 98.0},
    "rare": {"hp": 430.0, "accel_sec": 4.4, "top_speed": 280.0, "dba": 94.0},
    "uncommon": {"hp": 290.0, "accel_sec": 5.8, "top_speed": 240.0, "dba": 89.0},
    "common": {"hp": 180.0, "accel_sec": 8.0, "top_speed": 205.0, "dba": 84.0},
}

CURATED_RIVAL_SPOTS = [
    {
        "id": "rival_spot_1",
        "user_id": "rival_alex",
        "username": "ApexPredator",
        "car_name": "Porsche 911 GT3 RS",
        "make_model": "Porsche 911 GT3 RS",
        "rarity_tier": "legendary",
        "power_hp": 525.0,
        "zero_to_100_s": 3.2,
        "top_speed_kmh": 296.0,
        "acoustic_telemetry": {"peak_dba": 103.8, "tier": "Track Weapon"},
        "paint_multiplier": 1.35,
        "colorway": "Viola Metallic (PTS)",
        "image_url": "https://images.unsplash.com/photo-1614162692292-7ac56d7f7f1e?w=800",
    },
    {
        "id": "rival_spot_2",
        "user_id": "rival_marco",
        "username": "MonacoScout",
        "car_name": "Ferrari SF90 Stradale",
        "make_model": "Ferrari SF90 Stradale",
        "rarity_tier": "mythic",
        "power_hp": 986.0,
        "zero_to_100_s": 2.5,
        "top_speed_kmh": 340.0,
        "acoustic_telemetry": {"peak_dba": 101.5, "tier": "Track Weapon"},
        "paint_multiplier": 1.20,
        "colorway": "Rosso Corsa",
        "image_url": "https://images.unsplash.com/photo-1592198084033-aade902d1aae?w=800",
    },
    {
        "id": "rival_spot_3",
        "user_id": "rival_elena",
        "username": "NurburgQueen",
        "car_name": "McLaren 765LT",
        "make_model": "McLaren 765LT",
        "rarity_tier": "legendary",
        "power_hp": 755.0,
        "zero_to_100_s": 2.8,
        "top_speed_kmh": 330.0,
        "acoustic_telemetry": {"peak_dba": 105.2, "tier": "Track Weapon"},
        "paint_multiplier": 1.25,
        "colorway": "Papaya Spark",
        "image_url": "https://images.unsplash.com/photo-1621135802920-133df287f89c?w=800",
    },
    {
        "id": "rival_spot_4",
        "user_id": "rival_kenji",
        "username": "TokyoMidnight",
        "car_name": "Nissan GT-R Nismo",
        "make_model": "Nissan GT-R Nismo",
        "rarity_tier": "epic",
        "power_hp": 600.0,
        "zero_to_100_s": 2.9,
        "top_speed_kmh": 315.0,
        "acoustic_telemetry": {"peak_dba": 99.4, "tier": "Sport Exhaust"},
        "paint_multiplier": 1.10,
        "colorway": "Stealth Gray",
        "image_url": "https://images.unsplash.com/photo-1552519507-da3b142c6e3d?w=800",
    },
]


def _get_vehicle_spec_fallback(car_name: str) -> dict[str, Any]:
    """Queries local vehicle specifications database for powertrain numbers."""
    if not car_name:
        return {}
    try:
        matches = search_local_vehicle_database(query=car_name, limit=1)
        if matches and isinstance(matches, list):
            m = matches[0]
            return {
                "power_hp": float(m.get("power_hp") or 0.0) or None,
                "top_speed_kmh": float(m.get("top_speed_kmh") or 0.0) or None,
                "zero_to_100_s": float(m.get("zero_to_100_s") or 0.0) or None,
            }
    except Exception as e:
        logger.debug("Vehicle DB spec lookup error: %s", e)
    return {}


def calculate_battle_power(spot_data: dict[str, Any], attribute: str) -> float:
    """Calculates battle power for a vehicle spot across 5 duel attributes.

    Args:
        spot_data: Vehicle spot data dictionary (from Firestore or garage).
        attribute: One of 'horsepower', 'acceleration', 'top_speed', 'exhaust_decibels', 'rarity_tier'.

    Returns:
        float score rounded to 2 decimal places.
    """
    if attribute not in SUPPORTED_ATTRIBUTES:
        raise ValueError(
            f"Invalid attribute '{attribute}'. Supported attributes: {list(SUPPORTED_ATTRIBUTES.keys())}"
        )

    tier = str(spot_data.get("rarity_tier") or "rare").strip().lower()
    defaults = TIER_DEFAULTS.get(tier, TIER_DEFAULTS["rare"])
    car_name = spot_data.get("car_name") or spot_data.get("make_model") or ""
    specs = spot_data.get("specs") if isinstance(spot_data.get("specs"), dict) else {}

    if attribute == "horsepower":
        # 1. Direct field on spot or nested specs
        hp = (
            spot_data.get("power_hp")
            or spot_data.get("horsepower")
            or specs.get("power_hp")
            or specs.get("horsepower")
        )
        if hp is not None:
            try:
                return round(float(hp), 2)
            except (ValueError, TypeError):
                pass

        # 2. Query vehicles.sqlite
        local_specs = _get_vehicle_spec_fallback(car_name)
        if local_specs.get("power_hp"):
            return round(float(local_specs["power_hp"]), 2)

        # 3. Fallback based on tier
        return round(float(defaults["hp"]), 2)

    elif attribute == "acceleration":
        # 0-100 km/h time (lower seconds is better; score = 100 / seconds)
        accel_sec = (
            spot_data.get("zero_to_100_s")
            or spot_data.get("acceleration_sec")
            or spot_data.get("acceleration")
            or specs.get("zero_to_100_s")
            or specs.get("acceleration_sec")
            or specs.get("acceleration")
        )
        val = None
        if accel_sec is not None:
            try:
                val = float(accel_sec)
            except (ValueError, TypeError):
                val = None

        if not val or val <= 0:
            local_specs = _get_vehicle_spec_fallback(car_name)
            val = local_specs.get("zero_to_100_s")

        if not val or val <= 0:
            val = defaults["accel_sec"]

        # Higher is better: 100 / seconds
        power = 100.0 / max(val, 0.5)
        return round(power, 2)

    elif attribute == "top_speed":
        # Top speed in km/h
        speed = (
            spot_data.get("top_speed_kmh")
            or spot_data.get("top_speed")
            or specs.get("top_speed_kmh")
            or specs.get("top_speed")
        )
        if speed is not None:
            try:
                return round(float(speed), 2)
            except (ValueError, TypeError):
                pass

        local_specs = _get_vehicle_spec_fallback(car_name)
        if local_specs.get("top_speed_kmh"):
            return round(float(local_specs["top_speed_kmh"]), 2)

        return round(float(defaults["top_speed"]), 2)

    elif attribute == "exhaust_decibels":
        # Calibrated peak dBA
        telem = spot_data.get("acoustic_telemetry") or {}
        dba = (
            telem.get("peak_dba")
            or spot_data.get("peak_dba")
            or spot_data.get("exhaust_dba")
        )
        if dba is not None:
            try:
                return round(float(dba), 2)
            except (ValueError, TypeError):
                pass
        return round(float(defaults["dba"]), 2)

    elif attribute == "rarity_tier":
        # Weighted score based on production rarity and PTS paint multiplier
        base_score = TIER_BASE_SCORES.get(tier, 400.0)
        paint_mult = float(spot_data.get("paint_multiplier") or 1.0)
        if spot_data.get("is_pts") or "pts" in str(spot_data.get("colorway", "")).lower():
            paint_mult = max(paint_mult, 1.35)

        chassis_mult = float(spot_data.get("first_finder_multiplier") or 1.0)
        total_rarity = base_score * paint_mult * chassis_mult
        return round(total_rarity, 2)

    return 0.0


def resolve_battle(
    player_spot: dict[str, Any],
    opponent_spot: dict[str, Any],
    attribute: str,
    wager: int = 100,
) -> dict[str, Any]:
    """Resolves a 1v1 PvP duel comparing chosen attribute battle power."""
    wager = max(50, min(int(wager), 500))
    player_power = calculate_battle_power(player_spot, attribute)
    opponent_power = calculate_battle_power(opponent_spot, attribute)

    player_car = player_spot.get("car_name") or player_spot.get("make_model") or "Your Car"
    opponent_car = opponent_spot.get("car_name") or opponent_spot.get("make_model") or "Rival Car"
    attr_label = SUPPORTED_ATTRIBUTES.get(attribute, attribute)

    if player_power > opponent_power:
        winner = "player"
        points_awarded = wager
        commentary = (
            f"🏆 Victory! Your {player_car} dominated with {player_power} vs {opponent_power} "
            f"in {attr_label}. You claimed the {wager * 2} PTS pot!"
        )
    elif player_power < opponent_power:
        winner = "opponent"
        points_awarded = -wager
        commentary = (
            f"💥 Defeat! Rival {opponent_car} edged out your {player_car} with "
            f"{opponent_power} vs {player_power} in {attr_label}."
        )
    else:
        winner = "draw"
        points_awarded = 0
        commentary = (
            f"🤝 Dead Heat Tie! Both vehicles locked at an identical {player_power} in {attr_label}. "
            f"Your {wager} PTS wager has been fully refunded."
        )

    outcome = "win" if winner == "player" else ("loss" if winner == "opponent" else "draw")
    awarded_points = wager * 2 if winner == "player" else (wager if winner == "draw" else 0)

    return {
        "winner": winner,
        "outcome": outcome,
        "attribute": attribute,
        "attribute_label": attr_label,
        "wager": wager,
        "pot": wager * 2,
        "points_delta": points_awarded,
        "net_delta": points_awarded,
        "awarded_points": awarded_points,
        "player_power": player_power,
        "player_score": player_power,
        "opponent_power": opponent_power,
        "opponent_score": opponent_power,
        "power_delta": round(abs(player_power - opponent_power), 2),
        "commentary": commentary,
        "opponent": {
            "id": opponent_spot.get("id"),
            "username": opponent_spot.get("username", "Rival Spotter"),
            "car_name": opponent_car,
            "rarity_tier": opponent_spot.get("rarity_tier", "rare"),
            "score": opponent_power,
        },
        "player_spot": {
            "id": player_spot.get("id"),
            "car_name": player_car,
            "rarity_tier": player_spot.get("rarity_tier", "rare"),
            "colorway": player_spot.get("colorway", "Standard"),
            "image_url": player_spot.get("image_url"),
            "score": player_power,
        },
        "opponent_spot": {
            "id": opponent_spot.get("id"),
            "user_id": opponent_spot.get("user_id", "rival_spotter"),
            "username": opponent_spot.get("username", "Rival Spotter"),
            "car_name": opponent_car,
            "rarity_tier": opponent_spot.get("rarity_tier", "rare"),
            "colorway": opponent_spot.get("colorway", "Standard"),
            "image_url": opponent_spot.get("image_url"),
            "score": opponent_power,
        },
    }


def find_opponent_spot(user_id: str, preferred_tier: str | None = None) -> dict[str, Any]:
    """Finds an asynchronous opponent car from Firestore or curated rivals."""
    db = get_db()
    candidates = []

    try:
        spots_ref = db.collection("spots").limit(30)
        for doc in spots_ref.stream():
            d = doc.to_dict() | {"id": doc.id}
            if d.get("user_id") != user_id:
                candidates.append(d)
    except Exception as e:
        logger.warning("Error fetching opponent spots from Firestore: %s", e)

    if preferred_tier and len(candidates) > 3:
        tiered = [c for c in candidates if str(c.get("rarity_tier", "")).lower() == preferred_tier.lower()]
        if tiered:
            candidates = tiered

    if candidates:
        return random.choice(candidates)

    return random.choice(CURATED_RIVAL_SPOTS)


def execute_battle_challenge(
    user_id: str,
    spot_id: str,
    attribute: str,
    wager: int = 100,
) -> dict[str, Any]:
    """Coordinates and executes an asynchronous battle challenge with atomic Firestore points update."""
    if attribute not in SUPPORTED_ATTRIBUTES:
        raise ValueError(f"Invalid attribute '{attribute}'. Supported attributes: {list(SUPPORTED_ATTRIBUTES.keys())}")

    try:
        wager_val = int(wager)
    except (ValueError, TypeError):
        raise ValueError("Wager must be an integer.")

    if not (50 <= wager_val <= 500):
        raise ValueError(f"Point wager {wager} is out of bounds. Must be between 50 and 500 PTS.")

    wager = wager_val
    db = get_db()

    player_spot = None
    if spot_id:
        try:
            doc = db.collection("spots").document(spot_id).get()
            if doc.exists:
                player_spot = doc.to_dict() | {"id": doc.id}
        except Exception as e:
            logger.warning("Failed to fetch player spot '%s': %s", spot_id, e)

    if not player_spot:
        try:
            user_spots = [
                d.to_dict() | {"id": d.id}
                for d in db.collection("spots").where("user_id", "==", user_id).limit(1).stream()
            ]
            if user_spots:
                player_spot = user_spots[0]
        except Exception:
            pass

    if not player_spot:
        player_spot = {
            "id": spot_id or "default_player_spot",
            "user_id": user_id,
            "car_name": "Ferrari 488 Pista",
            "rarity_tier": "legendary",
            "power_hp": 710.0,
            "zero_to_100_s": 2.85,
            "top_speed_kmh": 340.0,
            "acoustic_telemetry": {"peak_dba": 102.5},
            "paint_multiplier": 1.2,
            "colorway": "Rosso Scuderia",
        }

    opponent_spot = find_opponent_spot(user_id=user_id, preferred_tier=player_spot.get("rarity_tier"))

    resolution = resolve_battle(
        player_spot=player_spot,
        opponent_spot=opponent_spot,
        attribute=attribute,
        wager=wager,
    )

    pts_delta = resolution["points_delta"]
    from google.cloud import firestore

    try:
        user_ref = db.collection("users").document(user_id)
        if user_ref.get().exists:
            user_ref.update({"total_points": firestore.Increment(pts_delta)})

        leaderboard_ref = db.collection("leaderboard").document(user_id)
        if leaderboard_ref.get().exists:
            leaderboard_ref.update({"total_points": firestore.Increment(pts_delta)})

        db.collection("battles").add({
            "challenger_user_id": user_id,
            "rival_user_id": opponent_spot.get("user_id"),
            "attribute": attribute,
            "wager": wager,
            "winner": resolution["winner"],
            "points_delta": pts_delta,
            "timestamp": firestore.SERVER_TIMESTAMP,
        })
    except Exception as e:
        logger.warning("Battle points persistence warning: %s", e)

    out = dict(resolution)
    out["success"] = True
    out["battle"] = resolution
    return out
