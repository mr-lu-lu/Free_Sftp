from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import unittest
from pathlib import Path

import asyncssh
import paramiko
from paramiko.sftp import CMD_CLOSE, CMD_HANDLE, CMD_NAME, CMD_OPENDIR, CMD_READDIR, CMD_STATUS

from ftpmate.core import FTPConfig, create_client
from ftpmate.sftp import SFTPClient


class TestSSHServer(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return username == "user" and password == "pass"


class SftpIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server_root = self.root / "server"
        self.server_root.mkdir()
        self.known_hosts = self.root / "known_hosts"
        self.host_key = asyncssh.generate_private_key("ssh-ed25519")
        self.loop = asyncio.new_event_loop()
        self.stop_event = asyncio.Event()
        self.ready = threading.Event()
        self.port = 0
        self.thread = threading.Thread(target=self._run_server, daemon=True)
        self.thread.start()
        if not self.ready.wait(10):
            self.fail("SFTP test server did not start")
        self.client = create_client(
            FTPConfig(
                host="127.0.0.1",
                port=self.port,
                username="user",
                password="pass",
                protocol="sftp",
                known_hosts=str(self.known_hosts),
                timeout=10,
            )
        )
        self.client.connect()

    def _run_server(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        server = await asyncssh.listen(
            "127.0.0.1",
            0,
            server_host_keys=[self.host_key],
            server_factory=TestSSHServer,
            sftp_factory=lambda chan: asyncssh.SFTPServer(
                chan, chroot=os.fsencode(str(self.server_root))
            ),
            password_auth=True,
        )
        self.port = server.get_port()
        self.ready.set()
        await self.stop_event.wait()
        server.close()
        await server.wait_closed()

    def tearDown(self) -> None:
        self.client.disconnect()
        self.loop.call_soon_threadsafe(self.stop_event.set)
        self.thread.join(timeout=5)
        self.loop.close()
        self.temp.cleanup()

    def test_upload_download_and_manage(self) -> None:
        source = self.root / "source"
        source.mkdir()
        (source / "hello.txt").write_text("hello sftp", encoding="utf-8")
        (source / "nested").mkdir()
        (source / "nested" / "data.bin").write_bytes(b"\x00\x01\x02" * 1000)

        self.client.ensure_remote_dir("/upload")
        self.client.upload_tree(str(source), "/upload")
        names = {entry.name for entry in self.client.list_dir("/upload")}
        self.assertIn("hello.txt", names)
        self.assertIn("nested", names)
        self.assertEqual(self.client.file_size("/upload/hello.txt"), 10)
        self.assertTrue(self.known_hosts.exists())
        self.assertTrue(self.client.host_key_fingerprint.startswith("SHA256:"))

        self.client.rename("/upload/hello.txt", "/upload/renamed.txt")
        self.assertTrue(self.client.exists("/upload/renamed.txt"))
        target = self.root / "download"
        self.client.download_tree("/upload", str(target))
        self.assertEqual((target / "renamed.txt").read_text(encoding="utf-8"), "hello sftp")
        self.assertEqual(
            (target / "nested" / "data.bin").read_bytes(),
            b"\x00\x01\x02" * 1000,
        )

        self.client.remove("/upload", True)
        self.assertEqual(self.client.list_dir("/"), [])

    def test_upload_resume(self) -> None:
        payload = bytes(range(256)) * 1024
        source = self.root / "large.bin"
        source.write_bytes(payload)
        partial = self.server_root / "large.bin"
        partial.write_bytes(payload[:100000])

        self.client.upload_file(str(source), "/large.bin")
        self.assertEqual(partial.read_bytes(), payload)

    def test_zero_byte_upload(self) -> None:
        source = self.root / "empty.txt"
        source.write_bytes(b"")
        self.client.upload_file(str(source), "/empty.txt")
        self.assertEqual(self.client.file_size("/empty.txt"), 0)

    def test_reconnect_uses_saved_host_key(self) -> None:
        self.client.disconnect()
        self.client.connect()
        self.assertTrue(self.client.connected)
        self.assertTrue(self.client.host_key_fingerprint.startswith("SHA256:"))


class SftpEncodingTests(unittest.TestCase):
    def test_gbk_fallback_and_round_trip(self) -> None:
        client = SFTPClient(FTPConfig(host="127.0.0.1", protocol="sftp", encoding="utf-8"))
        raw = "中文文件.txt".encode("gbk")
        self.assertEqual(client._decode_name(raw), "中文文件.txt")
        self.assertIn(client._name_encoding.lower(), ("gb18030", "gbk"))
        self.assertEqual(client._encode_path("中文文件.txt"), raw)

    def test_raw_listing_with_gbk_filename(self) -> None:
        class FakeSftp:
            def __init__(self, name: bytes):
                self.name = name
                self.reads = 0

            def _request(self, command, *args):
                if command == CMD_OPENDIR:
                    message = paramiko.Message()
                    message.add_string(b"handle")
                    message.rewind()
                    return CMD_HANDLE, message
                if command == CMD_READDIR:
                    self.reads += 1
                    if self.reads > 1:
                        raise EOFError
                    message = paramiko.Message()
                    message.add_int(1)
                    message.add_string(self.name)
                    message.add_string(self.name)
                    message.add_int(0)
                    message.rewind()
                    return CMD_NAME, message
                if command == CMD_CLOSE:
                    message = paramiko.Message()
                    message.add_int(0)
                    message.rewind()
                    return CMD_STATUS, message
                raise AssertionError(f"unexpected command: {command}")

        client = SFTPClient(FTPConfig(host="127.0.0.1", protocol="sftp", encoding="utf-8"))
        client._ensure = lambda: FakeSftp("报告.txt".encode("gbk"))
        entries = client._listdir_attr("/")
        self.assertEqual([entry.filename for entry in entries], ["报告.txt"])


if __name__ == "__main__":
    unittest.main()
