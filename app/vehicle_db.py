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

"""Local SQLite database interface for the comprehensive vehicle dataset (164 makes, 2,649 models, 30,535 engines)."""

import os
import sqlite3
from typing import Any

import logging

logger = logging.getLogger(__name__)

GCS_BUCKET_NAME = "cardex-spots-qwiklabs-gcp-04-6f324b699fdd"
GCS_SQLITE_BLOB = "data/vehicles.sqlite"
LOCAL_TMP_SQLITE = "/tmp/vehicles.sqlite"

# Locate vehicles.sqlite database
SQLITE_PATHS = [
    LOCAL_TMP_SQLITE,
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "vehicles.sqlite"),
    "/config/Desktop/vehicle-makes-models-main/data/vehicles.sqlite",
]


def _ensure_sqlite_file() -> str | None:
    """Ensures vehicles.sqlite is available locally, downloading from GCS if needed."""
    for path in SQLITE_PATHS:
        if os.path.exists(path):
            return path

    # In deployed container environments, fetch from GCS bucket
    try:
        from google.cloud import storage

        client = storage.Client()
        bucket = client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(GCS_SQLITE_BLOB)
        if blob.exists():
            logger.info("Downloading %s from gs://%s to %s...", GCS_SQLITE_BLOB, GCS_BUCKET_NAME, LOCAL_TMP_SQLITE)
            blob.download_to_filename(LOCAL_TMP_SQLITE)
            if os.path.exists(LOCAL_TMP_SQLITE):
                return LOCAL_TMP_SQLITE
    except Exception as e:
        logger.warning("Could not download vehicles.sqlite from GCS: %s", e)

    return None


def get_sqlite_connection() -> sqlite3.Connection | None:
    """Returns a connection to the vehicles.sqlite database."""
    path = _ensure_sqlite_file()
    if path and os.path.exists(path):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn
    return None


def search_local_vehicle_database(
    query: str,
    make: str | None = None,
    min_hp: int | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Search the comprehensive local vehicle dataset by make, model, generation, or keyword.

    Args:
        query: Freeform search text (e.g. 'Skyline', 'GT-R', 'Chiron', 'V12').
        make: Optional specific make to filter by (e.g. 'Ferrari', 'Nissan').
        min_hp: Optional minimum horsepower filter.
        limit: Maximum number of vehicle entries to return (default 5).

    Returns:
        A list of matching vehicle records with powertrain specs and performance numbers.
    """
    conn = get_sqlite_connection()
    if not conn:
        return []

    cursor = conn.cursor()
    params: list[Any] = []
    where_clauses: list[str] = []

    if make:
        where_clauses.append("LOWER(m.name) = LOWER(?)")
        params.append(make.strip())

    if query:
        search_pattern = f"%{query.strip()}%"
        where_clauses.append(
            "(m.name LIKE ? OR mo.name LIKE ? OR g.name LIKE ? OR e.label LIKE ? OR (m.name || ' ' || mo.name) LIKE ?)"
        )
        params.extend([search_pattern] * 5)

    if min_hp:
        where_clauses.append("e.power_hp >= ?")
        params.append(min_hp)

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    params.append(limit)

    sql = f"""
        SELECT 
            m.name as make,
            mo.name as model,
            mo.year_start,
            mo.year_end,
            g.name as generation,
            e.label as engine_label,
            e.power_hp,
            e.top_speed_kmh,
            e.zero_to_100_s,
            e.torque_nm,
            e.cylinders,
            e.displacement_cc,
            e.fuel_type,
            e.transmission,
            e.drivetrain,
            e.curb_weight_kg
        FROM models mo
        JOIN makes m ON mo.make_id = m.id
        LEFT JOIN generations g ON g.model_id = mo.id
        LEFT JOIN engines e ON e.generation_id = g.id
        WHERE {where_sql}
        ORDER BY e.power_hp DESC NULLS LAST
        LIMIT ?
    """

    cursor.execute(sql, params)
    rows = cursor.fetchall()
    results = []
    for r in rows:
        results.append({
            "make": r["make"],
            "model": r["model"],
            "year_range": f"{r['year_start'] or ''}-{r['year_end'] or 'Present'}",
            "generation": r["generation"],
            "engine": r["engine_label"],
            "power_hp": r["power_hp"],
            "top_speed_kmh": r["top_speed_kmh"],
            "zero_to_100_s": r["zero_to_100_s"],
            "torque_nm": r["torque_nm"],
            "cylinders": r["cylinders"],
            "displacement_cc": r["displacement_cc"],
            "transmission": r["transmission"],
            "drivetrain": r["drivetrain"],
            "curb_weight_kg": r["curb_weight_kg"],
        })
    conn.close()
    return results


def get_vehicle_specs_by_make_model(make: str, model: str) -> dict[str, Any] | None:
    """Fetch the top performance specification for a specific make and model from the local dataset."""
    conn = get_sqlite_connection()
    if not conn:
        return None

    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT 
            m.name as make,
            mo.name as model,
            mo.year_start,
            mo.year_end,
            g.name as generation,
            e.label as engine_label,
            e.power_hp,
            e.top_speed_kmh,
            e.zero_to_100_s,
            e.torque_nm,
            e.cylinders,
            e.displacement_cc,
            e.transmission,
            e.drivetrain,
            e.curb_weight_kg
        FROM models mo
        JOIN makes m ON mo.make_id = m.id
        LEFT JOIN generations g ON g.model_id = mo.id
        LEFT JOIN engines e ON e.generation_id = g.id
        WHERE LOWER(m.name) = LOWER(?) AND LOWER(mo.name) LIKE LOWER(?)
        ORDER BY e.power_hp DESC NULLS LAST
        LIMIT 1
        """,
        (make.strip(), f"%{model.strip()}%"),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None

    return {
        "make": row["make"],
        "model": row["model"],
        "year_range": f"{row['year_start'] or ''}-{row['year_end'] or 'Present'}",
        "generation": row["generation"],
        "engine": row["engine_label"],
        "power_hp": row["power_hp"],
        "top_speed_kmh": row["top_speed_kmh"],
        "zero_to_100_s": row["zero_to_100_s"],
        "torque_nm": row["torque_nm"],
        "cylinders": row["cylinders"],
        "displacement_cc": row["displacement_cc"],
        "transmission": row["transmission"],
        "drivetrain": row["drivetrain"],
        "curb_weight_kg": row["curb_weight_kg"],
    }
