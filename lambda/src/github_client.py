"""
GitHub API client for committing generated files.
"""

import base64
import json
import logging
from urllib.request import Request, urlopen
from urllib.error import HTTPError

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
TIMEOUT_SECONDS = 30


def commit_file(
    owner: str,
    repo: str,
    path: str,
    token: str,
    message: str,
    content: str,
    branch: str = "main",
) -> dict:
    """
    Create or update a file in a GitHub repository.

    Uses the GitHub Contents API (PUT /repos/{owner}/{repo}/contents/{path}).
    If the file already exists, its SHA is fetched first to allow updates.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }

    # Check if file already exists (need SHA for updates)
    existing_sha = _get_file_sha(url, headers)

    encoded_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    payload = {
        "message": message,
        "content": encoded_content,
        "branch": branch,
    }

    if existing_sha:
        payload["sha"] = existing_sha
        logger.info("Updating existing file: %s", path)
    else:
        logger.info("Creating new file: %s", path)

    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="PUT")

    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            response_data = json.loads(response.read().decode("utf-8"))
            logger.info("Successfully committed %s (status: %d)", path, response.status)
            return response_data
    except HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        logger.error("GitHub API error for %s: %d - %s", path, e.code, error_body)
        raise RuntimeError(f"GitHub commit failed for {path}: HTTP {e.code}") from e


def _get_file_sha(url: str, headers: dict) -> str | None:
    """Get the SHA of an existing file, or None if it doesn't exist."""
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data.get("sha")
    except HTTPError as e:
        if e.code == 404:
            return None
        raise
