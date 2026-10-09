#!/usr/bin/env python3
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

"""Migration script to add production_units column to vehicles database and seed limited edition caps."""

import os
import sqlite3
import sys

DEFAULT_MASS_PRODUCTION_UNITS = 1_000_000

# Known global production run numbers for limited editions, halo hypercars, and special series
PRODUCTION_CAPS = {
    # Porsche
    ("Porsche", "Carrera GT"): 1270,
    ("Porsche", "911 GT3 RS 4.0"): 600,
    ("Porsche", "918 Spyder"): 918,
    ("Porsche", "911 GT2 RS"): 1000,
    ("Porsche", "911 Sport Classic"): 1250,
    ("Porsche", "911 Dakar"): 2500,
    ("Porsche", "911 S/T"): 1963,
    ("Porsche", "911 R"): 991,
    ("Porsche", "959"): 292,

    # McLaren
    ("McLaren", "F1"): 106,
    ("McLaren", "F1 LM"): 6,
    ("McLaren", "P1"): 375,
    ("McLaren", "P1 GTR"): 58,
    ("McLaren", "Senna"): 500,
    ("McLaren", "Speedtail"): 106,
    ("McLaren", "Elva"): 149,
    ("McLaren", "Sabre"): 15,
    ("McLaren", "Solus GT"): 25,

    # Lexus
    ("Lexus", "LFA"): 500,
    ("Lexus", "LFA Nurburgring Edition"): 50,

    # Bugatti
    ("Bugatti", "Chiron"): 500,
    ("Bugatti", "Chiron Super Sport 300+"): 30,
    ("Bugatti", "Chiron Pur Sport"): 60,
    ("Bugatti", "Chiron Profilée"): 1,
    ("Bugatti", "Profilee"): 1,
    ("Bugatti", "Veyron"): 450,
    ("Bugatti", "Divo"): 40,
    ("Bugatti", "Centodieci"): 10,
    ("Bugatti", "La Voiture Noire"): 1,
    ("Bugatti", "Bolide"): 40,
    ("Bugatti", "Tourbillon"): 250,
    ("Bugatti", "EB110"): 139,

    # Ferrari
    ("Ferrari", "Daytona SP3"): 599,
    ("Ferrari", "Monza SP1"): 250,
    ("Ferrari", "Monza SP2"): 249,
    ("Ferrari", "LaFerrari"): 499,
    ("Ferrari", "LaFerrari Aperta"): 210,
    ("Ferrari", "Enzo"): 399,
    ("Ferrari", "F50"): 349,
    ("Ferrari", "F40"): 1311,
    ("Ferrari", "288 GTO"): 272,
    ("Ferrari", "599 GTO"): 599,
    ("Ferrari", "F12tdf"): 799,
    ("Ferrari", "812 Competizione"): 999,
    ("Ferrari", "812 Competizione A"): 599,

    # Lamborghini
    ("Lamborghini", "Veneno"): 13,
    ("Lamborghini", "Sian"): 63,
    ("Lamborghini", "Sian Roadster"): 19,
    ("Lamborghini", "Countach LPI 800-4"): 112,
    ("Lamborghini", "Reventon"): 21,
    ("Lamborghini", "Centenario"): 40,
    ("Lamborghini", "Sesto Elemento"): 20,
    ("Lamborghini", "Murcielago SV"): 186,
    ("Lamborghini", "Aventador SVJ"): 900,

    # Pagani
    ("Pagani", "Zonda"): 140,
    ("Pagani", "Huayra"): 100,
    ("Pagani", "Huayra BC"): 20,
    ("Pagani", "Huayra Roadster BC"): 40,
    ("Pagani", "Huayra Codalunga"): 5,
    ("Pagani", "Utopia"): 99,

    # Koenigsegg
    ("Koenigsegg", "Agera RS"): 25,
    ("Koenigsegg", "Jesko"): 125,
    ("Koenigsegg", "Regera"): 80,
    ("Koenigsegg", "One:1"): 6,
    ("Koenigsegg", "CC850"): 70,
    ("Koenigsegg", "Gemera"): 300,

    # Aston Martin
    ("Aston Martin", "Valkyrie"): 150,
    ("Aston Martin", "Valkyrie AMR Pro"): 40,
    ("Aston Martin", "One-77"): 77,
    ("Aston Martin", "Vulcan"): 24,
    ("Aston Martin", "Victor"): 1,
    ("Aston Martin", "Valour"): 110,

    # Mercedes-AMG
    ("Mercedes-Benz", "AMG ONE"): 275,
    ("Mercedes-Benz", "SLR McLaren Stirling Moss"): 75,
    ("Mercedes-Benz", "CLK GTR"): 25,
    ("Mercedes-Benz", "SLS AMG Black Series"): 350,
    ("Mercedes-Benz", "AMG GT Black Series"): 1700,

    # BMW
    ("BMW", "M3 CSL"): 1383,
    ("BMW", "M4 CSL"): 1000,
    ("BMW", "3.0 CSL"): 50,
    ("BMW", "M4 GTS"): 700,

    # Ford
    ("Ford", "GT"): 1350,
}


