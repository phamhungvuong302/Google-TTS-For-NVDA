from __future__ import annotations

import builtins
import contextlib
import math
import sys
import webbrowser
from collections.abc import Callable, Iterator
from typing import Any

import wx
from synthDrivers.googleTtsForNvda import language_utils
from synthDrivers.googleTtsForNvda.bridge import SYNTH_NAME
from synthDrivers.googleTtsForNvda.language_utils import call_compat as call_compat

try:
    import globalVars as _globalVars
except Exception:
    _globalVars = None  # type: ignore[assignment]

try:
    import synthDriverHandler as _synthDriverHandler
except Exception:
    _synthDriverHandler = None  # type: ignore[assignment]

try:
    import gui as _gui
    from gui import guiHelper as _guiHelper
except Exception:
    _gui = None  # type: ignore[assignment]
    _guiHelper = None  # type: ignore[assignment]

_log = language_utils.get_nvda_logger()

try:
    import addonHandler

    addonHandler.initTranslation()
except Exception:

    def _(message: str) -> str:
        return message


PAYPAL_DONATE_URL = "https://paypal.me/phamhungvuong302"
DONATE_DOC_URL = "https://github.com/phamhungvuong302/Google-TTS-For-NVDA/blob/main/DONATE.md"
DONATE_TRANSFER_MEMO = "Thanks for Google TTS For NVDA"
_donateDialogActive = False


def _nvda_module(name: str, fallback: Any = None) -> Any:
    return sys.modules.get(name, fallback)


def _nvda_log() -> Any:
    logHandlerMod = sys.modules.get("logHandler")
    nvdaLog = getattr(logHandlerMod, "log", None) if logHandlerMod is not None else None
    if nvdaLog is not None:
        return language_utils.ensure_nvda_logger_methods(nvdaLog)
    return _log


def is_secure_mode() -> bool:
    """Return True when NVDA is running in secure mode."""
    try:
        globalVars = _nvda_module("globalVars", _globalVars)
        return bool(getattr(getattr(globalVars, "appArgs", None), "secure", False))
    except Exception:
        return False


def is_google_synth_current() -> bool:
    """Return True when Google TTS For NVDA is the active synthesizer."""
    try:
        synthDriverHandler = _nvda_module("synthDriverHandler", _synthDriverHandler)
        if synthDriverHandler is None:
            return False
        return getattr(synthDriverHandler.getSynth(), "name", "") == SYNTH_NAME
    except Exception:
        return False


def nvda_translate(message: str) -> str:
    """Translate a core NVDA UI string using NVDA's own translation catalog."""
    try:
        languageHandlerMod = _nvda_module("languageHandler")
        translationRef = getattr(languageHandlerMod, "installedTranslation", None)
        translation = translationRef() if callable(translationRef) else None
        if translation is not None:
            return str(translation.gettext(message))
    except Exception:
        _nvda_log().debugWarning("Could not use NVDA translation for core string.", exc_info=True)
    nvdaGettext = getattr(builtins, "_", None)
    if callable(nvdaGettext):
        try:
            return str(nvdaGettext(message))
        except Exception:
            pass
    return message


def _from_dip(window: wx.Window, value: int) -> int:
    try:
        return int(window.FromDIP(value))
    except Exception:
        return value


