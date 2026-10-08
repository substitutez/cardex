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
                try:
                    start = text_val.index("<a2ui-json>") + len("<a2ui-json>")
                    end = text_val.index("</a2ui-json>")
                    json_str = text_val[start:end].strip()
                    card_data = json.loads(json_str)
                    out.append({"kind": "a2ui", "data": {"surfaceUpdate": card_data}})
                    before = text_val[:text_val.index("<a2ui-json>")].strip()
                    after = text_val[end + len("</a2ui-json>"):].strip()
                    if before:
                        out.append({"kind": "text", "text": before})
                    if after:
                        out.append({"kind": "text", "text": after})
                    continue
                except Exception as e:
                    print("Failed to parse <a2ui-json>:", e)
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


@app.post("/chat")
async def chat(req: Request):
    body = await req.json()
    message = body.get("message", "")
    image_input = body.get("image")
    user_id = body.get("user_id") or "web-user"
    parts: list[dict] = []

    uploaded_image_url = None
    if image_input:
        if image_input.startswith("http://") or image_input.startswith("https://"):
            uploaded_image_url = image_input
        else:
            try:
                uploaded_image_url = _upload_base64_image(image_input)
            except Exception as e:
                print("Failed to upload image to GCS:", e)

    # Build agent prompt
    if uploaded_image_url:
        if message.strip():
            agent_text = (
                f"I spotted this car and took this photo: {uploaded_image_url}. {message.strip()}"
            )
        else:
            agent_text = (
                f"Please identify the car in this photo I spotted and record the spot: {uploaded_image_url}"
            )
    else:
        agent_text = message

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

        # Non-streaming fallback: pull parts from the final task's artifacts.
        if not got_artifact_update and last_task is not None:
            print("FALLBACK: last_task =", last_task)
            for artifact in getattr(last_task, "artifacts", None) or []:
                parts.extend(_extract_parts(artifact.parts))
            # Also check if task has messages or output
            if not parts and getattr(last_task, "output", None):
                parts.extend(_extract_parts(last_task.output))

    if not parts:
        parts = [{"kind": "text", "text": "(The agent didn't return a reply.)"}]
    return JSONResponse({"parts": parts, "image_url": uploaded_image_url})


# Serve the chat UI (keep this mount last so /chat wins).
app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
