from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import asyncssh

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class SmokeSSHServer(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return username == "user" and password == "pass"


def pump(root, predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        root.update()
        if predicate():
            return
        time.sleep(0.02)
    raise TimeoutError("UI condition was not reached")


def main() -> None:
    temp = tempfile.TemporaryDirectory()
    base = Path(temp.name)
    server_root = base / "server"
    server_root.mkdir()
    os.environ["APPDATA"] = str(base / "appdata")

    loop = asyncio.new_event_loop()
    stop_event = asyncio.Event()
    ready = threading.Event()
    holder: dict[str, object] = {}

    async def serve() -> None:
        server = await asyncssh.listen(
            "127.0.0.1",
            0,
            server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
            server_factory=SmokeSSHServer,
            sftp_factory=lambda chan: asyncssh.SFTPServer(
                chan, chroot=os.fsencode(str(server_root))
            ),
            password_auth=True,
        )
        holder["server"] = server
        holder["port"] = server.get_port()
        ready.set()
        await stop_event.wait()
        server.close()
        await server.wait_closed()

    def run_server() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(serve())

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    if not ready.wait(10):
        raise RuntimeError("SFTP test server did not start")

    from main import create_root
    from ftpmate.ui import FTPMateApp

    root = create_root()
    root.withdraw()
    app = FTPMateApp(root)
    app.protocol_var.set("SFTP")
    app.host_var.set("127.0.0.1")
    app.port_var.set(str(holder["port"]))
    app.user_var.set("user")
    app.password_var.set("pass")
    app._connect()
    pump(root, lambda: app.connected)
    assert app.client is not None
    assert app.client.host_key_fingerprint

    source = base / "source"
    source.mkdir()
    payload = source / "smoke.txt"
    payload.write_text("ui sftp upload", encoding="utf-8")
    app._load_local(str(source))
    app._upload_paths([str(payload)])
    pump(root, lambda: any(entry.name == "smoke.txt" for entry in app.remote_entries))
    assert (server_root / "smoke.txt").read_text(encoding="utf-8") == "ui sftp upload"

    download_dir = base / "download"
    download_dir.mkdir()
    app._load_local(str(download_dir))
    remote_entry = next(entry for entry in app.remote_entries if entry.name == "smoke.txt")
    app._download_entries([remote_entry])
    pump(root, lambda: (download_dir / "smoke.txt").exists())
    pump(root, lambda: not app.transfer_active)
    assert (download_dir / "smoke.txt").read_text(encoding="utf-8") == "ui sftp upload"

    app._on_close()
    loop.call_soon_threadsafe(stop_event.set)
    thread.join(timeout=5)
    loop.close()
    temp.cleanup()
    print("ui-sftp-smoke-ok")


if __name__ == "__main__":
    main()
