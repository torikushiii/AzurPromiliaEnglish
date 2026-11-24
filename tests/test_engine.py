import hashlib
import json
import random
import struct
import sys
import tempfile
import unittest
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "patcher"))

import engine  # noqa: E402

SPAN = engine.SPAN
U32 = engine.U32
BUILD = "3219598"
POOL = ("raw_base_package", "pool.r")
TABLE = ("raw_streaming_package", "table.r")


def fake_hash(value: bytes) -> int:
    return int.from_bytes(hashlib.blake2b(value, digest_size=8).digest(), "little")


def reference_crypt(body: bytes, seed: bytes, position: int = SPAN) -> bytes:
    expanded = seed * 2
    out = bytearray()
    i = 0
    while i < len(body):
        phase = position & 4095
        take = min(4096 - phase, len(body) - i)
        start = (phase + seed[(position >> 12) & 255]) & 4095
        chunk = body[i : i + take]
        key = expanded[start : start + take]
        out += bytes(a ^ b for a, b in zip(chunk, key, strict=True))
        i += take
        position += take
    return bytes(out)


def build_pool(strings: list[bytes], buckets: int = 7) -> bytes:
    groups: list[list[tuple[int, int]]] = [[] for _ in range(buckets)]
    for index, value in enumerate(strings):
        h = fake_hash(value)
        groups[h % buckets].append((h, index))
    body = 24 + 4 * buckets
    offsets, payload = [], bytearray()
    for group in groups:
        if not group:
            offsets.append(0)
            continue
        offsets.append(body + SPAN + len(payload))
        payload += U32.pack(len(group))
        payload += b"".join(engine.BUCKET_ITEM.pack(h, i) for h, i in group)
    total = body + len(payload)
    out = bytearray(
        struct.pack(
            "<6I", total, SPAN, SPAN + 24, SPAN + body, buckets, len(strings) + 1
        )
    )
    out += struct.pack(f"<{buckets}I", *offsets) + payload
    begin = total + SPAN
    offsets_at = begin + 20
    strings_at = offsets_at + 4 * len(strings)
    blob, positions = bytearray(), []
    for value in strings:
        positions.append(strings_at + len(blob))
        blob += U32.pack(len(value)) + value
    out += struct.pack(
        "<5I",
        20 + 4 * len(strings) + len(blob),
        begin,
        offsets_at,
        strings_at,
        len(strings),
    )
    out += struct.pack(f"<{len(strings)}I", *positions) + blob
    return bytes(out)


def pool_strings(data: bytes) -> list[bytes]:
    *_, offsets, _, count = engine.parse_pool(data)
    result = []
    for i in range(count):
        at = U32.unpack_from(data, offsets + 4 * i)[0] - SPAN
        size = U32.unpack_from(data, at)[0]
        result.append(data[at + 4 : at + 4 + size])
    return result


class Fixture:
    def __init__(self, seed: int = 1):
        rng = random.Random(seed)
        self.base = [f"日本語 {i}".encode() for i in range(40)]
        self.added = [f"English {i}".encode() for i in range(25)]
        pool_seed, table_seed = rng.randbytes(4096), rng.randbytes(4096)
        pool_header, table_header = rng.randbytes(SPAN), rng.randbytes(SPAN)

        stock_pool = build_pool(self.base)
        self.pool_stock = pool_header + reference_crypt(stock_pool, pool_seed)
        self.pool_patched = pool_header + reference_crypt(
            build_pool(self.base + self.added), pool_seed
        )

        table = bytearray(rng.randbytes(50_000))
        patched = bytearray(table)
        edits = array("I")
        for k, offset in enumerate(range(100, 40_000, 1_000)):
            before = U32.unpack_from(table, offset)[0]
            after = len(self.base) + (k % len(self.added))
            U32.pack_into(patched, offset, after)
            edits.extend((offset, before, after))
        self.table_stock = table_header + reference_crypt(bytes(table), table_seed)
        self.table_patched = table_header + reference_crypt(bytes(patched), table_seed)

        sha = engine._sha
        self.patch = engine.PatchData(
            version="9.9",
            build=BUILD,
            files={
                "pool": engine.FileInfo(
                    "pool",
                    POOL[1],
                    POOL[0],
                    sha(self.pool_stock),
                    sha(self.pool_patched),
                    pool_seed,
                ),
                "table": engine.FileInfo(
                    "table",
                    TABLE[1],
                    TABLE[0],
                    sha(self.table_stock),
                    sha(self.table_patched),
                    table_seed,
                ),
            },
            stock_count=len(self.base),
            new_strings=self.added,
            new_hashes=array("Q", (fake_hash(s) for s in self.added)),
            edits=edits,
            game_version="1.0.1",
        )

    def make_game(self, root: Path, builds=(BUILD,)) -> Path:
        (root / engine.EXE_NAME).write_bytes(b"")
        (root / engine.LAUNCHER_CONFIG).write_text(json.dumps({"version": "1.0.1"}))
        for (package, name), raw in (
            (POOL, self.pool_stock),
            (TABLE, self.table_stock),
        ):
            res = root / engine.DATA_DIR / ".res" / package
            (res / "BundleFiles").mkdir(parents=True)
            (res / "BundleFiles" / name).write_bytes(raw)
            (res / "ManifestFiles").mkdir()
            for build in builds:
                (res / "ManifestFiles" / f"0release_{package}_{build}.bytes").touch()
        return root

    @staticmethod
    def file(root: Path, spec) -> Path:
        package, name = spec
        return root / engine.DATA_DIR / ".res" / package / "BundleFiles" / name


