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

"""Unit tests for CarDex user authentication and persistence."""

import sys
import uuid
import pytest
from pathlib import Path

# Add frontend directory to path so auth_db is importable
frontend_dir = str(Path(__file__).resolve().parent.parent / "frontend")
if frontend_dir not in sys.path:
    sys.path.insert(0, frontend_dir)

import auth_db


def test_password_hashing_and_verification():
    raw_pw = "SupercarSpotter2026!"
    hashed, salt = auth_db.hash_password(raw_pw)
    assert hashed != raw_pw
    assert len(salt) > 0
    assert auth_db.verify_password(raw_pw, hashed, salt) is True
    assert auth_db.verify_password("wrongpassword", hashed, salt) is False


def test_calculate_rank():
    assert auth_db.calculate_rank(0) == "Rookie Spotter"
    assert auth_db.calculate_rank(500) == "Rookie Spotter"
    assert auth_db.calculate_rank(1500) == "Amateur Spotter"
    assert auth_db.calculate_rank(6000) == "Pro Spotter"
    assert auth_db.calculate_rank(20000) == "Senior Spotter"
    assert auth_db.calculate_rank(60000) == "Master Spotter"
    assert auth_db.calculate_rank(150000) == "Apex Legend"


def test_user_registration_and_authentication():
    unique_suffix = uuid.uuid4().hex[:8]
    username = f"spotter_{unique_suffix}"
    email = f"spotter_{unique_suffix}@example.com"
    password = "SafePassword99!"

    # 1. Register user
    reg_result = auth_db.register_user(username=username, email=email, password=password)
    assert "token" in reg_result
    assert reg_result["user"]["username"] == username
    assert reg_result["user"]["email"] == email
    assert reg_result["user"]["total_points"] == 0
    assert reg_result["user"]["rank"] == "Rookie Spotter"

    # 2. Validate session token
    user_id = auth_db.validate_session(reg_result["token"])
    assert user_id == username.lower()

    # 3. Duplicate registration should raise ValueError
    with pytest.raises(ValueError, match="already taken"):
        auth_db.register_user(username=username, email=f"other_{unique_suffix}@example.com", password=password)

    with pytest.raises(ValueError, match="already registered"):
        auth_db.register_user(username=f"other_{unique_suffix}", email=email, password=password)

    # 4. Authenticate user
    auth_result = auth_db.authenticate_user(username_or_email=username, password=password)
    assert "token" in auth_result
    assert auth_result["user"]["username"] == username

    # 5. Authenticate with email
    auth_by_email = auth_db.authenticate_user(username_or_email=email, password=password)
    assert auth_by_email["user"]["username"] == username

    # 6. Invalid password should fail
    with pytest.raises(ValueError, match="Invalid"):
        auth_db.authenticate_user(username_or_email=username, password="wrong_password")


def test_user_profile_and_stats():
    profile = auth_db.get_user_profile("dan_the_spotter")
    assert profile["user_id"] == "dan_the_spotter"
    assert profile["total_points"] > 0
    assert profile["total_spots"] > 0

    stats = auth_db.get_user_stats("dan_the_spotter")
    assert stats["submissions_count"] == stats["total_spots"]
    assert isinstance(stats["rarity_counts"], dict)

    submissions = auth_db.get_user_submissions("dan_the_spotter")
    assert len(submissions) > 0
    first_spot = submissions[0]
    assert "car_name" in first_spot
    assert "rarity_tier" in first_spot
    assert "points_awarded" in first_spot