def _estimate_wrapped_line_count(control: wx.TextCtrl, text: str, width: int) -> int:
    try:
        charWidth = max(1, int(control.GetTextExtent("M")[0]))
    except Exception:
        charWidth = _from_dip(control, 8)
    availableChars = max(12, width // max(1, charWidth))
    lines = 0
    for line in (text or "").splitlines() or [""]:
        lines += max(1, math.ceil(len(line) / availableChars))
    return lines


def _estimate_text_width(control: wx.TextCtrl, text: str) -> int:
    widths: list[int] = []
    for line in (text or "").splitlines() or [""]:
        try:
            widths.append(int(control.GetTextExtent(line)[0]))
        except Exception:
            widths.append(len(line) * _from_dip(control, 8))
    return max(widths or [0]) + _from_dip(control, 28)


def _max_read_only_text_width(control: wx.TextCtrl) -> int:
    defaultMaxWidth = _from_dip(control, 760)
    try:
        displayIndex = wx.Display.GetFromWindow(control)
        if displayIndex < 0:
            displayIndex = 0
        displayWidth = wx.Display(displayIndex).GetClientArea().GetWidth()
    except Exception:
        return defaultMaxWidth
    return min(defaultMaxWidth, max(_from_dip(control, 420), int(displayWidth * 0.75)))


def _read_only_text_target_width(control: wx.TextCtrl, text: str, width: int | None) -> int:
    if width is not None:
        return _from_dip(control, width)
    contentWidth = _estimate_text_width(control, text)
    minWidth = _from_dip(control, 360)
    maxWidth = _max_read_only_text_width(control)
    targetWidth = max(contentWidth, minWidth)
    return min(maxWidth, targetWidth)


def resize_read_only_text_for_content(
    control: wx.TextCtrl,
    minLines: int = 2,
    maxLines: int = 6,
    width: int | None = None,
) -> None:
    if not window_is_alive(control):
        return
    try:
        text = control.GetValue()
        targetWidth = _read_only_text_target_width(control, text, width)
        lineCount = _estimate_wrapped_line_count(control, text, targetWidth)
        lineCount = max(minLines, min(maxLines, lineCount))
        try:
            lineHeight = max(1, int(control.GetCharHeight()))
        except Exception:
            lineHeight = _from_dip(control, 16)
        height = lineCount * lineHeight + _from_dip(control, 14)
        control.SetMinSize((targetWidth, height))
        with contextlib.suppress(Exception):
            control.InvalidateBestSize()
    except RuntimeError:
        return


def bind_read_only_text_focus_announcement(
    control: wx.TextCtrl,
    minLines: int = 2,
    maxLines: int = 6,
    width: int | None = None,
) -> None:
    # Kept for existing call sites; focus now uses the normal read-only edit behavior.
    resize_read_only_text_for_content(control, minLines=minLines, maxLines=maxLines, width=width)


def format_size_mb(size: int) -> str:
    """Format bytes as megabytes with 1 decimal place."""
    if size <= 0:
        return ""
    return f"{size / (1024 * 1024):.1f} MB"


def format_size_auto(size: int) -> str:
    """Format bytes automatically choosing MB, KB, or bytes."""
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} bytes"


def window_is_alive(window: wx.Window | None) -> bool:
    """Return True if a wx window object still exists and is not being deleted."""
    if window is None or isinstance(window, bool):
        return False
    try:
        if hasattr(type(window), "__bool__") and not bool(window):
            return False
        isBeingDeleted = getattr(window, "IsBeingDeleted", None)
        if callable(isBeingDeleted) and isBeingDeleted():
            return False
        getId = getattr(window, "GetId", None)
        if callable(getId):
            getId()
        return True
    except RuntimeError:
        return False
    except Exception:
        return False


def dialog_parent(preferredParent: wx.Window | None = None) -> wx.Window | None:
    """Return a live top-level parent window, falling back to gui.mainFrame when alive."""
    if window_is_alive(preferredParent):
        getTopLevelParent = getattr(preferredParent, "GetTopLevelParent", None)
        if callable(getTopLevelParent):
            try:
                parent = getTopLevelParent()
                if window_is_alive(parent):
                    return parent
            except RuntimeError:
                pass
        return preferredParent
    guiMod = _nvda_module("gui", _gui)
    if guiMod is None:
        return None
    mainFrame = getattr(guiMod, "mainFrame", None)
    if window_is_alive(mainFrame):
        return mainFrame
    return None


def pre_popup() -> None:
    """Safely invoke gui.mainFrame.prePopup() when mainFrame is available."""
    guiMod = _nvda_module("gui", _gui)
    if guiMod is None:
        return
    mainFrame = getattr(guiMod, "mainFrame", None)
    prePopup = getattr(mainFrame, "prePopup", None)
    if callable(prePopup):
        with contextlib.suppress(RuntimeError):
            prePopup()


def post_popup() -> None:
    """Safely invoke gui.mainFrame.postPopup() when mainFrame is available."""
    guiMod = _nvda_module("gui", _gui)
    if guiMod is None:
        return
    mainFrame = getattr(guiMod, "mainFrame", None)
    postPopup = getattr(mainFrame, "postPopup", None)
    if callable(postPopup):
        with contextlib.suppress(RuntimeError):
            postPopup()


@contextlib.contextmanager
def popup_parent_context(preferredParent: wx.Window | None = None) -> Iterator[wx.Window | None]:
    parentWindow = dialog_parent(preferredParent)
    guiMod = _nvda_module("gui", _gui)
    mainFrame = getattr(guiMod, "mainFrame", None) if guiMod is not None else None
    wrapPopup = parentWindow is mainFrame and mainFrame is not None and window_is_alive(mainFrame)
    if wrapPopup:
        pre_popup()
    try:
        yield parentWindow
    finally:
        if wrapPopup:
            post_popup()


_popup_parent_context = popup_parent_context


