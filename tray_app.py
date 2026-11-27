"""Windows system tray integration and single-instance lock for Elden Ring Co-op Tracker."""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import webbrowser
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable

STATE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "EldenRingCoopTracker"
URL_FILE = STATE_DIR / "url.txt"
MUTEX_NAME = "Local\\EldenRingCoopTracker_SingleInstance"
ERROR_ALREADY_EXISTS = 183


def _read_running_url(default_url: str = "http://127.0.0.1:8777") -> str:
    try:
        raw = URL_FILE.read_text(encoding="utf-8").strip()
        if raw.startswith(("http://", "https://")):
            return raw
    except OSError:
        pass
    return default_url


def write_running_url(url: str) -> None:
    """Persist active local server URL so re-launching the app opens the running tab."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        URL_FILE.write_text(url, encoding="utf-8")
    except OSError:
        pass


def clear_running_url() -> None:
    """Remove persisted URL file when the server shuts down."""
    try:
        if URL_FILE.exists():
            URL_FILE.unlink()
    except OSError:
        pass


def acquire_single_instance(skip_mutex: bool = False) -> tuple[bool, int | None]:
    """Ensure only one tray instance runs; if already running, open its browser URL."""
    if skip_mutex or sys.platform != "win32":
        return True, None

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    err = ctypes.get_last_error()
    if not handle:
        return True, None

    if err == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        webbrowser.open(_read_running_url())
        return False, None

    return True, int(handle)


def release_single_instance(mutex_handle: int | None) -> None:
    """Release the Windows named mutex handle on clean exit."""
    if not mutex_handle or sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(wintypes.HANDLE(mutex_handle))
    except Exception:
        pass


def _load_tray_icon(base_dir: Path) -> Any:
    """Load bundled app.ico for pystray or synthesize a gold badge fallback."""
    from PIL import Image, ImageDraw

    candidates = [
        base_dir / "app.ico",
        base_dir / "assets" / "app.ico",
    ]
    for path in candidates:
        if path.is_file():
            try:
                with Image.open(path) as im:
                    return im.convert("RGBA")
            except Exception:
                pass

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, 62, 62], fill="#14171c", outline="#d9a441", width=4)
    d.ellipse([16, 16, 48, 48], outline="#f0d79a", width=3)
    return img


def run_tray(
    server: Any,
    url: str,
    base_dir: Path,
    get_status_text: Callable[[], str],
    check_update_fn: Callable[..., dict[str, Any]],
    open_browser: bool = True,
    mutex_handle: int | None = None,
) -> None:
    """Run the HTTP server alongside a Windows system tray icon until Exit is clicked."""
    import pystray

    threading.Thread(target=server.serve_forever, daemon=True).start()
    write_running_url(url)

    if open_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()

    def on_open(icon: Any, item: Any) -> None:
        webbrowser.open(url)

    def on_check_updates(icon: Any, item: Any) -> None:
        def _worker() -> None:
            res = check_update_fn(force=True)
            if res.get("update_available"):
                latest = res.get("latest", "")
                rel_url = res.get("release_url", "")
                try:
                    icon.notify(
                        f"Доступна новая версия {latest}. Открываю страницу загрузки…",
                        "Elden Ring Co-op Tracker",
                    )
                except Exception:
                    pass
                if rel_url:
                    webbrowser.open(rel_url)
            else:
                cur = res.get("current", "")
                msg = f"Установлена актуальная версия ({cur})." if cur else "Установлена актуальная версия."
                try:
                    icon.notify(msg, "Elden Ring Co-op Tracker")
                except Exception:
                    pass

        threading.Thread(target=_worker, daemon=True).start()

    def on_exit(icon: Any, item: Any) -> None:
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Открыть карту", on_open, default=True),
        pystray.MenuItem(lambda item: get_status_text(), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Проверить обновления", on_check_updates),
        pystray.MenuItem("Выход", on_exit),
    )
    icon = pystray.Icon(
        "EldenRingCoopTracker",
        _load_tray_icon(base_dir),
        "Elden Ring Co-op Tracker",
        menu,
    )

    try:
        icon.run()
    finally:
        clear_running_url()
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
        release_single_instance(mutex_handle)
