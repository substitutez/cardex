#!/usr/bin/env python3
"""CarDex Autonomous Authentication & Security Pipeline Verification Runner.

Tests rate limiting, PBKDF2/token_hex security, Apple Guideline 5.1.1(v)
account deletion, demo spotter authentication, and Firestore quota initialization.
"""

import sys
import time
import uuid
from pathlib import Path

# Add frontend directory to path
cardex_dir = Path(__file__).resolve().parent.parent
frontend_dir = str(cardex_dir / "frontend")
if frontend_dir not in sys.path:
    sys.path.insert(0, frontend_dir)

import auth_db
import main as frontend_main
from fastapi.testclient import TestClient

client = TestClient(frontend_main.app)


def test_registration_success():
    """1. Registers a fresh user and verifies session token + initial 5/5 quota."""
    unique_suffix = uuid.uuid4().hex[:8]
    username = f"spotter_{unique_suffix}"
    email = f"spotter_{unique_suffix}@cardex.internal"
    password = "SecureSpotter2026!"

    headers = {"X-Forwarded-For": "10.0.1.1"}
    payload = {"username": username, "email": email, "password": password}

    res = client.post("/api/auth/register", json=payload, headers=headers)
    assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"

    data = res.json()
    assert data.get("success") is True, f"Expected success: True, got {data}"
    assert "token" in data and len(data["token"]) == 64, f"Token must be 32-byte hex (64 chars), got {data.get('token')}"

    # Verify session token validity
    user_id = auth_db.validate_session(data["token"])
    assert user_id == username.lower(), f"Expected user_id {username.lower()}, got {user_id}"

    # Verify initial 5/5 quota in registration response
    quota = data.get("quota") or data.get("user", {}).get("quota")
    assert quota is not None, "Quota missing from registration response"
    assert quota.get("daily_limit") == 5, f"Expected daily_limit 5, got {quota.get('daily_limit')}"
    assert quota.get("scans_today") == 0, f"Expected scans_today 0, got {quota.get('scans_today')}"
    assert quota.get("remaining_scans") == 5 or quota.get("remaining_free") == 5, f"Expected 5 remaining scans, got {quota}"

    # Also verify quota endpoint returns 5/5 quota
    q_res = client.get("/api/user/quota", headers={"Authorization": f"Bearer {data['token']}"})
    assert q_res.status_code == 200, f"Quota endpoint failed: {q_res.text}"
    q_data = q_res.json()
    assert q_data.get("daily_limit") == 5, f"Quota API daily_limit != 5: {q_data}"
    assert q_data.get("scans_today") == 0, f"Quota API scans_today != 0: {q_data}"

    return username, email, password, data["token"]


def test_registration_duplicate(username, password):
    """2. Re-registers the same username and verifies HTTP 400."""
    headers = {"X-Forwarded-For": "10.0.1.2"}
    payload = {
        "username": username,
        "email": f"diff_{uuid.uuid4().hex[:6]}@cardex.internal",
        "password": password,
    }
    res = client.post("/api/auth/register", json=payload, headers=headers)
    assert res.status_code == 400, f"Expected HTTP 400 for duplicate username, got {res.status_code}: {res.text}"
    data = res.json()
    assert data.get("success") is False, f"Expected success: False, got {data}"
    err = data.get("error") or data.get("detail") or ""
    assert "already taken" in err.lower() or "choose another" in err.lower(), f"Unexpected error text: {err}"


def test_login_valid_credentials(username, email, password):
    """3. Authenticates with correct password and checks valid session response."""
    headers = {"X-Forwarded-For": "10.0.1.3"}
    payload = {"username": username, "password": password}

    res = client.post("/api/auth/login", json=payload, headers=headers)
    assert res.status_code == 200, f"Expected 200 for valid login, got {res.status_code}: {res.text}"

    data = res.json()
    assert data.get("success") is True, f"Expected success: True, got {data}"
    assert "token" in data and len(data["token"]) == 64, f"Invalid session token: {data.get('token')}"

    user = data.get("user", {})
    assert user.get("user_id") == username.lower(), f"user_id mismatch: {user.get('user_id')}"
    assert user.get("email") == email, f"email mismatch: {user.get('email')}"


