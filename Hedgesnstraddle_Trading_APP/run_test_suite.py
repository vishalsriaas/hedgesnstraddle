import sys
import os
import json
import urllib.request
import urllib.parse
import time

BASE_URL = "http://127.0.0.1:8085"

def make_req(url, method="GET", data=None, headers=None):
    if headers is None:
        headers = {}
    
    encoded_data = None
    if data:
        if isinstance(data, dict) and headers.get("Content-Type") == "application/x-www-form-urlencoded":
            encoded_data = urllib.parse.urlencode(data).encode('utf-8')
        elif isinstance(data, dict):
            encoded_data = json.dumps(data).encode('utf-8')
            headers["Content-Type"] = "application/json"
        elif isinstance(data, bytes):
            encoded_data = data

    req = urllib.request.Request(url, data=encoded_data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode('utf-8')
            return resp.status, json.loads(body) if "json" in resp.headers.get("Content-Type", "") else body
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8')
        return e.code, body
    except Exception as e:
        return 500, str(e)

def run_suite():
    print("=" * 65)
    print("HEDGESNSTRADDLE COMPREHENSIVE AUTOMATED TEST SUITE")
    print("=" * 65)

    # 1. Health Check
    status, res = make_req(f"{BASE_URL}/docs")
    if status != 200:
        print(f"[TEST 1 FAILED] Could not connect to API Gateway on port 8085: HTTP {status}")
        return False
    print(f"[TEST 1] API Gateway Status: HTTP {status} - OK")

    # 2. Authentication Login
    login_payload = {"username": "admin", "password": "Admin@123"}
    status, res = make_req(
        f"{BASE_URL}/api/v1/auth/login", 
        method="POST", 
        data=login_payload, 
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    if status != 200:
        print(f"[TEST 2 FAILED] Login failed with status {status}: {res}")
        return False
    
    token = res.get("access_token")
    headers = {"Authorization": f"Bearer {token}"}
    print(f"[TEST 2] Authentication & JWT Generation: Token Received - OK")

    # 3. Dashboard Snapshot
    status, snap = make_req(f"{BASE_URL}/api/v1/dashboard/snapshot", headers=headers)
    if status != 200:
        print(f"[TEST 3 FAILED] Snapshot failed: {status}")
        return False
    print(f"[TEST 3] Dashboard Snapshot: BTC Mark=${snap['market']['btc_mark_price']}, Spot=${snap['market']['btc_spot_price']} - OK")

    # 4. Straddle Config Read & Update
    status, cfg = make_req(f"{BASE_URL}/api/v1/config/straddle", headers=headers)
    if status != 200:
        print(f"[TEST 4 FAILED] Straddle config read failed")
        return False
    
    update_straddle = {"TRADE_QTY": "0.15", "WINDOW_START": "18:50"}
    status, res = make_req(f"{BASE_URL}/api/v1/config/straddle", method="POST", data=update_straddle, headers=headers)
    if status != 200:
        print(f"[TEST 4 FAILED] Straddle config update failed: {res}")
        return False
    print(f"[TEST 4] Straddle Config Read/Write: {res['message']} - OK")

    # 5. Hedge Config Read & Update
    status, cfg = make_req(f"{BASE_URL}/api/v1/config/hedge", headers=headers)
    if status != 200:
        print(f"[TEST 5 FAILED] Hedge config read failed")
        return False
    
    update_hedge = {"MAX_OPTION_SPEND": "450.0", "Q_MAX_BTC": "1200.0"}
    status, res = make_req(f"{BASE_URL}/api/v1/config/hedge", method="POST", data=update_hedge, headers=headers)
    if status != 200:
        print(f"[TEST 5 FAILED] Hedge config update failed: {res}")
        return False
    print(f"[TEST 5] Hedge Config Read/Write: {res['message']} - OK")

    # 6. Hedge Strategy Rules Update
    strategy_payload = {
        "strategy_name": "Bullish Hedge",
        "enabled": True,
        "direction": "Bullish",
        "trade_start_h": 5, "trade_start_m": 0,
        "trade_end_h": 7, "trade_end_m": 30,
        "contract_qty": 10.0,
        "max_premium": 250.0
    }
    status, res = make_req(f"{BASE_URL}/api/v1/config/hedge/strategies", method="POST", data=strategy_payload, headers=headers)
    if status != 200:
        print(f"[TEST 6 FAILED] Hedge Strategy Rules update failed: {res}")
        return False
    print(f"[TEST 6] Hedge Strategy Rules Update: {res['message']} - OK")

    # 7. Emergency Square-off Straddle
    status, res = make_req(f"{BASE_URL}/api/v1/dashboard/straddle/squareoff", method="POST", headers=headers)
    if status != 200:
        print(f"[TEST 7 FAILED] Straddle Emergency Square-off failed: {res}")
        return False
    print(f"[TEST 7] Straddle Emergency Square-off Signal: {res['message']} - OK")

    # 8. Emergency Square-off Hedge
    status, res = make_req(f"{BASE_URL}/api/v1/dashboard/hedge/squareoff", method="POST", headers=headers)
    if status != 200:
        print(f"[TEST 8 FAILED] Hedge Emergency Square-off failed: {res}")
        return False
    print(f"[TEST 8] Hedge Emergency Square-off Signal: {res['message']} - OK")

    # 9. Config Audit Logs
    status, logs = make_req(f"{BASE_URL}/api/v1/audit/logs", headers=headers)
    if status != 200:
        print(f"[TEST 9 FAILED] Audit logs query failed: {status}")
        return False
    print(f"[TEST 9] Audit Logs Query: {len(logs)} audit entries recorded - OK")

    # 10. CSV Report Export
    status, csv_data = make_req(f"{BASE_URL}/api/v1/reports/trades.csv", headers=headers)
    if status != 200:
        print(f"[TEST 10 FAILED] CSV export failed: {status}")
        return False
    print(f"[TEST 10] Trade CSV Report Export: Received {len(csv_data)} bytes - OK")

    print("=" * 65)
    print("ALL 10 VERIFICATION TESTS PASSED SUCCESSFULLY (100% CLEAN)")
    print("=" * 65)
    return True

if __name__ == "__main__":
    success = run_suite()
    sys.exit(0 if success else 1)
