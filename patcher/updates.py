"""Check GitHub for a newer patch release and download its patch data.

Only talks to this repository's public releases: it reads the release list and, when
the user asks for it, downloads the release's patch-data.bin and its SHA-256. It never
downloads or runs a program.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import engine

REPO = "torikushiii/AzurPromiliaEnglish"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/"
DOWNLOADS = f"https://github.com/{REPO}/releases/download/"
DATA_ASSET = "patch-data.bin"
HASH_ASSET = "patch-data.bin.sha256"
MAX_DATA_BYTES = 64 * 1024 * 1024
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AP-English-Patcher"
DOWNLOADED = DATA_DIR / DATA_ASSET


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    data_url: str = ""
    hash_url: str = ""

    @property
    def has_data(self) -> bool:
        return bool(self.data_url and self.hash_url)


def parse_version(text: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def is_newer(candidate: str, current: str) -> bool:
    new, mine = parse_version(candidate), parse_version(current)
    return bool(new and mine and new > mine)


def release_from_json(data: dict, current: str) -> Release | None:
    """The release in GitHub's JSON if it is newer than `current`, else None."""
    tag = str(data.get("tag_name", ""))
    if not is_newer(tag, current):
        return None
    url = str(data.get("html_url", ""))
    if not url.startswith(RELEASES_PAGE):  # only ever open this repository's releases
        url = RELEASES_PAGE + "latest"
    assets = {}
    for asset in data.get("assets") or []:
        name = str(asset.get("name", ""))
        link = str(asset.get("browser_download_url", ""))
        if link.startswith(DOWNLOADS) and link.endswith("/" + name):
            assets[name] = link
    return Release(
        tag.lstrip("v"), url, assets.get(DATA_ASSET, ""), assets.get(HASH_ASSET, "")
    )


def _get(url: str, current: str, timeout: float, limit: int) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": f"AP-English-Patcher/{current}"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("The download is larger than expected.")
    return data


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
        return release_from_json(data, current)
    except Exception:
        return None


def download_patch_data(
    release: Release, current: str, target: Path = DOWNLOADED, timeout: float = 60.0
) -> tuple[engine.PatchData, Path]:
    """Download and verify the release's patch data.

    Returns the loaded data and the downloaded file next to `target`; nothing replaces
    the current data until keep_download() is called. Refused unless the SHA-256 matches
    the published one and the file loads as patch data of the release's version.
    """
    if not release.has_data:
        raise UpdateError(f"Patch {release.version} has no patch data to download.")
    try:
        listed = _get(release.hash_url, current, timeout, 4096).decode("ascii").split()
        data = _get(release.data_url, current, timeout, MAX_DATA_BYTES)
    except UpdateError:
        raise
    except Exception as e:
        raise UpdateError(f"The download failed: {e}") from e
    if not listed or not re.fullmatch(r"[0-9a-fA-F]{64}", listed[0]):
        raise UpdateError("The published checksum could not be read.")
    if hashlib.sha256(data).hexdigest() != listed[0].lower():
        raise UpdateError("The downloaded patch data does not match its checksum.")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".download")
    tmp.write_bytes(data)
    try:
        patch = engine.load_patch_data(tmp)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise UpdateError(f"The downloaded patch data is damaged: {e}") from e
    if parse_version(patch.version) != parse_version(release.version):
        tmp.unlink(missing_ok=True)
        raise UpdateError(
            f"The download contains patch {patch.version}, not {release.version}."
        )
    return patch, tmp


def keep_download(tmp: Path, target: Path = DOWNLOADED) -> None:
    os.replace(tmp, target)


def switch_patch_data(
    game: Path | None,
    old: engine.PatchData,
    new: engine.PatchData,
    tmp: Path,
    *,
    target: Path = DOWNLOADED,
    progress=lambda _msg: None,
    on_switched=lambda _patch: None,
) -> str:
    """Make `new` the patch data in use; reinstall it if the game has `old` installed.

    New data cannot undo an older version's changes, so an installed patch is restored
    with the old data first. Refused, with nothing changed, when the new data is for a
    different game build than the installed game.
    """
    patched = False
    if game is not None and engine.is_game_dir(engine.normalize_game_dir(game)):
        game = engine.normalize_game_dir(game)
        before, after = engine.inspect(game, old), engine.inspect(game, new)
        if after.kind == "wrong_build" and before.kind != "wrong_build":
            tmp.unlink(missing_ok=True)
            raise UpdateError(
                f"Patch {new.version} is for {new.game_label}, a different game "
                "version than the one installed. Update the game first, then try again."
            )
        patched = before.kind in ("patched", "mixed")
        if patched:
            progress(f"Removing patch {old.version}...")
            engine.restore(game, old)
    keep_download(tmp, target)
    on_switched(new)
    if not patched:
        return (
            f"Patch {new.version} is ready. Click Install English Patch to install it."
        )
    progress(f"Installing patch {new.version}...")
    try:
        engine.install(game, new)
    except engine.PatchError as e:
        raise UpdateError(
            f"Patch {new.version} was downloaded, but installing it failed: {e} "
            "The game has the original Japanese text; click Install to try "
            "again."
        ) from e
    return f"Updated to patch {new.version}."


def best_patch_data(
    bundled: engine.PatchData, downloaded: Path = DOWNLOADED
) -> engine.PatchData:
    """The downloaded patch data if newer than the built-in one, else the built-in."""
    try:
        saved = engine.load_patch_data(downloaded)
    except Exception:
        return bundled
    return saved if is_newer(saved.version, bundled.version) else bundled
