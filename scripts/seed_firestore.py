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

"""Seed script to populate Firestore with initial CarDex vehicle database and spotter leaderboard.

IMPORTANT: PROJECT_ID is hardcoded as a literal string to avoid project number resolution
bugs on Agent Platform.
"""

from google.cloud import firestore

# Hardcoded GCP Project ID (never derive from google.auth.default or GOOGLE_CLOUD_PROJECT)
PROJECT_ID = "qwiklabs-gcp-04-6f324b699fdd"

SEED_CARS = [
    {
        "id": "bugatti-la-voiture-noire",
        "make": "Bugatti",
        "model": "La Voiture Noire",
        "year": 2019,
        "trim": "1-of-1 Bespoke Grand Tourisme",
        "colorways": ["Deep Gloss Black Carbon Fiber"],
        "rarity_tier": "Mythic 1-of-1",
        "points": 50000,
        "production_count": 1,
        "global_spotted_count": 1,
        "is_special_edition": True,
        "special_edition_notes": (
            "A modern one-off tribute to Jean Bugatti's lost Type 57 SC Atlantic. "
            "Features a handcrafted carbon-fibre body, 6 distinct exhaust tips, and an $18.7M price tag."
        ),
        "specs": {
            "engine": "8.0L Quad-Turbo W16",
            "horsepower": 1479,
            "top_speed_mph": 261,
            "zero_to_sixty_sec": 2.4,
        },
    },
    {
        "id": "pagani-zonda-cinque",
        "make": "Pagani",
        "model": "Zonda Cinque",
        "year": 2009,
        "trim": "Cinque Coupé (Chassis #1 of 5)",
        "colorways": ["Bianco Benny / Naked Carbon Fiber with Rosso Corsa Accents"],
        "rarity_tier": "Legendary",
        "points": 18000,
        "production_count": 5,
        "global_spotted_count": 4,
        "is_special_edition": True,
        "special_edition_notes": (
            "Commissioned at the request of the Hong Kong Pagani dealer. "
            "First road car to use Pagani's patented Carbo-Titanium weave and roof-mounted ram air scoop."
        ),
        "specs": {
            "engine": "7.3L Naturally Aspirated AMG V12",
            "horsepower": 669,
            "top_speed_mph": 217,
            "zero_to_sixty_sec": 3.4,
        },
    },
    {
        "id": "mclaren-f1-lm",
        "make": "McLaren",
        "model": "F1 LM",
        "year": 1995,
        "trim": "Le Mans Edition (LM1-LM5)",
        "colorways": ["Historic Papaya Orange"],
        "rarity_tier": "Legendary",
        "points": 25000,
        "production_count": 5,
        "global_spotted_count": 2,
        "is_special_edition": True,
        "special_edition_notes": (
            "Built to celebrate McLaren's outright victory at the 1995 24 Hours of Le Mans. "
            "Stripped interior, high-downforce GTR carbon wing, and unrestricted race-spec BMW V12."
        ),
        "specs": {
            "engine": "6.1L Naturally Aspirated BMW S70/2 V12",
            "horsepower": 680,
            "top_speed_mph": 225,
            "zero_to_sixty_sec": 2.9,
        },
    },
    {
        "id": "koenigsegg-jesko-absolut",
        "make": "Koenigsegg",
        "model": "Jesko Absolut",
        "year": 2024,
        "trim": "Low-Drag Top Speed Spec",
        "colorways": ["Tang Orange", "Graphite Grey", "Naked Carbon Fiber"],
        "rarity_tier": "Legendary",
        "points": 15000,
        "production_count": 125,
        "global_spotted_count": 14,
        "is_special_edition": True,
        "special_edition_notes": (
            "The fastest car Koenigsegg will ever build. Features rear fighter-jet fin stabilizers "
            "replacing the active wing to achieve a drag coefficient of just 0.278 Cd."
        ),
        "specs": {
            "engine": "5.0L Twin-Turbo Flat-Plane V8 (E85)",
            "horsepower": 1600,
            "top_speed_mph": 330,
            "zero_to_sixty_sec": 2.5,
        },
    },
    {
        "id": "ferrari-daytona-sp3",
        "make": "Ferrari",
        "model": "Daytona SP3",
        "year": 2023,
        "trim": "Icona Series",
        "colorways": ["Rosso Corsa", "Bianco Spino", "Giallo Modena"],
        "rarity_tier": "Epic",
        "points": 6000,
        "production_count": 599,
        "global_spotted_count": 48,
        "is_special_edition": True,
        "special_edition_notes": (
            "Third model in Ferrari's Icona program honoring the legendary 1-2-3 finish at the 1967 24 Hours of Daytona. "
            "Features striking horizontal blade rear strakes and butterfly doors."
        ),
        "specs": {
            "engine": "6.5L Naturally Aspirated 65° V12 (F140HC)",
            "horsepower": 829,
            "top_speed_mph": 211,
            "zero_to_sixty_sec": 2.85,
        },
    },
    {
        "id": "porsche-911-gt3-rs-weissach",
        "make": "Porsche",
        "model": "911 GT3 RS",
        "year": 2023,
        "trim": "992.1 Generation with Weissach Package",
        "colorways": ["Arctic Silver / Pyro Red", "Ice Grey Metallic", "Guards Red"],
        "rarity_tier": "Rare",
        "points": 2500,
        "production_count": 3500,
        "global_spotted_count": 420,
        "is_special_edition": True,
        "special_edition_notes": (
            "Street-legal race car with active aerodynamic Drag Reduction System (DRS) and "
            "exposed carbon fibre bonnet, roof, and anti-roll bars via the Weissach Package."
        ),
        "specs": {
            "engine": "4.0L Naturally Aspirated Boxer-6 (9,000 RPM)",
            "horsepower": 518,
            "top_speed_mph": 184,
            "zero_to_sixty_sec": 3.0,
        },
    },
    {
        "id": "bmw-m3-cs",
        "make": "BMW",
        "model": "M3 CS",
        "year": 2024,
        "trim": "G80 Competition Sport Edition",
        "colorways": ["Signal Green", "Frozen Solid White Metallic", "Black Sapphire"],
        "rarity_tier": "Uncommon",
        "points": 750,
        "production_count": 1800,
        "global_spotted_count": 2840,
        "is_special_edition": True,
        "special_edition_notes": (
            "Lightweight Competition Sport version of the M3 sedan with yellow motorsport DRL headlights, "
            "carbon-ceramic brakes, and unique chassis tuning."
        ),
        "specs": {
            "engine": "3.0L Twin-Turbo S58 Inline-6",
            "horsepower": 543,
            "top_speed_mph": 188,
            "zero_to_sixty_sec": 3.2,
        },
    },
    {
        "id": "mazda-mx5-miata-club",
        "make": "Mazda",
        "model": "MX-5 Miata",
        "year": 2024,
        "trim": "Club Edition with Brembo / BBS / Recaro Package",
        "colorways": ["Soul Red Crystal Metallic", "Aero Grey", "Jet Black Mica"],
        "rarity_tier": "Common",
        "points": 150,
        "production_count": 50000,
        "global_spotted_count": 164200,
        "is_special_edition": False,
        "special_edition_notes": (
            "The quintessential lightweight pure sports car. 50:50 weight distribution, "
            "Bilstein dampers, limited-slip differential, and crisp 6-speed manual transmission."
        ),
        "specs": {
            "engine": "2.0L Skyactiv-G Inline-4",
            "horsepower": 181,
            "top_speed_mph": 136,
            "zero_to_sixty_sec": 5.7,
        },
    },
]

