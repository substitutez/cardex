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

"""ADK Function Tools for CarDex interacting with the Firestore backend."""

import base64
import json
import logging
import os
import re
import urllib.parse
import urllib.request
import uuid
from typing import Any
from google import genai
from google.genai import types
from google.cloud import storage
from .firestore_db import (
    get_car_by_id_or_name,
    get_leaderboard_rankings,
    get_user_garage_spots,
    list_catalog_cars,
    record_car_spot_entry,
    submit_dispute_review,
    record_chassis_registry_entry,
    award_user_badge,
)
from .vehicle_db import search_local_vehicle_database
from .anti_cheat import verify_image_integrity
from .paint_matcher import match_oem_paint_color
from .quota_limiter import check_and_reserve_scan, commit_scan_deduction
from .scoring_engine import compute_dynamic_spot_score
from .a2ui_utils import (
    build_paint_match_card,
    build_point_breakdown_card,
    build_paywall_card,
    build_acoustic_verification_card,
    build_chassis_verification_card,
)
from .audio_classifier import classify_engine_note

logger = logging.getLogger("cardex.car_tools")

CLOUD_STORAGE_BUCKET = "cardex-spots-qwiklabs-gcp-04-6f324b699fdd"
VERTEX_PROJECT_ID = "qwiklabs-gcp-04-6f324b699fdd"
VERTEX_LOCATION = "global"


def lookup_car_in_cardex(query: str) -> dict[str, Any]:
    """Look up a car in the CarDex Firestore database by make, model, or slug.

    Falls back to the comprehensive 30,535-engine vehicle specifications database if not yet in CarDex.

    Args:
        query: The name, make, model, or document slug of the car (e.g. 'Bugatti Bolide', 'Zonda Cinque', 'Koenigsegg One:1').

    Returns:
        A dictionary containing the car's specifications, rarity tier, points, production count,
        global spotted count, colorways, and historical/provenance notes.
    """
    car = get_car_by_id_or_name(query)
    if car:
        return {"found": True, "source": "cardex_catalog", "car": car}

    # Fallback to local vehicle specifications dataset (164 makes, 2,649 models)
    local_matches = search_local_vehicle_database(query=query, limit=3)
    if local_matches:
        return {
            "found": True,
            "source": "vehicle_specs_database",
            "message": f"'{query}' is not yet registered as a scored collectible in CarDex Firestore, but verified factory specs were found in the vehicle database.",
            "specs_record": local_matches[0],
            "other_matches": local_matches[1:],
            "can_spot": True,
            "prompt": "You can log a new spot for this vehicle to earn spotter points and add it to your garage!",
        }

    return {
        "found": False,
        "message": f"Car matching '{query}' was not found in CarDex or the vehicle specifications database. You can search the web or submit it for review.",
    }


def list_cardex_cars(
    rarity_tier: str | None = None,
    make: str | None = None,
) -> list[dict[str, Any]]:
    """List vehicles stored in the CarDex catalog, optionally filtered by make or rarity tier.

    Args:
        rarity_tier: Optional rarity filter (e.g., 'Mythic 1-of-1', 'Legendary', 'Epic', 'Rare', 'Uncommon', 'Common').
        make: Optional manufacturer filter (e.g., 'Porsche', 'Bugatti', 'Ferrari', 'Pagani', 'McLaren').

    Returns:
        A list of car entries with make, model, trim, rarity tier, points, and global spotted counts.
    """
    return list_catalog_cars(make=make, rarity_tier=rarity_tier, limit=20)


