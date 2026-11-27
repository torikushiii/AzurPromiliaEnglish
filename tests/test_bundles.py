import json
import lzma
import random
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "patcher"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bundles  # noqa: E402
import engine  # noqa: E402
from test_engine import POOL, Fixture  # noqa: E402

_order = list(range(16))
random.Random(9).shuffle(_order)
# a real key's first 8 bytes hold a permutation of the 16 nibble values
KEY = bytes((_order[2 * i] << 4) | _order[2 * i + 1] for i in range(8)) + bytes(
    range(40, 48)
)
BOOT_PATH = r"AzurPromiliaJP_Data\StreamingAssets\.res\static_package\boot.r"
CSV = (
    "\ufeffkey,chs,cht,en,jp,kr\n"
    "alert_title,提醒,提醒,Alert,警告,경고\n"
    'retry,重试,重試,Retry,再試行,"다시 시도"\n'
    "initializing,正在初始化…,,,初期化中……,\n"
    "start_tip,开始旅程,,,旅を始める,\n"
).encode()
STRINGS = {
    "alert_title": "Alert",
    "initializing": "Initializing...",
    "start_tip": "Begin Your Journey",
}


def compress(plain: bytes) -> bytes:
    seqs, tail = bundles.lz4_compress(plain)
    return bundles.lz4_encode([*seqs, (tail, len(plain) - tail, 0, 0)], plain)


def table_block(rng: random.Random) -> bytes:
    head = bytes(rng.randrange(4) for _ in range(300)) + bundles.TAG
    head += bytes(-len(head) % 4)
    body = head + struct.pack("<i", len(CSV)) + CSV
    return body + bytes(rng.randrange(8) for _ in range(2000))


def make_bundle(blocks: list[bytes]) -> bytes:
    """A small UnityCN bundle with LZ4 blocks encrypted under KEY."""
    cipher = bundles.Cipher(KEY)
    streams = [compress(b) for b in blocks]
    stored = [cipher.encrypt(s, i) for i, s in enumerate(streams)]
    info = bytearray(16) + struct.pack(">i", len(blocks))
    for b, s in zip(blocks, stored, strict=True):
        info += struct.pack(">IIH", len(b), len(s), 0x103)
    info += struct.pack(">i", 1) + struct.pack(">qqI", 0, sum(map(len, blocks)), 4)
    info += b"CAB-test\x00"
    info_stream = compress(bytes(info))
    head = b"UnityFS\x00" + struct.pack(">I", 8) + b"5.x.x\x00" + b"0.0.0\x00"
    size_at = len(head)
    head += struct.pack(">qIII", 0, len(info_stream), len(info), 0x643)
    head += bytes(-len(head) % 16) + bytes(65)
    head += bytes(-len(head) % 16) + info_stream
    head += bytes(-len(head) % 16)
    data = bytearray(head + b"".join(stored))
    struct.pack_into(">q", data, size_at, len(data))
    return bytes(data)


def boot_info(stock: bytes) -> bundles.TextBundle:
    info = bundles.TextBundle(
        "boot", "csv", BOOT_PATH, 0, len(stock), KEY.hex(), [1],
        engine._sha(stock), "", 0, 0, "", dict(STRINGS),
    )  # fmt: skip
    start, end, _ = bundles.changed_range(stock, info)
    info.backup_start, info.backup_end = start, end
    info.backup_sha256 = engine._sha(stock[start:end])
    info.patched_sha256 = engine._sha(bundles.patched_bytes(stock, info))
    return info


class BundleTests(unittest.TestCase):
    def setUp(self):
        rng = random.Random(5)
        self.other = bytes(rng.randrange(16) for _ in range(5000))
        self.stock = make_bundle([self.other, table_block(rng)])
        self.info = boot_info(self.stock)

    def table_text(self, data: bytes) -> str:
        plain = bundles.table_plain(data, self.info)
        start, length = bundles.find_table(plain)
        return plain[start : start + length].decode("utf-8-sig")

    def test_lz4_round_trip(self):
        plain = bytes(random.Random(1).randrange(6) for _ in range(20000))
        for seqs, tail in (bundles.lz4_compress(plain), bundles.lz4_optimal(plain)):
            stream = bundles.lz4_encode([*seqs, (tail, len(plain) - tail, 0, 0)], plain)
            self.assertEqual(bundles.lz4_parse(stream, len(plain))[1], plain)

    def test_cipher_round_trip(self):
        stream = compress(table_block(random.Random(2)))
        cipher = bundles.Cipher(KEY)
        encrypted = cipher.encrypt(stream, 4)
        self.assertNotEqual(encrypted, stream)
        self.assertEqual(cipher.decrypt(encrypted, 4), stream)

    def test_patch_keeps_size_and_other_blocks(self):
        patched = bundles.patched_bytes(self.stock, self.info)
        self.assertEqual(len(patched), len(self.stock))
        self.assertEqual(
            patched[: self.info.backup_start], self.stock[: self.info.backup_start]
        )
        self.assertEqual(
            patched[self.info.backup_end :], self.stock[self.info.backup_end :]
        )
        text = self.table_text(patched)
        self.assertIn("initializing,正在初始化…,,,Initializing...,", text)
        self.assertIn("start_tip,开始旅程,,,Begin Your Journey,", text)
        self.assertIn('retry,重试,重試,Retry,再試行,"다시 시도"', text)
        self.assertEqual(len(text.encode()), len(CSV.decode("utf-8-sig").encode()))

    def test_restore_is_exact(self):
        patched = bundles.patched_bytes(self.stock, self.info)
        backup = self.stock[self.info.backup_start : self.info.backup_end]
        self.assertEqual(bundles.restored_bytes(patched, self.info, backup), self.stock)

    def test_plan_gives_the_same_bytes(self):
        plan = bundles.build_plan(self.stock, self.info)
        self.assertTrue(plan["blocks"])
        self.assertEqual(
            bundles._patched(self.stock, self.info, plan),
            bundles.patched_bytes(self.stock, self.info),
        )

    def test_plan_holds_no_text(self):
        plan = bundles.build_plan(self.stock, self.info)
        values = [v for flat in plan["blocks"].values() for v in flat]
        self.assertTrue(all(isinstance(v, int) for v in values))

    def test_stale_plan_falls_back_to_full_rebuild(self):
        plan = bundles.build_plan(self.stock, self.info)
        expected = bundles.patched_bytes(self.stock, self.info)
        broken = {"blocks": {k: [v[0] + 1, *v[1:]] for k, v in plan["blocks"].items()}}
        with self.assertRaises(bundles.BundleError):
            bundles._patched(self.stock, self.info, broken)
        self.assertEqual(bundles.patched_bytes(self.stock, self.info, broken), expected)

    def test_longer_english_is_refused(self):
        self.info.strings = {"alert_title": "x" * 500}
        with self.assertRaises(bundles.BundleError):
            bundles.patched_bytes(self.stock, self.info)


class GameBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.fixture = Fixture()
        (root / "game").mkdir()
        self.game = self.fixture.make_game(root / "game")
        rng = random.Random(5)
        self.stock = make_bundle(
            [bytes(rng.randrange(16) for _ in range(5000)), table_block(rng)]
        )
        self.info = boot_info(self.stock)
        self.patch = self.fixture.patch
        self.patch.text_bundles = [self.info]
        self.target = self.game / Path(BOOT_PATH.replace("\\", "/"))
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(self.stock)
        backups = mock.patch.object(engine, "BACKUP_DIR", root / "backup")
        backups.start()
        self.addCleanup(backups.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_install_and_restore(self):
        self.assertEqual(engine.inspect(self.game, self.patch).kind, "stock")
        engine.install(self.game, self.patch)
        status = engine.inspect(self.game, self.patch)
        self.assertEqual(status.kind, "patched")
        self.assertEqual(status.states["boot"], "patched")
        self.assertEqual(
            engine._sha(self.target.read_bytes()), self.info.patched_sha256
        )
        engine.restore(self.game, self.patch)
        self.assertEqual(engine.inspect(self.game, self.patch).kind, "stock")
        self.assertEqual(self.target.read_bytes(), self.stock)

    def test_restore_without_backup_changes_nothing(self):
        engine.install(self.game, self.patch)
        for path in engine.BACKUP_DIR.iterdir():
            path.unlink()
        pool = Fixture.file(self.game, POOL).read_bytes()
        with self.assertRaises(engine.PatchError):
            engine.restore(self.game, self.patch)
        self.assertEqual(Fixture.file(self.game, POOL).read_bytes(), pool)
        self.assertEqual(engine.inspect(self.game, self.patch).kind, "patched")

    def test_changed_bundle_is_skipped(self):
        data = bytearray(self.stock)
        data[-1] ^= 1
        self.target.write_bytes(bytes(data))
        engine.install(self.game, self.patch)
        self.assertEqual(engine.inspect(self.game, self.patch).kind, "patched")
        self.assertEqual(self.target.read_bytes(), bytes(data))

    def test_patch_data_round_trip(self):
        path = Path(self.tmp.name) / "patch.bin"
        self.patch.bundle_plans = {"boot": bundles.build_plan(self.stock, self.info)}
        engine.save_patch_data(path, self.patch)
        self.assertEqual(engine.load_patch_data(path), self.patch)

    def test_install_uses_the_plan(self):
        self.patch.bundle_plans = {"boot": bundles.build_plan(self.stock, self.info)}
        with mock.patch.object(
            bundles, "rewrite", side_effect=AssertionError("search")
        ):
            engine.install(self.game, self.patch)
        self.assertEqual(
            engine._sha(self.target.read_bytes()), self.info.patched_sha256
        )

    def test_old_loaders_ignore_plans(self):
        # 1.7.1 and older build TextBundle(**b) from "bundles" and ignore unknown keys
        path = Path(self.tmp.name) / "patch.bin"
        self.patch.bundle_plans = {"boot": bundles.build_plan(self.stock, self.info)}
        engine.save_patch_data(path, self.patch)
        raw = lzma.decompress(path.read_bytes())
        (n,) = struct.unpack_from("<I", raw, len(engine.MAGIC))
        start = len(engine.MAGIC) + 4
        header = json.loads(raw[start : start + n])
        self.assertIn("plans", header)
        for b in header["bundles"]:
            self.assertEqual(set(b), set(bundles.TextBundle.__dataclass_fields__))


if __name__ == "__main__":
    unittest.main()
