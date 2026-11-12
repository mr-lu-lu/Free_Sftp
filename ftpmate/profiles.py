from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from pathlib import Path


APP_NAME = "FTPMate"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _make_blob(data: bytes):
    buffer = ctypes.create_string_buffer(data)
    blob = DATA_BLOB(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return blob, buffer


def protect_secret(value: str) -> str:
    if not value:
        return ""
    raw = value.encode("utf-8")
    if sys.platform != "win32":
        return "b64:" + base64.b64encode(raw).decode("ascii")
    in_blob, in_buffer = _make_blob(raw)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "FTPMate",
        None,
        None,
        None,
        1,
        ctypes.byref(out_blob),
    ):
        raise ctypes.WinError()
    try:
        protected = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return "dpapi:" + base64.b64encode(protected).decode("ascii")
    finally:
        kernel32.LocalFree(out_blob.pbData)
        del in_buffer


def unprotect_secret(value: str) -> str:
    if not value:
        return ""
    if value.startswith("b64:"):
        return base64.b64decode(value[4:]).decode("utf-8")
    if not value.startswith("dpapi:"):
        return value
    if sys.platform != "win32":
        return ""
    protected = base64.b64decode(value[6:])
    in_blob, in_buffer = _make_blob(protected)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        1,
        ctypes.byref(out_blob),
    ):
        raise ctypes.WinError()
    try:
        raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return raw.decode("utf-8")
    finally:
        kernel32.LocalFree(out_blob.pbData)
        del in_buffer


@dataclass
class Profile:
    name: str
    host: str = ""
    port: int = 21
    username: str = "anonymous"
    password: str = ""
    password_enc: str = ""
    protocol: str = "ftp"
    tls: bool = False
    insecure_tls: bool = False
    key_file: str = ""
    key_passphrase: str = ""
    key_passphrase_enc: str = ""
    encoding: str = "utf-8"
    passive: bool = True
    timeout: int = 30
    remote_path: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        encrypted = self.password_enc
        if self.password:
            encrypted = protect_secret(self.password)
        encrypted_passphrase = self.key_passphrase_enc
        if self.key_passphrase:
            encrypted_passphrase = protect_secret(self.key_passphrase)
        data["password"] = ""
        data["password_enc"] = encrypted
        data["key_passphrase"] = ""
        data["key_passphrase_enc"] = encrypted_passphrase
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        encrypted = str(data.get("password_enc", ""))
        try:
            password = unprotect_secret(encrypted)
        except Exception:
            password = ""
        passphrase_enc = str(data.get("key_passphrase_enc", ""))
        try:
            key_passphrase = unprotect_secret(passphrase_enc)
        except Exception:
            key_passphrase = ""
        protocol = str(data.get("protocol", "") or "").lower()
        if protocol not in ("ftp", "ftps", "sftp"):
            protocol = "ftps" if bool(data.get("tls", False)) else "ftp"
        return cls(
            name=str(data.get("name", "")),
            host=str(data.get("host", "")),
            port=int(data.get("port", 21) or 21),
            username=str(data.get("username", "anonymous")),
            password=password,
            password_enc=encrypted,
            protocol=protocol,
            tls=protocol == "ftps",
            insecure_tls=bool(data.get("insecure_tls", False)),
            key_file=str(data.get("key_file", "")),
            key_passphrase=key_passphrase,
            key_passphrase_enc=passphrase_enc,
            encoding=str(data.get("encoding", "utf-8") or "utf-8"),
            passive=bool(data.get("passive", True)),
            timeout=int(data.get("timeout", 30) or 30),
            remote_path=str(data.get("remote_path", "")),
        )


@dataclass
class Settings:
    profiles: list[Profile] = field(default_factory=list)
    last_profile: str = ""
    theme: str = "dark"
    last_local_path: str = ""
    last_remote_path: str = ""
    window_geometry: str = ""

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "profiles": [profile.to_dict() for profile in self.profiles],
            "last_profile": self.last_profile,
            "theme": self.theme,
            "last_local_path": self.last_local_path,
            "last_remote_path": self.last_remote_path,
            "window_geometry": self.window_geometry,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        profiles = []
        for item in data.get("profiles", []):
            if isinstance(item, dict) and item.get("name"):
                profiles.append(Profile.from_dict(item))
        return cls(
            profiles=profiles,
            last_profile=str(data.get("last_profile", "")),
            theme=str(data.get("theme", "dark") or "dark"),
            last_local_path=str(data.get("last_local_path", "")),
            last_remote_path=str(data.get("last_remote_path", "")),
            window_geometry=str(data.get("window_geometry", "")),
        )

    def find_profile(self, name: str) -> Profile | None:
        for profile in self.profiles:
            if profile.name == name:
                return profile
        return None

    def upsert_profile(self, profile: Profile) -> None:
        for index, existing in enumerate(self.profiles):
            if existing.name == profile.name:
                self.profiles[index] = profile
                return
        self.profiles.append(profile)

    def delete_profile(self, name: str) -> None:
        self.profiles = [profile for profile in self.profiles if profile.name != name]
        if self.last_profile == name:
            self.last_profile = ""


def settings_path() -> Path:
    root = os.environ.get("APPDATA")
    if root:
        return Path(root) / APP_NAME / "settings.json"
    return Path.home() / f".{APP_NAME.lower()}" / "settings.json"


def load_settings() -> Settings:
    path = settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return Settings.from_dict(data)
    except (OSError, ValueError, TypeError):
        pass
    return Settings()


def save_settings(settings: Settings) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(settings.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)
