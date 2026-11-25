from __future__ import annotations

import base64
import binascii
import functools
import http.client
import http.server
import inspect
import io
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import Any

try:
    import winreg
except ImportError:  # pragma: no cover - NVDA add-on is Windows-only.
    winreg = None  # type: ignore

try:
    import config  # type: ignore
except Exception:  # pragma: no cover - used by standalone smoke tests.
    config = None  # type: ignore

try:
    import addonHandler

    addonHandler.initTranslation()
except Exception:  # pragma: no cover - used by standalone smoke tests.

    def _(message: str) -> str:
        return message


from . import audio_math, voice_store
from .catalog import ENGINE_DIR, ENGINE_VERSION, VoiceCatalog, is_package_supported_by_engine
from .language_utils import get_nvda_logger, is_debug_for_synth_driver
from .websocketClientRepo import websocket

log = get_nvda_logger()

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
SYNTH_NAME = "googleTtsForNvda"
BINDING_NAME = "googleTtsForNvdaBridge"
SAMPLE_RATE = 24000
STARTUP_POLL_INTERVAL = 0.01
_STARTUP_STDERR_MAX_BYTES = 4096
_STARTUP_STDERR_SNIPPET_MAX_CHARS = 500
BROWSER_WINDOW_HIDE_POLL_INTERVAL_SECONDS = 0.15
STOP_EXPRESSION = "window.googleTtsForNvdaStop && window.googleTtsForNvdaStop()"
LOCAL_CACHE_DIR_NAME = "googleTtsForNvda"
CHROME_PROFILE_DIR_NAME = "chromeProfiles"
EDGE_PROFILE_DIR_NAME = "edgeProfiles"
BRAVE_PROFILE_DIR_NAME = "braveProfiles"
PERSISTENT_PROFILE_DIR_NAME = "persistentSession"
PERSISTENT_PROFILE_MAX_BYTES = 500 * 1024 * 1024
PERSISTENT_PROFILE_SIZE_CHECK_FILE_NAME = "profileSizeCheck.json"
PERSISTENT_PROFILE_SIZE_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
RUNTIME_MEMORY_STARTUP_GRACE_SECONDS = 90
RUNTIME_MEMORY_CHECK_INTERVAL_SECONDS = 30
RUNTIME_PRIVATE_BYTES_RECYCLE_THRESHOLD = 1280 * 1024 * 1024
RUNTIME_WORKING_SET_BYTES_RECYCLE_THRESHOLD = 1024 * 1024 * 1024
RUNTIME_MEMORY_RECYCLE_CONFIRMATIONS = 2
CONFIG_SECTION = "googleTtsForNvda"
CONFIG_BROWSER_RUNTIME = "browserRuntime"
CONFIG_AUTO_LANGUAGE_DETECTION = "autoLanguageDetection"
CONFIG_AUTO_LANGUAGE_PREFERRED = "autoLanguagePreferred"
CONFIG_AUTO_LANGUAGE_CANDIDATES = "autoLanguageCandidates"
CONFIG_AUTO_LANGUAGE_PROFILES = "autoLanguageProfiles"
CONFIG_AUTO_UPDATE_CHECK = "autoUpdateCheckOnStartup"
CONFIG_KEEP_BROWSER_RUNTIME_READY = "keepBrowserRuntimeReady"
BROWSER_RUNTIME_EDGE = "edge"
BROWSER_RUNTIME_CHROME = "chrome"
BROWSER_RUNTIME_BRAVE = "brave"
DEFAULT_BROWSER_RUNTIME = BROWSER_RUNTIME_CHROME
DEFAULT_AUTO_LANGUAGE_DETECTION = False
DEFAULT_AUTO_LANGUAGE_PREFERRED = ""
DEFAULT_AUTO_LANGUAGE_CANDIDATES = ""
DEFAULT_AUTO_LANGUAGE_PROFILES = ""
DEFAULT_AUTO_UPDATE_CHECK = False
DEFAULT_KEEP_BROWSER_RUNTIME_READY = False
CONFIG_SPEC: dict[str, str] = {
    CONFIG_AUTO_LANGUAGE_CANDIDATES: f"string(default={DEFAULT_AUTO_LANGUAGE_CANDIDATES})",
    CONFIG_AUTO_LANGUAGE_DETECTION: f"boolean(default={str(DEFAULT_AUTO_LANGUAGE_DETECTION).lower()})",
    CONFIG_AUTO_LANGUAGE_PREFERRED: f"string(default={DEFAULT_AUTO_LANGUAGE_PREFERRED})",
    CONFIG_AUTO_LANGUAGE_PROFILES: f"string(default={DEFAULT_AUTO_LANGUAGE_PROFILES})",
    CONFIG_AUTO_UPDATE_CHECK: f"boolean(default={str(DEFAULT_AUTO_UPDATE_CHECK).lower()})",
    CONFIG_BROWSER_RUNTIME: f"string(default={DEFAULT_BROWSER_RUNTIME})",
    CONFIG_KEEP_BROWSER_RUNTIME_READY: f"boolean(default={str(DEFAULT_KEEP_BROWSER_RUNTIME_READY).lower()})",
}
BROWSER_RUNTIME_LABELS = {
    BROWSER_RUNTIME_EDGE: "Microsoft Edge",
    BROWSER_RUNTIME_CHROME: "Google Chrome",
    BROWSER_RUNTIME_BRAVE: "Brave",
}
BROWSER_RUNTIMES = (BROWSER_RUNTIME_CHROME, BROWSER_RUNTIME_EDGE, BROWSER_RUNTIME_BRAVE)


def ensure_config_spec() -> None:
    """Register the add-on config spec and evict any stale unvalidated section cache.

    NVDA initializes speech (core.py:speech.initialize) before global plugins
    (core.py:globalPluginHandler.initialize). If SynthDriver.__init__ accesses
    config.conf[CONFIG_SECTION] before config.conf.spec[CONFIG_SECTION] is set,
    NVDA's AggregatedSection.__getitem__ caches KeyError (or an empty _spec) on
    rootSection._cache[CONFIG_SECTION], causing subsequent config reads/writes
    to raise KeyError even after the global plugin sets config.conf.spec.
    """
    if config is None:
        return
    conf = getattr(config, "conf", None)
    if conf is None:
        return
    spec = getattr(conf, "spec", None)
    if spec is None:
        return
    existing = spec.get(CONFIG_SECTION) if hasattr(spec, "get") else None
    specChanged = not isinstance(existing, dict)
    if isinstance(existing, dict):
        for key, value in CONFIG_SPEC.items():
            if existing.get(key) != value:
                specChanged = True
                break
    try:
        spec[CONFIG_SECTION] = dict(CONFIG_SPEC)
    except Exception:
        log.debugWarning("Could not register Google TTS config spec.", exc_info=True)
        return
    for holder in (conf, getattr(conf, "rootSection", None)):
        if holder is None:
            continue
        cache = getattr(holder, "_cache", None)
        if not isinstance(cache, dict) or CONFIG_SECTION not in cache:
            continue
        cachedValue = cache.get(CONFIG_SECTION)
        cachedSpec = getattr(cachedValue, "_spec", None)
        if specChanged or cachedValue is KeyError or isinstance(cachedValue, type) or not cachedSpec:
            cache.pop(CONFIG_SECTION, None)


ensure_config_spec()


class CdpError(Exception):
    def __init__(
        self,
        message: str,
        technicalDetail: str | None = None,
        *,
        audioStarted: bool = False,
    ) -> None:
        super().__init__(message)
        self.technicalDetail = technicalDetail
        self.audioStarted = audioStarted


class _BrowserSpeechError(CdpError):
    """Speech failure reported by the browser harness."""

    def __init__(self, message: str, technicalDetail: str | None, *, audioStarted: bool) -> None:
        super().__init__(message, technicalDetail, audioStarted=audioStarted)


class _BrowserProfileInUseError(CdpError):
    pass


class CdpCancelled(Exception):
    pass


AudioCallback = Callable[[bytes], None]
MarkCallback = Callable[..., None]
SegmentEndCallback = Callable[[], None]


def _mark_callback_accepts_sample_offset(callback: MarkCallback | None) -> bool:
    if callback is None:
        return False
    try:
        params = list(inspect.signature(callback).parameters.values())
    except (TypeError, ValueError):
        return False
    positional = 0
    for param in params:
        if param.kind == inspect.Parameter.VAR_POSITIONAL:
            return True
        if param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            positional += 1
    return positional >= 2


class _ThreadingTcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc_type, _exc_value, _tb = sys.exc_info()
        if exc_type is not None and issubclass(exc_type, (ConnectionResetError, BrokenPipeError, OSError)):
            return
        super().handle_error(request, client_address)

    def finish_request(self, request: Any, client_address: Any) -> None:
        with suppress(ConnectionResetError, BrokenPipeError, OSError):
            super().finish_request(request, client_address)


_NO_STORE_BRIDGE_ROUTES = frozenset({"/", "/index.html", "/voices.json", "/bridgeHarness.js"})


class _BridgeRequestHandler(http.server.SimpleHTTPRequestHandler):
    server_version = "GoogleTtsForNvda"
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".wasm": "application/wasm",
        ".js": "application/javascript",
        ".mjs": "application/javascript",
        ".json": "application/json",
    }

    def end_headers(self) -> None:
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        route = urllib.parse.urlparse(getattr(self, "path", "") or "").path
        if route in _NO_STORE_BRIDGE_ROUTES:
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return

    def translate_path(self, path: str) -> str:
        route = urllib.parse.urlparse(path).path
        if route == "/":
            target = WEB_DIR / "index.html"
        elif route == "/voices.json":
            target = voice_store.data_root() / "runtime" / "voices.json"
        elif route.startswith("/engine/"):
            target = _safe_join(ENGINE_DIR, route[len("/engine/") :])
        elif route.startswith("/voices/"):
            target = _safe_join(voice_store.voice_dir(), route[len("/voices/") :])
        elif route.endswith(".zvoice"):
            target = _safe_join(voice_store.voice_dir(), route.lstrip("/"))
        else:
            target = _safe_join(WEB_DIR, route.lstrip("/"))
        if target.is_dir():
            return str(WEB_DIR / "__invalid__")
        return str(target)


def _safe_join(root: Path, relative: str) -> Path:
    root = root.resolve()
    unquoted = urllib.parse.unquote(relative)
    if "\x00" in unquoted:
        return root / "__invalid__"
    try:
        target = (root / unquoted.replace("/", os.sep)).resolve()
    except (OSError, ValueError):
        return root / "__invalid__"
    if root != target and root not in target.parents:
        return root / "__invalid__"
    return target


_LOCAL_HTTP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _read_json_endpoint(port: int, path: str, method: str = "GET", timeout: float = 5) -> Any:
    url = f"http://127.0.0.1:{port}{path}"
    request = urllib.request.Request(url, method=method)
    with _LOCAL_HTTP_OPENER.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _raise_if_cancelled(cancelEvent: threading.Event | None) -> None:
    if cancelEvent is not None and cancelEvent.is_set():
        raise CdpCancelled()


def _friendly_cdp_error(message: str, technicalDetail: str | None = None) -> CdpError:
    if technicalDetail:
        log.debug("Google TTS Chromium browser runtime detail: %s", technicalDetail)
    return CdpError(message, technicalDetail)


_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def _format_startup_stderr(raw: bytes, *, truncated: bool = False) -> str:
    """Format captured Chromium startup stderr into a concise single-line snippet."""
    if not raw:
        return ""
    if truncated:
        # Skip up to 3 orphaned UTF-8 continuation bytes if ring-buffer eviction split a multi-byte character.
        start = 0
        limit = min(3, len(raw))
        while start < limit and (raw[start] & 0xC0) == 0x80:
            start += 1
        if start:
            raw = raw[start:]
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    text = _ANSI_ESCAPE_RE.sub("", text)
    lines: list[str] = []
    for rawLine in text.splitlines():
        sanitized = "".join(
            "" if ch in ("\x00", "\ufeff") else (" " if (ord(ch) < 32 or 127 <= ord(ch) <= 159) else ch)
            for ch in rawLine
        )
        cleaned = " ".join(sanitized.split())
        if cleaned:
            lines.append(cleaned)
    if not lines:
        return ""
    joined = " | ".join(lines)
    if len(joined) > _STARTUP_STDERR_SNIPPET_MAX_CHARS:
        return "..." + joined[-(_STARTUP_STDERR_SNIPPET_MAX_CHARS - 3) :]
    if truncated and not joined.startswith("..."):
        prefixed = "..." + joined
        if len(prefixed) > _STARTUP_STDERR_SNIPPET_MAX_CHARS:
            return "..." + joined[-(_STARTUP_STDERR_SNIPPET_MAX_CHARS - 3) :]
        return prefixed
    return joined


def _append_stderr_detail(detail: str, stderrSnippet: str) -> str:
    """Append a formatted stderr snippet once onto a technical error detail string."""
    if not stderrSnippet or "[stderr:" in detail:
        return detail
    return f"{detail} [stderr: {stderrSnippet}]"


def _cancel_thread_sync_io(nativeThreadId: int) -> bool:
    """Cancel a pending synchronous I/O call (e.g. ReadFile on a pipe) on a Windows thread."""
    if os.name != "nt" or nativeThreadId <= 0:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        openThread = ctypes.WINFUNCTYPE(
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )(("OpenThread", kernel32))
        cancelSyncIo = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE)(("CancelSynchronousIo", kernel32))
        closeHandle = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE)(("CloseHandle", kernel32))
        THREAD_TERMINATE = 0x0001
        handle = openThread(THREAD_TERMINATE, False, int(nativeThreadId))
        if not handle:
            return False
        try:
            return bool(cancelSyncIo(handle))
        finally:
            closeHandle(handle)
    except Exception:
        return False


