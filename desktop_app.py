"""FormFlow Desktop App & Always-On Background Tray Service (Windows).

Runs FormFlow as a native-feeling local Windows desktop application:
- Keeps the Inbox watcher, background worker, and local-only Waitress HTTP
  server (127.0.0.1:5000) running silently in the background with a Windows
  System Tray icon (even when the app window is closed).
- Opens the UI in a clean standalone app window (Edge/Chrome --app mode:
  no address bar, no browser tabs, native PDF preview, downloads, and Copy).
- Single-instance safe: double-clicking the FormFlow shortcut while already
  running simply opens/focuses the app window instead of starting a second
  watcher or failing on port 5000.
- Supports per-user Windows login Auto-Start (HKCU Run, no Admin needed)
  plus one-click tray actions: Open FormFlow, Open Inbox Folder,
  Copy Chrome Extension Token, Start with Windows toggle, and Quit.

CLI modes:
    pythonw.exe desktop_app.py                Start service+tray & open app window
    pythonw.exe desktop_app.py --background   Start service+tray silently (Auto-Start)
    python.exe  desktop_app.py --install      Create Inbox, shortcuts, and Auto-Start
    python.exe  desktop_app.py --uninstall    Remove Auto-Start and Desktop shortcuts
    python.exe  desktop_app.py --copy-token   Copy local Chrome extension token
"""
from __future__ import annotations

import argparse
import ctypes
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

APP_NAME = "FormFlow"
APP_ROOT = Path(__file__).resolve().parent
AUTOSTART_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE_NAME = "FormFlow"


def _configure_logging(data_dir: Path) -> logging.Logger:
    """Redirect logs to rotating data/logs/desktop.log (safe under pythonw.exe)."""
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "desktop.log"

    # Under pythonw.exe, sys.stdout and sys.stderr can be None; guard safely.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    logger = logging.getLogger("formflow.desktop")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            log_file, maxBytes=512 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
    return logger


def server_url(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}"


def is_server_running(port: int, timeout: float = 0.6) -> bool:
    """Return True if a local FormFlow instance is already answering /healthz."""
    try:
        with urllib.request.urlopen(f"{server_url(port)}/healthz", timeout=timeout) as resp:
            return 200 <= int(resp.status) < 400
    except Exception:
        return False


