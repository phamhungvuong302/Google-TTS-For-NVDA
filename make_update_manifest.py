#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse

ADDON_ID = "googleTtsForNvda"
BUILD_INFO_FILE_NAME = "buildInfo.json"
DEFAULT_CHANNEL = "stable"
DEFAULT_OUTPUT = "stable.json"
MAX_UPDATE_PACKAGE_BYTES = 512 * 1024 * 1024
DEFAULT_URL_TEMPLATE = (
    "https://github.com/phamhungvuong302/Google-TTS-For-NVDA/"
    "releases/download/v{version}/googleTtsForNvda-{version}.nvda-addon"
)
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]*$")
TRANSLATED_MANIFEST_RE = re.compile(r"^locale/([^/]+)/manifest\.ini$")
IGNORED_SEARCH_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "node_modules",
}


class ManifestError(RuntimeError):
    pass


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] in ('"', "'"):
        quote = value[0]
        escaped = False
        chars: list[str] = []
        for idx in range(1, len(value)):
            ch = value[idx]
            if escaped:
                chars.append(ch)
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == quote:
                trailing = value[idx + 1 :].lstrip()
                if not trailing or trailing.startswith("#"):
                    return "".join(chars).strip()
                break
            chars.append(ch)
    if "#" in value:
        return value.split("#", 1)[0].strip()
    return value


def _find_unescaped_delimiter(text: str, delimiter: str) -> int:
    idx = 0
    length = len(text)
    while idx < length:
        if text[idx] == "\\":
            idx += 2
            continue
        if text.startswith(delimiter, idx):
            return idx
        idx += 1
    return -1


def _unescape_multiline_value(value: str) -> str:
    escapes = {'"': '"', "'": "'", "\\": "\\"}
    return re.sub(r"\\(.)", lambda m: escapes.get(m.group(1), m.group(0)), value)


