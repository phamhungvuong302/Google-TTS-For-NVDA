"""Unit tests for make_update_manifest.py."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import make_update_manifest


def _write_addon_archive(
    archivePath: Path,
    manifestText: str | None = None,
    *,
    buildInfo: dict[str, object] | None = None,
    localeManifests: dict[str, str] | None = None,
) -> Path:
    archivePath.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archivePath, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if manifestText is not None:
            archive.writestr("manifest.ini", manifestText)
        if buildInfo is not None:
            archive.writestr(make_update_manifest.BUILD_INFO_FILE_NAME, json.dumps(buildInfo))
        for locale, text in (localeManifests or {}).items():
            archive.writestr(f"locale/{locale}/manifest.ini", text)
    return archivePath


def _sample_manifest(
    *,
    name: str = make_update_manifest.ADDON_ID,
    version: str = "1.2",
    minimumNVDAVersion: str = "2024.1",
    lastTestedNVDAVersion: str = "2026.2",
    changelog: str = '"""Initial release."""',
) -> str:
    return (
        f"name = {name}\n"
        f"version = {version}\n"
        f"minimumNVDAVersion = {minimumNVDAVersion}\n"
        f"lastTestedNVDAVersion = {lastTestedNVDAVersion}\n"
        f"changelog = {changelog}\n"
    )


class ParseManifestTests(unittest.TestCase):
    def test_parses_single_line_quoted_and_inline_comment_values(self) -> None:
        parsed = make_update_manifest._parse_manifest(
            'name = "googleTtsForNvda" # trailing comment\n'
            "version = 1.2 # comment\n"
            "summary = 'Google TTS'\n"
            'quoted = "a \\"b\\" c"\n'
        )
        self.assertEqual("googleTtsForNvda", parsed["name"])
        self.assertEqual("1.2", parsed["version"])
        self.assertEqual("Google TTS", parsed["summary"])
        self.assertEqual('a "b" c', parsed["quoted"])

    def test_parses_multiline_with_escaped_triple_quotes(self) -> None:
        parsed = make_update_manifest._parse_manifest(
            'changelog = """Line 1\nSay \\"\\"\\"hello\\"\\"\\" here\nLine 3"""\n'
        )
        self.assertEqual('Line 1\nSay """hello""" here\nLine 3', parsed["changelog"])

    def test_unterminated_multiline_raises(self) -> None:
        with self.assertRaises(make_update_manifest.ManifestError):
            make_update_manifest._parse_manifest('changelog = """unclosed\nline 2\n')


class VersionSortKeyTests(unittest.TestCase):
    def test_orders_versions_and_prereleases(self) -> None:
        key = make_update_manifest._version_sort_key
        self.assertLess(key("1.2"), key("1.10"))
        self.assertLess(key("1.2-beta.1"), key("1.2"))
        self.assertLess(key("1.2-alpha.1"), key("1.2-beta.1"))
        self.assertEqual(key("1.2")[0], key("1.2.0")[0])

    def test_rejects_non_numeric_version(self) -> None:
        with self.assertRaises(make_update_manifest.ManifestError):
            make_update_manifest._version_sort_key("alpha")


