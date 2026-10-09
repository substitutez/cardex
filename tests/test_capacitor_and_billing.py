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

"""Unit and Integration Tests for Capacitor Configuration, iOS Scaffolding,
Stripe Checkout, and RevenueCat Webhook Subsystems.
"""

import json
import os
import plistlib
import sys
import uuid
import pytest

# Ensure AGENT_ENGINE_RESOURCE_NAME is populated for tests
if "AGENT_ENGINE_RESOURCE_NAME" not in os.environ:
    os.environ["AGENT_ENGINE_RESOURCE_NAME"] = "projects/861697384142/locations/us-east1/reasoningEngines/5178590915173810176"

# Add frontend and project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from starlette.testclient import TestClient
from frontend.main import app
from frontend.billing import handle_revenuecat_webhook, create_checkout_session
import quota_limiter


@pytest.fixture
def test_client():
    return TestClient(app)


def test_capacitor_config_json_structure():
    """Verify capacitor.config.json has required keys and Cloud Run server URL."""
    config_path = os.path.join(os.path.dirname(__file__), "..", "capacitor.config.json")
    assert os.path.exists(config_path), "capacitor.config.json must exist in root"

    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data.get("appId") == "com.cardex.spotter"
    assert data.get("appName") == "CarDex"
    assert data.get("webDir") == "frontend/static"
    assert "server" in data
    assert data["server"].get("url") == "https://cardex-frontend-861697384142.us-east1.run.app"


def test_ios_info_plist_privacy_descriptions():
    """Verify ios/App/App/Info.plist contains required Apple Privacy usage keys."""
    plist_path = os.path.join(os.path.dirname(__file__), "..", "ios", "App", "App", "Info.plist")
    assert os.path.exists(plist_path), "Info.plist must exist in ios/App/App/"

    with open(plist_path, "rb") as f:
        plist_data = plistlib.load(f)

    assert "NSCameraUsageDescription" in plist_data
    assert "CarDex uses the camera to scan and identify spotted vehicles in real time." in plist_data["NSCameraUsageDescription"]

    assert "NSLocationWhenInUseUsageDescription" in plist_data
    assert "CarDex uses your location to map car sightings on the local radar." in plist_data["NSLocationWhenInUseUsageDescription"]

    assert "NSMicrophoneUsageDescription" in plist_data
    assert "CarDex uses the microphone to capture optional vehicle exhaust audio clips." in plist_data["NSMicrophoneUsageDescription"]


def test_xcode_project_in_app_purchase_capability():
    """Verify project.pbxproj contains In-App Purchase system capability enabled."""
    pbx_path = os.path.join(os.path.dirname(__file__), "..", "ios", "App", "App.xcodeproj", "project.pbxproj")
    assert os.path.exists(pbx_path), "project.pbxproj must exist"

    with open(pbx_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "com.apple.InAppPurchase" in content
    assert "enabled = 1;" in content


def test_stripe_create_checkout_session_endpoints(test_client):
    """Test POST /api/billing/stripe/create-checkout-session creates session."""
    user_id = f"test_stripe_{uuid.uuid4().hex[:8]}"

    # Subscription mode
    res_sub = test_client.post(
        "/api/billing/stripe/create-checkout-session",
        json={"type": "subscription", "user_id": user_id},
    )
    assert res_sub.status_code == 200
    sub_data = res_sub.json()
    assert "checkout_url" in sub_data
    assert sub_data["type"] == "subscription"

    # Refill mode
    res_refill = test_client.post(
        "/api/billing/stripe/create-checkout-session",
        json={"type": "refill", "user_id": user_id},
    )
    assert res_refill.status_code == 200
    refill_data = res_refill.json()
    assert "checkout_url" in refill_data
    assert refill_data["type"] == "refill"


def test_stripe_webhook_flow(test_client):
    """Test POST /api/billing/stripe/webhook grants Pro and Refill credits."""
    user_id = f"test_stripe_hook_{uuid.uuid4().hex[:8]}"

    # 1. Subscription webhook event
    sub_event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "client_reference_id": user_id,
                "metadata": {"type": "subscription", "user_id": user_id},
                "mode": "subscription",
            }
        },
    }
    res_sub = test_client.post(
        "/api/billing/stripe/webhook",
        json=sub_event,
    )
    assert res_sub.status_code == 200
    q = quota_limiter.get_user_quota(user_id)
    assert q["tier"] == "pro"

    # 2. Refill webhook event
    refill_user = f"test_stripe_refill_{uuid.uuid4().hex[:8]}"
    refill_event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "client_reference_id": refill_user,
                "metadata": {"type": "refill", "user_id": refill_user},
                "mode": "payment",
            }
        },
    }
    res_refill = test_client.post(
        "/api/billing/stripe/webhook",
        json=refill_event,
    )
    assert res_refill.status_code == 200
    q2 = quota_limiter.get_user_quota(refill_user)
    assert q2["refill_credits"] >= 5


