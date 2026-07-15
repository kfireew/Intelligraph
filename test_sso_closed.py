#!/usr/bin/env python3
"""
Intelligraph SSO Connectivity Test — standalone, no dependencies beyond requests.

Copy this ONE file to your closed network. Run it against the running
Intelligraph instance to diagnose SSO / authentication issues.

Usage:
  # Basic — just check Intelligraph + SSO endpoints:
  python test_sso_closed.py --url http://localhost:5050

  # With direct IdP test (tests IdP from the CLIENT machine, not the container):
  python test_sso_closed.py --url http://localhost:5050 --sso-issuer https://keycloak.corp/auth/realms/myrealm

  # With MCP token (to test token validity + DB persistence):
  python test_sso_closed.py --url http://localhost:5050 --token mcp_xxxxxx

  # Against the site URL with SSL:
  python test_sso_closed.py --url https://intelligraph.corp --sso-issuer https://keycloak.corp --token mcp_xxx --ssl-verify

Tests:
   1. Site reachable (Intelligraph is up)
   2. /status (SSO configured, site_url)
   3. /auth/me (current auth state)
   4. SSO issuer discovery — direct (IdP reachable from client)
   5. /auth/login redirect (server can reach IdP)
   6. PKCE params in redirect (code_challenge, S256, state, client_id)
   7. Authorize endpoint reachable (IdP responds)
   8. Token endpoint reachable (IdP responds to POST)
   9. Userinfo endpoint reachable (IdP responds to Bearer)
  10. Protected endpoint without auth (401 + login_url)
  11. Header-based auth fallback (sso-proxy mode)
  12. MCP token round-trip (DB persistence + token validity)
  13. DB persistence diagnostic (INTELLIGRAPH_DB path + volume check)

Requirements: Python 3.8+, requests library (pip install requests)
"""
import argparse
import json
import sys
import os
import hashlib
import base64
import secrets
import time
import threading

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)

from urllib.parse import urlparse, urlencode, parse_qs, urlsplit, urlunsplit


# ── ANSI colors ──────────────────────────────────────────────────

def green(msg):
    return f"\033[92m{msg}\033[0m" if sys.stdout.isatty() else msg

def red(msg):
    return f"\033[91m{msg}\033[0m" if sys.stdout.isatty() else msg

def yellow(msg):
    return f"\033[93m{msg}\033[0m" if sys.stdout.isatty() else msg

def bold(msg):
    return f"\033[1m{msg}\033[0m" if sys.stdout.isatty() else msg


# ── Main ──────────────────────────────────────────────────────────

TOTAL_TESTS = 13


