"""Make an ADK agent's A2UI output render in the ADK dev UI (`adk web`).

`adk web` has a built-in A2UI renderer, but it only fires when a response part is
a text/plain Blob wrapped in <a2a_datapart_json>...</a2a_datapart_json> with
custom_metadata {"a2a:response": "true"}. This callback takes the A2UI JSON the
model emits as text and rewraps it into exactly that format.

Wire it up with `after_model_callback=a2ui_callback` on your Agent. Pin the A2UI
schema to **version 0.8** (this callback keys off v0.8 messages: beginRendering,
surfaceUpdate, dataModelUpdate). Copy this file next to your agent.py.

Robustness notes (why this file is more than a one-liner):
  * The model often *self-wraps* — it imitates the wrapped format it sees in its
    own history and emits `<a2a_datapart_json>{"kind":"data","data":{...}}</...>`
    or concatenated JSON objects. This callback strips those tags, splits
    concatenated objects, and unwraps `{"kind":"data","data":{...}}` envelopes.
  * On large / deeply-nested surfaces the model sometimes emits *invalid* JSON
    (e.g. a `]` where `}}` belonged). When that happens we can only partially
    parse the payload and would be left with a lone `beginRendering` and no body,
    which renders as a BLANK surface. To avoid that bad UX we require a surface to
    actually have renderable content; if it doesn't, we return a short plain-text
    fallback instead of a blank card.
"""

import json
import re

from google.genai import types
try:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.models.llm_response import LlmResponse
except ModuleNotFoundError:
    CallbackContext = object
    LlmResponse = object

# A2UI message kinds this renderer understands (v0.8).
_A2UI_KEYS = ("beginRendering", "surfaceUpdate", "dataModelUpdate", "deleteSurface")

# Tags the model may wrap around its output (its own render wrapper, or the SDK's).
_TAG_RE = re.compile(r"</?(?:a2a_datapart_json|a2ui-json)>")

# Shown when the model emitted A2UI we could not fully parse (usually malformed
# JSON on a large surface). Better than a blank card or a wall of raw JSON.
_FALLBACK_TEXT = (
    "I couldn't render that view. Could you ask again, maybe for a simpler summary?"
)

# adk web can only render an <Image> whose url is a real, fetchable http(s) link.
# Models frequently emit an Image pointing at a bare artifact filename or a
# relative path (e.g. "recipe_image_123.png"), which renders as a broken-image
# icon. We swap those for this short note; the generated media still shows up in
# the adk web Artifacts panel.
_HTTP_URL_RE = re.compile(r"^https?://", re.I)
_IMAGE_NOTE = "Image generated — open the Artifacts panel to view it."


def _wrap_a2ui_part(a2ui_message: dict) -> types.Part:
    """Wrap a single A2UI message for rendering in adk web."""
    datapart_json = json.dumps(
        {
            "kind": "data",
            "metadata": {"mimeType": "application/json+a2ui"},
            "data": a2ui_message,
        }
    )
    blob_data = (
        b"<a2a_datapart_json>" + datapart_json.encode("utf-8") + b"</a2a_datapart_json>"
    )
    return types.Part(
        inline_data=types.Blob(
            data=blob_data,
            mime_type="text/plain",
        )
    )


def _iter_json_values(text: str):
    """Yield top-level JSON values from a string of concatenated objects/arrays.

    Tolerates junk between values (tags, whitespace, commas). Stops at the first
    unparseable value, so a valid prefix is still returned (partial recovery).
    """
    decoder = json.JSONDecoder()
    idx = 0
    n = len(text)
    while idx < n:
        while idx < n and text[idx] not in "{[":
            idx += 1
        if idx >= n:
            break
        try:
            value, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            break  # malformed from here on; keep whatever we already parsed
        yield value
        idx = end


def _extract_a2ui_messages(text: str) -> list[dict]:
    """Pull A2UI messages out of the model's raw text output.

    Handles: markdown fences, self-wrap tags, plain arrays, concatenated objects,
    and `{"kind":"data","data":{...}}` envelopes. Returns a flat list of A2UI
    messages (each keyed by one of _A2UI_KEYS).
    """
    # Strip markdown fences.
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
    # Strip any wrapper tags so the JSON scanner sees clean payload.
    text = _TAG_RE.sub("", text).strip()

    values: list = []
    for value in _iter_json_values(text):
        if isinstance(value, list):
            values.extend(value)
        else:
            values.append(value)

    messages: list[dict] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        # Unwrap {"kind":"data","data":{...}} envelopes the model may imitate.
        inner = value.get("data")
        if isinstance(inner, dict) and any(k in inner for k in _A2UI_KEYS):
            messages.append(inner)
        elif any(k in value for k in _A2UI_KEYS):
            messages.append(value)
    return messages


