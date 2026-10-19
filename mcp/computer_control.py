#!/usr/bin/env python3
"""Computer Control: an MCP server that lets Claude see and operate this computer.

One file, macOS and Windows. Mouse and keyboard go through pyautogui. Screenshots
use the platform's own tools (macOS `screencapture` + `sips`, Windows PIL
ImageGrab). The MCP protocol (JSON-RPC over stdio) is implemented here, so the
only third-party package on macOS is pyautogui and its pyobjc bindings.

Coordinate model: every x/y a tool takes or returns is in the pixel space of the
most recent `screenshot` image. The server remembers that image's display and
scale and converts to real screen coordinates, so Retina / HiDPI scaling and
multiple monitors are handled here, not by the model.

Emergency stop: slam the mouse pointer into any screen corner. The next action
fails and Claude is told the user stopped it.

  python computer_control.py              run the MCP server on stdio
  python computer_control.py --check      verify imports, print versions
  python computer_control.py --self-test  permissions + one capture, no input
"""
from __future__ import annotations

import base64
import inspect
import json
import os
import string
import subprocess
import sys
import tempfile
import time
import types
import typing
from dataclasses import dataclass
from typing import Any, Literal

VERSION = "1.1.0"
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"
DEFAULT_MAX_SIZE = 1400

# The protocol owns stdout; anything a library prints goes to stderr instead.
_PROTOCOL_OUT = sys.stdout
sys.stdout = sys.stderr

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    try:  # physical pixels everywhere, so screenshots and clicks agree
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import pyautogui  # noqa: E402  (after DPI awareness on Windows)

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.04
if IS_MAC:
    pyautogui.DARWIN_CATCH_UP_TIME = 0.02


def log(*args: Any) -> None:
    print("[computer-control]", *args, file=sys.stderr, flush=True)


class ToolError(Exception):
    pass


@dataclass
class Display:
    index: int          # 1-based; 1 is the main / primary display
    x: float            # global origin, top-left based
    y: float
    width: float        # size in input coordinates (points on macOS, pixels on Windows)
    height: float
    ref: int = 0        # macOS display id


def fit(w: float, h: float, max_side: int) -> tuple[int, int]:
    s = min(1.0, max_side / max(w, h))
    return max(1, round(w * s)), max(1, round(h * s))


def jpeg_size(data: bytes) -> tuple[int, int]:
    """Width and height from a JPEG's SOF marker, without decoding it."""
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    raise ValueError("not a JPEG")


def png_size(data: bytes) -> tuple[int, int]:
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


# ---------------------------------------------------------------------------
# macOS backend
# ---------------------------------------------------------------------------

