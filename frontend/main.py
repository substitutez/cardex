"""Minimal FastAPI proxy for a deployed A2A agent (Agent Runtime, agents-cli 1.1.0+).

The browser talks ONLY to this proxy (same origin, no CORS, no GCP creds in the
browser). The proxy authenticates with Application Default Credentials and
forwards chat to the deployed agent over the A2A protocol, returning replies as
structured parts the chat UI knows how to show:

  * {"kind": "text", "text": ...}  -> a normal chat bubble
  * {"kind": "a2ui", "data": ...}  -> one A2UI message (beginRendering /
    surfaceUpdate); static/index.html renders these as a card.

Why A2A: agents-cli 1.1.0 (GA) deploys ADK agents to Agent Runtime as A2A agents
and no longer registers the reasoning-engine operation schema the old
`agent_engines.get(...).stream_query()` path relied on (operation_schemas() comes
back empty). The container serves the A2A protocol over the Agent Engine HTTP
passthrough, so this proxy fetches the agent's card and sends messages with the
a2a-sdk client (the same path `agents-cli run --mode a2a` uses). This works for
both A2A and plain ADK 1.1.0 deployments (the container serves A2A either way).

Run:
  pip install -r requirements.txt
  export AGENT_ENGINE_RESOURCE_NAME="projects/.../locations/.../reasoningEngines/..."
  export AGENT_DIRECTORY="app"   # your agent's app directory (agents-cli-manifest.yaml)
  python main.py                 # -> http://localhost:8080
"""

import json
import os
import uuid

import google.auth
import google.auth.transport.requests
import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types import (
    AgentCard,
    FilePart,
    Message,
    Part,
    Role,
    TaskArtifactUpdateEvent,
    TextPart,
    TransportProtocol,
)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import base64
import auth_db
import quota_limiter
import anti_cheat
import firestore_db
import billing
import a2ui_utils
import paint_matcher

try:
    from cache import (
        get_cached_leaderboard,
        set_cached_leaderboard,
        get_cached_loudest_leaderboard,
        set_cached_loudest_leaderboard,
        get_cached_user_quota,
        set_cached_user_quota,
        invalidate_user_quota,
        is_redis_available,
    )
except ImportError:
    from app.cache import (
        get_cached_leaderboard,
        set_cached_leaderboard,
        get_cached_loudest_leaderboard,
        set_cached_loudest_leaderboard,
        get_cached_user_quota,
        set_cached_user_quota,
        invalidate_user_quota,
        is_redis_available,
    )

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

RESOURCE = os.environ["AGENT_ENGINE_RESOURCE_NAME"]
# The agent's app directory (matches agent_directory in agents-cli-manifest.yaml).
AGENT_DIRECTORY = os.environ.get("AGENT_DIRECTORY", "app")
# Location is embedded in the resource name: projects/<p>/locations/<loc>/reasoningEngines/<id>.
LOCATION = RESOURCE.split("/locations/")[1].split("/")[0]

# A2A endpoint for an Agent Runtime deployment, via the Agent Engine HTTP
# passthrough. The card lives at the well-known path under this base.
A2A_BASE = (
    f"https://{LOCATION}-aiplatform.googleapis.com/reasoningEngines/v1/"
    f"{RESOURCE}/api/a2a/{AGENT_DIRECTORY}"
)
A2A_CARD_URL = f"{A2A_BASE}/.well-known/agent-card.json"

# The agent tags its A2UI data parts with this mime type.
_A2UI_MIME = "application/json+a2ui"

# One set of ADC credentials, refreshed per request (access tokens expire ~1h).
_creds, _ = google.auth.default(
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)


def _auth_headers() -> dict[str, str]:
    _creds.refresh(google.auth.transport.requests.Request())
    return {
        "Authorization": f"Bearer {_creds.token}",
        "Content-Type": "application/json",
    }


app = FastAPI()


@app.exception_handler(Exception)
async def _json_errors(request: Request, exc: Exception):
    # Always return JSON so the browser never receives a plain-text 500 page
    # (which shows up in the chat as "Unexpected token 'I', "Internal S"... is
    # not valid JSON"). Any server-side failure now surfaces as a readable
    # message in the chat bubble instead.
    return JSONResponse(
        status_code=200,
        content={
            "parts": [{"kind": "text", "text": f"Error: {type(exc).__name__}: {exc}"}]
        },
    )


# Reuse ONE A2A context per user so the agent remembers the conversation.
_contexts: dict[str, str] = {}
# Cache the agent card after the first fetch.
_card: AgentCard | None = None


