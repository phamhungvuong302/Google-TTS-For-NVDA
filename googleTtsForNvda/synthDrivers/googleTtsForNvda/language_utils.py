"""Language and locale normalization helpers for Google TTS For NVDA."""

from __future__ import annotations

import contextlib
import functools
import inspect
import json
import logging
import os
import sys
import threading
import unicodedata
from pathlib import Path
from typing import Any

try:
    import wx as _wx
except Exception:  # pragma: no cover - NVDA/wx is not available in all unit test contexts.
    _wx = None  # type: ignore[assignment]

try:
    import addonHandler as _addonHandler

    _addonHandler.initTranslation()
    del _addonHandler
except Exception:  # pragma: no cover - NVDA is not available in all unit test contexts.

    def _(message: str) -> str:
        return message


LOCALE_DIR = Path(__file__).resolve().parents[2] / "locale"
_languageSortRulesByLocale: dict[str, dict[str, Any] | None] = {}

LANGUAGE_NAMES: dict[str, str] = {
    "ar-XA": _("Arabic"),
    "as-IN": _("Assamese"),
    "bg-BG": _("Bulgarian"),
    "bn-BD": _("Bengali (Bangladesh)"),
    "bn-IN": _("Bengali (India)"),
    "brx-IN": _("Bodo"),
    "bs-BA": _("Bosnian"),
    "ca-ES": _("Catalan"),
    "cmn-CN": _("Mandarin Chinese (China)"),
    "cmn-TW": _("Mandarin Chinese (Taiwan)"),
    "cs-CZ": _("Czech"),
    "cy-GB": _("Welsh"),
    "da-DK": _("Danish"),
    "de-DE": _("German"),
    "doi-IN": _("Dogri"),
    "el-GR": _("Greek"),
    "en-AU": _("English (Australia)"),
    "en-GB": _("English (UK)"),
    "en-IN": _("English (India)"),
    "en-NG": _("English (Nigeria)"),
    "en-US": _("English (US)"),
    "es-ES": _("Spanish (Spain)"),
    "es-US": _("Spanish (US)"),
    "et-EE": _("Estonian"),
    "fi-FI": _("Finnish"),
    "fil-PH": _("Filipino"),
    "fr-CA": _("French (Canada)"),
    "fr-FR": _("French (France)"),
    "gu-IN": _("Gujarati"),
    "he-IL": _("Hebrew"),
    "hi-IN": _("Hindi"),
    "hr-HR": _("Croatian"),
    "hu-HU": _("Hungarian"),
    "id-ID": _("Indonesian"),
    "is-IS": _("Icelandic"),
    "it-IT": _("Italian"),
    "ja-JP": _("Japanese"),
    "jv-ID": _("Javanese"),
    "km-KH": _("Khmer"),
    "kn-IN": _("Kannada"),
    "ko-KR": _("Korean"),
    "kok-IN": _("Konkani"),
    "ks-IN": _("Kashmiri"),
    "lt-LT": _("Lithuanian"),
    "lv-LV": _("Latvian"),
    "mai-IN": _("Maithili"),
    "ml-IN": _("Malayalam"),
    "mni-IN": _("Manipuri"),
    "mr-IN": _("Marathi"),
    "ms-MY": _("Malay"),
    "nb-NO": _("Norwegian Bokmål"),
    "ne-NP": _("Nepali"),
    "nl-BE": _("Flemish"),
    "nl-NL": _("Dutch"),
    "or-IN": _("Odia"),
    "pa-IN": _("Punjabi"),
    "pl-PL": _("Polish"),
    "pt-BR": _("Portuguese (Brazil)"),
    "pt-PT": _("Portuguese (Portugal)"),
    "ro-RO": _("Romanian"),
    "ru-RU": _("Russian"),
    "sa-IN": _("Sanskrit"),
    "sat-IN": _("Santali"),
    "sd-IN": _("Sindhi"),
    "si-LK": _("Sinhala"),
    "sk-SK": _("Slovak"),
    "sl-SI": _("Slovenian"),
    "sq-AL": _("Albanian"),
    "sr-RS": _("Serbian"),
    "su-ID": _("Sundanese"),
    "sv-SE": _("Swedish"),
    "sw-KE": _("Swahili"),
    "ta-IN": _("Tamil"),
    "te-IN": _("Telugu"),
    "th-TH": _("Thai"),
    "tr-TR": _("Turkish"),
    "uk-UA": _("Ukrainian"),
    "ur-IN": _("Urdu (India)"),
    "ur-PK": _("Urdu (Pakistan)"),
    "vi-VN": _("Vietnamese"),
    "yue-HK": _("Cantonese"),
}
_LANGUAGE_NAMES_LOWER: dict[str, str] = {k.lower(): v for k, v in LANGUAGE_NAMES.items()}