def test_login_invalid_credentials(username):
    """4. Sends wrong password and verifies HTTP 401 Unauthorized."""
    headers = {"X-Forwarded-For": "10.0.1.4"}
    payload = {"username": username, "password": "CompletelyWrongPassword!123"}

    res = client.post("/api/auth/login", json=payload, headers=headers)
    assert res.status_code == 401, f"Expected 401 Unauthorized, got {res.status_code}: {res.text}"
    data = res.json()
    assert data.get("success") is False, f"Expected success: False, got {data}"


def test_auth_rate_limiting():
    """5. Sends 6 rapid bad login attempts and verifies HTTP 429 on the 6th call."""
    ip = "198.51.100.99"
    headers = {"X-Forwarded-For": ip}
    payload = {"username": "rate_limited_tester", "password": "WrongPassword!"}

    # First 5 calls must be processed through auth and return 401
    for attempt in range(1, 6):
        res = client.post("/api/auth/login", json=payload, headers=headers)
        assert res.status_code == 401, f"Attempt {attempt} expected 401, got {res.status_code}: {res.text}"

    # 6th call must exceed the sliding-window limit and return HTTP 429
    res6 = client.post("/api/auth/login", json=payload, headers=headers)
    assert res6.status_code == 429, f"Attempt 6 expected 429, got {res6.status_code}: {res6.text}"

    data6 = res6.json()
    detail = data6.get("detail", "")
    assert "Too many attempts" in detail and "5 minutes" in detail, f"Unexpected 429 detail: {detail}"


def test_demo_account():
    """6. Verifies @Dan_the_spotter logs in successfully and returns valid garage telemetry."""
    headers = {"X-Forwarded-For": "10.0.1.6"}
    # Test with leading '@' as spotters and reviewers often type
    payload = {"username": "@Dan_the_spotter", "password": "password123"}

    res = client.post("/api/auth/login", json=payload, headers=headers)
    assert res.status_code == 200, f"Expected 200 for demo login, got {res.status_code}: {res.text}"

    data = res.json()
    assert data.get("success") is True, f"Expected success: True, got {data}"
    demo_token = data.get("token")
    assert demo_token is not None, "Missing token for demo account"

    user = data.get("user", {})
    assert user.get("user_id") == "dan_the_spotter", f"user_id mismatch: {user.get('user_id')}"
    assert user.get("total_points", 0) > 0, f"Demo account should have non-zero points: {user}"

    # Verify garage telemetry submissions
    sub_res = client.get(
        "/api/user/submissions?user_id=dan_the_spotter",
        headers={"Authorization": f"Bearer {demo_token}"},
    )
    assert sub_res.status_code == 200, f"Submissions query failed: {sub_res.text}"
    sub_data = sub_res.json()
    submissions = sub_data.get("submissions", [])
    assert len(submissions) > 0, f"Expected populated garage submissions for demo user, got {len(submissions)}"


