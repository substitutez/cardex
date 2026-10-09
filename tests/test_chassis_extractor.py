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

"""Unit and integration tests for Macro Badge & VIN Extraction Pipeline."""

import json
from unittest.mock import MagicMock, patch
import pytest

from app.car_tools import (
    PRODUCTION_NUMBER_REGEX,
    SPECIAL_EDITION_REGEX,
    VIN_PATTERN_REGEX,
    extract_chassis_markings,
    decode_vin_specifications,
    identify_and_spot_car,
)
from app.agent import cardex_agent, root_agent


# 1. Regex Pattern Unit Tests
def test_production_number_regex_matches():
    """Verify production number regex captures various standard format plaques."""
    test_cases = [
        ("Limited edition plaque No. 042/600 on console", "042/600"),
        ("Center console badge stamped '1 of 500'", "1 of 500"),
        ("Carbon kickplate inscribed: 001 of 100", "001 of 100"),
        ("Steering wheel plaque 77 / 77 limited run", "77 / 77"),
        ("Plaque 5/25", "5/25"),
    ]
    for text, expected in test_cases:
        match = PRODUCTION_NUMBER_REGEX.search(text)
        assert match is not None, f"Failed to match: {text}"
        assert match.group(1).strip() == expected


def test_special_editions_regex_matches():
    """Verify heritage and special edition regex matches exact designations."""
    editions = [
        ("Porsche 911 GT3 RS Weissach Package", "Weissach"),
        ("911 GT3 Touring Edition", "Touring"),
        ("Mercedes-AMG GT Black Series", "Black Series"),
        ("Ferrari 288 GTO", "GTO"),
        ("Porsche Exclusive Manufaktur Sonderwunsch", "Sonderwunsch"),
        ("Ferrari Tailor Made bespoke specification", "Tailor Made"),
    ]
    for text, expected in editions:
        match = SPECIAL_EDITION_REGEX.search(text)
        assert match is not None, f"Failed to match edition: {text}"
        assert match.group(1).strip().lower() == expected.lower()


def test_vin_regex_matches():
    """Verify 17-character VIN pattern matching."""
    valid_vins = [
        "WP0AB2A91PS256789",
        "1FA6P8CF5H5123456",
        "ZHWUA19S1CLA01234",
    ]
    for vin in valid_vins:
        text = f"Lower windshield placard shows VIN: {vin} in clear font"
        match = VIN_PATTERN_REGEX.search(text)
        assert match is not None, f"Failed to match VIN: {vin}"
        assert match.group(1).strip() == vin

    # Invalid lengths or letters
    invalid_texts = [
        "VIN: WP0AB2A91P",  # too short
        "VIN: WP0AB2A91PS25678901234",  # too long
    ]
    for text in invalid_texts:
        match = VIN_PATTERN_REGEX.search(text)
        assert match is None or len(match.group(1)) != 17


# 2. extract_chassis_markings Unit Tests
@patch("app.car_tools.genai.Client")
@patch("app.car_tools.record_chassis_registry_entry")
@patch("app.car_tools.award_user_badge")
def test_extract_chassis_markings_numbered_edition(mock_award, mock_record, mock_genai_cls):
    """Verify extract_chassis_markings extracts numbered plaque, records entry, awards badge, and applies 2.5x."""
    mock_client = MagicMock()
    mock_genai_cls.return_value = mock_client
    mock_response = MagicMock()
    mock_response.text = json.dumps({
        "extracted_text": "Porsche 911 GT3 RS Weissach - Unit 042/600",
        "production_number": "042/600",
        "edition": "Weissach",
        "vin": None,
        "location_observed": "Center console plaque",
    })
    mock_client.models.generate_content.return_value = mock_response

    mock_record.return_value = {
        "unit_number": "042/600",
        "first_spotted_by": "spotter_alex",
        "verified_at": "2026-10-09T08:00:00Z",
    }

    result = extract_chassis_markings(
        image_bytes=b"dummy_jpeg_bytes",
        user_id="spotter_alex",
        vehicle_hint="Porsche 911 GT3 RS",
    )

    assert result["success"] is True
    assert result["production_number"] == "042/600"
    assert result["edition"] == "Weissach"
    assert result["chassis_verified"] is True
    assert result["badge_awarded"] == "Chassis Archivist"
    assert result["first_finder_multiplier"] == 2.5

    # Verify Firestore record was called with make_model_edition
    mock_record.assert_called_once()
    args, kwargs = mock_record.call_args
    assert "Porsche 911 GT3 RS" in kwargs.get("make_model_edition", args[0] if args else "")
    assert kwargs.get("unit_number", args[1] if len(args) > 1 else "") == "042/600"
    assert kwargs.get("user_id", args[2] if len(args) > 2 else "") == "spotter_alex"

    # Verify permanent badge awarded to user
    mock_award.assert_called_once_with("spotter_alex", "Chassis Archivist")


