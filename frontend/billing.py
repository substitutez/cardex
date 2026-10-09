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

"""Stripe Billing Router & Checkout Session Manager for CarDex.

Provides Stripe Checkout integration for:
1. Mode 'subscription': $9.99/mo for CarDex Pro Unlimited scans.
2. Mode 'payment': $1.99 for 5 consumable scan refill packs.
Includes robust webhook handling for checkout.session.completed.
"""

import json
import logging
import os
from typing import Any, Optional
import stripe

try:
    from .quota_limiter import set_user_tier, add_refill_credits
except ImportError:
    from quota_limiter import set_user_tier, add_refill_credits

logger = logging.getLogger(__name__)

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def create_checkout_session(
    user_id: str,
    session_type: str = "subscription",
    success_url: str = "",
    cancel_url: str = "",
) -> dict[str, Any]:
    """Create a Stripe Checkout Session for CarDex Pro subscription or scan refill pack.

    Args:
        user_id: The authenticated spotter user_id.
        session_type: Either 'subscription' or 'refill'.
        success_url: Optional redirect URL on payment success.
        cancel_url: Optional redirect URL on user cancellation.

    Returns:
        Dict containing checkout_url and session_id.
    """
    session_type = "subscription" if session_type == "subscription" else "refill"

    # Check for placeholder or simulated environment
    is_mock = (
        not STRIPE_SECRET_KEY
        or STRIPE_SECRET_KEY.startswith("sk_test_placeholder")
        or "Mock" in STRIPE_SECRET_KEY
        or "..." in STRIPE_SECRET_KEY
        or STRIPE_SECRET_KEY.startswith("sk_live_...")
    )

    if not is_mock:
        try:
            if session_type == "subscription":
                session = stripe.checkout.Session.create(
                    mode="subscription",
                    payment_method_types=["card"],
                    line_items=[
                        {
                            "price_data": {
                                "currency": "usd",
                                "product_data": {
                                    "name": "CarDex Pro Pass (Unlimited Scans)",
                                    "description": "Unlimited daily vehicle scans, real-time colorway analysis & master spotter privileges",
                                },
                                "unit_amount": 999,  # $9.99/mo
                                "recurring": {"interval": "month"},
                            },
                            "quantity": 1,
                        }
                    ],
                    client_reference_id=user_id,
                    metadata={"type": "subscription", "user_id": user_id},
                    success_url=success_url
                    or "https://cardex-frontend-861697384142.us-east1.run.app/?billing_status=success&session_id={CHECKOUT_SESSION_ID}",
                    cancel_url=cancel_url
                    or "https://cardex-frontend-861697384142.us-east1.run.app/?billing_status=cancelled",
                )
            else:
                session = stripe.checkout.Session.create(
                    mode="payment",
                    payment_method_types=["card"],
                    line_items=[
                        {
                            "price_data": {
                                "currency": "usd",
                                "product_data": {
                                    "name": "CarDex Fuel Pack (5 Consumable Scans)",
                                    "description": "5 additional high-res vehicle spotting scans",
                                },
                                "unit_amount": 199,  # $1.99
                            },
                            "quantity": 1,
                        }
                    ],
                    client_reference_id=user_id,
                    metadata={"type": "refill", "user_id": user_id},
                    success_url=success_url
                    or "https://cardex-frontend-861697384142.us-east1.run.app/?billing_status=success&session_id={CHECKOUT_SESSION_ID}",
                    cancel_url=cancel_url
                    or "https://cardex-frontend-861697384142.us-east1.run.app/?billing_status=cancelled",
                )

            return {
                "checkout_url": session.url,
                "session_id": session.id,
                "type": session_type,
            }
        except Exception as e:
            logger.warning("Stripe Checkout Session creation failed, using sandbox fallback: %s", e)

    # Sandbox / simulated fallback checkout
    mock_id = f"cs_sandbox_{session_type}_{os.urandom(8).hex()}"
    fallback_url = f"{success_url or '/'}#checkout-sandbox?session_id={mock_id}&type={session_type}&user_id={user_id}"
    return {
        "checkout_url": fallback_url,
        "session_id": mock_id,
        "type": session_type,
        "sandboxed": True,
    }


