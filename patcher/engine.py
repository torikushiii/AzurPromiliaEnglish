from __future__ import annotations

import hashlib
import json
import lzma
import os
import string
import struct
import subprocess
import sys
import tempfile
from array import array
from dataclasses import dataclass, field
from pathlib import Path

import bundles

SPAN = 0x1018
PAGE = 4096
PERIOD = 256 * PAGE
U32 = struct.Struct("<I")
BUCKET_ITEM = struct.Struct("<QI")
EXE_NAME = "AzurPromiliaJP.exe"
DATA_DIR = "AzurPromiliaJP_Data"
LAUNCHER_CONFIG = "game-launcher-config.json"
MAGIC = b"APEN-PATCH-1\n"
CREATE_NO_WINDOW = 0x08000000
BACKUP_DIR = (
    Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AP-English-Patcher" / "backup"
)


class PatchError(Exception):
    pass


@dataclass
class FileInfo:
    role: str
    name: str
    package: str
    stock_sha256: str
    patched_sha256: str
    seed: bytes


@dataclass
class PatchData:
    version: str
    build: str
    files: dict[str, FileInfo]
    stock_count: int
    new_strings: list[bytes]
    new_hashes: array  # uint64 spookyhash of each new string
    edits: array  # flat uint32 triples: offset, stock value, patched value
    game_version: str = ""
    text_bundles: list[bundles.TextBundle] = field(default_factory=list)

    @property
    def game_label(self) -> str:
        if self.game_version:
            return f"game {self.game_version} (build {self.build})"
        return f"game build {self.build}"


def save_patch_data(path: Path, data: PatchData) -> None:
    strings_blob = b"".join(U32.pack(len(s)) + s for s in data.new_strings)
    blobs = [
        data.files["pool"].seed,
        data.files["table"].seed,
        data.new_hashes.tobytes(),
        data.edits.tobytes(),
        strings_blob,
    ]
    header = {
        "version": data.version,
        "build": data.build,
        "game_version": data.game_version,
        "stock_count": data.stock_count,
        "new_strings": len(data.new_strings),
        "edits": len(data.edits) // 3,
        "files": {
            role: {
                k: getattr(f, k)
                for k in ("name", "package", "stock_sha256", "patched_sha256")
            }
            for role, f in data.files.items()
        },
        "blob_sizes": [len(b) for b in blobs],
    }
    if data.text_bundles:
        header["bundles"] = [b.as_json() for b in data.text_bundles]
    head = json.dumps(header, ensure_ascii=False).encode("utf-8")
    payload = MAGIC + U32.pack(len(head)) + head + b"".join(blobs)
    path.write_bytes(lzma.compress(payload, preset=9))


def load_patch_data(path: Path | None = None) -> PatchData:
    if path is None:
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        path = base / "data" / "patch-data.bin"
    raw = lzma.decompress(path.read_bytes())
    if not raw.startswith(MAGIC):
        raise PatchError("The patch data file is damaged.")
    pos = len(MAGIC)
    (head_len,) = U32.unpack_from(raw, pos)
    pos += 4
    header = json.loads(raw[pos : pos + head_len].decode("utf-8"))
    pos += head_len
    blobs = []
    for size in header["blob_sizes"]:
        blobs.append(raw[pos : pos + size])
        pos += size
    seed_pool, seed_table, hashes_blob, edits_blob, strings_blob = blobs
    hashes = array("Q")
    hashes.frombytes(hashes_blob)
    edits = array("I")
    edits.frombytes(edits_blob)
    strings_list, p = [], 0
    for _ in range(header["new_strings"]):
        (n,) = U32.unpack_from(strings_blob, p)
        strings_list.append(strings_blob[p + 4 : p + 4 + n])
        p += 4 + n
    seeds = {"pool": seed_pool, "table": seed_table}
    files = {
        role: FileInfo(role=role, seed=seeds[role], **info)
        for role, info in header["files"].items()
    }
    assert len(hashes) == len(strings_list) and len(edits) == 3 * header["edits"]
    return PatchData(
        header["version"],
        header["build"],
        files,
        header["stock_count"],
        strings_list,
        hashes,
        edits,
        header.get("game_version", ""),
        [bundles.TextBundle(**b) for b in header.get("bundles", [])],
    )


