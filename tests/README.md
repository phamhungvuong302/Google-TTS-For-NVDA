# Standalone Regression Tests

Google TTS For NVDA includes a standalone regression test suite across **20 test modules** (summarized below) that runs **without importing or installing NVDA**: isolated modules load via `test_support.py`, NVDA/wx integration hooks run under lightweight shims in `test_nvda_integration_shims.py`, and `bridgeHarness.js` executes under Node.js `node:vm` in `test_bridge_harness.py`.

---

## Running the Tests

Run the complete test suite from the repository root:

```powershell
python -m unittest discover -s tests -v
```

Run an individual module, test class, or test method:

```powershell
python -m unittest tests.test_speech_processing -v
python -m unittest tests.test_nvda_integration_shims.RealNvdaIntegrationShimTests -v
python -m unittest tests.test_bridge_concurrency.EnsureConnectionCancellationTests.test_cancelled_connection_does_not_terminate_process_manager -v
```

---

## Test Suite Index

| Test Module | Tests | Classes | Coverage Summary |
| :--- | :---: | :---: | :--- |
| `test_audio_math.py` | 11 | 1 | `rate_to_chrome`, `pitch_to_chrome`, `uses_protected_engine_rate` (SeaNet & `lemonbalm`, `PROTECTED_ENGINE_RATE`), `build_speech_options` soft-knee boost & rate/pitch decoupling, non-finite/bool coercion, break/tail rate factors, and gain makeup. |
| `test_bridge_concurrency.py` | 34 | 6 | `_connectionLock` fallback scope, `terminate()` connect-abort & queued-waiter epoch fencing, `update_catalog` in-flight recycle, locked engine capture, disconnected `stop_runtime` skip, `_runtimeBusyLock` (including `preload_voice`), `CdpClient.connect()` lock-free/error/race handling, `CdpClient.request()` stopped-dispatcher & timeout cancel callback, `CdpDispatcher` non-dict frame & disconnect wakeup, cancellation cleanup (including replaced processes), sample-rate validation, idle reconnect retry (excluding failed `ensure_connection`), and recycle epoch tracking. |
| `test_bridge_harness.py` | 22 | 1 | Node.js `vm` runtime tests for `web/bridgeHarness.js`: engine discovery, missing CDP binding rejection, `utteranceId` filtering, control-sequence cancellation & `finally` cleanup, synchronous & trailing error guards against worklet empty/end/audio, `MessageChannel` zero-delay scheduling, WASM `_GoogleTtsGetTimepoints*` extraction under WSOLA (`artificialRate`, Unicode codepoint offsets, `emitMarks`, JS `"word"` fallback), WSOLA tempo/pitch decoupling, Nyquist anti-aliasing, on-demand install coalescing, and audio drain/errors. |
| `test_bridge_helpers.py` | 76 | 25 | Runtime discovery/fallback order, Edge WebView2 checks, hidden startup & `--no-proxy-server` & priority elevation, page target URL matching, profile-lock & persistent/temporary switch & cancel handling, `_StartupStderrCapture` bounded stderr capture/UTF-8 & ANSI sanitization/pipe cancel & cleanup, `_safe_join` (including NUL bytes), bridge HTTP headers & Windows `allow_reuse_address`, transient/recycle classification, memory counters, CDP error formatting, and `ensure_config_spec`. |
| `test_build_i18n.py` | 40 | 2 | End-to-end POT extraction, PO creation/merge/rollback, MO compilation, manifest sync, interactive menu navigation/exit, and Markdown/HTML documentation build pipelines. |
| `test_build_i18n_helpers.py` | 232 | 26 | Strict PO parsing/escaping/syntax fallback, header merge canonicalization, fuzzy & obsolete entries, format/HTML tag/brace & math checks, `languageSort.json` validation, Markdown↔HTML conversion (tables, definition lists, entities), RTL rendering, and CLI/interactive recovery. |
| `test_dependency_isolation.py` | 11 | 1 | `websocketClientRepo` isolation, `proxy_info` `http_no_proxy` bypass, package-relative internal imports, pure-module zero-NVDA-import guard, `globalPlugins` `speech`/`sayAll` guards, and CLD2/browser/WASM asset anchoring. |
| `test_generate_voices_json_helpers.py` | 29 | 8 | Version sorting, bundled engine discovery, `voices.json` selection, catalog/engine version alignment, textproto comment stripping, HTTPS redirect guards, `dependentVoiceId` resolution, and CLI argument parsing. |
| `test_language_profiles.py` | 67 | 4 | BCP 47 & NVDA locale mapping, shared display names & `languageSort.json` sort keys, dialect redirects, aliases, normalization & NFC composition (preserving `U+037E`/`U+0387`), CLDR currencies/units, numbers, single-char & spelling routing, clause boundaries, script-range integration, URLs/domains, Latin exemplars, Han disambiguation, and emoji runs. |
| `test_make_update_manifest.py` | 10 | 4 | `manifest.ini` single-line & escaped triple-quoted parsing, version sort keys, highest-version/`updateBuild` package discovery, localized `releaseNotesByLocale` extraction, validation guards (ID, URL, size), and atomic manifest output via `main()`. |
| `test_nvda_integration_shims.py` | 35 | 1 | Execution tests under NVDA/wx shims for real `synthDrivers` and `globalPlugins` hooks: voice `.dic` loading/caching, auto-language speech/SayAll/spelling/character hooks, `setSynth` & Settings/Voice Manager UI guards, NVDA logging level/category sync, config compat, fatal fallback, pause modes (`BreakCommand(0)`, hidden-boundary leading-silence trimming) & multi-sentence batching, `SayAll` `IndexCommand` progression on `segmentEnd`, WASM sample-offset splitting + `WavePlayer.feed(..., onDone=...)` index sync + checkpoint cache, slow-consumer drain, `WavePlayer` feed serialization, and updater popup flows. |
| `test_performance.py` | 19 | 6 | `SHORT_CACHE_MAX_CHARS` / `SHORT_CACHE_MAX_HIDDEN_SEGMENTS` multi-segment batch limits, `PcmLeadBuffer` (`LIVE_MULTI_SEGMENT_LEAD_MS`), `PAUSE_MODE_*` & rate-scaled pause timings, adaptive JS packet sizing, and multilingual segmentation / multi-second 24 kHz PCM shortener throughput benchmarks. |
| `test_segmentation_fuzz.py` | 11 | 2 | Property-based fuzzing across synthetic Unicode corpora (CJK, Thai, Arabic, Devanagari, emoji sequences, URLs, decimals, combining marks) verifying full coverage, length bounds, and indexed segment invariants. |
| `test_speech_processing.py` | 66 | 10 | `PcmSilenceShortener` (`effective_chrome_rate`, `keep_silence_ms_for_rate`, `min_pause_trigger_ms_for_rate`, gain-scaled `pause_silence_noise_floor`, EOF glitch suppression, leading-silence & hidden-boundary seam trimming, exact fallback, hold caps), `PcmLeadBuffer`, `interpolate_byte_offset`, `TextSegmenter` (`segmentation_corpus.json`, `spoken_bridge_segments`, `needs_index_boundary_space`, `ends_with_pause_punctuation`, grapheme-extend/ZWJ/Kana-mark cut guards, no-space script & UCD punctuation boundaries), Unicode sentence terminators, abbreviation/URL/domain boundaries, and short/segment audio cache keys (`trimLeadingSilence`). |
| `test_standby_concurrency.py` | 23 | 6 | Standby generation counter, cancel events, `claim_bridge`, `release_synth_bridge`, `terminate`, unhealthy-bridge replacement & cancelled-connect cleanup, and lock-free catalog refresh & watcher shutdown. |
| `test_support.py` | — | — | Shared module loaders (`load_module_from_path`, `load_driver_module`, `load_fresh_driver_module`), fakes (`FakeCdpClient`, `FakeEngine`, `FakeProcessManager`, `FakeHttpResponse`, `FakeConf`), builders (`make_fake_bridge`, `make_test_voice_package`, `write_test_engine`), i18n/CLI fixtures (`temporary_doc_tree`, `temporary_po_file`, `temporary_manifest_file`, `interactive_options_for`, `patched_i18n_cli`, `captured_prints`), and PCM helpers (`pcm_bytes`, `pcm_samples`). |
| `test_synth_driver_helpers.py` | 18 | 4 | Pure rate-factor helpers (`interpolate_rate_factor`, `break_rate_factor`, `end_of_utterance_rate_factor`) and `_LANGUAGE_WORD_RE` word matching. |
| `test_unicode_data.py` | 42 | 11 | UCD/CLDR parsing, supplemental metadata, region-specific `likelySubtags` & parent-locale resolution, range merging, script aliases, no-space & grapheme-extend ranges, UCD General Category lookups, combining/currency/emoji/unit/Latin-exemplar extraction (including `{...}` escapes), normalization table generation, and `voices.json` engine version gating. |
| `test_updater_security.py` | 81 | 15 | SHA-256 verification, HTTPS-only & redirect-downgrade blocking, path traversal/reparse-point/reserved-device-name guards, non-positive & max size bounds, manifest & locale-fallback release-notes parsing, and version/build comparison. |
| `test_voice_package_lifecycle.py` | 29 | 7 | Voice package download & HTTPS redirect guards, SHA-256 verification cache, copy/remove lifecycle, script-subtag & schema validation in `VoiceCatalog`, and bundled `lemonbalm` support/dependencies. |
| `test_watcher.py` | 19 | 5 | `DirectoryChangeWatcher` start/stop/rapid-restart lifecycle, callbacks, handle cleanup on `FindNextChangeNotification` failure, error recovery, and live filesystem integration. |
| **Total** | **875** | **145** | **20 test modules** |