def _browser_attempt_context(runtime: str | None, isPersistent: bool) -> str:
    """Describe the browser runtime and profile mode for startup diagnostics."""
    label = BROWSER_RUNTIME_LABELS.get(runtime, runtime) if runtime else "Chromium"
    profileKind = "persistent" if isPersistent else "temporary"
    return f"{label}, {profileKind} profile"


def _append_technical_detail(error: BaseException, extraDetail: str) -> None:
    """Append diagnostic context onto an exception's technicalDetail without altering user text."""
    if not extraDetail:
        return
    existing = str(getattr(error, "technicalDetail", "") or ("" if isinstance(error, CdpError) else error)).strip()
    with suppress(AttributeError, TypeError):
        error.technicalDetail = f"{existing} {extraDetail}".strip() if existing else extraDetail  # type: ignore[attr-defined]


def _format_runtime_failure_detail(runtime: str | None, error: BaseException) -> str:
    """Format a one-line per-runtime failure summary for fallback diagnostics."""
    label = BROWSER_RUNTIME_LABELS.get(runtime, runtime) if runtime else "Chromium"
    detail = str(getattr(error, "technicalDetail", "") or error)
    return f"{label}: {detail}"


class _StartupStderrCapture:
    """Bounded daemon reader that drains a browser stderr pipe to prevent blocking."""

    def __init__(self, stream: Any, maxBytes: int = _STARTUP_STDERR_MAX_BYTES) -> None:
        self._stream = stream
        self._maxBytes = max(0, int(maxBytes))
        self._lock = threading.Lock()
        self._ioLock = threading.Lock()
        self._buffer = bytearray()
        self._capturing = True
        self._truncated = False
        self._stopRequested = threading.Event()
        self._finished = threading.Event()
        self._nativeThreadId: int | None = None
        self._thread: threading.Thread | None = None
        if stream is not None and callable(getattr(stream, "read", None)):
            thread = threading.Thread(
                name="googleTtsForNvda.browserStderr",
                target=self._drain_loop,
                daemon=True,
            )
            try:
                thread.start()
                self._thread = thread
            except Exception:
                log.debugWarning("Could not start Google TTS browser stderr reader thread.", exc_info=True)
                if isinstance(stream, io.IOBase):
                    with suppress(Exception):
                        stream.close()
                self._finished.set()
        else:
            self._finished.set()

    def _drain_loop(self) -> None:
        readOne = getattr(self._stream, "read1", None)
        readFn = readOne if callable(readOne) else getattr(self._stream, "read", None)
        shouldClose = isinstance(self._stream, io.IOBase)
        with self._ioLock:
            self._nativeThreadId = threading.get_native_id()
        try:
            if not callable(readFn):
                return
            while not self._stopRequested.is_set():
                chunkSize = 4096 if self._capturing else 65536
                try:
                    chunk = readFn(chunkSize)
                except Exception:
                    break
                if not isinstance(chunk, (bytes, bytearray)):
                    break
                shouldClose = True
                if not chunk:
                    break
                if not self._capturing or self._maxBytes <= 0:
                    continue
                with self._lock:
                    if not self._capturing or self._maxBytes <= 0:
                        continue
                    if len(chunk) >= self._maxBytes:
                        if self._buffer or len(chunk) > self._maxBytes:
                            self._truncated = True
                        self._buffer = bytearray(chunk[-self._maxBytes :])
                    else:
                        self._buffer.extend(chunk)
                        excess = len(self._buffer) - self._maxBytes
                        if excess > 0:
                            del self._buffer[:excess]
                            self._truncated = True
        finally:
            with self._ioLock:
                self._nativeThreadId = None
            if shouldClose:
                with suppress(Exception):
                    if callable(getattr(self._stream, "close", None)):
                        self._stream.close()
            self._finished.set()

    def clear(self) -> None:
        """Discard retained startup stderr bytes once the browser connects."""
        with self._lock:
            self._capturing = False
            self._truncated = False
            self._buffer.clear()

    def close(self) -> None:
        """Stop capturing and abort any lingering Windows pipe read after process termination."""
        self.clear()
        self._stopRequested.set()
        if self._finished.is_set():
            return
        if self._thread is not None and not self._thread.is_alive():
            self._finished.set()
            return
        if self._finished.wait(timeout=0.05):
            return
        currentTid = threading.get_native_id()
        for _ in range(3):
            with self._ioLock:
                if self._finished.is_set():
                    return
                nativeTid = self._nativeThreadId
                if nativeTid is not None and nativeTid != currentTid:
                    _cancel_thread_sync_io(nativeTid)
            if self._finished.wait(timeout=0.05):
                return
            if self._thread is not None and not self._thread.is_alive():
                self._finished.set()
                return

    def wait_and_format(
        self,
        timeout: float = 0.2,
        *,
        cancelEvent: threading.Event | None = None,
    ) -> str:
        """Wait briefly for stderr drain completion and return formatted tail text."""
        with self._lock:
            if not self._capturing:
                return ""
        if timeout > 0 and not self._finished.is_set() and self._thread is not None and self._thread.is_alive():
            if cancelEvent is None:
                self._finished.wait(timeout=timeout)
            else:
                deadline = time.monotonic() + timeout
                while not self._finished.is_set() and not cancelEvent.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._finished.wait(timeout=min(0.02, remaining))
        with self._lock:
            if not self._capturing:
                return ""
            raw = bytes(self._buffer)
            truncated = self._truncated
        return _format_startup_stderr(raw, truncated=truncated)


def _no_browser_runtime_error() -> CdpError:
    return _friendly_cdp_error(
        _(
            "No supported Chromium browser runtime was found. Install Google Chrome, Microsoft Edge, or Brave, or set CHROME_PATH, EDGE_PATH, or BRAVE_PATH to a browser executable."
        ),
        "No supported Chromium browser runtime executable was found.",
    )


_TRANSIENT_RUNTIME_EVALUATE_ERRORS = (
    "Cannot find default execution context",
    "Execution context was destroyed",
    "Cannot find context with specified id",
    "Inspected target navigated or closed",
)


def _is_transient_runtime_evaluate_error(error: CdpError) -> bool:
    technicalDetail = str(getattr(error, "technicalDetail", "") or "")
    return "CDP error for Runtime.evaluate:" in technicalDetail and any(
        message in technicalDetail for message in _TRANSIENT_RUNTIME_EVALUATE_ERRORS
    )


_RUNTIME_RECYCLE_ERROR_MARKERS = (
    "Timed out waiting for browser speech audio",
    "Timed out waiting for WASM engine synthesis to complete",
    "Browser DevTools websocket closed",
    "Browser DevTools websocket is not connected",
    "Could not send CDP command",
    "Runtime.evaluate",
    "WASM TTS engine was not loaded",
    "googleTtsForNvdaBridge is not available",
)


def _runtime_error_requires_recycle(error: BaseException) -> bool:
    if isinstance(error, CdpCancelled):
        return False
    if isinstance(error, _BrowserSpeechError):
        return True
    if not isinstance(error, CdpError):
        return True
    technicalDetail = str(getattr(error, "technicalDetail", "") or "")
    return any(marker in technicalDetail for marker in _RUNTIME_RECYCLE_ERROR_MARKERS)


def _format_bytes(byteCount: int) -> str:
    return f"{byteCount / (1024 * 1024):.1f} MB"


def _build_win32_process_apis(winDllFactory: Any, winFuncType: Any) -> dict[str, Any] | None:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    kernel32 = winDllFactory("kernel32", use_last_error=True)
    psapi = winDllFactory("psapi", use_last_error=True)
    return {
        "ctypes": ctypes,
        "wintypes": wintypes,
        "PROCESSENTRY32W": PROCESSENTRY32W,
        "PROCESS_MEMORY_COUNTERS_EX": PROCESS_MEMORY_COUNTERS_EX,
        "createSnapshot": winFuncType(wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD)(
            ("CreateToolhelp32Snapshot", kernel32)
        ),
        "process32First": winFuncType(wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))(
            ("Process32FirstW", kernel32)
        ),
        "process32Next": winFuncType(wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))(
            ("Process32NextW", kernel32)
        ),
        "openProcess": winFuncType(wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)(
            ("OpenProcess", kernel32)
        ),
        "terminateProcess": winFuncType(wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)(("TerminateProcess", kernel32)),
        "getProcessTimes": winFuncType(
            wintypes.BOOL,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        )(("GetProcessTimes", kernel32)),
        "closeHandle": winFuncType(wintypes.BOOL, wintypes.HANDLE)(("CloseHandle", kernel32)),
        "getProcessMemoryInfo": winFuncType(
            wintypes.BOOL,
            wintypes.HANDLE,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
            wintypes.DWORD,
        )(("GetProcessMemoryInfo", psapi)),
    }


@functools.lru_cache(maxsize=2)
def _win32_process_apis_cached(winDllFactory: Any, winFuncType: Any) -> dict[str, Any] | None:
    return _build_win32_process_apis(winDllFactory, winFuncType)


def _win32_process_apis() -> dict[str, Any] | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
    except Exception:
        return None
    winDllFactory = getattr(ctypes, "WinDLL", None)
    winFuncType = getattr(ctypes, "WINFUNCTYPE", None)
    if winDllFactory is None or winFuncType is None:
        return None
    if getattr(winDllFactory, "__module__", "") == "ctypes" and getattr(winFuncType, "__module__", "") == "ctypes":
        return _win32_process_apis_cached(winDllFactory, winFuncType)
    return _build_win32_process_apis(winDllFactory, winFuncType)


def _process_creation_time(apis: dict[str, Any], handle: int) -> tuple[int, int] | None:
    wintypes = apis["wintypes"]
    ctypes = apis["ctypes"]
    creation = wintypes.FILETIME()
    exitTime = wintypes.FILETIME()
    kernelTime = wintypes.FILETIME()
    userTime = wintypes.FILETIME()
    if not apis["getProcessTimes"](
        handle,
        ctypes.byref(creation),
        ctypes.byref(exitTime),
        ctypes.byref(kernelTime),
        ctypes.byref(userTime),
    ):
        return None
    return int(creation.dwLowDateTime), int(creation.dwHighDateTime)


