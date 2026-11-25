import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "patcher"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import engine  # noqa: E402
import updates  # noqa: E402
from test_engine import POOL, TABLE, Fixture  # noqa: E402

DL = updates.DOWNLOADS


def release_json(tag="v1.5", assets=None):
    if assets is None:
        assets = [
            {
                "name": "patch-data.bin",
                "browser_download_url": f"{DL}{tag}/patch-data.bin",
            },
            {
                "name": "patch-data.bin.sha256",
                "browser_download_url": f"{DL}{tag}/patch-data.bin.sha256",
            },
        ]
    return {
        "tag_name": tag,
        "html_url": f"{updates.RELEASES_PAGE}tag/{tag}",
        "assets": assets,
    }


class ReleaseTests(unittest.TestCase):
    def test_newer_release_with_data(self):
        release = updates.release_from_json(release_json(), "1.4")
        self.assertEqual(release.version, "1.5")
        self.assertTrue(release.has_data)

    def test_same_or_older_release_is_ignored(self):
        self.assertIsNone(updates.release_from_json(release_json("v1.4"), "1.4"))
        self.assertIsNone(updates.release_from_json(release_json("v1.3"), "1.4"))

    def test_links_outside_this_repository_are_ignored(self):
        assets = [
            {
                "name": "patch-data.bin",
                "browser_download_url": "https://example.com/patch-data.bin",
            },
            {
                "name": "patch-data.bin.sha256",
                "browser_download_url": f"{DL}v1.5/patch-data.bin.sha256",
            },
        ]
        data = release_json(assets=assets)
        data["html_url"] = "https://example.com/elsewhere"
        release = updates.release_from_json(data, "1.4")
        self.assertFalse(release.has_data)
        self.assertEqual(release.url, updates.RELEASES_PAGE + "latest")

    def test_release_without_data_still_links_the_page(self):
        release = updates.release_from_json(release_json(assets=[]), "1.4")
        self.assertFalse(release.has_data)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.target = self.dir / "patch-data.bin"
        fixture = Fixture(english="Better", version="1.5")
        source = self.dir / "source.bin"
        engine.save_patch_data(source, fixture.patch)
        self.data = source.read_bytes()
        self.release = updates.release_from_json(release_json(), "1.4")

    def tearDown(self):
        self.tmp.cleanup()

    def serve(self, data, checksum=None):
        checksum = checksum or hashlib.sha256(data).hexdigest()
        files = {
            self.release.data_url: data,
            self.release.hash_url: f"{checksum}  patch-data.bin\n".encode(),
        }
        return mock.patch.object(
            updates.urllib.request,
            "urlopen",
            lambda request, **_: FakeResponse(files[request.full_url]),
        )

    def test_verified_download(self):
        with self.serve(self.data):
            patch, tmp = updates.download_patch_data(self.release, "1.4", self.target)
        self.assertEqual(patch.version, "1.5")
        self.assertFalse(self.target.exists())  # nothing replaced until it is kept
        updates.keep_download(tmp, self.target)
        self.assertEqual(self.target.read_bytes(), self.data)

    def test_checksum_mismatch_is_refused(self):
        with (
            self.serve(self.data, checksum="0" * 64),
            self.assertRaises(updates.UpdateError),
        ):
            updates.download_patch_data(self.release, "1.4", self.target)
        self.assertEqual(list(self.dir.glob("patch-data.bin*")), [])

    def test_damaged_data_is_refused(self):
        with (
            self.serve(b"not patch data"),
            self.assertRaises(updates.UpdateError),
        ):
            updates.download_patch_data(self.release, "1.4", self.target)
        self.assertEqual(list(self.dir.glob("patch-data.bin*")), [])

    def test_version_must_match_the_release(self):
        release = updates.release_from_json(release_json("v1.6"), "1.4")
        files = {
            release.data_url: self.data,
            release.hash_url: f"{hashlib.sha256(self.data).hexdigest()}  x\n".encode(),
        }
        with (
            mock.patch.object(
                updates.urllib.request,
                "urlopen",
                lambda request, **_: FakeResponse(files[request.full_url]),
            ),
            self.assertRaises(updates.UpdateError),
        ):
            updates.download_patch_data(release, "1.4", self.target)
        self.assertEqual(list(self.dir.glob("patch-data.bin*")), [])


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.old = Fixture(version="1.4")
        self.new = Fixture(english="Better", version="1.5")
        self.game = self.dir / "game"
        self.game.mkdir()
        self.target = self.dir / "patch-data.bin"
        self.download = self.dir / "patch-data.bin.download"
        engine.save_patch_data(self.download, self.new.patch)

    def tearDown(self):
        self.tmp.cleanup()

    def switch(self, **kw):
        return updates.switch_patch_data(
            self.game,
            self.old.patch,
            self.new.patch,
            self.download,
            target=self.target,
            **kw,
        )

    def test_installed_patch_is_replaced(self):
        self.old.make_game(self.game)
        engine.install(self.game, self.old.patch)
        switched = []
        message = self.switch(on_switched=switched.append)
        self.assertIn("Updated to patch 1.5", message)
        self.assertEqual(switched, [self.new.patch])
        self.assertEqual(engine.inspect(self.game, self.new.patch).kind, "patched")
        self.assertEqual(
            Fixture.file(self.game, POOL).read_bytes(), self.new.pool_patched
        )
        self.assertEqual(
            Fixture.file(self.game, TABLE).read_bytes(), self.new.table_patched
        )
        self.assertEqual(engine.load_patch_data(self.target), self.new.patch)

    def test_japanese_game_is_left_alone(self):
        self.old.make_game(self.game)
        message = self.switch()
        self.assertIn("Install", message)
        self.assertEqual(engine.inspect(self.game, self.new.patch).kind, "stock")
        self.assertTrue(self.target.exists())

    def test_other_game_build_is_refused(self):
        self.old.make_game(self.game)
        engine.install(self.game, self.old.patch)
        other = Fixture(english="Better", version="1.5")
        other.patch.build = "3300000"
        with self.assertRaises(updates.UpdateError):
            updates.switch_patch_data(
                self.game,
                self.old.patch,
                other.patch,
                self.download,
                target=self.target,
            )
        self.assertFalse(self.target.exists())
        self.assertFalse(self.download.exists())
        self.assertEqual(engine.inspect(self.game, self.old.patch).kind, "patched")

    def test_without_a_game_folder_the_data_is_kept(self):
        updates.switch_patch_data(
            None, self.old.patch, self.new.patch, self.download, target=self.target
        )
        self.assertTrue(self.target.exists())


class BestDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "patch-data.bin"
        self.bundled = Fixture(version="1.4").patch

    def tearDown(self):
        self.tmp.cleanup()

    def test_newer_download_wins(self):
        newer = Fixture(english="Better", version="1.5").patch
        engine.save_patch_data(self.path, newer)
        self.assertEqual(updates.best_patch_data(self.bundled, self.path), newer)

    def test_older_download_is_ignored(self):
        engine.save_patch_data(self.path, Fixture(version="1.3").patch)
        self.assertIs(updates.best_patch_data(self.bundled, self.path), self.bundled)

    def test_missing_or_damaged_download_is_ignored(self):
        self.assertIs(updates.best_patch_data(self.bundled, self.path), self.bundled)
        self.path.write_bytes(b"junk")
        self.assertIs(updates.best_patch_data(self.bundled, self.path), self.bundled)


if __name__ == "__main__":
    unittest.main()
