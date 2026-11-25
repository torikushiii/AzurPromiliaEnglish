"""Check GitHub for a newer patch release.

Only reads the public release list; sends nothing else.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass

REPO = "torikushiii/AzurPromiliaEnglish"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/"


@dataclass(frozen=True)
class Release:
    version: str
    url: str


def parse_version(text: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def newer_release(current: str, timeout: float = 5.0) -> Release | None:
    """The latest published release if it is newer than `current`, else None.

    Network and parsing problems also return None: the check must never get in the way.
    """
    try:
        request = urllib.request.Request(
            LATEST_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"AP-English-Patcher/{current}",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        tag = str(data.get("tag_name", ""))
        url = str(data.get("html_url", ""))
    except Exception:
        return None
    latest, mine = parse_version(tag), parse_version(current)
    if not latest or not mine or latest <= mine:
        return None
    if not url.startswith(RELEASES_PAGE):  # only ever open this repository's releases
        url = RELEASES_PAGE + "latest"
    return Release(tag.lstrip("v"), url)