def _period_stream(seed: bytes) -> bytes:
    # Byte at absolute position p is seed[((p & 4095) + seed[(p >> 12) & 255]) & 4095].
    return b"".join(seed[s:] + seed[:s] for s in (seed[k] for k in range(256)))


def crypt_body(body, seed: bytes) -> bytes:
    period = _period_stream(seed)
    body = memoryview(body)
    out = bytearray(len(body))
    pos, i, n = SPAN, 0, len(body)
    while i < n:
        off = pos % PERIOD
        take = min(PERIOD - off, n - i)
        a = int.from_bytes(body[i : i + take], "little")
        b = int.from_bytes(period[off : off + take], "little")
        out[i : i + take] = (a ^ b).to_bytes(take, "little")
        i += take
        pos += take
    return bytes(out)


def _u(data, offset):
    return U32.unpack_from(data, offset)[0]


def parse_pool(data):
    total, start, index, body, buckets, hashes = struct.unpack_from("<6I", data)
    if not (start == SPAN and index == start + 24 and body == index + 4 * buckets):
        raise PatchError("Unexpected string pool layout.")
    raw_end, begin, offsets, strings_at, count = struct.unpack_from("<5I", data, total)
    if not (
        begin == total + SPAN
        and offsets == begin + 20
        and strings_at == offsets + 4 * count
        and total + raw_end == len(data)
    ):
        raise PatchError("Unexpected string pool layout.")
    return (
        total,
        index - SPAN,
        body - SPAN,
        buckets,
        hashes,
        offsets - SPAN,
        strings_at - SPAN,
        count,
    )


def _write_pool(buckets_payload, offsets_table, buckets, hash_total, hash_data):
    hash_length = 24 + 4 * buckets + len(buckets_payload)
    out = bytearray(
        struct.pack(
            "<6I", hash_length, SPAN, SPAN + 24, SPAN + hash_data, buckets, hash_total
        )
    )
    out += offsets_table.tobytes()
    out += buckets_payload
    return out, hash_length


def rebuild_pool(data, stock_count, new_strings, new_hashes):
    (
        total,
        hash_indices,
        hash_data,
        buckets,
        hash_total,
        old_offsets,
        old_strings,
        count,
    ) = parse_pool(data)
    if count != stock_count:
        raise PatchError("The string pool is not the expected stock version.")
    additions = {}
    for i, h in enumerate(new_hashes):
        additions.setdefault(h % buckets, []).append((h, stock_count + i))
    offsets = array("I", bytes(4 * buckets))
    payload = bytearray()
    cursor = hash_data + SPAN
    for bucket in range(buckets):
        old = _u(data, hash_indices + 4 * bucket)
        added = additions.get(bucket, ())
        if not old and not added:
            continue
        old_count = 0
        if old:
            if old != cursor:
                raise PatchError("Unexpected string pool layout.")
            old_count = _u(data, old - SPAN)
            cursor += 4 + 12 * old_count
        offsets[bucket] = hash_data + SPAN + len(payload)
        payload += U32.pack(old_count + len(added))
        if old:
            payload += data[old - SPAN + 4 : old - SPAN + 4 + 12 * old_count]
        for record in added:
            payload += BUCKET_ITEM.pack(*record)
    out, hash_length = _write_pool(
        payload, offsets, buckets, hash_total + len(new_strings), hash_data
    )
    values_count = count + len(new_strings)
    start = SPAN + hash_length
    offsets_at = start + 20
    string_start = offsets_at + 4 * values_count
    delta = string_start - (old_strings + SPAN)
    values = array("I")
    values.frombytes(data[old_offsets:old_strings])
    values = array("I", (v + delta for v in values))
    tail = bytearray(data[old_strings:])
    for s in new_strings:
        values.append(string_start + len(tail))
        tail += U32.pack(len(s)) + s
    out += struct.pack(
        "<5I",
        20 + len(values) * 4 + len(tail),
        start,
        offsets_at,
        string_start,
        values_count,
    )
    out += values.tobytes()
    out += tail
    return bytes(out)


