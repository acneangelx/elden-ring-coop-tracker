"""Local HTTP + SSE live sync server for Elden Ring Seamless Co-op (.co2) map tracker."""

from __future__ import annotations

import argparse
import functools
import json
import mimetypes
import os
import posixpath
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import tray_app
from er_save_parser import SaveDataStore, discover_save_files, parse_save_state, read_slot_checksum

APP_VERSION = "1.3.0"
GITHUB_LATEST_RELEASE_API = "https://api.github.com/repos/acneangelx/elden-ring-coop-tracker/releases/latest"
GITHUB_RELEASES_URL = "https://github.com/acneangelx/elden-ring-coop-tracker/releases"
UPDATE_CACHE_TTL = 6 * 3600

BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
WEB_DIR = BASE_DIR / "web"
WEB_ZIP = BASE_DIR / "web.zip"

mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

_update_lock = threading.Lock()
_update_cache: tuple[float, dict[str, Any]] | None = None


def _parse_version(tag: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", tag))


def _check_github_update(force: bool = False) -> dict[str, Any]:
    """Query GitHub Releases once per 6 hours to detect newer tracker releases."""
    global _update_cache
    now = time.monotonic()
    if not force:
        with _update_lock:
            if _update_cache and (now - _update_cache[0]) < UPDATE_CACHE_TTL:
                return dict(_update_cache[1])

    result: dict[str, Any] = {
        "current": APP_VERSION,
        "latest": APP_VERSION,
        "update_available": False,
        "release_url": GITHUB_RELEASES_URL,
    }
    try:
        req = urllib.request.Request(
            GITHUB_LATEST_RELEASE_API,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"EldenRingCoopTracker/{APP_VERSION}",
            },
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        tag = str(payload.get("tag_name") or "").strip()
        html_url = str(payload.get("html_url") or GITHUB_RELEASES_URL).strip()
        cur_v = _parse_version(APP_VERSION)
        lat_v = _parse_version(tag)
        if lat_v and cur_v:
            result["latest"] = tag.lstrip("vV") or tag
            result["release_url"] = html_url
            result["update_available"] = lat_v > cur_v
    except Exception:
        pass

    with _update_lock:
        _update_cache = (time.monotonic(), dict(result))
    return result


class ZipAssetStore:
    """Thread-safe reader for bundled web.zip static assets."""

    def __init__(self, zip_path: Path) -> None:
        self._zf = zipfile.ZipFile(zip_path, "r")
        self._lock = threading.Lock()
        self._names = set(self._zf.namelist())

    def read(self, rel_path: str) -> bytes | None:
        clean = posixpath.normpath(rel_path.lstrip("/"))
        if clean in ("", "."):
            clean = "index.html"
        if clean.startswith("..") or clean not in self._names:
            return None
        with self._lock:
            return self._zf.read(clean)


class LiveStateManager:
    """Caches parsed save state and refreshes when mtime/size/checksum changes."""

    def __init__(self, store: SaveDataStore) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, int], tuple[int, int, str, dict[str, Any]]] = {}
        self._latest_state: dict[str, Any] | None = None

    def latest_summary(self) -> str:
        """Return a short one-line status summary for the system tray menu."""
        with self._lock:
            st = self._latest_state
        if not st:
            return "Ожидание сохранения .co2…"
        ch = st.get("character") or {}
        c = st.get("counts") or {}
        return f"{ch.get('name', '?')} (Ур. {ch.get('level', '?')}) · Благодати {c.get('graces', 0)} · Боссы {c.get('bosses', 0)}"

    def default_save_path(self) -> str | None:
        saves = discover_save_files()
        return saves[0]["path"] if saves else None

    def get_state(self, file_path: str | None, slot: int = 0, force: bool = False) -> dict[str, Any]:
        target = file_path or self.default_save_path()
        if not target or not os.path.exists(target):
            return {"ok": False, "error": "Файл сохранения (.co2/.sl2) не найден."}

        try:
            st = os.stat(target)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

        checksum = read_slot_checksum(target, slot)
        key = (target, slot)
        if not force:
            with self._lock:
                cached = self._cache.get(key)
                if (
                    cached
                    and cached[0] == st.st_mtime_ns
                    and cached[1] == st.st_size
                    and cached[2] == checksum
                ):
                    self._latest_state = cached[3]
                    return {"ok": True, **cached[3]}

        try:
            state = parse_save_state(self.store, target, slot)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        with self._lock:
            self._cache[key] = (
                int(state["mtime_ns"]),
                int(state["size"]),
                str(state.get("checksum", checksum)),
                state,
            )
            self._latest_state = state
        return {"ok": True, **state}


