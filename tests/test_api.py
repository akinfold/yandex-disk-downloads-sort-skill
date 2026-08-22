import os
import tempfile
import unittest

import helpers
import yadisk_api as api
from fake_api import TOKEN


class PathHelperTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(api.normalize_path("/Загрузки/"), "disk:/Загрузки")
        self.assertEqual(api.normalize_path("Загрузки"), "disk:/Загрузки")
        self.assertEqual(api.normalize_path("disk:/a//b/./c/"), "disk:/a/b/c")
        self.assertEqual(api.normalize_path("trash:/x"), "trash:/x")
        self.assertEqual(api.normalize_path("disk:/a/back\\slash.txt"), "disk:/a/back\\slash.txt")
        self.assertEqual(api.normalize_path(""), "disk:/")

    def test_parent_and_join(self):
        self.assertEqual(api.parent_of("disk:/a/b/c.txt"), "disk:/a/b")
        self.assertEqual(api.parent_of("disk:/a"), "disk:/")
        self.assertEqual(api.parent_of("disk:/"), "")
        self.assertEqual(api.join_path("disk:/a/", "b.txt"), "disk:/a/b.txt")

    def test_suffix(self):
        self.assertEqual(api.with_suffix("report.pdf", 2), "report (2).pdf")
        self.assertEqual(api.with_suffix("archive.tar.gz", 3), "archive (3).tar.gz")
        self.assertEqual(api.with_suffix("README", 2), "README (2)")

    def test_encode_params_is_strict(self):
        encoded = api.encode_params({"path": "disk:/Загрузки/a b+c#d.txt", "limit": 5, "skip": None})
        self.assertIn("path=disk%3A%2F%D0%97", encoded)
        self.assertIn("a%20b%2Bc%23d.txt", encoded)
        self.assertIn("limit=5", encoded)
        self.assertNotIn("skip", encoded)


