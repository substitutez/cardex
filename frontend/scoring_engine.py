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

"""Dynamic Rarity & Deflationary Scoring Engine for CarDex.

Calculates vehicle spot points using an economic pricing model that self-adjusts based on
production volume (N_prod), trailing 30-day encounter frequency (D_30), paint finish (M_paint),
and global first-finder discovery bonuses (B_first).
"""

import datetime
import math
from typing import Any
try:
    from .firestore_db import get_db
except ImportError:
    from firestore_db import get_db

# Benchmark production volume dictionary for iconic supercars / hypercars
KNOWN_PRODUCTION_RUNS: dict[str, int] = {
    # 1-of-1 / Bespoke
    "bugatti chiron profilée": 1,
    "bugatti la voiture noire": 1,
    "ferrari sp3jc": 1,
    "ferrari omologata": 1,
    "pagani zonda hp barchetta": 3,
    "koenigsegg one:1": 6,
    "bugatti centodieci": 10,
    "mclaren f1 lm": 6,
    "aston martin valkyrie amr pro": 40,
    "bugatti bolide": 40,
    "ferrari daytona sp3": 599,
    "ferrari monza sp1": 499,
    "ferrari monza sp2": 499,
    "mclaren senna": 500,
    "mclaren speedtail": 106,
    "mclaren p1": 375,
    "porsche 918 spyder": 918,
    "porsche carrera gt": 1270,
    "porsche 911 gt3 rs 4.0": 600,
    "porsche 911 s/t": 1963,
    "porsche 911 sport classic": 1250,
    "koenigsegg jesko": 125,
    "koenigsegg regera": 80,
    "koenigsegg cc850": 70,
    "pagani huayra": 100,
    "pagani utopia": 99,
    "lexus lfa": 500,
    "ford gt (2017)": 1350,
    "lamborghini sian": 63,
    "lamborghini countach lpi 800-4": 112,
    "ferrari f40": 1315,
    "ferrari f50": 349,
    "ferrari enzo": 400,
    "ferrari laferrari": 499,
}


def lookup_production_run(make: str, model: str, trim: str = "") -> int:
    """Estimate or lookup production volume N_prod for a vehicle."""
    import unicodedata

    def _clean(s: str) -> str:
        # Strip accents and lower
        return "".join(
            c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
        ).strip().lower()

    key = _clean(f"{make} {model}")
    full_key = _clean(f"{make} {model} {trim}")

    for k, v in KNOWN_PRODUCTION_RUNS.items():
        k_clean = _clean(k)
        if k_clean in full_key or k_clean in key or full_key in k_clean:
            return v

    # Heuristics based on model and trim name
    if any(w in full_key for w in ["1 of 1", "one of one", "unique", "bespoke"]):
        return 1
    if any(w in full_key for w in ["weissach", "gtd", "amr", "track weapon", "special edition"]):
        return 500
    if any(w in full_key for w in ["gt3 rs", "gt2 rs", "gt3", "gt4", "superleggera", "svj", "scuderia"]):
        return 4500 if "gt3 rs" in full_key else 3000
    if any(w in full_key for w in ["turbo s", "m8", "black series", "amg gt"]):
        return 10000

    # Default mass-production assumption
    return 150000


def calculate_base_points(n_prod: int) -> int:
    """Calculate BasePoints(N_prod).

    Scales inversely with sqrt(N_prod):
    - N_prod = 1      -> 50,000 PTS (Mythic 1-of-1)
    - N_prod = 50     -> ~14,142 PTS
    - N_prod = 500    -> ~4,472 PTS
    - N_prod = 5,000  -> ~1,414 PTS
    - N_prod >= 150k  -> 250 PTS (Minimum Floor)
    """
    if n_prod <= 1:
        return 50000
    raw = 100000.0 / math.sqrt(n_prod)
    return int(max(250, min(50000, round(raw))))


def get_trailing_30d_encounters(car_identifier: str) -> int:
    """Count sightings of this vehicle on CarDex within the last 30 days."""
    try:
        db = get_db()
        thirty_days_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)).isoformat()
        spots_ref = (
            db.collection("spots")
            .where("car_id", "==", car_identifier)
            .where("spotted_at", ">=", thirty_days_ago)
        )
        docs = list(spots_ref.stream())
        return len(docs)
    except Exception:
        return 0


def calculate_encounter_decay(encounters_30d: int) -> float:
    """Calculate 30-day platform encounter decay factor D_30.

    D_30 = max(0.35, 1 / (1 + 0.08 * S_30))
    """
    decay = 1.0 / (1.0 + 0.08 * encounters_30d)
    return max(0.35, round(decay, 3))


def get_paint_multiplier(is_pts: bool = False, is_bespoke_or_carbon: bool = False) -> float:
    """Calculate finish multiplier M_paint.

    - Standard = 1.0x
    - OEM Paint to Sample (PTS) / Individual = 1.35x
    - Bespoke / Exposed Carbon Weave / 1-of-1 Livery = 1.75x
    """
    if is_bespoke_or_carbon:
        return 1.75
    if is_pts:
        return 1.35
    return 1.0


def check_first_finder_status(car_identifier: str, location: str = "") -> tuple[float, str]:
    """Check if this is the first time this vehicle has been spotted globally or regionally."""
    try:
        db = get_db()
        existing = list(db.collection("spots").where("car_id", "==", car_identifier).limit(2).stream())
        if len(existing) == 0:
            return 2.0, "Global First Finder (2.0x Multiplier)"
        return 1.0, "Standard Encounter"
    except Exception:
        return 1.0, "Standard Encounter"


def determine_rarity_tier(final_points: int) -> str:
    """Determine descriptive rarity tier based on economic point valuation."""
    if final_points >= 35000:
        return "Mythic 1-of-1"
    if final_points >= 15000:
        return "Legendary"
    if final_points >= 6000:
        return "Epic"
    if final_points >= 2000:
        return "Rare"
    if final_points >= 800:
        return "Uncommon"
    return "Common"


def compute_dynamic_spot_score(
    make: str,
    model: str,
    trim: str = "",
    car_identifier: str = "",
    is_pts: bool = False,
    is_bespoke_or_carbon: bool = False,
    location: str = "Public Road",
    production_run_override: int | None = None,
) -> dict[str, Any]:
    """Execute the complete dynamic deflationary scoring equation.

    Points = round(BasePoints(N_prod) * D_30 * M_paint * B_first)
    """
    car_id = car_identifier or f"{make}-{model}".strip().lower().replace(" ", "-")
    n_prod = production_run_override or lookup_production_run(make, model, trim)
    base_points = calculate_base_points(n_prod)

    encounters_30d = get_trailing_30d_encounters(car_id)
    d_30 = calculate_encounter_decay(encounters_30d)

    m_paint = get_paint_multiplier(is_pts=is_pts, is_bespoke_or_carbon=is_bespoke_or_carbon)
    b_first, first_finder_label = check_first_finder_status(car_id, location)

    raw_points = base_points * d_30 * m_paint * b_first
    final_points = int(round(raw_points))
    rarity_tier = determine_rarity_tier(final_points)

    return {
        "final_points": final_points,
        "rarity_tier": rarity_tier,
        "breakdown": {
            "n_prod": n_prod,
            "base_points": base_points,
            "encounters_30d": encounters_30d,
            "decay_factor_d30": d_30,
            "paint_multiplier": m_paint,
            "first_finder_multiplier": b_first,
            "first_finder_status": first_finder_label,
        },
    }
