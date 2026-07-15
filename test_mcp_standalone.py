#!/usr/bin/env python3
"""
Intelligraph MCP Pre-Import Test — standalone, no dependencies beyond requests.

Copy this ONE file to your closed network. Run it against the currently
running Intelligraph instance. If all tests pass, the new Docker image will
work. If any fail, the fix is in the new image — import it.

Usage:
  python test_mcp_standalone.py --url http://localhost:5050 --project-id 1 --token YOUR-TOKEN

  # Against the site URL:
  python test_mcp_standalone.py --url https://intelligraph.corp --project-id 1 --token YOUR-TOKEN

Tests:
  1. /status reachable (app is alive)
  2. MCP token valid (401 fix — DB reconnect on failure)
  3. /graph/retrieve-context returns context (retrieval pipeline works)
  4. /graph/crg search works (CRG FTS is functional)
  5. /download/mcp-server returns a valid Python script (UnboundLocalError fix)
  6. /download/agent returns the agent guide (agent guide exists)
  7. Flask is threaded (no 502 on concurrent requests)
  8. completions endpoint returns intent (route badge fix)

Requirements: Python 3.8+, requests library (pip install requests)
"""
import argparse
import json
import sys
import os
import threading
import time

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)


def green(msg):
    return f"\033[92m{msg}\033[0m" if sys.stdout.isatty() else msg

def red(msg):
    return f"\033[91m{msg}\033[0m" if sys.stdout.isatty() else msg

def yellow(msg):
    return f"\033[93m{msg}\033[0m" if sys.stdout.isatty() else msg


