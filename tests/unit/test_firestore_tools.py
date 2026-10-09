from app.car_tools import (
    lookup_car_in_cardex,
    list_cardex_cars,
    record_car_spot,
    view_my_garage,
    view_leaderboard,
    submit_car_review,
    decode_vin_specifications,
    search_vehicle_specs_database,
    lookup_nhtsa_models_by_year,
    fetch_carapi_live_specs,
    fetch_car_image_and_provenance,
    identify_and_spot_car,
)
from app.firestore_db import FIRESTORE_PROJECT_ID


def test_firestore_project_id_hardcoded():
    assert FIRESTORE_PROJECT_ID == "qwiklabs-gcp-04-6f324b699fdd"
    assert not FIRESTORE_PROJECT_ID.isdigit(), "Must be string project ID, not numeric"


def test_lookup_seeded_cars():
    res = lookup_car_in_cardex("Bugatti La Voiture Noire")
    assert res["found"] is True
    car = res["car"]
    assert car["make"] == "Bugatti"
    assert car["rarity_tier"] == "Mythic 1-of-1"
    assert car["points"] == 50000
    assert car["production_count"] == 1


def test_list_cars_filter():
    rare_cars = list_cardex_cars(rarity_tier="Rare")
    assert len(rare_cars) >= 1
    assert any("GT3 RS" in c.get("model", "") for c in rare_cars)


def test_spot_and_garage_flow():
    user = "pytest_spotter"
    spot = record_car_spot(
        car_name_or_id="koenigsegg-jesko-absolut",
        colorway="Tang Orange",
        location="Monaco GP Circuit",
        notes="Testing spot recording",
        user_id=user,
    )
    assert spot["points_awarded"] == 15000
    assert spot["rarity_tier"] == "Legendary"

    garage = view_my_garage(user_id=user)
    assert len(garage) >= 1
    assert any("Jesko" in s.get("car_name", "") for s in garage)


def test_submit_review():
    review = submit_car_review(
        car_name="McLaren F1 LM",
        issue_description="Colorway check",
        proposed_correction="Confirmed Papaya Orange",
        user_id="pytest_spotter",
    )
    assert review["status"] == "pending_review"
    assert "review_id" in review


def test_decode_vin_specifications():
    # Valid Porsche 911 VIN
    res = decode_vin_specifications("WP0AB2A91PS256789")
    assert res["valid"] is True
    assert res["make"] == "PORSCHE"
    assert res["model"] == "911"
    assert res["year"] == 2023
    assert res["plant_country"] == "GERMANY"

    # Invalid VIN length
    invalid_res = decode_vin_specifications("SHORTVIN")
    assert invalid_res["valid"] is False
    assert "Invalid VIN length" in invalid_res["error"]


def test_integrated_vehicles_in_cardex():
    # Verify new cars from vehicle-makes-models dataset were added
    bolide = lookup_car_in_cardex("Bugatti Bolide")
    assert bolide["found"] is True
    assert bolide["car"]["rarity_tier"] == "Mythic 1-of-1"
    assert bolide["car"]["specs"]["horsepower"] == 1850

    nevera = lookup_car_in_cardex("Rimac Nevera")
    assert nevera["found"] is True
    assert nevera["car"]["rarity_tier"] == "Legendary"

    veneno = lookup_car_in_cardex("Lamborghini Veneno")
    assert veneno["found"] is True
    assert veneno["car"]["points"] == 35000


def test_search_vehicle_specs_database():
    # Test searching the 30,535-engine SQLite database directly
    results = search_vehicle_specs_database(query="Skyline", make="Nissan", limit=3)
    assert len(results) >= 1
    assert any("Skyline" in r["model"] for r in results)

    # Test high horsepower filter
    monsters = search_vehicle_specs_database(query="Chiron", min_hp=1400, limit=2)
    assert len(monsters) >= 1
    assert monsters[0]["power_hp"] >= 1400


def test_lookup_car_fallback_to_specs_db():
    # A car not in Firestore should fall back to SQLite dataset
    res = lookup_car_in_cardex("Toyota Supra")
    assert res["found"] is True
    assert res["source"] == "vehicle_specs_database"
    assert "specs_record" in res


def test_lookup_nhtsa_models_by_year():
    res = lookup_nhtsa_models_by_year("Porsche", 2024)
    assert res["source"] == "US NHTSA Federal vPIC API"
    assert res["make"] == "Porsche"
    assert res["model_year"] == 2024
    assert res["count"] >= 5
    assert "911" in res["models"]


def test_fetch_carapi_live_specs():
    res = fetch_carapi_live_specs(make="Ferrari", model="488 GTB", year=2019)
    assert res["source"] == "CarAPI Live Service"
    assert res["make"] == "Ferrari"
    assert res["model"] == "488 GTB"
    assert len(res["trims"]) >= 1
    assert "msrp" in res["trims"][0]
    assert len(res["exterior_colors"]) >= 1
    assert "rgb" in res["exterior_colors"][0]


def test_fetch_car_image_and_provenance():
    res = fetch_car_image_and_provenance("McLaren Senna")
    assert res["source"] == "Wikimedia Open Automotive Archive"
    assert "McLaren Senna" in res["car_name"]
    assert res["thumbnail_url"] is not None
    assert res["thumbnail_url"].startswith("http")
    assert "Senna" in res["summary"]


def test_identify_and_spot_car_multimodal_vision():
    import uuid
    from unittest.mock import patch
    test_user = f"dan_vision_{uuid.uuid4().hex[:8]}"
    test_image_url = "https://thumb.wikimedia.org/wikipedia/commons/thumb/9/95/McLaren_Senna_IMG_3279.jpg/330px-McLaren_Senna_IMG_3279.jpg"
    with patch("app.car_tools.verify_image_integrity", return_value={"passed": True}):
        res = identify_and_spot_car(
            image_input=test_image_url,
            location="Monaco Casino Square",
            user_id=test_user,
            notes="Testing multimodal vision recognition and GCS upload",
            auto_log_spot=True,
        )
    assert res["success"] is True
    assert res["image_public_url"].startswith("https://storage.googleapis.com/cardex-spots-qwiklabs-gcp-04-6f324b699fdd/spots/")
    assert res["spot_logged"] is True
    assert res["points_awarded"] >= 500
    assert "vision_identification" in res
    assert res["vision_identification"]["make"] == "McLaren"
    assert "Senna" in res["vision_identification"]["model"]
    assert res["spot_details"]["image_url"] == res["image_public_url"]