def _process_tree_ids(rootPid: int) -> list[int]:
    apis = _win32_process_apis()
    if apis is None:
        return [rootPid]
    ctypes = apis["ctypes"]
    PROCESSENTRY32W = apis["PROCESSENTRY32W"]
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    snapshot = apis["createSnapshot"](TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return [rootPid]
    childrenByParent: dict[int, list[int]] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not apis["process32First"](snapshot, ctypes.byref(entry)):
            return [rootPid]
        while True:
            parentPid = int(entry.th32ParentProcessID)
            processId = int(entry.th32ProcessID)
            childrenByParent.setdefault(parentPid, []).append(processId)
            if not apis["process32Next"](snapshot, ctypes.byref(entry)):
                break
    finally:
        apis["closeHandle"](snapshot)

    result: list[int] = []
    pending = [rootPid]
    seen: set[int] = set()
    while pending:
        processId = pending.pop()
        if processId in seen:
            continue
        seen.add(processId)
        result.append(processId)
        pending.extend(childrenByParent.get(processId, ()))
    return result


def _process_memory_counters(processId: int) -> tuple[int, int] | None:
    apis = _win32_process_apis()
    if apis is None:
        return None
    ctypes = apis["ctypes"]
    PROCESS_MEMORY_COUNTERS_EX = apis["PROCESS_MEMORY_COUNTERS_EX"]
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    handle = apis["openProcess"](PROCESS_QUERY_LIMITED_INFORMATION, False, processId)
    if not handle:
        handle = apis["openProcess"](PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, processId)
    if not handle:
        return None
    try:
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
        if not apis["getProcessMemoryInfo"](handle, ctypes.byref(counters), counters.cb):
            return None
        return int(counters.PrivateUsage), int(counters.WorkingSetSize)
    finally:
        apis["closeHandle"](handle)


def _terminate_subprocess(process: Any) -> None:
    if process is None:
        return
    popenCls = subprocess.Popen
    isRealPopen = isinstance(popenCls, type) and isinstance(process, popenCls)
    rootPid = getattr(process, "pid", None) if isRealPopen else None
    apis = _win32_process_apis() if isRealPopen else None
    descendantCreationTimes: dict[int, tuple[int, int] | None] = {}
    descendantPids = _process_tree_ids(int(rootPid)) if rootPid and rootPid > 0 else []
    if apis is not None and descendantPids:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        for pid in descendantPids:
            if pid <= 0 or pid == os.getpid():
                continue
            handle = apis["openProcess"](PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                continue
            try:
                descendantCreationTimes[pid] = _process_creation_time(apis, handle)
            finally:
                apis["closeHandle"](handle)
    if process.poll() is None:
        with suppress(Exception):
            process.terminate()
            process.wait(timeout=2)
        if process.poll() is None:
            with suppress(Exception):
                process.kill()
                process.wait(timeout=1)
    if apis is None or not descendantPids:
        return
    currentPid = os.getpid()
    rootExited = process.poll() is not None
    pids = [
        pid
        for pid in descendantPids
        if pid > 0 and pid != currentPid and not (rootExited and rootPid is not None and pid == int(rootPid))
    ]
    if not pids:
        return
    PROCESS_TERMINATE = 0x0001
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    for pid in reversed(pids):
        expectedCreation = descendantCreationTimes.get(pid)
        if expectedCreation is None:
            continue
        handle = apis["openProcess"](PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            # Verify creation time before terminating so a reused PID is never killed.
            if _process_creation_time(apis, handle) == expectedCreation:
                apis["terminateProcess"](handle, 1)
        finally:
            apis["closeHandle"](handle)


def _process_tree_memory_usage(rootPid: int) -> dict[str, int] | None:
    totalPrivateBytes = 0
    totalWorkingSetBytes = 0
    processCount = 0
    for processId in _process_tree_ids(rootPid):
        counters = _process_memory_counters(processId)
        if counters is None:
            continue
        privateBytes, workingSetBytes = counters
        totalPrivateBytes += privateBytes
        totalWorkingSetBytes += workingSetBytes
        processCount += 1
    if processCount <= 0:
        return None
    return {
        "privateBytes": totalPrivateBytes,
        "workingSetBytes": totalWorkingSetBytes,
        "processCount": processCount,
    }


def _browser_profile_in_use_error(
    exitCode: int = 21,
    *,
    context: str | None = None,
    stderrSnippet: str = "",
) -> _BrowserProfileInUseError:
    message = _(
        "The browser profile used by Google TTS For NVDA is already in use. "
        "Restart NVDA, or close any leftover supported Chromium browser helper processes."
    )
    contextText = f" ({context})" if context else ""
    technicalDetail = _append_stderr_detail(
        f"Chromium browser runtime{contextText} exited with profile-in-use code {exitCode}.",
        stderrSnippet,
    )
    log.debug("Google TTS Chromium browser runtime detail: %s", technicalDetail)
    return _BrowserProfileInUseError(message, technicalDetail)


def _hidden_chrome_startup_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startupInfo = subprocess.STARTUPINFO()
    startupInfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupInfo.wShowWindow = 0
    return {
        "startupinfo": startupInfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


@functools.lru_cache(maxsize=1)
def _chrome_window_hider_apis() -> tuple[Any, Any, Any, Any, Any] | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    windowProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    # Use local prototypes so this helper does not mutate ctypes function
    # signatures shared with NVDA core in the same Python process.
    enumWindows = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        windowProc,
        wintypes.LPARAM,
    )(("EnumWindows", user32))
    getWindowThreadProcessId = ctypes.WINFUNCTYPE(
        wintypes.DWORD,
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    )(("GetWindowThreadProcessId", user32))
    isWindowVisible = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HWND,
    )(("IsWindowVisible", user32))
    showFuncName = "ShowWindowAsync" if hasattr(user32, "ShowWindowAsync") else "ShowWindow"
    showWindowAsync = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HWND,
        ctypes.c_int,
    )((showFuncName, user32))
    return windowProc, enumWindows, getWindowThreadProcessId, isWindowVisible, showWindowAsync


def _hide_chrome_windows(processId: int) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        apis = _chrome_window_hider_apis()
        if apis is None:
            return
        windowProc, enumWindows, getWindowThreadProcessId, isWindowVisible, showWindowAsync = apis
        targetPids = set(_process_tree_ids(processId))
        targetPids.add(processId)

        @windowProc
        def enum_window(hwnd: int, _param: int) -> bool:
            ownerProcessId = wintypes.DWORD()
            getWindowThreadProcessId(hwnd, ctypes.byref(ownerProcessId))
            if ownerProcessId.value in targetPids and isWindowVisible(hwnd):
                showWindowAsync(hwnd, 0)
            return True

        enumWindows(enum_window, 0)
    except Exception:
        log.debugWarning("Could not hide Google TTS browser helper window.", exc_info=True)


def _elevate_chrome_priority(processId: int) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
        PROCESS_POWER_THROTTLING = 4
        PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
        PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1

        class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
            _fields_ = [
                ("Version", wintypes.DWORD),
                ("ControlMask", wintypes.DWORD),
                ("StateMask", wintypes.DWORD),
            ]

        kernel32 = ctypes.windll.kernel32
        openProcess = ctypes.WINFUNCTYPE(
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )(("OpenProcess", kernel32))
        setPriorityClass = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HANDLE,
            wintypes.DWORD,
        )(("SetPriorityClass", kernel32))
        closeHandle = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HANDLE,
        )(("CloseHandle", kernel32))

        setProcessInformation = None
        if hasattr(kernel32, "SetProcessInformation"):
            setProcessInformation = ctypes.WINFUNCTYPE(
                wintypes.BOOL,
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            )(("SetProcessInformation", kernel32))

        throttlingState = PROCESS_POWER_THROTTLING_STATE(
            PROCESS_POWER_THROTTLING_CURRENT_VERSION,
            PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
            0,
        )

        for pid in _process_tree_ids(processId):
            handle = openProcess(0x0200, False, pid)
            if not handle:
                continue
            try:
                setPriorityClass(handle, ABOVE_NORMAL_PRIORITY_CLASS)
                if setProcessInformation is not None:
                    setProcessInformation(
                        handle,
                        PROCESS_POWER_THROTTLING,
                        ctypes.cast(ctypes.byref(throttlingState), ctypes.c_void_p),
                        ctypes.sizeof(throttlingState),
                    )
            finally:
                closeHandle(handle)
    except Exception:
        log.debugWarning("Could not elevate Google TTS browser process priority.", exc_info=True)


def _normalize_browser_runtime(runtime: str | None) -> str:
    runtime = str(runtime or "").strip().lower()
    if runtime in BROWSER_RUNTIMES:
        return runtime
    return DEFAULT_BROWSER_RUNTIME


def configured_browser_runtime() -> str:
    if config is None:
        return DEFAULT_BROWSER_RUNTIME
    try:
        return _normalize_browser_runtime(config.conf[CONFIG_SECTION][CONFIG_BROWSER_RUNTIME])
    except Exception:
        return DEFAULT_BROWSER_RUNTIME


def _set_config_value(key: str, value: Any) -> None:
    if config is None:
        return
    with suppress(Exception):
        config.conf[CONFIG_SECTION][key] = value
    try:
        baseProfile = config.conf.profiles[0]
        if CONFIG_SECTION not in baseProfile:
            baseProfile[CONFIG_SECTION] = {}
        baseProfile[CONFIG_SECTION][key] = value
    except Exception:
        pass


def set_configured_browser_runtime(runtime: str) -> str:
    runtime = _normalize_browser_runtime(runtime)
    _set_config_value(CONFIG_BROWSER_RUNTIME, runtime)
    return runtime


def configured_keep_browser_runtime_ready() -> bool:
    if config is None:
        return DEFAULT_KEEP_BROWSER_RUNTIME_READY
    try:
        value = config.conf[CONFIG_SECTION][CONFIG_KEEP_BROWSER_RUNTIME_READY]
        return audio_math.coerce_bool(value, DEFAULT_KEEP_BROWSER_RUNTIME_READY)
    except Exception:
        return DEFAULT_KEEP_BROWSER_RUNTIME_READY


def set_keep_browser_runtime_ready(enabled: bool) -> bool:
    enabled = bool(enabled)
    _set_config_value(CONFIG_KEEP_BROWSER_RUNTIME_READY, enabled)
    return enabled


def _runtime_fallback_order(runtime: str | None = None) -> tuple[str, ...]:
    preferred = _normalize_browser_runtime(runtime)
    return tuple(dict.fromkeys((preferred, BROWSER_RUNTIME_CHROME, BROWSER_RUNTIME_EDGE, BROWSER_RUNTIME_BRAVE)))


