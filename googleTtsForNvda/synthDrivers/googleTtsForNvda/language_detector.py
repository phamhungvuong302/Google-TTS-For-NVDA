from __future__ import annotations

import ctypes
import functools
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .language_utils import canonical_language_root as _canonical_language_root
from .language_utils import get_nvda_logger
from .language_utils import language_root as _language_root
from .language_utils import normalize_language as _normalize_language

log = get_nvda_logger()

# Shared sentinel, command class, and attribute name used by both the synth
# driver and the global plugin to attach / retrieve a Google TTS language tag
# across NVDA's speech pipeline.  Defined once here so both modules use the
# same object identity for ``is`` / ``isinstance`` checks.
GOOGLE_TTS_LANG_CHANGE_ATTR: str = "googleTtsForNvdaLanguage"
MISSING_GOOGLE_TTS_LANGUAGE: object = object()

try:
    from speech.commands import SpeechCommand as _SpeechCommandBase
except Exception:  # pragma: no cover - NVDA is not available in local unit checks.
    _SpeechCommandBase = object  # type: ignore[misc,assignment]


class GoogleTtsProfileLangCommand(_SpeechCommandBase):  # type: ignore[valid-type,misc]
    def __init__(self, language: str | None, *, spelling: bool = False) -> None:
        self.language = language
        self.spelling = bool(spelling)

    def __repr__(self) -> str:
        return f"GoogleTtsProfileLangCommand({self.language!r}, spelling={self.spelling!r})"


_DLL_DIR = Path(__file__).with_name("cld2")
_DLL_NAMES = ("cld2_x64.dll", "cld2.dll") if ctypes.sizeof(ctypes.c_void_p) == 8 else ("cld2_x86.dll",)
_MIN_RELIABLE_PERCENT = 30
_MAX_CLD2_INPUT_CHARS = 65536
_LANGUAGE_ALIASES = {
    "ar": {"ar-xa"},
    "ar-xa": {"ar"},
    "cmn": {"zh"},
    "cmn-cn": {"zh-cn", "zh-hans"},
    "cmn-tw": {"zh-tw", "zh-hant"},
    "fil": {"tl", "fil-ph"},
    "fil-ph": {"fil", "tl"},
    "gom": {"kok", "kok-in"},
    "he": {"iw", "he-il"},
    "he-il": {"he", "iw"},
    "id": {"in", "id-id"},
    "id-id": {"id", "in"},
    "in": {"id", "id-id"},
    "iw": {"he", "he-il"},
    "jv": {"jw", "jv-id"},
    "jv-id": {"jv", "jw"},
    "jw": {"jv", "jv-id"},
    "kok": {"gom", "kok-in"},
    "kok-in": {"kok", "gom"},
    "mni": {"mni-in", "mni-mtei"},
    "mni-in": {"mni", "mni-mtei"},
    "mni-mtei": {"mni", "mni-in"},
    "nb": {"no", "nn", "nb-no"},
    "nb-no": {"nb", "no", "nn"},
    "nn": {"nb", "nb-no", "no"},
    "no": {"nb", "nb-no", "nn"},
    "or": {"ory", "or-in"},
    "or-in": {"or", "ory"},
    "ory": {"or", "or-in"},
    "pa": {"pan", "pa-in"},
    "pa-in": {"pa", "pan"},
    "pan": {"pa", "pa-in"},
    "sat": {"sat-in", "sat-olck"},
    "sat-in": {"sat", "sat-olck"},
    "sat-olck": {"sat", "sat-in"},
    "sr": {"sr-rs", "sr-cyrl", "sr-latn"},
    "sr-rs": {"sr", "sr-cyrl", "sr-latn"},
    "sr-cyrl": {"sr", "sr-rs"},
    "sr-latn": {"sr", "sr-rs"},
    "tl": {"fil", "fil-ph"},
    "yue": {"yue-hk", "zh-hk"},
    "yue-hk": {"yue", "zh-hk"},
    "zh": {"cmn", "cmn-cn", "cmn-tw"},
    "zh-cn": {"cmn-cn", "zh-hans"},
    "zh-hans": {"cmn-cn", "zh-cn"},
    "zh-hant": {"cmn-tw", "yue-hk", "zh-hk", "zh-tw"},
    "zh-hk": {"yue-hk", "zh-hant"},
    "zh-tw": {"cmn-tw", "zh-hant"},
}
_CHINESE_LANGUAGE_ROOTS = {"cmn", "zh"}
# Specific dialect → best supported dialect redirects.
# Only dialects that are commonly requested but not always available need entries.
_LANGUAGE_REDIRECTS: dict[str, str] = {
    "fr-ca": "fr-fr",
    "fr-be": "fr-fr",
    "fr-ch": "fr-fr",
    "fr-lu": "fr-fr",
    "fr-mc": "fr-fr",
    "pt-pt": "pt-br",
    "pt-ao": "pt-br",
    "pt-mz": "pt-br",
    "es-es": "es-mx",
    "es-ar": "es-mx",
    "es-co": "es-mx",
    "es-cl": "es-mx",
    "es-ve": "es-mx",
    "es-pe": "es-mx",
    "es-ec": "es-mx",
    "de-at": "de-de",
    "de-ch": "de-de",
    "en-ca": "en-us",
    "en-gb": "en-us",
    "en-au": "en-us",
    "en-in": "en-us",
    "en-za": "en-us",
    "en-nz": "en-us",
    "it-ch": "it-it",
}