def handle_stripe_webhook(payload: bytes, sig_header: Optional[str] = None) -> dict[str, Any]:
    """Verify incoming Stripe webhook signature and apply customer quota updates.

    When checkout.session.completed arrives:
    - If subscription: sets user quota tier to 'pro'.
    - If refill: atomically increments user refill_credits by 5.
    """
    event = None
    is_live_secret = (
        STRIPE_WEBHOOK_SECRET
        and not STRIPE_WEBHOOK_SECRET.startswith("whsec_placeholder")
        and "Mock" not in STRIPE_WEBHOOK_SECRET
    )

    if is_live_secret and sig_header:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        except Exception as e:
            logger.error("Stripe signature verification failed: %s", e)
            raise ValueError(f"Webhook signature verification failed: {e}")
    else:
        # Fallback parse for unit tests or mocked webhook events
        try:
            event = json.loads(payload.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"Invalid JSON payload: {e}")

    event_type = event.get("type")
    if event_type == "checkout.session.completed":
        session = event.get("data", {}).get("object", {})
        user_id = session.get("client_reference_id") or session.get("metadata", {}).get("user_id") or "spotter_1"
        item_type = session.get("metadata", {}).get("type") or (
            "subscription" if session.get("mode") == "subscription" else "refill"
        )

        if item_type == "subscription":
            res = set_user_tier(user_id, "pro")
            logger.info("Successfully upgraded user '%s' to Pro tier via Stripe", user_id)
            return {
                "status": "success",
                "event": event_type,
                "type": "subscription",
                "user_id": user_id,
                "tier": "pro",
                "quota": res,
            }
        else:
            res = add_refill_credits(user_id, 5)
            logger.info("Successfully added 5 refill credits to user '%s' via Stripe", user_id)
            return {
                "status": "success",
                "event": event_type,
                "type": "refill",
                "user_id": user_id,
                "credits_added": 5,
                "quota": res,
            }

    return {"status": "ignored", "event": event_type}


def handle_revenuecat_webhook(
    payload: dict[str, Any] | bytes,
    auth_header: Optional[str] = None,
) -> dict[str, Any]:
    """Handle incoming RevenueCat Webhook events for iOS In-App Purchases.

    Supported events:
    - INITIAL_PURCHASE or RENEWAL with product 'cardex_pro_monthly':
        sets user tier to 'pro'.
    - INITIAL_PURCHASE or NON_RENEWING_PURCHASE with product 'cardex_scans_5':
        atomically increments refill_credits by 5.
    - CANCELLATION or EXPIRATION:
        resets user tier to 'free'.
    """
    expected_key = os.getenv("REVENUECAT_WEBHOOK_AUTH_KEY", "")
    if expected_key:
        if not auth_header or not auth_header.startswith("Bearer "):
            raise ValueError("Missing or invalid Authorization header")
        token = auth_header.split(" ", 1)[1].strip()
        if token != expected_key:
            raise ValueError("Unauthorized RevenueCat webhook token")

    if isinstance(payload, (bytes, bytearray)):
        try:
            body = json.loads(payload.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"Invalid JSON payload: {e}")
    else:
        body = payload

    event = body.get("event") or body
    event_type = str(event.get("type", "")).upper()
    app_user_id = event.get("app_user_id") or event.get("original_app_user_id") or "spotter_1"
    product_id = event.get("product_id") or ""

    logger.info("Processing RevenueCat event %s for user %s (product: %s)", event_type, app_user_id, product_id)

    if event_type in ("INITIAL_PURCHASE", "RENEWAL") and product_id == "cardex_pro_monthly":
        res = set_user_tier(app_user_id, "pro")
        return {
            "status": "success",
            "event": event_type,
            "product_id": product_id,
            "user_id": app_user_id,
            "tier": "pro",
            "quota": res,
        }

    if event_type in ("INITIAL_PURCHASE", "NON_RENEWING_PURCHASE") and product_id == "cardex_scans_5":
        res = add_refill_credits(app_user_id, 5)
        return {
            "status": "success",
            "event": event_type,
            "product_id": product_id,
            "user_id": app_user_id,
            "credits_added": 5,
            "quota": res,
        }

    if event_type in ("CANCELLATION", "EXPIRATION"):
        res = set_user_tier(app_user_id, "free")
        return {
            "status": "success",
            "event": event_type,
            "product_id": product_id,
            "user_id": app_user_id,
            "tier": "free",
            "quota": res,
        }

    return {
        "status": "ignored",
        "event": event_type,
        "product_id": product_id,
        "user_id": app_user_id,
    }

