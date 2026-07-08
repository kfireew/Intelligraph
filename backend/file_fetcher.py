"""
file_fetcher.py — On-demand sparse file fetcher.

After a repo is cloned, built, and deleted, the retrieval pipeline still
needs source files for code chunking. This module fetches ONLY the
requested files via git's sparse-checkout + blobless clone, minimizing
network transfer and disk usage.

Usage:
    fetched_dir = fetch_files_sparse(git_url, file_paths, token=...)
    if fetched_dir:
        try:
            chunks = chunk_files(file_paths, repo_dir=fetched_dir, ...)
        finally:
            shutil.rmtree(fetched_dir, ignore_errors=True)

All git calls reuse _git_auth_args / _git_env from app.py for SSL + Bearer auth.
"""

import os
import shutil
import subprocess
import tempfile
import stat
import logging

log = logging.getLogger(__name__)

FETCH_TIMEOUT = 90
MAX_FILE_BATCH = 30  # cap files per sparse-checkout to keep command line short


def _rmtree_hard(path):
    """shutil.rmtree that handles Windows read-only .git files."""
    def _on_error(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass
    shutil.rmtree(path, onerror=_on_error)


def fetch_files_sparse(git_url, file_paths, git_auth_args=None, git_env=None,
                       timeout=FETCH_TIMEOUT):
    """Sparse-clone only the requested files.

    Args:
        git_url:       Git remote URL (Bitbucket or GitHub).
        file_paths:    List of relative file paths to fetch.
        git_auth_args: List of git -c args from _git_auth_args(). Default SSL-only.
        git_env:       Dict env from _git_env(). Default os.environ + GIT_TERMINAL_PROMPT=0.
        timeout:       Total timeout for the clone+checkout.

    Returns:
        (tmp_dir, error_type) where error_type is one of:
        - None on success (tmp_dir is the path)
        - "auth" if git auth failed (expired/invalid token, 401/403)
        - "timeout" if the operation timed out
        - "not_found" if repo not found
        - "other" for other failures
        On error, tmp_dir is None and has been cleaned up.
    """
    if not git_url or not file_paths:
        return None

    # Deduplicate and cap
    unique_paths = []
    seen = set()
    for fp in file_paths:
        fp = fp.strip().lstrip("./").lstrip("/")
        if fp and fp not in seen and not fp.startswith(".git"):
            seen.add(fp)
            unique_paths.append(fp)
    if not unique_paths:
        return None
    unique_paths = unique_paths[:MAX_FILE_BATCH]

    if git_auth_args is None:
        git_auth_args = ["-c", "http.sslVerify=false"]
    if git_env is None:
        git_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    tmp_dir = tempfile.mkdtemp(prefix="intelligraph-sparse-")

    try:
        # 1. Blobless sparse clone — no working tree files yet
        clone_cmd = ["git"] + git_auth_args + [
            "clone",
            "--depth", "1",
            "--filter=blob:none",
            "--sparse",
            "--no-checkout",
            git_url,
            tmp_dir,
        ]
        r = subprocess.run(
            clone_cmd,
            capture_output=True, text=True,
            timeout=timeout, env=git_env,
        )
        if r.returncode != 0:
            stderr = (r.stderr or "").lower()
            log.warning("Sparse clone failed: %s", stderr[:300])
            _rmtree_hard(tmp_dir)
            if any(p in stderr for p in ("401", "403", "authentication", "access denied", "could not read", "authorization")):
                return None, "auth"
            if "not found" in stderr or "does not exist" in stderr:
                return None, "not_found"
            if "certificate" in stderr or "ssl" in stderr or "tls" in stderr:
                return None, "ssl"
            return None, "other"

        # 2. Configure sparse-checkout for just the requested files
        sparse_cmd = ["git", "sparse-checkout", "set", "--no-cone"] + unique_paths
        r = subprocess.run(
            sparse_cmd,
            cwd=tmp_dir,
            capture_output=True, text=True,
            timeout=30, env=git_env,
        )
        if r.returncode != 0:
            log.warning("Sparse-checkout set failed: %s", (r.stderr or "")[:300])
            _rmtree_hard(tmp_dir)
            return None, "other"

        # 3. Checkout — materializes only the sparse-set files
        checkout_cmd = ["git"] + git_auth_args + ["checkout", "HEAD"]
        r = subprocess.run(
            checkout_cmd,
            cwd=tmp_dir,
            capture_output=True, text=True,
            timeout=timeout, env=git_env,
        )
        if r.returncode != 0:
            stderr = (r.stderr or "").lower()
            log.warning("Sparse checkout failed: %s", stderr[:300])
            _rmtree_hard(tmp_dir)
            if any(p in stderr for p in ("401", "403", "authentication", "access denied")):
                return None, "auth"
            return None, "other"

        log.info("Sparse fetch OK: %d files from %s", len(unique_paths), git_url)
        return tmp_dir, None

    except subprocess.TimeoutExpired:
        log.warning("Sparse fetch timed out after %ds for %s", timeout, git_url)
        _rmtree_hard(tmp_dir)
        return None, "timeout"
    except FileNotFoundError:
        log.warning("git not found — cannot sparse fetch")
        _rmtree_hard(tmp_dir)
        return None, "other"
    except Exception as e:
        log.warning("Sparse fetch exception: %s", str(e)[:300])
        _rmtree_hard(tmp_dir)
        return None, "other"