def test_account_deletion():
    """7. Deletes a test account via /api/auth/delete-account and verifies subsequent login fails and data is purged."""
    unique_suffix = uuid.uuid4().hex[:8]
    del_username = f"del_user_{unique_suffix}"
    del_email = f"del_{unique_suffix}@cardex.internal"
    del_password = "DeleteTest2026!"

    # 1. Register fresh test account
    reg_res = client.post(
        "/api/auth/register",
        json={"username": del_username, "email": del_email, "password": del_password},
        headers={"X-Forwarded-For": "10.0.1.7"},
    )
    assert reg_res.status_code == 200, f"Registration failed: {reg_res.text}"
    token = reg_res.json()["token"]
    user_id = del_username.lower()

    # 2. Verify account exists in Firestore
    db = auth_db.get_firestore_client()
    user_ref = db.collection("users").document(user_id)
    assert user_ref.get().exists is True, f"Firestore document users/{user_id} should exist before deletion"

    # Add a mock spot in garage subcollection to verify recursive subcollection purge
    user_ref.collection("garage").document("test_car_1").set({"car_name": "Porsche GT3 RS", "points": 1500})
    assert user_ref.collection("garage").document("test_car_1").get().exists is True

    # 3. Request account deletion
    del_res = client.post(
        "/api/auth/delete-account",
        headers={"Authorization": f"Bearer {token}"},
        json={"user_id": user_id},
    )
    assert del_res.status_code == 200, f"Expected 200 for delete-account, got {del_res.status_code}: {del_res.text}"
    del_data = del_res.json()
    assert del_data.get("success") is True, f"Delete response missing success: {del_data}"

    # 4. Verify subsequent login fails with HTTP 401
    login_res = client.post(
        "/api/auth/login",
        json={"username": del_username, "password": del_password},
        headers={"X-Forwarded-For": "10.0.1.8"},
    )
    assert login_res.status_code == 401, f"Expected 401 for deleted user login, got {login_res.status_code}: {login_res.text}"

    # 5. Verify data is purged from Firestore
    assert user_ref.get().exists is False, f"Firestore document users/{user_id} still exists after deletion"
    assert user_ref.collection("garage").document("test_car_1").get().exists is False, "Garage subcollection spot was not purged"

    # 6. Verify session token is invalidated
    assert auth_db.validate_session(token) is None, "Session token still valid after account deletion"


def main():
    print("=" * 80)
    print("🛡️  CARDEX AUTONOMOUS AUTHENTICATION & SECURITY PIPELINE TEST RUNNER")
    print("=" * 80)

    tests = [
        ("test_registration_success", test_registration_success),
        ("test_registration_duplicate", test_registration_duplicate),
        ("test_login_valid_credentials", test_login_valid_credentials),
        ("test_login_invalid_credentials", test_login_invalid_credentials),
        ("test_auth_rate_limiting", test_auth_rate_limiting),
        ("test_demo_account", test_demo_account),
        ("test_account_deletion", test_account_deletion),
    ]

    results = []
    all_passed = True
    shared_state = {}

    for name, func in tests:
        print(f"\n[ RUNNING ] {name}...")
        start_time = time.perf_counter()
        try:
            if name == "test_registration_success":
                u, e, p, t = func()
                shared_state["username"] = u
                shared_state["email"] = e
                shared_state["password"] = p
                shared_state["token"] = t
            elif name == "test_registration_duplicate":
                func(shared_state["username"], shared_state["password"])
            elif name == "test_login_valid_credentials":
                func(shared_state["username"], shared_state["email"], shared_state["password"])
            elif name == "test_login_invalid_credentials":
                func(shared_state["username"])
            else:
                func()

            duration = time.perf_counter() - start_time
            print(f"[  PASS   ] {name} completed in {duration:.3f}s")
            results.append((name, f"{duration:.3f}s", "PASS"))
        except Exception as ex:
            duration = time.perf_counter() - start_time
            print(f"[  FAIL   ] {name} failed in {duration:.3f}s: {ex}")
            results.append((name, f"{duration:.3f}s", f"FAIL: {ex}"))
            all_passed = False
            import traceback
            traceback.print_exc()

    print("\n" + "=" * 80)
    print("DIAGNOSTIC VERIFICATION SUMMARY")
    print("=" * 80)
    print(f"{'Test Name':<35} | {'Execution Time':<16} | {'Status':<10}")
    print("-" * 35 + "-+-" + "-" * 16 + "-+-" + "-" * 10)
    for t_name, t_time, t_status in results:
        print(f"{t_name:<35} | {t_time:<16} | {t_status:<10}")
    print("=" * 80)

    if not all_passed:
        print("❌ SOME AUTH PIPELINE TESTS FAILED.")
        sys.exit(1)
    else:
        print("✅ ALL 7 AUTH PIPELINE TESTS PASSED SUCCESSFULLY.")
        sys.exit(0)


if __name__ == "__main__":
    main()