class CryptTests(unittest.TestCase):
    def test_matches_reference_across_period_boundary(self):
        rng = random.Random(7)
        seed = rng.randbytes(4096)
        body = rng.randbytes(engine.PERIOD + 10_000)
        encrypted = engine.crypt_body(body, seed)
        self.assertEqual(encrypted, reference_crypt(body, seed))
        self.assertEqual(engine.crypt_body(encrypted, seed), body)


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.base = [f"string {i}".encode() for i in range(60)]
        self.added = [f"added {i}".encode() for i in range(30)]
        self.hashes = array("Q", (fake_hash(s) for s in self.added))

    def test_rebuild_matches_pool_built_from_scratch(self):
        rebuilt = engine.rebuild_pool(
            build_pool(self.base), len(self.base), self.added, self.hashes
        )
        self.assertEqual(rebuilt, build_pool(self.base + self.added))
        self.assertEqual(pool_strings(rebuilt), self.base + self.added)

    def test_strip_is_exact_inverse(self):
        stock = build_pool(self.base)
        rebuilt = engine.rebuild_pool(stock, len(self.base), self.added, self.hashes)
        self.assertEqual(engine.strip_pool(rebuilt, len(self.base)), stock)

    def test_rebuild_rejects_wrong_stock_count(self):
        with self.assertRaises(engine.PatchError):
            engine.rebuild_pool(build_pool(self.base), 59, self.added, self.hashes)


class PatchDataTests(unittest.TestCase):
    def test_round_trip(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "patch.bin"
            engine.save_patch_data(path, fixture.patch)
            loaded = engine.load_patch_data(path)
        self.assertEqual(loaded, fixture.patch)
        self.assertEqual(loaded.game_label, "game 1.0.1 (build 3219598)")

    def test_shipped_patch_data(self):
        data = engine.load_patch_data()
        self.assertEqual(data.build, BUILD)
        self.assertTrue(data.version)
        self.assertTrue(data.game_version)
        self.assertGreater(len(data.new_strings), 0)
        self.assertEqual(len(data.new_strings), len(data.new_hashes))
        self.assertGreater(len(data.edits), 0)
        self.assertEqual(len(data.edits) % 3, 0)
        limit = data.stock_count + len(data.new_strings)
        self.assertTrue(all(v < limit for v in data.edits[2::3]))
        for info in data.files.values():
            self.assertEqual(len(info.seed), 4096)
            for digest in (info.stock_sha256, info.patched_sha256):
                self.assertRegex(digest, r"^[0-9a-f]{64}$")
        for value in data.new_strings:
            value.decode("utf-8")


class GameTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = self.fixture.patch

    def tearDown(self):
        self.tmp.cleanup()

    def status(self):
        return engine.inspect(self.root, self.patch)

    def test_install_and_restore(self):
        game = self.fixture.make_game(self.root)
        self.assertEqual(self.status().kind, "stock")
        engine.install(game, self.patch)
        self.assertEqual(self.status().kind, "patched")
        pool, table = Fixture.file(game, POOL), Fixture.file(game, TABLE)
        self.assertEqual(pool.read_bytes(), self.fixture.pool_patched)
        self.assertEqual(table.read_bytes(), self.fixture.table_patched)
        engine.install(game, self.patch)
        engine.restore(game, self.patch)
        self.assertEqual(self.status().kind, "stock")
        self.assertEqual(pool.read_bytes(), self.fixture.pool_stock)
        self.assertEqual(table.read_bytes(), self.fixture.table_stock)
        self.assertEqual(list(game.rglob("*.tmp")), [])

    def test_mixed_install_can_be_restored(self):
        game = self.fixture.make_game(self.root)
        Fixture.file(game, POOL).write_bytes(self.fixture.pool_patched)
        self.assertEqual(self.status().kind, "mixed")
        engine.restore(game, self.patch)
        self.assertEqual(self.status().kind, "stock")

    def test_unknown_files_are_refused(self):
        game = self.fixture.make_game(self.root)
        table = Fixture.file(game, TABLE)
        data = bytearray(table.read_bytes())
        data[-1] ^= 1
        table.write_bytes(bytes(data))
        self.assertEqual(self.status().kind, "unknown")
        with self.assertRaises(engine.PatchError):
            engine.install(game, self.patch)
        self.assertEqual(table.read_bytes(), bytes(data))

    def test_newer_build_is_reported(self):
        self.fixture.make_game(self.root, builds=(BUILD, "3300000"))
        status = self.status()
        self.assertEqual(status.kind, "wrong_build")
        self.assertIn("3300000", status.message)

    def test_missing_manifests_are_reported(self):
        self.fixture.make_game(self.root, builds=())
        self.assertEqual(self.status().kind, "wrong_build")

    def test_folder_without_game(self):
        self.assertEqual(self.status().kind, "not_found")


if __name__ == "__main__":
    unittest.main()