SEED_LEADERBOARD = [
    {
        "user_id": "apex_hunter",
        "username": "ApexHunter",
        "total_points": 52500,
        "total_spots": 31,
        "rarest_spot": "Bugatti La Voiture Noire",
        "rank": 1,
    },
    {
        "user_id": "hypercar_scout",
        "username": "HypercarScout",
        "total_points": 38000,
        "total_spots": 19,
        "rarest_spot": "McLaren F1 LM",
        "rank": 2,
    },
    {
        "user_id": "trackside_tom",
        "username": "TracksideTom",
        "total_points": 14200,
        "total_spots": 24,
        "rarest_spot": "Ferrari Daytona SP3",
        "rank": 3,
    },
]


def seed():
    print(f"Connecting to Firestore for project: {PROJECT_ID}...")
    db = firestore.Client(project=PROJECT_ID)

    print(f"Seeding {len(SEED_CARS)} vehicles into 'cars' collection...")
    for car in SEED_CARS:
        doc_id = car["id"]
        doc_ref = db.collection("cars").document(doc_id)
        doc_ref.set(car)
        print(f"  ✓ Seeded: [{car['rarity_tier']}] {car['make']} {car['model']} ({doc_id})")

    print(f"\nSeeding {len(SEED_LEADERBOARD)} spotter profiles into 'leaderboard' collection...")
    for spotter in SEED_LEADERBOARD:
        doc_id = spotter["user_id"]
        db.collection("leaderboard").document(doc_id).set(spotter)
        print(f"  ✓ Seeded Leaderboard: #{spotter['rank']} {spotter['username']} ({spotter['total_points']:,} pts)")

    print("\n✅ Firestore database successfully seeded for CarDex!")


if __name__ == "__main__":
    seed()