def is_windows_store_nvda() -> bool:
    """Return True on Windows Store (AppX) NVDA builds where the attribute exists."""
    try:
        import config
    except Exception:
        return False
    return bool(getattr(config, "isAppX", False))


def show_message_box(
    message: str,
    caption: str | None = None,
    style: int | None = None,
    parent: wx.Window | None = None,
) -> int:
    """Display a modal message box compatible with NVDA 2024–2027."""
    dialogTitle = caption or _("Google TTS For NVDA")
    effectiveStyle = int(getattr(wx, "OK", 4) if style is None else style)
    return language_utils.show_message_box(message, dialogTitle, effectiveStyle, parent)


def stop_call_later(timer: Any) -> None:
    """Safely stop a wx.CallLater timer handle if present."""
    if timer is None:
        return
    stopFn = getattr(timer, "Stop", None)
    if callable(stopFn):
        with contextlib.suppress(Exception):
            stopFn()


def step_after_synth_switch(
    attempts: int,
    *,
    isStillActive: Callable[[], bool],
    isGoogleSynthCurrent: Callable[[], bool],
    onReady: Callable[[], None],
    onTimeout: Callable[[], None],
    retry: Callable[[int], None],
    isBusy: Callable[[], bool] | None = None,
    maxAttempts: int = 600,
) -> None:
    """Advance one 500 ms poll step waiting for the user to switch away from Google TTS."""
    if not isStillActive():
        return
    if not isGoogleSynthCurrent():
        if isBusy is not None and isBusy():
            retry(attempts)
            return
        onReady()
        return
    if attempts >= maxAttempts:
        onTimeout()
        return
    retry(attempts + 1)


def is_modal_dialog_open() -> bool:
    """Return True when NVDA reports a blocking modal dialog or message box is open."""
    try:
        import gui
    except Exception:
        return False
    try:
        blockAction = getattr(gui, "blockAction", None)
        if blockAction is None:
            from gui import blockAction  # type: ignore[no-redef]
        context = getattr(getattr(blockAction, "Context", None), "MODAL_DIALOG_OPEN", None)
        blockFn = getattr(context, "blockActionIf", None)
        if callable(blockFn):
            return bool(blockFn())
    except Exception:
        pass
    try:
        messageMod = getattr(gui, "message", None)
        if messageMod is None:
            from gui import message as messageMod  # type: ignore[no-redef]
        isModalActive = getattr(messageMod, "isModalMessageBoxActive", None)
        if callable(isModalActive):
            return bool(isModalActive())
    except Exception:
        pass
    return False


def open_synthesizer_dialog(
    parent: wx.Window | None = None,
    title: str | None = None,
) -> bool:
    """Open NVDA's Select Synthesizer dialog across NVDA versions."""
    import gui

    log = _nvda_log()
    dialogTitle = title or _("Google TTS For NVDA")
    try:
        from gui import settingsDialogs

        dialogClass = getattr(settingsDialogs, "SynthesizerSelectionDialog", None)
        if dialogClass is None:
            dialogClass = getattr(settingsDialogs, "SynthesizerDialog", None)
        if dialogClass is None:
            raise RuntimeError("Select Synthesizer dialog class was not found.")
        mainFrame = getattr(gui, "mainFrame", None)
        if mainFrame is None or not callable(getattr(mainFrame, "popupSettingsDialog", None)):
            raise RuntimeError("NVDA mainFrame is unavailable.")
        if is_modal_dialog_open():
            mainFrame.popupSettingsDialog(dialogClass)
            return False
        mainFrame.popupSettingsDialog(dialogClass)
        return True
    except Exception as exc:
        log.error("Could not open Select Synthesizer dialog: %s", exc, exc_info=True)
        show_message_box(
            _("The Select Synthesizer dialog could not be opened."),
            dialogTitle,
            wx.OK | wx.ICON_ERROR,
            dialog_parent(parent),
        )
        return False


def show_runtime_error_dialog(
    message: str | None = None,
    parent: wx.Window | None = None,
    title: str | None = None,
    delayMs: int = 0,
) -> None:
    """Show a standardized modal error dialog for Google TTS runtime or speech failures."""
    isMainThread = getattr(wx, "IsMainThread", None)
    if callable(isMainThread):
        try:
            onMainThread = bool(isMainThread())
        except Exception:
            onMainThread = True
        if not onMainThread:
            # Never fall through to modal GUI work on a worker thread, even if CallAfter fails.
            try:
                wx.CallAfter(
                    show_runtime_error_dialog,
                    message=message,
                    parent=parent,
                    title=title,
                    delayMs=delayMs,
                )
            except Exception:
                with contextlib.suppress(Exception):
                    _nvda_log().debugWarning(
                        "Could not marshal Google TTS runtime error dialog to the main thread.",
                        exc_info=True,
                    )
            return

    dialogTitle = title or _("Google TTS For NVDA")
    dialogMessage = message or _("Google TTS For NVDA could not start speech in the Chromium browser runtime.")

    def _display() -> None:
        try:
            show_message_box(
                dialogMessage,
                dialogTitle,
                wx.OK | wx.ICON_ERROR,
                dialog_parent(parent),
            )
        except Exception:
            with contextlib.suppress(Exception):
                _nvda_log().exception("Could not show Google TTS runtime error dialog.")

    if delayMs > 0:
        try:
            wx.CallLater(delayMs, _display)
            return
        except Exception:
            pass
    _display()