def _subtag_alias_candidates(languageKey: str) -> list[str]:
    """Return deterministic alias lookup keys including 2-part subtag combinations for 3-part BCP-47 tags."""
    keys = [languageKey]
    parts = languageKey.split("-")
    if len(parts) >= 3:
        for subKey in (f"{parts[0]}-{parts[-1]}", f"{parts[0]}-{parts[1]}"):
            if subKey not in keys:
                keys.append(subKey)
    aliases: list[str] = []
    seen: set[str] = set()
    for lookupKey in keys:
        for alias in sorted(_language_aliases(lookupKey)):
            if alias not in seen:
                seen.add(alias)
                aliases.append(alias)
    return aliases


def redirect_language(language: str | None, available_languages: set[str]) -> str | None:
    """Redirect an unsupported locale to the best available alternative.

    When a specific locale is not available, return the most appropriate
    supported locale.
    Returns None if no redirect is needed (language is already available)
    or no suitable redirect exists.
    """
    if not language:
        return None
    key = _normalize_language(language)
    availableByKey = {
        normalized: available
        for available in sorted(available_languages)
        if available and (normalized := _normalize_language(available))
    }
    if key in availableByKey:
        return None
    # Check explicit redirects first.
    redirect = _LANGUAGE_REDIRECTS.get(key)
    if redirect and redirect in availableByKey:
        return availableByKey[redirect]
    # Try alias matches deterministically (e.g., zh-CN -> cmn-CN, zh-Hant-TW -> cmn-TW, yue -> zh-HK).
    for alias in _subtag_alias_candidates(key):
        if alias in availableByKey:
            return availableByKey[alias]
    # Try root and alias-root matches deterministically (e.g., fr-CA -> fr-FR, tl-PH -> fil-PH, zh-SG -> cmn-CN).
    root = key.split("-", 1)[0]
    rootMatches = list(
        dict.fromkeys(
            [
                root,
                _canonical_language_root(root),
                *(alias.split("-", 1)[0] for alias in sorted(_language_aliases(root))),
            ]
        )
    )
    for targetRoot in rootMatches:
        for availableKey in sorted(availableByKey):
            if availableKey.split("-", 1)[0] == targetRoot:
                return availableByKey[availableKey]
    return None


@dataclass(frozen=True)
class DetectionResult:
    language: str
    percent: int
    textBytes: int
    isReliable: bool