@patch("app.car_tools.genai.Client")
@patch("app.car_tools.decode_vin_specifications")
@patch("app.car_tools.record_chassis_registry_entry")
@patch("app.car_tools.award_user_badge")
def test_extract_chassis_markings_vin_lookup(mock_award, mock_record, mock_decode, mock_genai_cls):
    """Verify extract_chassis_markings calls decode_vin_specifications when VIN is extracted."""
    mock_client = MagicMock()
    mock_genai_cls.return_value = mock_client
    mock_response = MagicMock()
    mock_response.text = json.dumps({
        "extracted_text": "Windshield VIN Placard WP0AB2A91PS256789",
        "production_number": None,
        "edition": None,
        "vin": "WP0AB2A91PS256789",
        "location_observed": "Lower windshield tag",
    })
    mock_client.models.generate_content.return_value = mock_response

    mock_decode.return_value = {
        "valid": True,
        "vin": "WP0AB2A91PS256789",
        "make": "PORSCHE",
        "model": "911 GT3",
        "year": 2023,
        "engine_horsepower": "502",
        "plant_country": "GERMANY",
    }

    result = extract_chassis_markings(
        image_bytes=b"dummy_jpeg_bytes",
        user_id="spotter_alex",
        vehicle_hint="",
    )

    assert result["success"] is True
    assert result["vin"] == "WP0AB2A91PS256789"
    assert result["chassis_verified"] is True
    mock_decode.assert_called_once_with("WP0AB2A91PS256789")
    assert result["vin_decoded"]["make"] == "PORSCHE"
    assert result["vin_decoded"]["engine_horsepower"] == "502"
    # No numbered plaque, so no Chassis Archivist badge or 2.5x
    assert result["badge_awarded"] is None
    assert result["first_finder_multiplier"] == 1.0


# 3. Integration with identify_and_spot_car
@patch("app.car_tools.check_and_reserve_scan")
@patch("app.car_tools.commit_scan_deduction")
@patch("app.car_tools.genai.Client")
@patch("app.car_tools.storage.Client")
@patch("app.car_tools.record_car_spot_entry")
@patch("app.car_tools.extract_chassis_markings")
def test_identify_and_spot_car_chassis_archivist_integration(
    mock_chassis_extractor,
    mock_record_spot,
    mock_storage,
    mock_genai_cls,
    mock_commit,
    mock_reserve,
):
    """Verify identify_and_spot_car applies 2.5x multiplier and attaches chassis verification card."""
    mock_reserve.return_value = (True, "OK", {"allowed": True, "scans_today": 1, "daily_limit": 5, "tier": "free"})
    mock_commit.return_value = {"scans_today": 2, "daily_limit": 5}

    mock_client = MagicMock()
    mock_genai_cls.return_value = mock_client
    mock_response = MagicMock()
    mock_response.text = json.dumps({
        "make": "Porsche",
        "model": "911 GT3 RS",
        "trim": "Weissach",
        "observed_colorway": "Arctic Grey",
        "confidence": 0.98,
        "key_identifying_features": ["Swan neck wing", "Fender louvers"],
    })
    mock_client.models.generate_content.return_value = mock_response

    # Mock Cloud Storage upload
    mock_bucket = MagicMock()
    mock_storage.return_value.bucket.return_value = mock_bucket
    mock_blob = MagicMock()
    mock_bucket.blob.return_value = mock_blob
    mock_blob.public_url = "https://storage.googleapis.com/test-bucket/test.jpg"

    # Mock extract_chassis_markings returning numbered plaque
    mock_chassis_extractor.return_value = {
        "success": True,
        "vin": "WP0AB2A91PS256789",
        "vin_decoded": {"valid": True, "make": "Porsche", "model": "911 GT3 RS", "year": 2023, "engine_horsepower": "518"},
        "production_number": "042/600",
        "edition": "Weissach",
        "make_model_edition": "Porsche 911 GT3 RS Weissach",
        "raw_text": "Unit 042/600 Weissach",
        "detected_markings": ["Production Plaque: 042/600", "Special Edition: Weissach"],
        "chassis_verified": True,
        "badge_awarded": "Chassis Archivist",
        "first_finder_multiplier": 2.5,
        "registry_entry": {"unit_number": "042/600", "first_spotted_by": "spotter_alex", "verified_at": "2026-10-09T08:00:00Z"},
    }

    mock_record_spot.return_value = {"id": "spot_test_123", "points_awarded": 2500}

    result = identify_and_spot_car(
        image_input="data:image/jpeg;base64,dGVzdA==",
        user_id="spotter_alex",
        latitude=37.7749,
        longitude=-122.4194,
    )

    assert result["success"] is True
    # Multiplier should be 2.5x in dynamic scoring breakdown
    scoring = result["dynamic_scoring"]
    assert scoring["first_spotter_bonus"] == 2.5
    assert scoring["breakdown"]["first_finder_multiplier"] == 2.5
    assert "chassis_archivist" in scoring["breakdown"]
    assert scoring["breakdown"]["chassis_archivist"]["applied"] is True
    assert scoring["breakdown"]["chassis_archivist"]["unit_number"] == "042/600"

    # Chassis markings returned in result
    assert result["chassis_markings"] is not None
    assert result["chassis_markings"]["production_number"] == "042/600"

    # A2UI cards should contain chassis_verification card
    a2ui_card_types = [c.get("type") for c in result["a2ui_cards"]]
    assert "chassis_verification" in a2ui_card_types
    chassis_card = next(c for c in result["a2ui_cards"] if c.get("type") == "chassis_verification")
    assert chassis_card["unit_number"] == "042/600"
    assert chassis_card["edition"] == "Weissach"
    assert chassis_card["first_finder_multiplier"] == 2.5


# 4. Agent Tool Structure Test
def test_extract_chassis_markings_registered_in_agent():
    """Verify extract_chassis_markings is registered in the ADK agent tools list."""
    tool_names = [getattr(t, "name", None) or getattr(t, "__name__", None) for t in cardex_agent.tools]
    assert "extract_chassis_markings" in tool_names
    assert "decode_vin_specifications" in tool_names