def copy_text_to_clipboard(text: str) -> bool:
    """Copy plain text to the wx clipboard and return True on success."""
    clipboard = getattr(wx, "TheClipboard", None)
    textDataObjectCls = getattr(wx, "TextDataObject", None)
    if clipboard is None or not callable(textDataObjectCls):
        return False
    try:
        if not clipboard.Open():
            return False
    except Exception:
        return False
    try:
        setDataResult = clipboard.SetData(textDataObjectCls(text))
        if setDataResult is False:
            return False
        clipboard.Flush()
        return True
    except Exception:
        return False
    finally:
        with contextlib.suppress(Exception):
            clipboard.Close()


def show_manual_web_url_dialog(
    url: str,
    *,
    parent: wx.Window | None = None,
    messageText: str | None = None,
    addressLabel: str | None = None,
) -> None:
    """Display a modal dialog allowing the user to copy a URL that could not be opened automatically."""
    if is_secure_mode():
        return
    with _popup_parent_context(parent) as parentWindow:
        guiHelperMod = _nvda_module("gui.guiHelper", _guiHelper)
        border = getattr(guiHelperMod, "BORDER_FOR_DIALOGS", 10)
        boxSizerHelperCls = getattr(guiHelperMod, "BoxSizerHelper", None)
        if not callable(boxSizerHelperCls):
            return
        dialog = wx.Dialog(parentWindow, title=_("Google TTS For NVDA"))
        try:
            mainSizer = wx.BoxSizer(wx.VERTICAL)
            helper = boxSizerHelperCls(dialog, sizer=mainSizer)
            message = wx.StaticText(
                dialog,
                label=messageText or _("The download page could not be opened. Please visit this address manually:"),
            )
            message.Wrap(_from_dip(dialog, 520))
            helper.addItem(message, flag=wx.EXPAND)
            resolvedAddressLabel = addressLabel or _("Download address")
            urlControl = helper.addLabeledControl(
                resolvedAddressLabel + ":",
                wx.TextCtrl,
                value=url,
                style=wx.TE_READONLY | wx.TE_MULTILINE | wx.TE_WORDWRAP,
            )
            urlControl.SetName(resolvedAddressLabel)
            bind_read_only_text_focus_announcement(urlControl, minLines=2, maxLines=5)
            buttonSizer = wx.BoxSizer(wx.HORIZONTAL)
            copyButton = wx.Button(dialog, label=_("&Copy link"))
            closeButton = wx.Button(dialog, id=wx.ID_OK, label=_("&Close"))
            buttonSizer.Add(copyButton, border=border, flag=wx.RIGHT)
            buttonSizer.Add(closeButton)
            mainSizer.Add(buttonSizer, border=border, flag=wx.ALL | wx.ALIGN_RIGHT)
            dialog.SetSizerAndFit(mainSizer)
            dialog.SetEscapeId(wx.ID_OK)
            closeButton.SetDefault()

            def on_copy(evt: wx.CommandEvent | None = None) -> None:
                if copy_text_to_clipboard(url):
                    show_message_box(
                        _("The link was copied to the clipboard."),
                        _("Google TTS For NVDA"),
                        wx.OK | wx.ICON_INFORMATION,
                        dialog,
                    )
                else:
                    show_message_box(
                        _("The link could not be copied to the clipboard."),
                        _("Google TTS For NVDA"),
                        wx.OK | wx.ICON_ERROR,
                        dialog,
                    )

            copyButton.Bind(wx.EVT_BUTTON, on_copy)
            closeButton.Bind(wx.EVT_BUTTON, lambda evt: dialog.EndModal(wx.ID_OK))
            dialog.ShowModal()
        finally:
            dialog.Destroy()