def record_car_spot(
    car_name_or_id: str,
    colorway: str = "Standard",
    location: str = "Public Road",
    notes: str = "",
    user_id: str = "spotter_1",
    image_url: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> dict[str, Any]:
    """Record a newly spotted car in public to the user's garage and update the global spot count.

    This awards points based on the vehicle's rarity tier, increments the Shazam-style global
    spotted count, and updates the spotter leaderboard.
    Coordinates are quantized to ~500m geohash to protect privacy.

    Args:
        car_name_or_id: The car's name, make/model, or doc ID (e.g. 'Ferrari Daytona SP3').
        colorway: The paint color or livery observed (e.g. 'Rosso Corsa', 'Naked Carbon').
        location: Where the car was spotted (e.g. 'Rodeo Drive, Beverly Hills', 'Monaco Harbour').
        notes: Personal observations or custom modifications noted.
        user_id: The ID of the spotter user (defaults to 'spotter_1').
        image_url: Optional public HTTPS URL of the spotted car image stored in Cloud Storage.
        latitude: Optional GPS latitude (will be fuzz-quantized before persisting).
        longitude: Optional GPS longitude (will be fuzz-quantized before persisting).

    Returns:
        A dictionary confirming the spot, points earned, new global spotted count, and spot ID.
    """
    res = record_car_spot_entry(
        car_id=car_name_or_id,
        colorway=colorway,
        user_id=user_id,
        location=location,
        notes=notes,
        image_url=image_url,
        latitude=latitude,
        longitude=longitude,
    )
    if latitude and longitude:
        try:
            from .cluster_beacon import record_spot_cluster_check
            meet = record_spot_cluster_check({
                "id": res.get("spot_id"),
                "user_id": user_id,
                "latitude": latitude,
                "longitude": longitude,
                "car_name": car_name_or_id,
                "spot_timestamp": res.get("spotted_at"),
            })
            if meet:
                res["car_meet_detected"] = True
                res["meet_cluster_id"] = meet.get("id")
        except Exception as e:
            logger.debug("Cluster meet check error: %s", e)
    return res


def view_my_garage(user_id: str = "spotter_1") -> list[dict[str, Any]]:
    """Retrieve all cars spotted and collected by the spotter in their personal garage / CarDex.

    Args:
        user_id: The user ID to retrieve the garage for (defaults to 'spotter_1').

    Returns:
        A list of all spotted car records with timestamps, points awarded, and locations.
    """
    return get_user_garage_spots(user_id=user_id)


def view_leaderboard(limit: int = 10) -> list[dict[str, Any]]:
    """View the CarDex spotter leaderboard rankings.

    Args:
        limit: Number of top spotters to display (defaults to 10).

    Returns:
        A list of top-ranked spotters with their username, rank, total points, and rarest spot.
    """
    return get_leaderboard_rankings(limit=limit)


def submit_car_review(
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
    """Submit a vehicle identification review or dispute when the AI or database has an error.

    Allows spotters to dispute an incorrect trim, wrong colorway, or report unlisted 1-of-1 builds.
    Routes to the Master Spotter adjudication queue for community consensus voting.

    Args:
        car_name: The name or model of the vehicle in question.
        issue_description: What went wrong or was misidentified.
        proposed_correction: The correct information or provenance.
        user_id: The user submitting the review (defaults to 'spotter_1').
        spot_id: Optional ID of the specific spot being contested.
        image_url: Optional image of the car being disputed.
        proposed_make: Correct manufacturer if misclassified.
        proposed_model: Correct model if misclassified.
        proposed_trim: Correct trim or special edition package.
        proposed_color: Correct paint color or OEM finish.

    Returns:
        A dictionary with the submission status and review ID.
    """
    res = submit_dispute_review(
        car_name=car_name,
        issue_description=issue_description,
        proposed_correction=proposed_correction,
        user_id=user_id,
        spot_id=spot_id,
        image_url=image_url,
        proposed_make=proposed_make,
        proposed_model=proposed_model,
        proposed_trim=proposed_trim,
        proposed_color=proposed_color,
    )
    res_dict = dict(res)
    res_dict["status"] = "pending_review"
    return res_dict


def decode_vin_specifications(vin: str) -> dict[str, Any]:
    """Decode a 17-character Vehicle Identification Number (VIN) using the official US NHTSA database.

    Fetches verified manufacturer specs, engine size, cylinder count, horsepower, body style,
    and assembly plant country. Useful when spotters provide a VIN or want to verify rare builds.

    Args:
        vin: The 17-character Vehicle Identification Number (e.g. 'WP0AB2A91PS256789').

    Returns:
        A dictionary with decoded vehicle specifications (make, model, year, trim, engine, etc.)
        or validation error details.
    """
    vin_clean = vin.strip().upper()
    if len(vin_clean) != 17:
        return {
            "valid": False,
            "vin": vin_clean,
            "error": f"Invalid VIN length: VIN must be exactly 17 characters (received {len(vin_clean)}: '{vin_clean}').",
        }

    url = f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{urllib.parse.quote(vin_clean)}?format=json"
    req = urllib.request.Request(url, headers={"User-Agent": "CarDex-Spotter/1.0"})

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("Results", [{}])[0]

            make = results.get("Make", "").strip()
            model = results.get("Model", "").strip()
            year = results.get("ModelYear", "").strip()

            if not make and not model:
                return {
                    "valid": False,
                    "vin": vin_clean,
                    "error": f"No vehicle record found for VIN '{vin_clean}'. Error: {results.get('ErrorText', 'Unknown')}",
                }

            return {
                "valid": True,
                "vin": vin_clean,
                "make": make,
                "model": model,
                "year": int(year) if year.isdigit() else year,
                "trim": results.get("Trim", "").strip() or results.get("Series", "").strip(),
                "body_class": results.get("BodyClass", "").strip(),
                "vehicle_type": results.get("VehicleType", "").strip(),
                "doors": results.get("Doors", "").strip(),
                "drive_type": results.get("DriveType", "").strip(),
                "engine_cylinders": results.get("EngineCylinders", "").strip(),
                "engine_displacement_liters": results.get("DisplacementL", "").strip(),
                "engine_horsepower": results.get("EngineHP", "").strip(),
                "fuel_type_primary": results.get("FuelTypePrimary", "").strip(),
                "plant_country": results.get("PlantCountry", "").strip(),
                "manufacturer": results.get("Manufacturer", "").strip(),
            }
    except Exception as e:
        return {"valid": False, "vin": vin_clean, "error": f"NHTSA API request failed: {str(e)}"}


# Regex patterns for macro badge, special edition, and VIN extraction
PRODUCTION_NUMBER_REGEX = re.compile(r"(\d{1,4}\s*(?:of|\/)\s*\d{1,4})", re.IGNORECASE)
SPECIAL_EDITION_REGEX = re.compile(
    r"\b(Weissach|Touring|Black\s+Series|GTO|Sonderwunsch|Tailor\s+Made)\b",
    re.IGNORECASE,
)
VIN_PATTERN_REGEX = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b")

SPECIAL_EDITION_CANONICAL = {
    "weissach": "Weissach",
    "touring": "Touring",
    "black series": "Black Series",
    "gto": "GTO",
    "sonderwunsch": "Sonderwunsch",
    "tailor made": "Tailor Made",
}


def extract_chassis_markings(
    image_bytes: bytes | str,
    user_id: str = "spotter_1",
    vehicle_hint: str = "",
) -> dict[str, Any]:
    r"""Inspect an automotive image for macro badges, limited edition plaques, and VIN placards.

    1. Macro Badge Cropping & OCR:
       - Instructs Gemini Vision to run a localized high-resolution crop on visible badges,
         steering wheel center consoles, kickplates, or lower-windshield VIN placards.
       - Extracts text strings matching regex patterns:
         - Production numbers: r"(\d{1,4}\s*(?:of|\/)\s*\d{1,4})" (e.g. '1 of 500', '042/600')
         - Heritage/Special editions: 'Weissach', 'Touring', 'Black Series', 'GTO', 'Sonderwunsch', 'Tailor Made'
         - Standard 17-character VIN patterns: r"[A-HJ-NPR-Z0-9]{17}"
    2. Chassis Verification Lookup:
       - If a VIN is extracted, calls decode_vin_specifications directly to verify manufacturer registry data.
       - If a numbered edition plaque is found, writes it to a new Firestore collection chassis_registry/{make_model_edition}:
         - Record: {"unit_number": "042/600", "first_spotted_by": userId, "verified_at": timestamp}
         - Awards user permanent "Chassis Archivist" badge and applies 2.5x First-Finder discovery multiplier.

    Args:
        image_bytes: Raw JPEG/PNG image bytes, base64 data URI string, or base64 string.
        user_id: Identifier of the spotter discovering the vehicle.
        vehicle_hint: Optional vehicle make/model name for contextual registry key construction.

    Returns:
        A dictionary with extracted markings, VIN specifications, chassis registry status,
        awarded badges, and score multiplier.
    """
    raw_bytes = None
    if isinstance(image_bytes, str):
        cleaned = image_bytes.strip()
        if "," in cleaned and "base64" in cleaned:
            cleaned = cleaned.split(",", 1)[1]
        try:
            raw_bytes = base64.b64decode(cleaned)
        except Exception:
            raw_bytes = image_bytes.encode("utf-8")
    elif isinstance(image_bytes, bytes):
        raw_bytes = image_bytes
    else:
        raw_bytes = b""

    extracted_text = ""
    detected_markings: list[str] = []
    gemini_data: dict[str, Any] = {}
    raw_response_text = ""

    # 1. Query Gemini Vision with localized macro cropping & OCR prompt
    try:
        genai_client = genai.Client(enterprise=True, project=VERTEX_PROJECT_ID, location=VERTEX_LOCATION)
        prompt = (
            "You are CarDex's macro badge, VIN, and chassis marking OCR specialist.\n"
            "Instructed task: Run localized high-resolution inspection and OCR on any visible badges, "
            "steering wheel center consoles, door sill kickplates, glovebox plaques, or lower-windshield VIN placards.\n"
            "Extract all visible inscriptions, limited edition plaques, special edition badges, or 17-character VIN placards.\n"
            "Return a valid JSON object matching:\n"
            "{\n"
            '  "extracted_text": "all raw text strings visible on badges, plaques, kickplates, or VIN placards",\n'
            '  "production_number": "any limited edition numbered string (e.g. \'042/600\', \'1 of 500\'), or null",\n'
            '  "edition": "heritage/special edition name (\'Weissach\', \'Touring\', \'Black Series\', \'GTO\', \'Sonderwunsch\', \'Tailor Made\'), or null",\n'
            '  "vin": "any 17-character VIN string found, or null",\n'
            '  "location_observed": "where the markings were detected (e.g. \'Center console plaque\', \'Kickplate\', \'Lower windshield\')",\n'
            '  "make_model_hint": "make and model from badge if visible"\n'
            "}\n"
            "Return ONLY raw JSON, with no markdown backticks."
        )

        resp = genai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_bytes(data=raw_bytes, mime_type="image/jpeg"),
                prompt,
            ],
        )
        if resp and resp.text:
            raw_response_text = resp.text.strip()
            clean_json = raw_response_text
            if clean_json.startswith("```"):
                lines = clean_json.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                clean_json = "\n".join(lines).strip()
            try:
                gemini_data = json.loads(clean_json)
                extracted_text = gemini_data.get("extracted_text", "")
            except Exception:
                extracted_text = raw_response_text
    except Exception as e:
        logger.warning("Gemini Vision chassis OCR inspection error: %s", e)

    # 2. Comprehensive Regex Extraction
    search_corpus = f"{extracted_text} {gemini_data.get('production_number', '')} {gemini_data.get('edition', '')} {gemini_data.get('vin', '')} {raw_response_text}"

    # 2a. Production numbers: r"(\d{1,4}\s*(?:of|\/)\s*\d{1,4})" (e.g. '1 of 500', '042/600')
    production_number = None
    prod_match = PRODUCTION_NUMBER_REGEX.search(search_corpus)
    if prod_match:
        production_number = prod_match.group(1).strip()
        detected_markings.append(f"Production Plaque: {production_number}")

    # 2b. Heritage/Special editions: "Weissach", "Touring", "Black Series", "GTO", "Sonderwunsch", "Tailor Made"
    edition = None
    edition_match = SPECIAL_EDITION_REGEX.search(search_corpus)
    if edition_match:
        matched_str = edition_match.group(1).strip()
        norm_key = re.sub(r"\s+", " ", matched_str.lower())
        edition = SPECIAL_EDITION_CANONICAL.get(norm_key, matched_str)
        detected_markings.append(f"Special Edition: {edition}")

    # 2c. Standard 17-character VIN patterns: r"[A-HJ-NPR-Z0-9]{17}"
    vin = None
    vin_match = VIN_PATTERN_REGEX.search(search_corpus)
    if vin_match:
        vin = vin_match.group(1).strip().upper()
        detected_markings.append(f"VIN: {vin}")

    # 3. Chassis Verification Lookup:
    # 3a. If a VIN is extracted, call decode_vin_specifications directly to verify manufacturer registry data
    vin_decoded = None
    if vin:
        vin_decoded = decode_vin_specifications(vin)

    # 3b. Determine make_model_edition
    make_model_edition = ""
    if vehicle_hint:
        if edition and edition.lower() not in vehicle_hint.lower():
            make_model_edition = f"{vehicle_hint} {edition}".strip()
        else:
            make_model_edition = vehicle_hint.strip()
    elif vin_decoded and vin_decoded.get("valid"):
        vm = f"{vin_decoded.get('make', '')} {vin_decoded.get('model', '')}".strip()
        make_model_edition = f"{vm} {edition or ''}".strip()
    elif edition:
        make_model_edition = f"Special Edition {edition}".strip()
    elif production_number:
        make_model_edition = f"Numbered Edition {production_number}".strip()
    else:
        make_model_edition = "Unverified Chassis"

    # 3c. If a numbered edition plaque is found, write it to a new Firestore collection chassis_registry/{make_model_edition}:
    # Record: {"unit_number": "042/600", "first_spotted_by": userId, "verified_at": timestamp}
    # Award the user a permanent "Chassis Archivist" badge and apply the 2.5x First-Finder discovery multiplier
    registry_entry = None
    badge_awarded = None
    first_finder_multiplier = 1.0

    if production_number:
        registry_entry = record_chassis_registry_entry(
            make_model_edition=make_model_edition,
            unit_number=production_number,
            user_id=user_id,
            extra_data={
                "edition": edition,
                "vin": vin,
                "location_observed": gemini_data.get("location_observed"),
            } if (edition or vin or gemini_data.get("location_observed")) else None,
        )
        award_user_badge(user_id, "Chassis Archivist")
        badge_awarded = "Chassis Archivist"
        first_finder_multiplier = 2.5

    return {
        "success": True,
        "vin": vin,
        "vin_decoded": vin_decoded,
        "production_number": production_number,
        "edition": edition,
        "make_model_edition": make_model_edition,
        "raw_text": extracted_text,
        "detected_markings": detected_markings,
        "chassis_verified": bool(vin or production_number),
        "badge_awarded": badge_awarded,
        "first_finder_multiplier": first_finder_multiplier,
        "registry_entry": registry_entry,
    }