def _win_program_roots() -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()
    for envVar in ("PROGRAMFILES", "PROGRAMW6432", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        raw = os.environ.get(envVar, "")
        if not raw:
            continue
        key = os.path.normcase(raw)
        if key in seen:
            continue
        seen.add(key)
        roots.append(Path(raw))
    return roots


def _winreg_open_keys(hive: Any, subKey: str) -> list[Any]:
    if winreg is None:
        return []
    keyRead = getattr(winreg, "KEY_READ", 0x20019)
    wowFlags: list[int] = [0]
    for flagName in ("KEY_WOW64_64KEY", "KEY_WOW64_32KEY"):
        flag = getattr(winreg, flagName, 0)
        if flag:
            wowFlags.append(flag)
    opened: list[Any] = []
    for wowFlag in wowFlags:
        try:
            key = winreg.OpenKey(hive, subKey, 0, keyRead | wowFlag) if wowFlag else winreg.OpenKey(hive, subKey)
            opened.append(key)
        except OSError:
            pass
    return opened


def _edge_webview2_candidates() -> list[str]:
    executableName = "msedgewebview2.exe"
    envFolder = os.environ.get("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", "")
    envCandidates: list[str] = []
    if envFolder:
        envPath = Path(envFolder)
        envCandidates.append(str(envPath / executableName))
        with suppress(OSError):
            envCandidates.extend(str(candidate) for candidate in envPath.glob(f"*/{executableName}"))
    commonRoots = [root / "Microsoft" / "EdgeWebView" / "Application" for root in _win_program_roots()]
    commonCandidates = [str(root / executableName) for root in commonRoots]
    versionedCandidates: list[str] = []
    for root in commonRoots:
        with suppress(OSError):
            versionedCandidates.extend(str(candidate) for candidate in root.glob(f"*/{executableName}"))
    return [*envCandidates, *commonCandidates, *versionedCandidates]


def _registry_has_edge_webview2_runtime() -> bool:
    if winreg is None:
        return False
    registryRoots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\EdgeUpdate\Clients"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, subKey in registryRoots:
        with ExitStack() as stack:
            openedKeys = [stack.enter_context(rootKey) for rootKey in _winreg_open_keys(hive, subKey)]
            for rootKey in openedKeys:
                try:
                    index = 0
                    while True:
                        try:
                            childName = winreg.EnumKey(rootKey, index)
                        except OSError:
                            break
                        index += 1
                        try:
                            with winreg.OpenKey(rootKey, childName) as childKey:
                                values = []
                                for valueName in ("name", "DisplayName"):
                                    try:
                                        value, _ = winreg.QueryValueEx(childKey, valueName)
                                        values.append(str(value))
                                    except OSError:
                                        pass
                                name = " ".join(values).lower()
                                if "webview2" not in name and "edge webview" not in name:
                                    continue
                                version = ""
                                for versionValueName in ("pv", "DisplayVersion"):
                                    try:
                                        rawVersion, _ = winreg.QueryValueEx(childKey, versionValueName)
                                        text = str(rawVersion).strip()
                                        if text:
                                            version = text
                                            break
                                    except OSError:
                                        pass
                                if version and version != "0.0.0.0":
                                    return True
                        except OSError:
                            pass
                except OSError:
                    pass
    return False


def _registry_app_paths(executableName: str) -> list[str]:
    if winreg is None:
        return []
    paths: list[str] = []
    seen: set[str] = set()
    registryKeys = [
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executableName}"),
        (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{executableName}"),
    ]
    for hive, subKey in registryKeys:
        for key in _winreg_open_keys(hive, subKey):
            try:
                with key:
                    path, _ = winreg.QueryValueEx(key, "")
                    text = str(path)
                    norm = os.path.normcase(text)
                    if text and norm not in seen:
                        seen.add(norm)
                        paths.append(text)
            except OSError:
                pass
    return paths


def _browser_candidates(runtime: str) -> list[str]:
    runtime = _normalize_browser_runtime(runtime)
    roots = _win_program_roots()
    if runtime == BROWSER_RUNTIME_EDGE:
        executableName = "msedge.exe"
        envPath = os.environ.get("EDGE_PATH", "")
        commonPaths = [
            *[str(root / "Microsoft" / "Edge" / "Application" / executableName) for root in roots],
            shutil.which("msedge.exe") or "",
            shutil.which("msedge") or "",
        ]
    elif runtime == BROWSER_RUNTIME_BRAVE:
        executableName = "brave.exe"
        envPath = os.environ.get("BRAVE_PATH", "")
        commonPaths = [
            *[str(root / "BraveSoftware" / "Brave-Browser" / "Application" / executableName) for root in roots],
            shutil.which("brave.exe") or "",
            shutil.which("brave") or "",
        ]
    else:
        executableName = "chrome.exe"
        envPath = os.environ.get("CHROME_PATH", "")
        commonPaths = [
            *[str(root / "Google" / "Chrome" / "Application" / executableName) for root in roots],
            shutil.which("chrome.exe") or "",
            shutil.which("chrome") or "",
        ]
    return [envPath, *_registry_app_paths(executableName), *commonPaths]


def browser_path_for_runtime(runtime: str) -> str | None:
    for candidate in _browser_candidates(runtime):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def browser_executable_available(runtime: str) -> bool:
    return browser_path_for_runtime(runtime) is not None


def edge_webview2_available() -> bool:
    for candidate in _edge_webview2_candidates():
        if candidate and Path(candidate).is_file():
            return True
    return _registry_has_edge_webview2_runtime()


def browser_runtime_available(runtime: str | None = None) -> bool:
    if runtime is None:
        return find_browser() is not None
    normalized = str(runtime).strip().lower()
    if normalized not in BROWSER_RUNTIMES:
        return False
    if not browser_executable_available(normalized):
        return False
    if normalized == BROWSER_RUNTIME_EDGE:
        return edge_webview2_available()
    return True


def browser_availability() -> dict[str, bool]:
    return {runtime: browser_runtime_available(runtime) for runtime in BROWSER_RUNTIMES}


def _browser_choices(
    runtime: str | None = None,
    skipRuntimes: set[str] | None = None,
) -> tuple[tuple[str, str], ...]:
    skipRuntimes = skipRuntimes or set()
    choices: list[tuple[str, str]] = []
    for candidateRuntime in _runtime_fallback_order(runtime or configured_browser_runtime()):
        if candidateRuntime in skipRuntimes:
            continue
        path = browser_path_for_runtime(candidateRuntime)
        if not path:
            continue
        if candidateRuntime == BROWSER_RUNTIME_EDGE and not edge_webview2_available():
            continue
        choices.append((candidateRuntime, path))
    return tuple(choices)


def _find_browser_choice(runtime: str | None = None) -> tuple[str, str] | None:
    choices = _browser_choices(runtime)
    return choices[0] if choices else None


def browser_runtime_snapshot(runtime: str | None = None) -> dict[str, Any]:
    selectedRuntime = _normalize_browser_runtime(runtime or configured_browser_runtime())
    paths = {candidateRuntime: browser_path_for_runtime(candidateRuntime) for candidateRuntime in BROWSER_RUNTIMES}
    executableAvailability = {candidateRuntime: path is not None for candidateRuntime, path in paths.items()}
    edgeWebView2Available = edge_webview2_available()
    availability = {
        candidateRuntime: bool(path) and (candidateRuntime != BROWSER_RUNTIME_EDGE or edgeWebView2Available)
        for candidateRuntime, path in paths.items()
    }
    effectivePath = None
    effectiveRuntime = None
    for candidateRuntime in _runtime_fallback_order(selectedRuntime):
        if not availability.get(candidateRuntime, False):
            continue
        effectivePath = paths.get(candidateRuntime)
        if effectivePath:
            effectiveRuntime = candidateRuntime
            break
    return {
        "selectedRuntime": selectedRuntime,
        "paths": paths,
        "executableAvailability": executableAvailability,
        "edgeWebView2Available": edgeWebView2Available,
        "availability": availability,
        "effectivePath": effectivePath,
        "effectiveRuntime": effectiveRuntime,
    }


def browser_runtime_for_path(browserPath: str) -> str:
    exeName = Path(browserPath).name.lower()
    if exeName in ("msedge.exe", "msedge"):
        return BROWSER_RUNTIME_EDGE
    if exeName in ("brave.exe", "brave"):
        return BROWSER_RUNTIME_BRAVE
    return BROWSER_RUNTIME_CHROME


def effective_browser_runtime(runtime: str | None = None) -> str | None:
    choice = _find_browser_choice(runtime)
    if choice is None:
        return None
    return choice[0]


def find_browser(runtime: str | None = None) -> str | None:
    choice = _find_browser_choice(runtime)
    if choice is None:
        return None
    return choice[1]


def edge_webview2_blocks_effective_runtime(runtime: str | None = None) -> bool:
    selectedRuntime = _normalize_browser_runtime(runtime or configured_browser_runtime())
    if edge_webview2_available():
        return False
    if _find_browser_choice(selectedRuntime) is not None:
        return False
    return any(
        candidateRuntime == BROWSER_RUNTIME_EDGE and browser_executable_available(BROWSER_RUNTIME_EDGE)
        for candidateRuntime in _runtime_fallback_order(selectedRuntime)
    )


class CdpDispatcher:
    """Event-driven WebSocket message dispatcher separating network receive loops from callers."""

    def __init__(self, ws: websocket.WebSocket) -> None:
        self._ws = ws
        self._sendLock = threading.Lock()
        self._stateLock = threading.Lock()
        self._pendingEvents: dict[int, threading.Event] = {}
        self._responses: dict[int, dict[str, Any]] = {}
        self._eventHandlers: dict[int, Callable[[dict[str, Any]], None]] = {}
        self._handlerErrors: dict[int, BaseException] = {}
        self._stopped = threading.Event()
        self._readerThread: threading.Thread | None = None

    def start(self) -> None:
        with self._stateLock:
            if self._readerThread is not None and self._readerThread.is_alive():
                return
            self._stopped.clear()
            self._readerThread = threading.Thread(
                name="googleTtsForNvda.cdpReader",
                target=self._reader_loop,
                daemon=True,
            )
            self._readerThread.start()

    def stop(self) -> None:
        self._stopped.set()
        with self._stateLock:
            for event in self._pendingEvents.values():
                event.set()
            self._pendingEvents.clear()
            self._responses.clear()
            self._eventHandlers.clear()
            self._handlerErrors.clear()

    def send(self, command: dict[str, Any]) -> None:
        payload = json.dumps(command)
        with self._sendLock:
            self._ws.send(payload)

    def register_request(
        self,
        msgId: int,
        eventHandler: Callable[[dict[str, Any]], None] | None = None,
    ) -> threading.Event:
        event = threading.Event()
        with self._stateLock:
            if self._stopped.is_set():
                event.set()
                return event
            self._pendingEvents[msgId] = event
            if eventHandler is not None:
                self._eventHandlers[msgId] = eventHandler
        return event

    def unregister_request(self, msgId: int) -> tuple[dict[str, Any] | None, BaseException | None]:
        with self._stateLock:
            self._pendingEvents.pop(msgId, None)
            self._eventHandlers.pop(msgId, None)
            return self._responses.pop(msgId, None), self._handlerErrors.pop(msgId, None)

    def _reader_loop(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    rawMessage = self._ws.recv()
                except (websocket.WebSocketTimeoutException, TimeoutError):
                    continue
                except Exception:
                    break
                if not rawMessage:
                    break
                try:
                    message = json.loads(rawMessage)
                except Exception:
                    continue
                if not isinstance(message, dict):
                    continue

                with self._stateLock:
                    handlers = list(self._eventHandlers.items())
                    msgId = message.get("id")
                    event = self._pendingEvents.get(msgId) if msgId is not None else None
                    if event is not None and msgId is not None:
                        self._responses[msgId] = message

                for handlerMsgId, handler in handlers:
                    try:
                        handler(message)
                    except Exception as exc:
                        with self._stateLock:
                            self._handlerErrors.setdefault(handlerMsgId, exc)
                            handlerEvent = self._pendingEvents.get(handlerMsgId)
                            if handlerEvent is not None:
                                handlerEvent.set()
                        log.debugWarning("Error in CDP event handler", exc_info=True)

                if event is not None:
                    event.set()
        finally:
            self._stopped.set()
            with self._stateLock:
                for ev in self._pendingEvents.values():
                    ev.set()


class BrowserProcessManager:
    """Manages browser executable discovery, profile directories, HTTP server, and subprocess lifecycle."""

    def __init__(self, catalog: VoiceCatalog) -> None:
        self.catalog = catalog
        self._server: _ThreadingTcpServer | None = None
        self._serverThread: threading.Thread | None = None
        self._serverPort: int | None = None
        self._chromeProcess: subprocess.Popen[bytes] | None = None
        self._debugPort: int | None = None
        self._profileDir: Path | None = None
        self._profileRuntime: str | None = None
        self._profileIsPersistent = False
        self._stderrCapture: _StartupStderrCapture | None = None
        self._lock = threading.RLock()

    @classmethod
    def find_browser(cls) -> str | None:
        return find_browser()

    @classmethod
    def browser_runtime_available(cls, runtime: str | None = None) -> bool:
        return browser_runtime_available(runtime)

    @property
    def chrome_process(self) -> subprocess.Popen[bytes] | None:
        return self._chromeProcess

    def browser_memory_usage(self) -> dict[str, int] | None:
        process = self._chromeProcess
        if process is None or process.poll() is not None:
            return None
        return _process_tree_memory_usage(process.pid)

    @property
    def debug_port(self) -> int | None:
        return self._debugPort

    @property
    def server_port(self) -> int | None:
        return self._serverPort

    @property
    def profile_runtime(self) -> str | None:
        return self._profileRuntime

    def _write_runtime_voices_json(self) -> None:
        runtimeDir = voice_store.data_root() / "runtime"
        runtimeDir.mkdir(parents=True, exist_ok=True)
        payload = self.catalog.to_runtime_json()
        tmpPath: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=runtimeDir,
                prefix="voices_",
                suffix=".tmp",
                delete=False,
            ) as handle:
                tmpPath = Path(handle.name)
                handle.write(payload)
                handle.flush()
            targetPath = runtimeDir / "voices.json"
            for attempt in range(3):
                try:
                    os.replace(tmpPath, targetPath)
                    tmpPath = None
                    break
                except PermissionError:
                    if attempt >= 2:
                        raise
                    time.sleep(0.02 * (attempt + 1))
        finally:
            if tmpPath is not None:
                with suppress(OSError):
                    tmpPath.unlink()

    def start_server(self) -> int:
        with self._lock:
            if self._server is not None and self._serverPort is not None:
                return self._serverPort
            self._write_runtime_voices_json()
            self._server = _ThreadingTcpServer(("127.0.0.1", 0), _BridgeRequestHandler)
            self._serverPort = int(self._server.server_address[1])
            self._serverThread = threading.Thread(
                name="googleTtsForNvda.http",
                target=self._server.serve_forever,
                daemon=True,
            )
            self._serverThread.start()
            return self._serverPort

    def update_catalog(self, catalog: VoiceCatalog) -> None:
        with self._lock:
            self.catalog = catalog
            if self._server is not None:
                try:
                    self._write_runtime_voices_json()
                except Exception:
                    log.debugWarning("Could not update runtime voices.json catalog.", exc_info=True)

    def startup_stderr_snippet(
        self,
        *,
        timeout: float = 0.0,
        cancelEvent: threading.Event | None = None,
    ) -> str:
        stderrCapture = getattr(self, "_stderrCapture", None)
        if stderrCapture is None:
            return ""
        return stderrCapture.wait_and_format(timeout=timeout, cancelEvent=cancelEvent)

    def clear_startup_stderr(self) -> None:
        stderrCapture = getattr(self, "_stderrCapture", None)
        if stderrCapture is not None:
            stderrCapture.clear()

    def _reset_browser_process_state(self) -> None:
        proc = self._chromeProcess
        _terminate_subprocess(proc)
        self._chromeProcess = None
        self._debugPort = None
        stderrCapture = getattr(self, "_stderrCapture", None)
        if stderrCapture is not None:
            stderrCapture.close()
        elif proc is not None:
            procStderr = getattr(proc, "stderr", None)
            if isinstance(procStderr, io.IOBase):
                with suppress(Exception):
                    procStderr.close()
        self._stderrCapture = None

    def _stop_browser_process(self, *, removeProfile: bool) -> None:
        self._reset_browser_process_state()
        if removeProfile:
            self._remove_chrome_profile()
        else:
            self._release_chrome_profile()

    def stop_browser_process(self, *, removeProfile: bool = False) -> None:
        with self._lock:
            self._stop_browser_process(removeProfile=removeProfile)

    def _prepare_browser_start(self, cancelEvent: threading.Event | None = None) -> bool:
        self.start_server()
        if self._chromeProcess is not None and self._chromeProcess.poll() is None:
            if self._debugPort is not None and self._serverPort is not None:
                return True
            self._stop_browser_process(removeProfile=False)
        elif self._chromeProcess is not None:
            self._stop_browser_process(removeProfile=False)
        _raise_if_cancelled(cancelEvent)
        return False

    def _browser_choices_or_raise(
        self,
        skipRuntimes: set[str] | None = None,
    ) -> tuple[tuple[str, str], ...]:
        choices = _browser_choices(skipRuntimes=skipRuntimes)
        if choices:
            return choices
        if edge_webview2_blocks_effective_runtime():
            raise _friendly_cdp_error(
                _(
                    "Microsoft Edge WebView2 Runtime was not found. "
                    "Install or repair Microsoft Edge WebView2 Runtime before using Microsoft Edge as the Google TTS For NVDA Chromium browser runtime."
                ),
                "Microsoft Edge executable was found, but Microsoft Edge WebView2 Runtime was not found.",
            )
        raise _no_browser_runtime_error()

    def _log_browser_runtime_failure(self, runtime: str, error: Exception, willTryFallback: bool) -> None:
        label = BROWSER_RUNTIME_LABELS.get(runtime, runtime)
        action = "trying the next fallback runtime" if willTryFallback else "no fallback runtime remains"
        detail = str(getattr(error, "technicalDetail", "") or error)
        log.warning("Google TTS %s browser runtime failed; %s. %s", label, action, detail)
        log.debugWarning(
            "Google TTS %s browser runtime failure traceback (%s).",
            label,
            action,
            exc_info=True,
        )

    def _start_browser_choice(
        self,
        browserRuntime: str,
        browserPath: str,
        cancelEvent: threading.Event | None = None,
    ) -> tuple[int, int]:
        persistentFailureDetail: str | None = None
        for usePersistentProfile in (True, False):
            profileDir = self._get_browser_profile_dir(browserRuntime, usePersistentProfile, cancelEvent)
            devToolsFile = profileDir / "DevToolsActivePort"
            with suppress(FileNotFoundError):
                devToolsFile.unlink()
            pageUrl = self._page_url()
            args = [
                browserPath,
                "--headless=new",
                "--remote-debugging-port=0",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={profileDir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-proxy-server",
                "--disable-background-networking",
                "--disable-breakpad",
                "--disable-crash-reporter",
                "--disable-gpu",
                "--noerrdialogs",
                "--autoplay-policy=no-user-gesture-required",
                "--window-position=-32000,-32000",
                "--window-size=1,1",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--js-flags=--no-idle-gc --wasm-lazy-compilation=false --wasm-dynamic-tiering --max-old-space-size=512",
                "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling,TimerThrottlingForBackgroundTabs,msEdgeEfficiencyMode",
                "--enable-features=AudioWorkletThreadRealtimePriority,WebAssemblySimd,WebAssemblyTiering,WasmCodeGC,WasmCodeProtection",
                "--enable-wasm-simd",
                pageUrl,
            ]
            try:
                oldStderrCapture = getattr(self, "_stderrCapture", None)
                if oldStderrCapture is not None:
                    oldStderrCapture.close()
                    self._stderrCapture = None
                self._chromeProcess = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    **_hidden_chrome_startup_kwargs(),
                )
                self._stderrCapture = _StartupStderrCapture(getattr(self._chromeProcess, "stderr", None))
                _hide_chrome_windows(self._chromeProcess.pid)
                _elevate_chrome_priority(self._chromeProcess.pid)
                self._debugPort = self._read_devtools_port(devToolsFile, cancelEvent)
                _hide_chrome_windows(self._chromeProcess.pid)
                _elevate_chrome_priority(self._chromeProcess.pid)
            except _BrowserProfileInUseError as exc:
                self._reset_browser_process_state()
                if usePersistentProfile:
                    persistentFailureDetail = str(getattr(exc, "technicalDetail", "") or "")
                    log.debugWarning(
                        "Google TTS persistent browser profile is in use; retrying with a temporary profile (%s).",
                        persistentFailureDetail or exc,
                    )
                    self._release_chrome_profile(cleanupLockFiles=False)
                    continue
                if persistentFailureDetail:
                    _append_technical_detail(exc, f"[after persistent profile failure: {persistentFailureDetail}]")
                self._remove_chrome_profile()
                raise
            except CdpCancelled:
                self._stop_browser_process(removeProfile=False)
                raise
            except Exception as exc:
                if not usePersistentProfile and persistentFailureDetail:
                    _append_technical_detail(exc, f"[after persistent profile failure: {persistentFailureDetail}]")
                self._stop_browser_process(removeProfile=not self._profileIsPersistent)
                raise
            assert self._serverPort is not None and self._debugPort is not None
            return self._serverPort, self._debugPort
        raise _browser_profile_in_use_error(context=_browser_attempt_context(browserRuntime, False))

    def _start_first_available_browser(
        self,
        choices: tuple[tuple[str, str], ...],
        cancelEvent: threading.Event | None = None,
    ) -> tuple[int, int]:
        lastError: Exception | None = None
        for index, (browserRuntime, browserPath) in enumerate(choices):
            _raise_if_cancelled(cancelEvent)
            try:
                return self._start_browser_choice(browserRuntime, browserPath, cancelEvent)
            except CdpCancelled:
                raise
            except Exception as exc:
                lastError = exc
                self._log_browser_runtime_failure(browserRuntime, exc, index < len(choices) - 1)
        if lastError is not None:
            raise lastError
        raise _no_browser_runtime_error()

    def start_browser(self, cancelEvent: threading.Event | None = None) -> tuple[int, int]:
        with self._lock:
            if self._prepare_browser_start(cancelEvent):
                assert self._serverPort is not None and self._debugPort is not None
                return self._serverPort, self._debugPort
            return self._start_first_available_browser(
                self._browser_choices_or_raise(),
                cancelEvent,
            )

    def get_page_websocket_url(self, cancelEvent: threading.Event | None = None) -> str:
        with self._lock:
            pageUrl = self._page_url()
            debugPort = self._debugPort
            chromeProcess = self._chromeProcess
            profileRuntime = self._profileRuntime
            profileIsPersistent = self._profileIsPersistent
            stderrCapture = getattr(self, "_stderrCapture", None)
        if debugPort is None:
            raise _friendly_cdp_error(
                _("The Chromium browser runtime is not ready yet."),
                "Browser DevTools port is not ready.",
            )
        context = _browser_attempt_context(profileRuntime, profileIsPersistent)

        def raise_if_browser_exited() -> None:
            if chromeProcess is None:
                return
            exitCode = chromeProcess.poll()
            if exitCode is None:
                return
            stderrSnippet = (
                stderrCapture.wait_and_format(timeout=0.2, cancelEvent=cancelEvent) if stderrCapture is not None else ""
            )
            _raise_if_cancelled(cancelEvent)
            detail = _append_stderr_detail(
                f"The Chromium browser runtime ({context}) exited unexpectedly before becoming ready "
                f"(exit code {exitCode} while waiting for DevTools page target).",
                stderrSnippet,
            )
            raise _friendly_cdp_error(
                _("The Chromium browser runtime closed before Google TTS For NVDA was ready."),
                detail,
            ) from None

        lastWindowHideAt = 0.0
        for _attempt in range(200):
            _raise_if_cancelled(cancelEvent)
            raise_if_browser_exited()
            if chromeProcess is not None:
                now = time.monotonic()
                if now - lastWindowHideAt >= BROWSER_WINDOW_HIDE_POLL_INTERVAL_SECONDS:
                    _hide_chrome_windows(chromeProcess.pid)
                    lastWindowHideAt = now
            try:
                targets = _read_json_endpoint(debugPort, "/json/list", timeout=0.5)
            except (
                OSError,
                TimeoutError,
                urllib.error.URLError,
                http.client.HTTPException,
                json.JSONDecodeError,
                ValueError,
            ):
                raise_if_browser_exited()
                time.sleep(STARTUP_POLL_INTERVAL)
                continue
            if isinstance(targets, list):
                pageUrlTrimmed = pageUrl.rstrip("/")
                pageIndexUrl = f"{pageUrlTrimmed}/index.html"
                for target in targets:
                    if not isinstance(target, dict):
                        continue
                    if target.get("type") != "page":
                        continue
                    wsUrl = target.get("webSocketDebuggerUrl")
                    if not isinstance(wsUrl, str):
                        continue
                    targetUrl = target.get("url")
                    if isinstance(targetUrl, str) and targetUrl.rstrip("/") in (pageUrlTrimmed, pageIndexUrl):
                        return wsUrl
            time.sleep(STARTUP_POLL_INTERVAL)
        stderrSnippet = (
            stderrCapture.wait_and_format(timeout=0.0, cancelEvent=cancelEvent) if stderrCapture is not None else ""
        )
        detail = _append_stderr_detail(f"Could not find browser speech page target ({context}).", stderrSnippet)
        raise _friendly_cdp_error(
            _("Google TTS For NVDA could not find its speech page in the Chromium browser runtime."),
            detail,
        )

    def start_and_get_websocket_url(
        self,
        cancelEvent: threading.Event | None = None,
        skipRuntimes: set[str] | None = None,
    ) -> str:
        with self._lock:
            if self._prepare_browser_start(cancelEvent):
                try:
                    return self.get_page_websocket_url(cancelEvent)
                except CdpCancelled:
                    raise
                except Exception as exc:
                    self._log_browser_runtime_failure(
                        self._profileRuntime or configured_browser_runtime(),
                        exc,
                        True,
                    )
                    self._stop_browser_process(removeProfile=not self._profileIsPersistent)

            choices = self._browser_choices_or_raise(skipRuntimes=skipRuntimes)
            lastError: Exception | None = None
            failureSummaries: list[str] = []
            for index, (browserRuntime, browserPath) in enumerate(choices):
                _raise_if_cancelled(cancelEvent)
                try:
                    self._start_browser_choice(browserRuntime, browserPath, cancelEvent)
                    return self.get_page_websocket_url(cancelEvent)
                except CdpCancelled:
                    self._stop_browser_process(removeProfile=False)
                    raise
                except Exception as exc:
                    lastError = exc
                    failureSummaries.append(_format_runtime_failure_detail(browserRuntime, exc))
                    self._stop_browser_process(removeProfile=not self._profileIsPersistent)
                    self._log_browser_runtime_failure(browserRuntime, exc, index < len(choices) - 1)
            if lastError is not None:
                if len(failureSummaries) > 1:
                    _append_technical_detail(
                        lastError,
                        f"| Previous runtime failures: {'; '.join(failureSummaries[:-1])}",
                    )
                raise lastError
            raise _no_browser_runtime_error()

    def terminate(self) -> None:
        with self._lock:
            self._reset_browser_process_state()
            if self._server is not None:
                if (
                    self._serverThread is not None
                    and self._serverThread.is_alive()
                    and threading.current_thread() is not self._serverThread
                ):
                    with suppress(Exception):
                        self._server.shutdown()
                    with suppress(Exception):
                        self._serverThread.join(timeout=2)
                with suppress(Exception):
                    self._server.server_close()
            self._server = None
            self._serverThread = None
            self._serverPort = None
            self._release_chrome_profile()

    def _get_browser_profile_dir(
        self,
        runtime: str,
        usePersistentProfile: bool = True,
        cancelEvent: threading.Event | None = None,
    ) -> Path:
        runtime = _normalize_browser_runtime(runtime)
        if self._profileDir is not None:
            if self._profileRuntime == runtime and self._profileIsPersistent == usePersistentProfile:
                self._profileDir.mkdir(parents=True, exist_ok=True)
                return self._profileDir
            self._release_chrome_profile(cleanupLockFiles=not (self._profileIsPersistent and not usePersistentProfile))
        _raise_if_cancelled(cancelEvent)
        root = self._browser_profile_root(runtime)
        root.mkdir(parents=True, exist_ok=True)
        self._cleanup_old_browser_profiles(root, runtime, cancelEvent)
        _raise_if_cancelled(cancelEvent)
        if usePersistentProfile:
            profileDir = root / PERSISTENT_PROFILE_DIR_NAME
        else:
            profileDir = root / f"session-{os.getpid()}-{time.monotonic_ns()}"
        reused = profileDir.exists()
        profileDir.mkdir(parents=True, exist_ok=True)
        if not usePersistentProfile:
            for lockName in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
                with suppress(OSError):
                    (profileDir / lockName).unlink()
        with suppress(OSError):
            (profileDir / "DevToolsActivePort").unlink()
        self._profileDir = profileDir
        self._profileRuntime = runtime
        self._profileIsPersistent = usePersistentProfile
        log.debug(
            "Google TTS %s browser profile directory: %s (reused=%s, persistent=%s)",
            runtime,
            profileDir,
            reused,
            usePersistentProfile,
        )
        return self._profileDir

    def _browser_profile_root(self, runtime: str) -> Path:
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path(tempfile.gettempdir())
        return root / LOCAL_CACHE_DIR_NAME / self._browser_profile_dir_name(runtime)

    def _browser_profile_dir_name(self, runtime: str) -> str:
        runtime = _normalize_browser_runtime(runtime)
        if runtime == BROWSER_RUNTIME_EDGE:
            return EDGE_PROFILE_DIR_NAME
        if runtime == BROWSER_RUNTIME_BRAVE:
            return BRAVE_PROFILE_DIR_NAME
        return CHROME_PROFILE_DIR_NAME

    def _persistent_profile_size_check_due(self, root: Path) -> bool:
        try:
            raw = json.loads((root / PERSISTENT_PROFILE_SIZE_CHECK_FILE_NAME).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return True
        if not isinstance(raw, dict):
            return True
        checkedAt = raw.get("checkedAt")
        if not isinstance(checkedAt, (int, float)):
            return True
        elapsed = time.time() - float(checkedAt)
        return not (0 <= elapsed < PERSISTENT_PROFILE_SIZE_CHECK_INTERVAL_SECONDS)

    def _remember_persistent_profile_size_check(self, root: Path, totalSize: int, reset: bool) -> None:
        payload = {
            "checkedAt": time.time(),
            "totalSize": totalSize,
            "reset": reset,
        }
        with suppress(OSError):
            (root / PERSISTENT_PROFILE_SIZE_CHECK_FILE_NAME).write_text(
                json.dumps(payload, separators=(",", ":")),
                encoding="utf-8",
            )

    def _cleanup_old_browser_profiles(
        self,
        root: Path,
        runtime: str,
        cancelEvent: threading.Event | None = None,
    ) -> None:
        cutoff = time.time() - 2 * 24 * 60 * 60
        try:
            children = list(root.iterdir())
        except OSError:
            children = []
        for child in children:
            _raise_if_cancelled(cancelEvent)
            if not child.is_dir() or not child.name.startswith("session-"):
                continue
            try:
                if child.stat().st_mtime >= cutoff:
                    continue
                shutil.rmtree(child, ignore_errors=True)
            except OSError:
                continue
        # Guard against unbounded persistent profile growth.
        persistent = root / PERSISTENT_PROFILE_DIR_NAME
        if persistent.is_dir() and self._persistent_profile_size_check_due(root):
            try:
                totalSize = 0
                for candidate in persistent.rglob("*"):
                    _raise_if_cancelled(cancelEvent)
                    with suppress(OSError):
                        if not candidate.is_file():
                            continue
                        totalSize += candidate.stat().st_size
                        if totalSize > PERSISTENT_PROFILE_MAX_BYTES:
                            break
                profileReset = totalSize > PERSISTENT_PROFILE_MAX_BYTES
                if profileReset:
                    lockHeld = False
                    for lockName in ("lockfile", "SingletonLock"):
                        lockPath = persistent / lockName
                        if lockPath.exists():
                            try:
                                lockPath.unlink()
                            except OSError:
                                lockHeld = True
                                break
                    if not lockHeld:
                        log.debugWarning(
                            "Persistent %s browser profile exceeds 500 MB (%d bytes), resetting.",
                            runtime,
                            totalSize,
                        )
                        shutil.rmtree(persistent, ignore_errors=True)
                    else:
                        profileReset = False
                self._remember_persistent_profile_size_check(root, totalSize, profileReset)
            except OSError:
                pass

    def _release_chrome_profile(self, *, cleanupLockFiles: bool = True) -> None:
        """Release the profile directory reference without deleting it.

        Preserves the persistent profile (including the browser's compiled
        WASM code cache) so the next startup can skip WASM recompilation.  Only
        transient files (lock files, DevToolsActivePort) are cleaned up unless
        another browser process owns the persistent profile.
        """
        profileDir = self._profileDir
        profileIsPersistent = self._profileIsPersistent
        self._profileDir = None
        self._profileRuntime = None
        self._profileIsPersistent = False
        if profileDir is None:
            return
        if not profileIsPersistent:
            with suppress(OSError):
                shutil.rmtree(profileDir, ignore_errors=True)
            return
        if not cleanupLockFiles:
            return
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile", "DevToolsActivePort"):
            with suppress(OSError):
                (profileDir / name).unlink()

    def _remove_chrome_profile(self) -> None:
        """Fully delete the profile directory (used on browser startup failure)."""
        profileDir = self._profileDir
        self._profileDir = None
        self._profileRuntime = None
        self._profileIsPersistent = False
        if profileDir is None:
            return
        try:
            shutil.rmtree(profileDir, ignore_errors=True)
        except OSError:
            log.debugWarning("Could not remove Google TTS browser session profile.", exc_info=True)

    def _read_devtools_port(self, devToolsFile: Path, cancelEvent: threading.Event | None = None) -> int:
        lastReadError: Exception | None = None
        loggedReadError = False

        def record_transient_read_error(error: Exception) -> None:
            nonlocal lastReadError, loggedReadError
            lastReadError = error
            if loggedReadError:
                return
            log.debug(
                "Google TTS browser DevTools port file is not readable yet: %s (%r)",
                devToolsFile,
                error,
                exc_info=error.__traceback__ is not None,
            )
            loggedReadError = True

        stderrCapture = getattr(self, "_stderrCapture", None)
        context = _browser_attempt_context(self._profileRuntime, self._profileIsPersistent)
        lastWindowHideAt = 0.0
        for _attempt in range(400):
            _raise_if_cancelled(cancelEvent)
            if self._chromeProcess is not None:
                now = time.monotonic()
                if now - lastWindowHideAt >= BROWSER_WINDOW_HIDE_POLL_INTERVAL_SECONDS:
                    _hide_chrome_windows(self._chromeProcess.pid)
                    lastWindowHideAt = now
            if self._chromeProcess is not None and self._chromeProcess.poll() is not None:
                exitCode = self._chromeProcess.returncode
                waitTimeout = 0.05 if (exitCode in (0, 21) and self._profileIsPersistent) else 0.2
                stderrSnippet = (
                    stderrCapture.wait_and_format(timeout=waitTimeout, cancelEvent=cancelEvent)
                    if stderrCapture is not None
                    else ""
                )
                _raise_if_cancelled(cancelEvent)
                if exitCode in (0, 21) and self._profileIsPersistent:
                    raise _browser_profile_in_use_error(exitCode, context=context, stderrSnippet=stderrSnippet)
                if exitCode == 21:
                    raise _browser_profile_in_use_error(exitCode, context=context, stderrSnippet=stderrSnippet)
                detail = _append_stderr_detail(
                    f"Chromium browser runtime ({context}) exited before DevTools became available: {exitCode}",
                    stderrSnippet,
                )
                raise _friendly_cdp_error(
                    _("The Chromium browser runtime closed before Google TTS For NVDA was ready."),
                    detail,
                )
            try:
                devToolsReady = devToolsFile.is_file()
                if devToolsReady:
                    # Chromium may create DevToolsActivePort before Windows lets us read
                    # it, especially when Edge is initializing a reused profile.
                    lines = devToolsFile.read_text(encoding="utf-8").splitlines()
                    if lines:
                        try:
                            port = int(lines[0].strip())
                        except ValueError as exc:
                            record_transient_read_error(exc)
                        else:
                            if 0 < port <= 65535:
                                return port
                            record_transient_read_error(ValueError(f"Invalid DevTools port: {port}"))
            except (OSError, UnicodeError) as exc:
                record_transient_read_error(exc)
            time.sleep(STARTUP_POLL_INTERVAL)
        detail = f"Timed out waiting for browser DevTools ({context})."
        if lastReadError is not None:
            detail = f"{detail} Last DevToolsActivePort read error: {lastReadError!r}"
        stderrSnippet = (
            stderrCapture.wait_and_format(timeout=0.0, cancelEvent=cancelEvent) if stderrCapture is not None else ""
        )
        detail = _append_stderr_detail(detail, stderrSnippet)
        raise _friendly_cdp_error(
            _("The Chromium browser runtime did not start in time."),
            detail,
        )

    def _page_url(self) -> str:
        if self._serverPort is None:
            raise _friendly_cdp_error(
                _("Google TTS For NVDA could not start its local browser bridge."),
                "Bridge HTTP server is not running.",
            )
        return f"http://127.0.0.1:{self._serverPort}/"


class CdpClient:
    """Core WebSocket IPC transport and message ID correlation layer for browser CDP interaction."""

    def __init__(self) -> None:
        self._ws: websocket.WebSocket | None = None
        self._dispatcher: CdpDispatcher | None = None
        self._msgId = 0
        self._msgIdLock = threading.Lock()
        self._lock = threading.RLock()
        self._connectGeneration = 0

    @property
    def ws(self) -> websocket.WebSocket | None:
        return self._ws

    @property
    def dispatcher(self) -> CdpDispatcher | None:
        return self._dispatcher

    @staticmethod
    def _close_transport(dispatcher: CdpDispatcher | None, ws: websocket.WebSocket | None) -> None:
        readerThread: threading.Thread | None = None
        if dispatcher is not None:
            readerThread = getattr(dispatcher, "_readerThread", None)
            with suppress(Exception):
                dispatcher.stop()
        if ws is not None:
            with suppress(Exception):
                ws.close()
        if (
            isinstance(readerThread, threading.Thread)
            and readerThread is not threading.current_thread()
            and readerThread.is_alive()
        ):
            readerThread.join(timeout=1.0)

    def connect(self, wsUrl: str) -> None:
        self.close()
        with self._lock:
            connectGeneration = getattr(self, "_connectGeneration", 0)
        ws = websocket.create_connection(
            wsUrl,
            timeout=15,
            suppress_origin=True,
            http_no_proxy=["127.0.0.1", "localhost", "::1"],
        )
        dispatcher: CdpDispatcher | None = None
        try:
            ws.settimeout(0.05)
            dispatcher = CdpDispatcher(ws)
            dispatcher.start()
        except Exception:
            self._close_transport(dispatcher, ws)
            raise
        staleDispatcher: CdpDispatcher | None = None
        staleWs: websocket.WebSocket | None = None
        aborted = False
        with self._lock:
            if getattr(self, "_connectGeneration", 0) != connectGeneration:
                aborted = True
            else:
                staleDispatcher = self._dispatcher
                staleWs = self._ws
                self._ws = ws
                self._dispatcher = dispatcher
        if aborted:
            self._close_transport(dispatcher, ws)
            raise CdpCancelled()
        if staleDispatcher is not None or staleWs is not None:
            self._close_transport(staleDispatcher, staleWs)

    def close(self) -> None:
        with self._lock:
            self._connectGeneration = getattr(self, "_connectGeneration", 0) + 1
            dispatcher = self._dispatcher
            self._dispatcher = None
            ws = self._ws
            self._ws = None
        self._close_transport(dispatcher, ws)

    def is_connected(self) -> bool:
        with self._lock:
            dispatcher = self._dispatcher
            stoppedEvent = getattr(dispatcher, "_stopped", None)
            return (
                self._ws is not None
                and self._ws.connected
                and dispatcher is not None
                and not (stoppedEvent is not None and stoppedEvent.is_set())
            )

    def next_msg_id(self) -> int:
        with self._msgIdLock:
            self._msgId += 1
            return self._msgId

    def send_command_async(self, method: str, params: dict[str, Any] | None = None) -> None:
        with self._lock:
            dispatcher = self._dispatcher
            ws = self._ws
        stoppedEvent = getattr(dispatcher, "_stopped", None)
        if ws is None or not ws.connected or dispatcher is None or (stoppedEvent is not None and stoppedEvent.is_set()):
            return
        msgId = self.next_msg_id()
        command = {"id": msgId, "method": method, "params": params or {}}
        try:
            dispatcher.send(command)
        except Exception:
            log.debugWarning("Could not asynchronously send CDP command %s", method, exc_info=True)

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30,
        eventHandler: Callable[[dict[str, Any]], None] | None = None,
        cancelEvent: threading.Event | None = None,
        onCancelCallback: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        _raise_if_cancelled(cancelEvent)
        with self._lock:
            dispatcher = self._dispatcher
            ws = self._ws
        stoppedEvent = getattr(dispatcher, "_stopped", None)
        if ws is None or dispatcher is None or not ws.connected or (stoppedEvent is not None and stoppedEvent.is_set()):
            raise _friendly_cdp_error(
                _("Google TTS For NVDA is not connected to the Chromium browser runtime."),
                "Browser DevTools websocket is not connected.",
            )
        msgId = self.next_msg_id()
        command = {"id": msgId, "method": method, "params": params or {}}
        event = dispatcher.register_request(msgId, eventHandler=eventHandler)
        response: dict[str, Any] | None = None
        handlerError: BaseException | None = None
        try:
            try:
                dispatcher.send(command)
            except Exception as exc:
                if cancelEvent is not None and cancelEvent.is_set():
                    raise CdpCancelled() from None
                if isinstance(exc, CdpError):
                    raise
                raise _friendly_cdp_error(
                    _("The Chromium browser runtime connection closed unexpectedly."),
                    f"Could not send CDP command {method}: {exc}",
                ) from exc
            timedOut = False
            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                if cancelEvent is not None and cancelEvent.is_set():
                    if onCancelCallback is not None:
                        with suppress(Exception):
                            onCancelCallback()
                    raise CdpCancelled()
                if event.is_set() or (stoppedEvent is not None and stoppedEvent.is_set()):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timedOut = True
                    break
                if event.wait(timeout=min(0.01, remaining)) or (stoppedEvent is not None and stoppedEvent.is_set()):
                    break
            if timedOut:
                if cancelEvent is not None and cancelEvent.is_set():
                    if onCancelCallback is not None:
                        with suppress(Exception):
                            onCancelCallback()
                    raise CdpCancelled()
                if not event.is_set() and not (stoppedEvent is not None and stoppedEvent.is_set()):
                    if onCancelCallback is not None:
                        with suppress(Exception):
                            onCancelCallback()
                    raise _friendly_cdp_error(
                        _("The Chromium browser runtime did not respond in time."),
                        f"Timed out waiting for {method}.",
                    )
        finally:
            response, handlerError = dispatcher.unregister_request(msgId)

        if handlerError is not None:
            if cancelEvent is not None and cancelEvent.is_set():
                raise CdpCancelled()
            if onCancelCallback is not None:
                with suppress(Exception):
                    onCancelCallback()
            raise handlerError
        if response is None:
            if cancelEvent is not None and cancelEvent.is_set():
                raise CdpCancelled()
            raise _friendly_cdp_error(
                _("The Chromium browser runtime connection closed unexpectedly."),
                "Browser DevTools websocket closed.",
            )
        if "error" in response:
            raise _friendly_cdp_error(
                _("The Chromium browser runtime reported an error while processing speech."),
                f"CDP error for {method}: {response['error']}",
            )
        exceptionDetails = (response.get("result") or {}).get("exceptionDetails")
        if isinstance(exceptionDetails, dict):
            raise _friendly_cdp_error(
                _("The Chromium browser runtime reported an error while preparing speech."),
                self.format_exception(exceptionDetails),
            )
        return response

    def format_exception(self, exceptionDetails: dict[str, Any]) -> str:
        exception = exceptionDetails.get("exception")
        if isinstance(exception, dict) and exception.get("description"):
            return str(exception["description"])
        if exceptionDetails.get("text"):
            return str(exceptionDetails["text"])
        return "Browser DevTools runtime exception."


class WasmTtsEngineBridge:
    """Routes high-level domain speech synthesis commands to the Google WASM TTS engine via CDP."""

    def __init__(self, cdp_client: CdpClient, catalog: VoiceCatalog) -> None:
        self._cdp = cdp_client
        self.catalog = catalog
        self._lastStopSentAt = 0.0
        self._stopLock = threading.Lock()
        self._runtimeBusyLock = threading.Lock()
        self._runtimeBusy = False

    @property
    def runtime_busy(self) -> bool:
        with self._runtimeBusyLock:
            return self._runtimeBusy

    def _set_runtime_busy(self, busy: bool) -> None:
        with self._runtimeBusyLock:
            self._runtimeBusy = busy

    def enable_cdp_domains(self, cancelEvent: threading.Event | None = None) -> None:
        self._cdp.request("Runtime.enable", timeout=15, cancelEvent=cancelEvent)
        self._cdp.request("Page.enable", timeout=15, cancelEvent=cancelEvent)
        self._cdp.request("Runtime.addBinding", {"name": BINDING_NAME}, timeout=15, cancelEvent=cancelEvent)

    def wait_until_ready(self, cancelEvent: threading.Event | None = None) -> None:
        expression = """
		typeof window.googleTtsForNvdaSpeak === "function"
		&& typeof window.googleTtsForNvdaPreload === "function"
		&& typeof window.googleTtsForNvdaBridge === "function"
		&& typeof window.googleTtsForNvdaReady === "function"
		&& window.googleTtsForNvdaReady() === true
		"""
        for _attempt in range(400):
            _raise_if_cancelled(cancelEvent)
            try:
                response = self._cdp.request(
                    "Runtime.evaluate",
                    {"expression": expression, "returnByValue": True},
                    timeout=5,
                    cancelEvent=cancelEvent,
                )
            except CdpError as exc:
                if _is_transient_runtime_evaluate_error(exc):
                    time.sleep(STARTUP_POLL_INTERVAL)
                    continue
                raise
            if ((response.get("result") or {}).get("result") or {}).get("value") is True:
                return
            time.sleep(STARTUP_POLL_INTERVAL)
        raise _friendly_cdp_error(
            _("Google TTS For NVDA could not finish loading the browser speech engine."),
            "Browser speech harness did not finish loading.",
        )

    def _require_installed_package(self, options: dict[str, Any]) -> Any:
        voiceId = str(options.get("voiceId") or "")
        try:
            package = self.catalog.package_for_voice(voiceId)
        except KeyError as exc:
            raise _friendly_cdp_error(
                _("This voice package is not installed. Open Google TTS Voice Manager to install it."),
                f"Unknown voice id: {voiceId}.",
            ) from exc
        if not is_package_supported_by_engine(package):
            raise _friendly_cdp_error(
                _("This voice package is not supported by the bundled Google TTS engine."),
                f"Unsupported voice package for {package.id}.",
            )
        if not voice_store.is_package_installed(package):
            raise _friendly_cdp_error(
                _("This voice package is not installed. Open Google TTS Voice Manager to install it."),
                f"Missing voice package: {package.id}.",
            )
        return package

    def preload_voice(self, options: dict[str, Any], cancelEvent: threading.Event | None = None) -> dict[str, Any]:
        self._require_installed_package(options)
        _raise_if_cancelled(cancelEvent)
        payload = {
            "sessionId": f"preload-{time.monotonic_ns()}",
            "voiceName": options["voiceName"],
            "lang": options["lang"],
            "text": str(options.get("warmupText") or " "),
        }
        with self._stopLock:
            self._lastStopSentAt = 0.0
        self._set_runtime_busy(True)
        try:
            response = self._cdp.request(
                "Runtime.evaluate",
                {
                    "expression": f"window.googleTtsForNvdaPreload({json.dumps(payload, ensure_ascii=False)})",
                    "awaitPromise": True,
                    "returnByValue": True,
                    "userGesture": True,
                    "timeout": 60000,
                },
                timeout=70,
                cancelEvent=cancelEvent,
                onCancelCallback=self.send_fast_stop,
            )
        finally:
            self._set_runtime_busy(False)
        value = ((response.get("result") or {}).get("result") or {}).get("value")
        return value if isinstance(value, dict) else {"success": True}

    def speak(
        self,
        text: str,
        options: dict[str, Any],
        onAudio: AudioCallback,
        cancelEvent: threading.Event | None = None,
        onMark: MarkCallback | None = None,
        onSegmentEnd: SegmentEndCallback | None = None,
        segments: list[str] | None = None,
        hasPreviousSegment: bool = False,
    ) -> dict[str, Any]:
        if not text.strip():
            return {"success": True, "empty": True}
        self._require_installed_package(options)
        _raise_if_cancelled(cancelEvent)
        sessionId = f"{time.monotonic_ns()}"
        payload = {
            "sessionId": sessionId,
            "text": text,
            "voiceName": options["voiceName"],
            "lang": options["lang"],
            "rate": options["rate"],
            "artificialRate": options.get("artificialRate", 1),
            "pitch": options["pitch"],
            "postPitch": options.get("postPitch", 1),
            "volume": options["volume"],
            "outputGain": options.get("outputGain", options["volume"]),
            "emitMarks": onMark is not None,
        }
        markAcceptsSampleOffset = _mark_callback_accepts_sample_offset(onMark)
        if segments:
            payload["segments"] = segments
        if hasPreviousSegment:
            payload["hasPreviousSegment"] = True
        state: dict[str, Any] = {"audioChunks": 0, "segmentEnds": 0, "done": False}
        startedAt = time.perf_counter()
        firstAudioAt: float | None = None
        lastAudioAt: float | None = None
        lastSegmentEndAt: float | None = None
        maxAudioGapMs = 0.0
        maxSegmentResumeMs = 0.0

        def fail_speech_event(detail: str) -> None:
            state["error"] = detail
            log.debug("Google TTS Chromium browser runtime detail: %s", detail)
            raise _BrowserSpeechError(
                _("Google TTS For NVDA could not speak this text."),
                detail,
                audioStarted=bool(state["audioChunks"]),
            )

        def handle_event(message: dict[str, Any]) -> None:
            nonlocal firstAudioAt, lastAudioAt, lastSegmentEndAt, maxAudioGapMs, maxSegmentResumeMs
            if message.get("method") != "Runtime.bindingCalled":
                return
            params = message.get("params") or {}
            if params.get("name") != BINDING_NAME:
                return
            rawPayload = params.get("payload")
            if not isinstance(rawPayload, str):
                return
            try:
                event = json.loads(rawPayload)
            except (ValueError, TypeError):
                return
            if not isinstance(event, dict):
                return
            if event.get("sessionId") != sessionId:
                return
            eventType = event.get("type")
            if eventType == "started":
                if is_debug_for_synth_driver(configMod=sys.modules.get("config") or config):
                    log.debug(
                        "Google TTS session %s started in Chromium after %.1f ms.",
                        sessionId,
                        (time.perf_counter() - startedAt) * 1000,
                    )
            elif eventType == "audio":
                if state.get("error"):
                    return
                if cancelEvent is not None and cancelEvent.is_set():
                    return
                rawRate = event.get("sampleRate")
                try:
                    if isinstance(rawRate, bool):
                        raise ValueError("Boolean sampleRate is invalid.")
                    sampleRate = SAMPLE_RATE if rawRate is None else int(rawRate)
                except (TypeError, ValueError):
                    fail_speech_event("Invalid browser audio sample rate.")
                if sampleRate != SAMPLE_RATE:
                    fail_speech_event(f"Unexpected browser audio sample rate: {sampleRate}.")
                try:
                    audio = base64.b64decode(str(event.get("data") or ""), validate=True)
                except (binascii.Error, ValueError):
                    fail_speech_event("Invalid browser audio payload.")
                if len(audio) % 2:
                    fail_speech_event("Invalid browser audio payload length.")
                if audio:
                    if cancelEvent is not None and cancelEvent.is_set():
                        return
                    now = time.perf_counter()
                    if firstAudioAt is None:
                        firstAudioAt = now
                        if is_debug_for_synth_driver(configMod=sys.modules.get("config") or config):
                            log.debug(
                                "Google TTS session %s first audio after %.1f ms.",
                                sessionId,
                                (firstAudioAt - startedAt) * 1000,
                            )
                    if lastAudioAt is not None:
                        maxAudioGapMs = max(maxAudioGapMs, (now - lastAudioAt) * 1000)
                    if lastSegmentEndAt is not None:
                        maxSegmentResumeMs = max(maxSegmentResumeMs, (now - lastSegmentEndAt) * 1000)
                        lastSegmentEndAt = None
                    lastAudioAt = now
                    state["audioChunks"] += 1
                    onAudio(audio)
            elif eventType == "mark":
                if state.get("error") or (cancelEvent is not None and cancelEvent.is_set()):
                    return
                if onMark is not None:
                    with suppress(TypeError, ValueError):
                        charIndex = max(0, int(event.get("charIndex") or 0))
                        if markAcceptsSampleOffset:
                            rawSampleOffset = event.get("sampleOffset")
                            sampleOffset = (
                                None
                                if rawSampleOffset is None or isinstance(rawSampleOffset, bool)
                                else max(0, int(rawSampleOffset))
                            )
                            onMark(charIndex, sampleOffset)
                        else:
                            onMark(charIndex)
            elif eventType == "segmentEnd":
                if state.get("error") or (cancelEvent is not None and cancelEvent.is_set()):
                    return
                state["segmentEnds"] += 1
                lastSegmentEndAt = time.perf_counter()
                if onSegmentEnd is not None:
                    onSegmentEnd()
            elif eventType == "done":
                state["done"] = True
            elif eventType == "error":
                detail = str(event.get("message") or "Browser speech synthesis failed.")
                fail_speech_event(detail)

        expression = f"window.googleTtsForNvdaSpeak({json.dumps(payload, ensure_ascii=False)})"
        with self._stopLock:
            self._lastStopSentAt = 0.0
        self._set_runtime_busy(True)
        try:
            response = self._cdp.request(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "awaitPromise": True,
                    "returnByValue": True,
                    "userGesture": True,
                    "timeout": 120000,
                },
                timeout=130,
                eventHandler=handle_event,
                cancelEvent=cancelEvent,
                onCancelCallback=self.send_fast_stop,
            )
        except CdpCancelled:
            raise
        except CdpError as exc:
            if state["audioChunks"]:
                exc.audioStarted = True
            raise
        finally:
            self._set_runtime_busy(False)
        result = (response.get("result") or {}).get("result") or {}
        if result.get("subtype") == "error":
            detail = str(result.get("description") or "Browser speech evaluation failed.")
            log.debug("Google TTS Chromium browser runtime detail: %s", detail)
            raise _BrowserSpeechError(
                _("Google TTS For NVDA could not start speech in the Chromium browser runtime."),
                detail,
                audioStarted=bool(state["audioChunks"]),
            )
        if state.get("error"):
            raise _BrowserSpeechError(
                _("Google TTS For NVDA could not speak this text."),
                str(state["error"]),
                audioStarted=bool(state["audioChunks"]),
            )
        state["firstAudioMs"] = round((firstAudioAt - startedAt) * 1000, 1) if firstAudioAt else None
        state["maxAudioGapMs"] = round(maxAudioGapMs, 1)
        state["maxSegmentResumeMs"] = round(maxSegmentResumeMs, 1)
        if is_debug_for_synth_driver(configMod=sys.modules.get("config") or config):
            log.debug(
                "Google TTS session %s summary: chars=%d, segments=%d, audioChunks=%d, "
                "firstAudioMs=%s, maxAudioGapMs=%.1f, maxSegmentResumeMs=%.1f.",
                sessionId,
                len(text),
                len(segments) if segments else 1,
                state["audioChunks"],
                state["firstAudioMs"],
                maxAudioGapMs,
                maxSegmentResumeMs,
            )
        value = result.get("value")
        if isinstance(value, dict):
            value.update(state)
            return value
        return state

    def send_fast_stop(self) -> None:
        if not self._cdp.is_connected():
            return
        with self._stopLock:
            now = time.monotonic()
            if now - self._lastStopSentAt < 0.02:
                return
            self._lastStopSentAt = now
            self._cdp.send_command_async(
                "Runtime.evaluate",
                {
                    "expression": STOP_EXPRESSION,
                    "awaitPromise": False,
                    "returnByValue": True,
                },
            )

    def stop_runtime(self) -> None:
        try:
            self._cdp.request(
                "Runtime.evaluate",
                {
                    "expression": STOP_EXPRESSION,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
                timeout=5,
            )
        except Exception:
            log.debugWarning("Could not stop Google TTS Chromium browser runtime.", exc_info=True)

    def cancel_current(self) -> None:
        if self.runtime_busy:
            self.send_fast_stop()


class _CompositeCancelEvent:
    """Cancel-event view that triggers when either underlying event is set."""

    def __init__(self, primary: threading.Event | None, secondary: threading.Event) -> None:
        self._primary = primary
        self._secondary = secondary

    def is_set(self) -> bool:
        return self._secondary.is_set() or (self._primary is not None and self._primary.is_set())


class ChromeTtsBridge:
    """Public facade orchestrating BrowserProcessManager, CdpClient, and WasmTtsEngineBridge."""

    def __init__(self, catalog: VoiceCatalog | None = None) -> None:
        self.catalog = catalog or VoiceCatalog.load()
        self._process_manager = BrowserProcessManager(self.catalog)
        self._cdp_client = CdpClient()
        self._engine = WasmTtsEngineBridge(self._cdp_client, self.catalog)
        self._engineRefLock = threading.Lock()
        self._lock = threading.RLock()
        self._connectionLock = threading.Lock()
        self._activeConnectCancelEvent: threading.Event | None = None
        self._needsRecycle = False
        self._recycleUrgent = False
        self._recycleReason = ""
        self._lastMemoryCheckAt = 0.0
        self._runtimeReadyAt = 0.0
        self._highMemorySampleCount = 0
        self._lifecycleEpoch = 0

    def _set_engine_locked(self, engine: WasmTtsEngineBridge) -> None:
        engineRefLock = getattr(self, "_engineRefLock", None)
        if engineRefLock is not None:
            with engineRefLock:
                self._engine = engine
        else:
            self._engine = engine

    @classmethod
    def find_browser(cls) -> str | None:
        return BrowserProcessManager.find_browser()

    @classmethod
    def browser_runtime_available(cls, runtime: str | None = None) -> bool:
        return BrowserProcessManager.browser_runtime_available(runtime)

    @property
    def _ws(self) -> websocket.WebSocket | None:
        return self._cdp_client.ws

    @property
    def _dispatcher(self) -> CdpDispatcher | None:
        return self._cdp_client.dispatcher

    @property
    def _chromeProcess(self) -> subprocess.Popen[bytes] | None:
        return self._process_manager.chrome_process

    @property
    def _debugPort(self) -> int | None:
        return self._process_manager.debug_port

    @property
    def _serverPort(self) -> int | None:
        return self._process_manager.server_port

    def _mark_runtime_for_recycle_locked(self, reason: str, *, urgent: bool = False) -> None:
        if not self._needsRecycle:
            if urgent or reason.startswith(("runtime error", "memory threshold")):
                log.debugWarning("Google TTS Chromium runtime marked for recycle: %s", reason)
            else:
                log.debug("Google TTS Chromium runtime marked for recycle: %s", reason)
        elif urgent and not self._recycleUrgent:
            log.debugWarning("Google TTS Chromium runtime recycle promoted to urgent: %s", reason)
        self._needsRecycle = True
        self._recycleUrgent = self._recycleUrgent or urgent
        self._recycleReason = reason

    def _mark_runtime_error_for_recycle(self, error: BaseException) -> None:
        if not _runtime_error_requires_recycle(error):
            return
        detail = str(getattr(error, "technicalDetail", "") or error.__class__.__name__)
        reason = detail.splitlines()[0] if detail else error.__class__.__name__
        with self._lock:
            self._mark_runtime_for_recycle_locked(f"runtime error: {reason}", urgent=True)

    def _mark_memory_recycle_if_needed_locked(self) -> None:
        now = time.monotonic()
        if self._runtimeReadyAt and now - self._runtimeReadyAt < RUNTIME_MEMORY_STARTUP_GRACE_SECONDS:
            return
        if now - self._lastMemoryCheckAt < RUNTIME_MEMORY_CHECK_INTERVAL_SECONDS:
            return
        self._lastMemoryCheckAt = now
        usage = self._process_manager.browser_memory_usage()
        if usage is None:
            return
        privateBytes = int(usage.get("privateBytes", 0))
        workingSetBytes = int(usage.get("workingSetBytes", 0))
        if (
            privateBytes <= RUNTIME_PRIVATE_BYTES_RECYCLE_THRESHOLD
            and workingSetBytes <= RUNTIME_WORKING_SET_BYTES_RECYCLE_THRESHOLD
        ):
            self._highMemorySampleCount = 0
            return
        reason = (
            "memory threshold exceeded "
            f"(private={_format_bytes(privateBytes)}, "
            f"workingSet={_format_bytes(workingSetBytes)}, "
            f"processes={usage.get('processCount', 0)})"
        )
        self._highMemorySampleCount += 1
        if self._highMemorySampleCount < RUNTIME_MEMORY_RECYCLE_CONFIRMATIONS:
            log.debugWarning(
                "Google TTS Chromium runtime memory above threshold (%d/%d): %s",
                self._highMemorySampleCount,
                RUNTIME_MEMORY_RECYCLE_CONFIRMATIONS,
                reason,
            )
            return
        self._mark_runtime_for_recycle_locked(reason, urgent=False)

    def _reset_runtime_locked(self) -> None:
        self._lifecycleEpoch = getattr(self, "_lifecycleEpoch", 0) + 1
        activeConnectCancel = getattr(self, "_activeConnectCancelEvent", None)
        if activeConnectCancel is not None:
            activeConnectCancel.set()
        self._cdp_client.close()
        self._process_manager.terminate()
        self._set_engine_locked(WasmTtsEngineBridge(self._cdp_client, self.catalog))
        self._needsRecycle = False
        self._recycleUrgent = False
        self._recycleReason = ""
        self._lastMemoryCheckAt = 0.0
        self._runtimeReadyAt = 0.0
        self._highMemorySampleCount = 0

    def maybe_recycle_runtime(self, *, allowIdleRecycle: bool = True, checkMemory: bool = True) -> bool:
        with self._lock:
            if checkMemory:
                self._mark_memory_recycle_if_needed_locked()
            if not self._needsRecycle:
                return False
            if self._engine.runtime_busy:
                return False
            if not self._recycleUrgent and not allowIdleRecycle:
                return False
            reason = self._recycleReason or "runtime health check"
            log.debug("Recycling Google TTS Chromium runtime: %s", reason)
            self._reset_runtime_locked()
            return True

    def is_connected(self) -> bool:
        with self._lock:
            return self._cdp_client.is_connected()

    def update_catalog(self, catalog: VoiceCatalog) -> None:
        with self._lock:
            oldIds = tuple(pkg.id for pkg in self.catalog.packages)
            newIds = tuple(pkg.id for pkg in catalog.packages)
            self.catalog = catalog
            self._process_manager.update_catalog(catalog)
            self._engine.catalog = catalog
            if oldIds != newIds and (
                self._cdp_client.is_connected()
                or self._process_manager.chrome_process is not None
                or getattr(self, "_activeConnectCancelEvent", None) is not None
            ):
                self._mark_runtime_for_recycle_locked("installed voice catalog changed", urgent=True)
        if oldIds != newIds:
            with suppress(Exception):
                self.maybe_recycle_runtime(allowIdleRecycle=True, checkMemory=False)

    def safe_for_standby_release(self) -> bool:
        """Return whether standby may safely keep and reuse this runtime."""
        with self._lock:
            return not self._needsRecycle and not self._engine.runtime_busy and self._cdp_client.is_connected()

    def ensure_connection(self, cancelEvent: threading.Event | None = None) -> None:
        _raise_if_cancelled(cancelEvent)
        with self._lock:
            if self._cdp_client.is_connected():
                return
            startEpoch = getattr(self, "_lifecycleEpoch", 0)
        connLock = getattr(self, "_connectionLock", None)
        if connLock is None:
            connLock = threading.Lock()
            self._connectionLock = connLock
        while not connLock.acquire(timeout=0.05):
            _raise_if_cancelled(cancelEvent)
            if getattr(self, "_lifecycleEpoch", 0) != startEpoch:
                raise CdpCancelled()
        connectAbortEvent = threading.Event()
        effectiveCancelEvent: Any = (
            _CompositeCancelEvent(cancelEvent, connectAbortEvent) if cancelEvent is not None else connectAbortEvent
        )
        try:
            with self._lock:
                if getattr(self, "_lifecycleEpoch", 0) != startEpoch:
                    raise CdpCancelled()
                if self._cdp_client.is_connected():
                    return
                self._activeConnectCancelEvent = connectAbortEvent
            skipRuntimes: set[str] = set()
            connectionFailures: list[str] = []
            while True:
                _raise_if_cancelled(effectiveCancelEvent)
                with self._lock:
                    if getattr(self, "_lifecycleEpoch", 0) != startEpoch:
                        raise CdpCancelled()
                    processManager = self._process_manager
                    cdpClient = self._cdp_client
                    engine = self._engine
                runtime: str | None = None
                chromeGetter = getattr(processManager, "chrome_process", None)
                existingProc = chromeGetter() if callable(chromeGetter) else chromeGetter
                hadExistingChrome = existingProc is not None and existingProc.poll() is None
                gotWebsocketUrl = False
                try:
                    startedAt = time.perf_counter()
                    wsUrl = processManager.start_and_get_websocket_url(
                        cancelEvent=effectiveCancelEvent,
                        skipRuntimes=skipRuntimes,
                    )
                    gotWebsocketUrl = True
                    websocketReadyAt = time.perf_counter()
                    runtime = processManager.profile_runtime
                    profilePersistent = getattr(processManager, "_profileIsPersistent", None)
                    _raise_if_cancelled(effectiveCancelEvent)
                    cdpClient.connect(wsUrl)
                    cdpConnectedAt = time.perf_counter()
                    engine.enable_cdp_domains(cancelEvent=effectiveCancelEvent)
                    cdpDomainsEnabledAt = time.perf_counter()
                    engine.wait_until_ready(cancelEvent=effectiveCancelEvent)
                    harnessReadyAt = time.perf_counter()
                    clearStderr = getattr(processManager, "clear_startup_stderr", None)
                    if callable(clearStderr):
                        clearStderr()
                    with self._lock:
                        if getattr(self, "_lifecycleEpoch", 0) != startEpoch:
                            cdpClient.close()
                            processManager.terminate()
                            raise CdpCancelled()
                        self._runtimeReadyAt = time.monotonic()
                        self._lastMemoryCheckAt = self._runtimeReadyAt
                        self._highMemorySampleCount = 0
                    profileMode = (
                        "persistent"
                        if profilePersistent is True
                        else ("temporary" if profilePersistent is False else "unknown")
                    )
                    log.info(
                        "Google TTS connected to %s browser runtime (engine %s, %s profile).",
                        BROWSER_RUNTIME_LABELS.get(runtime or "", runtime or "unknown"),
                        ENGINE_VERSION,
                        profileMode,
                    )
                    log.debug(
                        "Google TTS %s browser startup timings: browser target %.1f ms, CDP connect %.1f ms, CDP domains %.1f ms, harness ready %.1f ms, total %.1f ms.",
                        runtime or "unknown",
                        (websocketReadyAt - startedAt) * 1000,
                        (cdpConnectedAt - websocketReadyAt) * 1000,
                        (cdpDomainsEnabledAt - cdpConnectedAt) * 1000,
                        (harnessReadyAt - cdpDomainsEnabledAt) * 1000,
                        (harnessReadyAt - startedAt) * 1000,
                    )
                    return
                except CdpCancelled:
                    with self._lock:
                        if getattr(self, "_lifecycleEpoch", 0) == startEpoch:
                            cdpClient.close()
                            currentGetter = getattr(processManager, "chrome_process", None)
                            currentProc = currentGetter() if callable(currentGetter) else currentGetter
                            if currentProc is not None and not (hadExistingChrome and currentProc is existingProc):
                                stopBrowserProcess = getattr(
                                    processManager,
                                    "stop_browser_process",
                                    getattr(processManager, "_stop_browser_process", None),
                                )
                                if callable(stopBrowserProcess):
                                    with suppress(Exception):
                                        stopBrowserProcess(removeProfile=False)
                    raise
                except Exception as exc:
                    with suppress(Exception):
                        stderrGetter = getattr(processManager, "startup_stderr_snippet", None)
                        existingDetail = str(getattr(exc, "technicalDetail", "") or exc)
                        if callable(stderrGetter) and "[stderr:" not in existingDetail:
                            currentGetter = getattr(processManager, "chrome_process", None)
                            currentProc = currentGetter() if callable(currentGetter) else currentGetter
                            pollFn = getattr(currentProc, "poll", None)
                            procExited = callable(pollFn) and pollFn() is not None
                            stderrSnippet = stderrGetter(
                                timeout=0.1 if procExited else 0.0,
                                cancelEvent=effectiveCancelEvent,
                            )
                            if stderrSnippet:
                                _append_technical_detail(exc, f"[stderr: {stderrSnippet}]")
                    with self._lock:
                        epochChanged = getattr(self, "_lifecycleEpoch", 0) != startEpoch
                        if not epochChanged:
                            cdpClient.close()
                            runtime = runtime or processManager.profile_runtime
                            processManager.terminate()
                    if epochChanged or effectiveCancelEvent.is_set():
                        raise CdpCancelled() from None
                    if gotWebsocketUrl and runtime:
                        runtime = _normalize_browser_runtime(runtime)
                        skipRuntimes.add(runtime)
                        if _browser_choices(skipRuntimes=skipRuntimes):
                            connectionFailures.append(_format_runtime_failure_detail(runtime, exc))
                            processManager._log_browser_runtime_failure(runtime, exc, True)
                            continue
                    if connectionFailures:
                        _append_technical_detail(
                            exc,
                            f"| Previous connection failures: {'; '.join(connectionFailures)}",
                        )
                    if gotWebsocketUrl and runtime:
                        logFailure = getattr(processManager, "_log_browser_runtime_failure", None)
                        if callable(logFailure):
                            logFailure(runtime, exc, False)
                    raise
        finally:
            if getattr(self, "_activeConnectCancelEvent", None) is connectAbortEvent:
                self._activeConnectCancelEvent = None
            connLock.release()

    def preload_voice(self, options: dict[str, Any], cancelEvent: threading.Event | None = None) -> dict[str, Any]:
        connected = False
        try:
            _raise_if_cancelled(cancelEvent)
            self.maybe_recycle_runtime(allowIdleRecycle=False, checkMemory=False)
            self.ensure_connection(cancelEvent=cancelEvent)
            connected = True
            with self._lock:
                engine = self._engine
            return engine.preload_voice(options, cancelEvent=cancelEvent)
        except CdpCancelled:
            raise
        except Exception as exc:
            if connected:
                self._mark_runtime_error_for_recycle(exc)
            raise

    def speak(
        self,
        text: str,
        options: dict[str, Any],
        onAudio: AudioCallback,
        cancelEvent: threading.Event | None = None,
        onMark: MarkCallback | None = None,
        onSegmentEnd: SegmentEndCallback | None = None,
        segments: list[str] | None = None,
        hasPreviousSegment: bool = False,
    ) -> dict[str, Any]:
        attempt = 0
        while True:
            connected = False
            try:
                _raise_if_cancelled(cancelEvent)
                self.maybe_recycle_runtime(allowIdleRecycle=False, checkMemory=False)
                self.ensure_connection(cancelEvent=cancelEvent)
                connected = True
                with self._lock:
                    engine = self._engine
                return engine.speak(
                    text,
                    options,
                    onAudio,
                    cancelEvent=cancelEvent,
                    onMark=onMark,
                    onSegmentEnd=onSegmentEnd,
                    segments=segments,
                    hasPreviousSegment=hasPreviousSegment,
                )
            except CdpCancelled:
                raise
            except Exception as exc:
                if connected:
                    self._mark_runtime_error_for_recycle(exc)
                detail = str(getattr(exc, "technicalDetail", "") or "")
                retryableConnectionLoss = connected and (
                    isinstance(exc, _BrowserSpeechError)
                    or (
                        isinstance(exc, CdpError)
                        and _runtime_error_requires_recycle(exc)
                        and any(
                            marker in detail for marker in ("Browser DevTools websocket", "Could not send CDP command")
                        )
                    )
                )
                if retryableConnectionLoss:
                    recycled = False
                    try:
                        recycled = self.maybe_recycle_runtime(
                            allowIdleRecycle=True,
                            checkMemory=False,
                        )
                    except Exception:
                        log.debugWarning("Could not recycle the failed Google TTS browser runtime.", exc_info=True)
                    if attempt == 0 and recycled and not getattr(exc, "audioStarted", False):
                        attempt += 1
                        log.debugWarning("Retrying Google TTS speech once with a fresh browser runtime.")
                        continue
                raise

    def stop_runtime(self) -> None:
        try:
            with self._lock:
                if not self._cdp_client.is_connected():
                    return
                engine = self._engine
            engine.stop_runtime()
        except Exception:
            log.debugWarning("Could not stop Google TTS Chromium browser runtime.", exc_info=True)

    def cancel_current(self) -> None:
        engineRefLock = getattr(self, "_engineRefLock", None) or self._lock
        with engineRefLock:
            engine = self._engine
        engine.cancel_current()

    def terminate(self) -> None:
        activeConnectCancel = getattr(self, "_activeConnectCancelEvent", None)
        if activeConnectCancel is not None:
            activeConnectCancel.set()
        with self._lock:
            self._reset_runtime_locked()