class TokenTests(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        for key in ("YANDEX_DISK_TOKEN", "YANDEX_DISK_OAUTH_TOKEN", "YANDEX_DISK_TOKEN_FILE"):
            os.environ.pop(key, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_env_precedence_and_file(self):
        with self.assertRaises(api.TokenMissing):
            api.load_token()
        os.environ["YANDEX_DISK_OAUTH_TOKEN"] = "alias"
        self.assertEqual(api.load_token(), "alias")
        os.environ["YANDEX_DISK_TOKEN"] = "primary"
        self.assertEqual(api.load_token(), "primary")
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".token") as handle:
            handle.write("  from-file \r\n")
        self.assertEqual(api.load_token(handle.name), "from-file")  # explicit file beats the environment
        del os.environ["YANDEX_DISK_TOKEN"], os.environ["YANDEX_DISK_OAUTH_TOKEN"]
        os.environ["YANDEX_DISK_TOKEN_FILE"] = handle.name
        self.assertEqual(api.load_token(), "from-file")

    def test_named_token_file_reports_its_own_problems(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".token") as handle:
            handle.write("")
        os.environ["YANDEX_DISK_TOKEN_FILE"] = handle.name
        with self.assertRaises(api.TokenMissing) as ctx:
            api.load_token()
        self.assertIn("is empty", str(ctx.exception))
        self.assertIn(handle.name, str(ctx.exception))
        os.environ["YANDEX_DISK_TOKEN_FILE"] = "/nonexistent/token/file"
        with self.assertRaises(api.TokenMissing) as ctx:
            api.load_token()
        self.assertIn("does not exist", str(ctx.exception))
        # an env var still wins over the file named by YANDEX_DISK_TOKEN_FILE
        os.environ["YANDEX_DISK_TOKEN"] = "y0_from_env"
        self.assertEqual(api.load_token(), "y0_from_env")

    def test_token_files_with_boms_and_bad_values(self):
        with tempfile.NamedTemporaryFile("wb", delete=False, suffix=".token") as handle:
            handle.write("y0_bom_token\n".encode("utf-8-sig"))
        self.assertEqual(api.load_token(handle.name), "y0_bom_token")
        with tempfile.NamedTemporaryFile("wb", delete=False, suffix=".token") as handle:
            handle.write("y0_utf16_token\r\n".encode("utf-16"))
        self.assertEqual(api.load_token(handle.name), "y0_utf16_token")
        with self.assertRaises(api.TokenMissing) as ctx:
            api.load_token("/nonexistent/token/file")
        self.assertNotIn("y0_", str(ctx.exception))
        os.environ["YANDEX_DISK_TOKEN"] = "y0_first\ny0_second"
        with self.assertRaises(api.TokenMissing) as ctx:
            api.load_token()
        self.assertNotIn("y0_first", str(ctx.exception))  # never echo the value


class ClientTests(helpers.FakeServerTest):
    def client(self, token=TOKEN, **kw):
        kw.setdefault("sleep", lambda _s: None)
        return api.YandexDisk(token, base_url=self.base_url, **kw)

    def test_unauthorized(self):
        with self.assertRaises(api.YandexDiskError) as ctx:
            self.client("wrong").disk_info()
        self.assertEqual((ctx.exception.status, ctx.exception.code), (401, "UnauthorizedError"))

    def test_downloads_path_from_system_folders(self):
        self.assertEqual(self.client().downloads_path(), "disk:/Загрузки")

    def test_pagination(self):
        for i in range(7):
            self.fake.add_file(f"disk:/Загрузки/f{i}.txt")
        names = [i["name"] for i in self.client().iter_children("disk:/Загрузки", page_size=3)]
        self.assertEqual(names, [f"f{i}.txt" for i in range(7)])
        list_calls = [q for m, p, q in self.fake.log if p == "/v1/disk/resources" and m == "GET"]
        self.assertEqual([q["offset"] for q in list_calls], ["0", "3", "6"])

    def test_exists(self):
        self.assertTrue(self.client().exists("disk:/Загрузки"))
        self.assertFalse(self.client().exists("disk:/Nope"))

    def test_mkdir_creates_parents_and_is_idempotent(self):
        disk = self.client()
        self.assertEqual(disk.mkdir("disk:/Загрузки/A/B/C"), "created")
        self.assertEqual(disk.mkdir("disk:/Загрузки/A/B/C"), "exists")
        self.assertIn("disk:/Загрузки/A/B", self.fake.tree)

    def test_mkdir_on_a_file_path_is_an_error(self):
        disk = self.client()
        self.fake.add_file("disk:/Загрузки/Архивы", 5)
        with self.assertRaises(api.YandexDiskError) as ctx:
            disk.mkdir("disk:/Загрузки/Архивы")
        self.assertEqual(ctx.exception.status, 409)
        self.assertTrue(disk.exists("disk:/Загрузки/Архивы"))
        self.assertFalse(disk.is_dir("disk:/Загрузки/Архивы"))
        self.assertTrue(disk.is_dir("disk:/Загрузки"))

    def test_retry_after_never_negative(self):
        self.assertEqual(api._retry_after({"Retry-After": "-3"}, 0), 0.0)
        self.assertEqual(api._retry_after({"Retry-After": "999"}, 0), 30.0)
        self.assertEqual(api._retry_after({}, 2), 4.0)

    def test_permanent_errors_are_not_retried(self):
        waits = []
        disk = api.YandexDisk(TOKEN, base_url="http://127.0.0.1:1", sleep=waits.append, timeout=2)
        with self.assertRaises(api.YandexDiskError):
            disk.disk_info()
        self.assertEqual(waits, [])

    def test_move_conflicts(self):
        disk = self.client()
        self.fake.add_file("disk:/Загрузки/a.txt")
        self.fake.add_file("disk:/Загрузки/b.txt")
        with self.assertRaises(api.YandexDiskError) as ctx:
            disk.move("disk:/Загрузки/a.txt", "disk:/Загрузки/b.txt")
        self.assertEqual(ctx.exception.code, api.ERR_ALREADY_EXISTS)
        with self.assertRaises(api.YandexDiskError) as ctx:
            disk.move("disk:/Загрузки/missing.txt", "disk:/Загрузки/c.txt")
        self.assertEqual(ctx.exception.status, 404)

    def test_move_waits_for_deferred_operation(self):
        self.fake.async_moves = True
        self.fake.add_file("disk:/Загрузки/a.txt")
        result = self.client().move("disk:/Загрузки/a.txt", "disk:/Загрузки/z.txt")
        self.assertEqual(result["status"], "success")
        self.assertIn("disk:/Загрузки/z.txt", self.fake.tree)
        polls = [q for m, p, q in self.fake.log if p.startswith("/v1/disk/operations")]
        self.assertEqual(len(polls), 2)  # in-progress, then success
        self.assertEqual(polls[0].get("id"), "op1")  # the real API's ?id= href form is followed as-is

    def test_lost_move_response_marks_last_retried(self):
        disk = self.client()
        self.fake.add_file("disk:/Загрузки/a.txt")
        self.fake.lose_response_for.add("disk:/Загрузки/a.txt")
        with self.assertRaises(api.YandexDiskError) as ctx:
            disk.move("disk:/Загрузки/a.txt", "disk:/Загрузки/z.txt")
        self.assertEqual(ctx.exception.status, 404)
        self.assertTrue(disk.last_retried)
        self.assertIn("disk:/Загрузки/z.txt", self.fake.tree)

    def test_retries_on_429_with_retry_after(self):
        waits = []
        disk = self.client(sleep=waits.append)
        self.fake.inject.append((429, {"error": "TooManyRequests", "description": "slow down"}, {"Retry-After": "3"}))
        self.fake.inject.append((503, None, {}))
        info = disk.disk_info()
        self.assertIn("system_folders", info)
        self.assertEqual(waits[0], 3.0)
        self.assertEqual(len(waits), 2)

    def test_gives_up_after_max_retries(self):
        disk = self.client(max_retries=1)
        self.fake.inject.extend([(500, None, {})] * 3)
        with self.assertRaises(api.YandexDiskError) as ctx:
            disk.disk_info()
        self.assertEqual(ctx.exception.status, 500)

    def test_error_prefers_english_description(self):
        with self.assertRaises(api.YandexDiskError) as ctx:
            self.client().get("disk:/nope")
        self.assertEqual(ctx.exception.message, "Resource not found.")


if __name__ == "__main__":
    unittest.main()