def _sanitize_image_components(messages: list[dict]) -> None:
    """Replace un-fetchable <Image> components with a short <Text> note, in place.

    Keeps the component's `id` so any parent child-reference still resolves; only
    the component body changes from Image to Text. An Image is kept only if its
    url is a literal http(s) link (a path-bound or relative/filename url can't be
    fetched by adk web and would render as a broken-image icon).
    """
    for m in messages:
        surface = m.get("surfaceUpdate")
        if not isinstance(surface, dict):
            continue
        for c in surface.get("components") or []:
            if not isinstance(c, dict):
                continue
            comp = c.get("component")
            if not isinstance(comp, dict) or "Image" not in comp:
                continue
            img = comp.get("Image")
            url = img.get("url") if isinstance(img, dict) else None
            literal = url.get("literalString") if isinstance(url, dict) else None
            if isinstance(literal, str) and _HTTP_URL_RE.match(literal):
                continue  # real link -> leave the Image alone
            c["component"] = {
                "Text": {
                    "text": {"literalString": _IMAGE_NOTE},
                    "usageHint": "body",
                }
            }


def _component_ids_and_refs(components: list) -> tuple[set, set]:
    """Return (defined ids, referenced child ids) for a component list."""
    ids: set = set()
    refs: set = set()
    for c in components:
        if not isinstance(c, dict):
            continue
        if "id" in c:
            ids.add(c["id"])
        comp = c.get("component")
        if not isinstance(comp, dict):
            continue
        for spec in comp.values():  # e.g. {"Card": {...}} / {"Column": {...}}
            if not isinstance(spec, dict):
                continue
            if isinstance(spec.get("child"), str):
                refs.add(spec["child"])
            children = spec.get("children")
            if isinstance(children, dict):
                for cid in children.get("explicitList") or []:
                    if isinstance(cid, str):
                        refs.add(cid)
    return ids, refs


def _surface_is_renderable(messages: list[dict]) -> bool:
    """True only if the messages form a surface adk web can actually draw.

    Guards against the two blank-card failure modes flash models produce:
      * a lone `beginRendering` with no `surfaceUpdate` body (malformed JSON), and
      * a surface whose `root` (or a child ref) points at an id that was never
        defined — the whole tree then renders as nothing.
    dataModelUpdate / deleteSurface messages are always considered renderable.
    """
    all_ids: set = set()
    all_refs: set = set()
    roots: list = []
    has_body = False
    for m in messages:
        if "dataModelUpdate" in m or "deleteSurface" in m:
            return True
        br = m.get("beginRendering")
        if isinstance(br, dict) and isinstance(br.get("root"), str):
            roots.append(br["root"])
        su = m.get("surfaceUpdate")
        if isinstance(su, dict) and su.get("components"):
            has_body = True
            ids, refs = _component_ids_and_refs(su["components"])
            all_ids |= ids
            all_refs |= refs
    if not has_body:
        return False
    if any(root not in all_ids for root in roots):
        return False  # root points at an undefined component -> blank
    if all_refs - all_ids:
        return False  # dangling child references -> blank
    return True


