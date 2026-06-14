"""
Bitbucket Data Center auth for non-interactive Git clone.

Credential resolution order:
1. Explicit access_token from clone form
2. Existing linked Bitbucket credential for logged-in user (not yet implemented)
3. Existing stored project credential (not yet implemented)
4. No-auth public clone attempt
5. Fail with missing_repo_credentials

Token is NEVER logged, stored in project metadata, or embedded in Git URLs.
"""

import os
import platform
import subprocess
import tempfile
import logging
import re

logger = logging.getLogger(__name__)

# ── GIT_ASKPASS helper ──────────────────────────────────────────────

def _create_askpass_script():
    """Create a temporary GIT_ASKPASS script.

    Reads GIT_PASSWORD / GIT_USERNAME from environment (never embedded).
    Returns path to the script. Caller MUST unlink after use.
    """
    is_win = platform.system() == "Windows"

    if is_win:
        content = (
            '@echo off\r\n'
            'echo.%* | findstr /i "password" >nul\r\n'
            'if not errorlevel 1 echo.%GIT_PASSWORD%&exit /b 0\r\n'
            'echo.%* | findstr /i "token" >nul\r\n'
            'if not errorlevel 1 echo.%GIT_PASSWORD%&exit /b 0\r\n'
            'echo.%* | findstr /i "username" >nul\r\n'
            'if not errorlevel 1 if not "%GIT_USERNAME%"=="" echo.%GIT_USERNAME%&exit /b 0\r\n'
            'echo.%GIT_PASSWORD%\r\n'
        )
        suffix = ".bat"
    else:
        content = (
            "#!/bin/sh\n"
            'case "$1" in\n'
            "  *[Pp]assword*|*[Tt]oken*) echo \"${GIT_PASSWORD}\" ;;\n"
            '  *[Uu]sername*) echo "${GIT_USERNAME:-}" ;;\n'
            "  *) echo \"${GIT_PASSWORD}\" ;;\n"
            "esac\n"
        )
        suffix = ".sh"

    fd, path = tempfile.mkstemp(suffix=suffix, prefix="git-askpass-")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    if not is_win:
        os.chmod(path, 0o500)
    return path


# ── Git command helpers ─────────────────────────────────────────────

def run_git(cmd_args, repo_dir, token=None, username=None, timeout=120):
    """Run a git command. If token is provided, uses GIT_ASKPASS for non-interactive auth.

    NEVER logs the token. Returns subprocess.CompletedProcess.
    The caller is responsible for managing the askpass script lifetime.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"

    askpass_path = None
    if token:
        askpass_path = _create_askpass_script()
        env["GIT_ASKPASS"] = askpass_path
        env["GIT_PASSWORD"] = token
        if username:
            env["GIT_USERNAME"] = username

    try:
        result = subprocess.run(
            cmd_args,
            cwd=repo_dir or "/",
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    finally:
        if askpass_path:
            try:
                os.unlink(askpass_path)
            except OSError:
                pass

    return result


def preflight_git_access(git_url, token=None, username=None, timeout=30):
    """Verify git access via git ls-remote.

    Returns (success: bool, error_type: str | None).
    error_type is None on success, or one of:
      bitbucket_auth_failed, repo_not_found_or_no_access, clone_failed
    """
    result = run_git(
        ["git", "ls-remote", git_url],
        repo_dir=None,
        token=token,
        username=username,
        timeout=timeout,
    )

    if result.returncode == 0:
        return True, None

    stderr = (result.stderr or "").lower()
    if "not found" in stderr or "could not read" in stderr:
        return False, "repo_not_found_or_no_access"
    if (
        "authentication required" in stderr
        or "access denied" in stderr
        or "403" in stderr
        or "401" in stderr
        or "authentication failed" in stderr
    ):
        return False, "bitbucket_auth_failed"
    return False, "clone_failed"


def clean_remote_url(repo_dir):
    """Remove any embedded credentials from remote.origin.url.

    If the URL contained credentials, fixes it and returns True.
    Returns False if no credentials were found.
    """
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=10,
    )
    url = result.stdout.strip()
    cleaned = re.sub(r"://[^@]+@", "://", url)
    if cleaned != url:
        subprocess.run(
            ["git", "remote", "set-url", "origin", cleaned],
            cwd=repo_dir,
            capture_output=True,
            timeout=10,
        )
        return True
    return False


def token_display(token):
    """Return a masked version of the token for logging (e.g. BBDC-****abcd)."""
    if not token:
        return "[NONE]"
    if len(token) >= 8:
        return token[:8] + "****" + token[-4:]
    return "****" + token[-4:] if len(token) >= 4 else "****"


# ── Credential resolution ───────────────────────────────────────────

def resolve_bitbucket_credential(
    access_token=None,
    bitbucket_username=None,
    use_linked_credentials=True,
    user_key=None,
    project_id=None,
):
    """Resolve Bitbucket Data Center credentials.

    Returns (source: str, token: str, username: str | None) or None.
    source is one of: explicit_http_token, linked_bitbucket, stored_project_credential

    NOTE: linked_bitbucket and stored_project_credential are NOT YET IMPLEMENTED.
    The function always returns None unless an explicit access_token is provided.
    """
    # 1. Explicit token from clone form
    if access_token:
        token = access_token.strip()
        username = bitbucket_username.strip() if bitbucket_username else None
        return ("explicit_http_token", token, username)

    # 2. Linked credential (not yet implemented — would need encrypted secret store)
    # if use_linked_credentials and user_key:
    #     token = _lookup_linked_credential(user_key)
    #     if token:
    #         return ("linked_bitbucket", token, None)

    # 3. Stored project credential (not yet implemented)
    # if project_id:
    #     token = _lookup_project_credential(project_id)
    #     if token:
    #         return ("stored_project_credential", token, None)

    return None


# ── Error message builders ──────────────────────────────────────────

def missing_credential_error(is_oidc_user):
    """Return (status_code, body) for missing credentials."""
    if is_oidc_user:
        return (
            400,
            {
                "error": "missing_repo_credentials",
                "message": (
                    "You are logged in to Intelligraph, but Intelligraph does not have "
                    "Bitbucket Git credentials. Provide a Bitbucket HTTP access token "
                    "or link a Bitbucket account with repository read access."
                ),
            },
        )
    return (
        400,
        {
            "error": "missing_repo_credentials",
            "message": "Provide a Bitbucket HTTP access token to clone this repository.",
        },
    )


def auth_failed_error():
    return (
        401,
        {
            "error": "bitbucket_auth_failed",
            "message": (
                "Bitbucket rejected the provided credentials. Check that the HTTP access "
                "token has repository read permission."
            ),
        },
    )


def repo_not_found_error():
    return (
        404,
        {
            "error": "repo_not_found_or_no_access",
            "message": "Repository was not found or the credentials do not have access.",
        },
    )


def username_required_error():
    return (
        400,
        {
            "error": "bitbucket_username_required",
            "message": "This Bitbucket server requires a username with the HTTP access token.",
        },
    )


def clone_failed_error(trace_id=""):
    return (
        500,
        {
            "error": "clone_failed",
            "message": "Git clone failed after authentication preflight.",
            "trace_id": trace_id,
        },
    )