def _parse_manifest(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        rawLine = lines[index]
        line = rawLine.strip()
        index += 1
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, rawValue = line.split("=", 1)
        key = key.strip()
        rawValue = rawValue.strip()
        if not key:
            continue
        if rawValue.startswith(('"""', "'''")):
            delimiter = rawValue[:3]
            valuePart = rawValue[3:]
            parts: list[str] = []
            end = _find_unescaped_delimiter(valuePart, delimiter)
            if end >= 0:
                parts.append(valuePart[:end])
            else:
                parts.append(valuePart)
                while index < len(lines):
                    nextLine = lines[index]
                    index += 1
                    end = _find_unescaped_delimiter(nextLine, delimiter)
                    if end >= 0:
                        parts.append(nextLine[:end])
                        break
                    parts.append(nextLine)
                else:
                    raise ManifestError(f"Unterminated triple-quoted value for {key}.")
            values[key] = _unescape_multiline_value("\n".join(parts)).strip()
            continue
        values[key] = _unquote(rawValue)
    return values


def _read_manifest_from_archive(archive: zipfile.ZipFile, memberName: str) -> dict[str, str]:
    with archive.open(memberName, "r") as manifestFile:
        text = manifestFile.read().decode("utf-8-sig")
    return _parse_manifest(text)


def _read_addon_manifest(addonPath: Path) -> dict[str, str]:
    try:
        with zipfile.ZipFile(addonPath, "r") as archive:
            return _read_manifest_from_archive(archive, "manifest.ini")
    except KeyError as exc:
        raise ManifestError(f"{addonPath} does not contain manifest.ini.") from exc
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise ManifestError(f"Could not read {addonPath} as an NVDA add-on package: {exc}") from exc


def _json_string(data: dict[str, object], key: str, default: str | None = None) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{BUILD_INFO_FILE_NAME} has an invalid {key}.")
    return value.strip()


def _json_positive_int(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ManifestError(f"{BUILD_INFO_FILE_NAME} has an invalid {key}.")
    return value


def _read_addon_build_info_from_archive(archive: zipfile.ZipFile, version: str) -> dict[str, object]:
    try:
        rawData = archive.read(BUILD_INFO_FILE_NAME)
    except KeyError:
        return {
            "baseVersion": version,
            "displayVersion": version,
            "updateBuild": 0,
        }
    try:
        data = json.loads(rawData.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{BUILD_INFO_FILE_NAME} is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{BUILD_INFO_FILE_NAME} is not a JSON object.")
    if "schema" in data and _json_positive_int(data, "schema") != 1:
        raise ManifestError(f"{BUILD_INFO_FILE_NAME} has unsupported schema: {data.get('schema')}.")
    baseVersion = _json_string(data, "baseVersion", version)
    if baseVersion != version:
        raise ManifestError(
            f"{BUILD_INFO_FILE_NAME} baseVersion {baseVersion} must match manifest.ini version {version}."
        )
    return {
        "baseVersion": baseVersion,
        "displayVersion": _json_string(data, "displayVersion", baseVersion),
        "updateBuild": _json_positive_int(data, "updateBuild"),
    }


def _read_addon_build_info(addonPath: Path, version: str) -> dict[str, object]:
    try:
        with zipfile.ZipFile(addonPath, "r") as archive:
            return _read_addon_build_info_from_archive(archive, version)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ManifestError(f"Could not read {addonPath} as an NVDA add-on package: {exc}") from exc


def _read_release_notes_by_locale_from_archive(archive: zipfile.ZipFile) -> dict[str, str]:
    notesByLocale: dict[str, str] = {}
    for memberName in sorted(archive.namelist()):
        normalizedName = memberName.replace("\\", "/")
        match = TRANSLATED_MANIFEST_RE.match(normalizedName)
        if match is None:
            continue
        language = match.group(1).strip()
        if not language:
            continue
        try:
            manifest = _read_manifest_from_archive(archive, memberName)
        except (KeyError, UnicodeDecodeError, ManifestError) as exc:
            raise ManifestError(f"Could not read translated manifest {normalizedName}: {exc}") from exc
        changelog = str(manifest.get("changelog") or "").strip()
        if changelog:
            notesByLocale[language] = changelog
    return notesByLocale


def _read_release_notes_by_locale(addonPath: Path) -> dict[str, str]:
    try:
        with zipfile.ZipFile(addonPath, "r") as archive:
            return _read_release_notes_by_locale_from_archive(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ManifestError(f"Could not read translated manifests from {addonPath}: {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version_sort_key(
    version: str,
) -> tuple[tuple[int, ...], int, tuple[tuple[int, int | str], ...], tuple[int, ...]]:
    stripped = version.strip()
    base = stripped.split("+", 1)[0]
    mainPart, sep, prePart = base.partition("-")
    if not sep:
        match = re.match(r"^\s*v?(\d+(?:\.\d+)*)(.*)$", base, flags=re.IGNORECASE)
        if match and match.group(2).strip("._ "):
            mainPart = match.group(1)
            prePart = match.group(2).strip("._ ")
    rawNumericParts = tuple(int(part) for part in re.findall(r"\d+", mainPart))
    if not rawNumericParts:
        raise ManifestError(f"Version has no numeric parts: {version}")
    normalizedList = list(rawNumericParts)
    while len(normalizedList) > 1 and normalizedList[-1] == 0:
        normalizedList.pop()
    normalizedParts = tuple(normalizedList)
    normalizedPre = prePart.strip()
    if not normalizedPre:
        return (normalizedParts, 1, (), rawNumericParts)
    preTokens: list[tuple[int, int | str]] = []
    for token in re.findall(r"\d+|[A-Za-z]+", normalizedPre):
        if token.isdigit():
            preTokens.append((0, int(token)))
        else:
            preTokens.append((1, token.casefold()))
    return (normalizedParts, 0, tuple(preTokens), rawNumericParts)


def _iter_addon_packages(searchDir: Path) -> list[Path]:
    packages: list[Path] = []
    for path in searchDir.rglob("*.nvda-addon"):
        if any(part in IGNORED_SEARCH_DIRS for part in path.parts):
            continue
        packages.append(path)
    return sorted(packages)


def _find_addon_package(searchDir: Path, allowNameMismatch: bool = False) -> Path:
    searchDir = searchDir.resolve()
    if not searchDir.exists():
        raise ManifestError(f"Search directory was not found: {searchDir}")
    if not searchDir.is_dir():
        raise ManifestError(f"Search path is not a directory: {searchDir}")
    candidates: list[
        tuple[
            tuple[tuple[int, ...], int, tuple[tuple[int, int | str], ...], tuple[int, ...]],
            int,
            float,
            Path,
        ]
    ] = []
    mismatchedNames: list[str] = []
    for packagePath in _iter_addon_packages(searchDir):
        try:
            with zipfile.ZipFile(packagePath, "r") as archive:
                manifest = _read_manifest_from_archive(archive, "manifest.ini")
                addonId = _require_manifest_value(manifest, "name")
                version = _require_manifest_value(manifest, "version")
                if addonId != ADDON_ID or not VERSION_RE.match(version):
                    continue
                versionKey = _version_sort_key(version)
                buildInfo = _read_addon_build_info_from_archive(archive, version)
        except (ManifestError, KeyError, OSError, zipfile.BadZipFile, UnicodeDecodeError):
            continue
        expectedFileName = f"{ADDON_ID}-{version}.nvda-addon"
        if packagePath.name != expectedFileName and not allowNameMismatch:
            mismatchedNames.append(f"{packagePath} should be named {expectedFileName}")
            continue
        try:
            modifiedTime = packagePath.stat().st_mtime
        except OSError:
            modifiedTime = 0
        updateBuild = int(buildInfo["updateBuild"])  # type: ignore[call-overload]
        candidates.append((versionKey, updateBuild, modifiedTime, packagePath))
    if not candidates:
        detail = ""
        if mismatchedNames:
            detail = " Matching packages with unexpected names: " + "; ".join(mismatchedNames)
        raise ManifestError(f"No valid {ADDON_ID} .nvda-addon package was found under {searchDir}.{detail}")
    candidates.sort(key=lambda item: (item[0], item[1], item[2], str(item[3])))
    return candidates[-1][3]


def _require_manifest_value(manifest: dict[str, str], key: str) -> str:
    value = str(manifest.get(key) or "").strip()
    if not value:
        raise ManifestError(f"manifest.ini is missing {key}.")
    return value


def build_update_manifest(
    addonPath: Path,
    urlTemplate: str,
    channel: str,
    allowNameMismatch: bool = False,
) -> dict[str, object]:
    addonPath = addonPath.resolve()
    if not addonPath.is_file():
        raise ManifestError(f"Add-on package was not found: {addonPath}")
    try:
        with zipfile.ZipFile(addonPath, "r") as archive:
            try:
                manifest = _read_manifest_from_archive(archive, "manifest.ini")
            except KeyError as exc:
                raise ManifestError(f"{addonPath} does not contain manifest.ini.") from exc
            addonId = _require_manifest_value(manifest, "name")
            if addonId != ADDON_ID:
                raise ManifestError(f"Expected add-on id {ADDON_ID}, found {addonId}.")
            version = _require_manifest_value(manifest, "version")
            if not VERSION_RE.match(version):
                raise ManifestError(f"Version is not safe for a release URL: {version}")
            _version_sort_key(version)
            buildInfo = _read_addon_build_info_from_archive(archive, version)
            notesByLocale = _read_release_notes_by_locale_from_archive(archive)
    except ManifestError:
        raise
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise ManifestError(f"Could not read {addonPath} as an NVDA add-on package: {exc}") from exc
    expectedFileName = f"{ADDON_ID}-{version}.nvda-addon"
    if addonPath.name != expectedFileName and not allowNameMismatch:
        raise ManifestError(
            f"Package file name should be {expectedFileName}, found {addonPath.name}. "
            "Rename the package or pass --allow-name-mismatch."
        )
    channel = str(channel or "").strip()
    if not channel:
        raise ManifestError("Update channel cannot be empty.")
    try:
        url = urlTemplate.format(version=version, addonId=ADDON_ID, fileName=expectedFileName).strip()
    except (KeyError, IndexError, ValueError) as exc:
        raise ManifestError(f"Invalid URL template: {exc}") from exc
    parsedUrl = urlparse(url)
    if parsedUrl.scheme.lower() != "https" or not parsedUrl.netloc or any(ch.isspace() for ch in url):
        raise ManifestError(f"Formatted download URL must be a valid https:// URL, got {url!r}.")
    packageSize = addonPath.stat().st_size
    if packageSize <= 0:
        raise ManifestError(f"Add-on package is empty: {addonPath}")
    if packageSize > MAX_UPDATE_PACKAGE_BYTES:
        raise ManifestError(f"Add-on package exceeds maximum supported size ({MAX_UPDATE_PACKAGE_BYTES} bytes).")
    updateManifest: dict[str, object] = {
        "schema": 2,
        "addonId": addonId,
        "channel": channel,
        "version": version,
        "baseVersion": buildInfo["baseVersion"],
        "displayVersion": buildInfo["displayVersion"],
        "updateBuild": buildInfo["updateBuild"],
        "fileName": expectedFileName,
        "minimumNVDAVersion": _require_manifest_value(manifest, "minimumNVDAVersion"),
        "lastTestedNVDAVersion": _require_manifest_value(manifest, "lastTestedNVDAVersion"),
        "url": url,
        "sha256": _sha256(addonPath),
        "size": packageSize,
        "releaseNotes": str(manifest.get("changelog") or "").strip(),
    }
    if notesByLocale:
        updateManifest["releaseNotesByLocale"] = notesByLocale
    return updateManifest


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=("Create stable.json for the Google TTS For NVDA private updater from a built .nvda-addon package.")
    )
    parser.add_argument(
        "addon",
        nargs="?",
        type=Path,
        help=(
            "Optional path to the built .nvda-addon package. "
            "If omitted, the script auto-detects the highest-version package under --search-dir."
        ),
    )
    parser.add_argument(
        "--search-dir",
        "--dist-dir",
        dest="search_dir",
        default=Path("."),
        type=Path,
        help="Directory to scan recursively when addon is omitted. Default: current directory",
    )
    parser.add_argument(
        "--output",
        default=None,
        type=Path,
        help=(
            f"Output update manifest path. Default: {DEFAULT_OUTPUT} next to the selected .nvda-addon package. "
            "Relative paths are resolved from the package directory."
        ),
    )
    parser.add_argument(
        "--channel",
        default=DEFAULT_CHANNEL,
        help=f"Update channel written to the manifest. Default: {DEFAULT_CHANNEL}",
    )
    parser.add_argument(
        "--url-template",
        default=DEFAULT_URL_TEMPLATE,
        help=(
            "Download URL template. Available fields: {version}, {addonId}, {fileName}. "
            f"Default: {DEFAULT_URL_TEMPLATE}"
        ),
    )
    parser.add_argument(
        "--allow-name-mismatch",
        action="store_true",
        help="Do not fail if the package file name does not match googleTtsForNvda-{version}.nvda-addon.",
    )
    return parser.parse_args(argv)


def _resolve_output_path(outputPath: Path | None, addonPath: Path) -> Path:
    if outputPath is None:
        return addonPath.parent / DEFAULT_OUTPUT
    if outputPath.is_absolute():
        return outputPath.resolve()
    return (addonPath.parent / outputPath).resolve()


def _atomic_write_text(targetPath: Path, content: str) -> None:
    targetPath.parent.mkdir(parents=True, exist_ok=True)
    tempPath: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=targetPath.parent,
            prefix=f".{targetPath.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            tempPath = Path(stream.name)
            stream.write(content)
        tempPath.replace(targetPath)
        tempPath = None
    finally:
        if tempPath is not None:
            tempPath.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        addonPath = (args.addon or _find_addon_package(args.search_dir, args.allow_name_mismatch)).resolve()
        updateManifest = build_update_manifest(
            addonPath=addonPath,
            urlTemplate=args.url_template,
            channel=args.channel,
            allowNameMismatch=args.allow_name_mismatch,
        )
        outputPath = _resolve_output_path(args.output, addonPath)
        _atomic_write_text(
            outputPath,
            json.dumps(updateManifest, ensure_ascii=False, indent=2) + "\n",
        )
    except (ManifestError, OSError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"Created {outputPath}")
    print(f"Package: {addonPath.resolve()}")
    print(f"Version: {updateManifest['version']}")
    print(f"Build: {updateManifest['updateBuild']}")
    print(f"Size: {updateManifest['size']} bytes")
    print(f"SHA256: {updateManifest['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