class MacBackend:
    paste_mod = "command"

    def __init__(self) -> None:
        import ctypes
        import ctypes.util

        import Quartz
        from AppKit import NSPasteboard, NSPasteboardItem, NSWorkspace
        self.Q, self.NSPasteboard, self.NSPasteboardItem, self.NSWorkspace = \
            Quartz, NSPasteboard, NSPasteboardItem, NSWorkspace
        ax = ctypes.cdll.LoadLibrary(ctypes.util.find_library("ApplicationServices"))
        ax.AXIsProcessTrusted.restype = ctypes.c_bool
        self._ax_trusted = ax.AXIsProcessTrusted

    def displays(self) -> list[Display]:
        Q = self.Q
        _, ids, count = Q.CGGetActiveDisplayList(16, None, None)
        main = Q.CGMainDisplayID()
        ids = sorted(list(ids[:count]) if ids else [main], key=lambda d: d != main)
        out = []
        for i, did in enumerate(ids, start=1):
            b = Q.CGDisplayBounds(did)
            out.append(Display(i, b.origin.x, b.origin.y, b.size.width, b.size.height, int(did)))
        return out

    # Fast path: grab, scale and encode in-process with Quartz (~0.1 s at 4K). The
    # screencapture + sips path below is the fallback if the OS ever refuses it.
    def _grab(self, d: Display) -> Any:
        try:
            return self.Q.CGDisplayCreateImage(d.ref) if d.ref else None
        except Exception:
            return None

    def _cursor(self, d: Display, tw: int, th: int) -> tuple[float, float] | None:
        p = self.Q.CGEventGetLocation(self.Q.CGEventCreate(None))
        cx, cy = (p.x - d.x) * tw / d.width, (p.y - d.y) * th / d.height
        return (cx, cy) if 0 <= cx < tw and 0 <= cy < th else None

    def _encode(self, img: Any, tw: int, th: int, fmt: str, cursor: tuple[float, float] | None = None) -> bytes:
        Q = self.Q
        ctx = Q.CGBitmapContextCreate(None, tw, th, 8, 0, Q.CGImageGetColorSpace(img),  # no color conversion
                                      Q.kCGImageAlphaNoneSkipLast)
        Q.CGContextSetInterpolationQuality(ctx, Q.kCGInterpolationHigh)
        Q.CGContextDrawImage(ctx, Q.CGRectMake(0, 0, tw, th), img)
        if cursor:  # the grab leaves the pointer out; draw a small arrow where it is
            s, (cx, cy) = max(1.0, tw / 1400), cursor
            pts = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]
            Q.CGContextBeginPath(ctx)
            for i, (px, py) in enumerate(pts):
                (Q.CGContextMoveToPoint if i == 0 else Q.CGContextAddLineToPoint)(ctx, cx + px * s, th - (cy + py * s))
            Q.CGContextClosePath(ctx)
            Q.CGContextSetRGBFillColor(ctx, 1, 1, 1, 1)
            Q.CGContextSetRGBStrokeColor(ctx, 0, 0, 0, 1)
            Q.CGContextSetLineWidth(ctx, s)
            Q.CGContextDrawPath(ctx, Q.kCGPathFillStroke)
        out = Q.CFDataCreateMutable(None, 0)
        dest = Q.CGImageDestinationCreateWithData(out, "public.jpeg" if fmt == "jpeg" else "public.png", 1, None)
        props = {Q.kCGImageDestinationLossyCompressionQuality: 0.7} if fmt == "jpeg" else None
        Q.CGImageDestinationAddImage(dest, Q.CGBitmapContextCreateImage(ctx), props)
        if not Q.CGImageDestinationFinalize(dest):
            raise ToolError("Could not encode the screenshot.")
        return bytes(out)

    def _capture(self, d: Display, ext: str, show_cursor: bool) -> str:
        fd, path = tempfile.mkstemp(suffix="." + ext)
        os.close(fd)
        cmd = ["screencapture", "-x", "-t", ext, "-D", str(d.index)] + (["-C"] if show_cursor else [])
        r = subprocess.run(cmd + [path], capture_output=True, text=True, timeout=15)
        if r.returncode != 0 or not os.path.getsize(path):
            os.unlink(path)
            raise ToolError(f"screencapture failed: {r.stderr.strip() or 'no image'}. Screen Recording "
                            f"permission may be missing for '{responsible_app()}'.")
        return path

    @staticmethod
    def _sips(args: list[str]) -> None:
        r = subprocess.run(["sips", *args], capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            raise ToolError(f"sips failed: {r.stderr.strip()}")

    def screenshot(self, d: Display, max_size: int, show_cursor: bool, fmt: str) -> tuple[bytes, int, int]:
        img = self._grab(d)
        if img is not None:
            w, h = fit(d.width, d.height, max_size)
            return self._encode(img, w, h, fmt, self._cursor(d, w, h) if show_cursor else None), w, h
        ext = "jpg" if fmt == "jpeg" else "png"
        path = self._capture(d, ext, show_cursor)
        try:
            w, h = fit(d.width, d.height, max_size)
            opts = ["-s", "formatOptions", "70"] if ext == "jpg" else []
            self._sips(["-z", str(h), str(w), *opts, path])
            with open(path, "rb") as f:
                data = f.read()
        finally:
            os.unlink(path)
        w, h = jpeg_size(data) if ext == "jpg" else png_size(data)
        return data, w, h

    def crop(self, d: Display, box: tuple[float, float, float, float], max_size: int) -> tuple[bytes, int, int]:
        img = self._grab(d)
        if img is not None:
            s = self.Q.CGImageGetWidth(img) / d.width  # pixels per point
            x0, y0, x1, y1 = (v * s for v in box)
            part = self.Q.CGImageCreateWithImageInRect(img, self.Q.CGRectMake(x0, y0, x1 - x0, y1 - y0))
            w, h = fit(self.Q.CGImageGetWidth(part), self.Q.CGImageGetHeight(part), max_size)
            return self._encode(part, w, h, "png"), w, h
        path = self._capture(d, "png", show_cursor=False)
        try:
            with open(path, "rb") as f:
                pw, ph = png_size(f.read(32))
            sx, sy = pw / d.width, ph / d.height  # pixels per point
            x0, y0, x1, y1 = box
            cw, ch = max(1, int((x1 - x0) * sx)), max(1, int((y1 - y0) * sy))
            self._sips(["--cropOffset", str(int(y0 * sy)), str(int(x0 * sx)), "-c", str(ch), str(cw), path])
            if max(cw, ch) > max_size:
                self._sips(["-Z", str(max_size), path])
            with open(path, "rb") as f:
                data = f.read()
        finally:
            os.unlink(path)
        w, h = png_size(data)
        return data, w, h

    def permissions(self) -> dict[str, Any]:
        return {"accessibility": bool(self._ax_trusted()),
                "screen_recording": bool(self.Q.CGPreflightScreenCaptureAccess()),
                "granted_to": responsible_app()}

    def input_problem(self) -> str | None:
        if self._ax_trusted():
            return None
        return (f"Accessibility permission is missing, so macOS ignores mouse and keyboard input. Ask the "
                f"user to enable '{responsible_app()}' in System Settings > Privacy & Security > "
                f"Accessibility, then restart it.")

    def capture_warning(self) -> str | None:
        if self.Q.CGPreflightScreenCaptureAccess():
            return None
        return "Screen Recording permission is not granted, so other apps' windows may be missing."

    # Clipboard: keeps every type on the pasteboard, not only text.
    def clipboard_snapshot(self) -> Any:
        pb = self.NSPasteboard.generalPasteboard()
        items = []
        for item in pb.pasteboardItems() or []:
            entry = {}
            for t in item.types():
                data = item.dataForType_(t)
                if data is not None:
                    entry[t] = data
            items.append(entry)
        return items

    def clipboard_restore(self, items: Any) -> None:
        pb = self.NSPasteboard.generalPasteboard()
        pb.clearContents()
        objs = []
        for entry in items or []:
            it = self.NSPasteboardItem.alloc().init()
            for t, data in entry.items():
                it.setData_forType_(data, t)
            objs.append(it)
        if objs:
            pb.writeObjects_(objs)

    def clipboard_set(self, text_: str) -> None:
        pb = self.NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text_, "public.utf8-plain-text")

    def clipboard_get(self) -> str:
        return self.NSPasteboard.generalPasteboard().stringForType_("public.utf8-plain-text") or ""

    def applications(self) -> list[dict[str, Any]]:
        ws = self.NSWorkspace.sharedWorkspace()
        front = ws.frontmostApplication()
        fpid = front.processIdentifier() if front else -1
        return [{"name": a.localizedName(), "id": a.bundleIdentifier(),
                 "frontmost": a.processIdentifier() == fpid, "hidden": bool(a.isHidden())}
                for a in ws.runningApplications() if a.activationPolicy() == 0]

    def open_app(self, name: str) -> None:
        r = subprocess.run(["open", "-a", name], capture_output=True, text=True, timeout=20)
        if r.returncode != 0 and "." in name:
            r = subprocess.run(["open", "-b", name], capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            raise ToolError(f"Could not open '{name}': {r.stderr.strip()}")

    def windows(self) -> list[dict[str, Any]]:
        Q = self.Q
        opts = Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements
        out = []
        for w in Q.CGWindowListCopyWindowInfo(opts, Q.kCGNullWindowID) or []:
            if w.get("kCGWindowLayer", 0) != 0:
                continue
            b = w.get("kCGWindowBounds", {})
            out.append({"app": w.get("kCGWindowOwnerName", ""), "title": w.get("kCGWindowName", ""),
                        "rect": (b.get("X", 0), b.get("Y", 0), b.get("Width", 0), b.get("Height", 0))})
        return out


# ---------------------------------------------------------------------------
# Windows backend
# ---------------------------------------------------------------------------

class WinBackend:
    paste_mod = "ctrl"

    def __init__(self) -> None:
        from PIL import Image, ImageDraw, ImageGrab
        self.Image, self.ImageDraw, self.ImageGrab = Image, ImageDraw, ImageGrab
        self.u32 = ctypes.windll.user32
        self.k32 = ctypes.windll.kernel32
        vp = ctypes.c_void_p
        self.u32.GetClipboardData.restype = vp
        self.u32.SetClipboardData.argtypes = [wintypes.UINT, vp]
        self.k32.GlobalAlloc.restype = vp
        self.k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        self.k32.GlobalLock.restype = vp
        self.k32.GlobalLock.argtypes = [vp]
        self.k32.GlobalUnlock.argtypes = [vp]

    def displays(self) -> list[Display]:
        mons: list[tuple[int, int, int, int, bool]] = []

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        proc_t = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                                    ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        def cb(hmon, hdc, rect, lp):
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            self.u32.GetMonitorInfoW(hmon, ctypes.byref(mi))
            r = mi.rcMonitor
            mons.append((r.left, r.top, r.right - r.left, r.bottom - r.top, bool(mi.dwFlags & 1)))
            return 1

        self.u32.EnumDisplayMonitors(None, None, proc_t(cb), 0)
        mons.sort(key=lambda m: not m[4])  # primary first
        if not mons:
            w, h = pyautogui.size()
            return [Display(1, 0, 0, w, h)]
        return [Display(i, m[0], m[1], m[2], m[3]) for i, m in enumerate(mons, start=1)]

    def _grab(self, d: Display, show_cursor: bool):
        box = (int(d.x), int(d.y), int(d.x + d.width), int(d.y + d.height))
        img = self.ImageGrab.grab(bbox=box, all_screens=True).convert("RGB")
        if show_cursor:  # ImageGrab leaves the pointer out; draw a small arrow where it is
            cx, cy = pyautogui.position()
            cx, cy = cx - d.x, cy - d.y
            if 0 <= cx < img.width and 0 <= cy < img.height:
                s = max(1, round(img.width / 1400))
                pts = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]
                self.ImageDraw.Draw(img).polygon([(cx + px * s, cy + py * s) for px, py in pts],
                                                 fill="white", outline="black", width=s)
        return img

    def _encode(self, img, fmt: str) -> bytes:
        import io
        buf = io.BytesIO()
        if fmt == "png":
            img.save(buf, "PNG", optimize=True)
        else:
            img.save(buf, "JPEG", quality=70, optimize=True)
        return buf.getvalue()

    def _fit(self, img, max_size: int):
        w, h = fit(img.width, img.height, max_size)
        return img if (w, h) == img.size else img.resize((w, h), self.Image.LANCZOS)

    def screenshot(self, d: Display, max_size: int, show_cursor: bool, fmt: str) -> tuple[bytes, int, int]:
        img = self._fit(self._grab(d, show_cursor), max_size)
        return self._encode(img, fmt), img.width, img.height

    def crop(self, d: Display, box: tuple[float, float, float, float], max_size: int) -> tuple[bytes, int, int]:
        img = self._grab(d, show_cursor=False).crop(tuple(int(v) for v in box))
        img = self._fit(img, max_size)
        return self._encode(img, "png"), img.width, img.height

    def permissions(self) -> dict[str, Any]:
        try:
            admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            admin = False
        return {"accessibility": True, "screen_recording": True, "elevated": admin,
                "note": "Windows needs no permission, but windows of programs running as administrator "
                        "only accept input if this server also runs as administrator."}

    def input_problem(self) -> str | None:
        return None

    def capture_warning(self) -> str | None:
        return None

    # Clipboard (Unicode text)
    def _open_clip(self) -> None:
        for _ in range(25):
            if self.u32.OpenClipboard(None):
                return
            time.sleep(0.02)
        raise ToolError("The clipboard is busy.")

    def clipboard_get(self) -> str:
        self._open_clip()
        try:
            h = self.u32.GetClipboardData(13)  # CF_UNICODETEXT
            if not h:
                return ""
            p = self.k32.GlobalLock(h)
            try:
                return ctypes.wstring_at(p)
            finally:
                self.k32.GlobalUnlock(h)
        finally:
            self.u32.CloseClipboard()

    def clipboard_set(self, text_: str) -> None:
        data = text_.encode("utf-16-le") + b"\x00\x00"
        h = self.k32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        ctypes.memmove(self.k32.GlobalLock(h), data, len(data))
        self.k32.GlobalUnlock(h)
        self._open_clip()
        try:
            self.u32.EmptyClipboard()
            self.u32.SetClipboardData(13, h)
        finally:
            self.u32.CloseClipboard()

    def clipboard_snapshot(self) -> Any:
        try:
            return self.clipboard_get()
        except ToolError:
            return None

    def clipboard_restore(self, saved: Any) -> None:
        if saved is not None:
            self.clipboard_set(saved)

    def _top_windows(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        proc_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        fg = self.u32.GetForegroundWindow()

        def exe_of(hwnd) -> str:
            pid = wintypes.DWORD()
            self.u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            hp = self.k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not hp:
                return ""
            try:
                buf = ctypes.create_unicode_buffer(1024)
                n = wintypes.DWORD(1024)
                ok = self.k32.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(n))
                return os.path.basename(buf.value) if ok else ""
            finally:
                self.k32.CloseHandle(hp)

        def cb(hwnd, lp):
            if not self.u32.IsWindowVisible(hwnd) or self.u32.IsIconic(hwnd):
                return True
            n = self.u32.GetWindowTextLengthW(hwnd)
            if n == 0:
                return True
            title = ctypes.create_unicode_buffer(n + 1)
            self.u32.GetWindowTextW(hwnd, title, n + 1)
            r = wintypes.RECT()
            self.u32.GetWindowRect(hwnd, ctypes.byref(r))
            if r.right - r.left < 2 or r.bottom - r.top < 2:
                return True
            out.append({"app": exe_of(hwnd), "title": title.value, "hwnd": hwnd, "frontmost": hwnd == fg,
                        "rect": (r.left, r.top, r.right - r.left, r.bottom - r.top)})
            return True

        self.u32.EnumWindows(proc_t(cb), 0)
        return out

    def applications(self) -> list[dict[str, Any]]:
        apps: dict[str, dict[str, Any]] = {}
        for w in self._top_windows():
            a = apps.setdefault(w["app"], {"name": w["app"], "id": w["app"], "frontmost": False, "windows": 0})
            a["frontmost"] = a["frontmost"] or w["frontmost"]
            a["windows"] += 1
        return list(apps.values())

    def open_app(self, name: str) -> None:
        low = name.lower()
        for w in self._top_windows():  # bring an open window forward first
            if low in w["title"].lower() or low == w["app"].lower().removesuffix(".exe"):
                self.u32.ShowWindow(w["hwnd"], 9)  # SW_RESTORE
                pyautogui.press("alt")  # lets SetForegroundWindow past the focus lock
                self.u32.SetForegroundWindow(w["hwnd"])
                return
        try:
            os.startfile(name)
        except OSError:
            r = subprocess.run(["cmd", "/c", "start", "", name], capture_output=True, text=True, timeout=20)
            if r.returncode != 0:
                raise ToolError(f"Could not open '{name}'. Try the program's exe name or full path.")

    def windows(self) -> list[dict[str, Any]]:
        return [{"app": w["app"], "title": w["title"], "rect": w["rect"]} for w in self._top_windows()]


