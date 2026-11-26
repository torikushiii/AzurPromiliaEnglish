import argparse
import hashlib
import importlib
import importlib.util
import json
import struct
import sys
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STUFF = ROOT / ".stuff"
sys.path.insert(0, str(STUFF / "scripts"))
sys.path.insert(0, str(ROOT / "patcher"))

spec = importlib.util.spec_from_file_location(
    "build", STUFF / "scripts" / "build-english-gameplay.py"
)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
archive = importlib.import_module("archive")
engine = importlib.import_module("engine")
bundles = importlib.import_module("bundles")
spec = importlib.util.spec_from_file_location(
    "textbundles", STUFF / "scripts" / "boot-dryrun.py"
)
tb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tb)
SHIPPED_BUNDLES = ("boot", "labels")
TEST_INSTALLS = {"boot": "boot-strings", "labels": "ui-labels"}
AES = importlib.import_module("Crypto.Cipher.AES")
unpad = importlib.import_module("Crypto.Util.Padding").unpad


def sha(data):
    return hashlib.sha256(data).hexdigest()


def seed_of(raw):
    version, keysize = struct.unpack_from("<II", raw)
    assert version == 1 and keysize == 4112 and keysize + 8 == engine.SPAN
    seed = unpad(
        AES.new(archive.aeskey, AES.MODE_CBC, archive.iv).decrypt(raw[8 : 8 + keysize]),
        16,
    )
    assert len(seed) == 4096
    return seed


def stock_bundle_file(role, raw):
    """The game file without our own test install (undone from its saved bytes)."""
    saved = STUFF / TEST_INSTALLS[role] / "test-install"
    info_path, orig_path = saved / f"{role}.json", saved / f"{role}.orig"
    if info_path.exists() and orig_path.exists():
        info = bundles.TextBundle(**json.loads(info_path.read_text("utf-8")))
        if sha(raw) == info.patched_sha256:
            raw = bundles.restored_bytes(raw, info, orig_path.read_bytes())
            assert sha(raw) == info.stock_sha256
    return raw


def text_bundles():
    out = []
    for role in SHIPPED_BUNDLES:
        kind, path, marker, strings_file = tb.TARGETS[role]
        stock = stock_bundle_file(role, (tb.GAME / path).read_bytes())
        info = tb.locate(stock, kind, path, marker)
        info.role = role
        info.strings = json.loads((STUFF / strings_file).read_text("utf-8"))
        start, end, _ = bundles.changed_range(stock, info)
        patched = bundles.patched_bytes(stock, info)
        info.backup_start, info.backup_end = start, end
        info.backup_sha256 = sha(stock[start:end])
        info.patched_sha256 = sha(patched)
        assert bundles.restored_bytes(patched, info, stock[start:end]) == stock
        print(
            f"{role}: {len(info.strings)} strings, stock {info.stock_sha256[:12]}  "
            f"english {info.patched_sha256[:12]}",
            flush=True,
        )
        out.append((info, stock))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--version", required=True, help="patch version shown to players, e.g. 1.0"
    )
    ap.add_argument(
        "--out", type=Path, default=ROOT / "patcher" / "data" / "patch-data.bin"
    )
    ap.add_argument(
        "--game-version",
        help="game version shown to players (default: read from the launcher config)",
    )
    args = ap.parse_args()
    game_version = args.game_version or engine.installed_game_version(b.GAME)
    if not game_version:
        raise SystemExit("Could not read the game version; pass --game-version.")
    builds = {
        build
        for package in (b.BASE, b.STREAM)
        for build in engine.installed_builds(b.GAME, package.name)
    }
    if builds != {"3219598"}:
        raise SystemExit(
            f"Installed game builds are {sorted(builds)}, expected 3219598."
        )

    installed = {}
    for line in (
        (STUFF / "gameplay-en-corrections-3219598" / "installed.sha256")
        .read_text()
        .split("\n")
    ):
        if line.strip():
            digest, name = line.split(maxsplit=1)
            installed[name.strip()] = digest
    roles = {"pool": (b.BASE, b.POOL_NAME), "table": (b.STREAM, b.TABLE_NAME)}
    stock, final, files = {}, {}, {}
    for role, (package, name) in roles.items():
        stock[role] = (b.OUTPUT / "original" / name).read_bytes()
        final[role] = (package / "BundleFiles" / name).read_bytes()
        if sha(final[role]) != installed[name]:
            raise SystemExit(
                f"Installed {name} does not match installed.sha256; "
                "reinstall the corrections batch first."
            )
        assert final[role][: engine.SPAN] == stock[role][: engine.SPAN]
        seed = seed_of(stock[role])
        assert engine.crypt_body(
            memoryview(stock[role])[engine.SPAN :], seed
        ) == b.decrypt(stock[role]), role
        files[role] = engine.FileInfo(
            role, name, package.name, sha(stock[role]), sha(final[role]), seed
        )
        print(
            f"{role}: stock {files[role].stock_sha256[:12]}  "
            f"english {files[role].patched_sha256[:12]}",
            flush=True,
        )

    pool_stock, pool_final = b.decrypt(stock["pool"]), b.decrypt(final["pool"])
    *_, stock_offsets, _, stock_count = engine.parse_pool(pool_stock)
    *_, final_offsets, _, final_count = engine.parse_pool(pool_final)
    new_strings = [
        b.pool_get(pool_final, final_offsets, i).encode("utf-8")
        for i in range(stock_count, final_count)
    ]
    new_hashes = array("Q", (b.spookyhash.hash64(s) for s in new_strings))
    print(f"strings: stock {stock_count}, added {len(new_strings)}", flush=True)

    table_stock, table_final = b.decrypt(stock["table"]), b.decrypt(final["table"])
    locations = b.language_tables(table_stock)
    assert locations == b.language_tables(table_final)
    edits = array("I")
    for offset in locations.values():
        for (key, before, ptr), (key2, after, ptr2) in zip(
            b.table_records(table_stock, offset),
            b.table_records(table_final, offset),
            strict=True,
        ):
            assert key == key2 and ptr == ptr2
            if before != after:
                edits.extend((ptr, before, after))
    print(f"table rows changed: {len(edits) // 3}", flush=True)
    shipped = text_bundles()

    data = engine.PatchData(
        args.version,
        "3219598",
        files,
        stock_count,
        new_strings,
        new_hashes,
        edits,
        game_version,
        [info for info, _ in shipped],
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    engine.save_patch_data(args.out, data)
    data = engine.load_patch_data(args.out)

    print("simulating install...", flush=True)
    assert (
        sha(engine._patched_pool(stock["pool"], data)) == files["pool"].patched_sha256
    )
    assert (
        sha(engine._swap_table(stock["table"], data, True))
        == files["table"].patched_sha256
    )
    for info, stock in shipped:
        loaded = next(b for b in data.text_bundles if b.role == info.role)
        patched = bundles.patched_bytes(stock, loaded)
        assert sha(patched) == loaded.patched_sha256
        backup = stock[loaded.backup_start : loaded.backup_end]
        assert (
            sha(bundles.restored_bytes(patched, loaded, backup)) == loaded.stock_sha256
        )
    print("simulating restore...", flush=True)
    assert sha(engine._stock_pool(final["pool"], data)) == files["pool"].stock_sha256
    assert (
        sha(engine._swap_table(final["table"], data, False))
        == files["table"].stock_sha256
    )
    print(
        f"OK: wrote {args.out} ({args.out.stat().st_size / 1e6:.1f} MB), "
        f"patch {args.version} for {data.game_label}"
    )


if __name__ == "__main__":
    main()
