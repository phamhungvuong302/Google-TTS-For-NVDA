from __future__ import annotations

import contextlib
import http.client
import json
import os
import socket
import ssl
import threading
import urllib.error
from collections import Counter
from collections.abc import Callable
from typing import Any

import addonHandler
import config
import synthDriverHandler
import ui
import wx
from gui import nvdaControls
from synthDrivers.googleTtsForNvda import audio_math, language_utils, standby, voice_store
from synthDrivers.googleTtsForNvda import bridge as browserBridge
from synthDrivers.googleTtsForNvda.catalog import VoiceCatalog, VoicePackage, is_package_supported_by_engine

from .uiUtils import (
    call_compat as _call_compat,
)
from .uiUtils import (
    dialog_parent,
    format_size_mb,
    is_google_synth_current,
    open_synthesizer_dialog,
    show_message_box,
    step_after_synth_switch,
    stop_call_later,
    window_is_alive,
)

log = language_utils.get_nvda_logger()

addonHandler.initTranslation()

SYNTH_NAME = browserBridge.SYNTH_NAME


def _language_codes_for_display(packages: list[VoicePackage]) -> list[str]:
    seen: set[str] = set()
    codes: list[str] = []
    for package in packages:
        normalizedCode = package.language.lower()
        if normalizedCode in seen:
            continue
        seen.add(normalizedCode)
        codes.append(package.language)
    codes.sort(
        key=lambda code: language_utils.visible_language_sort_key(language_utils.get_language_display_name(code))
    )
    return codes