_backend: Any = None


def get_backend() -> Any:
    global _backend
    if _backend is None:
        if IS_MAC:
            _backend = MacBackend()
        elif IS_WIN:
            _backend = WinBackend()
        else:
            raise ToolError("Only macOS and Windows are supported.")
    return _backend


# ---------------------------------------------------------------------------
# Coordinate mapping
# ---------------------------------------------------------------------------

@dataclass
class View:
    """Maps the last screenshot's pixels to global screen coordinates."""
    display: Display
    img_w: int
    img_h: int

    @property
    def sx(self) -> float:
        return self.display.width / self.img_w

    @property
    def sy(self) -> float:
        return self.display.height / self.img_h

    def to_global(self, x: float, y: float) -> tuple[float, float]:
        if not (0 <= x <= self.img_w and 0 <= y <= self.img_h):
            raise ToolError(f"({x}, {y}) is outside the {self.img_w}x{self.img_h} screenshot.")
        d = self.display
        gx = min(max(d.x + x * self.sx, d.x), d.x + d.width - 1)
        gy = min(max(d.y + y * self.sy, d.y), d.y + d.height - 1)
        return gx, gy

    def to_image(self, gx: float, gy: float) -> tuple[int, int]:
        return round((gx - self.display.x) / self.sx), round((gy - self.display.y) / self.sy)


