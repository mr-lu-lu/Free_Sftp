from __future__ import annotations

import json
import os
import posixpath
import queue
import shutil
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

try:
    import sv_ttk
except ImportError:
    sv_ttk = None

try:
    from tkinterdnd2 import COPY as DND_COPY, DND_FILES, DND_TEXT
except ImportError:
    DND_COPY = None
    DND_FILES = None
    DND_TEXT = None

from .core import (
    FTPConfig,
    FTPMateError,
    RemoteClient,
    RemoteEntry,
    TransferCancelled,
    collect_local_files,
    create_client,
    format_modified,
    human_size,
    remote_join,
    remote_name,
    remote_parent,
    remote_path,
)
from .profiles import Profile, Settings, load_settings, save_settings


REMOTE_DRAG_PREFIX = "FTPMATE_REMOTE:"


def get_work_area(root: tk.Misc) -> tuple[int, int, int, int]:
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def get_window_frame(root: tk.Misc) -> tuple[int, int, int, int]:
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)
        rect = wintypes.RECT()
        if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return (
                root.winfo_rootx() - rect.left,
                root.winfo_rooty() - rect.top,
                rect.right - root.winfo_rootx() - root.winfo_width(),
                rect.bottom - root.winfo_rooty() - root.winfo_height(),
            )
    except Exception:
        pass
    return 8, 31, 8, 8


@dataclass(slots=True)
class LocalEntry:
    name: str
    path: str
    is_dir: bool
    size: int = 0
    modified: datetime | None = None


def read_local_dir(path: str) -> list[LocalEntry]:
    entries: list[LocalEntry] = []
    with os.scandir(path) as iterator:
        for item in iterator:
            try:
                is_dir = item.is_dir(follow_symlinks=False)
                stat = item.stat(follow_symlinks=False)
                entries.append(
                    LocalEntry(
                        name=item.name,
                        path=item.path,
                        is_dir=is_dir,
                        size=0 if is_dir else stat.st_size,
                        modified=datetime.fromtimestamp(stat.st_mtime),
                    )
                )
            except OSError:
                continue
    return sorted(entries, key=lambda entry: (not entry.is_dir, entry.name.casefold()))


class AdvancedDialog(tk.Toplevel):
    def __init__(self, parent: "FTPMateApp", values: dict, protocol: str = "ftp"):
        super().__init__(parent.root)
        self.parent = parent
        self.protocol = protocol
        self.result: dict | None = None
        self.title("高级连接设置")
        self.resizable(False, False)
        self.transient(parent.root)
        self.encoding_var = tk.StringVar(value=values.get("encoding", "utf-8"))
        self.timeout_var = tk.StringVar(value=str(values.get("timeout", 30)))
        self.remote_path_var = tk.StringVar(value=values.get("remote_path", ""))
        self.passive_var = tk.BooleanVar(value=bool(values.get("passive", True)))
        self.insecure_var = tk.BooleanVar(value=bool(values.get("insecure_tls", False)))
        self.key_file_var = tk.StringVar(value=str(values.get("key_file", "")))
        self.key_passphrase_var = tk.StringVar(value=str(values.get("key_passphrase", "")))
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.grab_set()
        self.wait_visibility()
        self._center()

    def _build(self) -> None:
        frame = ttk.Frame(self, padding=18)
        frame.grid(sticky="nsew")
        ttk.Label(
            frame,
            text="远程文件名编码" if self.protocol == "sftp" else "远程编码",
        ).grid(row=0, column=0, sticky="w", pady=5)
        encoding = ttk.Combobox(
            frame,
            textvariable=self.encoding_var,
            values=("utf-8", "gb18030", "gbk", "latin-1"),
            state="readonly",
            width=22,
        )
        encoding.grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="超时（秒）").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Spinbox(
            frame,
            from_=5,
            to=300,
            textvariable=self.timeout_var,
            width=10,
        ).grid(row=1, column=1, sticky="w", pady=5)
        ttk.Label(frame, text="登录后目录").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.remote_path_var, width=30).grid(
            row=2, column=1, sticky="ew", pady=5
        )
        ttk.Label(frame, text="私钥文件（SFTP，可选）").grid(
            row=3, column=0, sticky="w", pady=5
        )
        key_row = ttk.Frame(frame)
        key_row.grid(row=3, column=1, sticky="ew", pady=5)
        key_row.columnconfigure(0, weight=1)
        key_entry = ttk.Entry(key_row, textvariable=self.key_file_var)
        key_entry.grid(row=0, column=0, sticky="ew")
        key_button = ttk.Button(key_row, text="浏览", command=self._browse_key_file, width=7)
        key_button.grid(row=0, column=1, padx=(6, 0))
        ttk.Label(frame, text="私钥口令（可选）").grid(
            row=4, column=0, sticky="w", pady=5
        )
        passphrase_entry = ttk.Entry(frame, textvariable=self.key_passphrase_var, show="*")
        passphrase_entry.grid(row=4, column=1, sticky="ew", pady=5)
        passive_check = ttk.Checkbutton(
            frame,
            text="被动模式（推荐，FTP/FTPS）",
            variable=self.passive_var,
        )
        passive_check.grid(row=5, column=0, columnspan=2, sticky="w", pady=3)
        insecure_text = (
            "忽略 SSH 主机密钥校验（仅内网调试）"
            if self.protocol == "sftp"
            else "FTPS 忽略证书校验（仅内网调试）"
        )
        ttk.Checkbutton(
            frame,
            text=insecure_text,
            variable=self.insecure_var,
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=3)
        if self.protocol != "sftp":
            key_entry.state(["disabled"])
            key_button.state(["disabled"])
            passphrase_entry.state(["disabled"])
        else:
            passive_check.state(["disabled"])
        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        delete_button = ttk.Button(
            buttons,
            text="删除当前配置",
            command=self._delete_profile,
        )
        delete_button.pack(side="left")
        if not self.parent.profile_var.get() or self.parent.profile_var.get() == "快速连接":
            delete_button.state(["disabled"])
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(
            buttons,
            text="确定",
            style="Accent.TButton",
            command=self._accept,
        ).pack(side="right")
        frame.columnconfigure(1, weight=1)

    def _browse_key_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 SSH 私钥文件",
            parent=self,
            filetypes=(
                ("私钥文件", "*.pem *.key id_rsa id_ed25519 id_ecdsa"),
                ("所有文件", "*.*"),
            ),
        )
        if path:
            self.key_file_var.set(path)

    def _accept(self) -> None:
        try:
            timeout = int(self.timeout_var.get())
        except ValueError:
            messagebox.showerror("设置错误", "超时必须是数字", parent=self)
            return
        if timeout < 5 or timeout > 300:
            messagebox.showerror("设置错误", "超时范围是 5 到 300 秒", parent=self)
            return
        self.result = {
            "encoding": self.encoding_var.get().strip() or "utf-8",
            "timeout": timeout,
            "remote_path": self.remote_path_var.get().strip(),
            "passive": self.passive_var.get(),
            "insecure_tls": self.insecure_var.get(),
            "key_file": self.key_file_var.get().strip(),
            "key_passphrase": self.key_passphrase_var.get(),
        }
        self.destroy()

    def _delete_profile(self) -> None:
        name = self.parent.profile_var.get()
        if not name or name == "快速连接":
            return
        if not messagebox.askyesno("删除配置", f"确定删除连接配置“{name}”吗？", parent=self):
            return
        self.parent.settings.delete_profile(name)
        save_settings(self.parent.settings)
        self.parent._refresh_profile_values("")
        self.parent._log(f"已删除连接配置：{name}")
        self.destroy()

    def _center(self) -> None:
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        x = self.parent.root.winfo_rootx() + (self.parent.root.winfo_width() - width) // 2
        y = self.parent.root.winfo_rooty() + (self.parent.root.winfo_height() - height) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")


class FTPMateApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings: Settings = load_settings()
        self.client: RemoteClient | None = None
        self.connected = False
        self.connecting = False
        self.transfer_active = False
        self.cancel_event = threading.Event()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.tasks: queue.Queue[object] = queue.Queue()
        self.local_entries: list[LocalEntry] = []
        self.remote_entries: list[RemoteEntry] = []
        self.local_path = self._initial_local_path()
        self.remote_path = "/"
        self.remote_home = "/"
        self.connected_profile_name = ""
        self._drop_targets: list[tk.Misc] = []
        self._local_drop_targets: list[tk.Misc] = []
        self.local_drag_enabled = False
        self.remote_drag_enabled = False
        self.advanced = {
            "encoding": "utf-8",
            "timeout": 30,
            "remote_path": "",
            "passive": True,
            "insecure_tls": False,
            "key_file": "",
            "key_passphrase": "",
        }
        self.local_context: tk.Menu | None = None
        self.remote_context: tk.Menu | None = None
        self._build()
        self._apply_theme()
        self._load_startup_profile()
        self._load_local(self.local_path)
        self._update_action_states()
        threading.Thread(target=self._task_worker, daemon=True).start()
        self.root.after(60, self._poll_events)

    def _initial_local_path(self) -> str:
        candidate = self.settings.last_local_path
        if candidate and os.path.isdir(candidate):
            return candidate
        return str(Path.home())

    def _setup_window_geometry(self) -> None:
        left, top, right, bottom = get_work_area(self.root)
        work_width = max(800, right - left)
        work_height = max(600, bottom - top)
        saved = self.settings.window_geometry
        if saved:
            try:
                self.root.geometry(saved)
            except tk.TclError:
                saved = None
        self.root.update_idletasks()
        frame_left, frame_top, frame_right, frame_bottom = get_window_frame(self.root)
        max_width = max(640, work_width - frame_left - frame_right)
        max_height = max(480, work_height - frame_top - frame_bottom)
        min_width = min(1180, max_width)
        min_height = min(700, max_height)
        self.root.minsize(min_width, min_height)

        if not saved:
            width = min(1380, max_width - 24)
            height = min(900, max_height - 24)
            outer_width = width + frame_left + frame_right
            outer_height = height + frame_top + frame_bottom
            x = left + (work_width - outer_width) // 2
            y = top + (work_height - outer_height) // 2
            self.root.geometry(f"{width}x{height}+{x}+{y}")
            return

        width = min(max(self.root.winfo_width(), min_width), max_width)
        height = min(max(self.root.winfo_height(), min_height), max_height)
        outer_width = width + frame_left + frame_right
        outer_height = height + frame_top + frame_bottom
        x = min(max(self.root.winfo_x(), left), right - outer_width)
        y = min(max(self.root.winfo_y(), top), bottom - outer_height)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _build(self) -> None:
        self.root.title("FTP Mate - Windows FTP 客户端")
        self._setup_window_geometry()
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<F5>", lambda _event: self._refresh_all())
        self.root.bind("<Control-u>", lambda _event: self._upload_selected())
        self.root.bind("<Control-d>", lambda _event: self._download_selected())
        self.root.bind("<Control-Return>", lambda _event: self._connect())

        outer = ttk.Frame(self.root, padding=(14, 12, 14, 10))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        self._build_header(outer)
        self._build_connection(outer)
        self._build_files(outer)
        self._build_transfer(outer)

        self.bottom_status = ttk.Label(outer, text="就绪", anchor="w")
        self.bottom_status.grid(row=4, column=0, sticky="ew", pady=(8, 0))

    def _build_header(self, parent: ttk.Frame) -> None:
        header = ttk.Frame(parent)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(1, weight=1)
        title = ttk.Label(header, text="FTP Mate", style="Title.TLabel")
        title.grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="Windows 与 Linux 服务器之间的双栏文件传输",
            foreground="#7b8494",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        status_area = ttk.Frame(header)
        status_area.grid(row=0, column=2, rowspan=2, sticky="e")
        self.status_dot = ttk.Label(status_area, text="●", foreground="#d9534f")
        self.status_dot.pack(side="left", padx=(0, 6))
        self.status_var = tk.StringVar(value="未连接")
        ttk.Label(status_area, textvariable=self.status_var).pack(side="left", padx=(0, 14))
        self.theme_button = ttk.Button(
            status_area,
            text="切换主题",
            command=self._toggle_theme,
        )
        self.theme_button.pack(side="left", padx=(0, 6))
        ttk.Button(status_area, text="使用帮助", command=self._show_help).pack(side="left")

    def _build_connection(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="服务器连接", padding=(12, 8, 12, 10))
        box.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        for column in (1, 3, 4):
            box.columnconfigure(column, weight=1)

        labels = ("连接配置", "主机", "端口", "用户名", "密码", "协议")
        for column, text in enumerate(labels):
            ttk.Label(box, text=text).grid(row=0, column=column, sticky="w", padx=(0, 8), pady=(0, 4))

        self.profile_var = tk.StringVar(value="快速连接")
        self.profile_combo = ttk.Combobox(
            box,
            textvariable=self.profile_var,
            state="readonly",
            width=16,
        )
        self.profile_combo.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_profile_selected)

        self.host_var = tk.StringVar()
        self.port_var = tk.StringVar(value="22")
        self.user_var = tk.StringVar()
        self.password_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.host_var).grid(
            row=1, column=1, sticky="ew", padx=(0, 8)
        )
        ttk.Entry(box, textvariable=self.port_var, width=7).grid(
            row=1, column=2, sticky="ew", padx=(0, 8)
        )
        ttk.Entry(box, textvariable=self.user_var).grid(
            row=1, column=3, sticky="ew", padx=(0, 8)
        )
        ttk.Entry(box, textvariable=self.password_var, show="*").grid(
            row=1, column=4, sticky="ew", padx=(0, 8)
        )
        self.protocol_var = tk.StringVar(value="SFTP")
        protocol_combo = ttk.Combobox(
            box,
            textvariable=self.protocol_var,
            values=("SFTP", "FTP", "FTPS (显式)"),
            state="readonly",
            width=13,
        )
        protocol_combo.grid(row=1, column=5, sticky="ew", padx=(0, 8))
        protocol_combo.bind("<<ComboboxSelected>>", self._on_protocol_changed)

        self.connect_button = ttk.Button(
            box,
            text="连接",
            style="Accent.TButton",
            command=self._connect,
            width=8,
        )
        self.connect_button.grid(row=1, column=6, padx=(0, 6))
        self.disconnect_button = ttk.Button(
            box,
            text="断开",
            command=self._disconnect,
            width=7,
        )
        self.disconnect_button.grid(row=1, column=7, padx=(0, 6))
        ttk.Button(
            box,
            text="保存配置",
            command=self._save_profile,
            width=10,
        ).grid(row=1, column=8, padx=(0, 6))
        ttk.Button(
            box,
            text="高级",
            command=self._show_advanced,
            width=7,
        ).grid(row=1, column=9)
        self._refresh_profile_values(self.settings.last_profile)

    def _build_files(self, parent: ttk.Frame) -> None:
        content = ttk.Frame(parent)
        content.grid(row=2, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.columnconfigure(2, weight=1)
        content.rowconfigure(0, weight=1)
        self._build_local_pane(content)
        self._build_actions(content)
        self._build_remote_pane(content)

    def _build_local_pane(self, parent: ttk.Frame) -> None:
        pane = ttk.LabelFrame(parent, text="本机文件", padding=8)
        pane.grid(row=0, column=0, sticky="nsew")
        pane.columnconfigure(0, weight=1)
        pane.rowconfigure(2, weight=1)
        toolbar = ttk.Frame(pane)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 7))
        ttk.Button(toolbar, text="上级", command=self._local_up, width=6).pack(side="left")
        ttk.Button(toolbar, text="刷新", command=lambda: self._load_local(self.local_path), width=6).pack(
            side="left", padx=(5, 0)
        )
        ttk.Button(toolbar, text="主目录", command=lambda: self._load_local(str(Path.home())), width=7).pack(
            side="left", padx=(5, 0)
        )
        ttk.Button(toolbar, text="浏览", command=self._browse_local, width=6).pack(
            side="left", padx=(5, 0)
        )
        ttk.Label(toolbar, text="双击文件夹进入，双击文件上传", foreground="#7b8494").pack(
            side="right"
        )
        self.local_path_var = tk.StringVar(value=self.local_path)
        local_path_entry = ttk.Entry(pane, textvariable=self.local_path_var)
        local_path_entry.grid(row=1, column=0, sticky="ew", pady=(0, 7))
        local_path_entry.bind("<Return>", lambda _event: self._local_path_entered())
        ttk.Button(pane, text="转到", command=self._local_path_entered, width=6).grid(
            row=1, column=1, padx=(6, 0), pady=(0, 7)
        )
        columns = ("name", "size", "modified", "kind")
        self.local_tree = ttk.Treeview(
            pane,
            columns=columns,
            show="headings",
            selectmode="extended",
        )
        self.local_tree.heading("name", text="名称")
        self.local_tree.heading("size", text="大小")
        self.local_tree.heading("modified", text="修改时间")
        self.local_tree.heading("kind", text="类型")
        self.local_tree.column("name", width=175, anchor="w", minwidth=140)
        self.local_tree.column("size", width=70, anchor="e", stretch=False)
        self.local_tree.column("modified", width=150, anchor="center", stretch=False)
        self.local_tree.column("kind", width=70, anchor="center", stretch=False)
        self.local_tree.grid(row=2, column=0, sticky="nsew")
        local_scroll = ttk.Scrollbar(pane, orient="vertical", command=self.local_tree.yview)
        local_scroll.grid(row=2, column=1, sticky="ns", padx=(4, 0))
        self.local_tree.configure(yscrollcommand=local_scroll.set)
        self.local_tree.bind("<Double-1>", self._local_double_click)
        self.local_tree.bind("<<TreeviewSelect>>", self._on_local_select)
        self.local_tree.bind("<Button-3>", self._show_local_context)
        self.local_tree.bind("<Control-a>", lambda _event: self._select_all(self.local_tree))
        if DND_FILES and hasattr(self.local_tree, "drag_source_register"):
            try:
                self.local_tree.drag_source_register(1, DND_FILES)
                self.local_tree.dnd_bind("<<DragInitCmd>>", self._on_local_drag_init)
                self.local_tree.dnd_bind("<<DragEndCmd>>", self._on_local_drag_end)
                self.local_drag_enabled = True
            except tk.TclError:
                pass
        self.local_status_var = tk.StringVar(value="")
        ttk.Label(pane, textvariable=self.local_status_var, foreground="#7b8494").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )
        self.local_pane = pane
        self._register_local_drop_targets(pane)
        self._make_context_menus()

    def _build_actions(self, parent: ttk.Frame) -> None:
        actions = ttk.Frame(parent, padding=(12, 0, 12, 0))
        actions.grid(row=0, column=1, sticky="ns")
        actions.rowconfigure(0, weight=1)
        actions.rowconfigure(4, weight=1)
        self.upload_button = ttk.Button(
            actions,
            text="上传到服务器  →",
            style="Accent.TButton",
            command=self._upload_selected,
            width=16,
        )
        self.upload_button.grid(row=1, column=0, pady=(0, 8))
        self.download_button = ttk.Button(
            actions,
            text="←  下载到本机",
            command=self._download_selected,
            width=16,
        )
        self.download_button.grid(row=2, column=0, pady=(0, 8))
        ttk.Label(
            actions,
            text="也可把文件\n直接拖到右侧",
            justify="center",
            foreground="#7b8494",
        ).grid(row=3, column=0)

    def _build_remote_pane(self, parent: ttk.Frame) -> None:
        pane = ttk.LabelFrame(parent, text="远程 Linux 服务器", padding=8)
        pane.grid(row=0, column=2, sticky="nsew")
        pane.columnconfigure(0, weight=1)
        pane.rowconfigure(2, weight=1)
        toolbar = ttk.Frame(pane)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 7))
        ttk.Button(toolbar, text="上级", command=self._remote_up, width=6).pack(side="left")
        ttk.Button(toolbar, text="刷新", command=self._refresh_remote, width=6).pack(
            side="left", padx=(5, 0)
        )
        ttk.Button(toolbar, text="主目录", command=self._remote_home_click, width=7).pack(
            side="left", padx=(5, 0)
        )
        ttk.Button(toolbar, text="根目录", command=lambda: self._load_remote("/"), width=7).pack(
            side="left", padx=(5, 0)
        )
        ttk.Button(toolbar, text="新建文件夹", command=self._remote_mkdir, width=11).pack(
            side="left", padx=(5, 0)
        )
        ttk.Label(toolbar, text="双击进入/下载 · 可拖入上传，也可拖文件到左侧下载", foreground="#7b8494").pack(
            side="right"
        )
        self.remote_path_var = tk.StringVar(value="/")
        self.remote_path_entry = ttk.Entry(pane, textvariable=self.remote_path_var)
        self.remote_path_entry.grid(row=1, column=0, sticky="ew", pady=(0, 7))
        self.remote_path_entry.bind("<Return>", lambda _event: self._remote_path_entered())
        ttk.Button(pane, text="转到", command=self._remote_path_entered, width=6).grid(
            row=1, column=1, padx=(6, 0), pady=(0, 7)
        )
        columns = ("name", "size", "modified", "mode")
        self.remote_tree = ttk.Treeview(
            pane,
            columns=columns,
            show="headings",
            selectmode="extended",
        )
        self.remote_tree.heading("name", text="名称")
        self.remote_tree.heading("size", text="大小")
        self.remote_tree.heading("modified", text="修改时间")
        self.remote_tree.heading("mode", text="权限")
        self.remote_tree.column("name", width=175, anchor="w", minwidth=140)
        self.remote_tree.column("size", width=70, anchor="e", stretch=False)
        self.remote_tree.column("modified", width=150, anchor="center", stretch=False)
        self.remote_tree.column("mode", width=70, anchor="center", stretch=False)
        self.remote_tree.grid(row=2, column=0, sticky="nsew")
        remote_scroll = ttk.Scrollbar(pane, orient="vertical", command=self.remote_tree.yview)
        remote_scroll.grid(row=2, column=1, sticky="ns", padx=(4, 0))
        self.remote_tree.configure(yscrollcommand=remote_scroll.set)
        self.remote_tree.bind("<Double-1>", self._remote_double_click)
        self.remote_tree.bind("<<TreeviewSelect>>", self._on_remote_select)
        self.remote_tree.bind("<Button-3>", self._show_remote_context)
        self.remote_tree.bind("<Delete>", lambda _event: self._delete_remote())
        self.remote_tree.bind("<F2>", lambda _event: self._rename_remote())
        self.remote_tree.bind("<Control-a>", lambda _event: self._select_all(self.remote_tree))
        if DND_TEXT and hasattr(self.remote_tree, "drag_source_register"):
            try:
                self.remote_tree.drag_source_register(1, DND_TEXT)
                self.remote_tree.dnd_bind("<<DragInitCmd>>", self._on_remote_drag_init)
                self.remote_tree.dnd_bind("<<DragEndCmd>>", self._on_remote_drag_end)
                self.remote_drag_enabled = True
            except tk.TclError:
                pass
        self.remote_pane = pane
        self._register_drop_targets(pane)
        self.remote_status_var = tk.StringVar(value="未连接")
        ttk.Label(pane, textvariable=self.remote_status_var, foreground="#7b8494").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )

    def _build_transfer(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="传输", padding=8)
        box.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        box.columnconfigure(1, weight=1)
        self.transfer_label_var = tk.StringVar(value="没有正在进行的传输")
        ttk.Label(box, textvariable=self.transfer_label_var, width=44, anchor="w").grid(
            row=0, column=0, sticky="w"
        )
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(
            box,
            variable=self.progress_var,
            maximum=100,
            mode="determinate",
        )
        self.progress_bar.grid(row=0, column=1, sticky="ew", padx=10)
        self.percent_var = tk.StringVar(value="0%")
        ttk.Label(box, textvariable=self.percent_var, width=7, anchor="e").grid(row=0, column=2)
        self.speed_var = tk.StringVar(value="")
        ttk.Label(box, textvariable=self.speed_var, width=18, anchor="e").grid(
            row=0, column=3, padx=(8, 0)
        )
        self.cancel_button = ttk.Button(
            box,
            text="取消传输",
            command=self._cancel_transfer,
            width=9,
        )
        self.cancel_button.grid(row=0, column=4, padx=(8, 0))
        self.cancel_button.state(["disabled"])
        self.log_text = ScrolledText(box, height=5, wrap="word", borderwidth=0)
        self.log_text.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(8, 0))
        self.log_text.configure(state="disabled")

    def _make_context_menus(self) -> None:
        self.local_context = tk.Menu(self.root, tearoff=False)
        self.local_context.add_command(label="上传到当前远程目录", command=self._upload_selected)
        self.local_context.add_command(label="在资源管理器中打开", command=self._open_local_in_explorer)
        self.local_context.add_separator()
        self.local_context.add_command(label="刷新", command=lambda: self._load_local(self.local_path))
        self.remote_context = tk.Menu(self.root, tearoff=False)
        self.remote_context.add_command(label="下载到当前本机目录", command=self._download_selected)
        self.remote_context.add_command(label="重命名", command=self._rename_remote)
        self.remote_context.add_command(label="删除", command=self._delete_remote)
        self.remote_context.add_separator()
        self.remote_context.add_command(label="新建文件夹", command=self._remote_mkdir)
        self.remote_context.add_command(label="刷新", command=self._refresh_remote)

    def _refresh_profile_values(self, selected: str = "") -> None:
        names = [profile.name for profile in self.settings.profiles]
        self.profile_combo["values"] = ["快速连接"] + names
        if selected and selected in names:
            self.profile_var.set(selected)
        elif self.profile_var.get() not in names:
            self.profile_var.set("快速连接")

    def _load_startup_profile(self) -> None:
        name = self.settings.last_profile
        if not name and self.settings.profiles:
            name = self.settings.profiles[0].name
        profile = self.settings.find_profile(name) if name else None
        if profile:
            self._apply_profile(profile)
            self._log(f"已载入连接配置：{profile.name}")
        else:
            self.status_var.set("未连接 · 请填写服务器信息")
            self._refresh_profile_values("")

    def _selected_protocol(self) -> str:
        value = self.protocol_var.get()
        if value == "SFTP":
            return "sftp"
        if value.startswith("FTPS"):
            return "ftps"
        return "ftp"

    def _on_protocol_changed(self, _event=None) -> None:
        protocol = self._selected_protocol()
        if self.port_var.get().strip() in ("", "21", "22"):
            self.port_var.set("22" if protocol == "sftp" else "21")
        if protocol in ("ftp", "ftps") and not self.user_var.get().strip():
            self.user_var.set("anonymous")

    def _apply_profile(self, profile: Profile) -> None:
        self.profile_var.set(profile.name)
        self.host_var.set(profile.host)
        self.port_var.set(str(profile.port))
        self.user_var.set(profile.username)
        self.password_var.set(profile.password)
        protocol = profile.protocol if profile.protocol in ("ftp", "ftps", "sftp") else "ftp"
        self.protocol_var.set({"sftp": "SFTP", "ftps": "FTPS (显式)", "ftp": "FTP"}[protocol])
        self.advanced = {
            "encoding": profile.encoding,
            "timeout": profile.timeout,
            "remote_path": profile.remote_path,
            "passive": profile.passive,
            "insecure_tls": profile.insecure_tls,
            "key_file": profile.key_file,
            "key_passphrase": profile.key_passphrase,
        }
        self.remote_path_var.set(profile.remote_path or "/")

    def _on_profile_selected(self, _event=None) -> None:
        name = self.profile_var.get()
        if name == "快速连接":
            return
        profile = self.settings.find_profile(name)
        if profile:
            self._apply_profile(profile)
            self.settings.last_profile = name
            self.status_var.set("配置已载入 · 点击连接")
            self._log(f"已载入连接配置：{name}")

    def _current_profile(self, name: str) -> Profile:
        host = self.host_var.get().strip()
        if not host:
            raise ValueError("请填写服务器主机名或 IP")
        protocol = self._selected_protocol()
        try:
            port = int(self.port_var.get().strip() or ("22" if protocol == "sftp" else "21"))
        except ValueError as exc:
            raise ValueError("端口必须是数字") from exc
        if port < 1 or port > 65535:
            raise ValueError("端口范围是 1 到 65535")
        try:
            timeout = int(self.advanced.get("timeout", 30))
        except (TypeError, ValueError):
            timeout = 30
        username = self.user_var.get().strip()
        if protocol == "sftp" and not username:
            raise ValueError("请输入 SSH 用户名")
        if not username:
            username = "anonymous"
        remote_start = str(self.advanced.get("remote_path", "") or "").strip()
        typed_path = self.remote_path_var.get().strip()
        if not self.connected and typed_path and typed_path != "/":
            remote_start = typed_path
        return Profile(
            name=name,
            host=host,
            port=port,
            username=username,
            password=self.password_var.get(),
            protocol=protocol,
            tls=protocol == "ftps",
            insecure_tls=bool(self.advanced.get("insecure_tls", False)),
            key_file=str(self.advanced.get("key_file", "") or ""),
            key_passphrase=str(self.advanced.get("key_passphrase", "") or ""),
            encoding=str(self.advanced.get("encoding", "utf-8") or "utf-8"),
            passive=bool(self.advanced.get("passive", True)),
            timeout=timeout,
            remote_path=remote_start,
        )

    def _save_profile(self) -> None:
        current = self.profile_var.get()
        default_name = current if current and current != "快速连接" else self.host_var.get().strip()
        name = simpledialog.askstring(
            "保存连接配置",
            "给这个服务器起个名字：",
            initialvalue=default_name,
            parent=self.root,
        )
        if name is None:
            return
        name = name.strip()
        if not name or name == "快速连接":
            messagebox.showerror("保存失败", "配置名称不能为空，也不能叫“快速连接”", parent=self.root)
            return
        try:
            profile = self._current_profile(name)
        except ValueError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return
        self.settings.upsert_profile(profile)
        self.settings.last_profile = name
        try:
            save_settings(self.settings)
        except OSError as exc:
            messagebox.showerror("保存失败", f"无法写入设置文件：{exc}", parent=self.root)
            return
        self._refresh_profile_values(name)
        self._log(f"已保存连接配置：{name}")
        self.status_var.set(f"已保存配置：{name}")

    def _show_advanced(self) -> None:
        dialog = AdvancedDialog(self, self.advanced, self._selected_protocol())
        self.root.wait_window(dialog)
        if dialog.result:
            self.advanced.update(dialog.result)
            self.remote_path_var.set(str(self.advanced.get("remote_path", "") or "/"))
            self._log("高级连接设置已更新")

    def _show_help(self) -> None:
        messagebox.showinfo(
            "使用帮助",
            "1. 选择 SFTP 或 FTP/FTPS，填写主机、端口、用户名和密码后点“连接”。\n"
            "2. 左侧是本机文件，右侧是 Linux 服务器目录。\n"
            "3. 双击文件夹进入，双击文件上传或下载。\n"
            "4. 左右两侧可双向拖拽：左侧文件拖到右侧上传，右侧文件拖到左侧下载。\n"
            "5. 也可以把 Windows 资源管理器中的文件直接拖到右侧远程目录。\n"
            "6. F5 刷新，Ctrl+U 上传，Ctrl+D 下载。\n"
            "7. “高级”里可设置私钥、编码、超时、被动模式和主机校验。\n"
            "8. SFTP 默认端口 22；FTP/FTPS 默认端口 21。",
            parent=self.root,
        )

    def _toggle_theme(self) -> None:
        self.settings.theme = "light" if self.settings.theme == "dark" else "dark"
        self._apply_theme()

    def _apply_theme(self) -> None:
        theme = self.settings.theme if self.settings.theme in ("dark", "light") else "dark"
        if sv_ttk is not None:
            try:
                sv_ttk.set_theme(theme)
            except Exception:
                pass
        else:
            try:
                ttk.Style().theme_use("clam")
            except tk.TclError:
                pass
        style = ttk.Style()
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Treeview", rowheight=28)
        for tree in (getattr(self, "local_tree", None), getattr(self, "remote_tree", None)):
            if tree is None:
                continue
            tree.tag_configure("dir", foreground="#58a6ff" if theme == "dark" else "#0067c0")
            tree.tag_configure("file", foreground="#d6dae2" if theme == "dark" else "#202124")
        if hasattr(self, "log_text"):
            if theme == "dark":
                self.log_text.configure(
                    bg="#1b1f27",
                    fg="#c9d1d9",
                    insertbackground="#ffffff",
                )
            else:
                self.log_text.configure(
                    bg="#f7f8fa",
                    fg="#202124",
                    insertbackground="#000000",
                )
            self.log_text.tag_configure("error", foreground="#ff7b72")
            self.log_text.tag_configure("ok", foreground="#3fb950")
            self.log_text.tag_configure("muted", foreground="#8b949e")
        if hasattr(self, "theme_button"):
            self.theme_button.configure(text="浅色模式" if theme == "dark" else "深色模式")

    def _connect(self) -> None:
        if self.connecting:
            return
        profile_name = self.profile_var.get()
        try:
            profile = self._current_profile(profile_name or "快速连接")
        except ValueError as exc:
            messagebox.showerror("连接失败", str(exc), parent=self.root)
            return
        config = FTPConfig(
            host=profile.host,
            port=profile.port,
            username=profile.username,
            password=profile.password,
            protocol=profile.protocol,
            tls=profile.tls,
            insecure_tls=profile.insecure_tls,
            key_file=profile.key_file,
            key_passphrase=profile.key_passphrase,
            encoding=profile.encoding,
            passive=profile.passive,
            timeout=profile.timeout,
        )
        self.connecting = True
        self.status_var.set(f"正在连接 {profile.host}:{profile.port} …")
        self.status_dot.configure(foreground="#d6a300")
        self.bottom_status.configure(
            text="正在建立 SSH/SFTP 连接…" if profile.protocol == "sftp" else "正在建立 FTP 连接…"
        )
        self._update_action_states()
        client = create_client(config)

        def task() -> None:
            try:
                client.connect()
                home = client.pwd()
                start = remote_path(profile.remote_path) if profile.remote_path else home
                if profile.remote_path and not profile.remote_path.startswith("/"):
                    start = remote_join(home, profile.remote_path)
                entries = client.list_dir(start)
                self._post(
                    "connected",
                    {
                        "client": client,
                        "home": home,
                        "start": start,
                        "entries": entries,
                        "profile": profile,
                    },
                )
            except Exception as exc:
                client.disconnect()
                self._post("connect_failed", str(exc))

        self._submit("连接服务器", task)

    def _disconnect(self) -> None:
        if self.transfer_active:
            messagebox.showinfo("正在传输", "请先取消当前传输，再断开连接", parent=self.root)
            return
        client = self.client
        self.client = None
        self.connected = False
        self.connecting = False
        self.remote_entries = []
        self._clear_tree(self.remote_tree)
        self.remote_home = "/"
        self.remote_path = "/"
        self.connected_profile_name = ""
        self.remote_path_var.set("/")
        self.remote_status_var.set("未连接")
        self.status_dot.configure(foreground="#d9534f")
        self.status_var.set("未连接")
        self.bottom_status.configure(text="已断开")
        self._update_action_states()
        if client is not None:
            self._submit("断开连接", client.disconnect)
            self._log("已断开服务器连接")

    def _handle_connected(self, payload: dict) -> None:
        self.connecting = False
        self.connected = True
        self.client = payload["client"]
        self.remote_home = payload["home"]
        self.remote_path = payload["start"]
        profile: Profile = payload["profile"]
        self.connected_profile_name = (
            profile.name if self.settings.find_profile(profile.name) else ""
        )
        self.status_dot.configure(foreground="#2ea043")
        protocol = "SFTP" if profile.protocol == "sftp" else ("FTPS" if profile.tls else "FTP")
        self.status_var.set(f"已连接 · {profile.username}@{profile.host}:{profile.port} · {protocol}")
        self.bottom_status.configure(text=f"连接成功，远程目录 {self.remote_path}")
        if self.settings.find_profile(profile.name):
            self.settings.last_profile = profile.name
        self._populate_remote(self.remote_path, payload["entries"])
        self._log(f"已连接 {profile.host}:{profile.port}，远程目录 {self.remote_path}", "ok")
        fingerprint = getattr(self.client, "host_key_fingerprint", None)
        if fingerprint:
            self._log(f"SSH 主机密钥指纹：{fingerprint}", "muted")
        self._update_action_states()

    def _handle_connect_failed(self, error: str) -> None:
        self.connecting = False
        self.connected = False
        self.connected_profile_name = ""
        self.status_dot.configure(foreground="#d9534f")
        self.status_var.set("连接失败")
        self.bottom_status.configure(text="连接失败")
        self._update_action_states()
        self._log(f"连接失败：{error}", "error")
        messagebox.showerror("连接失败", error, parent=self.root)

    def _submit(self, label: str, func, show_error: bool = True) -> None:
        def runner() -> None:
            try:
                func()
            except TransferCancelled:
                self._post("transfer_cancelled", "传输已取消")
            except Exception as exc:
                if show_error:
                    self._post("task_error", (label, str(exc)))
                else:
                    self._post("task_warning", (label, str(exc)))

        self.tasks.put(runner)

    def _task_worker(self) -> None:
        while True:
            task = self.tasks.get()
            if task is None:
                return
            try:
                task()
            except Exception as exc:
                self._post("task_error", ("后台任务", str(exc)))

    def _post(self, kind: str, payload: object = None) -> None:
        self.events.put((kind, payload))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        self.root.after(60, self._poll_events)

    def _handle_event(self, kind: str, payload: object) -> None:
        if kind == "connected":
            self._handle_connected(payload)
        elif kind == "connect_failed":
            self._handle_connect_failed(str(payload))
        elif kind == "local_entries":
            path, entries = payload
            self._populate_local(path, entries)
        elif kind == "remote_entries":
            path, entries = payload
            self._populate_remote(path, entries)
        elif kind == "remote_load_failed":
            path, error = payload
            self.remote_status_var.set(f"读取失败：{path}")
            self._log(f"读取远程目录失败 {path}：{error}", "error")
        elif kind == "task_warning":
            label, error = payload
            self.bottom_status.configure(text=f"{label}失败")
            self._log(f"{label}失败：{error}", "error")
            if self.transfer_active:
                self._finish_transfer_state()
        elif kind == "task_error":
            label, error = payload
            self._log(f"{label}失败：{error}", "error")
            self.bottom_status.configure(text=f"{label}失败")
            if self.transfer_active:
                self._finish_transfer_state()
            messagebox.showerror("操作失败", f"{label}失败：\n{error}", parent=self.root)
        elif kind == "progress":
            self._handle_progress(payload)
        elif kind == "transfer_file":
            self._handle_transfer_file(payload)
        elif kind == "transfer_done":
            self._handle_transfer_done(payload)
        elif kind == "transfer_cancelled":
            self._handle_transfer_cancelled(str(payload))
        elif kind == "ask_overwrite":
            self._handle_ask_overwrite(payload)
        elif kind == "refresh_local":
            self._load_local(self.local_path)
        elif kind == "refresh_remote":
            self._refresh_remote()
        elif kind == "log":
            message, tag = payload
            self._log(message, tag)

    def _log(self, message: str, tag: str | None = None) -> None:
        if not hasattr(self, "log_text"):
            return
        self.log_text.configure(state="normal")
        prefix = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{prefix}] {message}\n", (tag,) if tag else ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_tree(self, tree: ttk.Treeview) -> None:
        children = tree.get_children()
        if children:
            tree.delete(*children)

    def _enable(self, widget: ttk.Widget, enabled: bool) -> None:
        widget.state(["!disabled"] if enabled else ["disabled"])

    def _update_action_states(self) -> None:
        has_local = bool(self.local_tree.selection()) if hasattr(self, "local_tree") else False
        has_remote = bool(self.remote_tree.selection()) if hasattr(self, "remote_tree") else False
        can_upload = self.connected and has_local and not self.transfer_active
        can_download = self.connected and has_remote and not self.transfer_active
        if hasattr(self, "upload_button"):
            self._enable(self.upload_button, can_upload)
            self._enable(self.download_button, can_download)
            self._enable(self.connect_button, not self.connected and not self.connecting)
            self._enable(self.disconnect_button, self.connected and not self.connecting)
            self._enable(self.cancel_button, self.transfer_active)

    def _load_local(self, path: str) -> None:
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(path):
            messagebox.showerror("打开失败", f"目录不存在：\n{path}", parent=self.root)
            return
        try:
            entries = read_local_dir(path)
        except OSError as exc:
            messagebox.showerror("读取失败", f"无法读取目录：\n{exc}", parent=self.root)
            return
        self.local_path = path
        self.local_path_var.set(path)
        self._populate_local(path, entries)

    def _populate_local(self, path: str, entries: list[LocalEntry]) -> None:
        self.local_entries = entries
        self._clear_tree(self.local_tree)
        for index, entry in enumerate(entries):
            if entry.is_dir:
                kind = "文件夹"
                size = ""
            else:
                extension = os.path.splitext(entry.name)[1].lstrip(".").upper()
                kind = extension[:8] if extension else "文件"
                size = human_size(entry.size)
            self.local_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(entry.name, size, format_modified(entry.modified), kind),
                tags=("dir",) if entry.is_dir else ("file",),
            )
        self._update_local_status()
        self._remember_local_path()
        self._update_action_states()

    def _remember_local_path(self) -> None:
        if not self.local_path:
            return
        self.settings.last_local_path = self.local_path
        try:
            save_settings(self.settings)
        except OSError:
            pass

    def _update_local_status(self) -> None:
        selected = len(self.local_tree.selection())
        try:
            free = shutil.disk_usage(self.local_path).free
            free_text = f" · 可用 {human_size(free)}"
        except OSError:
            free_text = ""
        self.local_status_var.set(
            f"{len(self.local_entries)} 项" + (f" · 已选 {selected}" if selected else "") + free_text
        )

    def _populate_remote(self, path: str, entries: list[RemoteEntry]) -> None:
        self.remote_path = remote_path(path)
        self.remote_path_var.set(self.remote_path)
        self.remote_entries = entries
        self._clear_tree(self.remote_tree)
        for index, entry in enumerate(entries):
            self.remote_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    entry.name,
                    human_size(entry.size) if not entry.is_dir else "",
                    format_modified(entry.modified),
                    entry.mode,
                ),
                tags=("dir",) if entry.is_dir else ("file",),
            )
        selected = len(self.remote_tree.selection())
        self.remote_status_var.set(
            f"{len(entries)} 项" + (f" · 已选 {selected}" if selected else "") + f" · {self.remote_path}"
        )
        self.bottom_status.configure(text=f"远程目录：{self.remote_path}")
        self._remember_remote_path()
        self._update_action_states()

    def _remember_remote_path(self, persist: bool = True) -> None:
        if not self.remote_path:
            return
        self.settings.last_remote_path = self.remote_path
        self.advanced["remote_path"] = self.remote_path
        if self.connected_profile_name:
            profile = self.settings.find_profile(self.connected_profile_name)
            if profile is not None:
                profile.remote_path = self.remote_path
        if persist:
            try:
                save_settings(self.settings)
            except OSError:
                pass

    def _load_remote(self, path: str) -> None:
        if not self.connected or self.client is None:
            messagebox.showinfo("未连接", "请先连接服务器", parent=self.root)
            return
        target = remote_path(path)
        self.remote_path_var.set(target)
        self.remote_status_var.set(f"正在读取 {target} …")
        client = self.client

        def task() -> None:
            try:
                entries = client.list_dir(target)
                self._post("remote_entries", (target, entries))
            except Exception as exc:
                self._post("remote_load_failed", (target, str(exc)))

        self._submit("读取远程目录", task)

    def _refresh_remote(self) -> None:
        if self.connected:
            self._load_remote(self.remote_path)

    def _refresh_all(self) -> None:
        self._load_local(self.local_path)
        self._refresh_remote()

    def _local_up(self) -> None:
        parent = os.path.dirname(self.local_path.rstrip("\\/"))
        if parent and parent != self.local_path:
            self._load_local(parent)

    def _remote_up(self) -> None:
        if self.connected:
            self._load_remote(remote_parent(self.remote_path))

    def _remote_home_click(self) -> None:
        if self.connected:
            self._load_remote(self.remote_home)

    def _browse_local(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.local_path, parent=self.root)
        if selected:
            self._load_local(selected)

    def _local_path_entered(self) -> None:
        value = self.local_path_var.get().strip()
        if value:
            self._load_local(value)

    def _remote_path_entered(self) -> None:
        if not self.connected:
            return
        value = self.remote_path_var.get().strip()
        if value:
            target = value if value.startswith("/") else remote_join(self.remote_path, value)
            self._load_remote(target)

    def _selected_local_entries(self) -> list[LocalEntry]:
        result = []
        for item in self.local_tree.selection():
            try:
                result.append(self.local_entries[int(item)])
            except (ValueError, IndexError):
                continue
        return result

    def _selected_remote_entries(self) -> list[RemoteEntry]:
        result = []
        for item in self.remote_tree.selection():
            try:
                result.append(self.remote_entries[int(item)])
            except (ValueError, IndexError):
                continue
        return result

    def _local_double_click(self, event) -> None:
        row = self.local_tree.identify_row(event.y)
        if not row:
            return
        self.local_tree.selection_set(row)
        try:
            entry = self.local_entries[int(row)]
        except (ValueError, IndexError):
            return
        if entry.is_dir:
            self._load_local(entry.path)
        else:
            self._upload_paths([entry.path])

    def _remote_double_click(self, event) -> None:
        row = self.remote_tree.identify_row(event.y)
        if not row:
            return
        self.remote_tree.selection_set(row)
        try:
            entry = self.remote_entries[int(row)]
        except (ValueError, IndexError):
            return
        if entry.is_dir:
            self._load_remote(entry.path)
        elif self.connected:
            self._download_entries([entry])

    def _show_local_context(self, event) -> None:
        row = self.local_tree.identify_row(event.y)
        if row:
            if row not in self.local_tree.selection():
                self.local_tree.selection_set(row)
        if self.local_context is not None:
            try:
                self.local_context.tk_popup(event.x_root, event.y_root)
            finally:
                self.local_context.grab_release()

    def _show_remote_context(self, event) -> None:
        row = self.remote_tree.identify_row(event.y)
        if row:
            if row not in self.remote_tree.selection():
                self.remote_tree.selection_set(row)
        if self.remote_context is not None:
            try:
                self.remote_context.tk_popup(event.x_root, event.y_root)
            finally:
                self.remote_context.grab_release()

    def _open_local_in_explorer(self) -> None:
        try:
            os.startfile(self.local_path)
        except OSError as exc:
            messagebox.showerror("打开失败", str(exc), parent=self.root)

    def _select_all(self, tree: ttk.Treeview) -> str:
        children = tree.get_children()
        if children:
            tree.selection_set(children)
        return "break"

    def _on_local_select(self, _event=None) -> None:
        self._update_local_status()
        self._update_action_states()

    def _on_remote_select(self, _event=None) -> None:
        if hasattr(self, "remote_status_var"):
            selected = len(self.remote_tree.selection())
            self.remote_status_var.set(
                f"{len(self.remote_entries)} 项"
                + (f" · 已选 {selected}" if selected else "")
                + f" · {self.remote_path}"
            )
        self._update_action_states()

    def _require_transfer_ready(self) -> bool:
        if not self.connected or self.client is None:
            messagebox.showinfo("未连接", "请先连接服务器", parent=self.root)
            return False
        if self.transfer_active:
            messagebox.showinfo("正在传输", "请等待当前传输完成，或先取消当前传输", parent=self.root)
            return False
        return True

    def _begin_transfer(self, label: str) -> None:
        self.transfer_active = True
        self.cancel_event.clear()
        self.progress_var.set(0)
        self.percent_var.set("0%")
        self.speed_var.set("")
        self.transfer_label_var.set(label)
        self.cancel_button.configure(text="取消传输")
        self._update_action_states()

    def _finish_transfer_state(self) -> None:
        self.transfer_active = False
        self.cancel_button.configure(text="取消传输")
        self._enable(self.cancel_button, False)
        self._update_action_states()

    def _upload_selected(self) -> None:
        if not self._require_transfer_ready():
            return
        entries = self._selected_local_entries()
        if not entries:
            messagebox.showinfo("没有选择文件", "请先在左侧选择要上传的文件或文件夹", parent=self.root)
            return
        self._upload_paths([entry.path for entry in entries])

    def _upload_paths(self, paths: list[str]) -> None:
        if not self._require_transfer_ready():
            return
        existing = [os.path.abspath(path) for path in paths if os.path.exists(path)]
        if not existing:
            messagebox.showerror("上传失败", "没有可上传的文件或文件夹", parent=self.root)
            return
        client = self.client
        remote_dir = self.remote_path
        self._begin_transfer("正在准备上传…")
        self._log(f"准备上传 {len(existing)} 个项目到 {remote_dir}")

        def task() -> None:
            self._run_upload_batch(client, existing, remote_dir)

        self._submit("上传", task)

    def _run_upload_batch(self, client: RemoteClient, paths: list[str], remote_dir: str) -> None:
        files: list[tuple[str, str, int]] = []
        directories: list[str] = []
        for source in paths:
            source = os.path.abspath(source)
            name = os.path.basename(source.rstrip("\\/"))
            target = remote_join(remote_dir, name)
            if os.path.isdir(source):
                local_files, local_dirs = collect_local_files(source)
                directories.append(target)
                for relative in local_dirs:
                    if relative:
                        directories.append(remote_join(target, relative))
                for local_file, relative, size in local_files:
                    files.append((local_file, remote_join(target, relative), size))
            elif os.path.isfile(source):
                files.append((source, target, os.path.getsize(source)))
        directories = sorted(set(directories), key=lambda value: len(value))
        if not files and not directories:
            self._post("transfer_done", {
                "direction": "upload",
                "uploaded": 0,
                "skipped": 0,
                "failed": 0,
                "bytes": 0,
            })
            return
        self._post("transfer_file", {
            "name": "创建远程目录",
            "index": 0,
            "count": max(1, len(files)),
            "direction": "upload",
        })
        for directory in directories:
            if self.cancel_event.is_set():
                raise TransferCancelled("传输已取消")
            client.ensure_remote_dir(directory)

        remote_sizes: dict[str, int | None] = {}
        for _local_file, remote_file, _size in files:
            remote_sizes[remote_file] = client.file_size(remote_file)
        conflicts = [
            remote_file
            for _local_file, remote_file, _size in files
            if remote_sizes.get(remote_file) is not None
        ]
        skip_paths: set[str] = set()
        if conflicts:
            decision = self._ask_overwrite(conflicts)
            if decision == "cancel":
                self._post("transfer_cancelled", "已取消上传")
                return
            if decision == "skip":
                skip_paths = set(conflicts)

        uploaded = 0
        skipped = 0
        failed = 0
        total_bytes = 0
        consecutive_failures = 0
        for index, (local_file, remote_file, size) in enumerate(files, start=1):
            if self.cancel_event.is_set():
                raise TransferCancelled("传输已取消")
            if remote_file in skip_paths:
                skipped += 1
                continue
            self._post("transfer_file", {
                "name": os.path.basename(local_file),
                "index": index,
                "count": len(files),
                "direction": "upload",
            })
            try:
                callback = self._progress_callback(local_file, size)
                client.upload_file(local_file, remote_file, progress=callback, cancel=self.cancel_event.is_set)
                uploaded += 1
                total_bytes += size
                consecutive_failures = 0
                self._post("log", (f"已上传 {local_file} → {remote_file}", "ok"))
            except TransferCancelled:
                raise
            except Exception as exc:
                failed += 1
                consecutive_failures += 1
                self._post("log", (f"上传失败 {local_file}：{exc}", "error"))
                if consecutive_failures >= 3:
                    raise FTPMateError(f"连续上传失败，已停止：{exc}") from exc
        self._post("transfer_done", {
            "direction": "upload",
            "uploaded": uploaded,
            "skipped": skipped,
            "failed": failed,
            "bytes": total_bytes,
        })

    def _download_selected(self) -> None:
        if not self._require_transfer_ready():
            return
        entries = self._selected_remote_entries()
        if not entries:
            messagebox.showinfo("没有选择文件", "请先在右侧选择要下载的文件或文件夹", parent=self.root)
            return
        self._download_entries(entries)

    def _download_entries(self, entries: list[RemoteEntry]) -> None:
        if not self._require_transfer_ready():
            return
        client = self.client
        local_dir = self.local_path
        self._begin_transfer("正在准备下载…")
        self._log(f"准备下载 {len(entries)} 个项目到 {local_dir}")

        def task() -> None:
            self._run_download_batch(client, entries, local_dir)

        self._submit("下载", task)

    def _run_download_batch(self, client: RemoteClient, entries: list[RemoteEntry], local_dir: str) -> None:
        files: list[tuple[str, str, int]] = []
        directories: list[str] = []
        for entry in entries:
            target = os.path.join(local_dir, entry.name)
            if entry.is_dir:
                remote_files, remote_directories = client.remote_files(entry.path)
                directories.append(target)
                for directory in remote_directories:
                    relative = posixpath.relpath(directory, entry.path)
                    if relative != ".":
                        directories.append(os.path.join(target, *relative.split("/")))
                for remote_file in remote_files:
                    relative = posixpath.relpath(remote_file.path, entry.path)
                    files.append(
                        (
                            remote_file.path,
                            os.path.join(target, *relative.split("/")),
                            remote_file.size,
                        )
                    )
            else:
                files.append((entry.path, target, entry.size))
        for directory in directories:
            if self.cancel_event.is_set():
                raise TransferCancelled("传输已取消")
            os.makedirs(directory, exist_ok=True)

        conflicts = [local_path for _remote, local_path, _size in files if os.path.exists(local_path)]
        skip_paths: set[str] = set()
        if conflicts:
            decision = self._ask_overwrite(conflicts)
            if decision == "cancel":
                self._post("transfer_cancelled", "已取消下载")
                return
            if decision == "skip":
                skip_paths = set(conflicts)

        downloaded = 0
        skipped = 0
        failed = 0
        total_bytes = 0
        consecutive_failures = 0
        for index, (remote_file, local_path, size) in enumerate(files, start=1):
            if self.cancel_event.is_set():
                raise TransferCancelled("传输已取消")
            if local_path in skip_paths:
                skipped += 1
                continue
            self._post("transfer_file", {
                "name": os.path.basename(local_path),
                "index": index,
                "count": len(files),
                "direction": "download",
            })
            try:
                callback = self._progress_callback(os.path.basename(local_path), size)
                client.download_file(remote_file, local_path, progress=callback, cancel=self.cancel_event.is_set)
                downloaded += 1
                total_bytes += size
                consecutive_failures = 0
                self._post("log", (f"已下载 {remote_file} → {local_path}", "ok"))
            except TransferCancelled:
                raise
            except Exception as exc:
                failed += 1
                consecutive_failures += 1
                self._post("log", (f"下载失败 {remote_file}：{exc}", "error"))
                if consecutive_failures >= 3:
                    raise FTPMateError(f"连续下载失败，已停止：{exc}") from exc
        self._post("transfer_done", {
            "direction": "download",
            "uploaded": 0,
            "downloaded": downloaded,
            "skipped": skipped,
            "failed": failed,
            "bytes": total_bytes,
        })

    def _progress_callback(self, name: str, total: int):
        started = time.monotonic()
        last_emit = 0.0

        def callback(done: int, actual_total: int) -> None:
            nonlocal last_emit
            now = time.monotonic()
            effective_total = actual_total or total
            if now - last_emit < 0.08 and done < effective_total:
                return
            last_emit = now
            elapsed = max(now - started, 0.05)
            self._post("progress", {
                "name": name,
                "done": done,
                "total": effective_total,
                "speed": done / elapsed,
            })

        return callback

    def _handle_progress(self, payload: dict) -> None:
        done = int(payload.get("done", 0))
        total = int(payload.get("total", 0))
        speed = float(payload.get("speed", 0))
        if total > 0:
            percent = min(100.0, done * 100.0 / total)
            self.progress_var.set(percent)
            self.percent_var.set(f"{percent:.0f}%")
        else:
            self.progress_var.set(0)
            self.percent_var.set(human_size(done))
        self.speed_var.set(f"{human_size(int(speed))}/s" if speed > 0 else "")

    def _handle_transfer_file(self, payload: dict) -> None:
        direction = payload.get("direction", "upload")
        action = "上传" if direction == "upload" else "下载"
        name = str(payload.get("name", ""))
        index = int(payload.get("index", 0))
        count = int(payload.get("count", 0))
        prefix = f"{action} {index}/{count}" if count else action
        self.transfer_label_var.set(f"{prefix} · {name}")
        self.progress_var.set(0)
        self.percent_var.set("0%")
        self.speed_var.set("")

    def _handle_transfer_done(self, payload: dict) -> None:
        self._finish_transfer_state()
        direction = payload.get("direction", "upload")
        if direction == "upload":
            uploaded = int(payload.get("uploaded", 0))
            skipped = int(payload.get("skipped", 0))
            failed = int(payload.get("failed", 0))
            summary = f"上传完成：成功 {uploaded}，跳过 {skipped}，失败 {failed}"
        else:
            downloaded = int(payload.get("downloaded", 0))
            skipped = int(payload.get("skipped", 0))
            failed = int(payload.get("failed", 0))
            summary = f"下载完成：成功 {downloaded}，跳过 {skipped}，失败 {failed}"
        total_bytes = int(payload.get("bytes", 0))
        label = f"{summary} · {human_size(total_bytes)}"
        self.transfer_label_var.set(label)
        self.progress_var.set(100)
        self.percent_var.set("100%")
        self.speed_var.set("")
        self.bottom_status.configure(text=label)
        self._log(label, "ok" if int(payload.get("failed", 0)) == 0 else "error")
        if direction == "upload":
            self._refresh_remote()
        else:
            self._load_local(self.local_path)

    def _handle_transfer_cancelled(self, message: str) -> None:
        self._finish_transfer_state()
        self.transfer_label_var.set("传输已取消")
        self.progress_var.set(0)
        self.percent_var.set("0%")
        self.speed_var.set("")
        self.bottom_status.configure(text="传输已取消")
        self._log(message, "error")
        self._load_local(self.local_path)
        self._refresh_remote()

    def _cancel_transfer(self) -> None:
        if not self.transfer_active:
            return
        self.cancel_event.set()
        self.cancel_button.configure(text="正在取消…")
        self._enable(self.cancel_button, False)
        self.transfer_label_var.set("正在取消传输…")
        self._log("正在取消传输")

    def _ask_overwrite(self, conflicts: list[str]) -> str:
        if not conflicts:
            return "overwrite"
        box = {"result": "skip"}
        finished = threading.Event()
        self._post("ask_overwrite", {
            "conflicts": conflicts,
            "box": box,
            "event": finished,
        })
        if not finished.wait(timeout=300):
            return "skip"
        return box["result"]

    def _handle_ask_overwrite(self, payload: dict) -> None:
        conflicts = payload["conflicts"]
        preview = "\n".join(conflicts[:8])
        if len(conflicts) > 8:
            preview += f"\n…以及另外 {len(conflicts) - 8} 项"
        answer = messagebox.askyesnocancel(
            "文件已存在",
            f"发现 {len(conflicts)} 个同名文件：\n\n{preview}\n\n是：覆盖\n否：跳过\n取消：终止本次传输",
            parent=self.root,
        )
        if answer is True:
            payload["box"]["result"] = "overwrite"
        elif answer is False:
            payload["box"]["result"] = "skip"
        else:
            payload["box"]["result"] = "cancel"
        payload["event"].set()

    def _remote_mkdir(self) -> None:
        if not self.connected or self.client is None:
            messagebox.showinfo("未连接", "请先连接服务器", parent=self.root)
            return
        name = simpledialog.askstring("新建文件夹", "文件夹名称：", parent=self.root)
        if name is None:
            return
        name = name.strip()
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            messagebox.showerror("名称无效", "文件夹名称不能包含斜杠", parent=self.root)
            return
        target = remote_join(self.remote_path, name)
        client = self.client
        current = self.remote_path

        def task() -> None:
            client.mkdir(target)
            entries = client.list_dir(current)
            self._post("remote_entries", (current, entries))
            self._post("log", (f"已创建远程目录 {target}", "ok"))

        self._submit("新建文件夹", task)

    def _rename_remote(self) -> None:
        if not self.connected or self.client is None:
            return
        entries = self._selected_remote_entries()
        if len(entries) != 1:
            messagebox.showinfo("重命名", "请选择一个文件或文件夹", parent=self.root)
            return
        entry = entries[0]
        name = simpledialog.askstring(
            "重命名",
            "新的名称：",
            initialvalue=entry.name,
            parent=self.root,
        )
        if name is None:
            return
        name = name.strip()
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            messagebox.showerror("名称无效", "名称不能包含斜杠", parent=self.root)
            return
        target = remote_join(remote_parent(entry.path), name)
        client = self.client
        current = self.remote_path

        def task() -> None:
            client.rename(entry.path, target)
            entries_after = client.list_dir(current)
            self._post("remote_entries", (current, entries_after))
            self._post("log", (f"已重命名 {entry.path} → {target}", "ok"))

        self._submit("重命名", task)

    def _delete_remote(self) -> None:
        if not self.connected or self.client is None:
            return
        entries = self._selected_remote_entries()
        if not entries:
            return
        names = "\n".join(entry.name for entry in entries[:10])
        if len(entries) > 10:
            names += f"\n…以及另外 {len(entries) - 10} 项"
        if not messagebox.askyesno(
            "确认删除",
            f"确定删除以下远程项目吗？\n\n{names}\n\n文件夹会递归删除。",
            parent=self.root,
        ):
            return
        client = self.client
        current = self.remote_path

        def task() -> None:
            for entry in entries:
                client.remove(entry.path, entry.is_dir)
            entries_after = client.list_dir(current)
            self._post("remote_entries", (current, entries_after))
            self._post("log", (f"已删除 {len(entries)} 个远程项目", "ok"))

        self._submit("删除远程文件", task)

    def _register_drop_targets(self, widget: tk.Misc) -> None:
        if DND_FILES is None or not hasattr(widget, "drop_target_register"):
            return
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop)
            widget.dnd_bind("<<DropEnter>>", self._on_drop_enter)
            widget.dnd_bind("<<DropLeave>>", self._on_drop_leave)
        except (AttributeError, tk.TclError):
            return
        self._drop_targets.append(widget)
        for child in widget.winfo_children():
            self._register_drop_targets(child)

    def _on_local_drag_init(self, event):
        entries = self._selected_local_entries()
        if not entries:
            try:
                row = self.local_tree.identify_row(
                    event.y_root - self.local_tree.winfo_rooty()
                )
                if row:
                    self.local_tree.selection_set(row)
                    entries = self._selected_local_entries()
            except (AttributeError, tk.TclError):
                pass
        paths = tuple(entry.path for entry in entries if os.path.exists(entry.path))
        if paths and self.connected:
            self.remote_status_var.set(f"拖动 {len(paths)} 个项目，松开到右侧即可上传")
        return ((DND_COPY,), DND_FILES, paths)

    def _on_local_drag_end(self, _event=None):
        if self.connected:
            self._restore_remote_status()

    def _register_local_drop_targets(self, widget: tk.Misc) -> None:
        if DND_TEXT is None or not hasattr(widget, "drop_target_register"):
            return
        try:
            widget.drop_target_register(DND_TEXT)
            widget.dnd_bind("<<Drop>>", self._on_local_drop)
            widget.dnd_bind("<<DropEnter>>", self._on_local_drop_enter)
            widget.dnd_bind("<<DropLeave>>", self._on_local_drop_leave)
        except (AttributeError, tk.TclError):
            return
        self._local_drop_targets.append(widget)
        for child in widget.winfo_children():
            self._register_local_drop_targets(child)

    def _on_remote_drag_init(self, event):
        entries = self._selected_remote_entries()
        if not entries:
            try:
                row = self.remote_tree.identify_row(
                    event.y_root - self.remote_tree.winfo_rooty()
                )
                if row:
                    self.remote_tree.selection_set(row)
                    entries = self._selected_remote_entries()
            except (AttributeError, tk.TclError):
                pass
        payload = REMOTE_DRAG_PREFIX + json.dumps(
            [entry.path for entry in entries],
            ensure_ascii=False,
        )
        if entries:
            self.local_status_var.set(f"拖动 {len(entries)} 项，松开到左侧即可下载")
        return ((DND_COPY,), DND_TEXT, payload)

    def _on_remote_drag_end(self, _event=None):
        self._update_local_status()

    def _remote_entries_from_drop(self, data: str) -> list[RemoteEntry]:
        if not isinstance(data, str) or not data.startswith(REMOTE_DRAG_PREFIX):
            return []
        try:
            paths = json.loads(data[len(REMOTE_DRAG_PREFIX):])
        except (TypeError, ValueError):
            return []
        if not isinstance(paths, list):
            return []
        by_path = {entry.path: entry for entry in self.remote_entries}
        return [by_path[path] for path in paths if isinstance(path, str) and path in by_path]

    def _on_local_drop_enter(self, _event):
        self.local_status_var.set(f"松开鼠标，下载到 {self.local_path}")
        return "copy"

    def _on_local_drop_leave(self, _event):
        self._update_local_status()
        return "copy"

    def _on_local_drop(self, event):
        entries = self._remote_entries_from_drop(event.data)
        if not entries:
            self._update_local_status()
            return "refuse"
        self.local_status_var.set(f"正在下载 {len(entries)} 个项目到 {self.local_path} …")
        self._download_entries(entries)
        return "copy"

    def _drop_paths(self, data: str) -> list[str]:
        try:
            candidates = list(self.root.tk.splitlist(data))
        except tk.TclError:
            candidates = [data]
        paths: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            value = str(candidate).strip().strip('"')
            if not value:
                continue
            path = os.path.abspath(os.path.expanduser(value))
            key = os.path.normcase(path)
            if key not in seen and os.path.exists(path):
                seen.add(key)
                paths.append(path)
        return paths

    def _restore_remote_status(self) -> None:
        if not self.connected:
            self.remote_status_var.set("未连接")
            return
        selected = len(self.remote_tree.selection())
        self.remote_status_var.set(
            f"{len(self.remote_entries)} 项"
            + (f" · 已选 {selected}" if selected else "")
            + f" · {self.remote_path}"
        )

    def _on_drop_enter(self, _event):
        if self.connected:
            self.remote_status_var.set(f"松开鼠标，上传到 {self.remote_path}")
            return "copy"
        return "refuse"

    def _on_drop_leave(self, _event):
        self._restore_remote_status()
        return "copy"

    def _on_drop(self, event):
        if not self.connected:
            messagebox.showinfo("未连接", "请先连接服务器", parent=self.root)
            return "refuse"
        paths = self._drop_paths(event.data)
        if not paths:
            messagebox.showwarning("无法上传", "没有识别到可上传的文件或文件夹", parent=self.root)
            self._restore_remote_status()
            return "refuse"
        self.remote_status_var.set(f"正在上传 {len(paths)} 个项目到 {self.remote_path} …")
        self._upload_paths(paths)
        return "copy"

    def _on_close(self) -> None:
        if self.transfer_active:
            if not messagebox.askyesno("正在传输", "当前仍有传输，确定取消并退出吗？", parent=self.root):
                return
            self.cancel_event.set()
        self.settings.window_geometry = self.root.geometry()
        self.settings.last_local_path = self.local_path
        if self.connected:
            self._remember_remote_path(persist=False)
        self.settings.last_profile = self.profile_var.get() if self.profile_var.get() != "快速连接" else ""
        try:
            save_settings(self.settings)
        except OSError:
            pass
        client = self.client
        self.client = None
        if client is not None:
            threading.Thread(target=client.disconnect, daemon=True).start()
        self.root.destroy()
