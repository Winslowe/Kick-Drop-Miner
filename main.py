"""KickDropsMiner - Main Entry Point"""
import ctypes
import sys
from tkinter import messagebox

from core.browser import BROWSER_MANAGER, cleanup_browser_resources
from ui.app import App


def _acquire_single_instance():
    if sys.platform != "win32":
        return object()
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Local\\KickDropsMinerSingleInstance")
    if not handle or kernel32.GetLastError() == 183:
        if handle:
            kernel32.CloseHandle(handle)
        return None
    return handle


if __name__ == "__main__":
    instance_handle = _acquire_single_instance()
    if instance_handle is None:
        messagebox.showwarning(
            "Kick Drop Miner",
            "Uygulama zaten açık. Mevcut pencereyi kullanın.",
        )
        raise SystemExit(0)
    cleanup_browser_resources()
    app = App()
    try:
        app.mainloop()
    finally:
        BROWSER_MANAGER.close_all()
        if sys.platform == "win32":
            ctypes.windll.kernel32.CloseHandle(instance_handle)