_view: View | None = None


def get_display(index: int) -> Display:
    ds = get_backend().displays()
    if index < 1 or index > len(ds):
        raise ToolError(f"Display {index} does not exist. Available: 1..{len(ds)} (see list_displays).")
    return ds[index - 1]


def view() -> View:
    global _view
    if _view is None:
        d = get_display(1)
        _view = View(d, *fit(d.width, d.height, DEFAULT_MAX_SIZE))
    return _view


def responsible_app() -> str:
    """Best guess at the .app macOS attributes permissions to (it owns the process tree)."""
    pid = os.getppid()
    for _ in range(25):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=2).stdout.strip()
        except Exception:
            break
        if not out:
            break
        ppid, _, comm = out.partition(" ")
        if ".app/" in comm and "/Python.app/" not in comm:
            return comm.split(".app/")[0].rsplit("/", 1)[-1]
        pid = int(ppid)
        if pid <= 1:
            break
    return "the app that started this server"


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

_MOD_CMD = "command" if IS_MAC else "ctrl"   # "cmd" in a Windows shortcut means Ctrl
_MOD_WIN = "command" if IS_MAC else "win"
_MOD_ALT = "option" if IS_MAC else "alt"
_ENTER = "return" if IS_MAC else "enter"
KEY_ALIASES = {
    "cmd": _MOD_CMD, "command": _MOD_CMD, "super": _MOD_WIN, "meta": _MOD_WIN, "win": _MOD_WIN,
    "windows": _MOD_WIN, "ctrl": "ctrl", "control": "ctrl", "alt": _MOD_ALT, "option": _MOD_ALT,
    "opt": _MOD_ALT, "shift": "shift", "fn": "fn",
    "return": _ENTER, "enter": _ENTER, "esc": "esc", "escape": "esc", "tab": "tab",
    "space": "space", "spacebar": "space", "backspace": "backspace", "delete": "delete",
    "del": "delete", "forwarddelete": "delete",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
    "pageup": "pageup", "pagedown": "pagedown", "prior": "pageup", "next": "pagedown",
    "home": "home", "end": "end", "capslock": "capslock", "printscreen": "printscreen",
    "volumeup": "volumeup", "volumedown": "volumedown", "mute": "volumemute", "volumemute": "volumemute",
    "plus": "+", "minus": "-",
}
MODIFIERS = {"command", "ctrl", "option", "alt", "shift", "fn", "win"}


