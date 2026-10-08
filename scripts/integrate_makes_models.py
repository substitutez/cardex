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

"""Integrate vehicle data from Vehicle-makes-models dataset into Firestore with duplicate checking."""

import os
import sqlite3
from typing import Any
from google.cloud import firestore

# Hardcoded GCP Project ID (never derive from google.auth.default on Agent Platform)
FIRESTORE_PROJECT_ID = "qwiklabs-gcp-04-6f324b699fdd"
SQLITE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "vehicles.sqlite")

# Target vehicles to integrate from the dataset
CANDIDATES = [
    # Intentionally includes existing items to verify duplicate prevention
    {
        "id": "bugatti-la-voiture-noire",
        "make": "Bugatti",
        "model": "La Voiture Noire",
        "year": 2019,
        "trim": "1-of-1 Bespoke Grand Tourisme",
        "rarity_tier": "Mythic 1-of-1",
        "points": 50000,
        "production_count": 1,
        "is_special_edition": True,
        "special_edition_notes": "One-off tribute to lost Type 57 SC Atlantic.",
        "colorways": ["Deep Gloss Black Carbon Fiber"],
    },
    {
        "id": "pagani-zonda-cinque",
        "make": "Pagani",
        "model": "Zonda Cinque",
        "year": 2009,
        "trim": "Cinque Coupé",
        "rarity_tier": "Legendary",
        "points": 18000,
        "production_count": 5,
        "is_special_edition": True,
        "special_edition_notes": "1 of 5 built worldwide.",
        "colorways": ["Bianco Benny / Naked Carbon Fiber"],
    },
    # New additions from vehicle-makes-models dataset
    {
        "id": "bugatti-bolide",
        "make": "Bugatti",
        "model": "Bolide",
        "year": 2022,
        "trim": "Track Hypercar (W16 AWD)",
        "rarity_tier": "Mythic 1-of-1",
        "points": 35000,
        "production_count": 40,
        "is_special_edition": True,
        "special_edition_notes": "Track-only hypercar with radical X-wing aero and 1850 HP Quad-Turbo W16.",
        "colorways": ["French Racing Blue / Exposed Carbon", "Black / Yellow"],
    },
    {
        "id": "bugatti-tourbillon",
        "make": "Bugatti",
        "model": "Tourbillon",
        "year": 2024,
        "trim": "Cosworth V16 Hybrid",
        "rarity_tier": "Legendary",
        "points": 22000,
        "production_count": 250,
        "is_special_edition": True,
        "special_edition_notes": "Naturally aspirated 8.3L V16 co-developed with Cosworth revving to 9,000 RPM with 3 electric motors.",
        "colorways": ["Bugatti Blue Royale", "Silver / Blue Tinted Carbon"],
    },
    {
        "id": "bugatti-divo",
        "make": "Bugatti",
        "model": "Divo",
        "year": 2018,
        "trim": "Coachbuilt Aerodynamic Special",
        "rarity_tier": "Legendary",
        "points": 18000,
        "production_count": 40,
        "is_special_edition": True,
        "special_edition_notes": "Corner-focused coachbuilt hypercar named after Albert Divo. 40 units worldwide.",
        "colorways": ["Titanium Liquid Gloss / Divo Racing Blue"],
    },
    {
        "id": "bugatti-centodieci",
        "make": "Bugatti",
        "model": "Centodieci",
        "year": 2019,
        "trim": "EB110 Hommage",
        "rarity_tier": "Mythic 1-of-1",
        "points": 32000,
        "production_count": 10,
        "is_special_edition": True,
        "special_edition_notes": "Limited to 10 units celebrating Bugatti's 110th anniversary and the EB110.",
        "colorways": ["Bianco Puro / Black", "Italian Blue"],
    },
    {
        "id": "koenigsegg-one-1",
        "make": "Koenigsegg",
        "model": "One:1",
        "year": 2014,
        "trim": "Megawatt Car (1360 HP / 1360 KG)",
        "rarity_tier": "Mythic 1-of-1",
        "points": 30000,
        "production_count": 7,
        "is_special_edition": True,
        "special_edition_notes": "World's first 1:1 power-to-weight ratio production car (1 hp per 1 kg). Only 7 built.",
        "colorways": ["Matte White / Exposed Carbon / Clear Carbon Orange"],
    },
    {
        "id": "koenigsegg-gemera",
        "make": "Koenigsegg",
        "model": "Gemera",
        "year": 2020,
        "trim": "4-Seater Mega-GT AWD",
        "rarity_tier": "Legendary",
        "points": 15000,
        "production_count": 300,
        "is_special_edition": True,
        "special_edition_notes": "World's first Mega-GT with Freevalve Tiny Friendly Giant engine producing 1723 HP.",
        "colorways": ["Steel Grey", "Monstera Green", "K-White"],
    },
    {
        "id": "koenigsegg-regera",
        "make": "Koenigsegg",
        "model": "Regera",
        "year": 2015,
        "trim": "Direct Drive Hybrid Hypercar",
        "rarity_tier": "Legendary",
        "points": 14000,
        "production_count": 80,
        "is_special_edition": True,
        "special_edition_notes": "Koenigsegg Direct Drive (KDD) without a traditional gearbox, 1509 HP.",
        "colorways": ["Horizon Blue", "Candy Apple Red", "Bare Carbon Fiber"],
    },
    {
        "id": "pagani-huayra-bc",
        "make": "Pagani",
        "model": "Huayra",
        "year": 2019,
        "trim": "Huayra Roadster BC",
        "rarity_tier": "Legendary",
        "points": 17000,
        "production_count": 40,
        "is_special_edition": True,
        "special_edition_notes": "Named in honor of Benny Caiola, Pagani's first customer. Carbo-Triax HP62 monocoque.",
        "colorways": ["Naked Grigio Carbonio", "Rosso Monza"],
    },
    {
        "id": "pagani-utopia",
        "make": "Pagani",
        "model": "Utopia",
        "year": 2022,
        "trim": "Twin-Turbo V12 7MT",
        "rarity_tier": "Legendary",
        "points": 16000,
        "production_count": 99,
        "is_special_edition": True,
        "special_edition_notes": "Horacio Pagani's purist masterpiece: 864 HP AMG V12 paired to an optional gated 7-speed manual.",
        "colorways": ["Rinascimento Metallic", "Clear Carbon Fiber"],
    },
    {
        "id": "mclaren-senna",
        "make": "McLaren",
        "model": "Senna",
        "year": 2018,
        "trim": "Ultimate Series Track Weapon",
        "rarity_tier": "Epic",
        "points": 9000,
        "production_count": 500,
        "is_special_edition": True,
        "special_edition_notes": "A tribute to Ayrton Senna generating 800 kg of downforce at 155 mph.",
        "colorways": ["Mira Orange", "Victory Grey", "Senna Yellow / Green"],
    },
    {
        "id": "mclaren-p1",
        "make": "McLaren",
        "model": "P1",
        "year": 2013,
        "trim": "Holy Trinity Hybrid Hypercar",
        "rarity_tier": "Legendary",
        "points": 16000,
        "production_count": 375,
        "is_special_edition": True,
        "special_edition_notes": "Pioneering member of the Holy Trinity with IPAS instant power boost and DRS rear wing.",
        "colorways": ["Volcano Yellow", "Volcano Orange", "Supernova Silver"],
    },
    {
        "id": "mclaren-speedtail",
        "make": "McLaren",
        "model": "Speedtail",
        "year": 2018,
        "trim": "3-Seater Hyper-GT",
        "rarity_tier": "Legendary",
        "points": 17500,
        "production_count": 106,
        "is_special_edition": True,
        "special_edition_notes": "Central driver seat layout with flexible carbon fiber active rear ailerons. 250 mph top speed.",
        "colorways": ["Frozen Blue", "Volcano Red", "Liquid Silver"],
    },
    {
        "id": "ferrari-laferrari",
        "make": "Ferrari",
        "model": "LaFerrari",
        "year": 2013,
        "trim": "HY-KERS V12 Flagship",
        "rarity_tier": "Legendary",
        "points": 16000,
        "production_count": 499,
        "is_special_edition": True,
        "special_edition_notes": "Maranello's first hybrid hypercar combining an 800 HP 6.3L V12 with a 163 HP KERS electric motor.",
        "colorways": ["Rosso Corsa", "Giallo Modena", "Nero Daytona"],
    },
    {
        "id": "ferrari-monza-sp2",
        "make": "Ferrari",
        "model": "Monza",
        "year": 2018,
        "trim": "Icona Series Barchetta SP2",
        "rarity_tier": "Epic",
        "points": 11000,
        "production_count": 499,
        "is_special_edition": True,
        "special_edition_notes": "Windscreen-less Barchetta inspired by classic 1950s Ferrari racing barchettas.",
        "colorways": ["Grigio Titanio with Racing Stripe", "Rosso Corsa"],
    },
    {
        "id": "ferrari-812-competizione",
        "make": "Ferrari",
        "model": "812",
        "year": 2021,
        "trim": "Competizione V12",
        "rarity_tier": "Rare",
        "points": 6000,
        "production_count": 999,
        "is_special_edition": True,
        "special_edition_notes": "Ultimate evolution of front-mid engine V12 with an all-aluminum rear vortex generator screen.",
        "colorways": ["Giallo Fly", "Rosso Scuderia", "Blu Corsa"],
    },
    {
        "id": "lamborghini-veneno",
        "make": "Lamborghini",
        "model": "Veneno",
        "year": 2013,
        "trim": "50th Anniversary Racing Prototype",
        "rarity_tier": "Mythic 1-of-1",
        "points": 35000,
        "production_count": 4,
        "is_special_edition": True,
        "special_edition_notes": "Only 3 customer coupes ever sold worldwide, based on Aventador chassis with LMP-style dorsal fin.",
        "colorways": ["Grigio Metalluro with Italian Tricolore Accent"],
    },
    {
        "id": "lamborghini-sian-fkp-37",
        "make": "Lamborghini",
        "model": "Sian",
        "year": 2019,
        "trim": "Supercapacitor V12 Hybrid",
        "rarity_tier": "Legendary",
        "points": 15000,
        "production_count": 63,
        "is_special_edition": True,
        "special_edition_notes": "First hybrid Lamborghini utilizing a supercapacitor instead of lithium-ion batteries. 819 HP.",
        "colorways": ["Verde Gea with Oro Electrum Accents", "Blu Uranus"],
    },
    {
        "id": "lamborghini-countach-lpi-800-4",
        "make": "Lamborghini",
        "model": "Countach",
        "year": 2021,
        "trim": "Countach LPI 800-4 Retro Hommage",
        "rarity_tier": "Legendary",
        "points": 14000,
        "production_count": 112,
        "is_special_edition": True,
        "special_edition_notes": "50th anniversary hommage to the LP400 with modern supercapacitor V12 hybrid.",
        "colorways": ["Bianco Siderale", "Impact White", "Giallo Countach"],
    },
    {
        "id": "porsche-carrera-gt",
        "make": "Porsche",
        "model": "Carrera GT",
        "year": 2003,
        "trim": "5.7L V10 6MT Carbon Tub",
        "rarity_tier": "Epic",
        "points": 9500,
        "production_count": 1270,
        "is_special_edition": True,
        "special_edition_notes": "Derived from a cancelled Le Mans V10 engine, featuring a beechwood gearshift and carbon-ceramic clutch.",
        "colorways": ["GT Silver Metallic", "Fayence Yellow", "Guards Red", "Basalt Black"],
    },
    {
        "id": "porsche-918-spyder",
        "make": "Porsche",
        "model": "918",
        "year": 2013,
        "trim": "Weissach Package Hybrid AWD",
        "rarity_tier": "Legendary",
        "points": 15000,
        "production_count": 918,
        "is_special_edition": True,
        "special_edition_notes": "First production car to break the 7-minute Nürburgring Nordschleife barrier (6:57).",
        "colorways": ["Liquid Metal Chrome Blue", "Salzburg Racing Livery", "Martini Racing Livery"],
    },
    {
        "id": "rimac-nevera",
        "make": "Rimac",
        "model": "Nevera",
        "year": 2021,
        "trim": "Quad-Motor Electric Hypercar",
        "rarity_tier": "Legendary",
        "points": 16500,
        "production_count": 150,
        "is_special_edition": True,
        "special_edition_notes": "Record-shattering Croatian hypercar: 0-60 in 1.74s, 23 FIA speed records broken in a single day.",
        "colorways": ["Time Attack Edition (Black/Green)", "Signature Blue", "Calypso Red"],
    },
    {
        "id": "aston-martin-vulcan",
        "make": "Aston Martin",
        "model": "Vulcan",
        "year": 2015,
        "trim": "Track-Only 7.0L V12",
        "rarity_tier": "Legendary",
        "points": 18000,
        "production_count": 24,
        "is_special_edition": True,
        "special_edition_notes": "Track-only 820 HP naturally aspirated 7.0L V12 with carbon fiber monocoque.",
        "colorways": ["Fiamma Red", "Verdant Jade", "Ceramic Grey"],
    },
    {
        "id": "aston-martin-one-77",
        "make": "Aston Martin",
        "model": "One-77",
        "year": 2009,
        "trim": "7.3L Cosworth V12 Flagship",
        "rarity_tier": "Legendary",
        "points": 16000,
        "production_count": 77,
        "is_special_edition": True,
        "special_edition_notes": "Handcrafted aluminum body over carbon tub with 750 HP 7.3L Cosworth V12. 77 units produced.",
        "colorways": ["Black Pearl", "Morning Frost White", "Villa d'Este Blue"],
    },
    {
        "id": "lexus-lfa",
        "make": "Lexus",
        "model": "LFA",
        "year": 2010,
        "trim": "Nürburgring Package 4.8L V10",
        "rarity_tier": "Epic",
        "points": 9500,
        "production_count": 500,
        "is_special_edition": True,
        "special_edition_notes": "Yamaha acoustic-tuned 4.8L V10 revving to 9,000 RPM in 0.6 seconds. Only 50 Nürburgring editions.",
        "colorways": ["Whitest White", "Orange (Nürburgring Package)", "Pearl Red"],
    },
    {
        "id": "ford-gt",
        "make": "Ford",
        "model": "GT",
        "year": 2019,
        "trim": "Heritage Edition EcoBoost V6",
        "rarity_tier": "Rare",
        "points": 4500,
        "production_count": 1350,
        "is_special_edition": True,
        "special_edition_notes": "Carbon-tub Le Mans homologation supercar with active aero flying buttresses.",
        "colorways": ["Gulf Heritage Blue / Orange", "Liquid Red", "Shadow Black"],
    },
]