def wait_for_server(port: int, timeout: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_server_running(port, timeout=0.4):
            return True
        time.sleep(0.15)
    return False


def find_app_browser() -> tuple[str, Path] | None:
    """Locate Microsoft Edge or Google Chrome for standalone --app window mode.

    Prefers Microsoft Edge so FormFlow gets its own clean taskbar group separate
    from the operator's Chrome Sarathi tabs; falls back to Chrome if Edge is absent.
    """
    local_app = os.environ.get("LOCALAPPDATA", "")
    prog_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    prog_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")

    candidates: list[tuple[str, Path]] = [
        ("edge", Path(prog_x86) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
        ("edge", Path(prog_files) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
        ("edge", Path(local_app) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
        ("chrome", Path(prog_files) / "Google" / "Chrome" / "Application" / "chrome.exe"),
        ("chrome", Path(prog_x86) / "Google" / "Chrome" / "Application" / "chrome.exe"),
        ("chrome", Path(local_app) / "Google" / "Chrome" / "Application" / "chrome.exe"),
    ]
    for kind, p in candidates:
        if p.is_file():
            return kind, p

    for kind, exe in (("edge", "msedge"), ("chrome", "chrome")):
        found = shutil.which(exe)
        if found:
            return kind, Path(found)
    return None


def build_app_window_args(
    browser_kind: str, browser_exe: Path, url: str, data_dir: Path
) -> list[str]:
    """Build Chromium --app command line for a clean frameless-chrome desktop window."""
    profile_dir = data_dir / "app_window_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        str(browser_exe),
        f"--app={url}",
        f"--user-data-dir={profile_dir}",
        "--window-size=1280,840",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if browser_kind == "edge":
        args.append("--disable-features=msEdgeSidebarV2")
    return args


def open_app_window(url: str, data_dir: Path) -> bool:
    """Open FormFlow in a standalone native app window (or default browser fallback)."""
    found = find_app_browser()
    if found is not None:
        kind, exe = found
        args = build_app_window_args(kind, exe, url, data_dir)
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
        try:
            subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            return True
        except OSError:
            pass
    try:
        webbrowser.open(url)
        return True
    except Exception:
        return False


def _preferred_pythonw(app_root: Path = APP_ROOT) -> Path:
    """Resolve silent pythonw.exe (bundled runtime -> local venv -> current interpreter)."""
    for cand in (
        app_root / "runtime" / "pythonw.exe",
        app_root / ".venv311" / "Scripts" / "pythonw.exe",
        app_root / ".venv" / "Scripts" / "pythonw.exe",
        Path(sys.executable).with_name("pythonw.exe"),
    ):
        if cand.is_file():
            return cand
    return Path(sys.executable)


def autostart_command(app_root: Path = APP_ROOT) -> str:
    """Command registered in HKCU Run so FormFlow watches Inbox on Windows login."""
    pyw = _preferred_pythonw(app_root)
    script = app_root / "desktop_app.py"
    return f'"{pyw}" "{script}" --background'


def is_autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH, 0, winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, AUTOSTART_VALUE_NAME)
            return bool(str(val).strip())
    except OSError:
        return False


def set_autostart(enabled: bool, app_root: Path = APP_ROOT) -> bool:
    """Enable or disable per-user Windows login auto-start (zero Admin rights needed)."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH, 0, winreg.KEY_SET_VALUE
        ) as k:
            if enabled:
                winreg.SetValueEx(
                    k, AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ, autostart_command(app_root)
                )
            else:
                try:
                    winreg.DeleteValue(k, AUTOSTART_VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


def copy_text_to_clipboard(text: str) -> bool:
    """Copy text to Windows clipboard directly via Win32 API (never logs/echoes secrets)."""
    if sys.platform != "win32" or not text:
        return False
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_int
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_int
    user32.EmptyClipboard.restype = ctypes.c_int
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.restype = ctypes.c_int

    raw = text.encode("utf-16-le") + b"\x00\x00"
    hmem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(raw))
    if not hmem:
        return False
    ptr = kernel32.GlobalLock(hmem)
    if not ptr:
        kernel32.GlobalFree(hmem)
        return False
    ctypes.memmove(ptr, raw, len(raw))
    kernel32.GlobalUnlock(hmem)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(hmem)
        return False
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, hmem):
            kernel32.GlobalFree(hmem)
            return False
        return True
    finally:
        user32.CloseClipboard()


def read_or_create_api_token(data_dir: Path) -> str:
    """Return the local API token from data/.api_token (creating it if needed)."""
    import secrets

    data_dir.mkdir(parents=True, exist_ok=True)
    token_file = data_dir / ".api_token"
    if token_file.is_file():
        val = token_file.read_text(encoding="utf-8").strip()
        if len(val) >= 32:
            return val
    token = secrets.token_hex(32)
    token_file.write_text(token, encoding="utf-8")
    return token


def _desktop_dir() -> Path:
    """Resolve the user's real Windows Desktop path (works with OneDrive Desktop too)."""
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as k:
                raw, _ = winreg.QueryValueEx(k, "Desktop")
                expanded = Path(os.path.expandvars(str(raw)))
                if expanded.is_dir():
                    return expanded
        except OSError:
            pass
    return Path.home() / "Desktop"


def _create_windows_shortcut(
    lnk_path: Path,
    target: Path,
    *,
    arguments: str = "",
    working_dir: Path | None = None,
    icon: Path | None = None,
    description: str = "",
) -> bool:
    """Create a Windows .lnk shortcut via WScript.Shell COM (no external deps)."""
    if sys.platform != "win32":
        return False

    def _ps_quote(s: str) -> str:
        return "'" + s.replace("'", "''") + "'"

    lines = [
        "$ws = New-Object -ComObject WScript.Shell",
        f"$sc = $ws.CreateShortcut({_ps_quote(str(lnk_path))})",
        f"$sc.TargetPath = {_ps_quote(str(target))}",
    ]
    if arguments:
        lines.append(f"$sc.Arguments = {_ps_quote(arguments)}")
    if working_dir is not None:
        lines.append(f"$sc.WorkingDirectory = {_ps_quote(str(working_dir))}")
    if icon is not None and icon.is_file():
        lines.append(f"$sc.IconLocation = {_ps_quote(str(icon) + ',0')}")
    if description:
        lines.append(f"$sc.Description = {_ps_quote(description)}")
    lines.append("$sc.Save()")

    res = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "; ".join(lines)],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return res.returncode == 0 and lnk_path.is_file()


def install_desktop_integration(app_root: Path = APP_ROOT) -> dict[str, object]:
    """Create Inbox, Desktop shortcuts (FormFlow + FormFlow Inbox), and Auto-Start."""
    from app.config import load_config

    cfg = load_config()
    cfg.ensure_data_dirs()
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
    read_or_create_api_token(cfg.data_dir)

    desktop = _desktop_dir()
    desktop.mkdir(parents=True, exist_ok=True)
    pyw = _preferred_pythonw(app_root)
    ico = app_root / "app" / "static" / "favicon.ico"

    app_lnk = desktop / "FormFlow.lnk"
    inbox_lnk = desktop / "FormFlow Inbox.lnk"

    ok_app = _create_windows_shortcut(
        app_lnk,
        pyw,
        arguments=f'"{app_root / "desktop_app.py"}"',
        working_dir=app_root,
        icon=ico if ico.is_file() else None,
        description="FormFlow - Offline Driving Licence Desk Assistant",
    )
    ok_inbox = _create_windows_shortcut(
        inbox_lnk,
        cfg.inbox_dir,
        description="Drop customer photos, signatures, Aadhaar, LL, TR files here",
    )
    ok_auto = set_autostart(True, app_root=app_root)
    return {
        "inbox_dir": str(cfg.inbox_dir),
        "extension_dir": str(app_root / "extension"),
        "app_shortcut": str(app_lnk) if ok_app else None,
        "inbox_shortcut": str(inbox_lnk) if ok_inbox else None,
        "autostart_enabled": ok_auto,
    }


def uninstall_desktop_integration() -> dict[str, bool]:
    """Remove Windows Auto-Start and Desktop shortcuts (never deletes Inbox/data)."""
    removed_auto = set_autostart(False)
    desktop = _desktop_dir()
    removed_shortcuts = True
    for name in ("FormFlow.lnk", "FormFlow Inbox.lnk"):
        p = desktop / name
        if p.exists():
            try:
                p.unlink()
            except OSError:
                removed_shortcuts = False
    return {"autostart_removed": removed_auto, "shortcuts_removed": removed_shortcuts}


def _load_tray_icon_image(app_root: Path = APP_ROOT):
    from PIL import Image, ImageDraw

    for rel in ("extension/icons/icon48.png", "extension/icons/icon128.png"):
        p = app_root / rel
        if p.is_file():
            return Image.open(p).convert("RGBA")
    img = Image.new("RGBA", (64, 64), (15, 76, 129, 255))
    dr = ImageDraw.Draw(img)
    dr.rounded_rectangle([4, 4, 60, 60], radius=12, fill=(15, 76, 129, 255))
    dr.text((16, 20), "FF", fill=(255, 255, 255, 255))
    return img


def run_desktop_service(*, open_window: bool = True) -> int:
    """Start background watcher + Waitress server + System Tray icon."""
    from app import create_app
    from app.config import load_config
    from run import create_http_server, start_background

    cfg = load_config()
    cfg.ensure_data_dirs()
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
    logger = _configure_logging(cfg.data_dir)

    url = server_url(cfg.port)
    if is_server_running(cfg.port):
        logger.info("FormFlow already running on %s", url)
        if open_window:
            open_app_window(url, cfg.data_dir)
        return 0

    flask_app = create_app()
    cfg = flask_app.config["FORMFLOW"]
    read_or_create_api_token(cfg.data_dir)

    worker, watcher = None, None
    try:
        worker, watcher = start_background(flask_app)
        logger.info("Inbox watcher active on %s", cfg.inbox_dir)
    except Exception as e:
        logger.error("Inbox watcher failed to start: %s", e)

    http_server = create_http_server(flask_app)
    if http_server is None:
        logger.error("Waitress WSGI server unavailable; falling back to Flask dev server")

    def _serve() -> None:
        try:
            if http_server is not None:
                http_server.run()
            else:
                flask_app.run(host="127.0.0.1", port=cfg.port, debug=False, use_reloader=False)
        except Exception as exc:
            logger.error("HTTP server stopped: %s", exc)

    server_thread = threading.Thread(target=_serve, name="formflow-http", daemon=True)
    server_thread.start()

    ready = wait_for_server(cfg.port, timeout=12.0)
    logger.info("FormFlow server ready=%s at %s", ready, url)
    if open_window and ready:
        open_app_window(url, cfg.data_dir)

    def _shutdown() -> None:
        logger.info("Shutting down FormFlow service...")
        if http_server is not None:
            try:
                http_server.close()
            except Exception:
                pass
        if watcher is not None:
            try:
                watcher.stop()
            except Exception:
                pass
        if worker is not None:
            try:
                worker.stop()
            except Exception:
                pass

    try:
        import pystray
    except ImportError:
        logger.warning("pystray not installed; running headless until interrupted")
        try:
            server_thread.join()
        except KeyboardInterrupt:
            pass
        finally:
            _shutdown()
        return 0

    def _on_open(icon, item) -> None:
        open_app_window(url, cfg.data_dir)

    def _on_inbox(icon, item) -> None:
        cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(cfg.inbox_dir))  # type: ignore[attr-defined]
        except Exception as e:
            logger.warning("Could not open Inbox folder: %s", e)

    def _on_copy_token(icon, item) -> None:
        token = read_or_create_api_token(cfg.data_dir)
        copied = copy_text_to_clipboard(token)
        try:
            icon.notify(
                "Paste into FormFlow Chrome Extension Settings."
                if copied
                else "Open data\\.api_token to copy the extension token.",
                "FormFlow Extension Token Copied" if copied else "FormFlow Token",
            )
        except Exception:
            pass

    def _on_toggle_autostart(icon, item) -> None:
        set_autostart(not is_autostart_enabled(), app_root=APP_ROOT)

    def _on_check_updates(icon, item) -> None:
        def _worker() -> None:
            from app.services.updater import UpdateError, check_for_update, download_patch
            from app.version import APP_VERSION

            info = check_for_update(current_version=APP_VERSION)
            if info.error:
                try:
                    icon.notify(info.error, "FormFlow Update Check")
                except Exception:
                    pass
                return
            if not info.available:
                try:
                    icon.notify(f"You already have the latest version (v{APP_VERSION}).", "FormFlow Up to Date")
                except Exception:
                    pass
                return
            try:
                icon.notify(
                    f"Downloading FormFlow v{info.latest_version} (~{max(1, info.patch_bytes // 1024)} KB)...",
                    "Updating FormFlow",
                )
            except Exception:
                pass
            try:
                patch_zip = download_patch(info, cfg.data_dir / ".update_tmp")
            except UpdateError as exc:
                logger.warning("Update download failed: %s", exc)
                try:
                    icon.notify(str(exc), "FormFlow Update Failed")
                except Exception:
                    pass
                return
            pyw = _preferred_pythonw(APP_ROOT)
            creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )
            subprocess.Popen(
                [
                    str(pyw),
                    str(APP_ROOT / "desktop_app.py"),
                    "--apply-patch",
                    str(patch_zip),
                    "--sha256",
                    info.patch_sha256,
                    "--wait-pid",
                    str(os.getpid()),
                ],
                cwd=str(APP_ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            icon.stop()

        threading.Thread(target=_worker, name="formflow-update-check", daemon=True).start()

    def _on_quit(icon, item) -> None:
        icon.stop()

    from app.version import APP_VERSION

    menu = pystray.Menu(
        pystray.MenuItem("Open FormFlow", _on_open, default=True),
        pystray.MenuItem("Open Inbox Folder", _on_inbox),
        pystray.MenuItem("Copy Chrome Extension Token", _on_copy_token),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(f"Check for Updates (v{APP_VERSION})", _on_check_updates),
        pystray.MenuItem(
            "Start with Windows",
            _on_toggle_autostart,
            checked=lambda item: is_autostart_enabled(),
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit FormFlow", _on_quit),
    )

    tray_icon = pystray.Icon(
        APP_NAME,
        _load_tray_icon_image(APP_ROOT),
        f"FormFlow v{APP_VERSION} — Watching Inbox",
        menu,
    )

    def _silent_startup_update_check() -> None:
        time.sleep(8.0)
        try:
            from app.services.updater import check_for_update

            info = check_for_update(current_version=APP_VERSION, timeout=5.0)
            if info.available:
                tray_icon.notify(
                    f"FormFlow v{info.latest_version} is available. Right-click tray -> Check for Updates.",
                    "FormFlow Update Available",
                )
        except Exception:
            pass

    threading.Thread(target=_silent_startup_update_check, name="formflow-startup-upd", daemon=True).start()
    try:
        tray_icon.run()
    finally:
        _shutdown()
    return 0


def _wait_for_pid_exit(pid: int, timeout: float = 15.0) -> None:
    if pid <= 0 or sys.platform != "win32":
        time.sleep(1.0)
        return
    SYNCHRONIZE = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
    if not handle:
        return
    try:
        ctypes.windll.kernel32.WaitForSingleObject(handle, int(timeout * 1000))
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
    time.sleep(0.4)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FormFlow Windows Desktop App & Tray Service")
    parser.add_argument(
        "--background",
        action="store_true",
        help="Start background watcher, local server, and tray icon without opening a window.",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Create Inbox, Desktop shortcuts, and Windows login Auto-Start.",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove Windows login Auto-Start and Desktop shortcuts (keeps data and Inbox).",
    )
    parser.add_argument(
        "--copy-token",
        action="store_true",
        help="Copy the local Chrome extension API token to the Windows clipboard.",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Check GitHub Releases and apply the latest lightweight delta patch if available.",
    )
    parser.add_argument("--apply-patch", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--sha256", default="", help=argparse.SUPPRESS)
    parser.add_argument("--wait-pid", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.apply_patch:
        from app.config import load_config
        from app.services.updater import apply_patch_update

        cfg = load_config()
        logger = _configure_logging(cfg.data_dir)
        _wait_for_pid_exit(args.wait_pid)
        try:
            res = apply_patch_update(
                APP_ROOT,
                Path(args.apply_patch),
                args.sha256,
                data_dir=cfg.data_dir,
                verify_boot=True,
            )
            logger.info("Applied patch update cleanly: %s files updated", res["updated_files"])
        except Exception as exc:
            logger.error("Patch apply failed: %s", exc)
        pyw = _preferred_pythonw(APP_ROOT)
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
        subprocess.Popen(
            [str(pyw), str(APP_ROOT / "desktop_app.py"), "--background"],
            cwd=str(APP_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        return 0

    if args.update:
        from app.config import load_config
        from app.services.updater import UpdateError, apply_patch_update, check_for_update, download_patch
        from app.version import APP_VERSION

        cfg = load_config()
        print(f"Checking for updates (current: v{APP_VERSION})...")
        info = check_for_update(current_version=APP_VERSION)
        if info.error:
            print(f"Could not check updates: {info.error}")
            return 1
        if not info.available:
            print(f"FormFlow v{APP_VERSION} is already up to date.")
            return 0
        print(f"New version available: v{info.latest_version}. Downloading delta patch...")
        try:
            patch_zip = download_patch(info, cfg.data_dir / ".update_tmp")
            res = apply_patch_update(
                APP_ROOT, patch_zip, info.patch_sha256, data_dir=cfg.data_dir, verify_boot=True
            )
            print(f"Updated to v{info.latest_version} ({res['updated_files']} files updated).")
            return 0
        except UpdateError as exc:
            print(f"Update failed: {exc}")
            return 1

    if args.install:
        info = install_desktop_integration(APP_ROOT)
        print("FormFlow installed:")
        print(f"  Inbox folder    : {info['inbox_dir']}")
        print(f"  Extension folder: {info['extension_dir']}")
        print(f"  Auto-Start      : {'Enabled' if info['autostart_enabled'] else 'Not enabled'}")
        if info.get("app_shortcut"):
            print(f"  Desktop app     : {info['app_shortcut']}")
        if info.get("inbox_shortcut"):
            print(f"  Desktop Inbox   : {info['inbox_shortcut']}")
        return 0

    if args.uninstall:
        res = uninstall_desktop_integration()
        print("FormFlow desktop integration removed (Inbox and data folders were kept).")
        print(f"  Auto-Start removed: {res['autostart_removed']}")
        return 0

    if args.copy_token:
        from app.config import load_config

        cfg = load_config()
        tok = read_or_create_api_token(cfg.data_dir)
        ok = copy_text_to_clipboard(tok)
        print("Extension token copied to clipboard." if ok else "Could not access clipboard.")
        return 0 if ok else 1

    return run_desktop_service(open_window=not args.background)


if __name__ == "__main__":
    raise SystemExit(main())