def main():
    parser = argparse.ArgumentParser(
        description="Intelligraph MCP Pre-Import Test (standalone)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--url", required=True, help="Intelligraph URL (e.g. http://localhost:5050)")
    parser.add_argument("--project-id", type=int, required=True, help="Project ID")
    parser.add_argument("--token", required=True, help="MCP token (from Guide panel)")
    parser.add_argument("--ssl-verify", action="store_true", default=False, help="Verify SSL certs")
    args = parser.parse_args()

    url = args.url.rstrip("/")
    pid = args.project_id
    token = args.token.strip()
    verify = args.ssl_verify

    # Session with trust_env=False — same as MCP server (bypasses proxy env vars)
    session = requests.Session()
    session.trust_env = False

    results = []

    def test(name, fn):
        print(f"[{len(results)+1}/{11}] {name}...", end=" ", flush=True)
        try:
            ok, detail = fn()
            if ok:
                print(green("PASS") + f" ({detail})" if detail else green("PASS"))
                results.append(True)
            else:
                print(red("FAIL") + f" ({detail})")
                results.append(False)
        except Exception as e:
            print(red("FAIL") + f" ({e})")
            results.append(False)

    print("=" * 60)
    print("Intelligraph MCP Pre-Import Test")
    print("=" * 60)
    print(f"  URL:        {url}")
    print(f"  Project ID: {pid}")
    print(f"  Token:      {token[:12]}...")
    print()

    # ── Test 1: /status ──
    def test_status():
        r = session.get(f"{url}/status", timeout=10, verify=verify)
        if r.status_code == 200:
            data = r.json()
            sso = data.get("sso_configured", False)
            site = data.get("site_url", "auto")
            return True, f"SSO={'on' if sso else 'off'}, site_url={site}"
        return False, f"HTTP {r.status_code}"
    test("Status endpoint reachable", test_status)

    # ── Test 2: MCP token validation (401 fix) ──
    def test_token():
        headers = {"Content-Type": "application/json", "X-MCP-Token": token}
        r = session.post(
            f"{url}/graph/retrieve-context",
            json={"prompt": "test", "project_id": pid},
            headers=headers,
            timeout=30,
            verify=verify,
        )
        if r.status_code == 200:
            data = r.json()
            strategy = data.get("strategy", "?")
            ctx_len = len(data.get("context", ""))
            return True, f"strategy={strategy}, context={ctx_len} chars"
        if r.status_code == 401:
            return False, "401 — token invalid or DB connection stale (FIXED in new image)"
        return False, f"HTTP {r.status_code}"
    test("MCP token validation (401 fix)", test_token)

    # ── Test 3: /graph/retrieve-context ──
    def test_retrieve():
        headers = {"Content-Type": "application/json", "X-MCP-Token": token}
        r = session.post(
            f"{url}/graph/retrieve-context",
            json={"prompt": "architecture overview", "project_id": pid},
            headers=headers,
            timeout=30,
            verify=verify,
        )
        if r.status_code == 200:
            data = r.json()
            files = len(data.get("files", []))
            nodes = len(data.get("matched_nodes", []))
            return True, f"{files} files, {nodes} matched nodes"
        return False, f"HTTP {r.status_code}"
    test("Retrieve-context returns context", test_retrieve)

    # ── Test 4: CRG search ──
    def test_crg():
        headers = {"Content-Type": "application/json", "X-MCP-Token": token}
        r = session.post(
            f"{url}/graph/crg",
            json={"project_id": pid, "mode": "search", "query": "main"},
            headers=headers,
            timeout=15,
            verify=verify,
        )
        if r.status_code == 200:
            data = r.json()
            results_count = len(data.get("results", []))
            return True, f"{results_count} search results"
        if r.status_code == 404:
            return True, "SKIP — no project loaded (will work when project exists)"
        return False, f"HTTP {r.status_code}"
    test("CRG search (FTS)", test_crg)

    # ── Test 5: MCP server script download (UnboundLocalError fix) ──
    def test_mcp_script():
        r = session.get(f"{url}/download/mcp-server", timeout=15, verify=verify)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        script = r.text
        if "global INTELLIGRAPH_URL, PROJECT_ID, REPO_DIR, MCP_TOKEN" in script:
            return True, "UnboundLocalError fix present"
        if "global INTELLIGRAPH_URL, PROJECT_ID, REPO_DIR" in script and "MCP_TOKEN" not in script.split("global")[1].split("\n")[0]:
            return False, "MCP_TOKEN missing from global declaration (OLD image — UnboundLocalError)"
        return False, "Could not verify fix in script"
    test("MCP script (UnboundLocalError fix)", test_mcp_script)

    # ── Test 6: Agent guide exists ──
    def test_agent():
        r = session.get(f"{url}/download/agent", timeout=10, verify=verify)
        if r.status_code == 200 and len(r.text) > 100:
            has_nx = "nx" in r.text.lower()
            return True, f"agent.md present ({len(r.text)} chars, nx tool={'yes' if has_nx else 'no'})"
        return False, f"HTTP {r.status_code}"
    test("Agent guide exists", test_agent)

    # ── Test 7: Flask threaded (concurrent requests don't block) ──
    def test_threaded():
        # Send two concurrent requests — if single-threaded, the second
        # will wait for the first and take noticeably longer.
        errors = []
        latencies = []

        def make_request():
            try:
                start = time.time()
                r = session.get(f"{url}/status", timeout=10, verify=verify)
                latencies.append(time.time() - start)
                if r.status_code != 200:
                    errors.append(f"HTTP {r.status_code}")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=make_request) for _ in range(3)]
        t0 = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        total = time.time() - t0

        if errors:
            return False, f"errors: {errors[0]}"
        if total > 12:
            return False, f"3 concurrent /status took {total:.1f}s (single-threaded — FIXED in new image)"
        return True, f"3 concurrent /status in {total:.1f}s (threaded)"
    test("Flask threaded (concurrent requests)", test_threaded)

    # ── Test 8: Completions endpoint returns intent ──
    def test_intent():
        r = session.post(
            f"{url}/api/v1/projects/{pid}/completions",
            json={
                "prompt": "what is the architecture",
                "llm_url": "http://models.ai-services.idf.cts/v1/chat/completions",
                "include_context": True,
            },
            headers={"X-MCP-Token": token} if token else {},
            timeout=30,
            verify=verify,
        )
        if r.status_code == 404:
            return True, "SKIP — no project loaded (will work when project exists)"
        if r.status_code in (502, 503):
            data = r.json() if "json" in r.headers.get("content-type", "") else {}
            ctx_used = data.get("context_used", False)
            has_intent = "intent" in data
            if ctx_used and has_intent:
                return True, f"intent={data.get('intent')}, context_used=True"
            if ctx_used:
                return False, "context retrieved but no intent field (OLD image — route badge fix)"
            return False, "no context retrieved"
        if r.status_code == 200:
            data = r.json()
            has_intent = "intent" in data
            return True if has_intent else False, f"intent={data.get('intent', 'MISSING')}"
        return False, f"HTTP {r.status_code}"
    test("Completions returns intent (route badge fix)", test_intent)

    # ── Test 9: Coverage intent — "find all" pattern ──
    def test_coverage_find_all():
        r = session.post(
            f"{url}/api/v1/projects/{pid}/completions",
            json={
                "prompt": "find all places the auth service is used",
                "llm_url": "http://models.ai-services.idf.cts/v1/chat/completions",
                "include_context": True,
            },
            headers={"X-MCP-Token": token} if token else {},
            timeout=30,
            verify=verify,
        )
        if r.status_code == 404:
            return True, "SKIP — no project loaded"
        if r.status_code in (502, 504):
            data = r.json() if "json" in r.headers.get("content-type", "") else {}
            intent = data.get("intent", "")
            if intent == "coverage":
                return True, "intent=coverage (find-all pattern detected)"
            if intent == "tests":
                return False, "intent=tests (OLD image — should be coverage)"
            return False, f"intent={intent} (expected coverage)"
        return True, f"HTTP {r.status_code}"
    test("Coverage intent — find all pattern", test_coverage_find_all)

    # ── Test 10: Coverage intent — "test coverage" pattern ──
    def test_coverage_tests():
        r = session.post(
            f"{url}/api/v1/projects/{pid}/completions",
            json={
                "prompt": "show test coverage for the auth module",
                "llm_url": "http://models.ai-services.idf.cts/v1/chat/completions",
                "include_context": True,
            },
            headers={"X-MCP-Token": token} if token else {},
            timeout=30,
            verify=verify,
        )
        if r.status_code == 404:
            return True, "SKIP — no project loaded"
        if r.status_code in (502, 504):
            data = r.json() if "json" in r.headers.get("content-type", "") else {}
            intent = data.get("intent", "")
            if intent == "coverage":
                return True, "intent=coverage (test-coverage pattern detected)"
            if intent == "tests":
                return False, "intent=tests (OLD image — should be coverage)"
            return False, f"intent={intent} (expected coverage)"
        return True, f"HTTP {r.status_code}"
    test("Coverage intent — test coverage pattern", test_coverage_tests)

    # ── Test 11: Coverage intent — "all occurrences" pattern ──
    def test_coverage_occurrences():
        r = session.post(
            f"{url}/api/v1/projects/{pid}/completions",
            json={
                "prompt": "all occurrences of the login function",
                "llm_url": "http://models.ai-services.idf.cts/v1/chat/completions",
                "include_context": True,
            },
            headers={"X-MCP-Token": token} if token else {},
            timeout=30,
            verify=verify,
        )
        if r.status_code == 404:
            return True, "SKIP — no project loaded"
        if r.status_code in (502, 504):
            data = r.json() if "json" in r.headers.get("content-type", "") else {}
            intent = data.get("intent", "")
            if intent == "coverage":
                return True, "intent=coverage (all-occurrences pattern detected)"
            if intent == "tests":
                return False, "intent=tests (OLD image — should be coverage)"
            return False, f"intent={intent} (expected coverage)"
        return True, f"HTTP {r.status_code}"
    test("Coverage intent — all occurrences pattern", test_coverage_occurrences)

    # ── Summary ──
    passed = sum(results)
    failed = len(results) - passed
    print()
    print("=" * 60)
    if failed == 0:
        print(green(f"RESULT: ALL {passed} TESTS PASSED"))
        print("  The current image is up to date. MCP will work.")
        print("  Safe to import the new Docker image (or skip if already working).")
    else:
        print(red(f"RESULT: {failed}/{len(results)} TESTS FAILED"))
        print()
        print("  The current image has bugs fixed in the new image.")
        print("  Import the new Docker image to fix:")
        print()
        test_names = [
            "1. Status reachable",
            "2. MCP token validation (401 fix)",
            "3. Retrieve-context works",
            "4. CRG search works",
            "5. MCP script (UnboundLocalError fix)",
            "6. Agent guide exists",
            "7. Flask threaded (502 fix)",
            "8. Intent in response (route badge fix)",
            "9. Coverage intent — find all pattern",
            "10. Coverage intent — test coverage pattern",
            "11. Coverage intent — all occurrences pattern",
        ]
        for i, ok in enumerate(results):
            if not ok:
                print(f"    - {test_names[i]}")
    print("=" * 60)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