class _Cld2Detector:
    _MAX_LOAD_ATTEMPTS = 3

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._library: ctypes.CDLL | None = None
        self._loadAttempts = 0
        self._loadErrorLogged = False

    def detect(self, text: str) -> DetectionResult | None:
        if not text:
            return None
        library = self._load_library()
        if library is None:
            return None
        if len(text) > _MAX_CLD2_INPUT_CHARS:
            text = text[:_MAX_CLD2_INPUT_CHARS]
        if not text.isascii():
            text = unicodedata.normalize("NFC", text)
        encodedText = text.encode("utf-8", "replace")
        if not encodedText:
            return None
        languageCode = ctypes.create_string_buffer(16)
        percent = ctypes.c_int()
        textBytes = ctypes.c_int()
        isReliable = ctypes.c_int()
        try:
            result = library.cld2_detect_language(
                encodedText,
                len(encodedText),
                languageCode,
                len(languageCode),
                ctypes.byref(percent),
                ctypes.byref(textBytes),
                ctypes.byref(isReliable),
            )
        except Exception:
            log.debugWarning("Could not detect language with CLD2.", exc_info=True)
            return None
        if not result:
            return None
        language = languageCode.value.decode("ascii", "replace").strip()
        if not language:
            return None
        return DetectionResult(
            language=language,
            percent=max(0, min(100, int(percent.value))),
            textBytes=max(0, int(textBytes.value)),
            isReliable=bool(isReliable.value),
        )

    def _load_library(self) -> ctypes.CDLL | None:
        with self._lock:
            if self._library is not None:
                return self._library
            if self._loadAttempts >= self._MAX_LOAD_ATTEMPTS:
                return None
            self._loadAttempts += 1
            try:
                for dllName in _DLL_NAMES:
                    dllPath = _DLL_DIR / dllName
                    if not dllPath.is_file():
                        continue
                    try:
                        library = ctypes.CDLL(str(dllPath))
                        library.cld2_detect_language.argtypes = [
                            ctypes.c_char_p,
                            ctypes.c_int,
                            ctypes.c_char_p,
                            ctypes.c_int,
                            ctypes.POINTER(ctypes.c_int),
                            ctypes.POINTER(ctypes.c_int),
                            ctypes.POINTER(ctypes.c_int),
                        ]
                        library.cld2_detect_language.restype = ctypes.c_int
                        self._library = library
                        return self._library
                    except Exception:
                        if not self._loadErrorLogged:
                            log.debugWarning("Could not load CLD2 language detector from %s.", dllPath, exc_info=True)
                return None
            finally:
                if self._library is None:
                    self._loadErrorLogged = True


_detector = _Cld2Detector()


def _preferred_candidate(preferredLanguage: str | None, candidateLanguages: list[str]) -> str | None:
    if not preferredLanguage:
        return None
    if candidateLanguages:
        return _candidate_for_language(preferredLanguage, candidateLanguages)
    return preferredLanguage


def resolve_single_grapheme_candidate(
    stripped: str,
    candidateLanguages: list[str],
    preferredLanguage: str | None = None,
) -> str | None:
    """Return a non-preferred candidate language when a single grapheme matches its script or non-ASCII Latin exemplar."""
    if not stripped or not candidateLanguages:
        return None
    from .language_profiles import single_grapheme_candidate_root

    candidateRoots = list(dict.fromkeys(_language_root(candidate) for candidate in candidateLanguages))
    prefRoot = _language_root(preferredLanguage) if preferredLanguage else ""
    matchedRoot = single_grapheme_candidate_root(stripped, candidateRoots, prefRoot or None)
    if matchedRoot:
        matched = _candidate_for_language(matchedRoot, candidateLanguages)
        if matched:
            return matched
    return None


def _latin_fallback_candidate(
    normalizedText: str,
    candidateLanguages: list[str],
    candidateRoots: list[str],
) -> str | None:
    """Resolve a Latin candidate when the text actually contains Latin letters."""
    from .language_profiles import get_character_script, is_letter_codepoint, latin_exemplar_signal
    from .unicode_data import SUPPORTED_LANGUAGE_SCRIPTS

    hasLatinChars = any(get_character_script(ord(c)) == "Latin" for c in normalizedText if is_letter_codepoint(ord(c)))
    if not hasLatinChars:
        return None
    exemplarWinner = latin_exemplar_signal(normalizedText, candidateRoots)
    if exemplarWinner:
        return _candidate_for_language(exemplarWinner, candidateLanguages)
    if "en" in candidateRoots and any(c.isascii() and is_letter_codepoint(ord(c)) for c in normalizedText):
        return _candidate_for_language("en", candidateLanguages)
    latinCandidates = [
        c for c in candidateLanguages if "Latin" in SUPPORTED_LANGUAGE_SCRIPTS.get(_canonical_language_root(c), ())
    ]
    if len(latinCandidates) == 1:
        return latinCandidates[0]
    return None