SPECIAL_NVDA_LOCALES: dict[str, str] = {
    "cmn-CN": "zh_CN",
    "cmn-TW": "zh_TW",
    "yue-HK": "zh_HK",
    "ar-XA": "ar",
    "fil": "tl",
    "fil-PH": "tl",
}
_SPECIAL_NVDA_LOCALES_LOWER: dict[str, str] = {k.lower(): v for k, v in SPECIAL_NVDA_LOCALES.items()}

CANONICAL_ROOT_ALIASES: dict[str, str] = {
    "gom": "kok",
    "in": "id",
    "iw": "he",
    "jw": "jv",
    "nn": "nb",
    "no": "nb",
    "ory": "or",
    "pan": "pa",
    "tl": "fil",
    "zh": "cmn",
}


def call_compat(target: Any, *args: Any, **kwargs: Any) -> Any:
    """Invoke an NVDA callable while filtering parameters unsupported by its signature."""
    if not callable(target):
        raise TypeError(f"{target!r} is not callable")
    try:
        signature = inspect.signature(target, follow_wrapped=False)
    except (TypeError, ValueError):
        try:
            return target(*args, **kwargs)
        except TypeError as exc:
            # Retry without kwargs only when Python rejected an unexpected keyword
            # argument at call dispatch, never when target raised TypeError internally.
            if (
                kwargs
                and exc.__traceback__ is not None
                and exc.__traceback__.tb_next is None
                and "keyword" in str(exc).lower()
            ):
                return target(*args)
            raise
    parameters = signature.parameters
    acceptsVarKeyword = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values())
    acceptsVarPositional = any(param.kind == inspect.Parameter.VAR_POSITIONAL for param in parameters.values())
    positionalParams = [
        param
        for param in parameters.values()
        if param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    keywordNames = {
        param.name
        for param in parameters.values()
        if param.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    filteredKwargs = dict(kwargs) if acceptsVarKeyword else {k: v for k, v in kwargs.items() if k in keywordNames}
    if not acceptsVarPositional:
        maxPositional = len(positionalParams)
        if len(args) > maxPositional:
            for extraIndex in range(maxPositional, len(args)):
                extraValue = args[extraIndex]
                for param in parameters.values():
                    if (
                        param.kind == inspect.Parameter.KEYWORD_ONLY
                        and param.name not in filteredKwargs
                        and param.default is not inspect.Parameter.empty
                        and type(param.default) is type(extraValue)
                    ):
                        filteredKwargs[param.name] = extraValue
                        break
            args = args[:maxPositional]
    boundPositionalNames = {
        param.name for param in positionalParams[: len(args)] if param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
    }
    for boundName in boundPositionalNames:
        filteredKwargs.pop(boundName, None)
    return target(*args, **filteredKwargs)


def normalize_language(language: str | None) -> str:
    """Normalize a language tag to lower-case with hyphens (e.g. 'vi_vn' -> 'vi-vn')."""
    return str(language or "").strip().replace("_", "-").lower()


def language_root(language: str | None) -> str:
    """Return the lower-case primary language subtag (e.g. 'vi-VN' -> 'vi')."""
    return normalize_language(language).split("-", 1)[0]


def canonical_language_root(language: str | None) -> str:
    """Return the canonical lower-case primary language subtag resolving legacy/CLDR aliases."""
    root = language_root(language)
    return CANONICAL_ROOT_ALIASES.get(root, root)


def normalize_language_code(language: str | None) -> str:
    """Normalize a language tag preserving case with hyphens (e.g. 'vi_VN' -> 'vi-VN')."""
    return str(language or "").strip().replace("_", "-")


def parse_language_codes(value: str | None) -> list[str]:
    """Parse a comma-separated string into non-empty, case-insensitively deduplicated language codes."""
    codes: list[str] = []
    seen: set[str] = set()
    for rawCode in str(value or "").split(","):
        code = normalize_language_code(rawCode)
        key = code.lower()
        if not code or key in seen:
            continue
        codes.append(code)
        seen.add(key)
    return codes


def _fallback_normalize_locale(languageText: str) -> str:
    parts = [part for part in languageText.replace("-", "_").split("_") if part]
    if not parts:
        return ""
    normalizedParts = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 4 and part.isalpha():
            normalizedParts.append(part.title())
        elif len(part) == 2 and part.isalpha():
            normalizedParts.append(part.upper())
        else:
            normalizedParts.append(part)
    return "_".join(normalizedParts)


def get_nvda_locale_for_language(lang_code: str | None) -> str:
    """Convert a language tag into a valid NVDA locale code (e.g. 'cmn-CN' -> 'zh_CN')."""
    if not lang_code:
        return ""
    languageText = str(lang_code).strip()
    if languageText in SPECIAL_NVDA_LOCALES:
        return SPECIAL_NVDA_LOCALES[languageText]
    lowerLanguage = normalize_language(languageText)
    if lowerLanguage in _SPECIAL_NVDA_LOCALES_LOWER:
        return _SPECIAL_NVDA_LOCALES_LOWER[lowerLanguage]
    if lowerLanguage.startswith(("cmn-hk", "cmn-mo")):
        return "zh_HK"
    if lowerLanguage.startswith(("cmn-tw", "cmn-hant")):
        return "zh_TW"
    if lowerLanguage.startswith("cmn"):
        return "zh_CN"
    if lowerLanguage.startswith("yue"):
        return "zh_HK"
    try:
        import languageHandler

        normalized = languageHandler.normalizeLanguage(languageText)
    except Exception:
        normalized = ""
    result = str(normalized or "").strip()
    if not result:
        result = _fallback_normalize_locale(languageText)
    return result


@functools.lru_cache(maxsize=64)
def _nvda_locale_exists_in_dir(appDir: str, locale: str) -> bool:
    return bool(appDir) and os.path.isdir(os.path.join(appDir, "locale", locale))


def nvda_locale_exists(locale: str) -> bool:
    """Return True if the given locale exists in NVDA's installation directory."""
    if not locale:
        return False
    try:
        import globalVars

        appDir = str(getattr(globalVars, "appDir", "") or "")
    except Exception:
        return False
    return _nvda_locale_exists_in_dir(appDir, locale)


def resolve_nvda_locale(language: str | None) -> str:
    """Resolve the effective NVDA locale for speech messages or fall back to 'en'."""
    nvdaLocale = get_nvda_locale_for_language(language)
    if not nvdaLocale:
        return "en"
    if nvda_locale_exists(nvdaLocale):
        return nvdaLocale
    rootLocale = nvdaLocale.split("_", 1)[0]
    if rootLocale != nvdaLocale and nvda_locale_exists(rootLocale):
        return rootLocale
    return "en"


def _language_display_candidates(lang_code: str) -> tuple[str, ...]:
    nvdaLocale = get_nvda_locale_for_language(lang_code)
    candidates: list[str] = []
    for candidate in (nvdaLocale, nvdaLocale.split("_", 1)[0] if "_" in nvdaLocale else "", lang_code):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


def get_language_display_name(
    lang_code: str,
    custom_language_names: dict[str, str] | None = None,
) -> str:
    """Return a human-readable language description."""
    normalized = normalize_language(lang_code)
    if custom_language_names is None:
        direct = LANGUAGE_NAMES.get(lang_code)
        if direct:
            return direct
        matched = _LANGUAGE_NAMES_LOWER.get(normalized)
        if matched:
            return matched
    elif custom_language_names:
        for code, name in custom_language_names.items():
            if normalize_language(code) == normalized:
                return name
    for candidate in _language_display_candidates(lang_code):
        try:
            import languageHandler

            description = languageHandler.getLanguageDescription(candidate)
        except Exception:
            description = None
        if description:
            return description
    return lang_code


def _current_ui_language() -> str:
    try:
        import languageHandler

        return str(languageHandler.getLanguage() or "").strip().replace("-", "_")
    except Exception:
        return ""


def _locale_candidates(language: str) -> list[str]:
    cleaned = str(language or "").strip().replace("-", "_")
    if not cleaned:
        return []
    candidates: list[str] = []

    def _add(candidate: str) -> None:
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    _add(cleaned)
    parts = cleaned.split("_", 1)
    if len(parts) == 2 and parts[0] and parts[1]:
        _add(f"{parts[0].lower()}_{parts[1].upper()}")
    _add(cleaned.lower())
    _add(parts[0].lower())
    return candidates


def _language_sort_rules_for_current_ui() -> dict[str, Any] | None:
    for localeName in _locale_candidates(_current_ui_language()):
        rules = _load_language_sort_rules(localeName)
        if rules is not None:
            return rules
    return None


def _load_language_sort_rules(localeName: str) -> dict[str, Any] | None:
    if localeName in _languageSortRulesByLocale:
        return _languageSortRulesByLocale[localeName]
    path = LOCALE_DIR / localeName / "languageSort.json"
    try:
        rawRules = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _languageSortRulesByLocale[localeName] = None
        return None
    except Exception:
        get_nvda_logger().debugWarning("Could not load Google TTS language sort rules from %s.", path, exc_info=True)
        _languageSortRulesByLocale[localeName] = None
        return None
    rules = _normalize_language_sort_rules(rawRules)
    _languageSortRulesByLocale[localeName] = rules
    return rules


def _normalize_language_sort_rules(rawRules: Any) -> dict[str, Any] | None:
    if not isinstance(rawRules, dict):
        return None
    rawLetterOrder = rawRules.get("letterOrder")
    if not isinstance(rawLetterOrder, list) or not rawLetterOrder:
        return None
    letterOrder: dict[str, str] = {}
    for index, item in enumerate(rawLetterOrder):
        if not isinstance(item, str) or not item:
            return None
        letter = unicodedata.normalize("NFC", item.casefold())
        letterOrder[letter] = f"{index:04d}"
    stripPrefixes = tuple(prefix for prefix in rawRules.get("stripPrefixes", []) if isinstance(prefix, str) and prefix)
    ignoredMarks = _combining_marks_from_names(rawRules.get("ignoreCombiningMarks", []))
    return {
        "letterOrder": letterOrder,
        "stripPrefixes": stripPrefixes,
        "ignoredMarks": ignoredMarks,
    }


def _combining_marks_from_names(markNames: Any) -> set[str]:
    marks: set[str] = set()
    if not isinstance(markNames, list):
        return marks
    for name in markNames:
        if not isinstance(name, str):
            continue
        normalizedName = name.upper()
        try:
            marks.add(unicodedata.lookup(f"COMBINING {normalizedName}"))
        except KeyError:
            try:
                marks.add(unicodedata.lookup(f"COMBINING {normalizedName} ACCENT"))
            except KeyError:
                get_nvda_logger().debugWarning("Ignoring unknown Google TTS language sort combining mark: %s", name)
    return marks


def _strip_combining_marks(value: str, marks: set[str]) -> str:
    if not marks:
        return value
    return "".join(char for char in unicodedata.normalize("NFD", value) if char not in marks)


def _rule_based_visible_sort_key(displayName: str, rules: dict[str, Any]) -> tuple[str, str]:
    ignoredMarks = rules["ignoredMarks"]
    sortName = unicodedata.normalize("NFC", _strip_combining_marks(displayName.casefold(), ignoredMarks))
    for prefix in rules["stripPrefixes"]:
        normalizedPrefix = unicodedata.normalize("NFC", _strip_combining_marks(prefix.casefold(), ignoredMarks))
        if sortName.startswith(normalizedPrefix):
            sortName = sortName[len(normalizedPrefix) :].lstrip()
            break
    letterOrder = rules["letterOrder"]
    normalizedLetters = "".join(letterOrder.get(char, char) for char in sortName)
    return (normalizedLetters, sortName)


def visible_language_sort_key(displayName: str) -> tuple[str, str]:
    """Return a locale-aware sort key for a visible language display name."""
    rules = _language_sort_rules_for_current_ui()
    if rules is not None:
        return _rule_based_visible_sort_key(displayName, rules)
    return (displayName.casefold(), displayName)


def _resolve_alive_dialog_parent(parent: Any, guiMod: Any) -> Any:
    for candidate in (parent, getattr(guiMod, "mainFrame", None)):
        if candidate is None or isinstance(candidate, bool):
            continue
        try:
            isBeingDeleted = getattr(candidate, "IsBeingDeleted", None)
            if callable(isBeingDeleted) and isBeingDeleted():
                continue
            getId = getattr(candidate, "GetId", None)
            if callable(getId):
                getId()
            return candidate
        except RuntimeError:
            continue
    return None


def _button_with_default_focus(button: Any, focused: bool) -> Any:
    replaceFn = getattr(button, "_replace", None) or getattr(getattr(button, "value", None), "_replace", None)
    if callable(replaceFn):
        return replaceFn(defaultFocus=focused)
    return button


@contextlib.contextmanager
def _main_frame_popup_guard(mainFrame: Any, enabled: bool) -> Any:
    if enabled and mainFrame is not None:
        prePopup = getattr(mainFrame, "prePopup", None)
        if callable(prePopup):
            with contextlib.suppress(Exception):
                prePopup()
    try:
        yield
    finally:
        if enabled and mainFrame is not None:
            postPopup = getattr(mainFrame, "postPopup", None)
            if callable(postPopup):
                with contextlib.suppress(Exception):
                    postPopup()


def show_message_box(
    message: str,
    caption: str,
    style: int,
    parent: Any = None,
) -> int:
    """Display a modal message box across NVDA 2024–2027."""
    wx = _wx if _wx is not None else sys.modules.get("wx")
    if wx is None:
        import wx as _imported_wx

        wx = _imported_wx

    try:
        import core

        if getattr(core, "_hasShutdownBeenTriggered", False):
            return int(getattr(wx, "CANCEL", 16))
    except Exception:
        pass
    try:
        import gui
    except Exception:
        gui = None  # type: ignore

    messageMod = getattr(gui, "message", None) if gui is not None else None
    guiMsgBox = getattr(gui, "messageBox", None) if gui is not None else None
    modMsgBox = getattr(messageMod, "messageBox", None) if messageMod is not None else None
    messageDialogCls = getattr(messageMod, "MessageDialog", None)
    dialogTypeEnum = getattr(messageMod, "DialogType", None)
    defaultButtonEnum = getattr(messageMod, "DefaultButton", None)
    returnCodeEnum = getattr(messageMod, "ReturnCode", None)
    isMainThreadFn = getattr(wx, "IsMainThread", None)
    onMainThread = not callable(isMainThreadFn) or bool(isMainThreadFn())
    try:
        guiHelperMod = getattr(gui, "guiHelper", None)
    except Exception:
        guiHelperMod = None
    wxCallOnMain = getattr(messageMod, "wxCallOnMain", None) or getattr(guiHelperMod, "wxCallOnMain", None)
    if (
        callable(messageDialogCls)
        and dialogTypeEnum is not None
        and defaultButtonEnum is not None
        and returnCodeEnum is not None
        and (guiMsgBox is None or guiMsgBox is modMsgBox)
        and (onMainThread or callable(wxCallOnMain))
    ):
        if style & getattr(wx, "ICON_NONE", 0):
            dialogType = dialogTypeEnum.STANDARD
        elif style & getattr(wx, "ICON_ERROR", 0):
            dialogType = dialogTypeEnum.ERROR
        elif style & getattr(wx, "ICON_WARNING", 0):
            dialogType = dialogTypeEnum.WARNING
        else:
            dialogType = dialogTypeEnum.STANDARD
        buttons: list[Any] = []
        cancelDefault = bool(
            (style & int(getattr(wx, "CANCEL_DEFAULT", 0)))
            and not ((style & int(getattr(wx, "NO", 0))) and (style & int(getattr(wx, "NO_DEFAULT", 0))))
        )
        if style & (int(getattr(wx, "YES", 0)) | int(getattr(wx, "NO", 0))):
            noDefault = bool(style & int(getattr(wx, "NO_DEFAULT", 0)))
            yesBtn = defaultButtonEnum.YES
            if noDefault or cancelDefault:
                yesBtn = _button_with_default_focus(yesBtn, False)
            noBtn = _button_with_default_focus(defaultButtonEnum.NO, noDefault)
            buttons.extend((yesBtn, noBtn))
        else:
            okBtn = defaultButtonEnum.OK
            if cancelDefault and (style & int(getattr(wx, "CANCEL", 0))):
                okBtn = _button_with_default_focus(okBtn, False)
            buttons.append(okBtn)
        if style & int(getattr(wx, "CANCEL", 0)):
            buttons.append(_button_with_default_focus(defaultButtonEnum.CANCEL, cancelDefault))
        if style & int(getattr(wx, "HELP", 0)) and hasattr(defaultButtonEnum, "HELP"):
            buttons.append(defaultButtonEnum.HELP)

        codeMap: dict[int, int] = {}
        for enumName, wxName, idName in (
            ("YES", "YES", "ID_YES"),
            ("NO", "NO", "ID_NO"),
            ("CANCEL", "CANCEL", "ID_CANCEL"),
            ("OK", "OK", "ID_OK"),
            ("HELP", "HELP", "ID_HELP"),
        ):
            enumMember = getattr(returnCodeEnum, enumName, None)
            wxValue = getattr(wx, wxName, None)
            wxIdValue = getattr(wx, idName, None)
            if enumMember is not None and wxValue is not None:
                with contextlib.suppress(TypeError, ValueError):
                    codeMap[int(getattr(enumMember, "value", enumMember))] = int(wxValue)
            if wxIdValue is not None and wxValue is not None:
                codeMap.setdefault(int(wxIdValue), int(wxValue))
            if wxValue is not None:
                codeMap.setdefault(int(wxValue), int(wxValue))

        def _show_modern() -> int:
            dialogParent = _resolve_alive_dialog_parent(parent, gui)
            mainFrame = getattr(gui, "mainFrame", None)
            needPopupWrap = dialogParent is mainFrame and mainFrame is not None
            with _main_frame_popup_guard(mainFrame, needPopupWrap):
                dialog = messageDialogCls(
                    dialogParent, message, title=caption, dialogType=dialogType, buttons=tuple(buttons)
                )
                try:
                    rawResult = dialog.ShowModal()
                finally:
                    destroyFn = getattr(dialog, "Destroy", None)
                    if callable(destroyFn):
                        with contextlib.suppress(Exception):
                            destroyFn()
                try:
                    numericResult = int(getattr(rawResult, "value", rawResult))
                except (TypeError, ValueError):
                    return int(getattr(wx, "CANCEL", 16))
                return codeMap.get(numericResult, numericResult)

        if not onMainThread and callable(wxCallOnMain):
            return int(wxCallOnMain(_show_modern))
        return _show_modern()

    def _show_legacy() -> int:
        dialogParent = _resolve_alive_dialog_parent(parent, gui)
        if callable(guiMsgBox):
            return int(guiMsgBox(message, caption, style, dialogParent))
        wxMsgBox = getattr(wx, "MessageBox", None)
        if callable(wxMsgBox):
            mainFrame = getattr(gui, "mainFrame", None)
            needPopupWrap = dialogParent is mainFrame and mainFrame is not None
            with _main_frame_popup_guard(mainFrame, needPopupWrap):
                return int(wxMsgBox(message, caption, style, dialogParent))
        return int(getattr(wx, "CANCEL", 16))

    if not onMainThread:
        callAfter = getattr(wx, "CallAfter", None)
        getApp = getattr(wx, "GetApp", None)
        hasApp = callable(getApp) and (getApp() is not None)
        if not callable(callAfter):
            return _show_legacy() if not hasApp else int(getattr(wx, "CANCEL", 16))
        if callable(getApp) and not hasApp:
            return _show_legacy()
        done = threading.Event()
        resultBox: list[int] = []
        errorBox: list[BaseException] = []

        def _runner() -> None:
            try:
                resultBox.append(int(_show_legacy()))
            except BaseException as exc:
                errorBox.append(exc)
            finally:
                done.set()

        try:
            callAfter(_runner)
        except Exception:
            return int(getattr(wx, "CANCEL", 16))
        while not done.wait(0.25):
            try:
                import core

                if getattr(core, "_hasShutdownBeenTriggered", False):
                    return int(getattr(wx, "CANCEL", 16))
            except Exception:
                pass
            if callable(getApp) and getApp() is None:
                return int(getattr(wx, "CANCEL", 16))
        if errorBox:
            raise errorBox[0]
        return resultBox[0] if resultBox else int(getattr(wx, "CANCEL", 16))
    return _show_legacy()


NVDA_LOG_LEVEL_DEBUG_UNREDACTED = 5
NVDA_LOG_LEVEL_IO = 12
NVDA_LOG_LEVEL_DEBUGWARNING = 15
NVDA_LOG_LEVEL_OFF = 100

_NVDA_CUSTOM_LOG_LEVELS = (
    (NVDA_LOG_LEVEL_DEBUG_UNREDACTED, "DEBUG_UNREDACTED", None),
    (NVDA_LOG_LEVEL_IO, "IO", "io"),
    (NVDA_LOG_LEVEL_DEBUGWARNING, "DEBUGWARNING", "debugWarning"),
    (NVDA_LOG_LEVEL_OFF, "OFF", None),
)


def ensure_nvda_logger_methods(logger: Any) -> Any:
    """Attach NVDA custom log methods on standalone ``logging.Logger`` instances.

    Real NVDA ``logHandler.Logger`` instances already expose ``debugWarning`` and
    ``io`` natively and are returned untouched so NVDA's ``getCodePath()`` stack
    frame inspection remains exact.
    """
    if logger is None:
        return None
    if not isinstance(logger, logging.Logger):
        return logger
    if hasattr(logger, "debugWarning") and hasattr(logger, "io"):
        return logger

    for level, levelName, methodName in _NVDA_CUSTOM_LOG_LEVELS:
        if logging.getLevelName(level) != levelName:
            logging.addLevelName(level, levelName)
        if methodName is not None and not hasattr(logger, methodName):

            def _nvda_level_log(
                msg: Any,
                *args: Any,
                _level: int = level,
                _target: logging.Logger = logger,
                **kwargs: Any,
            ) -> Any:
                if not _target.isEnabledFor(_level):
                    return None
                callerStacklevel = kwargs.get("stacklevel", 1)
                if isinstance(callerStacklevel, int):
                    kwargs["stacklevel"] = callerStacklevel + 1
                return _target.log(_level, msg, *args, **kwargs)

            setattr(logger, methodName, _nvda_level_log)
    return logger


def get_nvda_logger(fallbackName: str = "googleTtsForNvda") -> Any:
    """Return NVDA's shared logger when available, else a compatible fallback logger."""
    logHandlerMod = sys.modules.get("logHandler")
    if logHandlerMod is None:
        with contextlib.suppress(Exception):
            import logHandler as logHandlerMod  # noqa: PLC0415
    nvdaLog = getattr(logHandlerMod, "log", None) if logHandlerMod is not None else None
    if nvdaLog is not None:
        return ensure_nvda_logger_methods(nvdaLog)
    return ensure_nvda_logger_methods(logging.getLogger(fallbackName))


def is_debug_for_synth_driver(
    synthDriverHandlerMod: Any = None,
    configMod: Any = None,
) -> bool:
    """Return True when NVDA's ``debugLog.synthDriver`` logging category is enabled."""
    resolvedSynthHandler = (
        synthDriverHandlerMod if synthDriverHandlerMod is not None else sys.modules.get("synthDriverHandler")
    )
    helper = getattr(resolvedSynthHandler, "isDebugForSynthDriver", None) if resolvedSynthHandler is not None else None
    if callable(helper):
        try:
            return bool(helper())
        except Exception:
            pass
    resolvedConfig = configMod if configMod is not None else sys.modules.get("config")
    conf = getattr(resolvedConfig, "conf", None) if resolvedConfig is not None else None
    if conf is not None:
        try:
            return bool(conf["debugLog"]["synthDriver"])
        except Exception:
            pass
    return False