def a2ui_callback(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> LlmResponse | None:
    """Convert A2UI JSON in text output to rendered components (or a clean fallback)."""
    if not llm_response.content or not llm_response.content.parts:
        return None

    for part in llm_response.content.parts:
        text = (part.text or "").strip()
        if not text:
            continue
        # Cheap gate: only touch parts that look like A2UI, leave prose alone.
        if not any(k in text for k in _A2UI_KEYS):
            continue

        messages = _extract_a2ui_messages(text)
        if not messages:
            continue

        # Turn un-fetchable <Image> URLs into a text note (no broken-image icons).
        _sanitize_image_components(messages)

        if not _surface_is_renderable(messages):
            # We recognized A2UI but couldn't recover a renderable surface — the
            # model emitted invalid JSON, a missing surface body, or an undefined
            # root/child reference. Return clean text instead of a blank card.
            return LlmResponse(
                content=types.Content(
                    role="model", parts=[types.Part(text=_FALLBACK_TEXT)]
                )
            )

        new_parts = [_wrap_a2ui_part(m) for m in messages]
        return LlmResponse(
            content=types.Content(role="model", parts=new_parts),
            custom_metadata={"a2a:response": "true"},
        )

    return None


process_model_response = a2ui_callback


def build_paint_match_card(
    paint_code: str,
    commercial_name: str,
    program: str,
    hex_color: str,
    delta_e: float,
    multiplier: float = 1.35,
) -> dict:
    """Creates a Holographic 'Verified Paint Match' A2UI card payload."""
    mult_pct = int(round((multiplier - 1.0) * 100))
    mult_badge = f"+{mult_pct}% Paint Multiplier" if mult_pct > 0 else "Standard Finish"
    return {
        "type": "verified_paint_match",
        "paint_code": paint_code,
        "commercial_name": commercial_name,
        "program": program,
        "hex_color": hex_color,
        "delta_e": round(delta_e, 2),
        "delta_e00": round(delta_e, 2),
        "multiplier": multiplier,
        "multiplier_badge": mult_badge,
        "badge_text": f"Code {paint_code}: {commercial_name} ({program})",
    }


def build_point_breakdown_card(
    car_name: str,
    base_points: int,
    production_units: int,
    recent_spots_30d: int,
    final_points: int,
    paint_multiplier: float = 1.0,
    first_spotter_bonus: float = 1.0,
    decay_factor: float = 1.0,
    rarity_tier: str = "Rare",
    acoustic_bonus: float = 1.0,
    acoustic_engine: str | None = None,
) -> dict:
    """Creates a Dynamic Point Breakdown A2UI card payload."""
    return {
        "type": "point_breakdown",
        "car_name": car_name,
        "base_points": base_points,
        "production_units": production_units,
        "scarcity_label": f"Units produced: {production_units:,}",
        "recent_spots_30d": recent_spots_30d,
        "velocity_label": f"Spotted {recent_spots_30d} times this month",
        "decay_factor": round(decay_factor, 2),
        "paint_multiplier": paint_multiplier,
        "first_spotter_bonus": first_spotter_bonus,
        "acoustic_bonus": acoustic_bonus,
        "acoustic_engine": acoustic_engine,
        "final_points": final_points,
        "rarity_tier": rarity_tier,
    }


def build_acoustic_verification_card(
    engine_config: str,
    confidence: float,
    loudness_dbfs: float,
    signature: str,
    rev_limiter: bool,
    bonus_multiplier: float = 1.25,
) -> dict:
    """Creates an Acoustic Engine Note Verification A2UI card payload."""
    return {
        "type": "acoustic_verification",
        "engine_config": engine_config,
        "confidence": round(confidence, 2),
        "loudness_dbfs": round(loudness_dbfs, 1),
        "signature": signature,
        "rev_limiter": rev_limiter,
        "bonus_multiplier": bonus_multiplier,
        "badge_text": f"🔊 +25% ACOUSTIC MATCH // {engine_config.upper()}",
    }


def build_paywall_card(quota_info: dict) -> dict:
    """Creates an Out-of-Fuel Paywall A2UI card payload."""
    scans_today = quota_info.get("scans_today", 5)
    daily_limit = quota_info.get("daily_limit", 5)
    next_reset = quota_info.get("next_reset_seconds", 3600)
    hours = next_reset // 3600
    minutes = (next_reset % 3600) // 60
    reset_text = f"{hours}h {minutes}m (Midnight UTC)" if hours > 0 else f"{minutes}m (Midnight UTC)"
    return {
        "type": "paywall_card",
        "title": "Daily Quota Limit Reached",
        "subtitle": f"{scans_today}/{daily_limit} Daily Scans Used",
        "scans_used": scans_today,
        "daily_limit": daily_limit,
        "progress_pct": min(100, int((scans_today / max(1, daily_limit)) * 100)),
        "countdown_text": reset_text,
        "reset_target": "Midnight UTC",
        "message": "Free daily fuel exhausted. Refill scans instantly or unlock CarDex Pro.",
        "quota": quota_info,
    }