---

## NVDA Static API Contract Checks (`check_nvda_api_contracts.py`)

Verify static AST contracts against local NVDA source checkouts (auto-discovers sibling `../NVDA source code` and `../nvda` trees when omitted):

```powershell
python tests\check_nvda_api_contracts.py
python tests\check_nvda_api_contracts.py "C:\path\to\NVDA source code"
```

The checker inspects local NVDA source trees (e.g., `nvda`, `2024.4.2`, `2025.3.3`, `2026.3.1`) without importing NVDA across **11 categories**:
1. **Synth driver**: `SynthDriver` settings/methods, `VoiceInfo`, `LanguageInfo`, `synthIndexReached`, `synthDoneSpeaking`, `getSynth`, `setSynth`, `findAndSetNextSynth`, `isDebugForSynthDriver`, `SynthSettingsRing.updateSupportedSettings`, `DriverSetting` / `AutoSettings.isSupported` / `StringParameterInfo`, and `speech.commands`.
2. **Audio output**: concrete `nvwave` player classes (`WavePlayer` / `WinmmWavePlayer` / `WasapiWavePlayer`) constructor & playback methods, `isInError` vs legacy `audioDeviceError`, and `outputDevice` config section (`audio` vs `speech`).
3. **Global plugin**: `GlobalPlugin`, `core.postNvdaStartup`, `gui.mainFrame` / `blockAction` / `messageBox` / `VoiceDictionaryDialog`.
4. **Speech hooks and language profiles**: `speech.extensions.filter_speechSequence`, `speech.speech` (`speak`, `processText`, `getSpellingSpeech`, `getSingleCharDescription`), `speech.sayAll` (`SayAllHandler`, `initialize`), `speech.shortcutKeys.shouldUseSpellingFunctionality`, `speechDictHandler.loadVoiceDict`, voice dictionary storage (`_speechDictDefinitions` vs `dictionaries`), and language switching (`speech.languageHandling` + `speech.manager.shouldSwitchVoice` + `SynthDriver.languageIsSupported` vs `speech.speak` config flags).
5. **Settings category**: `SettingsPanel`, `MultiCategorySettingsDialog`, `AutoSettingsMixin` (`_getSettingMaker`, `_updateValueForControl`, `onDiscard`, `refreshGui`), `VoiceSettingsPanel.makeSettings`, `NVDASettingsDialog.categoryClasses`, `guiHelper`, `SelectOnFocusSpinCtrl`, and `EnhancedInputSlider`.
6. **Voice Manager**: `DPIScaledDialog`, `languageHandler`, `synthDriverHandler.getSynth`, `ui.message`, and `SynthesizerSelectionDialog` / `SynthesizerDialog`.
7. **Updater**: `gui.addonGui` (`installAddon`, `promptUserForRestart`), `ui.message`, and `addonHandler.initTranslation`.
8. **Browser runtime and standby**: `addonHandler.initTranslation`, `config.conf`, `globalVars` (`appArgs`, `appDir`), and `logHandler` (`log`, `Logger.debugWarning`, `Logger.io`, `Logger.exception`).
9. **Shared NVDA state**: same core globals as category 8 plus `languageHandler.getLanguage`.
10. **High-risk signatures**: `synthDriverHandler.setSynth` signature compatibility, `AutoSettingsMixin.refreshGui(self)`, `gui.message` (`MessageDialog`, `DialogType`, `DefaultButton`, `ReturnCode`, `isModalMessageBoxActive`, `wxCallOnMain`) vs legacy `messageBox`, and `config.isAppX`.
11. **Add-on compatibility guards**: AST verification of **56** extensible `*args, **kwargs` entry points across `synthDrivers/__init__.py`, `globalPlugins/__init__.py`, and `settings.py`, `HandlerRegistrar` parameter checks (`_filter_auto_language_speech_sequence`, `_on_post_startup`), `language_utils` (`call_compat`, `show_message_box` / `_main_frame_popup_guard`, `ensure_nvda_logger_methods`, `is_debug_for_synth_driver`), `get_nvda_logger()` usage, `log.exception()` single-arg guard, secure-mode / `isAppX` / spelling-tail guards, and `SynthDriver` fallback guards (`_current_output_device`, `_audio_device_error`, `_feed_silence`, `_language_token_signal`).

---

## Interactive Manual Release Checklist

Before tagging a release, complete the live NVDA + Chromium verification procedures in [`NVDA_CHROMIUM_MANUAL_CHECKLIST.md`](NVDA_CHROMIUM_MANUAL_CHECKLIST.md).
