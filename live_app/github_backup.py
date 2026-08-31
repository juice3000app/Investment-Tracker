"""
live_app/github_backup.py

Free, durable off-instance backup for live_app's SQLite state, using the
user's own GitHub repo as storage (the Contents API). Render's free tier
has no persistent disk -- every time the service spins down and back up
(its normal behavior after ~15 minutes of no traffic, not just a code
redeploy), the container is fresh and the local SQLite file is gone.
This module lets the app back itself up automatically after every daily
job / manual refresh run (see daily_job.py's run_once), and restore
itself automatically at startup if it wakes up into a genuinely empty
database (see server.py), closing that gap without needing a paid plan.

Needs three environment variables, set on whatever host runs this:

    GITHUB_BACKUP_TOKEN     a GitHub personal access token (fine-grained,
                            scoped to just this one repo, Contents:
                            read+write permission -- see DEPLOYMENT.md)
    GITHUB_BACKUP_REPO      "owner/repo", e.g. "juice3000app/Investment-Tracker"
    GITHUB_BACKUP_PATH      path within the repo to store the backup JSON,
                            e.g. "backups/state-backup.json"

GITHUB_BACKUP_BRANCH is optional and defaults to "main".

Best-effort throughout, by design: a missing config, a network error, or
a bad response never raises past this module's two public functions --
push_backup/pull_backup return True/False/None and the caller logs and
moves on. Backing up (or restoring) must never be why the daily job, or
the server itself starting up, fails.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Optional

_API_ROOT = "https://api.github.com"
_TIMEOUT_SECONDS = 15


def _env(name: str) -> Optional[str]:
    return os.environ.get(name)


def _configured() -> bool:
    return bool(_env("GITHUB_BACKUP_TOKEN") and _env("GITHUB_BACKUP_REPO") and _env("GITHUB_BACKUP_PATH"))


def _api_url() -> str:
    repo = _env("GITHUB_BACKUP_REPO")
    path = _env("GITHUB_BACKUP_PATH")
    return f"{_API_ROOT}/repos/{repo}/contents/{path}"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_env('GITHUB_BACKUP_TOKEN')}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "signal-ledger-backup",
    }


def _branch() -> str:
    return _env("GITHUB_BACKUP_BRANCH") or "main"


def _get_existing_file() -> Optional[dict]:
    """The backup file's current metadata (sha + base64 content), or None
    if it doesn't exist yet. Raises on anything other than a 404 -- the
    two public functions below are what catch and swallow that."""
    req = urllib.request.Request(f"{_api_url()}?ref={_branch()}", headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def push_backup(bundle: dict) -> bool:
    """Writes `bundle` (the same dict state.export_all() produces) to the
    configured GitHub repo file, creating it on the first call and
    updating it (using its current sha, as GitHub's API requires) on
    every call after. Returns True on success, False on any failure --
    including simply being unconfigured. Never raises."""
    if not _configured():
        return False
    try:
        existing = _get_existing_file()
        content = base64.b64encode(json.dumps(bundle, indent=2).encode("utf-8")).decode("ascii")
        payload = {
            "message": f"Automated state backup ({bundle.get('exported_at', 'unknown time')})",
            "content": content,
            "branch": _branch(),
        }
        if existing and existing.get("sha"):
            payload["sha"] = existing["sha"]
        req = urllib.request.Request(
            _api_url(), data=json.dumps(payload).encode("utf-8"),
            headers=_headers(), method="PUT",
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def pull_backup() -> Optional[dict]:
    """Fetches and decodes the backup bundle from the configured GitHub
    repo file -- the same shape state.import_all() expects. Returns None
    if unconfigured, the file doesn't exist yet, or anything about the
    fetch/decode fails. Never raises."""
    if not _configured():
        return None
    try:
        existing = _get_existing_file()
        if not existing or "content" not in existing:
            return None
        raw = base64.b64decode(existing["content"])
        return json.loads(raw)
    except Exception:
        return None
