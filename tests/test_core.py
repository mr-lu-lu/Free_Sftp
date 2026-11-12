from __future__ import annotations

import tempfile
import threading
import unittest
import json
import os
from pathlib import Path
from unittest.mock import patch

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

from ftpmate.core import (
    FTPClient,
    FTPConfig,
    parse_unix_list_line,
    remote_join,
    remote_parent,
    remote_path,
)
from ftpmate.profiles import (
    Profile,
    Settings,
    protect_secret,
    save_settings,
    load_settings,
    unprotect_secret,
)


class PathTests(unittest.TestCase):
    def test_remote_paths(self) -> None:
        self.assertEqual(remote_path(""), "/")
        self.assertEqual(remote_path("home\\user"), "/home/user")
        self.assertEqual(remote_join("/srv/app", "data.txt"), "/srv/app/data.txt")
        self.assertEqual(remote_parent("/srv/app/data.txt"), "/srv/app")
        self.assertEqual(remote_parent("/"), "/")

    def test_unix_list_parser(self) -> None:
        entry = parse_unix_list_line(
            "-rw-r--r-- 1 root root 1234 Nov 12 10:20 report.txt",
            "/tmp",
        )
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertFalse(entry.is_dir)
        self.assertEqual(entry.name, "report.txt")
        self.assertEqual(entry.size, 1234)
        self.assertEqual(entry.path, "/tmp/report.txt")


class ProfileSecretTests(unittest.TestCase):
    def test_secret_round_trip(self) -> None:
        secret = "s3cret-密码"
        self.assertEqual(unprotect_secret(protect_secret(secret)), secret)

    def test_settings_round_trip_does_not_store_plain_password(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {"APPDATA": temp}):
                settings = Settings(
                    profiles=[
                        Profile(
                            name="test",
                            host="127.0.0.1",
                            username="user",
                            password="plain-secret",
                            protocol="sftp",
                            key_file="C:\\keys\\id_ed25519",
                            key_passphrase="key-secret",
                        )
                    ]
                )
                save_settings(settings)
                loaded = load_settings()
                self.assertEqual(loaded.profiles[0].password, "plain-secret")
                self.assertEqual(loaded.profiles[0].protocol, "sftp")
                self.assertEqual(loaded.profiles[0].key_file, "C:\\keys\\id_ed25519")
                self.assertEqual(loaded.profiles[0].key_passphrase, "key-secret")
                raw = (Path(temp) / "FTPMate" / "settings.json").read_text(encoding="utf-8")
                self.assertNotIn("plain-secret", raw)
                self.assertNotIn("key-secret", raw)
                data = json.loads(raw)
                self.assertTrue(data["profiles"][0]["password_enc"].startswith("dpapi:"))
                self.assertTrue(data["profiles"][0]["key_passphrase_enc"].startswith("dpapi:"))


class FtpIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server_root = self.root / "server"
        self.server_root.mkdir()
        authorizer = DummyAuthorizer()
        authorizer.add_user("user", "pass", str(self.server_root), perm="elradfmwMT")
        handler = FTPHandler
        handler.authorizer = authorizer
        handler.passive_ports = range(60000, 60100)
        self.server = FTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = FTPClient(
            FTPConfig(
                host="127.0.0.1",
                port=self.port,
                username="user",
                password="pass",
                timeout=10,
            )
        )
        self.client.connect()

    def tearDown(self) -> None:
        self.client.disconnect()
        self.server.close_all()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def test_upload_download_and_manage(self) -> None:
        source = self.root / "source"
        source.mkdir()
        (source / "hello.txt").write_text("hello ftp", encoding="utf-8")
        (source / "nested").mkdir()
        (source / "nested" / "data.bin").write_bytes(b"\x00\x01\x02" * 1000)

        self.client.ensure_remote_dir("/upload")
        self.client.upload_tree(str(source), "/upload")
        entries = self.client.list_dir("/upload")
        names = {entry.name for entry in entries}
        self.assertIn("hello.txt", names)
        self.assertIn("nested", names)
        self.assertEqual(self.client.file_size("/upload/hello.txt"), 9)

        self.client.rename("/upload/hello.txt", "/upload/renamed.txt")
        self.assertTrue(self.client.exists("/upload/renamed.txt"))
        self.assertEqual(self.client.file_size("/upload/renamed.txt"), 9)

        target = self.root / "download"
        self.client.download_tree("/upload", str(target))
        self.assertEqual((target / "renamed.txt").read_text(encoding="utf-8"), "hello ftp")
        self.assertEqual(
            (target / "nested" / "data.bin").read_bytes(),
            b"\x00\x01\x02" * 1000,
        )

        self.client.remove("/upload", True)
        self.assertEqual(self.client.list_dir("/"), [])

    def test_zero_byte_upload(self) -> None:
        source = self.root / "empty.txt"
        source.write_bytes(b"")
        self.client.upload_file(str(source), "/empty.txt")
        self.assertEqual(self.client.file_size("/empty.txt"), 0)


if __name__ == "__main__":
    unittest.main()