async def _get_card(client: httpx.AsyncClient) -> AgentCard:
    global _card
    if _card is None:
        resp = await client.get(A2A_CARD_URL)
        resp.raise_for_status()
        card = AgentCard(**resp.json())
        # Agent Runtime does not serve a public card URL, so point the client at
        # the passthrough base for message sends.
        card.url = A2A_BASE
        _card = card
    return _card


def _extract_parts(parts: list) -> list[dict]:
    """Turn A2A response parts into structured parts for the chat UI.

    Text parts pass through as {"kind": "text"}. A2UI data parts (tagged
    application/json+a2ui) become {"kind": "a2ui", "data": <message>} so the UI
    renders the card; each data part is one A2UI message (beginRendering or
    surfaceUpdate).
    """
    out: list[dict] = []
    for p in parts:
        root = getattr(p, "root", p)
        if isinstance(root, TextPart) and getattr(root, "text", None):
            text_val = root.text
            if "<a2ui-json>" in text_val and "</a2ui-json>" in text_val:
                remaining_text = text_val
                while "<a2ui-json>" in remaining_text and "</a2ui-json>" in remaining_text:
                    try:
                        start_tag_idx = remaining_text.index("<a2ui-json>")
                        end_tag_idx = remaining_text.index("</a2ui-json>")
                        before = remaining_text[:start_tag_idx].strip()
                        if before:
                            out.append({"kind": "text", "text": before})

                        json_str = remaining_text[start_tag_idx + len("<a2ui-json>"):end_tag_idx].strip()
                        card_data = json.loads(json_str)
                        if isinstance(card_data, list):
                            for item in card_data:
                                if isinstance(item, dict) and ("surfaceUpdate" in item or "beginRendering" in item):
                                    out.append({"kind": "a2ui", "data": item})
                                else:
                                    out.append({"kind": "a2ui", "data": {"surfaceUpdate": item}})
                        elif isinstance(card_data, dict):
                            if "surfaceUpdate" in card_data or "beginRendering" in card_data:
                                out.append({"kind": "a2ui", "data": card_data})
                            else:
                                out.append({"kind": "a2ui", "data": {"surfaceUpdate": card_data}})

                        remaining_text = remaining_text[end_tag_idx + len("</a2ui-json>"):].strip()
                    except Exception as e:
                        print("Failed to parse <a2ui-json>:", e)
                        break
                if remaining_text:
                    out.append({"kind": "text", "text": remaining_text})
                continue
            out.append({"kind": "text", "text": text_val})
        elif getattr(root, "data", None) is not None:
            data_val = root.data
            meta = getattr(root, "metadata", None) or {}
            mime = meta.get("mimeType") if isinstance(meta, dict) else None
            
            # Check if root.data itself is a dict containing {"data": ..., "metadata": {"mimeType": "application/json+a2ui"}}
            if isinstance(data_val, dict):
                inner_meta = data_val.get("metadata") or {}
                inner_mime = inner_meta.get("mimeType") if isinstance(inner_meta, dict) else None
                if mime == _A2UI_MIME or inner_mime == _A2UI_MIME or "surfaceUpdate" in data_val or "beginRendering" in data_val:
                    actual_a2ui = data_val.get("data", data_val)
                    out.append({"kind": "a2ui", "data": actual_a2ui})
                    continue
            if mime == _A2UI_MIME:
                out.append({"kind": "a2ui", "data": data_val})
        elif isinstance(root, FilePart):
            uri = getattr(getattr(root, "file", None), "uri", None)
            if uri:
                out.append({"kind": "text", "text": uri})
    return out


BUCKET_NAME = os.environ.get("GCS_BUCKET", "cardex-spots-qwiklabs-gcp-04-6f324b699fdd")
_storage_client = None


def _get_storage_client():
    global _storage_client
    if _storage_client is None:
        from google.cloud import storage
        _storage_client = storage.Client()
    return _storage_client


def _upload_base64_image(b64_str: str) -> str:
    """Uploads base64 image data to GCS and returns its public HTTPS URL."""
    import base64
    mime_type = "image/jpeg"
    ext = "jpg"
    data = b64_str
    if "," in b64_str and "base64" in b64_str:
        header, data = b64_str.split(",", 1)
        if "png" in header:
            mime_type = "image/png"
            ext = "png"
        elif "webp" in header:
            mime_type = "image/webp"
            ext = "webp"
        elif "jpeg" in header or "jpg" in header:
            mime_type = "image/jpeg"
            ext = "jpg"
    image_bytes = base64.b64decode(data)
    client = _get_storage_client()
    bucket = client.bucket(BUCKET_NAME)
    blob_name = f"spots/{uuid.uuid4().hex[:16]}.{ext}"
    blob = bucket.blob(blob_name)
    blob.upload_from_string(image_bytes, content_type=mime_type)
    return f"https://storage.googleapis.com/{BUCKET_NAME}/{blob_name}"


