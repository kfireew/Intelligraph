"""Quick connectivity test for Intelligraph MCP.

Usage:
  python test_mcp_conn.py http://localhost:5050

Run this on the closed-network machine to verify:
  1. The container is reachable
  2. /graph/ endpoints exist
  3. Token auth works (only after loading the new tar)

Before loading new tar:  steps 1-2 pass, step 3 shows "no token endpoint"
After loading new tar:   all steps pass
"""
import sys
import json
import requests

def main():
    url = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:5050"
    print(f"Testing Intelligraph at {url}")
    print()

    # 1. Container reachable?
    try:
        r = requests.get(f"{url}/status", timeout=10, verify=False)
        print(f"[1] Container reachable:     {r.status_code}  {'OK' if r.status_code == 200 else 'FAIL'}")
    except Exception as e:
        print(f"[1] Container reachable:     FAIL - {e}")
        print("    Fix: check container is running and port is mapped")
        return

    # 2. /graph/retrieve-context exists?
    try:
        r = requests.post(
            f"{url}/graph/retrieve-context",
            json={"prompt": "test", "project_id": 1},
            timeout=30, verify=False,
        )
        if r.status_code == 401:
            print(f"[2] /graph/ endpoint exists: {r.status_code}  OK (auth-gated, expected)")
        elif r.status_code == 200:
            data = r.json()
            print(f"[2] /graph/ endpoint exists: {r.status_code}  OK (returned context)")
        else:
            print(f"[2] /graph/ endpoint exists: {r.status_code}  {r.text[:100]}")
    except Exception as e:
        print(f"[2] /graph/ endpoint exists: FAIL - {e}")

    # 3. MCP token endpoint exists? (only on new tar)
    try:
        r = requests.post(f"{url}/projects/1/mcp-token", timeout=10, verify=False)
        if r.status_code == 401:
            print(f"[3] MCP token endpoint:      {r.status_code}  OK (needs SSO login — use web UI to generate)")
        elif r.status_code == 404:
            print(f"[3] MCP token endpoint:      {r.status_code}  NOT FOUND (old image — load new tar)")
        elif r.status_code == 405:
            print(f"[3] MCP token endpoint:      {r.status_code}  NOT FOUND (old image — load new tar)")
        else:
            print(f"[3] MCP token endpoint:      {r.status_code}  {r.text[:100]}")
    except Exception as e:
        print(f"[3] MCP token endpoint:      FAIL - {e}")

    # 4. Test with token (if provided)
    token = sys.argv[2] if len(sys.argv) > 2 else ""
    if token:
        try:
            r = requests.post(
                f"{url}/graph/retrieve-context",
                json={"prompt": "test", "project_id": 1},
                headers={"X-MCP-Token": token},
                timeout=30, verify=False,
            )
            if r.status_code == 200:
                print(f"[4] Token auth works:        {r.status_code}  OK!")
            elif r.status_code == 401:
                print(f"[4] Token auth works:        {r.status_code}  FAIL (token rejected or old image)")
            else:
                print(f"[4] Token auth works:        {r.status_code}  {r.text[:100]}")
        except Exception as e:
            print(f"[4] Token auth works:        FAIL - {e}")
    else:
        print(f"[4] Token auth:              skipped (no token arg)")
        print(f"    After generating a token in the web UI, run:")
        print(f"    python test_mcp_conn.py {url} mcp_YOUR_TOKEN_HERE")

    print()
    print("Expected results:")
    print("  OLD image:  [1] OK  [2] 401  [3] 404  [4] skipped")
    print("  NEW image:  [1] OK  [2] 401  [3] 401  [4] OK (with token)")

if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")  # suppress SSL warnings
    main()