class TrackerHTTPHandler(SimpleHTTPRequestHandler):
    """Serves static map files (disk or web.zip) and JSON/SSE live save-sync endpoints."""

    def __init__(
        self,
        *args: Any,
        state_mgr: LiveStateManager,
        zip_store: ZipAssetStore | None = None,
        **kwargs: Any,
    ) -> None:
        self.state_mgr = state_mgr
        self.zip_store = zip_store
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        if args and str(args[1]).startswith(("4", "5")):
            super().log_message(format, *args)

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_zip_asset(self, req_path: str) -> None:
        assert self.zip_store is not None
        rel = urllib.parse.unquote(req_path)
        data = self.zip_store.read(rel)
        if data is None:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        target_name = "index.html" if rel in ("", "/") else rel
        ctype, _ = mimetypes.guess_type(target_name)
        if not ctype:
            ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype = f"{ctype}; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/api/saves":
            saves = discover_save_files()
            self._send_json({"saves": saves})
            return

        if parsed.path == "/api/state":
            file_path = qs.get("file", [None])[0]
            slot_raw = qs.get("slot", ["0"])[0]
            force = qs.get("force", ["0"])[0] in ("1", "true", "yes")
            try:
                slot = max(0, min(9, int(slot_raw)))
            except ValueError:
                slot = 0
            self._send_json(self.state_mgr.get_state(file_path, slot, force=force))
            return

        if parsed.path == "/api/events":
            self._handle_sse(qs)
            return

        if parsed.path == "/api/update-check":
            force = qs.get("force", ["0"])[0] in ("1", "true", "yes")
            self._send_json(_check_github_update(force=force))
            return

        if self.zip_store is not None:
            self._serve_zip_asset(parsed.path)
            return

        super().do_GET()

    def _handle_sse(self, qs: dict[str, list[str]]) -> None:
        file_path = qs.get("file", [None])[0]
        slot_raw = qs.get("slot", ["0"])[0]
        try:
            slot = max(0, min(9, int(slot_raw)))
        except ValueError:
            slot = 0

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_sig: tuple[str, int, int, str] | None = None
        last_ping = time.monotonic()

        try:
            while True:
                target = file_path or self.state_mgr.default_save_path()
                sig: tuple[str, int, int, str] | None = None
                if target and os.path.exists(target):
                    try:
                        st = os.stat(target)
                        sig = (target, st.st_mtime_ns, st.st_size, read_slot_checksum(target, slot))
                    except OSError:
                        sig = None

                if sig != last_sig:
                    payload = self.state_mgr.get_state(target, slot)
                    data_str = json.dumps(payload, ensure_ascii=False)
                    self.wfile.write(f"event: state\ndata: {data_str}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    if payload.get("ok"):
                        last_sig = sig
                    last_ping = time.monotonic()
                elif time.monotonic() - last_ping >= 15.0:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_ping = time.monotonic()

                time.sleep(0.8)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return


class QuietHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server whose worker/SSE threads exit cleanly on shutdown."""

    daemon_threads = True


def _bind_server(host: str, start_port: int, handler_cls: Any) -> tuple[QuietHTTPServer, int]:
    for port in range(start_port, start_port + 11):
        try:
            return QuietHTTPServer((host, port), handler_cls), port
        except OSError:
            continue
    raise OSError(f"Не удалось занять ни один порт в диапазоне {start_port}..{start_port + 10}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Elden Ring Seamless Co-op Live Map Tracker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--console", action="store_true", help="Run in console mode instead of system tray")
    parser.add_argument("--tray", action="store_true", help="Run as a Windows system tray app")
    args = parser.parse_args()

    use_tray = (getattr(sys, "frozen", False) and not args.console) or args.tray
    mutex_handle: int | None = None
    if use_tray:
        ok, mutex_handle = tray_app.acquire_single_instance(skip_mutex=args.no_browser)
        if not ok:
            return

    store = SaveDataStore(BASE_DIR)
    state_mgr = LiveStateManager(store)
    zip_store = ZipAssetStore(WEB_ZIP) if (not WEB_DIR.is_dir() and WEB_ZIP.is_file()) else None
    handler = functools.partial(TrackerHTTPHandler, state_mgr=state_mgr, zip_store=zip_store)
    server, actual_port = _bind_server(args.host, args.port, handler)

    url = f"http://{args.host}:{actual_port}"
    if use_tray:
        tray_app.run_tray(
            server=server,
            url=url,
            base_dir=BASE_DIR,
            get_status_text=state_mgr.latest_summary,
            check_update_fn=_check_github_update,
            open_browser=not args.no_browser,
            mutex_handle=mutex_handle,
        )
        return

    print("=" * 60)
    print(f"  Elden Ring Seamless Co-op (.co2) Live Tracker v{APP_VERSION}")
    print(f"  Карта открыта по адресу: {url}")
    print("  Окно можно свернуть, но не закрывайте его во время игры.")
    print("  Для остановки нажмите Ctrl+C или закройте это окно.")
    print("=" * 60)

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка сервера...")
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