class FindAddonPackageTests(unittest.TestCase):
    def test_prefers_higher_version_then_higher_update_build(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dirA = root / "a"
            dirB = root / "b"
            pkgOld = _write_addon_archive(
                dirA / "googleTtsForNvda-1.1.nvda-addon",
                _sample_manifest(version="1.1"),
                buildInfo={"schema": 1, "baseVersion": "1.1", "updateBuild": 5},
            )
            pkgBuild1 = _write_addon_archive(
                dirA / "googleTtsForNvda-1.2.nvda-addon",
                _sample_manifest(version="1.2"),
                buildInfo={"schema": 1, "baseVersion": "1.2", "updateBuild": 1},
            )
            pkgBuild3 = _write_addon_archive(
                dirB / "googleTtsForNvda-1.2.nvda-addon",
                _sample_manifest(version="1.2"),
                buildInfo={"schema": 1, "baseVersion": "1.2", "updateBuild": 3},
            )
            self.assertNotEqual(pkgOld, pkgBuild1)
            selected = make_update_manifest._find_addon_package(root)
            self.assertEqual(pkgBuild3.resolve(), selected.resolve())

    def test_rejects_mismatched_filename_unless_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pkg = _write_addon_archive(root / "custom-name.nvda-addon", _sample_manifest(version="1.2"))
            with self.assertRaises(make_update_manifest.ManifestError):
                make_update_manifest._find_addon_package(root, allowNameMismatch=False)
            self.assertEqual(pkg.resolve(), make_update_manifest._find_addon_package(root, allowNameMismatch=True))


class BuildUpdateManifestTests(unittest.TestCase):
    def test_builds_manifest_with_localized_release_notes_and_build_info(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pkg = _write_addon_archive(
                Path(td) / "googleTtsForNvda-1.2.nvda-addon",
                _sample_manifest(version="1.2", changelog='"""English notes"""'),
                buildInfo={"schema": 1, "baseVersion": "1.2", "displayVersion": "1.2.1", "updateBuild": 2},
                localeManifests={
                    "vi": 'changelog = """Ghi chú tiếng Việt"""\n',
                    "zh_CN": 'changelog = """中文说明"""\n',
                },
            )
            manifest = make_update_manifest.build_update_manifest(
                pkg,
                make_update_manifest.DEFAULT_URL_TEMPLATE,
                make_update_manifest.DEFAULT_CHANNEL,
            )
            self.assertEqual(2, manifest["schema"])
            self.assertEqual(make_update_manifest.ADDON_ID, manifest["addonId"])
            self.assertEqual("1.2", manifest["version"])
            self.assertEqual("1.2.1", manifest["displayVersion"])
            self.assertEqual(2, manifest["updateBuild"])
            self.assertEqual("English notes", manifest["releaseNotes"])
            self.assertEqual(
                {"vi": "Ghi chú tiếng Việt", "zh_CN": "中文说明"},
                manifest["releaseNotesByLocale"],
            )
            self.assertEqual(hashlib.sha256(pkg.read_bytes()).hexdigest(), manifest["sha256"])
            self.assertEqual(pkg.stat().st_size, manifest["size"])

    def test_validation_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            missingManifest = _write_addon_archive(root / "googleTtsForNvda-1.2.nvda-addon", None)
            with self.assertRaises(make_update_manifest.ManifestError):
                make_update_manifest.build_update_manifest(
                    missingManifest, make_update_manifest.DEFAULT_URL_TEMPLATE, "stable"
                )

            wrongId = _write_addon_archive(
                root / "wrong.nvda-addon",
                _sample_manifest(name="otherAddon", version="1.2"),
            )
            with self.assertRaises(make_update_manifest.ManifestError):
                make_update_manifest.build_update_manifest(
                    wrongId, make_update_manifest.DEFAULT_URL_TEMPLATE, "stable", allowNameMismatch=True
                )

            validPkg = _write_addon_archive(
                root / "googleTtsForNvda-1.3.nvda-addon",
                _sample_manifest(version="1.3"),
            )
            for badUrl in (
                "http://example.com/{fileName}",
                "https://example.com/has space/{fileName}",
                "https://example.com/{unknownField}",
            ):
                with self.subTest(badUrl=badUrl), self.assertRaises(make_update_manifest.ManifestError):
                    make_update_manifest.build_update_manifest(validPkg, badUrl, "stable")

            with (
                mock.patch.object(make_update_manifest, "MAX_UPDATE_PACKAGE_BYTES", 8),
                self.assertRaises(make_update_manifest.ManifestError),
            ):
                make_update_manifest.build_update_manifest(
                    validPkg, make_update_manifest.DEFAULT_URL_TEMPLATE, "stable"
                )

    def test_main_writes_manifest_and_handles_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pkg = _write_addon_archive(
                root / "googleTtsForNvda-1.4.nvda-addon",
                _sample_manifest(version="1.4"),
            )
            out = root / "stable.json"
            with mock.patch("builtins.print"):
                rc = make_update_manifest.main([str(pkg), "--output", str(out)])
            self.assertEqual(0, rc)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual("1.4", data["version"])

            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                rcErr = make_update_manifest.main([str(root / "missing.nvda-addon")])
            self.assertEqual(1, rcErr)
            self.assertIn("[ERROR]", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