def norm_key(name: str) -> str:
    raw = name.strip()
    if len(raw) == 1:
        return raw
    k = raw.lower().replace("_", "").replace("-", "")
    if k in KEY_ALIASES:
        return KEY_ALIASES[k]
    if k.startswith("f") and k[1:].isdigit() and 1 <= int(k[1:]) <= 24:
        return k
    if k in pyautogui.KEYBOARD_KEYS:
        return k
    raise ToolError(f"Unknown key '{name}'. Use names like cmd, ctrl, alt/option, shift, return, esc, tab, "
                    f"space, backspace, delete, up/down/left/right, pageup, home, end, f1-f24, or one character.")


def parse_combo(combo: str) -> list[str]:
    parts = combo.split("+")
    keys, i = [], 0
    while i < len(parts):
        p = parts[i]
        if p == "" and i + 1 < len(parts) and parts[i + 1] == "":  # "cmd++" is cmd and plus
            keys.append("+")
            i += 2
            continue
        if p.strip():
            keys.append(norm_key(p))
        i += 1
    if len(keys) > 1:
        keys = [k.lower() if len(k) == 1 else k for k in keys]
    return keys


def parse_modifiers(mods: str | None) -> list[str]:
    if not mods:
        return []
    keys = parse_combo(mods)
    bad = [k for k in keys if k not in MODIFIERS]
    if bad:
        raise ToolError(f"Not modifier keys: {bad}. Use cmd, ctrl, alt/option, shift.")
    return keys


class held:
    """Hold keys down for the duration of a block."""

    def __init__(self, keys: list[str]):
        self.keys = keys

    def __enter__(self):
        for k in self.keys:
            pyautogui.keyDown(k)
        if self.keys:
            time.sleep(0.03)

    def __exit__(self, *exc):
        for k in reversed(self.keys):
            pyautogui.keyUp(k)
        return False


