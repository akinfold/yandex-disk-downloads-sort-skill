"""Shared test plumbing: import paths, a live fake server, env isolation."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "yandex-disk-downloads-sort", "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_api import TOKEN, FakeDisk, start_server  # noqa: E402


class FakeServerTest(unittest.TestCase):
    """A test case with a fresh fake Disk, a fresh workdir and env vars pointing at both."""

    downloads = "disk:/Загрузки"

    def setUp(self) -> None:
        self.fake = FakeDisk(downloads=self.downloads)
        self.server, self.base_url = start_server(self.fake)
        self.workdir = tempfile.mkdtemp(prefix="yadisk-sort-test-")
        self._env = dict(os.environ)
        for key in ("YANDEX_DISK_TOKEN", "YANDEX_DISK_OAUTH_TOKEN", "YANDEX_DISK_TOKEN_FILE"):
            os.environ.pop(key, None)
        os.environ["YANDEX_DISK_TOKEN"] = TOKEN
        os.environ["YANDEX_DISK_BASE_URL"] = self.base_url

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        os.environ.clear()
        os.environ.update(self._env)
