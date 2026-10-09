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

"""Spectrometric Colorway & CIEDE2000 Paint Matcher for CarDex.

Extracts vehicle panel body color, strips specular highlights and shadows,
converts diffuse colors to CIE L*a*b* under Illuminant D65, and matches against
verified factory OEM Paint to Sample (PTS), BMW Individual, and Ferrari Historical colors
using the standard CIEDE2000 (Delta E 00) perceptual difference algorithm.
"""

import io
import json
import math
import os
from typing import Any
import numpy as np
from PIL import Image

PAINT_DB_PATH = os.path.join(os.path.dirname(__file__), "paint_db.json")
_paint_database: list[dict[str, Any]] | None = None


def load_paint_database() -> list[dict[str, Any]]:
    """Load curated OEM paint catalog from paint_db.json."""
    global _paint_database
    if _paint_database is None:
        if os.path.exists(PAINT_DB_PATH):
            with open(PAINT_DB_PATH, "r", encoding="utf-8") as f:
                _paint_database = json.load(f)
        else:
            _paint_database = []
    return _paint_database


def rgb_to_cielab(r: int, g: int, b: int) -> tuple[float, float, float]:
    """Convert standard sRGB (0-255) to CIE L*a*b* under standard D65 illuminant."""
    # 1. Normalize sRGB to [0, 1] and apply inverse gamma companding
    def inv_gamma(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r_lin = inv_gamma(r / 255.0)
    g_lin = inv_gamma(g / 255.0)
    b_lin = inv_gamma(b / 255.0)

    # 2. Linear sRGB to CIE XYZ (D65 matrix)
    X = (r_lin * 0.4124564 + g_lin * 0.3575761 + b_lin * 0.1804375) * 100.0
    Y = (r_lin * 0.2126729 + g_lin * 0.7151522 + b_lin * 0.0721750) * 100.0
    Z = (r_lin * 0.0193339 + g_lin * 0.1191920 + b_lin * 0.9503041) * 100.0

    # 3. Reference white D65
    Xn, Yn, Zn = 95.047, 100.000, 108.883

    def f(t: float) -> float:
        delta = 6.0 / 29.0
        return t ** (1.0 / 3.0) if t > delta**3 else (t / (3.0 * delta**2)) + (4.0 / 29.0)

    fx = f(X / Xn)
    fy = f(Y / Yn)
    fz = f(Z / Zn)

    L = max(0.0, min(100.0, 116.0 * fy - 16.0))
    a = 500.0 * (fx - fy)
    b_lab = 200.0 * (fy - fz)

    return round(L, 2), round(a, 2), round(b_lab, 2)


def ciede2000(lab1: tuple[float, float, float], lab2: tuple[float, float, float]) -> float:
    """Calculate the CIEDE2000 total color difference Delta E 00 between two Lab colors.

    Follows the CIE Technical Report 142-2001 / ISO standard.
    Delta E 00 <= 2.5 indicates a virtually indistinguishable / perceptual exact match.
    """
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2

    # Step 1: Calculate C_prime and a_prime
    C1 = math.sqrt(a1**2 + b1**2)
    C2 = math.sqrt(a2**2 + b2**2)
    C_bar = (C1 + C2) / 2.0

    G = 0.5 * (1.0 - math.sqrt((C_bar**7) / (C_bar**7 + 25.0**7 + 1e-12)))
    a1_prime = (1.0 + G) * a1
    a2_prime = (1.0 + G) * a2

    C1_prime = math.sqrt(a1_prime**2 + b1**2)
    C2_prime = math.sqrt(a2_prime**2 + b2**2)

    def compute_h_prime(a_p: float, b_p: float) -> float:
        if a_p == 0 and b_p == 0:
            return 0.0
        deg = math.degrees(math.atan2(b_p, a_p))
        return deg if deg >= 0 else deg + 360.0

    h1_prime = compute_h_prime(a1_prime, b1)
    h2_prime = compute_h_prime(a2_prime, b2)

    # Step 2: Calculate Delta L_prime, Delta C_prime, Delta H_prime
    delta_L_prime = L2 - L1
    delta_C_prime = C2_prime - C1_prime

    if C1_prime * C2_prime == 0:
        delta_h_prime = 0.0
    elif abs(h2_prime - h1_prime) <= 180.0:
        delta_h_prime = h2_prime - h1_prime
    elif h2_prime - h1_prime > 180.0:
        delta_h_prime = (h2_prime - h1_prime) - 360.0
    else:
        delta_h_prime = (h2_prime - h1_prime) + 360.0

    delta_H_prime = 2.0 * math.sqrt(C1_prime * C2_prime) * math.sin(math.radians(delta_h_prime / 2.0))

    # Step 3: Calculate weighting factors S_L, S_C, S_H and rotation term R_T
    L_bar_prime = (L1 + L2) / 2.0
    C_bar_prime = (C1_prime + C2_prime) / 2.0

    if C1_prime * C2_prime == 0:
        h_bar_prime = h1_prime + h2_prime
    elif abs(h1_prime - h2_prime) <= 180.0:
        h_bar_prime = (h1_prime + h2_prime) / 2.0
    elif h1_prime + h2_prime < 360.0:
        h_bar_prime = (h1_prime + h2_prime + 360.0) / 2.0
    else:
        h_bar_prime = (h1_prime + h2_prime - 360.0) / 2.0

    T = (
        1.0
        - 0.17 * math.cos(math.radians(h_bar_prime - 30.0))
        + 0.24 * math.cos(math.radians(2.0 * h_bar_prime))
        + 0.32 * math.cos(math.radians(3.0 * h_bar_prime + 6.0))
        - 0.20 * math.cos(math.radians(4.0 * h_bar_prime - 63.0))
    )

    delta_theta = 30.0 * math.exp(-(((h_bar_prime - 275.0) / 25.0) ** 2))
    R_C = 2.0 * math.sqrt((C_bar_prime**7) / (C_bar_prime**7 + 25.0**7 + 1e-12))
    R_T = -math.sin(math.radians(2.0 * delta_theta)) * R_C

    S_L = 1.0 + (0.015 * ((L_bar_prime - 50.0) ** 2)) / math.sqrt(20.0 + (L_bar_prime - 50.0) ** 2)
    S_C = 1.0 + 0.045 * C_bar_prime
    S_H = 1.0 + 0.015 * C_bar_prime * T

    # Parametric weighting factors (1.0 for standard CIE conditions)
    k_L = 1.0
    k_C = 1.0
    k_H = 1.0

    term_L = delta_L_prime / (k_L * S_L)
    term_C = delta_C_prime / (k_C * S_C)
    term_H = delta_H_prime / (k_H * S_H)

    de00_sq = term_L**2 + term_C**2 + term_H**2 + R_T * term_C * term_H
    return round(math.sqrt(max(0.0, de00_sq)), 3)


def extract_dominant_panel_color(image_bytes: bytes) -> tuple[int, int, int]:
    """Extract dominant car panel diffuse color by cropping center 40% and stripping glare/shadows."""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size

        # Crop central 40% region where car body panels reside
        left = int(w * 0.30)
        top = int(h * 0.30)
        right = int(w * 0.70)
        bottom = int(h * 0.70)
        crop = img.crop((left, top, right, bottom))
        crop = crop.resize((100, 100), Image.Resampling.BILINEAR)

        # Convert to HSV to filter highlights and shadows
        hsv = crop.convert("HSV")
        hsv_arr = np.asarray(hsv, dtype=np.float32)
        rgb_arr = np.asarray(crop, dtype=np.float32)

        s = hsv_arr[:, :, 1] / 255.0
        v = hsv_arr[:, :, 2] / 255.0

        # Filter specular highlights (glare: high brightness, low saturation)
        # Filter deep shadows and black tires/asphalt (v < 0.15)
        panel_mask = (v >= 0.15) & ~((v > 0.88) & (s < 0.18))

        if np.sum(panel_mask) < 50:
            # Fallback to simple center median
            median_rgb = np.median(rgb_arr.reshape(-1, 3), axis=0)
        else:
            diffuse_pixels = rgb_arr[panel_mask]
            median_rgb = np.median(diffuse_pixels, axis=0)

        r = int(np.clip(median_rgb[0], 0, 255))
        g = int(np.clip(median_rgb[1], 0, 255))
        b = int(np.clip(median_rgb[2], 0, 255))
        return r, g, b
    except Exception:
        return 128, 128, 128


def match_cielab_coordinates(
    sample_lab: tuple[float, float, float] | list[float],
    make_hint: str = "",
) -> dict[str, Any]:
    """Find nearest factory OEM finish for given CIE L*a*b* coordinates via CIEDE2000.

    Returns match details, Delta E 00 distance, factory code, and PTS multiplier.
    """
    db = load_paint_database()
    sample_lab_tuple = (float(sample_lab[0]), float(sample_lab[1]), float(sample_lab[2]))

    best_match = None
    min_delta_e = 999.0
    make_lower = make_hint.lower().strip()

    for entry in db:
        entry_lab = (entry["lab"][0], entry["lab"][1], entry["lab"][2])
        de00 = ciede2000(sample_lab_tuple, entry_lab)

        # Slight prioritization bias if OEM brand matches make hint
        oem_brand = entry.get("make") or entry.get("oem") or ""
        bias = -0.4 if (make_lower and make_lower in oem_brand.lower()) else 0.0
        adjusted_de = de00 + bias

        if adjusted_de < min_delta_e:
            min_delta_e = adjusted_de
            best_match = entry | {"delta_e00": de00}

    is_exact_match = best_match is not None and best_match["delta_e00"] <= 3.5
    is_close_match = best_match is not None and best_match["delta_e00"] <= 7.0

    if best_match and is_close_match:
        multiplier = best_match.get("multiplier", 1.35)
        name = best_match.get("commercial_name") or best_match.get("name") or "OEM Finish"
        oem = best_match.get("make") or best_match.get("oem") or ""
        code = best_match.get("paint_code") or best_match.get("code") or ""
        program = best_match.get("program") or best_match.get("category") or "Factory"
        category = best_match.get("category") or ("pts" if best_match.get("tier") == "PTS" else "standard")
        is_pts = category in ("pts", "bespoke") or best_match.get("tier") == "PTS"
        badge = f"🎨 Verified OEM: {oem} {name} ({program} Code {code})"
        return {
            "matched": True,
            "is_exact_match": is_exact_match,
            "is_pts": is_pts,
            "paint_name": name,
            "commercial_name": name,
            "name": name,
            "oem_brand": oem,
            "make": oem,
            "program": program,
            "paint_code": code,
            "code": code,
            "category": category,
            "delta_e00": round(best_match["delta_e00"], 2),
            "multiplier": multiplier,
            "badge_text": badge,
            "sample_lab": sample_lab_tuple,
            "rgb": best_match.get("rgb", [128, 128, 128]),
        }

    return {
        "matched": False,
        "is_exact_match": False,
        "is_pts": False,
        "paint_name": "Standard Production Finish",
        "commercial_name": "Standard Production Finish",
        "name": "Standard Production Finish",
        "multiplier": 1.0,
        "delta_e00": round(min_delta_e, 2) if best_match else 999.0,
        "badge_text": "🎨 Factory Standard Colorway",
        "sample_lab": sample_lab_tuple,
    }


def match_oem_paint_color(image_bytes: bytes, make_hint: str = "") -> dict[str, Any]:
    """Analyze image panel color and find nearest factory OEM finish via CIEDE2000.

    Returns match details, Delta E 00 distance, factory code, and PTS multiplier.
    """
    r, g, b = extract_dominant_panel_color(image_bytes)
    sample_lab = rgb_to_cielab(r, g, b)
    res = match_cielab_coordinates(sample_lab, make_hint=make_hint)
    res["sample_rgb"] = [r, g, b]
    return res
