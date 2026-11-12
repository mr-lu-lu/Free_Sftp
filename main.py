from __future__ import annotations

import ctypes
import sys
import tkinter as tk
import traceback
from tkinter import messagebox

from ftpmate.ui import FTPMateApp


def set_windows_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def create_root() -> tk.Tk:
    try:
        from tkinterdnd2 import TkinterDnD

        return TkinterDnD.Tk()
    except Exception:
        return tk.Tk()


def main() -> None:
    set_windows_dpi_awareness()
    root = create_root()
    try:
        FTPMateApp(root)
    except Exception:
        detail = traceback.format_exc()
        try:
            messagebox.showerror("FTP Mate 启动失败", detail, parent=root)
        finally:
            root.destroy()
        raise
    root.mainloop()


if __name__ == "__main__":
    main()