TYPEABLE = set(string.ascii_letters + string.digits + string.punctuation + " \n\t")


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def image(data: bytes, mime: str) -> dict[str, Any]:
    return {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": mime}


def text(s: str) -> dict[str, Any]:
    return {"type": "text", "text": s}


def take_screenshot(display: int = 1, max_size: int = DEFAULT_MAX_SIZE, show_cursor: bool = True,
                    fmt: str = "jpeg") -> list[dict[str, Any]]:
    global _view
    d = get_display(display)
    data, w, h = get_backend().screenshot(d, max(200, min(int(max_size), 4096)), show_cursor, fmt)
    _view = View(d, w, h)
    note = (f"Screenshot of display {d.index}: {w}x{h} px (screen is {int(d.width)}x{int(d.height)}). "
            f"Use pixel coordinates from this image for all actions.")
    warn = get_backend().capture_warning()
    if warn:
        note += " WARNING: " + warn
    return [image(data, "image/jpeg" if fmt == "jpeg" else "image/png"), text(note)]


def with_shot(msg: str, screenshot_after: bool) -> list[dict[str, Any]]:
    if not screenshot_after:
        return [text(msg)]
    time.sleep(0.35)  # let the UI settle
    v = view()
    return [text(msg)] + take_screenshot(v.display.index, max(v.img_w, v.img_h))


# ---------------------------------------------------------------------------
# Actions (shared by the single tools and `batch`)
# ---------------------------------------------------------------------------

def require_input() -> None:
    problem = get_backend().input_problem()
    if problem:
        raise ToolError(problem)


def _pos(x: float | None, y: float | None) -> tuple[float, float] | None:
    if x is None and y is None:
        return None
    if x is None or y is None:
        raise ToolError("Give both x and y, or neither to act at the current pointer position.")
    return view().to_global(x, y)


def do_move(x: float, y: float) -> str:
    require_input()
    pyautogui.moveTo(*view().to_global(x, y))
    return f"Moved pointer to ({x}, {y})."


def do_click(x: float | None = None, y: float | None = None, button: str = "left", clicks: int = 1,
             modifiers: str | None = None) -> str:
    require_input()
    if button not in ("left", "right", "middle"):
        raise ToolError("button must be left, right or middle.")
    clicks = max(1, min(int(clicks), 3))
    p = _pos(x, y)
    mods = parse_modifiers(modifiers)
    if p:
        pyautogui.moveTo(*p)
        time.sleep(0.03)
    with held(mods):
        pyautogui.click(button=button, clicks=clicks, interval=0.06)
    kind = {1: "Clicked", 2: "Double-clicked", 3: "Triple-clicked"}[clicks]
    where = f"({x}, {y})" if p else "the current position"
    return f"{kind} {button} at {where}" + (f" with {'+'.join(mods)}" if mods else "") + "."


def do_drag(start_x: float, start_y: float, end_x: float, end_y: float, button: str = "left",
            duration: float = 0.5, modifiers: str | None = None) -> str:
    require_input()
    s, e = view().to_global(start_x, start_y), view().to_global(end_x, end_y)
    mods = parse_modifiers(modifiers)
    pyautogui.moveTo(*s)
    time.sleep(0.05)
    with held(mods):
        pyautogui.mouseDown(button=button)
        time.sleep(0.08)
        pyautogui.moveTo(*e, duration=max(0.1, min(float(duration), 5.0)), tween=pyautogui.easeInOutQuad)
        time.sleep(0.08)
        pyautogui.mouseUp(button=button)
    return f"Dragged from ({start_x}, {start_y}) to ({end_x}, {end_y})."


def do_scroll(direction: str, amount: int = 5, x: float | None = None, y: float | None = None,
              modifiers: str | None = None) -> str:
    require_input()
    amount = max(1, min(int(amount), 50))
    p = _pos(x, y)
    if p:
        pyautogui.moveTo(*p)
    step = amount if IS_MAC else amount * 120  # Windows counts wheel deltas, 120 per notch
    with held(parse_modifiers(modifiers)):
        if direction == "up":
            pyautogui.scroll(step)
        elif direction == "down":
            pyautogui.scroll(-step)
        elif direction == "left":
            pyautogui.hscroll(step if IS_MAC else -step)
        elif direction == "right":
            pyautogui.hscroll(-step if IS_MAC else step)
        else:
            raise ToolError("direction must be up, down, left or right.")
    return f"Scrolled {direction} by {amount}" + (f" at ({x}, {y})" if p else "") + "."


def do_type(text_: str, method: str = "auto") -> str:
    require_input()
    if not text_:
        return "Nothing to type."
    if method == "auto":
        method = "keys" if len(text_) <= 200 and set(text_) <= TYPEABLE else "paste"
    if method == "keys":
        bad = sorted(set(text_) - TYPEABLE)
        if bad:
            raise ToolError(f"These characters cannot be typed as keys: {bad!r}. Use method='paste'.")
        pyautogui.write(text_, interval=0.012)
        return f"Typed {len(text_)} characters."
    if method != "paste":
        raise ToolError("method must be auto, keys or paste.")
    b = get_backend()
    saved = b.clipboard_snapshot()
    try:
        b.clipboard_set(text_)
        time.sleep(0.05)
        pyautogui.hotkey(b.paste_mod, "v", interval=0.03)
        time.sleep(0.25)
    finally:
        b.clipboard_restore(saved)
    return f"Pasted {len(text_)} characters (clipboard restored)."


def do_keys(keys: str, repeat: int = 1) -> str:
    require_input()
    combos = [parse_combo(c) for c in keys.split()]
    if not combos:
        raise ToolError("keys is empty.")
    for _ in range(max(1, min(int(repeat), 100))):
        for combo in combos:
            if len(combo) == 1:
                pyautogui.press(combo[0])
            else:
                pyautogui.hotkey(*combo, interval=0.03)
            time.sleep(0.03)
    return f"Pressed {keys}" + (f" x{repeat}" if repeat > 1 else "") + "."


def do_hold(keys: str, seconds: float = 1.0) -> str:
    require_input()
    seconds = max(0.05, min(float(seconds), 10.0))
    with held(parse_combo(keys)):
        time.sleep(seconds)
    return f"Held {keys} for {seconds}s."


def do_mouse_button(action: str, button: str = "left") -> str:
    require_input()
    if action == "down":
        pyautogui.mouseDown(button=button)
    elif action == "up":
        pyautogui.mouseUp(button=button)
    else:
        raise ToolError("action must be down or up.")
    return f"Mouse {button} {action}."


def do_wait(seconds: float = 1.0) -> str:
    seconds = max(0.0, min(float(seconds), 60.0))
    time.sleep(seconds)
    return f"Waited {seconds}s."


ACTIONS = {"move": do_move, "click": do_click, "drag": do_drag, "scroll": do_scroll, "type": do_type,
           "keys": do_keys, "hold_key": do_hold, "mouse_button": do_mouse_button, "wait": do_wait}


# ---------------------------------------------------------------------------
# Tools (schemas come from the signatures, descriptions from the docstrings)
# ---------------------------------------------------------------------------

TOOLS: dict[str, dict[str, Any]] = {}
_READ = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
_ACT = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}
_SAFE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}


def _schema(tp: Any) -> dict[str, Any]:
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if origin is Literal:
        return {"type": "string", "enum": list(args)}
    if origin in (typing.Union, types.UnionType):
        inner = [a for a in args if a is not type(None)]
        return _schema(inner[0]) if len(inner) == 1 else {}
    if origin is list:
        return {"type": "array", "items": _schema(args[0]) if args else {}}
    if origin is dict or tp is dict:
        return {"type": "object"}
    return {int: {"type": "integer"}, float: {"type": "number"}, str: {"type": "string"},
            bool: {"type": "boolean"}}.get(tp, {})


def tool(annotations: dict[str, Any]):
    def deco(fn):
        hints = typing.get_type_hints(fn)
        props, required = {}, []
        for name, p in inspect.signature(fn).parameters.items():
            props[name] = _schema(hints.get(name, str))
            if p.default is inspect.Parameter.empty:
                required.append(name)
            elif p.default is not None:
                props[name]["default"] = p.default
        TOOLS[fn.__name__] = {"fn": fn, "spec": {
            "name": fn.__name__, "description": inspect.getdoc(fn) or "",
            "inputSchema": {"type": "object", "properties": props, "required": required},
            "annotations": annotations}}
        return fn
    return deco


@tool(_READ)
def screenshot(display: int = 1, max_size: int = DEFAULT_MAX_SIZE, show_cursor: bool = True,
               format: Literal["jpeg", "png"] = "jpeg"):
    """Capture a display and make it the coordinate space for later actions.

    display: 1 is the main display (see list_displays). max_size: longest side of the
    returned image in pixels (default 1400). format: jpeg is smaller, png is sharper.
    """
    return take_screenshot(display, max_size, show_cursor, format)