def find_database_path() -> str:
    """Find the active vehicles.sqlite database file."""
    candidates = [
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "vehicles.sqlite")),
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app", "vehicles.sqlite")),
        "/config/Desktop/vehicle-makes-models-main/data/vehicles.sqlite",
    ]
    for p in candidates:
        if os.path.exists(p) and not os.path.islink(p):
            return p
        if os.path.exists(p):
            return os.path.realpath(p)
    return candidates[0]


def run_migration():
    db_path = find_database_path()
    print(f"Target SQLite database: {db_path}")

    if not os.path.exists(db_path):
        print(f"Error: Database file does not exist at {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 1. Add production_units column to models table if it does not exist
    cursor.execute("PRAGMA table_info(models)")
    model_cols = [row[1] for row in cursor.fetchall()]
    if "production_units" not in model_cols:
        print("Adding 'production_units' column to 'models' table...")
        cursor.execute(f"ALTER TABLE models ADD COLUMN production_units INTEGER DEFAULT {DEFAULT_MASS_PRODUCTION_UNITS}")
    else:
        print("'production_units' column already exists on 'models' table.")

    # 2. Ensure vehicles table exists and has production_units
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='vehicles'")
    has_vehicles = cursor.fetchone() is not None

    if not has_vehicles:
        print("Creating 'vehicles' table with 'production_units'...")
        cursor.execute(f"""
            CREATE TABLE vehicles (
                id INTEGER PRIMARY KEY,
                make TEXT NOT NULL,
                model TEXT NOT NULL,
                year_start INTEGER,
                year_end INTEGER,
                production_units INTEGER DEFAULT {DEFAULT_MASS_PRODUCTION_UNITS}
            )
        """)
        cursor.execute("""
            INSERT INTO vehicles (id, make, model, year_start, year_end, production_units)
            SELECT mo.id, mk.name, mo.name, mo.year_start, mo.year_end, COALESCE(mo.production_units, 1000000)
            FROM models mo
            JOIN makes mk ON mo.make_id = mk.id
        """)
    else:
        cursor.execute("PRAGMA table_info(vehicles)")
        v_cols = [row[1] for row in cursor.fetchall()]
        if "production_units" not in v_cols:
            print("Adding 'production_units' column to 'vehicles' table...")
            cursor.execute(f"ALTER TABLE vehicles ADD COLUMN production_units INTEGER DEFAULT {DEFAULT_MASS_PRODUCTION_UNITS}")

    # Set default mass production units where null
    cursor.execute(f"UPDATE models SET production_units = {DEFAULT_MASS_PRODUCTION_UNITS} WHERE production_units IS NULL")
    cursor.execute(f"UPDATE vehicles SET production_units = {DEFAULT_MASS_PRODUCTION_UNITS} WHERE production_units IS NULL")

    # 3. Seed known limited edition production caps
    seeded_count = 0
    for (make, model_keyword), units in PRODUCTION_CAPS.items():
        # Update models table
        cursor.execute("""
            UPDATE models
            SET production_units = ?
            WHERE id IN (
                SELECT mo.id FROM models mo
                JOIN makes mk ON mo.make_id = mk.id
                WHERE LOWER(mk.name) = LOWER(?) AND LOWER(mo.name) LIKE LOWER(?)
            )
        """, (units, make, f"%{model_keyword}%"))
        updated_models = cursor.rowcount

        # Update vehicles table
        cursor.execute("""
            UPDATE vehicles
            SET production_units = ?
            WHERE LOWER(make) = LOWER(?) AND LOWER(model) LIKE LOWER(?)
        """, (units, make, f"%{model_keyword}%"))
        updated_vehicles = cursor.rowcount

        # If not present in existing models/vehicles, insert special record into vehicles
        if updated_vehicles == 0:
            cursor.execute("""
                INSERT INTO vehicles (make, model, year_start, year_end, production_units)
                VALUES (?, ?, NULL, NULL, ?)
            """, (make, model_keyword, units))
            seeded_count += 1
        else:
            seeded_count += updated_vehicles

    conn.commit()

    # Verification query
    cursor.execute("""
        SELECT make, model, production_units 
        FROM vehicles 
        WHERE production_units < 1000000 
        ORDER BY production_units ASC 
        LIMIT 10
    """)
    rows = cursor.fetchall()
    print(f"\nSuccessfully seeded limited production caps! Found {len(rows)} lowest-production samples:")
    for r in rows:
        print(f"  - {r[0]} {r[1]}: {r[2]:,} units")

    conn.close()
    print("\nDatabase migration completed cleanly.")


if __name__ == "__main__":
    run_migration()
