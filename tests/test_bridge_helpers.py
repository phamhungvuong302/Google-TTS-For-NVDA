"""Tests for pure helper functions in bridge.py.

These tests exercise browser discovery helpers, config normalization,
path safety, error classification, and utility functions that can run
without NVDA or a real browser process.
"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tests.test_support import FakeConf, load_driver_module

bridge = load_driver_module("bridge")


# ---------------------------------------------------------------------------
# _safe_join
# ---------------------------------------------------------------------------


class SafeJoinTests(unittest.TestCase):
    """Verify _safe_join prevents path traversal."""

    def setUp(self) -> None:
        import tempfile

        self._tmpdir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self._root = self._tmpdir / "root"
        self._root.mkdir()

    def test_normal_relative_path(self) -> None:
        result = bridge._safe_join(self._root, "subdir/file.txt")
        self.assertEqual((self._root / "subdir" / "file.txt").resolve(), result)

    def test_rejects_dotdot(self) -> None:
        result = bridge._safe_join(self._root, "../etc/passwd")
        self.assertEqual((self._root / "__invalid__").resolve(), result)

    def test_rejects_absolute_path(self) -> None:
        result = bridge._safe_join(self._root, "/etc/passwd")
        self.assertEqual((self._root / "__invalid__").resolve(), result)

    def test_rejects_encoded_dotdot(self) -> None:
        result = bridge._safe_join(self._root, "%2e%2e/etc/passwd")
        self.assertEqual((self._root / "__invalid__").resolve(), result)

    def test_accepts_clean_relative_path(self) -> None:
        result = bridge._safe_join(self._root, "clean/path")
        self.assertTrue(result.resolve().is_relative_to(self._root.resolve()))

    def test_rejects_null_byte_and_encoded_null(self) -> None:
        for badPath in ("foo\x00bar", "voices/%00evil", "%00"):
            with self.subTest(badPath=badPath):
                result = bridge._safe_join(self._root, badPath)
                self.assertEqual((self._root / "__invalid__").resolve(), result)

    def test_bridge_http_headers_and_reuse_address_policy(self) -> None:
        import os

        self.assertEqual(os.name != "nt", bridge._ThreadingTcpServer.allow_reuse_address)

        def collect_headers(requestPath: str) -> dict[str, str]:
            sent: dict[str, str] = {}
            handler = bridge._BridgeRequestHandler.__new__(bridge._BridgeRequestHandler)
            handler.path = requestPath
            handler.request_version = "HTTP/1.1"
            handler.send_header = lambda name, value: sent.__setitem__(name, value)  # type: ignore[method-assign]
            handler.flush_headers = lambda: None  # type: ignore[method-assign]
            handler._headers_buffer = []
            bridge._BridgeRequestHandler.end_headers(handler)
            return sent

        for noStorePath in ("/", "/index.html", "/voices.json", "/bridgeHarness.js", "/voices.json?v=1"):
            headers = collect_headers(noStorePath)
            self.assertEqual("no-store", headers.get("Cache-Control"), noStorePath)
            self.assertEqual("same-origin", headers.get("Cross-Origin-Opener-Policy"))
            self.assertEqual("require-corp", headers.get("Cross-Origin-Embedder-Policy"))
        engineHeaders = collect_headers("/engine/bindings_main.wasm")
        self.assertNotIn("Cache-Control", engineHeaders)


# ---------------------------------------------------------------------------
# _normalize_browser_runtime
# ---------------------------------------------------------------------------


class NormalizeBrowserRuntimeTests(unittest.TestCase):
    """Verify _normalize_browser_runtime maps inputs to valid runtimes."""

    def test_chrome_variants(self) -> None:
        for value in ("chrome", "Chrome", "CHROME", " chrome "):
            with self.subTest(value=value):
                self.assertEqual("chrome", bridge._normalize_browser_runtime(value))

    def test_edge_variants(self) -> None:
        for value in ("edge", "Edge", "EDGE"):
            with self.subTest(value=value):
                self.assertEqual("edge", bridge._normalize_browser_runtime(value))

    def test_brave_variants(self) -> None:
        for value in ("brave", "Brave", "BRAVE"):
            with self.subTest(value=value):
                self.assertEqual("brave", bridge._normalize_browser_runtime(value))

    def test_unknown_defaults_to_chrome(self) -> None:
        self.assertEqual("chrome", bridge._normalize_browser_runtime("unknown"))
        self.assertEqual("chrome", bridge._normalize_browser_runtime(""))
        self.assertEqual("chrome", bridge._normalize_browser_runtime(None))


# ---------------------------------------------------------------------------
# _runtime_fallback_order
# ---------------------------------------------------------------------------


class RuntimeFallbackOrderTests(unittest.TestCase):
    """Verify _runtime_fallback_order puts preferred first, then chrome, edge, brave."""

    def test_preferred_is_first(self) -> None:
        order = bridge._runtime_fallback_order("edge")
        self.assertEqual("edge", order[0])

    def test_chrome_is_second_by_default(self) -> None:
        order = bridge._runtime_fallback_order("chrome")
        self.assertEqual("chrome", order[0])
        self.assertEqual("edge", order[1])

    def test_all_runtimes_present(self) -> None:
        order = bridge._runtime_fallback_order("brave")
        self.assertEqual(3, len(order))
        self.assertIn("chrome", order)
        self.assertIn("edge", order)
        self.assertIn("brave", order)

    def test_no_duplicates(self) -> None:
        order = bridge._runtime_fallback_order("chrome")
        self.assertEqual(len(order), len(set(order)))


# ---------------------------------------------------------------------------
# _format_bytes
# ---------------------------------------------------------------------------


class FormatBytesTests(unittest.TestCase):
    """Verify _format_bytes formats byte counts as MB."""

    def test_one_mb(self) -> None:
        self.assertEqual("1.0 MB", bridge._format_bytes(1024 * 1024))

    def test_zero_bytes(self) -> None:
        self.assertEqual("0.0 MB", bridge._format_bytes(0))

    def test_half_mb(self) -> None:
        self.assertEqual("0.5 MB", bridge._format_bytes(512 * 1024))


# ---------------------------------------------------------------------------
# _is_transient_runtime_evaluate_error
# ---------------------------------------------------------------------------


class TransientErrorClassificationTests(unittest.TestCase):
    """Verify _is_transient_runtime_evaluate_error classifies CDP errors."""

    def test_transient_error_detected(self) -> None:
        error = bridge.CdpError(
            "CDP error",
            "CDP error for Runtime.evaluate: Cannot find default execution context",
        )
        self.assertTrue(bridge._is_transient_runtime_evaluate_error(error))

    def test_non_transient_error_not_detected(self) -> None:
        error = bridge.CdpError("Connection refused", "Connection refused")
        self.assertFalse(bridge._is_transient_runtime_evaluate_error(error))

    def test_non_cdp_error_not_detected(self) -> None:
        self.assertFalse(bridge._is_transient_runtime_evaluate_error(Exception("foo")))


# ---------------------------------------------------------------------------
# _runtime_error_requires_recycle
# ---------------------------------------------------------------------------


class RuntimeRecycleClassificationTests(unittest.TestCase):
    """Verify _runtime_error_requires_recycle classifies errors for recycling."""

    def test_browser_speech_error_requires_recycle(self) -> None:
        error = bridge._BrowserSpeechError("Could not speak", "detail", audioStarted=False)
        self.assertTrue(bridge._runtime_error_requires_recycle(error))

    def test_cdp_cancelled_does_not_require_recycle(self) -> None:
        self.assertFalse(bridge._runtime_error_requires_recycle(bridge.CdpCancelled()))

    def test_timeout_error_requires_recycle(self) -> None:
        error = bridge.CdpError("Timed out", "Timed out waiting for Runtime.evaluate")
        self.assertTrue(bridge._runtime_error_requires_recycle(error))

    def test_generic_exception_requires_recycle(self) -> None:
        self.assertTrue(bridge._runtime_error_requires_recycle(RuntimeError("oops")))

    def test_wasm_preload_timeout_and_missing_binding_require_recycle(self) -> None:
        for detail in (
            "Error: Timed out waiting for WASM engine synthesis to complete.",
            "Error: CDP bridge binding googleTtsForNvdaBridge is not available.",
        ):
            with self.subTest(detail=detail):
                error = bridge.CdpError("runtime error", detail)
                self.assertTrue(bridge._runtime_error_requires_recycle(error))


# ---------------------------------------------------------------------------
# _raise_if_cancelled
# ---------------------------------------------------------------------------


class RaiseIfCancelledTests(unittest.TestCase):
    """Verify _raise_if_cancelled raises CdpCancelled when event is set."""

    def test_set_event_raises(self) -> None:
        event = threading.Event()
        event.set()
        with self.assertRaises(bridge.CdpCancelled):
            bridge._raise_if_cancelled(event)

    def test_unset_event_passes(self) -> None:
        event = threading.Event()
        bridge._raise_if_cancelled(event)  # should not raise

    def test_none_event_passes(self) -> None:
        bridge._raise_if_cancelled(None)  # should not raise


# ---------------------------------------------------------------------------
# browser_runtime_for_path
# ---------------------------------------------------------------------------


class BrowserRuntimeForPathTests(unittest.TestCase):
    """Verify browser_runtime_for_path maps executable names to runtimes."""

    def test_msedge_exe(self) -> None:
        self.assertEqual("edge", bridge.browser_runtime_for_path("/Program Files/Microsoft/Edge/msedge.exe"))

    def test_msedge_no_ext(self) -> None:
        self.assertEqual("edge", bridge.browser_runtime_for_path("/usr/bin/msedge"))

    def test_brave_exe(self) -> None:
        self.assertEqual("brave", bridge.browser_runtime_for_path("/Brave/brave.exe"))

    def test_chrome_exe(self) -> None:
        self.assertEqual("chrome", bridge.browser_runtime_for_path("/Chrome/chrome.exe"))

    def test_unknown_defaults_to_chrome(self) -> None:
        self.assertEqual("chrome", bridge.browser_runtime_for_path("/something/unknown.exe"))


# ---------------------------------------------------------------------------
# _friendly_cdp_error
# ---------------------------------------------------------------------------


class FriendlyCdpErrorTests(unittest.TestCase):
    """Verify _friendly_cdp_error creates CdpError with technical detail."""

    def test_message_only(self) -> None:
        error = bridge._friendly_cdp_error("Something failed")
        self.assertIsInstance(error, bridge.CdpError)
        self.assertEqual("Something failed", str(error))
        self.assertIsNone(error.technicalDetail)

    def test_message_with_detail(self) -> None:
        error = bridge._friendly_cdp_error("Something failed", "detail info")
        self.assertEqual("Something failed", str(error))
        self.assertEqual("detail info", error.technicalDetail)


# ---------------------------------------------------------------------------
# browser_runtime_snapshot
# ---------------------------------------------------------------------------


class BrowserRuntimeSnapshotTests(unittest.TestCase):
    """Verify browser_runtime_snapshot returns a complete status dict."""

    def test_snapshot_returns_dict_with_expected_keys(self) -> None:
        snapshot = bridge.browser_runtime_snapshot()
        self.assertIsInstance(snapshot, dict)
        for key in (
            "selectedRuntime",
            "paths",
            "executableAvailability",
            "edgeWebView2Available",
            "availability",
            "effectivePath",
            "effectiveRuntime",
        ):
            self.assertIn(key, snapshot, f"Missing key: {key}")

    def test_selected_runtime_defaults_to_configured(self) -> None:
        snapshot = bridge.browser_runtime_snapshot()
        self.assertEqual(bridge.configured_browser_runtime(), snapshot["selectedRuntime"])

    def test_paths_are_strings_or_none(self) -> None:
        snapshot = bridge.browser_runtime_snapshot()
        for runtime, path in snapshot["paths"].items():
            self.assertIn(runtime, bridge.BROWSER_RUNTIMES)
            self.assertTrue(path is None or isinstance(path, str))

    def test_availability_matches_executable_and_edge(self) -> None:
        snapshot = bridge.browser_runtime_snapshot()
        for runtime in bridge.BROWSER_RUNTIMES:
            exe = snapshot["executableAvailability"].get(runtime, False)
            edge_ok = snapshot["edgeWebView2Available"]
            expected = bool(exe) and (runtime != bridge.BROWSER_RUNTIME_EDGE or edge_ok)
            self.assertEqual(expected, snapshot["availability"].get(runtime, False))


# ---------------------------------------------------------------------------
# edge_webview2_blocks_effective_runtime
# ---------------------------------------------------------------------------


class EdgeWebview2BlocksTests(unittest.TestCase):
    """Verify edge_webview2_blocks_effective_runtime returns a bool."""

    def test_returns_bool(self) -> None:
        result = bridge.edge_webview2_blocks_effective_runtime()
        self.assertIsInstance(result, bool)

    def test_with_explicit_runtime(self) -> None:
        for runtime in bridge.BROWSER_RUNTIMES:
            result = bridge.edge_webview2_blocks_effective_runtime(runtime)
            self.assertIsInstance(result, bool)


# ---------------------------------------------------------------------------
# effective_browser_runtime
# ---------------------------------------------------------------------------


class EffectiveBrowserRuntimeTests(unittest.TestCase):
    """Verify effective_browser_runtime returns a runtime string or None."""

    def test_returns_str_or_none(self) -> None:
        result = bridge.effective_browser_runtime()
        self.assertTrue(result is None or isinstance(result, str))

    def test_with_explicit_runtime(self) -> None:
        for runtime in bridge.BROWSER_RUNTIMES:
            result = bridge.effective_browser_runtime(runtime)
            self.assertTrue(result is None or isinstance(result, str))


# ---------------------------------------------------------------------------
# configured_browser_runtime
# ---------------------------------------------------------------------------


class ConfiguredBrowserRuntimeTests(unittest.TestCase):
    """Verify configured_browser_runtime returns a valid runtime string."""

    def test_returns_valid_runtime(self) -> None:
        runtime = bridge.configured_browser_runtime()
        self.assertIn(runtime, bridge.BROWSER_RUNTIMES)


# ---------------------------------------------------------------------------
# browser_executable_available
# ---------------------------------------------------------------------------


class BrowserExecutableAvailableTests(unittest.TestCase):
    """Verify browser_executable_available returns a bool."""

    def test_returns_bool(self) -> None:
        for runtime in bridge.BROWSER_RUNTIMES:
            result = bridge.browser_executable_available(runtime)
            self.assertIsInstance(result, bool)


# ---------------------------------------------------------------------------
# browser_availability
# ---------------------------------------------------------------------------


class BrowserAvailabilityTests(unittest.TestCase):
    """Verify browser_availability returns a dict of bools."""

    def test_returns_dict_with_all_runtimes(self) -> None:
        avail = bridge.browser_availability()
        self.assertIsInstance(avail, dict)
        for runtime in bridge.BROWSER_RUNTIMES:
            self.assertIn(runtime, avail)
            self.assertIsInstance(avail[runtime], bool)


# ---------------------------------------------------------------------------
# _browser_choices
# ---------------------------------------------------------------------------


class BrowserChoicesTests(unittest.TestCase):
    """Verify _browser_choices filters by availability."""

    def test_returns_tuple(self) -> None:
        choices = bridge._browser_choices()
        self.assertIsInstance(choices, tuple)

    def test_each_choice_is_runtime_path_pair(self) -> None:
        choices = bridge._browser_choices()
        for runtime, path in choices:
            self.assertIn(runtime, bridge.BROWSER_RUNTIMES)
            self.assertIsInstance(path, str)

    def test_skip_runtimes_excludes(self) -> None:
        choices = bridge._browser_choices(skipRuntimes={bridge.BROWSER_RUNTIME_CHROME})
        for runtime, _path in choices:
            self.assertNotEqual(bridge.BROWSER_RUNTIME_CHROME, runtime)


# ---------------------------------------------------------------------------
# browser_runtime_available
# ---------------------------------------------------------------------------


class BrowserRuntimeAvailableTests(unittest.TestCase):
    """Verify browser_runtime_available and classmethod delegations."""

    def test_browser_runtime_available_without_args(self) -> None:
        avail = bridge.browser_runtime_available()
        self.assertIsInstance(avail, bool)

    def test_browser_runtime_available_with_known_runtimes(self) -> None:
        for runtime in bridge.BROWSER_RUNTIMES:
            avail = bridge.browser_runtime_available(runtime)
            self.assertIsInstance(avail, bool)

    def test_browser_runtime_available_unknown_runtime_returns_false(self) -> None:
        self.assertFalse(bridge.browser_runtime_available("non_existent_browser_runtime"))

    def test_browser_process_manager_delegation(self) -> None:
        self.assertEqual(
            bridge.browser_runtime_available(),
            bridge.BrowserProcessManager.browser_runtime_available(),
        )
        for runtime in bridge.BROWSER_RUNTIMES:
            self.assertEqual(
                bridge.browser_runtime_available(runtime),
                bridge.BrowserProcessManager.browser_runtime_available(runtime),
            )

    def test_chrome_tts_bridge_delegation(self) -> None:
        self.assertEqual(
            bridge.browser_runtime_available(),
            bridge.ChromeTtsBridge.browser_runtime_available(),
        )
        for runtime in bridge.BROWSER_RUNTIMES:
            self.assertEqual(
                bridge.browser_runtime_available(runtime),
                bridge.ChromeTtsBridge.browser_runtime_available(runtime),
            )


# ---------------------------------------------------------------------------
# _browser_profile_in_use_error
# ---------------------------------------------------------------------------


class BrowserProfileInUseErrorTests(unittest.TestCase):
    """Verify profile-in-use error creation with exit codes."""

    def test_default_exit_code(self) -> None:
        err = bridge._browser_profile_in_use_error()
        self.assertIn("21", err.technicalDetail)

    def test_custom_exit_code_zero(self) -> None:
        err = bridge._browser_profile_in_use_error(0)
        self.assertIn("0", err.technicalDetail)


# ---------------------------------------------------------------------------
# _elevate_chrome_priority
# ---------------------------------------------------------------------------


class ElevateChromePriorityTests(unittest.TestCase):
    """Verify _elevate_chrome_priority handles process tree and invalid PIDs safely."""

    def test_elevate_current_process_does_not_raise(self) -> None:
        import ctypes
        import os
        import sys

        if sys.platform != "win32":
            bridge._elevate_chrome_priority(os.getpid())
            return

        class _PowerThrottlingState(ctypes.Structure):
            _fields_ = [
                ("Version", ctypes.c_uint32),
                ("ControlMask", ctypes.c_uint32),
                ("StateMask", ctypes.c_uint32),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)(
            ("OpenProcess", kernel32)
        )
        get_priority_class = ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(("GetPriorityClass", kernel32))
        set_priority_class = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)(
            ("SetPriorityClass", kernel32)
        )
        close_handle = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p)(("CloseHandle", kernel32))

        process_power_throttling = 4
        get_process_information = (
            ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)(
                ("GetProcessInformation", kernel32)
            )
            if hasattr(kernel32, "GetProcessInformation")
            else None
        )
        set_process_information = (
            ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)(
                ("SetProcessInformation", kernel32)
            )
            if hasattr(kernel32, "SetProcessInformation")
            else None
        )

        process_query_limited_information = 0x1000
        process_set_information = 0x0200
        handle = open_process(process_query_limited_information | process_set_information, False, os.getpid())
        prior_priority = get_priority_class(handle) if handle else 0
        prior_throttling = _PowerThrottlingState(1, 0, 0)
        has_prior_throttling = bool(
            handle
            and get_process_information is not None
            and get_process_information(
                handle,
                process_power_throttling,
                ctypes.cast(ctypes.byref(prior_throttling), ctypes.c_void_p),
                ctypes.sizeof(prior_throttling),
            )
        )
        try:
            bridge._elevate_chrome_priority(os.getpid())
        finally:
            if handle:
                if prior_priority:
                    set_priority_class(handle, prior_priority)
                if set_process_information is not None:
                    restore_state = prior_throttling if has_prior_throttling else _PowerThrottlingState(1, 0, 0)
                    set_process_information(
                        handle,
                        process_power_throttling,
                        ctypes.cast(ctypes.byref(restore_state), ctypes.c_void_p),
                        ctypes.sizeof(restore_state),
                    )
                close_handle(handle)

    def test_elevate_invalid_pid_does_not_raise(self) -> None:
        bridge._elevate_chrome_priority(999999)


class ProcessMemoryCountersTests(unittest.TestCase):
    """Verify _process_memory_counters prefers limited query rights and falls back safely."""

    def test_prefers_limited_information_before_vm_read(self) -> None:
        import ctypes
        import os
        from unittest.mock import MagicMock, patch

        if os.name != "nt":
            self.assertIsNone(bridge._process_memory_counters(os.getpid()))
            return

        requested_access: list[int] = []
        fake_handle = 0x1234

        def fake_open_process(desired_access: int, _inherit: bool, _pid: int) -> int:
            requested_access.append(desired_access)
            return fake_handle if desired_access == 0x1000 else 0

        def fake_get_memory_info(_handle: int, counters_ptr: Any, _size: int) -> int:
            counters = ctypes.cast(counters_ptr, ctypes.POINTER(ctypes.c_byte))
            # Just return failure if size unexpected; otherwise leave zeroed counters with valid success.
            _ = counters
            return 1

        fake_kernel32 = MagicMock()
        fake_psapi = MagicMock()

        def fake_win_dll(name: str, **_kwargs: Any) -> Any:
            return fake_kernel32 if name == "kernel32" else fake_psapi

        def _query(open_fn: Any) -> tuple[int, int] | None:
            def _winfunctype(_restype: Any, *_argtypes: Any) -> Any:
                def factory(spec: tuple[str, Any]) -> Any:
                    fn_name, _dll = spec
                    if fn_name == "OpenProcess":
                        return open_fn
                    if fn_name == "GetProcessMemoryInfo":
                        return fake_get_memory_info
                    return lambda *_a: 1

                return factory

            with (
                patch.object(ctypes, "WinDLL", side_effect=fake_win_dll),
                patch.object(ctypes, "WINFUNCTYPE", side_effect=_winfunctype),
            ):
                return bridge._process_memory_counters(4321)

        result = _query(fake_open_process)
        self.assertEqual([0x1000], requested_access)
        self.assertEqual((0, 0), result)

        requested_access.clear()

        def fallback_open_process(desired_access: int, _inherit: bool, _pid: int) -> int:
            requested_access.append(desired_access)
            return 0 if desired_access == 0x1000 else fake_handle

        fallback_result = _query(fallback_open_process)
        self.assertEqual([0x1000, 0x0410], requested_access)
        self.assertEqual((0, 0), fallback_result)


# ---------------------------------------------------------------------------
# _hidden_chrome_startup_kwargs & browser process creation handles
# ---------------------------------------------------------------------------


class HiddenChromeStartupKwargsTests(unittest.TestCase):
    """Verify _hidden_chrome_startup_kwargs returns proper startup flags."""

    def test_hidden_chrome_startup_kwargs(self) -> None:
        import os
        import subprocess

        kwargs = bridge._hidden_chrome_startup_kwargs()
        if os.name == "nt":
            self.assertIn("startupinfo", kwargs)
            self.assertIn("creationflags", kwargs)
            startup_info = kwargs["startupinfo"]
            self.assertTrue(startup_info.dwFlags & subprocess.STARTF_USESHOWWINDOW)
            self.assertEqual(0, startup_info.wShowWindow)
            self.assertEqual(getattr(subprocess, "CREATE_NO_WINDOW", 0), kwargs["creationflags"])
        else:
            self.assertEqual({}, kwargs)


class BrowserProcessManagerSpawnTests(unittest.TestCase):
    """Verify browser process spawning and shutdown isolation/guards."""

    @staticmethod
    def _spawn_browser_with_patches(
        temp_dir: str,
        *,
        sync_profile_state: bool = False,
        read_devtools_port: Any = 9222,
        stop_calls: list[bool] | None = None,
    ) -> tuple[Any, Path, Any]:
        from unittest.mock import MagicMock, patch

        profile_dir = Path(temp_dir)
        pm = bridge.BrowserProcessManager(catalog=bridge.VoiceCatalog([]))
        pm._serverPort = 8080
        mock_proc = MagicMock()
        mock_proc.pid = 4321

        def _get_profile(runtime: str, usePersistentProfile: bool = True, cancelEvent: Any = None) -> Path:
            if sync_profile_state:
                pm._profileDir = profile_dir
                pm._profileRuntime = runtime
                pm._profileIsPersistent = usePersistentProfile
            return profile_dir

        original_stop = pm._stop_browser_process

        def _spy_stop(*, removeProfile: bool = True) -> None:
            if stop_calls is not None:
                stop_calls.append(removeProfile)
            original_stop(removeProfile=removeProfile)

        port_patch = (
            patch.object(pm, "_read_devtools_port", side_effect=read_devtools_port)
            if isinstance(read_devtools_port, BaseException)
            else patch.object(pm, "_read_devtools_port", return_value=read_devtools_port)
        )
        with (
            patch.object(pm, "_get_browser_profile_dir", side_effect=_get_profile),
            patch.object(pm, "_page_url", return_value="http://127.0.0.1:8080/index.html"),
            port_patch,
            patch.object(pm, "_stop_browser_process", side_effect=_spy_stop),
            patch.object(bridge, "_hide_chrome_windows"),
            patch.object(bridge, "_elevate_chrome_priority"),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
        ):
            pm._start_browser_choice("chrome", "placeholder_browser.exe")
        return pm, profile_dir, mock_popen

    def test_start_browser_choice_passes_isolated_stdio_handles(self) -> None:
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            _pm, _profile_dir, mock_popen = self._spawn_browser_with_patches(temp_dir)
            mock_popen.assert_called_once()
            pos_args, kwargs = mock_popen.call_args
            launch_args = pos_args[0]
            self.assertFalse(any(str(arg).startswith("--remote-allow-origins") for arg in launch_args))
            self.assertIn("--no-proxy-server", launch_args)
            self.assertEqual(subprocess.DEVNULL, kwargs.get("stdin"))
            self.assertEqual(subprocess.DEVNULL, kwargs.get("stdout"))
            self.assertEqual(subprocess.PIPE, kwargs.get("stderr"))

    def test_cdp_client_connect_suppresses_origin_header(self) -> None:
        from unittest.mock import MagicMock, patch

        client = bridge.CdpClient()
        mock_ws = MagicMock()
        mock_dispatcher = MagicMock()
        with (
            patch.object(
                bridge.websocket,
                "create_connection",
                return_value=mock_ws,
            ) as mock_create_conn,
            patch.object(bridge, "CdpDispatcher", return_value=mock_dispatcher),
        ):
            client.connect("ws://127.0.0.1:9222/devtools/page/test")
            mock_create_conn.assert_called_once_with(
                "ws://127.0.0.1:9222/devtools/page/test",
                timeout=15,
                suppress_origin=True,
                http_no_proxy=["127.0.0.1", "localhost", "::1"],
            )

    def test_start_browser_choice_stops_spawned_process_without_removing_persistent_profile_on_cancel(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            stop_calls: list[bool] = []
            with self.assertRaises(bridge.CdpCancelled):
                self._spawn_browser_with_patches(
                    temp_dir,
                    sync_profile_state=True,
                    read_devtools_port=bridge.CdpCancelled(),
                    stop_calls=stop_calls,
                )

            self.assertEqual([False], stop_calls)
            self.assertTrue(Path(temp_dir).exists())

    def test_get_browser_profile_dir_switches_between_persistent_and_temporary(self) -> None:
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pm = bridge.BrowserProcessManager(catalog=bridge.VoiceCatalog([]))
            with patch.object(pm, "_browser_profile_root", return_value=root):
                persistentDir = pm._get_browser_profile_dir("chrome", usePersistentProfile=True)
                self.assertTrue(pm._profileIsPersistent)
                lockFile = persistentDir / "lockfile"
                lockFile.write_text("locked", encoding="utf-8")

                tempDir = pm._get_browser_profile_dir("chrome", usePersistentProfile=False)
                self.assertFalse(pm._profileIsPersistent)
                self.assertNotEqual(persistentDir, tempDir)
                self.assertTrue(tempDir.name.startswith("session-"))
                # Switching persistent -> temporary must not unlink another process's lockfile.
                self.assertTrue(lockFile.exists())

                pm._release_chrome_profile()
                self.assertFalse(tempDir.exists())

    def test_get_page_websocket_url_matches_trailing_slash_and_index_html(self) -> None:
        from unittest.mock import patch

        pm = bridge.BrowserProcessManager(catalog=bridge.VoiceCatalog([]))
        pm._serverPort = 8080
        pm._debugPort = 9222
        for candidateUrl in (
            "http://127.0.0.1:8080",
            "http://127.0.0.1:8080/",
            "http://127.0.0.1:8080/index.html",
        ):
            with self.subTest(candidateUrl=candidateUrl):
                targets = [
                    {"type": "page", "url": "about:blank", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/blank"},
                    {"type": "page", "url": candidateUrl, "webSocketDebuggerUrl": "ws://127.0.0.1:9222/bridge"},
                ]
                with patch.object(bridge, "_read_json_endpoint", return_value=targets):
                    self.assertEqual("ws://127.0.0.1:9222/bridge", pm.get_page_websocket_url())

    def test_startup_stderr_capture_caps_bytes_and_ignores_non_byte_mocks(self) -> None:
        import io
        from unittest.mock import MagicMock

        longPayload = (b"noise\r\n" * 800) + b"FATAL: policy blocked headless mode\r\n"
        capture = bridge._StartupStderrCapture(io.BytesIO(longPayload))
        formatted = capture.wait_and_format(timeout=2.0)
        self.assertTrue(formatted.startswith("..."))
        self.assertIn("FATAL: policy blocked headless mode", formatted)
        self.assertLessEqual(len(formatted), bridge._STARTUP_STDERR_SNIPPET_MAX_CHARS)
        capture.clear()
        self.assertEqual("", capture.wait_and_format(timeout=0.1))

        # Mock stream returning non-bytes must exit immediately without closing or looping.
        mockStream = MagicMock()
        mockCapture = bridge._StartupStderrCapture(mockStream)
        self.assertEqual("", mockCapture.wait_and_format(timeout=2.0))
        self.assertTrue(mockCapture._finished.is_set())
        mockStream.close.assert_not_called()

    def test_startup_stderr_capture_utf8_ansi_error_close_and_thread_cancel(self) -> None:
        import io
        import os
        import subprocess
        import sys
        import time
        from unittest.mock import MagicMock, patch

        # 1. Ring-buffer truncation mid-UTF-8 multi-byte char + ANSI CSI/OSC/BOM/NUL/C1 sanitization.
        utf8Prefix = "Tiếng Việt ".encode() * 6  # multi-byte UTF-8
        tail = b"\xef\xbb\xbf\x1b]0;win\x07\x1b[31mERROR\x1b[0m\x00\x7f  boom\r\n"
        capUtf8 = bridge._StartupStderrCapture(io.BytesIO(utf8Prefix + tail), maxBytes=len(tail) + 5)
        formatted = capUtf8.wait_and_format(timeout=2.0)
        self.assertTrue(formatted.startswith("..."))
        self.assertNotIn("\ufffd", formatted)
        self.assertNotIn("\ufeff", formatted)
        self.assertNotIn("\x1b", formatted)
        self.assertNotIn("[31m", formatted)
        self.assertNotIn("win", formatted)
        self.assertIn("ERROR boom", formatted)

        # 2. First-read OSError on a real io.IOBase stream still closes the stream.
        class _FailingStream(io.RawIOBase):
            def __init__(self) -> None:
                self.close_calls = 0

            def read(self, size: int = -1) -> bytes:
                raise OSError("pipe broken on first read")

            def close(self) -> None:
                self.close_calls += 1
                super().close()

        failing = _FailingStream()
        capFail = bridge._StartupStderrCapture(failing)
        self.assertEqual("", capFail.wait_and_format(timeout=2.0))
        self.assertTrue(capFail._finished.is_set())
        self.assertGreaterEqual(failing.close_calls, 1)

        # 3. Thread-start failure closes io.IOBase stream and never crashes or hangs.
        unstarted = _FailingStream()
        with patch.object(bridge.threading.Thread, "start", side_effect=RuntimeError("no threads")):
            capNoThread = bridge._StartupStderrCapture(unstarted)
        self.assertTrue(capNoThread._finished.is_set())
        self.assertEqual("", capNoThread.wait_and_format(timeout=2.0))
        self.assertGreaterEqual(unstarted.close_calls, 1)

        # 4. After clear(), wait_and_format returns immediately without waiting for timeout.
        class _BlockingStream(io.RawIOBase):
            def __init__(self) -> None:
                self.unblock = bridge.threading.Event()

            def read(self, size: int = -1) -> bytes:
                self.unblock.wait(timeout=5.0)
                return b""

        blocking = _BlockingStream()
        capClear = bridge._StartupStderrCapture(blocking)
        try:
            capClear.clear()
            t0 = time.monotonic()
            self.assertEqual("", capClear.wait_and_format(timeout=2.0))
            self.assertLess(time.monotonic() - t0, 0.5)
        finally:
            blocking.unblock.set()
            capClear.close()

        # 5. On Windows, close() unblocks a lingering subprocess stderr pipe via CancelSynchronousIo.
        if os.name == "nt":
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import sys, time; sys.stderr.buffer.write(b'still-open\\n'); sys.stderr.buffer.flush(); time.sleep(10)",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                capLive = bridge._StartupStderrCapture(proc.stderr)
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and not capLive.wait_and_format(timeout=0.0):
                    time.sleep(0.02)
                self.assertIn("still-open", capLive.wait_and_format(timeout=0.0))
                capLive.close()
                self.assertTrue(capLive._finished.is_set())
                assert proc.stderr is not None
                self.assertTrue(proc.stderr.closed)
            finally:
                proc.kill()
                proc.wait(timeout=2)

        # 6. ensure_connection() preserves startup stderr if wait_until_ready() fails and clears once ready.
        pm = bridge.BrowserProcessManager(catalog=bridge.VoiceCatalog([]))
        pm._stderrCapture = bridge._StartupStderrCapture(io.BytesIO(b"FATAL: wasm crash during init\r\n"))
        pm._stderrCapture._finished.wait(timeout=2.0)
        pm._chromeProcess = MagicMock()
        pm._chromeProcess.poll.return_value = 1
        pm._profileRuntime = bridge.BROWSER_RUNTIME_CHROME

        b = bridge.ChromeTtsBridge.__new__(bridge.ChromeTtsBridge)
        b._lock = bridge.threading.RLock()
        b._connectionLock = bridge.threading.Lock()
        b._engineRefLock = bridge.threading.Lock()
        b._lifecycleEpoch = 0
        b._process_manager = pm
        b._cdp_client = MagicMock()
        b._cdp_client.is_connected.return_value = False
        b._engine = MagicMock()
        b._engine.wait_until_ready.side_effect = bridge.CdpError("not ready", "harness timed out")
        with (
            patch.object(pm, "start_and_get_websocket_url", return_value="ws://127.0.0.1:9222/page"),
            patch.object(bridge, "_browser_choices", return_value=()),
            self.assertRaises(bridge.CdpError) as errCtx,
        ):
            b.ensure_connection()
        self.assertIn("FATAL: wasm crash during init", errCtx.exception.technicalDetail or "")
        self.assertIsNone(pm._stderrCapture)

        # 7. ensure_connection() attaches stderr onto non-CdpError (e.g. OSError from cdpClient.connect)
        #    and still cleans up even if startup_stderr_snippet raises.
        pm2 = bridge.BrowserProcessManager(catalog=bridge.VoiceCatalog([]))
        pm2._stderrCapture = bridge._StartupStderrCapture(io.BytesIO(b"ERROR: socket closed\r\n"))
        pm2._stderrCapture._finished.wait(timeout=2.0)
        pm2._chromeProcess = MagicMock()
        pm2._chromeProcess.poll.return_value = 1
        pm2._profileRuntime = bridge.BROWSER_RUNTIME_CHROME
        b._process_manager = pm2
        b._cdp_client = MagicMock()
        b._cdp_client.is_connected.return_value = False
        b._cdp_client.connect.side_effect = OSError("connection refused")
        with (
            patch.object(pm2, "start_and_get_websocket_url", return_value="ws://127.0.0.1:9222/page"),
            patch.object(bridge, "_browser_choices", return_value=()),
            self.assertRaises(OSError) as osErrCtx,
        ):
            b.ensure_connection()
        self.assertIn("ERROR: socket closed", getattr(osErrCtx.exception, "technicalDetail", ""))
        self.assertIn("connection refused", getattr(osErrCtx.exception, "technicalDetail", ""))
        self.assertIsNone(pm2._stderrCapture)

        pm3 = bridge.BrowserProcessManager(catalog=bridge.VoiceCatalog([]))
        pm3._profileRuntime = bridge.BROWSER_RUNTIME_CHROME
        b._process_manager = pm3
        b._cdp_client = MagicMock()
        b._cdp_client.is_connected.return_value = False
        b._cdp_client.connect.side_effect = RuntimeError("original connect failure")
        with (
            patch.object(pm3, "start_and_get_websocket_url", return_value="ws://127.0.0.1:9222/page"),
            patch.object(pm3, "startup_stderr_snippet", side_effect=RuntimeError("stderr probe failed")),
            patch.object(pm3, "terminate") as terminateMock,
            patch.object(bridge, "_browser_choices", return_value=()),
            self.assertRaisesRegex(RuntimeError, "original connect failure"),
        ):
            b.ensure_connection()
        b._cdp_client.close.assert_called_once()
        terminateMock.assert_called_once()

        # 8. _reset_browser_process_state closes raw io.IOBase proc.stderr if _stderrCapture was never attached.
        orphanStream = io.BytesIO(b"orphan")
        pm4 = bridge.BrowserProcessManager(catalog=bridge.VoiceCatalog([]))
        pm4._chromeProcess = SimpleNamespace(poll=lambda: 0, stderr=orphanStream)  # type: ignore[assignment]
        pm4._stderrCapture = None
        pm4._reset_browser_process_state()
        self.assertTrue(orphanStream.closed)

    def test_read_devtools_port_records_runtime_profile_and_stderr_on_temp_exit_zero(self) -> None:
        import io
        import tempfile
        from unittest.mock import MagicMock

        with tempfile.TemporaryDirectory() as temp_dir:
            pm = bridge.BrowserProcessManager(catalog=bridge.VoiceCatalog([]))
            pm._profileDir = Path(temp_dir)
            pm._profileRuntime = bridge.BROWSER_RUNTIME_EDGE
            pm._profileIsPersistent = False
            proc = MagicMock()
            proc.poll.return_value = 0
            proc.returncode = 0
            pm._chromeProcess = proc
            pm._stderrCapture = bridge._StartupStderrCapture(
                io.BytesIO(b"ERROR:headless_shell.cc(123)] DevTools blocked by policy\r\n")
            )

            with self.assertRaises(bridge.CdpError) as ctx:
                pm._read_devtools_port(Path(temp_dir) / "DevToolsActivePort")

            self.assertEqual(
                "The Chromium browser runtime closed before Google TTS For NVDA was ready.",
                str(ctx.exception),
            )
            detail = ctx.exception.technicalDetail or ""
            self.assertIn("Microsoft Edge", detail)
            self.assertIn("temporary profile", detail)
            self.assertIn(": 0", detail)
            self.assertIn("DevTools blocked by policy", detail)

    def test_start_browser_choice_preserves_persistent_failure_when_temporary_fails(self) -> None:
        import tempfile
        from unittest.mock import MagicMock, patch

        with tempfile.TemporaryDirectory() as temp_dir:
            pm = bridge.BrowserProcessManager(catalog=bridge.VoiceCatalog([]))
            pm._serverPort = 8080
            profileDir = Path(temp_dir)
            persistentErr = bridge._browser_profile_in_use_error(0, context="Google Chrome, persistent profile")
            tempErr = bridge._friendly_cdp_error(
                "The Chromium browser runtime closed before Google TTS For NVDA was ready.",
                "Chromium browser runtime (Google Chrome, temporary profile) exited before DevTools became available: 0",
            )
            mockProc = MagicMock()
            mockProc.pid = 4321
            mockProc.stderr = None

            def _get_profile(runtime: str, usePersistentProfile: bool = True, cancelEvent: Any = None) -> Path:
                pm._profileDir = profileDir
                pm._profileRuntime = runtime
                pm._profileIsPersistent = usePersistentProfile
                return profileDir

            with (
                patch.object(pm, "_get_browser_profile_dir", side_effect=_get_profile),
                patch.object(pm, "_page_url", return_value="http://127.0.0.1:8080/index.html"),
                patch.object(pm, "_read_devtools_port", side_effect=[persistentErr, tempErr]),
                patch.object(pm, "_stop_browser_process"),
                patch.object(bridge, "_hide_chrome_windows"),
                patch.object(bridge, "_elevate_chrome_priority"),
                patch("subprocess.Popen", return_value=mockProc),
                self.assertRaises(bridge.CdpError) as ctx,
            ):
                pm._start_browser_choice("chrome", "chrome.exe")

            detail = ctx.exception.technicalDetail or ""
            self.assertIn("temporary profile", detail)
            self.assertIn("after persistent profile failure:", detail)
            self.assertIn("persistent profile", detail)

    def test_start_and_get_websocket_url_logs_warning_and_aggregates_failures(self) -> None:
        from unittest.mock import patch

        pm = bridge.BrowserProcessManager(catalog=bridge.VoiceCatalog([]))
        chromeErr = bridge._friendly_cdp_error("closed", "Chrome temp failed (exit 0)")
        edgeErr = bridge._friendly_cdp_error("closed", "Edge temp failed (exit 0)")

        with (
            patch.object(pm, "_prepare_browser_start", return_value=False),
            patch.object(
                pm,
                "_browser_choices_or_raise",
                return_value=(("chrome", "chrome.exe"), ("edge", "msedge.exe")),
            ),
            patch.object(pm, "_start_browser_choice", side_effect=[chromeErr, edgeErr]),
            patch.object(pm, "_stop_browser_process"),
            self.assertLogs(bridge.log, level="WARNING") as logs,
            self.assertRaises(bridge.CdpError) as ctx,
        ):
            pm.start_and_get_websocket_url()

        joinedLogs = "\n".join(logs.output)
        self.assertIn("Google Chrome browser runtime failed; trying the next fallback runtime", joinedLogs)
        self.assertIn("Microsoft Edge browser runtime failed; no fallback runtime remains", joinedLogs)
        detail = ctx.exception.technicalDetail or ""
        self.assertIn("Edge temp failed (exit 0)", detail)
        self.assertIn("Previous runtime failures: Google Chrome: Chrome temp failed (exit 0)", detail)

    def test_start_browser_choice_terminates_exited_process_tree_on_devtools_failure(self) -> None:
        import io
        import tempfile
        from unittest.mock import MagicMock, patch

        with tempfile.TemporaryDirectory() as temp_dir:
            pm = bridge.BrowserProcessManager(catalog=bridge.VoiceCatalog([]))
            pm._serverPort = 8080
            profileDir = Path(temp_dir)

            def make_exited_proc() -> Any:
                proc = MagicMock()
                proc.pid = 4321
                proc.poll.return_value = 0
                proc.returncode = 0
                proc.stderr = io.BytesIO(b"ERROR: startup failed\r\n")
                return proc

            terminated: list[Any] = []

            def _get_profile(runtime: str, usePersistentProfile: bool = True, cancelEvent: Any = None) -> Path:
                pm._profileDir = profileDir
                pm._profileRuntime = runtime
                pm._profileIsPersistent = usePersistentProfile
                return profileDir

            with (
                patch.object(pm, "_get_browser_profile_dir", side_effect=_get_profile),
                patch.object(pm, "_page_url", return_value="http://127.0.0.1:8080/index.html"),
                patch.object(bridge, "_hide_chrome_windows"),
                patch.object(bridge, "_elevate_chrome_priority"),
                patch.object(
                    bridge,
                    "_terminate_subprocess",
                    side_effect=lambda proc: terminated.append(proc),
                ),
                patch("subprocess.Popen", side_effect=lambda *args, **kwargs: make_exited_proc()),
                self.assertRaises(bridge.CdpError),
            ):
                pm._start_browser_choice("chrome", "chrome.exe")

            self.assertEqual(2, len(terminated))
            self.assertTrue(all(proc is not None for proc in terminated))
            self.assertIsNone(pm._chromeProcess)
            self.assertIsNone(pm._stderrCapture)

    def test_browser_process_manager_terminate_server_shutdown_guards(self) -> None:
        catalog = SimpleNamespace(to_runtime_json=lambda: "{}")

        def new_manager() -> Any:
            mgr = bridge.BrowserProcessManager.__new__(bridge.BrowserProcessManager)
            mgr._lock = threading.RLock()
            mgr._catalog = catalog
            mgr._chromeProcess = None
            mgr._debugPort = None
            mgr._serverPort = 12345
            mgr._profileDir = None
            mgr._profileRuntime = None
            mgr._profileIsPersistent = True
            return mgr

        # 1. Dead or None _serverThread must NOT call server.shutdown(), only server_close().
        for threadObj in (None, SimpleNamespace(is_alive=lambda: False, join=lambda timeout=None: None)):
            mgr = new_manager()
            events: list[str] = []
            mgr._server = SimpleNamespace(
                shutdown=lambda _ev=events: _ev.append("shutdown"),
                server_close=lambda _ev=events: _ev.append("close"),
            )
            mgr._serverThread = threadObj
            mgr.terminate()
            self.assertEqual(["close"], events)
            self.assertIsNone(mgr._server)
            self.assertIsNone(mgr._serverThread)
            self.assertIsNone(mgr._serverPort)

        # 2. Terminating from inside the server thread itself must skip shutdown()/join().
        mgr = new_manager()
        selfEvents: list[str] = []
        currentThread = threading.current_thread()
        mgr._server = SimpleNamespace(
            shutdown=lambda: selfEvents.append("shutdown"),
            server_close=lambda: selfEvents.append("close"),
        )
        mgr._serverThread = currentThread
        mgr.terminate()
        self.assertEqual(["close"], selfEvents)

        # 3. Alive background _serverThread MUST call shutdown(), join(timeout=2), and server_close().
        mgr = new_manager()
        aliveEvents: list[Any] = []
        mgr._server = SimpleNamespace(
            shutdown=lambda: aliveEvents.append("shutdown"),
            server_close=lambda: aliveEvents.append("close"),
        )
        mgr._serverThread = SimpleNamespace(
            is_alive=lambda: True,
            join=lambda timeout=None: aliveEvents.append(("join", timeout)),
        )
        mgr.terminate()
        self.assertEqual(["shutdown", ("join", 2), "close"], aliveEvents)


class ConfiguredKeepBrowserRuntimeReadyTests(unittest.TestCase):
    """Verify configured_keep_browser_runtime_ready parses bools, strings, and None properly."""

    def test_string_and_bool_values(self) -> None:
        from unittest.mock import patch

        cases = (
            ("false", False),
            ("False", False),
            ("0", False),
            ("off", False),
            (False, False),
            ("true", True),
            ("1", True),
            ("yes", True),
            ("on", True),
            (True, True),
            (None, bridge.DEFAULT_KEEP_BROWSER_RUNTIME_READY),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                fake_conf = {bridge.CONFIG_SECTION: {bridge.CONFIG_KEEP_BROWSER_RUNTIME_READY: raw}}
                fake_config_mod = SimpleNamespace(conf=fake_conf)
                with patch.object(bridge, "config", fake_config_mod):
                    self.assertEqual(expected, bridge.configured_keep_browser_runtime_ready())


class EnsureConfigSpecTests(unittest.TestCase):
    """Verify ensure_config_spec registers defaults and evicts poisoned cache entries."""

    def test_ensure_config_spec_evicts_poisoned_cache_entries(self) -> None:
        from unittest.mock import patch

        fake_conf = FakeConf(
            cache={bridge.CONFIG_SECTION: SimpleNamespace(_spec={})},
            root_cache={bridge.CONFIG_SECTION: KeyError},
        )
        fake_config = SimpleNamespace(conf=fake_conf)
        with patch.object(bridge, "config", fake_config):
            bridge.ensure_config_spec()
            self.assertIn(bridge.CONFIG_SECTION, fake_conf.spec)
            self.assertIn(
                bridge.CONFIG_AUTO_UPDATE_CHECK,
                fake_conf.spec[bridge.CONFIG_SECTION],
            )
            self.assertNotIn(bridge.CONFIG_SECTION, fake_conf._cache)
            self.assertNotIn(bridge.CONFIG_SECTION, fake_conf.rootSection._cache)


class BridgeNvdaLoggingTests(unittest.TestCase):
    """Verify ChromeTtsBridge INFO startup logs and synthDriver-gated CDP traces."""

    def test_ensure_connection_logs_info_with_runtime_and_profile_mode(self) -> None:
        from tests.test_support import make_fake_bridge

        b = make_fake_bridge()
        b._process_manager._profileIsPersistent = True
        with self.assertLogs(bridge.log, level="INFO") as logs:
            b.ensure_connection()
        joined = "\n".join(logs.output)
        self.assertIn("INFO:", joined)
        self.assertIn("Google Chrome", joined)
        self.assertIn(bridge.ENGINE_VERSION, joined)
        self.assertIn("persistent profile", joined)


if __name__ == "__main__":
    unittest.main()
