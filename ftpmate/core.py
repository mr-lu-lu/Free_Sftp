from __future__ import annotations

import ftplib
import os
import posixpath
import re
import socket
import ssl
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Protocol


ProgressCallback = Callable[[int, int], None]
CancelCallback = Callable[[], bool]


class FTPMateError(RuntimeError):
    pass


class TransferCancelled(FTPMateError):
    pass


@dataclass(slots=True)
class FTPConfig:
    host: str
    port: int = 21
    username: str = "anonymous"
    password: str = ""
    protocol: str = "ftp"
    tls: bool = False
    insecure_tls: bool = False
    key_file: str = ""
    key_passphrase: str = ""
    known_hosts: str = ""
    encoding: str = "utf-8"
    passive: bool = True
    timeout: int = 30


@dataclass(slots=True)
class RemoteEntry:
    name: str
    path: str
    is_dir: bool
    size: int = 0
    modified: datetime | None = None
    mode: str = ""


@dataclass(slots=True)
class RemoteFile:
    path: str
    size: int


class RemoteClient(Protocol):
    host_key_fingerprint: str | None

    @property
    def connected(self) -> bool: ...

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def pwd(self) -> str: ...

    def list_dir(self, path: str) -> list[RemoteEntry]: ...

    def file_size(self, path: str) -> int | None: ...

    def exists(self, path: str) -> bool: ...

    def ensure_remote_dir(self, path: str) -> None: ...

    def mkdir(self, path: str) -> None: ...

    def rename(self, old_path: str, new_path: str) -> None: ...

    def remove(self, path: str, is_dir: bool) -> None: ...

    def upload_file(
        self,
        local_path: str,
        remote_file: str,
        progress: ProgressCallback | None = None,
        cancel: CancelCallback | None = None,
        resume: bool = True,
    ) -> None: ...

    def download_file(
        self,
        remote_file: str,
        local_path: str,
        progress: ProgressCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> None: ...

    def remote_files(self, root: str) -> tuple[list[RemoteFile], list[str]]: ...

    def upload_tree(
        self,
        local_root: str,
        remote_root: str,
        progress: ProgressCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> None: ...

    def download_tree(
        self,
        remote_root: str,
        local_root: str,
        progress: ProgressCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> None: ...


MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

UNIX_LIST_RE = re.compile(
    r"^([bcdlps-])([rwxstST-]{9})\s+\d+\s+\S+\s+\S+\s+"
    r"(\d+)\s+([A-Za-z]{3})\s+(\d{1,2})\s+(\d{2}:\d{2}|\d{4})\s+(.+)$"
)


def remote_path(path: str) -> str:
    value = (path or "/").strip().replace("\\", "/")
    if not value:
        value = "/"
    if not value.startswith("/"):
        value = "/" + value
    normalized = posixpath.normpath(value)
    if normalized in ("", "."):
        return "/"
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized


def remote_join(base: str, name: str) -> str:
    return remote_path(posixpath.join(remote_path(base), name))


def remote_parent(path: str) -> str:
    current = remote_path(path)
    if current == "/":
        return "/"
    parent = posixpath.dirname(current.rstrip("/"))
    return remote_path(parent or "/")


def remote_name(path: str) -> str:
    current = remote_path(path).rstrip("/")
    return posixpath.basename(current) or "/"


def remote_parts(path: str) -> list[str]:
    return [part for part in remote_path(path).split("/") if part]


def safe_remote_name(name: str) -> str:
    if not name or name in (".", "..") or "\r" in name or "\n" in name:
        raise FTPMateError("文件名无效")
    return name


def human_size(size: int | None) -> str:
    if size is None:
        return ""
    value = float(max(0, int(size)))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return ""


def format_modified(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M")


def parse_ftp_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None


def parse_unix_list_line(line: str, base: str) -> RemoteEntry | None:
    match = UNIX_LIST_RE.match(line)
    if not match:
        return None
    kind, permissions, raw_size, month_text, day_text, time_text, raw_name = match.groups()
    name = raw_name.split(" -> ", 1)[0]
    if name in (".", ".."):
        return None
    month = MONTHS.get(month_text.lower())
    if month is None:
        return None
    now = datetime.now()
    modified = None
    try:
        day = int(day_text)
        if ":" in time_text:
            hour, minute = (int(part) for part in time_text.split(":", 1))
            modified = datetime(now.year, month, day, hour, minute)
            if modified > now:
                modified = modified.replace(year=now.year - 1)
        else:
            modified = datetime(int(time_text), month, day)
    except ValueError:
        modified = None
    return RemoteEntry(
        name=name,
        path=remote_join(base, name),
        is_dir=kind == "d",
        size=0 if kind == "d" else int(raw_size),
        modified=modified,
        mode=permissions,
    )


def collect_local_files(root: str) -> tuple[list[tuple[str, str, int]], list[str]]:
    root = os.path.abspath(root)
    if os.path.isfile(root):
        return [(root, os.path.basename(root), os.path.getsize(root))], []
    files: list[tuple[str, str, int]] = []
    directories: list[str] = []
    for current, _dir_names, file_names in os.walk(root, followlinks=False):
        relative = os.path.relpath(current, root)
        remote_relative = "" if relative == "." else relative.replace(os.sep, "/")
        directories.append(remote_relative)
        for file_name in file_names:
            local_path = os.path.join(current, file_name)
            if not os.path.isfile(local_path):
                continue
            remote_relative_file = posixpath.join(remote_relative, file_name)
            files.append((local_path, remote_relative_file, os.path.getsize(local_path)))
    return files, directories


class RemoteClientBase:
    def remote_files(self, root: str) -> tuple[list[RemoteFile], list[str]]:
        files: list[RemoteFile] = []
        directories: list[str] = []

        def walk(path: str) -> None:
            directories.append(path)
            for entry in self.list_dir(path):
                if entry.is_dir:
                    walk(entry.path)
                else:
                    files.append(RemoteFile(entry.path, entry.size))

        walk(remote_path(root))
        return files, directories

    def upload_tree(
        self,
        local_root: str,
        remote_root: str,
        progress: ProgressCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> None:
        local_root = os.path.abspath(local_root)
        target_root = remote_path(remote_root)
        files, directories = collect_local_files(local_root)
        for directory in directories:
            target = target_root if not directory else remote_join(target_root, directory)
            self.ensure_remote_dir(target)
        for local_file, relative, _ in files:
            if cancel and cancel():
                raise TransferCancelled("传输已取消")
            target = remote_join(target_root, relative)
            self.ensure_remote_dir(remote_parent(target))
            self.upload_file(local_file, target, progress=progress, cancel=cancel)

    def download_tree(
        self,
        remote_root: str,
        local_root: str,
        progress: ProgressCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> None:
        source_root = remote_path(remote_root)
        files, directories = self.remote_files(source_root)
        for directory in directories:
            relative = posixpath.relpath(directory, source_root)
            target = local_root if relative == "." else os.path.join(local_root, *relative.split("/"))
            os.makedirs(target, exist_ok=True)
        for item in files:
            if cancel and cancel():
                raise TransferCancelled("传输已取消")
            relative = posixpath.relpath(item.path, source_root)
            target = os.path.join(local_root, *relative.split("/"))
            self.download_file(item.path, target, progress=progress, cancel=cancel)

    def iter_remote_files(self, root: str) -> Iterable[RemoteFile]:
        files, _ = self.remote_files(root)
        return files


def create_client(config: FTPConfig) -> RemoteClient:
    if config.protocol.lower() == "sftp":
        from .sftp import SFTPClient

        return SFTPClient(config)
    return FTPClient(config)


class FTPClient(RemoteClientBase):
    def __init__(self, config: FTPConfig):
        self.config = config
        self.ftp: ftplib.FTP | None = None
        self.host_key_fingerprint: str | None = None
        self._lock = threading.RLock()

    @property
    def connected(self) -> bool:
        return self.ftp is not None and self.ftp.sock is not None

    def connect(self) -> None:
        with self._lock:
            self.disconnect()
            use_tls = self.config.tls or self.config.protocol.lower() == "ftps"
            context = None
            if use_tls:
                if self.config.insecure_tls:
                    context = ssl.create_default_context()
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                else:
                    context = ssl.create_default_context()
            if use_tls:
                client: ftplib.FTP = ftplib.FTP_TLS(
                    context=context,
                    timeout=self.config.timeout,
                    encoding=self.config.encoding,
                )
            else:
                client = ftplib.FTP(
                    timeout=self.config.timeout,
                    encoding=self.config.encoding,
                )
            client.connect(self.config.host, self.config.port)
            client.set_pasv(self.config.passive)
            client.login(self.config.username, self.config.password)
            if use_tls and isinstance(client, ftplib.FTP_TLS):
                client.prot_p()
            if client.sock is not None:
                client.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self.ftp = client

    def disconnect(self) -> None:
        with self._lock:
            if self.ftp is None:
                return
            try:
                self.ftp.quit()
            except Exception:
                try:
                    self.ftp.close()
                except Exception:
                    pass
            self.ftp = None

    def _ensure(self) -> ftplib.FTP:
        if not self.connected:
            self.connect()
        if self.ftp is None:
            raise FTPMateError("未连接到服务器")
        return self.ftp

    def pwd(self) -> str:
        with self._lock:
            try:
                return remote_path(self._ensure().pwd())
            except Exception as exc:
                raise FTPMateError(f"获取远程目录失败: {exc}") from exc

    def list_dir(self, path: str) -> list[RemoteEntry]:
        current = remote_path(path)
        with self._lock:
            ftp = self._ensure()
            entries: list[RemoteEntry] = []
            try:
                entries = self._list_mlsd(ftp, current)
            except Exception:
                try:
                    entries = self._list_unix(ftp, current)
                except Exception:
                    entries = self._list_nlst(ftp, current)
            return sorted(
                (entry for entry in entries if entry.name not in (".", "..")),
                key=lambda entry: (not entry.is_dir, entry.name.casefold()),
            )

    def _list_mlsd(self, ftp: ftplib.FTP, base: str) -> list[RemoteEntry]:
        entries: list[RemoteEntry] = []
        for name, facts in ftp.mlsd(base):
            kind = (facts.get("type") or "").lower()
            if kind in ("cdir", "pdir"):
                continue
            is_dir = kind == "dir"
            raw_size = facts.get("size") or 0
            try:
                size = int(raw_size)
            except (TypeError, ValueError):
                size = 0
            entries.append(
                RemoteEntry(
                    name=name,
                    path=remote_join(base, name),
                    is_dir=is_dir,
                    size=0 if is_dir else size,
                    modified=parse_ftp_timestamp(facts.get("modify")),
                    mode=facts.get("unix.mode", ""),
                )
            )
        return entries

    def _list_unix(self, ftp: ftplib.FTP, base: str) -> list[RemoteEntry]:
        ftp.cwd(base)
        lines: list[str] = []
        ftp.retrlines("LIST", lines.append)
        entries = [parse_unix_list_line(line, base) for line in lines]
        return [entry for entry in entries if entry is not None]

    def _list_nlst(self, ftp: ftplib.FTP, base: str) -> list[RemoteEntry]:
        ftp.cwd(base)
        entries: list[RemoteEntry] = []
        for name in ftp.nlst():
            if name in (".", ".."):
                continue
            is_dir = False
            size = 0
            try:
                size = int(ftp.size(name) or 0)
            except Exception:
                try:
                    ftp.cwd(name)
                    is_dir = True
                    ftp.cwd(base)
                except Exception:
                    ftp.cwd(base)
            entries.append(
                RemoteEntry(
                    name=name,
                    path=remote_join(base, name),
                    is_dir=is_dir,
                    size=0 if is_dir else size,
                )
            )
        return entries

    def file_size(self, path: str) -> int | None:
        with self._lock:
            ftp = self._ensure()
            target = remote_path(path)
            try:
                return int(ftp.size(target) or 0)
            except Exception:
                parent = remote_parent(target)
                name = remote_name(target)
                try:
                    for entry in self.list_dir(parent):
                        if entry.name == name and not entry.is_dir:
                            return entry.size
                except Exception:
                    pass
                return None

    def exists(self, path: str) -> bool:
        with self._lock:
            ftp = self._ensure()
            target = remote_path(path)
            try:
                ftp.size(target)
                return True
            except Exception:
                parent = remote_parent(target)
                name = remote_name(target)
                try:
                    if any(entry.name == name for entry in self.list_dir(parent)):
                        return True
                except Exception:
                    pass
                try:
                    old = ftp.pwd()
                    ftp.cwd(target)
                    ftp.cwd(old)
                    return True
                except Exception:
                    return False

    def ensure_remote_dir(self, path: str) -> None:
        with self._lock:
            ftp = self._ensure()
            current = "/"
            for part in remote_parts(path):
                current = remote_join(current, safe_remote_name(part))
                try:
                    ftp.cwd(current)
                except Exception:
                    try:
                        ftp.mkd(current)
                    except Exception as exc:
                        try:
                            ftp.cwd(current)
                        except Exception:
                            raise FTPMateError(f"无法创建远程目录 {current}: {exc}") from exc

    def mkdir(self, path: str) -> None:
        with self._lock:
            try:
                self._ensure().mkd(remote_path(path))
            except Exception as exc:
                raise FTPMateError(f"创建目录失败: {exc}") from exc

    def rename(self, old_path: str, new_path: str) -> None:
        with self._lock:
            try:
                self._ensure().rename(remote_path(old_path), remote_path(new_path))
            except Exception as exc:
                raise FTPMateError(f"重命名失败: {exc}") from exc

    def remove(self, path: str, is_dir: bool) -> None:
        with self._lock:
            ftp = self._ensure()
            target = remote_path(path)
            if not is_dir:
                try:
                    ftp.delete(target)
                except Exception as exc:
                    raise FTPMateError(f"删除文件失败: {exc}") from exc
                return
            for entry in self.list_dir(target):
                self.remove(entry.path, entry.is_dir)
            try:
                ftp.rmd(target)
            except Exception as exc:
                raise FTPMateError(f"删除目录失败: {exc}") from exc

    def upload_file(
        self,
        local_path: str,
        remote_file: str,
        progress: ProgressCallback | None = None,
        cancel: CancelCallback | None = None,
        resume: bool = True,
    ) -> None:
        target = remote_path(remote_file)
        total = os.path.getsize(local_path)
        with self._lock:
            ftp = self._ensure()
            remote_size = self.file_size(target)
            if remote_size is not None and remote_size == total:
                if progress:
                    progress(total, total)
                return
            offset = remote_size if resume and remote_size is not None and 0 < remote_size < total else 0

            def run(start_offset: int) -> None:
                sent = start_offset

                def callback(block: bytes) -> None:
                    nonlocal sent
                    if cancel and cancel():
                        raise TransferCancelled("传输已取消")
                    sent += len(block)
                    if progress:
                        progress(sent, total)

                with open(local_path, "rb") as handle:
                    handle.seek(start_offset)
                    ftp.storbinary(
                        f"STOR {target}",
                        handle,
                        blocksize=64 * 1024,
                        callback=callback,
                        rest=start_offset or None,
                    )

            try:
                run(offset)
            except TransferCancelled:
                raise
            except Exception:
                if offset == 0:
                    raise
                run(0)

    def download_file(
        self,
        remote_file: str,
        local_path: str,
        progress: ProgressCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> None:
        source = remote_path(remote_file)
        total = self.file_size(source) or 0
        local_path = os.path.abspath(local_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        part_path = local_path + ".part"
        downloaded = 0
        try:
            with self._lock:
                ftp = self._ensure()

                def callback(block: bytes) -> None:
                    nonlocal downloaded
                    if cancel and cancel():
                        raise TransferCancelled("传输已取消")
                    handle.write(block)
                    downloaded += len(block)
                    if progress:
                        progress(downloaded, total)

                with open(part_path, "wb") as handle:
                    ftp.retrbinary(
                        f"RETR {source}",
                        callback,
                        blocksize=64 * 1024,
                    )
            os.replace(part_path, local_path)
        except Exception:
            try:
                if os.path.exists(part_path):
                    os.remove(part_path)
            except OSError:
                pass
            raise

    def remote_files(self, root: str) -> tuple[list[RemoteFile], list[str]]:
        files: list[RemoteFile] = []
        directories: list[str] = []

        def walk(path: str) -> None:
            directories.append(path)
            for entry in self.list_dir(path):
                if entry.is_dir:
                    walk(entry.path)
                else:
                    files.append(RemoteFile(entry.path, entry.size))

        walk(remote_path(root))
        return files, directories

    def upload_tree(
        self,
        local_root: str,
        remote_root: str,
        progress: ProgressCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> None:
        local_root = os.path.abspath(local_root)
        target_root = remote_path(remote_root)
        files, directories = collect_local_files(local_root)
        for directory in directories:
            target = target_root if not directory else remote_join(target_root, directory)
            self.ensure_remote_dir(target)
        for local_file, relative, _ in files:
            if cancel and cancel():
                raise TransferCancelled("传输已取消")
            target = remote_join(target_root, relative)
            self.ensure_remote_dir(remote_parent(target))
            self.upload_file(local_file, target, progress=progress, cancel=cancel)

    def download_tree(
        self,
        remote_root: str,
        local_root: str,
        progress: ProgressCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> None:
        source_root = remote_path(remote_root)
        files, directories = self.remote_files(source_root)
        for directory in directories:
            relative = posixpath.relpath(directory, source_root)
            target = local_root if relative == "." else os.path.join(local_root, *relative.split("/"))
            os.makedirs(target, exist_ok=True)
        for item in files:
            if cancel and cancel():
                raise TransferCancelled("传输已取消")
            relative = posixpath.relpath(item.path, source_root)
            target = os.path.join(local_root, *relative.split("/"))
            self.download_file(item.path, target, progress=progress, cancel=cancel)

    def iter_remote_files(self, root: str) -> Iterable[RemoteFile]:
        files, _ = self.remote_files(root)
        return files