def strip_pool(data, stock_count):
    (
        total,
        hash_indices,
        hash_data,
        buckets,
        hash_total,
        old_offsets,
        old_strings,
        count,
    ) = parse_pool(data)
    removed = count - stock_count
    if removed < 0:
        raise PatchError("The string pool is smaller than the stock version.")
    offsets = array("I", bytes(4 * buckets))
    payload = bytearray()
    for bucket in range(buckets):
        old = _u(data, hash_indices + 4 * bucket)
        if not old:
            continue
        n = _u(data, old - SPAN)
        base = old - SPAN + 4
        kept = [
            data[base + 12 * j : base + 12 * j + 12]
            for j in range(n)
            if BUCKET_ITEM.unpack_from(data, base + 12 * j)[1] < stock_count
        ]
        if not kept:
            continue
        offsets[bucket] = hash_data + SPAN + len(payload)
        payload += U32.pack(len(kept)) + b"".join(kept)
    out, hash_length = _write_pool(
        payload, offsets, buckets, hash_total - removed, hash_data
    )
    start = SPAN + hash_length
    offsets_at = start + 20
    string_start = offsets_at + 4 * stock_count
    values = array("I")
    values.frombytes(data[old_offsets : old_offsets + 4 * stock_count])
    end = (_u(data, old_offsets + 4 * stock_count) - SPAN) if removed else len(data)
    delta = string_start - (old_strings + SPAN)
    values = array("I", (v + delta for v in values))
    tail = data[old_strings:end]
    out += struct.pack(
        "<5I",
        20 + len(values) * 4 + len(tail),
        start,
        offsets_at,
        string_start,
        stock_count,
    )
    out += values.tobytes()
    out += tail
    return bytes(out)


def normalize_game_dir(path: Path) -> Path:
    path = Path(path)
    if path.is_file():
        path = path.parent
    if path.name.lower() == DATA_DIR.lower():
        path = path.parent
    return path


def is_game_dir(path: Path) -> bool:
    return (path / EXE_NAME).is_file() and (path / DATA_DIR / ".res").is_dir()


def find_game_dirs():
    rels = [
        r"YostarGames\AZUPRO_JP",
        r"Games\YostarGames\AZUPRO_JP",
        r"Program Files\YostarGames\AZUPRO_JP",
        r"Program Files (x86)\YostarGames\AZUPRO_JP",
    ]
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:\\")
        try:
            if not root.exists():
                continue
        except OSError:
            continue
        for rel in rels:
            candidate = root / rel
            if is_game_dir(candidate):
                yield candidate