def integrate():
    db = firestore.Client(project=FIRESTORE_PROJECT_ID)
    cars_ref = db.collection("cars")

    # 1. Fetch all existing documents from Firestore for strict deduplication
    existing_docs = list(cars_ref.stream())
    existing_ids = set()
    existing_make_models = set()

    for doc in existing_docs:
        data = doc.to_dict()
        existing_ids.add(doc.id.strip().lower())
        make = (data.get("make") or "").strip().lower()
        model = (data.get("model") or "").strip().lower()
        if make and model:
            existing_make_models.add(f"{make}::{model}")

    print(f"=== Starting Integration ===")
    print(f"Current Firestore car count: {len(existing_docs)}")
    print(f"Existing IDs: {sorted(list(existing_ids))}")

    # 2. Connect to SQLite to retrieve verified specs
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.row_factory = sqlite3.Row
    cursor = sqlite_conn.cursor()

    skipped_count = 0
    added_count = 0

    for cand in CANDIDATES:
        cand_id = cand["id"].strip().lower()
        cand_make = cand["make"].strip()
        cand_model = cand["model"].strip()
        cand_mm_key = f"{cand_make.lower()}::{cand_model.lower()}"

        # STRICT DUPLICATE CHECK: Skip if either doc ID or make+model exists
        if cand_id in existing_ids or cand_mm_key in existing_make_models:
            print(f"  [SKIPPED - DUPLICATE] '{cand['make']} {cand['model']}' (ID: {cand_id}) already exists in Firestore.")
            skipped_count += 1
            continue

        # Fetch official powertrain/performance specs from SQLite
        cursor.execute(
            """
            SELECT e.label as engine_label, e.power_hp, e.top_speed_kmh, e.zero_to_100_s,
                   e.torque_nm, e.cylinders, e.displacement_cc, e.transmission, e.drivetrain
            FROM models mo
            JOIN makes m ON mo.make_id = m.id
            LEFT JOIN generations g ON g.model_id = mo.id
            LEFT JOIN engines e ON e.generation_id = g.id
            WHERE LOWER(m.name) = LOWER(?) AND LOWER(mo.name) LIKE LOWER(?)
            ORDER BY e.power_hp DESC NULLS LAST
            LIMIT 1
            """,
            (cand_make, f"%{cand_model}%"),
        )
        sqlite_row = cursor.fetchone()

        specs = {}
        if sqlite_row:
            specs = {
                "engine": sqlite_row["engine_label"] or f"{cand_make} Performance Engine",
                "horsepower": int(sqlite_row["power_hp"]) if sqlite_row["power_hp"] else None,
                "top_speed_kmh": int(sqlite_row["top_speed_kmh"]) if sqlite_row["top_speed_kmh"] else None,
                "zero_to_100_s": float(sqlite_row["zero_to_100_s"]) if sqlite_row["zero_to_100_s"] else None,
                "torque_nm": int(sqlite_row["torque_nm"]) if sqlite_row["torque_nm"] else None,
                "cylinders": sqlite_row["cylinders"],
                "displacement_cc": sqlite_row["displacement_cc"],
                "transmission": sqlite_row["transmission"] or "Automated Dual-Clutch / Sequential",
                "drivetrain": sqlite_row["drivetrain"] or "RWD / AWD",
            }
        else:
            specs = {
                "engine": f"{cand_make} Special Edition Powertrain",
                "horsepower": None,
                "top_speed_kmh": None,
            }

        car_doc = {
            "id": cand_id,
            "make": cand_make,
            "model": cand_model,
            "year": cand["year"],
            "trim": cand["trim"],
            "colorways": cand["colorways"],
            "rarity_tier": cand["rarity_tier"],
            "points": cand["points"],
            "production_count": cand["production_count"],
            "global_spotted_count": 0,
            "is_special_edition": cand["is_special_edition"],
            "special_edition_notes": cand["special_edition_notes"],
            "specs": specs,
            "dataset_source": "vehicle-makes-models",
        }

        # Write to Firestore
        cars_ref.document(cand_id).set(car_doc)
        existing_ids.add(cand_id)
        existing_make_models.add(cand_mm_key)
        added_count += 1
        print(f"  [ADDED] '{cand_make} {cand_model}' ({cand['rarity_tier']} - {cand['points']} pts) -> ID: {cand_id}")

    sqlite_conn.close()

    total_final = len(list(cars_ref.stream()))
    print(f"\n=== Summary ===")
    print(f"Skipped duplicates: {skipped_count}")
    print(f"Newly added vehicles: {added_count}")
    print(f"Total cars in Firestore: {total_final}")


if __name__ == "__main__":
    integrate()