class VoiceManagerDialog(nvdaControls.DPIScaledDialog):
    def __init__(
        self,
        parent: wx.Window,
        onDestroy: Callable[[VoiceManagerDialog], None],
        initialPage: str = "installed",
    ) -> None:
        super().__init__(
            parent,
            title=_("Google TTS Voice Manager"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        scaleSize = getattr(self, "scaleSize", lambda value: value)
        self._onDestroy = onDestroy
        self.catalog = VoiceCatalog.load()
        self.installedPackages: list[VoicePackage] = []
        self.downloadPackages: list[VoicePackage] = []
        self._allInstalledPackages: list[VoicePackage] = []
        self._allInstalledPackageIds: set[str] = set()
        self._allUsableInstalledPackages: list[VoicePackage] = []
        self._allUsableInstalledPackageIds: set[str] = set()
        self._allDownloadPackages: list[VoicePackage] = []
        self.isBusy = False
        self._closing = False
        self._bulkChecking = False
        self._pendingRemoveAfterSynthSwitch: list[VoicePackage] | None = None
        self._pendingRemoveTimer: Any = None
        self._initialPage = initialPage
        self._lastProgressAnnouncement = -1
        self._build_ui()
        self.SetSize(scaleSize((880, 640)))
        self.SetMinSize(scaleSize((720, 520)))
        self.SetEscapeId(wx.ID_CLOSE)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
        self.refresh_lists()
        wx.CallAfter(self.focus_default_control)

    def _build_ui(self) -> None:
        root = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(root)

        self.notebook = wx.Notebook(self)
        root.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 10)

        self.installedPanel = wx.Panel(self.notebook)
        self.downloadPanel = wx.Panel(self.notebook)
        self.notebook.AddPage(self.installedPanel, _("Installed"))
        self.notebook.AddPage(self.downloadPanel, _("Download"))
        self._build_installed_tab()
        self._build_download_tab()
        self.notebook.Bind(wx.EVT_KEY_DOWN, self._on_notebook_key_down)

        statusRow = wx.BoxSizer(wx.HORIZONTAL)
        self.statusText = wx.StaticText(self, label=_("Ready."))
        self.statusText.SetName(_("Status"))
        self.progressGauge = wx.Gauge(self, range=100)
        self.progressGauge.SetName(_("Progress"))
        self.progressGauge.SetValue(0)
        statusRow.Add(self.statusText, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        statusRow.Add(self.progressGauge, 0, wx.ALIGN_CENTER_VERTICAL)
        root.Add(statusRow, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        buttonRow = wx.BoxSizer(wx.HORIZONTAL)
        self.refreshButton = wx.Button(self, label=_("&Refresh"))
        self.openFolderButton = wx.Button(self, label=_("&Open voice packages folder"))
        self.closeButton = wx.Button(self, id=wx.ID_CLOSE)
        self.refreshButton.Bind(wx.EVT_BUTTON, lambda evt: self.refresh_lists(announce=True))
        self.openFolderButton.Bind(wx.EVT_BUTTON, self.on_open_folder)
        self.closeButton.Bind(wx.EVT_BUTTON, lambda evt: self.Close())
        buttonRow.Add(self.refreshButton)
        buttonRow.AddSpacer(8)
        buttonRow.Add(self.openFolderButton)
        buttonRow.AddStretchSpacer()
        buttonRow.Add(self.closeButton)
        root.Add(buttonRow, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

    def _build_package_tab(
        self,
        panel: wx.Panel,
        *,
        filterComboName: str,
        onFilterChanged: Callable[[wx.CommandEvent], None],
        onSelectAll: Callable[[wx.CommandEvent], None],
        listName: str,
        onItemCheckChanged: Callable[[wx.ListEvent], None],
        actionLabel: str,
        onAction: Callable[[wx.CommandEvent], None],
    ) -> tuple[wx.StaticText, wx.Choice, wx.CheckBox, wx.ListCtrl, wx.Button]:
        sizer = wx.BoxSizer(wx.VERTICAL)
        panel.SetSizer(sizer)

        filterRow = wx.BoxSizer(wx.HORIZONTAL)
        filterLabel = wx.StaticText(panel, label=_("&Filter by language:"))
        languageCombo = wx.Choice(panel)
        languageCombo.SetName(filterComboName)
        languageCombo.Bind(wx.EVT_CHOICE, onFilterChanged)
        filterRow.Add(filterLabel, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        filterRow.Add(languageCombo, 1, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(filterRow, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        selectAllCheck = wx.CheckBox(panel, label=_("Check &all voice packages"))
        selectAllCheck.Bind(wx.EVT_CHECKBOX, onSelectAll)
        sizer.Add(selectAllCheck, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        listCtrl = self._create_list(panel, includeStatus=True)
        listCtrl.SetName(listName)
        listCtrl.Bind(wx.EVT_LIST_ITEM_CHECKED, onItemCheckChanged)
        listCtrl.Bind(wx.EVT_LIST_ITEM_UNCHECKED, onItemCheckChanged)
        sizer.Add(listCtrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        buttonRow = wx.BoxSizer(wx.HORIZONTAL)
        actionButton = wx.Button(panel, label=actionLabel)
        actionButton.Bind(wx.EVT_BUTTON, onAction)
        buttonRow.Add(actionButton)
        sizer.Add(buttonRow, 0, wx.EXPAND | wx.ALL, 8)
        return filterLabel, languageCombo, selectAllCheck, listCtrl, actionButton

    def _build_installed_tab(self) -> None:
        (
            self.installedFilterLabel,
            self.installedLanguageCombo,
            self.installedSelectAllCheck,
            self.installedList,
            self.removeButton,
        ) = self._build_package_tab(
            self.installedPanel,
            filterComboName=_("Filter installed voice packages by language"),
            onFilterChanged=self.on_installed_language_filter_changed,
            onSelectAll=self.on_installed_select_all,
            listName=_("Installed voice packages"),
            onItemCheckChanged=self._on_installed_item_check_changed,
            actionLabel=_("&Remove checked voice packages"),
            onAction=self.on_remove_selected,
        )

    def _build_download_tab(self) -> None:
        (
            self.downloadFilterLabel,
            self.downloadLanguageCombo,
            self.downloadSelectAllCheck,
            self.downloadList,
            self.downloadButton,
        ) = self._build_package_tab(
            self.downloadPanel,
            filterComboName=_("Filter downloadable voice packages by language"),
            onFilterChanged=self.on_download_language_filter_changed,
            onSelectAll=self.on_download_select_all,
            listName=_("Downloadable voice packages"),
            onItemCheckChanged=self._on_download_item_check_changed,
            actionLabel=_("&Download checked voice packages"),
            onAction=self.on_download_selected,
        )

    def _create_list(self, parent: wx.Window, includeStatus: bool = False) -> wx.ListCtrl:
        listCtrl = wx.ListCtrl(parent, style=wx.LC_REPORT | wx.LC_HRULES | wx.LC_VRULES)
        if hasattr(listCtrl, "EnableCheckBoxes"):
            listCtrl.EnableCheckBoxes()
        columns = [
            (_("Language"), 110),
            (_("Package"), 210),
            (_("Voices"), 300),
            (_("Size"), 100),
        ]
        if includeStatus:
            columns.append((_("Status"), 180))
        scaleSize = getattr(self, "scaleSize", lambda value: value)
        for index, (label, width) in enumerate(columns):
            listCtrl.InsertColumn(index, label, width=int(scaleSize(width)))
        return listCtrl

    def _update_language_combo(self, combo: wx.Choice, packages: list[VoicePackage]) -> None:
        unique_codes = _language_codes_for_display(packages)
        display_names = [_("All")] + [language_utils.get_language_display_name(code) for code in unique_codes]

        oldIdx = combo.GetSelection()
        oldSelectionCode = ""
        if hasattr(combo, "_langCodes") and 0 < oldIdx <= len(combo._langCodes):
            oldSelectionCode = combo._langCodes[oldIdx - 1]

        combo.Clear()
        combo.AppendItems(display_names)
        combo._langCodes = unique_codes

        newIdx = 0
        if oldSelectionCode and oldSelectionCode in unique_codes:
            newIdx = unique_codes.index(oldSelectionCode) + 1
        combo.SetSelection(newIdx)

    def _filter_packages_by_combo(self, combo: wx.Choice, allPackages: list[VoicePackage]) -> list[VoicePackage]:
        idx = combo.GetSelection()
        langCodes = getattr(combo, "_langCodes", None)
        if idx <= 0 or not langCodes or idx > len(langCodes):
            filtered = list(allPackages)
        else:
            targetCode = langCodes[idx - 1].lower()
            filtered = [pkg for pkg in allPackages if pkg.language.lower() == targetCode]
        filtered.sort(key=self._visible_package_sort_key)
        return filtered

    def _apply_installed_filter(self) -> None:
        self.installedPackages = self._filter_packages_by_combo(self.installedLanguageCombo, self._allInstalledPackages)
        self._populate_installed_list()
        self._refresh_buttons()

    def _apply_download_filter(self) -> None:
        self.downloadPackages = self._filter_packages_by_combo(self.downloadLanguageCombo, self._allDownloadPackages)
        self._populate_download_list()
        self._refresh_buttons()

    def on_installed_language_filter_changed(self, evt: wx.CommandEvent) -> None:
        self._apply_installed_filter()

    def on_download_language_filter_changed(self, evt: wx.CommandEvent) -> None:
        self._apply_download_filter()

    def _clear_pending_remove(self) -> None:
        self._pendingRemoveAfterSynthSwitch = None
        stop_call_later(getattr(self, "_pendingRemoveTimer", None))
        self._pendingRemoveTimer = None

    def refresh_lists(self, announce: bool = False) -> None:
        self._clear_pending_remove()
        self._allInstalledPackages = voice_store.physically_installed_packages(self.catalog)
        self._allInstalledPackageIds = {pkg.id for pkg in self._allInstalledPackages}
        self._allUsableInstalledPackages = voice_store.usable_installed_packages(self._allInstalledPackages)
        self._allUsableInstalledPackageIds = {pkg.id for pkg in self._allUsableInstalledPackages}
        self._allDownloadPackages = [pkg for pkg in self.catalog.packages if pkg.id not in self._allInstalledPackageIds]
        supportedDownloadCount = sum(1 for pkg in self._allDownloadPackages if is_package_supported_by_engine(pkg))

        summary = _("{installed} installed voice packages, {available} available to download.").format(
            installed=len(self._allInstalledPackages),
            available=supportedDownloadCount,
        )
        title = _(
            "{installed} installed voice packages, {available} available to download - Google TTS Voice Manager"
        ).format(
            installed=len(self._allInstalledPackages),
            available=supportedDownloadCount,
        )
        self.SetTitle(title)

        self._update_language_combo(self.installedLanguageCombo, self._allInstalledPackages)
        self._update_language_combo(self.downloadLanguageCombo, self._allDownloadPackages)

        self._apply_installed_filter()
        self._apply_download_filter()
        if announce:
            self.set_status(summary, 0, announce=True)

    def focus_default_control(self) -> None:
        if self._closing or not window_is_alive(self):
            return
        with contextlib.suppress(RuntimeError):
            if self._initialPage == "download":
                self._initialPage = "installed"
                self.show_download_tab()
                return
            self._focus_active_page()

    def show_download_tab(self) -> None:
        self.notebook.SetSelection(1)
        self._focus_download_tab()

    def _focus_active_page(self) -> None:
        if self.notebook.GetSelection() == 1:
            self._focus_download_tab()
        else:
            self._focus_installed_tab()

    def _focus_installed_tab(self) -> None:
        self.notebook.SetFocus()

    def _focus_download_tab(self) -> None:
        self.notebook.SetFocus()

    def on_char_hook(self, evt: wx.KeyEvent) -> None:
        if evt.GetKeyCode() != wx.WXK_TAB or not evt.ControlDown():
            evt.Skip()
            return
        pageCount = self.notebook.GetPageCount()
        if pageCount <= 1:
            evt.Skip()
            return
        currentPage = self.notebook.GetSelection()
        if currentPage == wx.NOT_FOUND:
            currentPage = 0
        direction = -1 if evt.ShiftDown() else 1
        newPage = (currentPage + direction) % pageCount
        self.notebook.SetSelection(newPage)
        self.notebook.SetFocus()

    def _on_notebook_key_down(self, evt: wx.KeyEvent) -> None:
        key = evt.GetKeyCode()
        if key in (wx.WXK_UP, wx.WXK_DOWN, wx.WXK_LEFT, wx.WXK_RIGHT):
            pageCount = self.notebook.GetPageCount()
            if pageCount > 1:
                currentPage = self.notebook.GetSelection()
                if currentPage == wx.NOT_FOUND:
                    currentPage = 0
                direction = -1 if key in (wx.WXK_UP, wx.WXK_LEFT) else 1
                newPage = (currentPage + direction) % pageCount
                self.notebook.SetSelection(newPage)
                self.notebook.SetFocus()
            return
        evt.Skip()

    def _populate_package_list(
        self,
        listCtrl: wx.ListCtrl,
        packages: list[VoicePackage],
        allPackages: list[VoicePackage],
        statusFn: Callable[[VoicePackage, int], str],
        selectAllCheck: wx.CheckBox,
    ) -> None:
        listCtrl.Freeze()
        try:
            listCtrl.DeleteAllItems()
            dependentCounts = Counter(
                pkg.dependentVoiceId
                for pkg in allPackages
                if pkg.dependentVoiceId and is_package_supported_by_engine(pkg)
            )
            for index, package in enumerate(packages):
                status = statusFn(package, dependentCounts.get(package.id, 0))
                self._insert_package_row(listCtrl, index, package, status)
            if listCtrl.ItemCount:
                listCtrl.Select(0)
            # Reset the select-all toggle when list contents change.
            selectAllCheck.SetValue(False)
        finally:
            listCtrl.Thaw()

    def _populate_installed_list(self) -> None:
        self._populate_package_list(
            self.installedList,
            self.installedPackages,
            self._allInstalledPackages,
            self._installed_package_status,
            self.installedSelectAllCheck,
        )

    def _populate_download_list(self) -> None:
        self._populate_package_list(
            self.downloadList,
            self.downloadPackages,
            self._allDownloadPackages,
            self._download_package_status,
            self.downloadSelectAllCheck,
        )

    def _visible_package_sort_key(self, package: VoicePackage) -> tuple[tuple[str, str], str]:
        return (
            language_utils.visible_language_sort_key(language_utils.get_language_display_name(package.language)),
            package.id,
        )

    def _insert_package_row(
        self,
        listCtrl: wx.ListCtrl,
        index: int,
        package: VoicePackage,
        status: str | Callable[[VoicePackage], str] | None = None,
    ) -> None:
        listCtrl.InsertItem(index, language_utils.get_language_display_name(package.language))
        listCtrl.SetItem(index, 1, package.id)
        listCtrl.SetItem(index, 2, self._speaker_names(package))
        listCtrl.SetItem(index, 3, self._format_size(package.compressedSize))
        if status:
            statusText = status(package) if callable(status) else status
            listCtrl.SetItem(index, 4, statusText)

    def _direct_dependents(self, package: VoicePackage, sourcePackages: list[VoicePackage]) -> list[VoicePackage]:
        return [
            candidate
            for candidate in sourcePackages
            if candidate.dependentVoiceId == package.id and is_package_supported_by_engine(candidate)
        ]

    def _direct_installed_dependents(self, package: VoicePackage) -> list[VoicePackage]:
        return self._direct_dependents(package, self._allInstalledPackages)

    def _direct_download_dependents(self, package: VoicePackage) -> list[VoicePackage]:
        return self._direct_dependents(package, self._allDownloadPackages)

    def _installed_package_status(self, package: VoicePackage, dependentCount: int | None = None) -> str:
        if not is_package_supported_by_engine(package):
            return _("Not supported by the bundled engine")
        if package.dependentVoiceId:
            if package.dependentVoiceId not in self._allInstalledPackageIds:
                return _("Not usable. Missing required package: {dependency}").format(
                    dependency=package.dependentVoiceId,
                )
            if package.dependentVoiceId not in self._allUsableInstalledPackageIds:
                return _("Not usable. Required package is not usable: {dependency}").format(
                    dependency=package.dependentVoiceId,
                )
        count = len(self._direct_installed_dependents(package)) if dependentCount is None else dependentCount
        if package.dependentVoiceId:
            if count:
                return _("Usable. Requires {dependency}; required by {count} installed packages").format(
                    dependency=package.dependentVoiceId,
                    count=count,
                )
            return _("Usable. Requires {dependency}").format(
                dependency=package.dependentVoiceId,
            )
        if count:
            return _("Usable. Required by {count} installed packages").format(
                count=count,
            )
        return _("Usable")

    def _download_package_status(self, package: VoicePackage, dependentCount: int | None = None) -> str:
        if not is_package_supported_by_engine(package):
            return _("Not supported by the bundled engine")
        if package.dependentVoiceId:
            if package.dependentVoiceId in self._allUsableInstalledPackageIds:
                return _("Available. Required package is installed: {dependency}").format(
                    dependency=package.dependentVoiceId,
                )
            return _("Available. Requires package: {dependency}").format(
                dependency=package.dependentVoiceId,
            )
        count = len(self._direct_download_dependents(package)) if dependentCount is None else dependentCount
        if count:
            return _("Available. Required by {count} downloadable packages").format(
                count=count,
            )
        return _("Available to download")

    def _speaker_names(self, package: VoicePackage) -> str:
        names = [str(speaker.get("name") or speaker.get("speaker") or "") for speaker in package.speakers]
        return ", ".join(name for name in names if name)

    def _format_size(self, size: int) -> str:
        return format_size_mb(size)

    def _checked_packages(self, listCtrl: wx.ListCtrl, packages: list[VoicePackage]) -> list[VoicePackage]:
        if hasattr(listCtrl, "IsItemChecked"):
            count = min(listCtrl.ItemCount, len(packages))
            return [packages[i] for i in range(count) if listCtrl.IsItemChecked(i)]
        return []

    def _with_installed_dependents(self, packages: list[VoicePackage]) -> list[VoicePackage]:
        expanded = list(packages)
        includedIds = {pkg.id for pkg in expanded}
        while True:
            added = False
            for package in self._allInstalledPackages:
                if package.id in includedIds:
                    continue
                if package.dependentVoiceId in includedIds:
                    expanded.append(package)
                    includedIds.add(package.id)
                    added = True
            if not added:
                return expanded

    def _with_required_download_dependencies(self, packages: list[VoicePackage]) -> list[VoicePackage]:
        expanded = list(packages)
        includedIds = {pkg.id for pkg in expanded}
        installedIds = {pkg.id for pkg in self._allInstalledPackages}
        catalogPackagesById = {pkg.id: pkg for pkg in self.catalog.packages}

        def add_dependency_chain(package: VoicePackage, seenIds: set[str]) -> None:
            dependencyId = package.dependentVoiceId
            if not dependencyId or dependencyId in seenIds:
                return
            seenIds.add(dependencyId)
            dependency = catalogPackagesById.get(dependencyId)
            if dependency is None or not is_package_supported_by_engine(dependency):
                return
            if dependency.id not in installedIds and dependency.id not in includedIds:
                expanded.append(dependency)
                includedIds.add(dependency.id)
            add_dependency_chain(dependency, seenIds)

        for package in list(expanded):
            add_dependency_chain(package, set())
        return expanded

    def _missing_dependency_for_package(
        self,
        package: VoicePackage,
        catalogPackagesById: dict[str, VoicePackage],
    ) -> str:
        seenIds: set[str] = set()
        dependencyId = package.dependentVoiceId
        while dependencyId:
            if dependencyId in seenIds:
                return ""
            seenIds.add(dependencyId)
            dependency = catalogPackagesById.get(dependencyId)
            if (
                dependency is None
                or not is_package_supported_by_engine(dependency)
                or not voice_store.is_package_installed(dependency)
            ):
                return dependencyId
            dependencyId = dependency.dependentVoiceId
        return ""

    def _dependency_depth(self, package: VoicePackage, packagesById: dict[str, VoicePackage]) -> int:
        depth = 0
        seen: set[str] = set()
        current = package
        while current.dependentVoiceId and current.dependentVoiceId not in seen:
            seen.add(current.id)
            parent = packagesById.get(current.dependentVoiceId)
            if parent is None:
                break
            depth += 1
            current = parent
        return depth

    def _dependents_first(self, packages: list[VoicePackage]) -> list[VoicePackage]:
        packagesById = {pkg.id: pkg for pkg in self._allInstalledPackages}
        return sorted(
            packages,
            key=lambda package: self._dependency_depth(package, packagesById),
            reverse=True,
        )

    def _dependencies_first(self, packages: list[VoicePackage]) -> list[VoicePackage]:
        packagesById = {pkg.id: pkg for pkg in self.catalog.packages}
        return sorted(
            packages,
            key=lambda package: self._dependency_depth(package, packagesById),
        )

    def _package_list_text(self, packages: list[VoicePackage]) -> str:
        return ", ".join(pkg.id for pkg in packages)

    def _usable_packages_after_removal(self, packages: list[VoicePackage]) -> list[VoicePackage]:
        removedIds = {pkg.id for pkg in packages}
        remaining = [pkg for pkg in self._allInstalledPackages if pkg.id not in removedIds]
        return voice_store.usable_installed_packages(remaining)

    def _removes_all_usable_voices(self, packages: list[VoicePackage]) -> bool:
        return bool(self._allUsableInstalledPackages) and not self._usable_packages_after_removal(packages)

    def _is_google_synth_current(self) -> bool:
        return is_google_synth_current()

    def _open_synthesizer_dialog(self) -> bool:
        return open_synthesizer_dialog(self, title=_("Google TTS Voice Manager"))

    def _confirm_yes_no(self, message: str, style: int = wx.YES_NO | wx.ICON_QUESTION) -> bool:
        answer = show_message_box(
            message,
            _("Google TTS Voice Manager"),
            style,
            dialog_parent(self),
        )
        return answer in (wx.YES, getattr(wx, "ID_YES", wx.YES))

    def _confirm_remove_last_inactive_voice(self, packages: list[VoicePackage]) -> bool:
        packageNames = self._package_list_text(packages)
        return self._confirm_yes_no(
            _(
                "You are about to remove the last usable voice package. "
                "After it is removed, Google TTS For NVDA will not have any voices available "
                "until you download another package.\n\n"
                "Packages to remove, including dependent packages: {packages}\n\n"
                "Choose Yes to remove these packages. Choose No to keep them installed."
            ).format(packages=packageNames),
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )

    def _confirm_remove_last_active_voice(self, packages: list[VoicePackage]) -> bool:
        packageNames = self._package_list_text(packages)
        return self._confirm_yes_no(
            _(
                "You are about to remove the last usable voice package. "
                "Google TTS For NVDA is currently selected as your synthesizer, so it needs "
                "at least one voice to keep speaking.\n\n"
                "Packages to remove, including dependent packages: {packages}\n\n"
                "Choose Yes to open Select Synthesizer and switch first. "
                "Choose No to keep this package installed."
            ).format(packages=packageNames),
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )

    def _schedule_remove_after_synth_switch(self, packages: list[VoicePackage]) -> None:
        self._clear_pending_remove()
        self._pendingRemoveAfterSynthSwitch = packages
        self.set_status(
            _("Waiting for you to switch away from Google TTS For NVDA before removing the last usable voice package."),
            0,
            announce=True,
        )
        if self._open_synthesizer_dialog():
            if self._pendingRemoveAfterSynthSwitch is packages:
                self._pendingRemoveTimer = wx.CallLater(500, self._remove_after_synth_switch, packages, 0)
        else:
            self._pendingRemoveAfterSynthSwitch = None

    def _remove_after_synth_switch(self, packages: list[VoicePackage], attempts: int) -> None:
        if self._pendingRemoveAfterSynthSwitch is not packages:
            return
        if self._closing or not window_is_alive(self):
            self._clear_pending_remove()
            return
        try:
            if not self.IsShown():
                self._clear_pending_remove()
                return
        except RuntimeError:
            self._clear_pending_remove()
            return

        def on_ready() -> None:
            self._pendingRemoveAfterSynthSwitch = None
            self._pendingRemoveTimer = None
            self._remove_packages(packages)

        def on_timeout() -> None:
            self._pendingRemoveAfterSynthSwitch = None
            self._pendingRemoveTimer = None
            self.set_status(
                _(
                    "The last usable voice package was kept because Google TTS For NVDA is still the current synthesizer."
                ),
                0,
                announce=True,
            )

        def retry(nextAttempts: int) -> None:
            self._pendingRemoveTimer = wx.CallLater(500, self._remove_after_synth_switch, packages, nextAttempts)

        step_after_synth_switch(
            attempts,
            isStillActive=lambda: self._pendingRemoveAfterSynthSwitch is packages,
            isGoogleSynthCurrent=self._is_google_synth_current,
            isBusy=lambda: self.isBusy,
            onReady=on_ready,
            onTimeout=on_timeout,
            retry=retry,
        )

    def _first_voice_id(self, packages: list[VoicePackage]) -> str | None:
        catalog = VoiceCatalog(packages)
        for speaker in catalog.speakers:
            if speaker.language == "en-US":
                return speaker.id
        if catalog.speakers:
            return catalog.speakers[0].id
        return None

    def _reset_configured_voice_if_removed(self, removedPackageIds: set[str]) -> str | None:
        if not removedPackageIds:
            return None
        try:
            synthConfig = config.conf["speech"][SYNTH_NAME]
            configuredVoice = str(synthConfig.get("voice") or "")
            configuredVariant = str(synthConfig.get("variant") or "")
        except Exception:
            return None
        usableCatalog = VoiceCatalog(self._allUsableInstalledPackages)
        removedPrefix = tuple(f"{packageId}:" for packageId in removedPackageIds)
        variantRemoved = configuredVariant.startswith(removedPrefix) or configuredVoice.startswith(removedPrefix)
        variantUnusable = False
        if usableCatalog.speakers and configuredVariant:
            try:
                usableCatalog.speaker_for_voice(configuredVariant)
            except KeyError:
                variantUnusable = True
        languageRemoved = False
        if usableCatalog.speakers and not configuredVariant and configuredVoice:
            configuredKey = language_utils.normalize_language(configuredVoice)
            languageRemoved = not any(
                language_utils.normalize_language(lang) == configuredKey for lang in usableCatalog.voices_by_language()
            )
        if not variantRemoved and not variantUnusable and not languageRemoved:
            return None
        fallbackVoice = self._first_voice_id(self._allUsableInstalledPackages)
        if not fallbackVoice:
            return None
        fallbackLanguage = ""
        try:
            fallbackLanguage = usableCatalog.language_for_voice(fallbackVoice)
        except Exception:
            log.debugWarning("Could not resolve fallback Google TTS voice language.", exc_info=True)
        try:
            synthConfig["variant"] = fallbackVoice
            if fallbackLanguage:
                synthConfig["voice"] = fallbackLanguage
        except Exception:
            log.debugWarning("Could not reset removed Google TTS configured voice.", exc_info=True)
            return None
        return fallbackVoice

    def _reset_auto_language_profile_variants_if_removed(self, removedPackageIds: set[str]) -> bool:
        if not removedPackageIds:
            return False
        try:
            section = config.conf[browserBridge.CONFIG_SECTION]
        except Exception:
            return False
        profiles: dict[str, Any] | None = None
        with contextlib.suppress(Exception):
            rawProfiles = section[browserBridge.CONFIG_AUTO_LANGUAGE_PROFILES]
            parsedProfiles = json.loads(str(rawProfiles or "{}"))
            if isinstance(parsedProfiles, dict):
                profiles = parsedProfiles

        removedPrefix = tuple(f"{packageId}:" for packageId in removedPackageIds)
        speakersByLanguage = VoiceCatalog(self._allUsableInstalledPackages).voices_by_language()
        speakersByNormalizedLanguage = {
            language_utils.normalize_language(lang): speakers for lang, speakers in speakersByLanguage.items()
        }
        changedCount = 0
        if profiles is not None:
            for rawLanguage, rawProfile in list(profiles.items()):
                if not isinstance(rawProfile, dict):
                    continue
                language = language_utils.normalize_language_code(str(rawLanguage or ""))
                speakers = speakersByLanguage.get(language) or speakersByNormalizedLanguage.get(
                    language_utils.normalize_language(language),
                    [],
                )
                validVoiceIds = {speaker.id for speaker in speakers}
                voiceId = str(rawProfile.get("voice") or "")
                voiceWasRemoved = voiceId.startswith(removedPrefix)
                voiceIsInvalid = bool(voiceId and voiceId not in validVoiceIds)
                if not voiceWasRemoved and not voiceIsInvalid and (speakers or not rawProfile.get("enabled", False)):
                    continue
                replacementVoice = speakers[0].id if speakers else ""
                profile = dict(rawProfile)
                updated = False
                if replacementVoice != voiceId:
                    profile["voice"] = replacementVoice
                    updated = True
                if not speakers and profile.get("enabled", False):
                    profile["enabled"] = False
                    updated = True
                if updated:
                    profiles[rawLanguage] = profile
                    changedCount += 1

        try:
            rawCandidates = str(section.get(browserBridge.CONFIG_AUTO_LANGUAGE_CANDIDATES, "") or "")
        except Exception:
            rawCandidates = ""
        parsedCandidates = language_utils.parse_language_codes(rawCandidates)
        filteredCandidates = [
            candidate
            for candidate in parsedCandidates
            if (
                speakersByLanguage.get(candidate)
                or speakersByNormalizedLanguage.get(language_utils.normalize_language(candidate))
            )
        ]
        try:
            preferred = language_utils.normalize_language_code(
                str(section.get(browserBridge.CONFIG_AUTO_LANGUAGE_PREFERRED, "") or "")
            )
        except Exception:
            preferred = ""
        preferredKey = language_utils.normalize_language(preferred)
        matchedPreferred = next(
            (
                candidate
                for candidate in filteredCandidates
                if language_utils.normalize_language(candidate) == preferredKey
            ),
            "",
        )
        newPreferred = matchedPreferred or (filteredCandidates[0] if filteredCandidates else "")
        try:
            rawDetection = section.get(browserBridge.CONFIG_AUTO_LANGUAGE_DETECTION, False)
            detectionEnabled = audio_math.coerce_bool(rawDetection, False)
        except Exception:
            detectionEnabled = False
        disableDetection = detectionEnabled and not filteredCandidates
        candidatesChanged = filteredCandidates != parsedCandidates
        preferredChanged = newPreferred != preferred
        if not changedCount and not candidatesChanged and not preferredChanged and not disableDetection:
            return False
        try:
            if changedCount and profiles is not None:
                section[browserBridge.CONFIG_AUTO_LANGUAGE_PROFILES] = json.dumps(
                    profiles,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            if candidatesChanged:
                section[browserBridge.CONFIG_AUTO_LANGUAGE_CANDIDATES] = ",".join(filteredCandidates)
            if preferredChanged:
                section[browserBridge.CONFIG_AUTO_LANGUAGE_PREFERRED] = newPreferred
            if disableDetection:
                section[browserBridge.CONFIG_AUTO_LANGUAGE_DETECTION] = False
        except Exception:
            log.debugWarning("Could not reset removed Google TTS automatic language profile variants.", exc_info=True)
            return False
        return bool(disableDetection or changedCount or candidatesChanged or preferredChanged)

    def _apply_reset_voice_to_current_synth(self, voiceId: str) -> None:
        try:
            currentSynth = synthDriverHandler.getSynth()
            if getattr(currentSynth, "name", "") != SYNTH_NAME:
                return
            try:
                language = currentSynth.catalog.language_for_voice(voiceId)
            except Exception:
                language = VoiceCatalog(self._allUsableInstalledPackages).language_for_voice(voiceId)
            if language:
                currentSynth.voice = language
            if hasattr(currentSynth, "variant"):
                currentSynth.variant = voiceId
        except Exception:
            log.debugWarning("Could not apply reset Google TTS voice to current synth.", exc_info=True)

    def _refresh_current_google_synth_catalog(self, reloadSettingsAndRing: bool = False) -> bool:
        try:
            currentSynth = synthDriverHandler.getSynth()
            if getattr(currentSynth, "name", "") != SYNTH_NAME:
                return False
            refreshed = False
            refreshCatalog = getattr(currentSynth, "refresh_installed_catalog", None)
            if callable(refreshCatalog):
                refreshed = bool(refreshCatalog())
            if reloadSettingsAndRing:
                loadSettings = getattr(currentSynth, "loadSettings", None)
                if callable(loadSettings):
                    with contextlib.suppress(Exception):
                        _call_compat(loadSettings, onlyChanged=True)
                try:
                    import globalVars

                    settingsRing = getattr(globalVars, "settingsRing", None)
                    if settingsRing is not None:
                        settingsRing.updateSupportedSettings(currentSynth)
                except Exception:
                    log.debugWarning("Could not refresh Google TTS settings ring.", exc_info=True)
            return refreshed
        except Exception:
            log.debugWarning("Could not refresh Google TTS live synth catalog after package change.", exc_info=True)
        return False

    def _warm_current_google_synth_voice(self) -> None:
        try:
            currentSynth = synthDriverHandler.getSynth()
            if getattr(currentSynth, "name", "") != SYNTH_NAME:
                return
            warmCurrentVoice = getattr(currentSynth, "_warm_current_voice_async", None)
            if callable(warmCurrentVoice):
                warmCurrentVoice()
        except Exception:
            log.debugWarning("Could not warm Google TTS voice after package change.", exc_info=True)

    def _refresh_standby_google_synth_runtime(self) -> None:
        try:
            standby.refresh_async("Google TTS voice packages changed")
        except Exception:
            log.debugWarning(
                "Could not refresh Google TTS standby browser runtime after package change.", exc_info=True
            )

    def _sync_download_select_all_checkbox(self) -> None:
        count = self.downloadList.ItemCount
        if count == 0 or not hasattr(self.downloadList, "IsItemChecked"):
            self.downloadSelectAllCheck.SetValue(False)
            return
        supportedIndices = [
            i
            for i in range(count)
            if i < len(self.downloadPackages) and is_package_supported_by_engine(self.downloadPackages[i])
        ]
        self.downloadSelectAllCheck.SetValue(
            bool(supportedIndices) and all(self.downloadList.IsItemChecked(i) for i in supportedIndices)
        )

    def _on_check_all(
        self,
        listCtrl: wx.ListCtrl,
        check: bool,
        packages: list[VoicePackage] | None = None,
        allowPackage: Callable[[VoicePackage], bool] | None = None,
    ) -> None:
        if not hasattr(listCtrl, "CheckItem"):
            return
        self._bulkChecking = True
        try:
            for i in range(listCtrl.ItemCount):
                if check and packages is not None and allowPackage is not None and i < len(packages):
                    if not allowPackage(packages[i]):
                        listCtrl.CheckItem(i, False)
                        continue
                listCtrl.CheckItem(i, check)
        finally:
            self._bulkChecking = False

    def on_installed_select_all(self, evt: wx.CommandEvent) -> None:
        """Toggle all checkboxes in the installed list to match the select-all checkbox."""
        self._on_check_all(self.installedList, evt.IsChecked())
        count = self.installedList.ItemCount
        if count > 0 and hasattr(self.installedList, "IsItemChecked"):
            self.installedSelectAllCheck.SetValue(all(self.installedList.IsItemChecked(i) for i in range(count)))

    def on_download_select_all(self, evt: wx.CommandEvent) -> None:
        """Toggle all checkboxes in the download list to match the select-all checkbox."""
        self._on_check_all(self.downloadList, evt.IsChecked(), self.downloadPackages, is_package_supported_by_engine)
        self._sync_download_select_all_checkbox()

    def _on_installed_item_check_changed(self, evt: wx.ListEvent) -> None:
        """Keep the select-all checkbox in sync when individual items are toggled."""
        if getattr(self, "_bulkChecking", False):
            evt.Skip()
            return
        count = self.installedList.ItemCount
        if count == 0 or not hasattr(self.installedList, "IsItemChecked"):
            return
        all_checked = all(self.installedList.IsItemChecked(i) for i in range(count))
        self.installedSelectAllCheck.SetValue(all_checked)
        evt.Skip()

    def _on_download_item_check_changed(self, evt: wx.ListEvent) -> None:
        """Keep the select-all checkbox in sync when individual items are toggled."""
        if getattr(self, "_bulkChecking", False):
            evt.Skip()
            return
        count = self.downloadList.ItemCount
        if count == 0 or not hasattr(self.downloadList, "IsItemChecked"):
            return
        index = evt.GetIndex()
        if (
            0 <= index < len(self.downloadPackages)
            and hasattr(self.downloadList, "CheckItem")
            and self.downloadList.IsItemChecked(index)
            and not is_package_supported_by_engine(self.downloadPackages[index])
        ):
            self.downloadList.CheckItem(index, False)
            self.set_status(
                _("This voice package is not supported by the bundled Google TTS engine."), 0, announce=True
            )
        self._sync_download_select_all_checkbox()
        evt.Skip()

    def on_download_selected(self, evt: wx.CommandEvent) -> None:
        checkedPackages = self._checked_packages(self.downloadList, self.downloadPackages)
        if not checkedPackages:
            self.set_status(_("No voice packages are checked for download."), 0, announce=True)
            return
        unsupportedPackages = [pkg for pkg in checkedPackages if not is_package_supported_by_engine(pkg)]
        packages = [pkg for pkg in checkedPackages if is_package_supported_by_engine(pkg)]
        if not packages:
            self.set_status(
                _("The checked voice packages are not supported by the bundled Google TTS engine."),
                0,
                announce=True,
            )
            return
        selectedDownloadIds = {pkg.id for pkg in packages}
        packages = self._dependencies_first(self._with_required_download_dependencies(packages))
        requiredPackages = [pkg for pkg in packages if pkg.id not in selectedDownloadIds]
        if requiredPackages:
            confirmMsg = _(
                "Download required voice packages?\n"
                "Selected: {selected}\n"
                "Also download packages required by your selection: {dependencies}"
            ).format(
                selected=self._package_list_text([pkg for pkg in packages if pkg.id in selectedDownloadIds]),
                dependencies=self._package_list_text(requiredPackages),
            )
            if not self._confirm_yes_no(confirmMsg):
                return
        totalCount = len(packages)
        catalogPackagesById = {pkg.id: pkg for pkg in self.catalog.packages}

        def work() -> dict[str, Any]:
            succeeded = 0
            succeededIds: list[str] = []
            failed: list[tuple[str, str]] = []
            lastOverall: int | None = None
            lastStatusMessage = ""
            for i, package in enumerate(packages):
                if self._closing:
                    break

                def _progress(
                    percent: int | None,
                    message: str,
                    _idx: int = i,
                    _pkgId: str = package.id,
                ) -> None:
                    nonlocal lastOverall, lastStatusMessage
                    if percent is not None:
                        overall = int((_idx * 100 + percent) / totalCount)
                        statusMessage = _(
                            "Downloading {current}/{total}: {package}, overall {percent} percent complete"
                        ).format(
                            current=_idx + 1,
                            total=totalCount,
                            package=_pkgId,
                            percent=overall,
                        )
                    else:
                        overall = None
                        statusMessage = _("Downloading {current}/{total}: {package}").format(
                            current=_idx + 1,
                            total=totalCount,
                            package=_pkgId,
                        )
                    if overall == lastOverall and statusMessage == lastStatusMessage:
                        return
                    if self._closing:
                        return
                    lastOverall = overall
                    lastStatusMessage = statusMessage
                    with contextlib.suppress(RuntimeError, AttributeError, AssertionError):
                        wx.CallAfter(
                            self.set_status,
                            statusMessage,
                            overall,
                        )

                try:
                    missingDependencyId = self._missing_dependency_for_package(package, catalogPackagesById)
                    if missingDependencyId:
                        failed.append(
                            (
                                package.id,
                                _("Missing required package: {dependency}").format(
                                    dependency=missingDependencyId,
                                ),
                            )
                        )
                        continue
                    voice_store.download_package(package, _progress)
                    succeeded += 1
                    succeededIds.append(package.id)
                except Exception as exc:
                    log.error("Failed to download %s: %s", package.id, exc, exc_info=True)
                    failed.append((package.id, self._user_friendly_error_message(exc)))
            return {"succeeded": succeeded, "succeededIds": succeededIds, "failed": failed}

        def done(result: Any | BaseException) -> None:
            self.isBusy = False
            if isinstance(result, BaseException):
                self._refresh_buttons()
                self.show_error(result)
                return
            self.refresh_lists()
            succeeded = result["succeeded"]
            failed = result["failed"]
            if failed:
                message = _(
                    "Downloaded {succeeded} of {total} packages. Could not download: {failList}. First error: {reason}"
                ).format(
                    succeeded=succeeded,
                    total=totalCount,
                    failList=", ".join(packageId for packageId, _reason in failed),
                    reason=failed[0][1],
                )
            elif succeeded == 1:
                message = _("Downloaded {package}.").format(package=packages[0].id)
            else:
                message = _("Downloaded {count} voice packages.").format(count=succeeded)
            if unsupportedPackages:
                message = _("{message} Skipped packages not supported by this engine: {packages}").format(
                    message=message,
                    packages=", ".join(package.id for package in unsupportedPackages),
                )
            succeededIds = set(result.get("succeededIds", []))
            requiredSucceededPackages = [pkg for pkg in requiredPackages if pkg.id in succeededIds]
            if requiredSucceededPackages:
                message = _("{message} Also downloaded required packages: {packages}").format(
                    message=message,
                    packages=", ".join(package.id for package in requiredSucceededPackages),
                )
            if succeededIds:
                self._refresh_current_google_synth_catalog()
                self._warm_current_google_synth_voice()
                self._refresh_standby_google_synth_runtime()
            self.set_status(message, 100, announce=True)
            self._focus_active_page()

        self._run_worker(work, done, _("Downloading voice packages..."))

    def on_remove_selected(self, evt: wx.CommandEvent) -> None:
        if self._pendingRemoveAfterSynthSwitch is not None:
            self._clear_pending_remove()
        selectedPackages = self._checked_packages(self.installedList, self.installedPackages)
        if not selectedPackages:
            self.set_status(_("No voice packages are checked for removal."), 0, announce=True)
            return
        selectedIds = {pkg.id for pkg in selectedPackages}
        packages = self._with_installed_dependents(selectedPackages)
        dependentPackages = [pkg for pkg in packages if pkg.id not in selectedIds]
        removesAllUsable = self._removes_all_usable_voices(packages)
        if removesAllUsable:
            packages = self._dependents_first(packages)
            if self._is_google_synth_current():
                if self._confirm_remove_last_active_voice(packages):
                    self._schedule_remove_after_synth_switch(packages)
                return
            if not self._confirm_remove_last_inactive_voice(packages):
                return
        else:
            if len(packages) == 1:
                confirmMsg = _("Remove voice package {package}?").format(package=packages[0].id)
            elif dependentPackages:
                selectedNames = self._package_list_text(selectedPackages)
                dependentNames = self._package_list_text(dependentPackages)
                confirmMsg = _(
                    "Remove {count} packages?\n"
                    "Selected: {selected}\n"
                    "Also remove packages that depend on your selection: {dependents}"
                ).format(
                    count=len(packages),
                    selected=selectedNames,
                    dependents=dependentNames,
                )
            else:
                packageNames = self._package_list_text(packages)
                confirmMsg = _("Remove {count} packages?\n{packages}").format(
                    count=len(packages),
                    packages=packageNames,
                )
            if not self._confirm_yes_no(confirmMsg):
                return
            packages = self._dependents_first(packages)
        self._remove_packages(packages)

    def _remove_packages(self, packages: list[VoicePackage]) -> None:
        totalCount = len(packages)

        def work() -> dict[str, Any]:
            succeeded = 0
            failed: list[tuple[str, str]] = []
            failedIds: set[str] = set()
            removedIds: list[str] = []
            for package in packages:
                if self._closing:
                    break
                blockedByDependent = next(
                    (
                        depPkg.id
                        for depPkg in packages
                        if depPkg.id in failedIds and getattr(depPkg, "dependentVoiceId", None) == package.id
                    ),
                    None,
                )
                if blockedByDependent is not None:
                    log.error(
                        "Cannot remove %s because dependent package %s could not be removed.",
                        package.id,
                        blockedByDependent,
                    )
                    depReason = next(
                        (reason for pkgId, reason in failed if pkgId == blockedByDependent),
                        _("An unexpected error occurred while managing voice packages."),
                    )
                    failedIds.add(package.id)
                    failed.append((package.id, depReason))
                    continue
                try:
                    voice_store.remove_package(package)
                    succeeded += 1
                    removedIds.append(package.id)
                except Exception as exc:
                    log.error("Failed to remove %s: %s", package.id, exc, exc_info=True)
                    failedIds.add(package.id)
                    failed.append((package.id, self._user_friendly_error_message(exc)))
            return {"succeeded": succeeded, "failed": failed, "removedIds": removedIds}

        def done(result: Any | BaseException) -> None:
            self.isBusy = False
            if isinstance(result, BaseException):
                self._refresh_buttons()
                self.show_error(result)
                return
            self.refresh_lists()
            succeeded = result["succeeded"]
            failed = result["failed"]
            removedIds = set(result.get("removedIds", []))
            resetVoice = self._reset_configured_voice_if_removed(removedIds)
            resetAutoProfiles = self._reset_auto_language_profile_variants_if_removed(removedIds)
            if removedIds:
                self._refresh_current_google_synth_catalog(reloadSettingsAndRing=resetAutoProfiles)
            if resetVoice:
                self._apply_reset_voice_to_current_synth(resetVoice)
            if removedIds or resetAutoProfiles or resetVoice:
                self._warm_current_google_synth_voice()
            if removedIds:
                self._refresh_standby_google_synth_runtime()
            if failed:
                message = _(
                    "Removed {succeeded} of {total} packages. Could not remove: {failList}. First error: {reason}"
                ).format(
                    succeeded=succeeded,
                    total=totalCount,
                    failList=", ".join(packageId for packageId, _reason in failed),
                    reason=failed[0][1],
                )
            elif succeeded == 1:
                message = _("Removed {package}.").format(package=packages[0].id)
            else:
                message = _("Removed {count} voice packages.").format(count=succeeded)
            if resetVoice:
                message = _("{message} The current variant was reset to {voice}.").format(
                    message=message,
                    voice=resetVoice,
                )
            self.set_status(message, 100, announce=True)
            self._focus_active_page()

        self._run_worker(work, done, _("Removing voice packages..."))

    def on_open_folder(self, evt: wx.CommandEvent) -> None:
        try:
            path = voice_store.voice_dir()
            os.startfile(os.fspath(path))  # type: ignore[attr-defined]
        except Exception as exc:
            self.show_error(exc)

    def _run_worker(
        self,
        work: Callable[[], Any],
        done: Callable[[Any | BaseException], None],
        busyMessage: str,
    ) -> None:
        if self.isBusy or self._closing:
            return
        self.isBusy = True
        self._lastProgressAnnouncement = 0
        with contextlib.suppress(RuntimeError):
            self.closeButton.SetFocus()
        self._refresh_buttons()
        self.set_status(busyMessage, 0, announce=True)

        def safe_done(result: object) -> None:
            if self._closing or not window_is_alive(self):
                self.isBusy = False
                return
            try:
                done(result)
            except RuntimeError:
                self.isBusy = False
                log.debug("Ignoring VoiceManagerDialog callback after dialog destruction.", exc_info=True)

        def run() -> None:
            try:
                result = work()
            except Exception as exc:
                result = exc
            with contextlib.suppress(RuntimeError, AttributeError, AssertionError):
                wx.CallAfter(safe_done, result)

        threading.Thread(target=run, name="googleTtsForNvda.voiceManager", daemon=True).start()

    def set_status(self, message: str, percent: int | None = None, announce: bool = False) -> None:
        if self._closing or not window_is_alive(self):
            return
        try:
            self.statusText.SetLabel(message)
            if percent is not None:
                value = max(0, min(100, int(percent)))
                self.progressGauge.SetValue(value)
                if 25 <= value < 100 and value // 25 > self._lastProgressAnnouncement // 25:
                    self._lastProgressAnnouncement = value
                    announce = True
            self.Layout()
        except RuntimeError:
            log.debug("Ignoring VoiceManagerDialog status update after dialog destruction.", exc_info=True)
            return
        if announce:
            ui.message(message)

    def _user_friendly_error_message(self, error: BaseException) -> str:
        if isinstance(error, PermissionError):
            return _("Could not write to the voice packages folder. Check folder permissions and try again.")
        if isinstance(error, FileNotFoundError):
            return _("The voice package file could not be found. Refresh the list and try again.")
        if isinstance(
            error,
            (
                urllib.error.URLError,
                http.client.HTTPException,
                TimeoutError,
                ConnectionError,
                socket.gaierror,
                ssl.SSLError,
            ),
        ):
            return _("Could not download the voice package. Check your internet connection and try again.")
        if isinstance(error, OSError):
            return _(
                "A file system error occurred while managing voice packages. "
                "Check the voice packages folder and try again."
            )
        voicePackageErrorCls = getattr(voice_store, "VoicePackageError", None)
        if isinstance(voicePackageErrorCls, type) and isinstance(error, voicePackageErrorCls):
            message = str(error).strip()
            if message:
                return message
        return _("An unexpected error occurred while managing voice packages.")

    def show_error(self, error: BaseException) -> None:
        message = self._user_friendly_error_message(error)
        technicalMessage = str(error).strip() or error.__class__.__name__
        log.error("Google TTS voice manager operation failed: %s", technicalMessage)
        self.set_status(_("Voice package operation failed: {message}").format(message=message), 0)
        show_message_box(message, _("Google TTS Voice Manager"), wx.OK | wx.ICON_ERROR, dialog_parent(self))

    def _refresh_buttons(self) -> None:
        hasInstalledItems = self.installedList.ItemCount > 0
        hasDownloadItems = any(is_package_supported_by_engine(package) for package in self.downloadPackages)
        for control in (
            self.refreshButton,
            self.openFolderButton,
            self.installedList,
            self.downloadList,
            self.installedLanguageCombo,
            self.downloadLanguageCombo,
        ):
            control.Enable(not self.isBusy)
        self.installedSelectAllCheck.Enable(not self.isBusy and hasInstalledItems)
        self.downloadSelectAllCheck.Enable(not self.isBusy and hasDownloadItems)
        self.removeButton.Enable(not self.isBusy and hasInstalledItems)
        self.downloadButton.Enable(not self.isBusy and hasDownloadItems)

    def prepare_for_shutdown(self) -> None:
        self._closing = True
        self._clear_pending_remove()

    def on_close(self, evt: wx.CloseEvent) -> None:
        if self.isBusy:
            canVeto = getattr(evt, "CanVeto", None)
            if callable(canVeto) and not canVeto():
                self.prepare_for_shutdown()
                self.Destroy()
                return
            evt.Veto()
            show_message_box(
                _("A voice package operation is still running."),
                _("Google TTS Voice Manager"),
                wx.OK | wx.ICON_INFORMATION,
                dialog_parent(self),
            )
            return
        self.prepare_for_shutdown()
        self.Destroy()

    def on_destroy(self, evt: wx.WindowDestroyEvent) -> None:
        if evt.GetEventObject() is self:
            self.prepare_for_shutdown()
            self._onDestroy(self)
        evt.Skip()