@tool(_READ)
def zoom(x0: float, y0: float, x1: float, y1: float, max_size: int = 1400):
    """Return a full-resolution crop of a region of the last screenshot, to read small text.

    Coordinates are in the last screenshot's pixel space. Does not change the coordinate space.
    """
    v = view()
    if x1 <= x0 or y1 <= y0:
        raise ToolError("Need x1 > x0 and y1 > y0.")
    (gx0, gy0), (gx1, gy1) = v.to_global(x0, y0), v.to_global(x1, y1)
    d = v.display
    data, w, h = get_backend().crop(d, (gx0 - d.x, gy0 - d.y, gx1 - d.x, gy1 - d.y), max_size)
    return [image(data, "image/png"),
            text(f"Zoom of ({x0},{y0})-({x1},{y1}) at {w}x{h}. Coordinates still refer to the last screenshot.")]


@tool(_ACT)
def click(x: float | None = None, y: float | None = None, button: Literal["left", "right", "middle"] = "left",
          clicks: int = 1, modifiers: str | None = None, screenshot_after: bool = False):
    """Click at (x, y), or at the current pointer position if both are omitted.

    clicks: 1, 2 (double) or 3 (triple). modifiers: e.g. "cmd", "shift", "cmd+shift".
    """
    return with_shot(do_click(x, y, button, clicks, modifiers), screenshot_after)


@tool(_ACT)
def move_mouse(x: float, y: float, screenshot_after: bool = False):
    """Move the pointer to (x, y) without clicking (for hover menus and tooltips)."""
    return with_shot(do_move(x, y), screenshot_after)


@tool(_ACT)
def drag(start_x: float, start_y: float, end_x: float, end_y: float,
         button: Literal["left", "right", "middle"] = "left", duration: float = 0.5,
         modifiers: str | None = None, screenshot_after: bool = False):
    """Press at the start point, move smoothly to the end point, and release."""
    return with_shot(do_drag(start_x, start_y, end_x, end_y, button, duration, modifiers), screenshot_after)


@tool(_ACT)
def scroll(direction: Literal["up", "down", "left", "right"], amount: int = 5, x: float | None = None,
           y: float | None = None, modifiers: str | None = None, screenshot_after: bool = False):
    """Scroll by `amount` wheel notches, over (x, y) if given (the view under the pointer scrolls)."""
    return with_shot(do_scroll(direction, amount, x, y, modifiers), screenshot_after)


@tool(_ACT)
def type_text(text: str, method: Literal["auto", "keys", "paste"] = "auto", screenshot_after: bool = False):
    """Type text into the focused field.

    auto: short plain ASCII is typed key by key; anything longer or non-ASCII (Chinese, emoji,
    accents) is pasted through the clipboard, which is restored afterwards.
    """
    return with_shot(do_type(text, method), screenshot_after)


@tool(_ACT)
def press_keys(keys: str, repeat: int = 1, screenshot_after: bool = False):
    """Press a key or shortcut. Join a combo with '+', separate steps with spaces.

    Examples: "return", "cmd+c", "cmd+shift+4", "tab tab return", "down". Names: cmd, ctrl,
    alt/option, shift, win, return, esc, tab, space, backspace, delete (forward delete),
    up/down/left/right, pageup, pagedown, home, end, f1-f24. On Windows "cmd" means Ctrl.
    """
    return with_shot(do_keys(keys, repeat), screenshot_after)


@tool(_ACT)
def hold_key(keys: str, seconds: float = 1.0):
    """Hold a key or combo down for some seconds (max 10), then release."""
    return [text(do_hold(keys, seconds))]


@tool(_ACT)
def mouse_button(action: Literal["down", "up"], button: Literal["left", "right", "middle"] = "left"):
    """Press or release a mouse button without moving, for custom drags. Always release what you press."""
    return [text(do_mouse_button(action, button))]


@tool(_ACT)
def batch(actions: list[dict], screenshot_after: bool = True):
    """Run several actions in order, stopping at the first error.

    Each action is {"action": name, ...params}, where name is one of: move, click, drag,
    scroll, type, keys, hold_key, mouse_button, wait. Params match the single tools, e.g.
    {"action": "click", "x": 100, "y": 200}, {"action": "type", "text": "hello"},
    {"action": "keys", "keys": "return"}, {"action": "wait", "seconds": 1}.
    """
    done: list[str] = []
    for i, a in enumerate(actions, start=1):
        a = dict(a)
        name = a.pop("action", None)
        fn = ACTIONS.get(name)
        if fn is None:
            raise ToolError(f"Step {i}: unknown action {name!r}. Done so far: {done}")
        if name == "type" and "text" in a:
            a["text_"] = a.pop("text")
        try:
            done.append(f"{i}. " + fn(**a))
        except TypeError as e:
            raise ToolError(f"Step {i} ({name}) bad parameters: {e}. Done so far: {done}")
        except ToolError as e:
            raise ToolError(f"Step {i} ({name}) failed: {e} Done so far: {done}")
    return with_shot("\n".join(done), screenshot_after)


@tool(_READ)
def cursor_position():
    """Where the pointer is, in last-screenshot coordinates."""
    v = view()
    x, y = v.to_image(*pyautogui.position())
    return [text(json.dumps({"x": x, "y": y, "on_screenshot_display": 0 <= x <= v.img_w and 0 <= y <= v.img_h}))]


@tool(_READ)
def list_displays():
    """List connected displays (index 1 is the main display) with size and origin."""
    return [text(json.dumps([{"display": d.index, "width": d.width, "height": d.height,
                              "origin": [d.x, d.y], "main": d.index == 1}
                             for d in get_backend().displays()]))]


@tool(_READ)
def list_applications():
    """List running apps that have windows; the frontmost one is flagged."""
    apps = sorted(get_backend().applications(), key=lambda a: (not a["frontmost"], (a["name"] or "").lower()))
    return [text(json.dumps(apps, ensure_ascii=False))]


