"""Moves the observer's browser page (the app, "Session | UVU Eye Tracking") to the second screen.

The calibration covers the main screen (the participant's screen) completely, so the observer's page must already be on the
other screen. Windows only; with one screen, or when nothing is found, it does nothing and never raises an error.
"""

import ctypes
from ctypes import wintypes

PAGE_TITLE = "UVU Eye Tracking"  # the title of every page of the app (the tested websites never have it)


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def _monitors(user32):
    """[(handle, work_rect, is_primary)]"""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    def callback(handle, _dc, _rect, _data):
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            found.append((handle, info.rcWork, bool(info.dwFlags & 1)))  # 1 = MONITORINFOF_PRIMARY
        return True

    user32.EnumDisplayMonitors(None, None, callback, 0)
    return found


def move_app_to_second_screen():
    """True if a window was moved."""
    try:
        user32 = ctypes.windll.user32
        monitors = _monitors(user32)
        others = [m for m in monitors if not m[2]]
        if not others:
            return False
        target_rect = others[0][1]

        windows = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd, _data):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length:
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buffer, length + 1)
                    if PAGE_TITLE in buffer.value:
                        windows.append(hwnd)
            return True

        user32.EnumWindows(callback, 0)

        moved = False
        for hwnd in windows:
            now_on = user32.MonitorFromWindow(hwnd, 2)  # 2 = nearest monitor
            if any(m[0] == now_on and not m[2] for m in monitors):
                continue  # already on the second screen
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE: a maximized window cannot be moved
            user32.SetWindowPos(hwnd, None, target_rect.left + 40, target_rect.top + 40, 1000, 700, 0x0004)  # SWP_NOZORDER
            user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE, now on the second screen
            moved = True
        return moved
    except Exception:
        return False
