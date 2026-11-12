from __future__ import annotations

import base64
import errno
import hashlib
import os
import socket
import stat
import threading
from datetime import datetime
from pathlib import Path

import paramiko
from paramiko.sftp import (
    CMD_ATTRS,
    CMD_CLOSE,
    CMD_HANDLE,
    CMD_LSTAT,
    CMD_NAME,
    CMD_OPENDIR,
    CMD_READDIR,
    CMD_REALPATH,
    CMD_STAT,
)
from paramiko.sftp_attr import SFTPAttributes

from .core import (
    CancelCallback,
    FTPConfig,
    FTPMateError,
    ProgressCallback,
    RemoteClientBase,
    RemoteEntry,
    TransferCancelled,
    remote_join,
    remote_name,
    remote_parent,
    remote_path,
    safe_remote_name,
)


def default_known_hosts_path() -> str:
    root = os.environ.get("APPDATA")
    base = Path(root) / "FTPMate" if root else Path.home() / ".ftpmate"
    return str(base / "known_hosts")


def host_key_fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


class SFTPClient(RemoteClientBase):
    def __init__(self, config: FTPConfig):
        self.config = config
        self.ssh: paramiko.SSHClient | None = None
        self.sftp: paramiko.SFTPClient | None = None
        self.host_key_fingerprint: str | None = None
        self._name_encoding = config.encoding or "utf-8"
        self._lock = threading.RLock()

    @property
    def connected(self) -> bool:
        if self.sftp is None or self.ssh is None:
            return False
        transport = self.ssh.get_transport()
        return transport is not None and transport.is_active()

    def connect(self) -> None:
        with self._lock:
            self.disconnect()
            self._name_encoding = self.config.encoding or "utf-8"
            ssh = paramiko.SSHClient()
            known_hosts = self.config.known_hosts or default_known_hosts_path()
            if not self.config.insecure_tls:
                try:
                    ssh.load_system_host_keys()
                except OSError:
                    pass
                if os.path.isfile(known_hosts):
                    try:
                        ssh.load_host_keys(known_hosts)
                    except OSError as exc:
                        raise FTPMateError(f"读取 SSH 主机密钥记录失败: {exc}") from exc
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            password = self.config.password or None
            use_key = bool(self.config.key_file.strip())
            kwargs = {
                "hostname": self.config.host,
                "port": self.config.port,
                "username": self.config.username,
                "password": password,
                "timeout": self.config.timeout,
                "banner_timeout": self.config.timeout,
                "auth_timeout": self.config.timeout,
                "allow_agent": not use_key and password is None,
                "look_for_keys": not use_key and password is None,
            }
            if use_key:
                kwargs["key_filename"] = self.config.key_file
                if self.config.key_passphrase:
                    kwargs["passphrase"] = self.config.key_passphrase

            try:
                ssh.connect(**kwargs)
                transport = ssh.get_transport()
                if transport is None:
                    raise paramiko.SSHException("SSH 传输通道未建立")
                self.host_key_fingerprint = host_key_fingerprint(transport.get_remote_server_key())
                sftp = ssh.open_sftp()
                sftp.get_channel().settimeout(self.config.timeout)
                if not self.config.insecure_tls:
                    try:
                        Path(known_hosts).parent.mkdir(parents=True, exist_ok=True)
                        ssh.save_host_keys(known_hosts)
                    except OSError:
                        pass
            except paramiko.BadHostKeyException as exc:
                ssh.close()
                raise FTPMateError(
                    "SSH 主机密钥与已保存记录不一致，连接已中止。"
                    f"如服务器确实更换过密钥，请删除 {known_hosts} 后重试。"
                ) from exc
            except paramiko.AuthenticationException as exc:
                ssh.close()
                raise FTPMateError("SSH 认证失败，请检查用户名、密码或私钥文件") from exc
            except paramiko.PasswordRequiredException as exc:
                ssh.close()
                raise FTPMateError("私钥已加密，请在“高级”中填写私钥口令") from exc
            except paramiko.SSHException as exc:
                ssh.close()
                raise FTPMateError(f"SSH 连接失败: {exc}") from exc
            except (socket.timeout, TimeoutError) as exc:
                ssh.close()
                raise FTPMateError("SSH 连接超时，请检查主机、端口和网络") from exc
            except OSError as exc:
                ssh.close()
                raise FTPMateError(f"无法连接 SSH 服务器: {exc}") from exc

            self.ssh = ssh
            self.sftp = sftp

    def disconnect(self) -> None:
        with self._lock:
            sftp, self.sftp = self.sftp, None
            ssh, self.ssh = self.ssh, None
            self.host_key_fingerprint = None
            if sftp is not None:
                try:
                    sftp.close()
                except Exception:
                    pass
            if ssh is not None:
                try:
                    ssh.close()
                except Exception:
                    pass

    def _ensure(self) -> paramiko.SFTPClient:
        if not self.connected:
            self.connect()
        if self.sftp is None:
            raise FTPMateError("未连接到 SFTP 服务器")
        return self.sftp

    def _decode_name(self, raw: bytes) -> str:
        candidates = ["utf-8", self._name_encoding, "gb18030", "gbk", "latin-1"]
        seen: set[str] = set()
        for encoding in candidates:
            key = encoding.lower()
            if not encoding or key in seen:
                continue
            seen.add(key)
            try:
                value = raw.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
            if encoding.lower() not in ("utf-8", "utf8") and any(ord(char) > 127 for char in value):
                self._name_encoding = encoding
            return value
        return raw.decode("utf-8", errors="replace")

    def _encode_path(self, path: str) -> bytes:
        try:
            return path.encode(self._name_encoding or "utf-8")
        except (UnicodeEncodeError, LookupError):
            return path.encode("utf-8", errors="replace")

    def _stat_path(self, path: str, follow: bool = True) -> SFTPAttributes:
        sftp = self._ensure()
        command = CMD_STAT if follow else CMD_LSTAT
        t, msg = sftp._request(command, self._encode_path(remote_path(path)))
        if t != CMD_ATTRS:
            raise FTPMateError("SFTP 服务器返回了无效的文件属性")
        return SFTPAttributes._from_msg(msg)

    def _realpath(self, path: str) -> str:
        sftp = self._ensure()
        t, msg = sftp._request(CMD_REALPATH, self._encode_path(path))
        if t != CMD_NAME:
            raise FTPMateError("SFTP 服务器无法解析远程路径")
        count = msg.get_int()
        if count != 1:
            raise FTPMateError("SFTP 服务器返回了无效的路径")
        return self._decode_name(msg.get_string())

    def _listdir_attr(self, path: str) -> list[SFTPAttributes]:
        sftp = self._ensure()
        t, msg = sftp._request(CMD_OPENDIR, self._encode_path(path))
        if t != CMD_HANDLE:
            raise FTPMateError("SFTP 服务器无法打开远程目录")
        handle = msg.get_binary()
        result: list[SFTPAttributes] = []
        try:
            while True:
                try:
                    t, msg = sftp._request(CMD_READDIR, handle)
                except EOFError:
                    break
                if t != CMD_NAME:
                    raise FTPMateError("SFTP 服务器返回了无效的目录内容")
                count = msg.get_int()
                for _ in range(count):
                    name = self._decode_name(msg.get_string())
                    longname = self._decode_name(msg.get_string())
                    attribute = SFTPAttributes._from_msg(msg, name, longname)
                    if name not in (".", ".."):
                        result.append(attribute)
        finally:
            try:
                sftp._request(CMD_CLOSE, handle)
            except Exception:
                pass
        return result

    def pwd(self) -> str:
        with self._lock:
            try:
                return remote_path(self._realpath("."))
            except Exception as exc:
                raise FTPMateError(f"获取远程目录失败: {exc}") from exc

    def list_dir(self, path: str) -> list[RemoteEntry]:
        current = remote_path(path)
        with self._lock:
            try:
                attributes = self._listdir_attr(current)
            except FileNotFoundError as exc:
                raise FTPMateError(f"远程目录不存在: {current}") from exc
            except OSError as exc:
                raise FTPMateError(f"读取远程目录失败: {exc}") from exc

            entries: list[RemoteEntry] = []
            for attribute in attributes:
                name = attribute.filename
                if name in (".", ".."):
                    continue
                item_path = remote_join(current, name)
                mode = int(attribute.st_mode or 0)
                is_dir = stat.S_ISDIR(mode)
                size = int(attribute.st_size or 0)
                if stat.S_ISLNK(mode):
                    try:
                        target = self._stat_path(item_path, follow=True)
                        is_dir = stat.S_ISDIR(int(target.st_mode or 0))
                        size = int(target.st_size or 0)
                        mode = int(target.st_mode or 0)
                    except OSError:
                        pass
                modified = None
                if attribute.st_mtime:
                    try:
                        modified = datetime.fromtimestamp(attribute.st_mtime)
                    except (OSError, OverflowError, ValueError):
                        modified = None
                entries.append(
                    RemoteEntry(
                        name=name,
                        path=item_path,
                        is_dir=is_dir,
                        size=0 if is_dir else size,
                        modified=modified,
                        mode=stat.filemode(mode) if mode else "",
                    )
                )
            return sorted(entries, key=lambda entry: (not entry.is_dir, entry.name.casefold()))

    def file_size(self, path: str) -> int | None:
        with self._lock:
            target = remote_path(path)
            try:
                attribute = self._stat_path(target, follow=True)
                if stat.S_ISDIR(int(attribute.st_mode or 0)):
                    return None
                return int(attribute.st_size or 0)
            except OSError:
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
            target = remote_path(path)
            try:
                self._stat_path(target, follow=True)
                return True
            except OSError:
                parent = remote_parent(target)
                name = remote_name(target)
                try:
                    return any(entry.name == name for entry in self.list_dir(parent))
                except Exception:
                    return False

    def ensure_remote_dir(self, path: str) -> None:
        with self._lock:
            current = "/"
            for part in remote_path(path).split("/"):
                if not part:
                    continue
                current = remote_join(current, safe_remote_name(part))
                try:
                    attribute = self._stat_path(current, follow=True)
                    if not stat.S_ISDIR(int(attribute.st_mode or 0)):
                        raise FTPMateError(f"远程路径已存在且不是目录: {current}")
                except FileNotFoundError:
                    try:
                        self._ensure().mkdir(self._encode_path(current))
                    except OSError as exc:
                        if getattr(exc, "errno", None) != errno.EEXIST:
                            raise FTPMateError(f"无法创建远程目录 {current}: {exc}") from exc
                except OSError as exc:
                    if getattr(exc, "errno", None) == errno.ENOENT:
                        try:
                            self._ensure().mkdir(self._encode_path(current))
                        except OSError as mkdir_exc:
                            if getattr(mkdir_exc, "errno", None) != errno.EEXIST:
                                raise FTPMateError(
                                    f"无法创建远程目录 {current}: {mkdir_exc}"
                                ) from mkdir_exc
                    else:
                        raise FTPMateError(f"无法访问远程目录 {current}: {exc}") from exc

    def mkdir(self, path: str) -> None:
        with self._lock:
            try:
                self._ensure().mkdir(self._encode_path(remote_path(path)))
            except Exception as exc:
                raise FTPMateError(f"创建目录失败: {exc}") from exc

    def rename(self, old_path: str, new_path: str) -> None:
        with self._lock:
            try:
                self._ensure().rename(
                    self._encode_path(remote_path(old_path)),
                    self._encode_path(remote_path(new_path)),
                )
            except Exception as exc:
                raise FTPMateError(f"重命名失败: {exc}") from exc

    def remove(self, path: str, is_dir: bool) -> None:
        with self._lock:
            sftp = self._ensure()
            target = remote_path(path)
            try:
                attribute = self._stat_path(target, follow=False)
                if stat.S_ISLNK(int(attribute.st_mode or 0)):
                    sftp.remove(self._encode_path(target))
                    return
            except OSError:
                pass
            if not is_dir:
                try:
                    sftp.remove(self._encode_path(target))
                except Exception as exc:
                    raise FTPMateError(f"删除文件失败: {exc}") from exc
                return
            for entry in self.list_dir(target):
                self.remove(entry.path, entry.is_dir)
            try:
                sftp.rmdir(self._encode_path(target))
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
        try:
            with self._lock:
                sftp = self._ensure()
                parent = remote_parent(target)
                if parent != "/":
                    self.ensure_remote_dir(parent)
                remote_size = self.file_size(target)
                if remote_size is not None and remote_size == total:
                    if progress:
                        progress(total, total)
                    return
                offset = remote_size if resume and remote_size is not None and 0 < remote_size < total else 0

                def run(start_offset: int) -> None:
                    sent = start_offset
                    mode = "r+b" if start_offset else "wb"
                    with sftp.open(self._encode_path(target), mode) as remote_handle:
                        if start_offset:
                            remote_handle.seek(start_offset)
                        with open(local_path, "rb") as local_handle:
                            local_handle.seek(start_offset)
                            while True:
                                if cancel and cancel():
                                    raise TransferCancelled("传输已取消")
                                block = local_handle.read(64 * 1024)
                                if not block:
                                    break
                                remote_handle.write(block)
                                sent += len(block)
                                if progress:
                                    progress(sent, total)

                try:
                    run(offset)
                except TransferCancelled:
                    raise
                except Exception:
                    if offset == 0:
                        raise
                    run(0)
        except TransferCancelled:
            raise
        except FTPMateError:
            raise
        except Exception as exc:
            raise FTPMateError(f"上传失败: {exc}") from exc

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
                sftp = self._ensure()
                with sftp.open(self._encode_path(source), "rb") as remote_handle, open(
                    part_path, "wb"
                ) as local_handle:
                    while True:
                        if cancel and cancel():
                            raise TransferCancelled("传输已取消")
                        block = remote_handle.read(64 * 1024)
                        if not block:
                            break
                        local_handle.write(block)
                        downloaded += len(block)
                        if progress:
                            progress(downloaded, total)
            os.replace(part_path, local_path)
        except Exception:
            try:
                if os.path.exists(part_path):
                    os.remove(part_path)
            except OSError:
                pass
            raise