@tool(_SAFE)
def open_application(name: str, screenshot_after: bool = False):
    """Launch an app or bring it to the front.

    macOS: "Safari" or "com.apple.Safari". Windows: "notepad", "chrome", a window title, or a full path.
    """
    get_backend().open_app(name)
    time.sleep(0.8)
    return with_shot(f"Opened {name}.", screenshot_after)


@tool(_READ)
def list_windows(app: str | None = None):
    """List visible windows (front to back) with title and bounds in last-screenshot coordinates.

    app: optional app-name filter. On macOS, titles need Screen Recording permission.
    """
    v = view()
    out = []
    for w in get_backend().windows():
        if app and app.lower() not in (w["app"] or "").lower():
            continue
        x, y, wd, ht = w["rect"]
        x0, y0 = v.to_image(x, y)
        x1, y1 = v.to_image(x + wd, y + ht)
        out.append({"app": w["app"], "title": w["title"], "bounds": [x0, y0, x1, y1],
                    "on_screenshot_display": 0 <= x0 < v.img_w and 0 <= y0 < v.img_h})
    return [text(json.dumps(out, ensure_ascii=False))]


@tool(_READ)
def clipboard_get():
    """Read the clipboard's text."""
    return [text(get_backend().clipboard_get())]


@tool(_SAFE)
def clipboard_set(value: str):
    """Put text on the clipboard."""
    get_backend().clipboard_set(value)
    return [text(f"Clipboard set ({len(value)} characters).")]


@tool(_READ)
def wait(seconds: float = 1.0, screenshot_after: bool = False):
    """Wait for the UI to settle or something to load (max 60 s)."""
    return with_shot(do_wait(seconds), screenshot_after)


@tool(_READ)
def check_permissions():
    """Report whether the OS lets this server control input and capture the screen."""
    p = get_backend().permissions()
    p["version"] = VERSION
    if IS_MAC and not (p["accessibility"] and p["screen_recording"]):
        p["fix"] = (f"System Settings > Privacy & Security: enable '{p['granted_to']}' under Accessibility "
                    f"and Screen & System Audio Recording, then restart it.")
    return [text(json.dumps(p))]


# ---------------------------------------------------------------------------
# MCP over stdio (JSON-RPC 2.0, one message per line)
# ---------------------------------------------------------------------------

INSTRUCTIONS = """\
Computer Control operates this computer's real screen, mouse and keyboard.
- Start with `screenshot`. All x/y values are pixel coordinates in the most recent screenshot image.
- After acting, verify with a new screenshot (or screenshot_after=true) before the next step.
- Use `zoom` to read small text instead of guessing.
- Prefer keyboard shortcuts and `open_application` over hunting for icons.
- `batch` runs several actions in one call when you are sure of the steps.
- If a tool reports STOPPED (the user hit the emergency stop), stop and ask the user.
- Never enter passwords, payment details or other secrets; ask the user to do that themselves.
"""
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    entry = TOOLS.get(name)
    if entry is None:
        return {"content": [text(f"Unknown tool {name!r}.")], "isError": True}
    try:
        return {"content": entry["fn"](**(args or {})), "isError": False}
    except pyautogui.FailSafeException:
        msg = ("STOPPED: the user moved the mouse into a screen corner (emergency stop). Do not continue. "
               "Ask the user whether and how to proceed.")
    except ToolError as e:
        msg = str(e)
    except TypeError as e:
        msg = f"Bad arguments for {name}: {e}"
    except Exception as e:
        log("error in", name, repr(e))
        msg = f"{type(e).__name__}: {e}"
    return {"content": [text(msg)], "isError": True}


def handle(msg: dict[str, Any]) -> dict[str, Any] | None:
    method, mid = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}
    if mid is None or method is None:  # notifications and stray responses
        return None
    if method == "initialize":
        asked = params.get("protocolVersion")
        result: Any = {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "computer-control", "version": VERSION},
            "instructions": INSTRUCTIONS,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [t["spec"] for t in TOOLS.values()]}
    elif method == "tools/call":
        result = call_tool(params.get("name", ""), params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def send(obj: Any) -> None:
    _PROTOCOL_OUT.write(json.dumps(obj, ensure_ascii=False) + "\n")
    _PROTOCOL_OUT.flush()


def serve() -> None:
    log(f"v{VERSION} ready")
    if hasattr(_PROTOCOL_OUT, "reconfigure"):
        _PROTOCOL_OUT.reconfigure(encoding="utf-8")
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        if isinstance(msg, list):
            out = [r for r in (handle(m) for m in msg) if r]
            if out:
                send(out)
        elif isinstance(msg, dict):
            r = handle(msg)
            if r:
                send(r)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if "--version" in args:
        print(VERSION, file=_PROTOCOL_OUT)
    elif "--check" in args:
        b = get_backend()
        print(json.dumps({"version": VERSION, "python": sys.version.split()[0],
                          "pyautogui": pyautogui.__version__, "tools": len(TOOLS),
                          "displays": len(b.displays())}), file=_PROTOCOL_OUT)
    elif "--self-test" in args:
        b = get_backend()
        report: dict[str, Any] = {"version": VERSION, "python": sys.executable, **b.permissions()}
        report["displays"] = [vars(d) for d in b.displays()]
        try:
            _, w, h = b.screenshot(get_display(1), DEFAULT_MAX_SIZE, False, "jpeg")
            report["screenshot"] = {"ok": True, "size": [w, h]}
        except Exception as e:
            report["screenshot"] = {"ok": False, "error": str(e)}
        print(json.dumps(report, indent=2), file=_PROTOCOL_OUT)
        sys.exit(0 if report["screenshot"]["ok"] else 1)
    else:
        serve()


if __name__ == "__main__":
    main()