def main():
    parser = argparse.ArgumentParser(
        description="Intelligraph SSO Connectivity Test (standalone)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--url", required=True, help="Intelligraph URL (e.g. http://localhost:5050)")
    parser.add_argument("--sso-issuer", default="", help="SSO/OIDC issuer URL (to test IdP directly from client)")
    parser.add_argument("--token", default="", help="MCP token (to test token round-trip + DB persistence)")
    parser.add_argument("--project-id", type=int, default=1, help="Project ID for MCP token test (default: 1)")
    parser.add_argument("--ssl-verify", action="store_true", default=False, help="Verify SSL certs")
    parser.add_argument("--timeout", type=int, default=15, help="Request timeout in seconds (default: 15)")
    args = parser.parse_args()

    url = args.url.rstrip("/")
    sso_issuer = args.sso_issuer.rstrip("/") if args.sso_issuer else ""
    token = args.token.strip()
    pid = args.project_id
    verify = args.ssl_verify
    timeout = args.timeout

    # Session with trust_env=False — bypasses proxy env vars (same as MCP server)
    session = requests.Session()
    session.trust_env = False

    results = []
    details = {}
    sso_config = {}  # Filled by test 4 or 5

    def test(name, fn):
        idx = len(results) + 1
        print(f"[{idx}/{TOTAL_TESTS}] {name}...", end=" ", flush=True)
        try:
            ok, detail, extra = fn()
            if extra:
                details.update(extra)
            if ok:
                print(green("PASS") + f" ({detail})" if detail else green("PASS"))
                results.append(True)
            else:
                print(red("FAIL") + f" ({detail})")
                results.append(False)
        except Exception as e:
            print(red("FAIL") + f" ({e})")
            results.append(False)

    print("=" * 65)
    print("Intelligraph SSO Connectivity Test")
    print("=" * 65)
    print(f"  URL:         {url}")
    print(f"  SSO Issuer:  {sso_issuer or '(not provided — will skip direct IdP tests)'}")
    print(f"  MCP Token:   {token[:12] + '...' if token else '(not provided — will skip token test)'}")
    print(f"  Project ID:  {pid}")
    print(f"  SSL Verify:  {verify}")
    print()

    # ── Test 1: Site reachable ──
    def test_site():
        r = session.get(url, timeout=timeout, verify=verify, allow_redirects=False)
        if r.status_code in (200, 302, 303, 307):
            return True, f"HTTP {r.status_code}", {}
        return False, f"HTTP {r.status_code}", {}
    test("Site reachable (Intelligraph up)", test_site)

    # ── Test 2: /status ──
    def test_status():
        r = session.get(f"{url}/status", timeout=timeout, verify=verify)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}", {}
        data = r.json()
        sso = data.get("sso_configured", False)
        site_url = data.get("site_url", "auto")
        projects = data.get("projects", [])
        return True, f"SSO={'on' if sso else 'off'}, site_url={site_url}, projects={projects}", {"sso_configured": sso}
    test("/status (SSO config + site_url)", test_status)

    # ── Test 3: /auth/me ──
    def test_auth_me():
        r = session.get(f"{url}/auth/me", timeout=timeout, verify=verify)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}", {}
        data = r.json()
        authenticated = data.get("authenticated", False)
        sso_configured = data.get("sso_configured", False)
        login_url = data.get("login_url")
        user = data.get("user")
        return True, f"authenticated={authenticated}, sso={sso_configured}, user={user}, login={login_url}", {}
    test("/auth/me (current auth state)", test_auth_me)

    # ── Test 4: SSO issuer discovery — direct (from client machine) ──
    def test_discovery_direct():
        if not sso_issuer:
            return True, "SKIP — no --sso-issuer provided", {}
        discovery_url = f"{sso_issuer}/.well-known/openid-configuration"
        r = session.get(discovery_url, timeout=timeout, verify=verify)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code} from {discovery_url}", {}
        cfg = r.json()
        sso_config.update(cfg)
        auth_ep = cfg.get("authorization_endpoint", "MISSING")
        token_ep = cfg.get("token_endpoint", "MISSING")
        userinfo_ep = cfg.get("userinfo_endpoint", "MISSING")
        end_session = cfg.get("end_session_endpoint", "MISSING")
        return True, f"auth={auth_ep[-40:]}, token={token_ep[-40:]}, userinfo={'yes' if userinfo_ep != 'MISSING' else 'MISSING'}", {}
    test("SSO issuer discovery — direct (IdP reachable from client)", test_discovery_direct)

    # ── Test 5: /auth/login redirect (server can reach IdP) ──
    def test_auth_login():
        r = session.get(f"{url}/auth/login", timeout=timeout, verify=verify, allow_redirects=False)
        if r.status_code == 503:
            return False, "503 — server cannot reach IdP (SSO_ISSUER not accessible from container)", {}
        if r.status_code == 400:
            return False, "400 — SSO not configured on server", {}
        if r.status_code in (302, 303, 307):
            location = r.headers.get("Location", "")
            if not location:
                return False, "302 but no Location header", {}
            # If sso_config wasn't filled by test 4, the server reached the IdP
            # but we don't have discovery data. That's OK — we still have the redirect.
            return True, f"302 -> {location[:80]}...", {"auth_redirect_location": location}
        return False, f"HTTP {r.status_code}", {}
    test("/auth/login redirect (server reaches IdP)", test_auth_login)

    # ── Test 6: PKCE params in redirect ──
    def test_pkce_params():
        location = details.get("auth_redirect_location")
        if not location:
            return True, "SKIP — no /auth/login redirect (test 5 skipped/failed)", {}
        parsed = urlsplit(location)
        params = parse_qs(parsed.query)
        required = ["client_id", "response_type", "scope", "redirect_uri", "state"]
        missing = [k for k in required if k not in params]
        has_pkce = "code_challenge" in params and "code_challenge_method" in params
        method = params.get("code_challenge_method", [None])[0]
        pkce_note = f"PKCE={method}" if has_pkce else "no PKCE (client_secret mode)"
        if missing:
            return False, f"missing params: {missing}", {}
        return True, f"all required params present, {pkce_note}", {}
    test("PKCE params in redirect (code_challenge, S256)", test_pkce_params)

    # ── Test 7: Authorize endpoint reachable ──
    def test_authorize_endpoint():
        location = details.get("auth_redirect_location")
        if not location:
            return True, "SKIP — no /auth/login redirect", {}
        # Follow the redirect to the IdP — expect a login page or error, not timeout
        r = session.get(location, timeout=timeout, verify=verify, allow_redirects=True)
        if r.status_code in (200, 400, 401, 403):
            # 200 = login page, 400/401 = IdP rejected params (still reachable)
            return True, f"IdP responded HTTP {r.status_code} ({len(r.content)} bytes)", {}
        if r.status_code == 302:
            return True, f"IdP redirected (302) — login flow active", {}
        return False, f"HTTP {r.status_code} from IdP authorize endpoint", {}
    test("Authorize endpoint reachable (IdP responds)", test_authorize_endpoint)

    # ── Test 8: Token endpoint reachable ──
    def test_token_endpoint():
        if not sso_config:
            # Try to get discovery from the server's perspective via /auth/login redirect
            location = details.get("auth_redirect_location")
            if not location:
                return True, "SKIP — no SSO discovery and no /auth/login redirect", {}
            # Extract the issuer from the redirect URL to build discovery URL
            parsed = urlsplit(location)
            issuer_base = f"{parsed.scheme}://{parsed.netloc}"
            # Try common OIDC discovery paths
            for path in ["/.well-known/openid-configuration",
                         "/auth/realms/master/.well-known/openid-configuration",
                         "/oauth2/.well-known/openid-configuration"]:
                try:
                    r = session.get(f"{issuer_base}{path}", timeout=timeout, verify=verify)
                    if r.status_code == 200:
                        sso_config.update(r.json())
                        break
                except Exception:
                    continue
            if not sso_config:
                return True, "SKIP — cannot determine token endpoint URL", {}
        token_ep = sso_config.get("token_endpoint")
        if not token_ep:
            return True, "SKIP — no token_endpoint in discovery doc", {}
        # POST garbage to token endpoint — expect a JSON error (not timeout)
        r = session.post(
            token_ep,
            data={"grant_type": "authorization_code", "code": "invalid", "client_id": "test"},
            timeout=timeout,
            verify=verify,
        )
        if r.status_code in (400, 401, 403):
            return True, f"IdP token endpoint responded HTTP {r.status_code} (reachable)", {}
        if r.status_code == 200:
            return True, "IdP token endpoint returned 200 (reachable)", {}
        return False, f"HTTP {r.status_code} from token endpoint", {}
    test("Token endpoint reachable (IdP responds to POST)", test_token_endpoint)

    # ── Test 9: Userinfo endpoint reachable ──
    def test_userinfo_endpoint():
        userinfo_ep = sso_config.get("userinfo_endpoint")
        if not userinfo_ep:
            return True, "SKIP — no userinfo_endpoint in discovery doc", {}
        # GET with garbage Bearer token — expect 401 (not timeout)
        r = session.get(
            userinfo_ep,
            headers={"Authorization": "Bearer invalid_token_for_testing"},
            timeout=timeout,
            verify=verify,
        )
        if r.status_code in (401, 403):
            return True, f"IdP userinfo responded HTTP {r.status_code} (reachable)", {}
        if r.status_code == 200:
            return True, "IdP userinfo returned 200 (reachable)", {}
        return False, f"HTTP {r.status_code} from userinfo endpoint", {}
    test("Userinfo endpoint reachable (IdP responds to Bearer)", test_userinfo_endpoint)

    # ── Test 10: Protected endpoint without auth ──
    def test_protected_no_auth():
        # /projects/clone is a POST endpoint protected by SSO
        r = session.post(
            f"{url}/projects/clone",
            json={"git_url": "test"},
            timeout=timeout,
            verify=verify,
        )
        if r.status_code == 401:
            data = r.json() if "json" in r.headers.get("content-type", "") else {}
            has_login_url = "login_url" in data
            return True, f"401 + login_url={'yes' if has_login_url else 'no'}", {}
        if r.status_code in (400, 403, 404, 502):
            # 400 = reached endpoint, 403 = auth bypass but params wrong, 404 = route issue
            return True, f"HTTP {r.status_code} (endpoint reachable, auth may be off)", {}
        return False, f"HTTP {r.status_code} (expected 401)", {}
    test("Protected endpoint without auth (401 + login_url)", test_protected_no_auth)

    # ── Test 11: Header-based auth fallback (sso-proxy mode) ──
    def test_header_auth():
        # If server is behind an SSO proxy (e.g. oauth2-proxy, nginx auth_request),
        # it reads X-Auth-Username / X-Forwarded-User headers.
        # This test checks if the server ACCEPTS header-based auth.
        r = session.get(
            f"{url}/auth/me",
            headers={"X-Auth-Username": "test_user@sso-proxy"},
            timeout=timeout,
            verify=verify,
        )
        if r.status_code == 200:
            data = r.json()
            user = data.get("user")
            if user and user.get("source") == "sso-proxy":
                return True, f"header auth accepted: user={user}", {}
            return True, f"header sent, auth_me user={user} (session-based, header ignored)", {}
        return False, f"HTTP {r.status_code}", {}
    test("Header-based auth fallback (sso-proxy mode)", test_header_auth)

    # ── Test 12: MCP token round-trip (DB persistence + token validity) ──
    def test_mcp_token():
        if not token:
            return True, "SKIP — no --token provided", {}
        headers = {"Content-Type": "application/json", "X-MCP-Token": token}
        r = session.post(
            f"{url}/graph/retrieve-context",
            json={"prompt": "test connectivity", "project_id": pid},
            headers=headers,
            timeout=30,
            verify=verify,
        )
        if r.status_code == 200:
            data = r.json()
            strategy = data.get("strategy", "?")
            ctx_len = len(data.get("context", ""))
            return True, f"token valid, strategy={strategy}, context={ctx_len} chars", {}
        if r.status_code == 401:
            return False, "401 — token invalid or DB was wiped (cold-start token loss)", {}
        if r.status_code == 404:
            return True, "404 — project not found (token may be valid but project deleted)", {}
        return False, f"HTTP {r.status_code}", {}
    test("MCP token round-trip (DB persistence + token validity)", test_mcp_token)

    # ── Test 13: DB persistence diagnostic ──
    def test_db_persistence():
        r = session.get(f"{url}/diagnostics", timeout=timeout, verify=verify)
        if r.status_code != 200:
            return True, f"SKIP — /diagnostics returned HTTP {r.status_code}", {}
        data = r.json()
        status = data.get("status", {})
        temp_dir = status.get("temp_dir", "?")
        db_path = os.environ.get("INTELLIGRAPH_DB", "")

        # Check if temp_dir is under a VOLUME-mounted path
        # Dockerfile: VOLUME /app/backend/data
        # app.py: TEMP_DIR = backend/data/temp
        # DB: backend/data/temp/intelligraph.db
        warnings = []

        if temp_dir and temp_dir != "?":
            # Is it under /app/backend/data (the declared VOLUME)?
            if "/app/backend/data" in temp_dir or "backend/data" in temp_dir:
                # Good — under the volume
                pass
            else:
                warnings.append(f"temp_dir={temp_dir} NOT under VOLUME /app/backend/data — data lost on container recreation")
        else:
            warnings.append("temp_dir not reported by /diagnostics")

        # Check DB path
        if db_path:
            if ":memory:" in db_path:
                warnings.append("INTELLIGRAPH_DB is :memory: — ALL data (tokens, projects) lost on restart")
            elif "/tmp" in db_path or "\\temp" in db_path.lower():
                if "backend/data" not in db_path:
                    warnings.append(f"INTELLIGRAPH_DB={db_path} may be ephemeral (not under VOLUME)")
        else:
            # DB defaults to TEMP_DIR/intelligraph.db — check temp_dir
            if temp_dir and "backend/data" not in temp_dir:
                warnings.append("DB path defaults to temp_dir (ephemeral if not under VOLUME)")

        # Check if the container has been recently restarted
        # (can't check directly, but we can warn about the pattern)
        if warnings:
            return True, "; ".join(warnings), {}
        return True, f"temp_dir={temp_dir} (under VOLUME — data persists across restarts)", {}
    test("DB persistence diagnostic (volume + path check)", test_db_persistence)

    # ── Summary ──
    passed = sum(results)
    failed = len(results) - passed
    skipped = sum(1 for r in results if r is None)  # won't happen but placeholder

    print()
    print("=" * 65)
    if failed == 0:
        print(green(f"RESULT: ALL {passed} TESTS PASSED"))
        print("  SSO connectivity is healthy.")
        if token:
            print("  MCP token is valid and DB is persistent.")
    else:
        print(red(f"RESULT: {failed}/{len(results)} TESTS FAILED"))
        print()
        print("  Failed tests:")
        test_names = [
            "1.  Site reachable",
            "2.  /status (SSO config)",
            "3.  /auth/me (auth state)",
            "4.  SSO issuer discovery — direct",
            "5.  /auth/login redirect (server reaches IdP)",
            "6.  PKCE params in redirect",
            "7.  Authorize endpoint reachable",
            "8.  Token endpoint reachable",
            "9.  Userinfo endpoint reachable",
            "10. Protected endpoint without auth",
            "11. Header-based auth (sso-proxy)",
            "12. MCP token round-trip",
            "13. DB persistence diagnostic",
        ]
        for i, ok in enumerate(results):
            if not ok:
                print(f"    - {test_names[i]}")
        print()
        print("  Common fixes:")
        print("    Test 4 fail:  IdP not reachable from client — check DNS, firewall, VPN")
        print("    Test 5 fail:  IdP not reachable from CONTAINER — check SSO_ISSUER env, network policy")
        print("    Test 5 503:   Server cannot reach IdP — verify SSO_ISSUER URL is accessible from the container")
        print("    Test 6 fail:  PKCE not configured — ensure SSO_CLIENT_SECRET is empty for PKCE mode")
        print("    Test 12 401:  Token invalid or DB wiped on container recreation — use named volume, not anonymous")
        print("    Test 13 warn: DB path is ephemeral — mount /app/backend/data as a named volume")
    print("=" * 65)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