def search_vehicle_specs_database(
    query: str,
    make: str | None = None,
    min_hp: int | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Search the comprehensive automotive database of 164 makes, 2,649 models, and 30,535 engine configurations.

    Use this to look up detailed factory performance data, horsepower, top speed (km/h), 0-100 km/h acceleration,
    torque (Nm), engine cylinders, displacement, transmission, and drivetrain for ANY production or special vehicle.

    Args:
        query: Freeform car name or keyword (e.g. 'Skyline GT-R', 'Supra', 'Aventador', 'V12', 'Hellcat').
        make: Optional specific make to filter by (e.g. 'Ferrari', 'Nissan', 'Porsche').
        min_hp: Optional minimum horsepower filter (e.g. 500 for high-performance cars).
        limit: Maximum number of results to return (default 5).

    Returns:
        A list of verified vehicle entries with power, speed, zero-to-hundred, torque, and dimensions.
    """
    return search_local_vehicle_database(query=query, make=make, min_hp=min_hp, limit=limit)


def lookup_nhtsa_models_by_year(make: str, model_year: int) -> dict[str, Any]:
    """Query official US Federal NHTSA vPIC database for all registered models by make and year.

    This is a 100% free, uncapped, federal government API with no rate limits or paywalls.
    Supports all production years from 1980 through 2026/2027.

    Args:
        make: Manufacturer name (e.g. 'Ferrari', 'Porsche', 'Bugatti', 'Ford', 'Toyota').
        model_year: 4-digit model year (e.g. 2024, 2025).

    Returns:
        A list of officially registered model names and make identifiers from the federal database.
    """
    make_clean = make.strip()
    url = f"https://vpic.nhtsa.dot.gov/api/vehicles/getmodelsformakeyear/make/{urllib.parse.quote(make_clean)}/modelyear/{model_year}?format=json"
    req = urllib.request.Request(url, headers={"User-Agent": "CarDex/1.0 (cardex@example.com)"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("Results", [])
            models = sorted(list({item["Model_Name"].strip() for item in results if item.get("Model_Name")}))
            return {
                "source": "US NHTSA Federal vPIC API",
                "make": make_clean,
                "model_year": model_year,
                "count": len(models),
                "models": models,
            }
    except Exception as e:
        return {
            "source": "US NHTSA Federal vPIC API",
            "make": make_clean,
            "model_year": model_year,
            "error": f"Failed to retrieve NHTSA models: {str(e)}",
        }


def fetch_carapi_live_specs(
    make: str,
    model: str,
    year: int = 2019,
) -> dict[str, Any]:
    """Fetch live vehicle trims, MSRP pricing, and OEM exterior paint colorways from CarAPI.

    Reads API credentials from environment variables ('CARAPI_KEY' or 'CARAPI_TOKEN', and 'CARAPI_SECRET')
    instead of hardcoding. If keys are present, authenticates to unlock all years (1990-2027);
    otherwise queries the public tier (2015-2020) with graceful fallback.

    Args:
        make: Vehicle manufacturer (e.g. 'Ferrari', 'Porsche', 'McLaren', 'BMW').
        model: Vehicle model name (e.g. '488 GTB', '911', '720S', 'M3').
        year: Model year (e.g. 2018, 2019). Defaults to 2019 on free tier.

    Returns:
        A dictionary containing real-world trims, factory MSRP/invoice prices, and official paint color names with RGB codes.
    """
    api_token = os.environ.get("CARAPI_TOKEN") or os.environ.get("CARAPI_KEY")
    api_secret = os.environ.get("CARAPI_SECRET")
    jwt_token: str | None = None

    # Authenticate if environment variables are provided
    if api_token and api_secret:
        auth_url = "https://carapi.app/api/auth/login"
        auth_payload = json.dumps({"api_token": api_token.strip(), "api_secret": api_secret.strip()}).encode("utf-8")
        auth_req = urllib.request.Request(
            auth_url,
            data=auth_payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CarDex/1.0",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(auth_req, timeout=5) as auth_resp:
                jwt_token = auth_resp.read().decode("utf-8").strip('"')
        except Exception:
            jwt_token = None

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CarDex/1.0",
        "Accept": "application/json",
    }
    if jwt_token:
        headers["Authorization"] = f"Bearer {jwt_token}"

    make_clean = make.strip()
    model_clean = model.strip()

    # Query Trims & Pricing
    trims_url = f"https://carapi.app/api/trims/v2?{urllib.parse.urlencode({'make': make_clean, 'model': model_clean, 'year': year, 'limit': 3})}"
    trims_req = urllib.request.Request(trims_url, headers=headers)

    # Query Exterior Colors & RGB
    colors_url = f"https://carapi.app/api/exterior-colors/v2?{urllib.parse.urlencode({'make': make_clean, 'model': model_clean, 'year': year, 'limit': 5})}"
    colors_req = urllib.request.Request(colors_url, headers=headers)

    trims_result = []
    colors_result = []

    try:
        with urllib.request.urlopen(trims_req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for t in data.get("data", []):
                trims_result.append({
                    "trim": t.get("trim"),
                    "description": t.get("description"),
                    "msrp": t.get("msrp"),
                    "invoice": t.get("invoice"),
                })
    except Exception as e:
        trims_error = str(e)
    else:
        trims_error = None

    try:
        with urllib.request.urlopen(colors_req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for c in data.get("data", []):
                colors_result.append({
                    "color_name": c.get("color"),
                    "rgb": c.get("rgb"),
                })
    except Exception:
        pass

    return {
        "source": "CarAPI Live Service",
        "authenticated": bool(jwt_token),
        "make": make_clean,
        "model": model_clean,
        "year": year,
        "trims": trims_result,
        "exterior_colors": colors_result,
        "error": trims_error,
        "auth_note": (
            "Premium mode active via environment variables."
            if jwt_token
            else "Free tier active (restricted to 2015-2020). Set CARAPI_KEY and CARAPI_SECRET in environment to unlock all years."
        ),
    }


def fetch_car_image_and_provenance(car_name: str) -> dict[str, Any]:
    """Retrieve verified high-resolution vehicle photos, thumbnail URLs, and encyclopedic provenance.

    A 100% free and uncapped tool backed by the Wikimedia REST API, providing real images
    suitable for displaying in the spotter UI or storing in Cloud Storage.

    Args:
        car_name: Name of the vehicle (e.g. 'Bugatti Bolide', 'McLaren Senna', 'Lexus LFA', 'Porsche 918').

    Returns:
        A dictionary containing real image URLs (high-res and thumbnail), descriptions, and encyclopedic summary.
    """
    clean_title = car_name.strip().replace(" ", "_")
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(clean_title)}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "CarDex/1.0 (cardex-spotter@example.com)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            thumb = data.get("thumbnail", {}).get("source")
            original = data.get("originalimage", {}).get("source")
            return {
                "source": "Wikimedia Open Automotive Archive",
                "car_name": data.get("title", car_name),
                "description": data.get("description"),
                "summary": data.get("extract"),
                "thumbnail_url": thumb,
                "high_res_image_url": original or thumb,
                "wikipedia_page": data.get("content_urls", {}).get("desktop", {}).get("page"),
            }
    except Exception as e:
        return {
            "source": "Wikimedia Open Automotive Archive",
            "car_name": car_name,
            "error": f"Failed to fetch media: {str(e)}",
        }


def identify_and_spot_car(
    image_input: str,
    location: str = "Public Road",
    user_id: str = "spotter_1",
    notes: str = "",
    auto_log_spot: bool = True,
    latitude: float | None = None,
    longitude: float | None = None,
    audio_input: str | bytes | None = None,
) -> dict[str, Any]:
    """Recognize a vehicle from an image using Gemini Multimodal Vision, with anti-cheat, colorimetry, and dynamic deflationary scoring.

    1. Enforces daily quota (5 scans/day on free tier, consumable refills, Pro tier).
    2. Runs 2D FFT Moiré screen detection and 64-bit DCT pHash duplicate detection.
    3. Multimodal vision recognition via Gemini 2.5 Flash in global region.
    4. Spectrometric CIEDE2000 color matching against factory OEM Paint to Sample (PTS) database.
    5. Acoustic engine note classification and verification (+25% point bonus upon matching cylinder cadence).
    6. Calculates dynamic deflationary score based on production run N_prod, 30d encounter decay D_30, paint finish M_paint, and first-finder bonus B_first.
    7. Logs the spot in Firestore and commits quota deduction only upon success.
    """
    # 0. Quota check before invoking AI models
    allowed, reason, quota_info = check_and_reserve_scan(user_id)
    if not allowed:
        return {
            "success": False,
            "quota_exceeded": True,
            "error": reason,
            "quota": quota_info,
        }

    raw_input = image_input.strip()
    image_bytes: bytes | None = None
    mime_type = "image/jpeg"

    # 1. Decode or download in-memory (DO NOT write to local file)
    try:
        if raw_input.startswith("http://") or raw_input.startswith("https://"):
            req = urllib.request.Request(
                raw_input,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CarDex/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                image_bytes = resp.read()
                content_type_header = resp.headers.get_content_type()
                if content_type_header and "/" in content_type_header:
                    mime_type = content_type_header

        elif raw_input.startswith("gs://"):
            parts = raw_input[5:].split("/", 1)
            if len(parts) == 2:
                src_bucket, src_blob = parts
                st_client = storage.Client(project=VERTEX_PROJECT_ID)
                b = st_client.bucket(src_bucket)
                bl = b.blob(src_blob)
                image_bytes = bl.download_as_bytes()
                if bl.content_type:
                    mime_type = bl.content_type
            else:
                return {"success": False, "error": f"Invalid GCS URI: {image_input}"}

        else:
            # Assume base64 string
            b64_data = raw_input
            if "," in b64_data and "base64" in b64_data:
                header, b64_data = b64_data.split(",", 1)
                if "image/png" in header:
                    mime_type = "image/png"
                elif "image/webp" in header:
                    mime_type = "image/webp"
                elif "image/jpeg" in header or "image/jpg" in header:
                    mime_type = "image/jpeg"
            image_bytes = base64.b64decode(b64_data)

    except Exception as e:
        return {"success": False, "error": f"Failed to read image input: {str(e)}"}

    if not image_bytes:
        return {"success": False, "error": "Empty or invalid image data provided."}

    # 2. Anti-Cheat & Forensic Verification (Moiré screen detection + pHash duplicate check)
    integrity = verify_image_integrity(image_bytes, user_id=user_id)
    if not integrity["passed"]:
        return {
            "success": False,
            "anti_cheat_triggered": True,
            "error": integrity["message"],
            "flag": integrity["flag"],
            "phash": integrity.get("phash"),
            "forensics": integrity.get("forensics"),
        }
    phash = integrity.get("phash")

    # 3. Upload in-memory to Google Cloud Storage
    ext = "png" if "png" in mime_type else ("webp" if "webp" in mime_type else "jpg")
    blob_name = f"spots/{uuid.uuid4().hex[:16]}.{ext}"

    try:
        st_client = storage.Client(project=VERTEX_PROJECT_ID)
        bucket = st_client.bucket(CLOUD_STORAGE_BUCKET)
        blob = bucket.blob(blob_name)
        blob.upload_from_string(image_bytes, content_type=mime_type)
        public_image_url = f"https://storage.googleapis.com/{CLOUD_STORAGE_BUCKET}/{blob_name}"
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to upload image to Cloud Storage bucket '{CLOUD_STORAGE_BUCKET}': {str(e)}",
        }

    # 4. Multimodal vision recognition using Gemini in global region
    try:
        genai_client = genai.Client(enterprise=True, project=VERTEX_PROJECT_ID, location=VERTEX_LOCATION)
        prompt = """You are CarDex's expert automotive spotter AI. Analyze this vehicle image in thorough detail.
Return a valid JSON object with the following fields:
- make (string, e.g. 'McLaren', 'Ferrari', 'Bugatti', 'Porsche')
- model (string, e.g. 'Senna', 'Daytona SP3', 'Bolide', '911 GT3 RS')
- generation_or_year (string, e.g. '2023', '992', 'Type 18.4')
- trim (string, e.g. 'Weissach Package', 'Coupe', 'Carbon Edition')
- observed_colorway (string, e.g. 'Papaya Orange', 'Rosso Corsa', 'Naked Carbon with Blue Accents')
- is_special_edition_or_one_of_one (boolean)
- special_edition_name (string or null)
- confidence (string: 'High', 'Medium', or 'Low')
- key_identifying_features (list of strings highlighting aero, headlights, exhausts, badging)
- estimated_rarity_tier (string: 'Common', 'Rare', 'Epic', 'Legendary', 'Mythic 1-of-1')
- estimated_points (integer)

Return ONLY the raw JSON object, without markdown formatting or code fences."""

        response = genai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                prompt,
            ],
        )

        resp_text = response.text.strip()
        if resp_text.startswith("```"):
            lines = resp_text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            resp_text = "\n".join(lines).strip()

        vision_data = json.loads(resp_text)
    except Exception as e:
        return {
            "success": False,
            "image_public_url": public_image_url,
            "error": f"Gemini multimodal vision analysis failed: {str(e)}",
        }

    detected_make = vision_data.get("make", "").strip()
    detected_model = vision_data.get("model", "").strip()
    detected_trim = vision_data.get("trim", "").strip()
    query_name = f"{detected_make} {detected_model}".strip()

    # 5. Spectrometric Colorway & CIEDE2000 Paint Matcher
    paint_match = match_oem_paint_color(image_bytes, make_hint=detected_make)
    is_pts = paint_match.get("is_pts", False)
    is_carbon = "carbon" in vision_data.get("observed_colorway", "").lower()

    if paint_match.get("matched"):
        resolved_colorway = f"{paint_match['oem_brand']} {paint_match['paint_name']}"
    else:
        resolved_colorway = vision_data.get("observed_colorway", "Standard")

    # 5.5 Multimodal Acoustic Engine Note Classification
    audio_analysis = None
    acoustic_multiplier = 1.0
    if audio_input:
        try:
            audio_bytes = None
            audio_mime = "audio/wav"
            if isinstance(audio_input, str):
                raw_audio = audio_input.strip()
                if "," in raw_audio and "base64" in raw_audio:
                    header, b64_str = raw_audio.split(",", 1)
                    if "webm" in header:
                        audio_mime = "audio/webm"
                    elif "mp3" in header:
                        audio_mime = "audio/mp3"
                    elif "aac" in header:
                        audio_mime = "audio/aac"
                    elif "ogg" in header:
                        audio_mime = "audio/ogg"
                    elif "wav" in header:
                        audio_mime = "audio/wav"
                    audio_bytes = base64.b64decode(b64_str)
                else:
                    try:
                        audio_bytes = base64.b64decode(raw_audio)
                    except Exception:
                        pass
            elif isinstance(audio_input, bytes):
                audio_bytes = audio_input

            if audio_bytes:
                audio_analysis = classify_engine_note(
                    audio_bytes=audio_bytes,
                    claimed_model=query_name,
                    mime_type=audio_mime,
                )
        except Exception as e:
            logger.warning("Acoustic analysis failed in identify_and_spot_car: %s", e)

    # 5.6 Macro Badge, Limited Edition Plaque & VIN Extraction
    chassis_markings = None
    try:
        chassis_markings = extract_chassis_markings(
            image_bytes=image_bytes,
            user_id=user_id,
            vehicle_hint=query_name,
        )
    except Exception as e:
        logger.warning("Chassis marking extraction failed in identify_and_spot_car: %s", e)

    # 6. Dynamic Rarity & Deflationary Scoring Engine
    car_slug = query_name.lower().replace(" ", "-")
    first_finder_override = None
    first_finder_label_override = None
    if chassis_markings and chassis_markings.get("production_number"):
        first_finder_override = 2.5
        first_finder_label_override = (
            f"Chassis Archivist Discovery ({chassis_markings['production_number']}) (2.5x Multiplier)"
        )

    score_info = compute_dynamic_spot_score(
        make=detected_make,
        model=detected_model,
        trim=detected_trim,
        car_identifier=car_slug,
        is_pts=is_pts,
        is_bespoke_or_carbon=is_carbon,
        location=location,
        first_finder_override=first_finder_override,
        first_finder_label_override=first_finder_label_override,
    )
    final_points = score_info["final_points"]
    rarity_tier = score_info["rarity_tier"]

    if chassis_markings and chassis_markings.get("production_number"):
        score_info["breakdown"]["chassis_archivist"] = {
            "applied": True,
            "badge": "Chassis Archivist",
            "unit_number": chassis_markings.get("production_number"),
            "edition": chassis_markings.get("edition"),
            "multiplier": 2.5,
        }

    # Apply Acoustic Verification bonus (+25% point bonus) if acoustic_match is True
    if audio_analysis and audio_analysis.get("acoustic_match"):
        acoustic_multiplier = 1.25
        final_points = int(round(final_points * 1.25))
        score_info["final_points"] = final_points
        score_info["breakdown"]["acoustic_verification"] = {
            "applied": True,
            "multiplier": 1.25,
            "engine_config": audio_analysis.get("engine_config"),
            "confidence": audio_analysis.get("confidence"),
            "peak_loudness_dbfs": audio_analysis.get("peak_loudness_dbfs"),
            "rev_limiter": audio_analysis.get("rev_limiter_detected"),
            "firing_frequency_hz": audio_analysis.get("firing_frequency_hz"),
            "signature": audio_analysis.get("acoustic_signature"),
            "bonus_points": int(round(final_points - (final_points / 1.25))),
        }

    # 7. Optionally record spot in Firestore
    spot_info = None
    if auto_log_spot:
        catalog_entry = get_car_by_id_or_name(query_name)
        car_identifier = catalog_entry.get("id") if catalog_entry else car_slug
        spot_notes = notes or f"Spotted via AI Vision: {', '.join(vision_data.get('key_identifying_features', [])[:2])}"
        if audio_analysis and audio_analysis.get("acoustic_match"):
            spot_notes += f" [Acoustic Verified: {audio_analysis.get('engine_config')} ({audio_analysis.get('peak_loudness_dbfs')} dBFS) +25% Bonus]"
        if chassis_markings and chassis_markings.get("production_number"):
            spot_notes += f" [Chassis #{chassis_markings.get('production_number')} Verified - Chassis Archivist]"
        if chassis_markings and chassis_markings.get("vin"):
            spot_notes += f" [VIN: {chassis_markings.get('vin')}]"

        spot_info = record_car_spot_entry(
            car_id=car_identifier,
            colorway=resolved_colorway,
            user_id=user_id,
            location=location,
            notes=spot_notes,
            image_url=public_image_url,
            points_override=final_points,
            rarity_override=rarity_tier,
            phash=phash,
            points_breakdown=score_info["breakdown"],
            paint_badge=paint_match.get("badge_text"),
            latitude=latitude,
            longitude=longitude,
        )

    # 8. Commit quota deduction only on successful vehicle identification
    updated_quota = commit_scan_deduction(user_id)

    # 9. Dynamic A2UI Cards
    a2ui_cards = []
    de00 = paint_match.get("delta_e00", 999.0)
    if de00 <= 2.0 and paint_match.get("matched"):
        rgb = paint_match.get("rgb", [58, 28, 68])
        hex_color = f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
        paint_card = build_paint_match_card(
            paint_code=paint_match.get("paint_code", ""),
            commercial_name=paint_match.get("paint_name", ""),
            program=paint_match.get("program", "Factory"),
            hex_color=hex_color,
            delta_e=de00,
            multiplier=paint_match.get("multiplier", 1.35),
        )
        a2ui_cards.append(paint_card)

    if audio_analysis and audio_analysis.get("acoustic_match"):
        acoustic_card = build_acoustic_verification_card(
            engine_config=audio_analysis.get("engine_config", "Unknown"),
            confidence=audio_analysis.get("confidence", 0.9),
            loudness_dbfs=audio_analysis.get("peak_loudness_dbfs", -12.0),
            signature=audio_analysis.get("acoustic_signature", ""),
            rev_limiter=audio_analysis.get("rev_limiter_detected", False),
            bonus_multiplier=1.25,
        )
        a2ui_cards.append(acoustic_card)

    if chassis_markings and (chassis_markings.get("production_number") or chassis_markings.get("vin")):
        chassis_card = build_chassis_verification_card(
            make_model_edition=chassis_markings.get("make_model_edition") or query_name,
            unit_number=chassis_markings.get("production_number"),
            vin=chassis_markings.get("vin"),
            edition=chassis_markings.get("edition"),
            vin_decoded=chassis_markings.get("vin_decoded"),
            badge_awarded=chassis_markings.get("badge_awarded"),
            first_finder_multiplier=chassis_markings.get("first_finder_multiplier", 1.0),
        )
        a2ui_cards.append(chassis_card)

    breakdown_card = build_point_breakdown_card(
        car_name=query_name,
        base_points=score_info["base_points"],
        production_units=score_info.get("production_run", 1_000_000),
        recent_spots_30d=score_info.get("recent_spots_30d", 0),
        final_points=final_points,
        paint_multiplier=score_info.get("paint_multiplier", 1.0),
        first_spotter_bonus=score_info.get("first_spotter_bonus", 1.0),
        decay_factor=score_info.get("decay_factor", 1.0),
        rarity_tier=rarity_tier,
        acoustic_bonus=acoustic_multiplier,
        acoustic_engine=audio_analysis.get("engine_config") if audio_analysis else None,
    )
    a2ui_cards.append(breakdown_card)

    return {
        "success": True,
        "image_public_url": public_image_url,
        "vision_identification": vision_data,
        "color_analysis": paint_match,
        "acoustic_analysis": audio_analysis,
        "chassis_markings": chassis_markings,
        "dynamic_scoring": score_info,
        "rarity_tier": rarity_tier,
        "points_awarded": final_points,
        "spot_logged": bool(spot_info),
        "spot_details": spot_info,
        "quota": updated_quota,
        "phash": phash,
        "a2ui_cards": a2ui_cards,
    }