def game_is_running() -> bool:
    try:
        out = subprocess.check_output(
            ["tasklist.exe", "/FI", f"IMAGENAME eq {EXE_NAME}", "/FO", "CSV", "/NH"],
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return EXE_NAME.lower().encode() in out.lower()


def _paths(game: Path, info: FileInfo) -> Path:
    return game / DATA_DIR / ".res" / info.package / "BundleFiles" / info.name


def installed_game_version(game: Path) -> str:
    try:
        config = json.loads((game / LAUNCHER_CONFIG).read_text(encoding="utf-8"))
        return str(config.get("version", ""))
    except (OSError, ValueError, AttributeError):
        return ""


def installed_builds(game: Path, package: str) -> set[str]:
    folder = game / DATA_DIR / ".res" / package / "ManifestFiles"
    prefix = f"0release_{package}_"
    return {
        p.name[len(prefix) : -len(".bytes")] for p in folder.glob(f"{prefix}*.bytes")
    }


def _wrong_build_message(game: Path, patch: PatchData, builds: set[str]) -> str:
    others = sorted(builds - {patch.build})
    version = installed_game_version(game)
    if others:
        found = f"build {', '.join(others)}"
        if version and version != patch.game_version:
            found = f"{version} ({found})"
        installed = f"the installed game is {found}"
    else:
        installed = "the installed game's version files were not found"
    return (
        f"This patch is for {patch.game_label}, but {installed}. "
        "Wait for an updated patch."
    )


@dataclass
class Status:
    kind: str
    message: str
    states: dict | None = None


def inspect(game: Path, patch: PatchData) -> Status:
    game = normalize_game_dir(game)
    if not is_game_dir(game):
        return Status("not_found", f"{EXE_NAME} was not found in this folder.")
    builds = set()
    for info in patch.files.values():
        builds |= installed_builds(game, info.package)
    targets = {role: _paths(game, info) for role, info in patch.files.items()}
    if builds != {patch.build} or not all(t.is_file() for t in targets.values()):
        return Status("wrong_build", _wrong_build_message(game, patch, builds))
    states = {}
    for role, info in patch.files.items():
        target = targets[role]
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        states[role] = (
            "stock"
            if digest == info.stock_sha256
            else "patched"
            if digest == info.patched_sha256
            else "other"
        )
    for info in patch.text_bundles:
        state = _bundle_state(game, info)
        if state:
            states[info.role] = state
    values = set(states.values())
    if values == {"stock"}:
        return Status("stock", "Original Japanese text is installed.", states)
    if values == {"patched"}:
        return Status("patched", f"English patch {patch.version} is installed.", states)
    if "other" not in values:
        return Status(
            "mixed",
            "The patch is only partly installed. Install or restore to fix it.",
            states,
        )
    return Status(
        "unknown",
        "The text files were changed by something else (another patch version or "
        "a game update). Use the in-game resource check to restore the original "
        "files, then try again.",
        states,
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bundle_path(game: Path, info: bundles.TextBundle) -> Path:
    return game / Path(info.path.replace("\\", "/"))


def _bundle_state(game: Path, info: bundles.TextBundle) -> str | None:
    """stock or patched; None when missing or changed by something else (skipped)."""
    target = _bundle_path(game, info)
    if not target.is_file():
        return None
    digest = _sha(target.read_bytes())
    if digest == info.stock_sha256:
        return "stock"
    if digest == info.patched_sha256:
        return "patched"
    return None


def _backup_path(info: bundles.TextBundle) -> Path:
    return BACKUP_DIR / f"{info.role}-{info.stock_sha256[:16]}.bin"


def _bundle_replacement(game: Path, info: bundles.TextBundle, to_patched: bool):
    target = _bundle_path(game, info)
    current = target.read_bytes()
    try:
        if to_patched:
            new = bundles.patched_bytes(current, info)
            original = current[info.backup_start : info.backup_end]
            if _sha(original) != info.backup_sha256:
                raise PatchError(f"{target.name} does not match the tested version.")
            backup = _backup_path(info)
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.is_file() or backup.read_bytes() != original:
                backup.write_bytes(original)
        else:
            saved = _backup_path(info)
            original = saved.read_bytes() if saved.is_file() else b""
            if _sha(original) != info.backup_sha256:
                raise PatchError(
                    f"The saved original of {target.name} is missing, so its English "
                    "startup text cannot be removed here. Use the in-game resource "
                    "check to restore the original files."
                )
            new = bundles.restored_bytes(current, info, original)
    except bundles.BundleError as e:
        raise PatchError(f"{target.name}: {e}") from e
    expected = info.patched_sha256 if to_patched else info.stock_sha256
    if _sha(new) != expected:
        raise PatchError(
            f"The rebuilt {target.name} did not match the tested version. "
            "Nothing was changed."
        )
    return target, current, new


def _patched_pool(raw: bytes, patch: PatchData) -> bytes:
    info = patch.files["pool"]
    plain = crypt_body(memoryview(raw)[SPAN:], info.seed)
    built = rebuild_pool(plain, patch.stock_count, patch.new_strings, patch.new_hashes)
    return raw[:SPAN] + crypt_body(built, info.seed)


def _stock_pool(raw: bytes, patch: PatchData) -> bytes:
    info = patch.files["pool"]
    plain = crypt_body(memoryview(raw)[SPAN:], info.seed)
    return raw[:SPAN] + crypt_body(strip_pool(plain, patch.stock_count), info.seed)


def _swap_table(raw: bytes, patch: PatchData, to_patched: bool) -> bytes:
    info = patch.files["table"]
    plain = bytearray(crypt_body(memoryview(raw)[SPAN:], info.seed))
    e = patch.edits
    for k in range(0, len(e), 3):
        offset, before, after = e[k], e[k + 1], e[k + 2]
        if not to_patched:
            before, after = after, before
        if U32.unpack_from(plain, offset)[0] != before:
            raise PatchError("The table archive does not match the expected version.")
        U32.pack_into(plain, offset, after)
    return raw[:SPAN] + crypt_body(plain, info.seed)


def _install(replacements, progress):
    staged = []
    try:
        for target, _, new in replacements:
            fd, name = tempfile.mkstemp(
                dir=target.parent, prefix=target.name + ".", suffix=".tmp"
            )
            with os.fdopen(fd, "wb") as f:
                f.write(new)
                f.flush()
                os.fsync(f.fileno())
            stage = Path(name)
            staged.append(stage)
            if stage.read_bytes() != new:
                raise PatchError(
                    f"Could not write a verified copy next to {target.name}."
                )
        if game_is_running():
            raise PatchError("Azur Promilia is running. Close the game and try again.")
        done = []
        try:
            for (target, current, new), stage in zip(replacements, staged, strict=True):
                if target.read_bytes() != current:
                    raise PatchError(
                        f"{target.name} changed while patching. Nothing was changed."
                    )
                progress(f"Replacing {target.name}...")
                os.replace(stage, target)
                done.append((target, current))
                if target.read_bytes() != new:
                    raise PatchError(f"Verification failed for {target.name}.")
        except BaseException:
            for target, current in reversed(done):
                target.write_bytes(current)
            raise
    except PermissionError as e:
        raise PatchError(
            "Windows refused to write to the game folder. Close the game and the "
            "launcher, or run this patcher as administrator."
        ) from e
    finally:
        for stage in staged:
            stage.unlink(missing_ok=True)


def _run(game: Path, patch: PatchData, to_patched: bool, progress) -> str:
    game = normalize_game_dir(game)
    status = inspect(game, patch)
    if status.kind in ("not_found", "wrong_build", "unknown"):
        raise PatchError(status.message)
    if game_is_running():
        raise PatchError("Azur Promilia is running. Close the game and try again.")
    want = "patched" if to_patched else "stock"
    if status.kind == want:
        return status.message
    # Pool first when patching (adds strings), table first when restoring.
    order = ("pool", "table") if to_patched else ("table", "pool")
    replacements = []
    for role in order:
        info = patch.files[role]
        target = _paths(game, info)
        current = target.read_bytes()
        if status.states[role] == want:
            continue
        progress(f"Building {target.name}...")
        if role == "pool":
            new = (
                _patched_pool(current, patch)
                if to_patched
                else _stock_pool(current, patch)
            )
        else:
            new = _swap_table(current, patch, to_patched)
        expected = info.patched_sha256 if to_patched else info.stock_sha256
        if _sha(new) != expected:
            raise PatchError(
                f"The rebuilt {target.name} did not match the tested version. "
                "Nothing was changed."
            )
        replacements.append((target, current, new))
    for info in patch.text_bundles:
        if status.states.get(info.role) not in (None, want):
            progress(f"Building {_bundle_path(game, info).name}...")
            replacements.append(_bundle_replacement(game, info, to_patched))
    _install(replacements, progress)
    final = inspect(game, patch)
    if final.kind != want:
        raise PatchError(
            "The files were written but did not verify. Use the in-game resource check "
            "to restore the original files."
        )
    return final.message


def _quiet(_message: str) -> None:
    pass


def install(game: Path, patch: PatchData, progress=_quiet) -> str:
    return _run(game, patch, True, progress)


def restore(game: Path, patch: PatchData, progress=_quiet) -> str:
    return _run(game, patch, False, progress)
