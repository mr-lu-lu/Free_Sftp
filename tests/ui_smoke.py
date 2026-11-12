from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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

    authorizer = DummyAuthorizer()
    authorizer.add_user("user", "pass", str(server_root), perm="elradfmwMT")
    handler = FTPHandler
    handler.authorizer = authorizer
    handler.passive_ports = range(60100, 60200)
    server = FTPServer(("127.0.0.1", 0), handler)
    port = server.socket.getsockname()[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    from main import create_root
    from ftpmate.profiles import Profile, load_settings
    from ftpmate.ui import FTPMateApp, REMOTE_DRAG_PREFIX

    root = create_root()
    root.withdraw()
    app = FTPMateApp(root)
    profile = Profile(
        name="smoke",
        host="127.0.0.1",
        port=port,
        username="user",
        password="pass",
        protocol="ftp",
    )
    app.settings.upsert_profile(profile)
    app.settings.last_profile = profile.name
    app._refresh_profile_values(profile.name)
    app._apply_profile(profile)
    assert app.remote_pane in app._drop_targets
    assert app.remote_tree in app._drop_targets
    assert app.local_drag_enabled
    assert app.remote_drag_enabled
    assert app.local_pane in app._local_drop_targets
    assert app.local_tree in app._local_drop_targets
    app.protocol_var.set("FTP")
    app.host_var.set("127.0.0.1")
    app.port_var.set(str(port))
    app.user_var.set("user")
    app.password_var.set("pass")
    app._connect()
    pump(root, lambda: app.connected)
    assert app.client is not None

    source = base / "source"
    source.mkdir()
    payload = source / "smoke.txt"
    payload.write_text("ui smoke upload", encoding="utf-8")
    internal = source / "internal-drag.txt"
    internal.write_text("internal drag upload", encoding="utf-8")
    app._load_local(str(source))
    assert load_settings().last_local_path == str(source)
    app._upload_paths([str(payload)])
    pump(root, lambda: any(entry.name == "smoke.txt" for entry in app.remote_entries))
    assert (server_root / "smoke.txt").read_text(encoding="utf-8") == "ui smoke upload"

    dropped = source / "dropped.txt"
    dropped.write_text("dropped upload", encoding="utf-8")
    app._on_drop(SimpleNamespace(data=root.tk.call("list", str(dropped))))
    pump(root, lambda: any(entry.name == "dropped.txt" for entry in app.remote_entries))
    assert (server_root / "dropped.txt").read_text(encoding="utf-8") == "dropped upload"

    row = next(str(index) for index, entry in enumerate(app.local_entries) if entry.name == internal.name)
    app.local_tree.selection_set(row)
    drag_result = app._on_local_drag_init(SimpleNamespace(y_root=0))
    assert drag_result[1] == "DND_Files"
    app._on_drop(SimpleNamespace(data=root.tk.call("list", *drag_result[2])))
    pump(root, lambda: any(entry.name == internal.name for entry in app.remote_entries))
    assert (server_root / internal.name).read_text(encoding="utf-8") == "internal drag upload"

    download_dir = base / "download"
    download_dir.mkdir()
    app._load_local(str(download_dir))
    remote_entry = next(entry for entry in app.remote_entries if entry.name == "smoke.txt")
    app._download_entries([remote_entry])
    pump(root, lambda: (download_dir / "smoke.txt").exists())
    pump(root, lambda: not app.transfer_active)
    assert (download_dir / "smoke.txt").read_text(encoding="utf-8") == "ui smoke upload"

    drag_download = base / "drag-download"
    drag_download.mkdir()
    app._load_local(str(drag_download))
    remote_dropped = next(
        entry for entry in app.remote_entries if entry.name == "dropped.txt"
    )
    app.remote_tree.selection_set(str(app.remote_entries.index(remote_dropped)))
    remote_drag = app._on_remote_drag_init(SimpleNamespace(y_root=0))
    assert remote_drag[1] == "DND_Text"
    assert remote_drag[2].startswith(REMOTE_DRAG_PREFIX)
    app._on_local_drop(SimpleNamespace(data=remote_drag[2]))
    pump(root, lambda: (drag_download / "dropped.txt").exists())
    pump(root, lambda: not app.transfer_active)
    assert (drag_download / "dropped.txt").read_text(encoding="utf-8") == "dropped upload"

    (server_root / "nested").mkdir()
    app._load_remote("/nested")
    pump(root, lambda: app.remote_path == "/nested")
    saved = app.settings.find_profile("smoke")
    assert saved is not None and saved.remote_path == "/nested"

    app._on_close()

    root2 = create_root()
    root2.withdraw()
    app2 = FTPMateApp(root2)
    assert app2.profile_var.get() == "smoke"
    assert app2.advanced["remote_path"] == "/nested"
    assert app2.local_path == str(drag_download)
    app2._connect()
    pump(root2, lambda: app2.connected)
    assert app2.remote_path == "/nested"
    app2._on_close()

    server.close_all()
    temp.cleanup()
    print("ui-smoke-ok")


if __name__ == "__main__":
    main()