def test_revenuecat_webhook_auth(test_client, monkeypatch):
    """Verify RevenueCat webhook enforces Bearer token authentication when configured."""
    monkeypatch.setenv("REVENUECAT_WEBHOOK_AUTH_KEY", "rc_secret_test_token_123")

    # Missing header -> 401
    res_unauth = test_client.post(
        "/api/billing/revenuecat-webhook",
        json={"event": {"type": "INITIAL_PURCHASE"}},
    )
    assert res_unauth.status_code == 401

    # Invalid header -> 401
    res_invalid = test_client.post(
        "/api/billing/revenuecat-webhook",
        json={"event": {"type": "INITIAL_PURCHASE"}},
        headers={"Authorization": "Bearer wrong_token"},
    )
    assert res_invalid.status_code == 401

    # Valid header -> 200
    res_valid = test_client.post(
        "/api/billing/revenuecat-webhook",
        json={
            "event": {
                "type": "INITIAL_PURCHASE",
                "app_user_id": "test_spotter_rc",
                "product_id": "cardex_pro_monthly",
            }
        },
        headers={"Authorization": "Bearer rc_secret_test_token_123"},
    )
    assert res_valid.status_code == 200


def test_revenuecat_webhook_purchase_and_cancellation_events(test_client):
    """Verify RevenueCat handles Pro purchase, Refill consumable, and Cancellation."""
    user_id = f"test_rc_user_{uuid.uuid4().hex[:8]}"

    # Ensure clean initial state
    quota_limiter.set_user_tier(user_id, "free")
    initial_q = quota_limiter.get_user_quota(user_id)
    initial_refills = initial_q.get("refill_credits", 0)

    # 1. INITIAL_PURCHASE for cardex_pro_monthly -> sets tier to "pro"
    res_pro = test_client.post(
        "/api/billing/revenuecat-webhook",
        json={
            "event": {
                "type": "INITIAL_PURCHASE",
                "app_user_id": user_id,
                "product_id": "cardex_pro_monthly",
            }
        },
    )
    assert res_pro.status_code == 200
    q = quota_limiter.get_user_quota(user_id)
    assert q["tier"] == "pro"

    # 2. RENEWAL for cardex_pro_monthly -> remains "pro"
    res_renew = test_client.post(
        "/api/billing/revenuecat-webhook",
        json={
            "event": {
                "type": "RENEWAL",
                "app_user_id": user_id,
                "product_id": "cardex_pro_monthly",
            }
        },
    )
    assert res_renew.status_code == 200
    assert quota_limiter.get_user_quota(user_id)["tier"] == "pro"

    # 3. NON_RENEWING_PURCHASE for cardex_scans_5 -> increments refill_credits by 5
    res_refill = test_client.post(
        "/api/billing/revenuecat-webhook",
        json={
            "event": {
                "type": "NON_RENEWING_PURCHASE",
                "app_user_id": user_id,
                "product_id": "cardex_scans_5",
            }
        },
    )
    assert res_refill.status_code == 200
    q_refill = quota_limiter.get_user_quota(user_id)
    assert q_refill["refill_credits"] == initial_refills + 5

    # 4. CANCELLATION -> resets tier to "free"
    res_cancel = test_client.post(
        "/api/billing/revenuecat-webhook",
        json={
            "event": {
                "type": "CANCELLATION",
                "app_user_id": user_id,
                "product_id": "cardex_pro_monthly",
            }
        },
    )
    assert res_cancel.status_code == 200
    q_free = quota_limiter.get_user_quota(user_id)
    assert q_free["tier"] == "free"

    # 5. EXPIRATION -> sets tier to "free"
    quota_limiter.set_user_tier(user_id, "pro")
    res_expire = test_client.post(
        "/api/billing/revenuecat-webhook",
        json={
            "event": {
                "type": "EXPIRATION",
                "app_user_id": user_id,
                "product_id": "cardex_pro_monthly",
            }
        },
    )
    assert res_expire.status_code == 200
    assert quota_limiter.get_user_quota(user_id)["tier"] == "free"


def test_frontend_index_html_contains_paywall_and_quota_features():
    """Verify frontend/static/index.html includes required fuel gauge and paywall cards."""
    html_path = os.path.join(os.path.dirname(__file__), "..", "frontend", "static", "index.html")
    assert os.path.exists(html_path)

    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    # Fuel gauge in cockpit header
    assert "Scans Remaining:" in html or "btn-quota-pill" in html
    assert "Pro Active: Unlimited" in html

    # Paywall cards
    assert "Pro Hunter Unlimited" in html
    assert "$9.99" in html
    assert "Emergency Refill" in html
    assert "$1.99" in html

    # Detection logic and RevenueCat SDK
    assert "appl_cAeseGBStfGlKoSBM9JWmfcAbqo" in html
    assert "isNativePlatform" in html
    assert "Purchases.purchaseProduct" in html
    assert "/api/billing/stripe/create-checkout-session" in html