def open_web_url(
    url: str,
    *,
    parent: wx.Window | None = None,
    logContext: str = "external link",
    fallbackMessage: str | None = None,
    addressLabel: str | None = None,
) -> None:
    """Open a web URL in the system browser, falling back to a manual copy dialog on failure."""
    try:
        opened = webbrowser.open(url, new=2)
        if opened is False:
            raise RuntimeError("webbrowser.open returned False.")
    except Exception:
        logger = _nvda_log()
        if logger is not None:
            with contextlib.suppress(Exception):
                logger.warning("Could not open URL for %s.", logContext, exc_info=True)
        show_manual_web_url_dialog(
            url,
            parent=parent,
            messageText=fallbackMessage,
            addressLabel=addressLabel,
        )


def show_three_button_dialog(
    *,
    title: str,
    messageText: str,
    primaryLabel: str,
    secondaryLabel: str,
    cancelLabel: str,
    parent: wx.Window | None = None,
    wrapWidth: int = 520,
) -> tuple[int, wx.Window | None]:
    """Display a modal three-button prompt returning (wx.ID_YES/NO/CANCEL, resolvedParent)."""
    result = wx.ID_CANCEL
    parentWindow: wx.Window | None = None
    with popup_parent_context(parent) as resolvedParent:
        parentWindow = resolvedParent
        guiHelperMod = _nvda_module("gui.guiHelper", _guiHelper)
        border = getattr(guiHelperMod, "BORDER_FOR_DIALOGS", 10)
        dialog = wx.Dialog(parentWindow, title=title)
        try:
            mainSizer = wx.BoxSizer(wx.VERTICAL)
            message = wx.StaticText(dialog, label=messageText)
            message.Wrap(_from_dip(dialog, wrapWidth))
            mainSizer.Add(message, border=border, flag=wx.ALL | wx.EXPAND)
            buttonSizer = wx.BoxSizer(wx.HORIZONTAL)
            primaryButton = wx.Button(dialog, label=primaryLabel)
            secondaryButton = wx.Button(dialog, label=secondaryLabel)
            cancelButton = wx.Button(dialog, id=wx.ID_CANCEL, label=cancelLabel)
            buttonSizer.Add(primaryButton, border=border, flag=wx.RIGHT)
            buttonSizer.Add(secondaryButton, border=border, flag=wx.RIGHT)
            buttonSizer.Add(cancelButton)
            mainSizer.Add(buttonSizer, border=border, flag=wx.ALL | wx.ALIGN_RIGHT)
            dialog.SetSizerAndFit(mainSizer)
            dialog.SetEscapeId(wx.ID_CANCEL)
            primaryButton.SetDefault()
            primaryButton.Bind(wx.EVT_BUTTON, lambda evt: dialog.EndModal(wx.ID_YES))
            secondaryButton.Bind(wx.EVT_BUTTON, lambda evt: dialog.EndModal(wx.ID_NO))
            cancelButton.Bind(wx.EVT_BUTTON, lambda evt: dialog.EndModal(wx.ID_CANCEL))
            result = dialog.ShowModal()
        finally:
            dialog.Destroy()
    return result, parentWindow


def show_donate_dialog(parent: wx.Window | None = None) -> None:
    """Show the Donate dialog with links to PayPal and payment methods."""
    global _donateDialogActive
    if _donateDialogActive or is_secure_mode():
        return
    _donateDialogActive = True
    try:
        result, parentWindow = show_three_button_dialog(
            title=_("Donate to Google TTS For NVDA"),
            messageText=_(
                "Google TTS For NVDA is free, open-source software, and donations are completely optional.\n\n"
                "Contributions go to the maintainer to support ongoing development, compatibility updates, "
                'and releases. When sending a transfer, please include the note "{memo}" so it is easy '
                "to identify.\n\n"
                "Choose Open PayPal link to donate through PayPal in your browser. "
                "Choose View payment methods to see bank transfer details for Vietnam, the US, and "
                "international SWIFT on GitHub."
            ).format(memo=DONATE_TRANSFER_MEMO),
            primaryLabel=_("Open &PayPal link"),
            secondaryLabel=_("&View payment methods"),
            cancelLabel=_("&Close"),
            parent=parent,
        )
        if result == wx.ID_YES:
            open_web_url(
                PAYPAL_DONATE_URL,
                parent=parentWindow,
                logContext="PayPal link",
                fallbackMessage=_("The PayPal link could not be opened. Please visit this address manually:"),
                addressLabel=_("PayPal address"),
            )
        elif result == wx.ID_NO:
            open_web_url(
                DONATE_DOC_URL,
                parent=parentWindow,
                logContext="payment methods page",
                fallbackMessage=_("The payment methods page could not be opened. Please visit this address manually:"),
                addressLabel=_("Payment methods address"),
            )
    finally:
        _donateDialogActive = False