@app.get("/healthz")
@app.get("/health")
async def healthz():
    """Health check probe endpoint for container orchestration and uptime monitors."""
    import datetime
    redis_ok = is_redis_available()
    return JSONResponse(
        content={
            "status": "healthy",
            "service": "cardex",
            "version": "2.0.0",
            "redis_cache": "connected" if redis_ok else "fallback_in_memory",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
        status_code=200,
    )


@app.get("/api/leaderboard")
async def get_leaderboard_endpoint(req: Request):
    """Retrieves top spotters ranked by total points, with 60s Redis caching."""
    limit_param = req.query_params.get("limit", "10")
    try:
        limit = max(1, min(100, int(limit_param)))
    except ValueError:
        limit = 10

    cached = get_cached_leaderboard()
    if cached is not None:
        return JSONResponse(
            content={"leaderboard": cached[:limit], "cached": True, "limit": limit},
            headers={"X-Cache-Status": "HIT", "Cache-Control": "public, max-age=60"},
        )

    try:
        rankings = firestore_db.get_leaderboard_rankings(limit=limit)
    except Exception as e:
        rankings = []

    set_cached_leaderboard(rankings, ttl=60)
    return JSONResponse(
        content={"leaderboard": rankings, "cached": False, "limit": limit},
        headers={"X-Cache-Status": "MISS", "Cache-Control": "public, max-age=60"},
    )


@app.get("/api/leaderboard/loudest")
async def get_loudest_leaderboard_endpoint(req: Request):
    """Retrieves top 50 loudest verified exhaust car spots globally (Exhaust Hall of Fame), with 60s Redis caching."""
    limit_param = req.query_params.get("limit", "50")
    engine_type = req.query_params.get("engine_type", None)
    timeframe = req.query_params.get("timeframe", "all")

    try:
        limit = max(1, min(100, int(limit_param)))
    except ValueError:
        limit = 50

    cached = get_cached_loudest_leaderboard(engine_type=engine_type, timeframe=timeframe)
    if cached is not None:
        return JSONResponse(
            content={
                "leaderboard": cached[:limit],
                "cached": True,
                "limit": limit,
                "engine_type": engine_type or "all",
                "timeframe": timeframe or "all",
            },
            headers={"X-Cache-Status": "HIT", "Cache-Control": "public, max-age=60"},
        )

    try:
        rankings = firestore_db.get_loudest_exhaust_leaderboard(
            limit=limit, engine_type=engine_type, timeframe=timeframe
        )
    except Exception as e:
        logger.warning("Error retrieving loudest leaderboard: %s", e)
        rankings = []

    set_cached_loudest_leaderboard(rankings, engine_type=engine_type, timeframe=timeframe, ttl=60)
    return JSONResponse(
        content={
            "leaderboard": rankings,
            "cached": False,
            "limit": limit,
            "engine_type": engine_type or "all",
            "timeframe": timeframe or "all",
        },
        headers={"X-Cache-Status": "MISS", "Cache-Control": "public, max-age=60"},
    )



@app.post("/api/auth/signup")
@app.post("/api/auth/register")
async def signup(req: Request):
    """Registers a new spotter and initializes their garage and points balance."""
    try:
        body = await req.json()
        username = body.get("username") or body.get("spotter_handle") or ""
        email = body.get("email", "")
        password = body.get("password", "")
        result = auth_db.register_user(username=username, email=email, password=password)
        return JSONResponse({"success": True, **result})
    except ValueError as ve:
        return JSONResponse({"success": False, "error": str(ve), "detail": str(ve)}, status_code=400)
    except Exception as e:
        return JSONResponse({"success": False, "error": f"Registration failed: {str(e)}", "detail": f"Registration failed: {str(e)}"}, status_code=500)


@app.post("/api/auth/signin")
@app.post("/api/auth/login")
async def signin(req: Request):
    """Authenticates a spotter with username/email and password."""
    try:
        body = await req.json()
        username_or_email = body.get("username_or_email") or body.get("username") or body.get("email") or ""
        password = body.get("password", "")
        if not username_or_email or not password:
            return JSONResponse({"success": False, "error": "Username/email and password are required.", "detail": "Username/email and password are required."}, status_code=400)
        result = auth_db.authenticate_user(username_or_email=username_or_email, password=password)
        return JSONResponse({"success": True, **result})
    except ValueError as ve:
        return JSONResponse({"success": False, "error": str(ve), "detail": str(ve)}, status_code=401)
    except Exception as e:
        return JSONResponse({"success": False, "error": f"Authentication failed: {str(e)}", "detail": f"Authentication failed: {str(e)}"}, status_code=500)


@app.get("/api/auth/me")
async def get_current_user(req: Request):
    """Returns profile and points for the currently signed-in spotter."""
    auth_header = req.headers.get("Authorization", "")
    token = None
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    if not token:
        token = req.query_params.get("token")
    user_id = auth_db.validate_session(token) if token else None
    if not user_id:
        # Check query param fallback
        param_user = req.query_params.get("user_id")
        if param_user:
            profile = auth_db.get_user_profile(param_user)
            return JSONResponse({"authenticated": False, "user": profile})
        return JSONResponse({"authenticated": False, "user": None})

    profile = auth_db.get_user_profile(user_id)
    return JSONResponse({"authenticated": True, "user": profile})


@app.post("/api/auth/signout")
async def signout(req: Request):
    """Revokes the current session token."""
    auth_header = req.headers.get("Authorization", "")
    token = None
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    if token:
        auth_db.revoke_session(token)
    return JSONResponse({"success": True})


@app.get("/api/user/submissions")
async def user_submissions(req: Request):
    """Retrieves all vehicle sightings and submissions logged by the user."""
    user_id = req.query_params.get("user_id")
    if not user_id:
        auth_header = req.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
            user_id = auth_db.validate_session(token)
    if not user_id:
        return JSONResponse({"error": "Authentication or user_id required"}, status_code=401)

    submissions = auth_db.get_user_submissions(user_id)
    profile = auth_db.get_user_profile(user_id)
    return JSONResponse({
        "user_id": user_id,
        "username": profile.get("username", user_id),
        "total_points": profile.get("total_points", 0),
        "points": profile.get("total_points", 0),
        "rank": profile.get("rank", "Rookie Spotter"),
        "submissions": submissions,
        "count": len(submissions),
        "total_spots": len(submissions),
    })


@app.get("/api/user/stats")
async def user_stats(req: Request):
    """Retrieves spot stats, rarity breakdown, and total points."""
    user_id = req.query_params.get("user_id")
    if not user_id:
        auth_header = req.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
            user_id = auth_db.validate_session(token)
    if not user_id:
        return JSONResponse({"error": "Authentication or user_id required"}, status_code=401)

    stats = auth_db.get_user_stats(user_id)
    return JSONResponse(stats)


@app.get("/api/user/quota")
async def get_user_quota_endpoint(req: Request):
    """Retrieves user's rolling 24h scan quota, remaining scans, and tier."""
    user_id = req.query_params.get("user_id")
    if not user_id:
        auth_header = req.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
            user_id = auth_db.validate_session(token)
    user_id = user_id or "spotter_1"
    cached = get_cached_user_quota(user_id)
    if cached is not None:
        return JSONResponse(cached, headers={"X-Cache-Status": "HIT"})
    quota = quota_limiter.get_user_quota(user_id)
    return JSONResponse(quota, headers={"X-Cache-Status": "MISS"})



@app.post("/api/user/refill")
async def refill_user_quota_endpoint(req: Request):
    """Purchases or grants consumable refill scan packs."""
    body = await req.json()
    user_id = body.get("user_id")
    if not user_id:
        auth_header = req.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
            user_id = auth_db.validate_session(token)
    user_id = user_id or "spotter_1"
    packs = int(body.get("packs", 1))
    added = packs * 5
    res = quota_limiter.add_refill_credits(user_id, added)
    return JSONResponse(res)


@app.post("/api/user/upgrade-pro")
async def upgrade_user_pro_endpoint(req: Request):
    """Toggles or sets the user's tier (free vs. pro)."""
    body = await req.json()
    user_id = body.get("user_id")
    if not user_id:
        auth_header = req.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
            user_id = auth_db.validate_session(token)
    user_id = user_id or "spotter_1"
    tier = body.get("tier", "pro")
    res = quota_limiter.set_user_tier(user_id, tier)
    return JSONResponse(res)


@app.post("/api/billing/stripe/create-checkout-session")
@app.post("/api/billing/create-checkout-session")
async def create_checkout_session_endpoint(req: Request):
    """Creates a Stripe Checkout Session for Pro subscription ($9.99/mo) or Refill pack ($1.99)."""
    body = await req.json()
    session_type = body.get("type", "subscription")

    user_id = body.get("user_id")
    if not user_id:
        auth_header = req.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
            user_id = auth_db.validate_session(token)
    user_id = user_id or "spotter_1"

    success_url = str(req.base_url)
    res = billing.create_checkout_session(
        user_id=user_id,
        session_type=session_type,
        success_url=success_url,
    )
    return JSONResponse(res)


@app.post("/api/billing/stripe/webhook")
@app.post("/api/billing/webhook")
async def stripe_webhook_endpoint(req: Request):
    """Stripe webhook handler for checkout.session.completed."""
    payload = await req.body()
    sig_header = req.headers.get("stripe-signature")
    try:
        res = billing.handle_stripe_webhook(payload, sig_header)
        return JSONResponse(res)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/billing/revenuecat-webhook")
async def revenuecat_webhook_endpoint(req: Request):
    """RevenueCat webhook handler for Apple In-App Purchases."""
    auth_header = req.headers.get("Authorization", "")
    payload = await req.body()
    try:
        res = billing.handle_revenuecat_webhook(payload, auth_header)
        return JSONResponse(res)
    except ValueError as e:
        status_code = 401 if "Unauthorized" in str(e) or "Authorization" in str(e) else 400
        return JSONResponse({"error": str(e)}, status_code=status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)



@app.get("/api/reviews/pending")
async def pending_disputes_endpoint(req: Request):
    """Retrieves disputes waiting for Master Spotter consensus review."""
    disputes = firestore_db.get_pending_disputes(limit=25)
    return JSONResponse(disputes)


@app.post("/api/reviews/vote")
async def vote_dispute_endpoint(req: Request):
    """Casts a blind vote on a pending dispute."""
    body = await req.json()
    review_id = body.get("review_id")
    if not review_id:
        return JSONResponse({"error": "review_id required"}, status_code=400)
    user_id = body.get("user_id")
    if not user_id:
        auth_header = req.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
            user_id = auth_db.validate_session(token)
    user_id = user_id or "spotter_1"
    vote_agree = bool(body.get("vote_agree", True))
    reviewer_rank = body.get("reviewer_rank", "Master Spotter")
    comment = body.get("comment", "")
    res = firestore_db.cast_dispute_vote(review_id, user_id, vote_agree, reviewer_rank, comment)
    return JSONResponse(res)


@app.post("/api/reviews/dispute")
async def submit_dispute_endpoint(req: Request):
    """Submits a car spotting classification dispute to the review queue."""
    body = await req.json()
    car_name = body.get("car_name", "Contested Sighting")
    issue_desc = body.get("issue_description", "")
    prop_corr = body.get("proposed_correction", "")
    user_id = body.get("user_id")
    if not user_id:
        auth_header = req.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
            user_id = auth_db.validate_session(token)
    user_id = user_id or "spotter_1"
    res = firestore_db.submit_dispute_review(
        car_name=car_name,
        issue_description=issue_desc,
        proposed_correction=prop_corr,
        user_id=user_id,
        spot_id=body.get("spot_id"),
        image_url=body.get("image_url"),
        proposed_make=body.get("proposed_make"),
        proposed_model=body.get("proposed_model"),
        proposed_trim=body.get("proposed_trim"),
        proposed_color=body.get("proposed_color"),
    )
    return JSONResponse(res)


@app.get("/api/radar")
async def get_radar_sightings_endpoint(req: Request):
    """Retrieves privacy-quantized vehicle sightings within a given radius logged in the trailing 7 days.

    Query parameters:
      lat: float (default: 43.7384 - Monaco GP Circuit)
      lng: float (default: 7.4246 - Monaco Harbour)
      radius_km: float (default 10.0, max 50.0)

    Returns JSON list of nearby car spotting contacts:
      [{"id": ..., "make_model": ..., "rarity_tier": ..., "lat": ..., "lng": ..., "age_hours": ..., "distance_km": ...}]
    """
    try:
        lat_str = req.query_params.get("lat")
        lng_str = req.query_params.get("lng")
        if not lat_str or not lng_str:
            lat = 43.7384
            lng = 7.4246
        else:
            lat = float(lat_str)
            lng = float(lng_str)

        radius_km = float(req.query_params.get("radius_km", 10.0))
        radius_km = max(0.5, min(radius_km, 50.0))
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": f"Invalid query parameters: {e}"}, status_code=400)

    sightings = firestore_db.query_radar_sightings(
        lat=lat,
        lng=lng,
        radius_km=radius_km,
        max_age_days=7,
    )
    return JSONResponse(sightings)


@app.post("/chat")
@app.post("/api/spots/upload")
@app.post("/api/spot")
async def chat(req: Request):
    body = await req.json()
    message = body.get("message", "")
    image_input = body.get("image")
    audio_input = body.get("audio")

    # Resolve authenticated spotter identity
    auth_header = req.headers.get("Authorization", "")
    token = None
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    session_user_id = auth_db.validate_session(token) if token else None

    user_id = session_user_id or body.get("user_id") or "spotter_1"
    user_profile = auth_db.get_user_profile(user_id)
    username = user_profile.get("username") or body.get("username") or user_id

    # 1. Quota Pre-Check if scanning an image
    if image_input:
        allowed, reason, quota_info = quota_limiter.check_and_reserve_scan(user_id)
        if not allowed:
            paywall_card = a2ui_utils.build_paywall_card(quota_info)
            return JSONResponse(
                {
                    "parts": [
                        {
                            "kind": "text",
                            "text": f"⚠️ **Daily Scan Limit Reached ({quota_info.get('scans_today', 5)}/{quota_info.get('daily_limit', 5)} Scans)**\n{reason}\n\nTo preserve platform economics and prevent server abuse, free accounts are limited to 5 scans per rolling 24 hours. Unlock unlimited scans with CarDex Pro or grab an instant 5-scan refill pack!",
                        },
                        {
                            "kind": "a2ui",
                            "data": paywall_card,
                        },
                    ],
                    "quota_exceeded": True,
                    "quota": quota_info,
                    "user": user_profile,
                },
                status_code=429,
            )

    parts: list[dict] = []

    # 2. Decode and Anti-Cheat Pre-Check for images
    uploaded_image_url = None
    decoded_bytes = None
    acoustic_telemetry = body.get("acoustic_telemetry")
    if image_input:
        # Pre-screen for screen-captures (Moiré) and duplicate hashes
        try:
            raw_data = image_input.strip()
            if raw_data.startswith("data:") and ";base64," in raw_data:
                _, b64 = raw_data.split(";base64,", 1)
                decoded_bytes = base64.b64decode(b64)
            elif not raw_data.startswith("http"):
                decoded_bytes = base64.b64decode(raw_data)

            if decoded_bytes:
                integrity = anti_cheat.verify_image_integrity(decoded_bytes)
                if not integrity["passed"]:
                    return JSONResponse({
                        "parts": [
                            {
                                "kind": "text",
                                "text": f"🛡️ **Anti-Cheat Integrity Alert**\n{integrity['message']}\n\n*Forensic Status:* `{integrity.get('flag')}`\n*Note:* CarDex requires genuine live sightings. Photos taken off monitors, screens, or duplicate submissions are flagged by our computer-vision integrity filters.",
                            }
                        ],
                        "anti_cheat_triggered": True,
                        "error": integrity["message"],
                        "flag": integrity.get("flag"),
                        "user": user_profile,
                    })
        except Exception as e:
            print("Anti-cheat pre-screening warning:", e)

        if image_input.startswith("http://") or image_input.startswith("https://"):
            uploaded_image_url = image_input
        else:
            try:
                uploaded_image_url = _upload_base64_image(image_input)
            except Exception as e:
                print("Failed to upload image to GCS:", e)

    # 2.5 Acoustic Classification & IEC 61672 Telemetry Pre-Check for audio exhaust recording
    audio_analysis = None
    if audio_input:
        try:
            import audio_classifier
            mime_type = "audio/wav"
            raw_audio = str(audio_input).strip()
            decoded_audio = None
            if raw_audio.startswith("data:") and ";base64," in raw_audio:
                hdr, b64 = raw_audio.split(";base64,", 1)
                if "webm" in hdr: mime_type = "audio/webm"
                elif "mp3" in hdr: mime_type = "audio/mp3"
                elif "aac" in hdr: mime_type = "audio/aac"
                elif "ogg" in hdr: mime_type = "audio/ogg"
                elif "wav" in hdr: mime_type = "audio/wav"
                decoded_audio = base64.b64decode(b64)
            else:
                decoded_audio = base64.b64decode(raw_audio)

            if decoded_audio:
                audio_analysis = audio_classifier.classify_engine_note(
                    decoded_audio,
                    claimed_model=message or "",
                    mime_type=mime_type,
                )
                computed_telemetry = audio_classifier.compute_iec_acoustic_telemetry(
                    decoded_audio,
                    claimed_model=message or "",
                    mime_type=mime_type,
                )
                if not acoustic_telemetry:
                    acoustic_telemetry = computed_telemetry
                else:
                    acoustic_telemetry.update(computed_telemetry)
        except Exception as e:
            print("Frontend audio analysis warning:", e)


    # Build agent prompt with explicit spotter identity, location context, and audio analysis
    req_lat = body.get("latitude") or body.get("lat")
    req_lng = body.get("longitude") or body.get("lng")
    loc_ctx = f", lat={req_lat}, lng={req_lng}" if req_lat and req_lng else ""
    acoustic_ctx = ""
    if audio_analysis:
        acoustic_ctx = f", exhaust_engine='{audio_analysis.get('engine_config')}', exhaust_loudness={audio_analysis.get('peak_loudness_dbfs')}dBFS, acoustic_match={audio_analysis.get('acoustic_match')}"
    spotter_tag = f"[Spotter Context: user_id='{user_id}', username='{username}'{loc_ctx}{acoustic_ctx}]"
    if uploaded_image_url:
        loc_phrase = f" at coordinates ({req_lat}, {req_lng})" if req_lat and req_lng else ""
        sound_phrase = f" with verified {audio_analysis.get('engine_config')} exhaust sound note (+25% acoustic verification bonus)" if (audio_analysis and audio_analysis.get("acoustic_match")) else ""
        if message.strip():
            agent_text = (
                f"{spotter_tag} I spotted this car{loc_phrase}{sound_phrase} and took this photo: {uploaded_image_url}. {message.strip()} "
                f"Please identify the car, award rarity points, and record the spot in my garage (user_id='{user_id}')."
            )
        else:
            agent_text = (
                f"{spotter_tag} Please identify the car in this photo I spotted{loc_phrase}{sound_phrase} and record the spot in my garage (user_id='{user_id}'): {uploaded_image_url}"
            )
    else:
        lower_msg = message.lower()
        if any(term in lower_msg for term in ["garage", "my spot", "collection", "my score", "my points"]):
            agent_text = f"{spotter_tag} {message.strip()} (Retrieve garage/spots for user_id='{user_id}')"
        else:
            agent_text = f"{spotter_tag} {message}"

    async with httpx.AsyncClient(headers=_auth_headers(), timeout=120) as client:
        card = await _get_card(client)
        factory = ClientFactory(
            ClientConfig(
                supported_transports=[
                    TransportProtocol.jsonrpc,
                    TransportProtocol.http_json,
                ],
                httpx_client=client,
            )
        )
        a2a_client = factory.create(card)

        msg = Message(
            message_id=str(uuid.uuid4()),
            role=Role.user,
            parts=[Part(root=TextPart(text=agent_text))],
            context_id=_contexts.get(user_id),
        )

        last_task = None
        got_artifact_update = False
        async for event in a2a_client.send_message(msg):
            print("A2A EVENT RECEIVED:", type(event), event)
            if not isinstance(event, tuple):
                if isinstance(event, Message):
                    parts.extend(_extract_parts(event.parts))
                continue
            task, update = event
            print("TASK:", type(task), "UPDATE:", type(update), update)
            if task is not None:
                last_task = task
                if getattr(task, "context_id", None):
                    _contexts[user_id] = task.context_id
            if isinstance(update, TaskArtifactUpdateEvent):
                got_artifact_update = True
                parts.extend(_extract_parts(update.artifact.parts))
            elif isinstance(update, Message):
                got_artifact_update = True
                parts.extend(_extract_parts(update.parts))

        # Fallback: pull parts from the final task's artifacts, output, or history.
        if not parts and last_task is not None:
            for artifact in getattr(last_task, "artifacts", None) or []:
                parts.extend(_extract_parts(artifact.parts))
            if not parts and getattr(last_task, "output", None):
                parts.extend(_extract_parts(last_task.output))
            if not parts and getattr(last_task, "history", None):
                for h_msg in reversed(last_task.history):
                    h_parts = getattr(h_msg, "parts", None) or []
                    extracted = _extract_parts(h_parts)
                    valid_parts = [p for p in extracted if p.get("kind") in ("text", "a2ui")]
                    if valid_parts:
                        parts.extend(valid_parts)
                        break

    if not parts:
        parts = [{"kind": "text", "text": "(The agent didn't return a reply.)"}]

    if decoded_bytes:
        try:
            paint_match = paint_matcher.match_oem_paint_color(decoded_bytes)
            de00 = paint_match.get("delta_e00", 999.0)
            has_paint_card = any(
                p.get("kind") == "a2ui" and p.get("data", {}).get("type") == "verified_paint_match"
                for p in parts
            )
            if not has_paint_card and paint_match.get("matched") and de00 <= 2.0:
                rgb = paint_match.get("rgb", [58, 28, 68])
                hex_color = f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
                paint_card = a2ui_utils.build_paint_match_card(
                    paint_code=paint_match.get("paint_code", ""),
                    commercial_name=paint_match.get("paint_name", ""),
                    program=paint_match.get("program", "Factory Finish"),
                    hex_color=hex_color,
                    delta_e=de00,
                    multiplier=paint_match.get("multiplier", 1.35),
                )
                parts.append({"kind": "a2ui", "data": paint_card})
        except Exception:
            pass

    # Inject holographic acoustic certification card if telemetry present or acoustic note matched
    if acoustic_telemetry or (audio_analysis and audio_analysis.get("acoustic_match")):
        telem = acoustic_telemetry or {}
        eng_config = telem.get("engine_config") or (audio_analysis.get("engine_config") if audio_analysis else "Sport V8")
        conf = float(telem.get("confidence") or (audio_analysis.get("confidence") if audio_analysis else 0.95))
        loud_dbfs = float(audio_analysis.get("peak_loudness_dbfs") if audio_analysis else (telem.get("peak_dba", 95.0) - 97.0))
        sig = audio_analysis.get("acoustic_signature") if audio_analysis else f"IEC 61672-1 Calibrated Engine Note ({telem.get('tier', 'Exhaust Dyno')})"
        rev_lim = bool(audio_analysis.get("rev_limiter_detected") if audio_analysis else False)
        
        has_acoustic_card = any(
            p.get("kind") == "a2ui" and p.get("data", {}).get("type") == "acoustic_verification"
            for p in parts
        )
        if not has_acoustic_card:
            acoustic_card = a2ui_utils.build_acoustic_verification_card(
                engine_config=eng_config,
                confidence=conf,
                loudness_dbfs=loud_dbfs,
                signature=sig,
                rev_limiter=rev_lim,
                bonus_multiplier=1.25,
                peak_dba=float(telem.get("peak_dba", 0.0)),
                peak_dbc=float(telem.get("peak_dbc", 0.0)),
                laeq=float(telem.get("laeq", 0.0)),
                tier=telem.get("tier", "Sport Exhaust"),
                badge=telem.get("badge", "SPORT_EXHAUST"),
                bonus_points=int(telem.get("bonus_points", 0)),
                ear_bleeder=bool(telem.get("ear_bleeder", False)),
                cadence_matched=bool(telem.get("cadence_matched", False)),
                dominant_hz=float(telem.get("dominant_hz", 0.0)),
                spectral_flatness=float(telem.get("spectral_flatness", 0.0)),
                spectral_signature_sample=telem.get("spectral_signature_sample", []),
            )
            parts.append({"kind": "a2ui", "data": acoustic_card})

    # Inject chassis verification card if markings or VIN detected
    chassis_markings = None
    if decoded_bytes:
        try:
            from app.car_tools import extract_chassis_markings
            has_chassis_card = any(
                p.get("kind") == "a2ui" and p.get("data", {}).get("type") == "chassis_verification"
                for p in parts
            )
            if not has_chassis_card:
                chassis_markings = extract_chassis_markings(decoded_bytes, user_id=user_id)
                if chassis_markings and (chassis_markings.get("production_number") or chassis_markings.get("vin")):
                    chassis_card = a2ui_utils.build_chassis_verification_card(
                        make_model_edition=chassis_markings.get("make_model_edition") or "Verified Chassis",
                        unit_number=chassis_markings.get("production_number"),
                        vin=chassis_markings.get("vin"),
                        edition=chassis_markings.get("edition"),
                        vin_decoded=chassis_markings.get("vin_decoded"),
                        badge_awarded=chassis_markings.get("badge_awarded"),
                        first_finder_multiplier=chassis_markings.get("first_finder_multiplier", 1.0),
                    )
                    parts.append({"kind": "a2ui", "data": chassis_card})
        except Exception as e:
            logger.warning("Chassis marking extraction in frontend error: %s", e)

    # Deduct quota scan credit on successful spotting scan
    latest_quota = None
    if image_input:
        latest_quota = quota_limiter.commit_scan_deduction(user_id)
    else:
        latest_quota = quota_limiter.get_user_quota(user_id)

    # Refresh user stats from Firestore to return live updated score
    updated_profile = auth_db.get_user_profile(user_id)
    return JSONResponse({
        "parts": parts,
        "image_url": uploaded_image_url,
        "acoustic_analysis": audio_analysis,
        "acoustic_telemetry": acoustic_telemetry,
        "chassis_markings": chassis_markings,
        "user": updated_profile,
        "quota": latest_quota,
    })


# Serve the chat UI (keep this mount last so /chat wins).
_static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_static_dir), name="static_prefix")
app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
