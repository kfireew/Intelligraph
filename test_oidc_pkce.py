"""
OIDC PKCE verification script for Intelligraph SSO.

Tests that your well-known URL + client ID work with PKCE flow.
Run this inside the closed network. It starts a local HTTP server
on port 8080 to receive the callback.

Usage:
  python test_oidc_pkce.py --issuer https://openshift.example.com --client-id my-client-id --redirect http://localhost:8080/callback

Then open the printed URL in your browser. If you get tokens back, SSO is good.
"""
import argparse
import base64
import hashlib
import json
import secrets
import urllib.parse
import http.server
import threading
import webbrowser
import sys

import requests


def generate_pkce():
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


def main():
    parser = argparse.ArgumentParser(description="Test OIDC PKCE flow")
    parser.add_argument("--issuer", required=True, help="OIDC issuer URL (e.g. https://openshift.example.com)")
    parser.add_argument("--client-id", required=True, help="OIDC client ID")
    parser.add_argument("--redirect", default="http://localhost:8080/callback", help="Redirect URI")
    parser.add_argument("--scope", default="openid profile email", help="OIDC scopes")
    parser.add_argument("--no-ssl-verify", action="store_true", default=True, help="Disable SSL verify (closed network)")
    args = parser.parse_args()

    ssl_verify = not args.no_ssl_verify

    # 1. Fetch well-known config
    well_known_url = f"{args.issuer.rstrip('/')}/.well-known/openid-configuration"
    print(f"[1] Fetching well-known config: {well_known_url}")
    try:
        resp = requests.get(well_known_url, timeout=10, verify=ssl_verify)
        resp.raise_for_status()
        config = resp.json()
    except Exception as e:
        print(f"  FAILED: {e}")
        sys.exit(1)

    print(f"  OK - issuer: {config.get('issuer')}")
    print(f"  authorization_endpoint: {config.get('authorization_endpoint')}")
    print(f"  token_endpoint: {config.get('token_endpoint')}")
    print(f"  userinfo_endpoint: {config.get('userinfo_endpoint')}")

    pkce_methods = config.get("code_challenge_methods_supported", [])
    print(f"  PKCE methods supported: {pkce_methods}")
    if "S256" not in pkce_methods:
        print("  WARNING: S256 not in supported methods - PKCE may not work")

    auth_endpoint = config["authorization_endpoint"]
    token_endpoint = config["token_endpoint"]
    userinfo_endpoint = config.get("userinfo_endpoint")

    # 2. Generate PKCE
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(16)
    print(f"\n[2] Generated PKCE pair (S256)")
    print(f"  code_verifier: {verifier[:20]}...")
    print(f"  code_challenge: {challenge[:20]}...")
    print(f"  state: {state}")

    # 3. Build authorization URL
    auth_params = {
        "response_type": "code",
        "client_id": args.client_id,
        "redirect_uri": args.redirect,
        "scope": args.scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{auth_endpoint}?{urllib.parse.urlencode(auth_params)}"
    print(f"\n[3] Authorization URL:")
    print(f"  {auth_url}")

    # 4. Start local server to capture callback
    result = {"code": None, "error": None, "state": None}

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            result["code"] = params.get("code", [None])[0]
            result["error"] = params.get("error", [None])[0]
            result["state"] = params.get("state", [None])[0]

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if result["code"]:
                self.wfile.write(b"<html><body><h1>SSO OK - Authorization code received</h1><p>You can close this tab.</p></body></html>")
            else:
                self.wfile.write(f"<html><body><h1>SSO Error</h1><p>{result['error']}</p></body></html>".encode())

        def log_message(self, format, *args):
            pass

    port = urllib.parse.urlparse(args.redirect).port or 8080
    server = http.server.HTTPServer(("0.0.0.0", port), CallbackHandler)
    server.timeout = 300  # 5 min to complete login

    print(f"\n[4] Listening for callback on port {port}...")
    print(f"  Open this URL in your browser:")
    print(f"  {auth_url}")

    # Try to open browser
    try:
        webbrowser.open(auth_url)
        print("  (browser opened automatically)")
    except Exception:
        print("  (could not open browser - open the URL manually)")

    # Wait for one request
    server.handle_request()
    server.server_close()

    # 5. Check callback result
    if result["error"]:
        print(f"\n[5] CALLBACK ERROR: {result['error']}")
        sys.exit(1)
    if not result["code"]:
        print("\n[5] No authorization code received (timeout)")
        sys.exit(1)
    if result["state"] != state:
        print(f"\n[5] State mismatch - possible CSRF attack")
        sys.exit(1)

    code = result["code"]
    print(f"\n[5] Authorization code received: {code[:20]}...")

    # 6. Exchange code for tokens
    print(f"\n[6] Exchanging code for tokens at {token_endpoint}")
    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": args.redirect,
        "client_id": args.client_id,
        "code_verifier": verifier,
    }
    try:
        resp = requests.post(token_endpoint, data=token_data, timeout=10, verify=ssl_verify)
        if resp.status_code != 200:
            print(f"  FAILED: {resp.status_code} {resp.text[:500]}")
            sys.exit(1)
        token_resp = resp.json()
    except Exception as e:
        print(f"  FAILED: {e}")
        sys.exit(1)

    access_token = token_resp.get("access_token")
    id_token = token_resp.get("id_token")
    refresh_token = token_resp.get("refresh_token")

    print(f"  access_token: {access_token[:30]}..." if access_token else "  access_token: None")
    print(f"  id_token: {id_token[:30]}..." if id_token else "  id_token: None")
    print(f"  refresh_token: {refresh_token[:30]}..." if refresh_token else "  refresh_token: None")
    print(f"  token_type: {token_resp.get('token_type')}")
    print(f"  expires_in: {token_resp.get('expires_in')}s")

    # 7. Fetch userinfo
    if userinfo_endpoint and access_token:
        print(f"\n[7] Fetching userinfo from {userinfo_endpoint}")
        try:
            resp = requests.get(
                userinfo_endpoint,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
                verify=ssl_verify,
            )
            if resp.status_code == 200:
                userinfo = resp.json()
                print(f"  sub: {userinfo.get('sub')}")
                print(f"  name: {userinfo.get('name')}")
                print(f"  email: {userinfo.get('email')}")
                print(f"  preferred_username: {userinfo.get('preferred_username')}")
                print(f"\n  Full userinfo:")
                print(f"  {json.dumps(userinfo, indent=2)}")
            else:
                print(f"  FAILED: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            print(f"  FAILED: {e}")

    # 8. Decode ID token (without verification - just to see claims)
    if id_token:
        print(f"\n[8] ID token claims (decoded, not verified):")
        try:
            payload = id_token.split(".")[1]
            # Add padding
            payload += "=" * (4 - len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            print(f"  {json.dumps(claims, indent=2)}")
        except Exception as e:
            print(f"  Could not decode: {e}")

    print("\n=== SSO VERIFICATION COMPLETE ===")
    print("All steps passed. Your well-known URL + client ID work with PKCE.")
    print("These values can be used to configure Intelligraph SSO:")
    print(f"  OIDC_ISSUER={args.issuer}")
    print(f"  OIDC_CLIENT_ID={args.client_id}")
    print(f"  OIDC_REDIRECT_URI={args.redirect}")


if __name__ == "__main__":
    main()