def detect_language(
    text: str,
    candidateLanguages: list[str],
    preferredLanguage: str | None = None,
) -> str | None:
    """Detect language for text restricted by candidate language hints and preferred language.

    Applies Mathematical Alphanumeric normalization, candidate language prior hints,
    preferred language routing for single characters and spelling clauses, and Unicode script/diacritic analysis.
    """
    if not text:
        return None
    from .language_profiles import (
        has_language_words,
        is_single_grapheme_token,
        is_spelling_only_text,
        language_script_signal,
        normalize_mathematical_alphanumeric,
    )

    normalizedText = normalize_mathematical_alphanumeric(text)
    stripped = normalizedText.strip()
    if not stripped:
        return None

    candidateRoots = list(dict.fromkeys(_language_root(c) for c in candidateLanguages))

    # Single character reading and alphabet spelling clauses route to preferred language
    if preferredLanguage:
        if is_single_grapheme_token(stripped):
            matchedCandidate = resolve_single_grapheme_candidate(stripped, candidateLanguages, preferredLanguage)
            if matchedCandidate:
                return matchedCandidate
            return _preferred_candidate(preferredLanguage, candidateLanguages)
        if is_spelling_only_text(stripped):
            return _preferred_candidate(preferredLanguage, candidateLanguages)

    # Text without language words (e.g. pure numbers, units, currencies, times, symbols, or emojis)
    # has no intrinsic language signal: return preferredLanguage fallback if provided, or None.
    if not has_language_words(stripped):
        if preferredLanguage:
            return _preferred_candidate(preferredLanguage, candidateLanguages)
        return None

    # CLD2 statistical detection
    result = _detector.detect(normalizedText)
    if result is not None:
        candidate = _candidate_for_language(result.language, candidateLanguages)
        if candidate is not None and (result.isReliable or result.percent >= _MIN_RELIABLE_PERCENT):
            return candidate

    # Non-Latin script fallback via Unicode script ranges before Latin recovery so
    # mixed text like "API の使い方" or "OK Привет" routes to its non-Latin script.
    scriptSignal = language_script_signal(normalizedText, candidateRoots)
    if scriptSignal:
        return _candidate_for_language(scriptSignal, candidateLanguages)

    # Latin exemplar & plain Latin heuristics (guarded by actual Latin letter presence)
    latinFallback = _latin_fallback_candidate(normalizedText, candidateLanguages, candidateRoots)
    if latinFallback:
        return latinFallback

    if preferredLanguage:
        return _preferred_candidate(preferredLanguage, candidateLanguages)

    return None


def _candidate_for_language(language: str, candidateLanguages: list[str]) -> str | None:
    languageKey = _normalize_language(language)
    if not languageKey:
        return None
    for candidate in candidateLanguages:
        if _normalize_language(candidate) == languageKey:
            return candidate
    languageAliases = set(_subtag_alias_candidates(languageKey))
    for candidate in candidateLanguages:
        if _normalize_language(candidate) in languageAliases:
            return candidate
    languageRoot = _language_root(languageKey)
    for candidate in candidateLanguages:
        if _language_root(candidate) == languageRoot:
            return candidate
    canonicalRoot = _canonical_language_root(languageKey)
    for candidate in candidateLanguages:
        if _canonical_language_root(candidate) == canonicalRoot:
            return candidate
    if _language_family(languageKey) == "zh":
        for candidate in candidateLanguages:
            if _language_family(candidate) == "zh":
                return candidate
    return None


@functools.lru_cache(maxsize=128)
def _language_match_keys_cached(languageKey: str) -> frozenset[str]:
    if not languageKey:
        return frozenset()
    aliases = {languageKey}
    aliases.update(_LANGUAGE_ALIASES.get(languageKey, set()))
    root = languageKey.split("-", 1)[0]
    aliases.add(root)
    aliases.update(_LANGUAGE_ALIASES.get(root, set()))
    if root in _CHINESE_LANGUAGE_ROOTS:
        aliases.update(_CHINESE_LANGUAGE_ROOTS)
    return frozenset(aliases)


def language_match_keys(language: str | None) -> set[str]:
    languageKey = _normalize_language(language)
    if not languageKey:
        return set()
    return set(_language_match_keys_cached(languageKey))


def _language_aliases(languageKey: str) -> set[str]:
    return set(_LANGUAGE_ALIASES.get(languageKey, set()))


def _language_family(language: str | None) -> str:
    root = _language_root(language)
    if root in _CHINESE_LANGUAGE_ROOTS:
        return "zh"
    return root


def language_matches(left: str | None, right: str | None) -> bool:
    """Return True when two language tags refer to the same language.

    Uses aliases, root-language matching, and Chinese-family merging so that
    regional variants (fr-FR, fr-CA) and CLDR aliases (fil, tl) are recognised
    as the same language when appropriate.
    """
    leftKeys = language_match_keys(left)
    rightKeys = language_match_keys(right)
    return bool(leftKeys and rightKeys and leftKeys.intersection(rightKeys))
