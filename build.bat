@echo off
setlocal enabledelayedexpansion
set "EXIT_CODE=0"

echo ============================================
echo   Google TTS For NVDA - Add-on Builder
echo ============================================
echo.

cd /d "%~dp0"

:: --------------- Read version from manifest.ini ---------------
set "VERSION="
for /f "usebackq delims=" %%V in (`python -B -c "from pathlib import Path; import re; m = re.search(r'(?m)^\s*version\s*=\s*([^\r\n#]+)', Path(r'googleTtsForNvda\manifest.ini').read_text(encoding='utf-8-sig')); print(m.group(1).strip().strip(chr(34) + chr(39)) if m else '')"`) do set "VERSION=%%V"

if "!VERSION!"=="" (
    echo [ERROR] Could not read version from manifest.ini.
    set "EXIT_CODE=1"
    goto cleanup_and_exit
)
echo Version: !VERSION!
echo.

:: --------------- Clean build artifacts ---------------
echo [1/6] Cleaning build artifacts...
if exist "__pycache__" (
    rmdir /s /q "__pycache__" 2>nul
    echo       Removed __pycache__
)
for /d /r "tests" %%D in (__pycache__) do (
    if exist "%%D" (
        rmdir /s /q "%%D" 2>nul
        echo       Removed %%D
    )
)
for /d /r "googleTtsForNvda" %%D in (__pycache__) do (
    if exist "%%D" (
        rmdir /s /q "%%D" 2>nul
        echo       Removed %%D
    )
)
if exist "googleTtsForNvda\googleTtsForNvda.nvda-addon" (
    del /f /q "googleTtsForNvda\googleTtsForNvda.nvda-addon" 2>nul
    echo       Removed stale .nvda-addon from source tree.
)
if exist "googleTtsForNvda\doc" (
    del /f /s /q "googleTtsForNvda\doc\*.mo" 2>nul
)
echo       Done.
echo.

:: --------------- Merge conflict marker check ---------------
echo [2/6] Checking for unresolved merge conflict markers...
python -B -c "import pathlib, sys; exts = {'.py', '.js', '.html', '.ini', '.json', '.bat', '.sh', '.md', '.po', '.pot', '.toml', '.txt', '.yml', '.yaml'}; files = [p for d in ('googleTtsForNvda', 'tests', '.github') if pathlib.Path(d).is_dir() for p in sorted(pathlib.Path(d).rglob('*')) if p.is_file() and p.suffix.lower() in exts] + [p for p in sorted(pathlib.Path('.').iterdir()) if p.is_file() and p.suffix.lower() in exts]; hits = [(p, i, s) for p in files for i, s in enumerate(p.read_text(encoding='utf-8', errors='replace').splitlines(), 1) if s in ('<<<<<<<', '|||||||', '>>>>>>>') or s.startswith(('<<<<<<< ', '||||||| ', '>>>>>>> ')) or (s == '=======' and p.suffix.lower() != '.md')]; [print(f'      [ERROR] {p}:{i}: {s.strip()}') for p, i, s in hits]; sys.exit(1 if hits else 0)"
if errorlevel 1 (
    echo [ERROR] Unresolved merge conflict markers found.
    set "EXIT_CODE=1"
    goto cleanup_and_exit
)
echo       Passed.
echo.

:: --------------- Python syntax check ---------------
echo [3/6] Checking Python syntax...
python -B -c "import pathlib; [compile(p.read_bytes(), str(p), 'exec') for p in sorted(pathlib.Path('googleTtsForNvda').rglob('*.py'))]"
if errorlevel 1 (
    echo [ERROR] Python syntax check failed.
    set "EXIT_CODE=1"
    goto cleanup_and_exit
)
echo       Passed.
echo.

:: --------------- JavaScript syntax check ---------------
echo [4/6] Checking JavaScript syntax...
node --check googleTtsForNvda\synthDrivers\googleTtsForNvda\web\bridgeHarness.js
if errorlevel 1 (
    echo [ERROR] JavaScript syntax check failed.
    set "EXIT_CODE=1"
    goto cleanup_and_exit
)
echo       Passed.
echo.

:: --------------- Verify no .zvoice in source ---------------
echo [5/6] Verifying no .zvoice files in source tree...
set "FOUND_ZVOICE=0"
for /r "googleTtsForNvda" %%F in (*.zvoice) do (
    echo       [ERROR] Found .zvoice file: %%F
    set "FOUND_ZVOICE=1"
)
if "!FOUND_ZVOICE!"=="1" (
    echo [ERROR] Voice data files must not be in the source tree.
    set "EXIT_CODE=1"
    goto cleanup_and_exit
)
echo       Clean - no .zvoice files found.
echo.

:: --------------- Package the add-on ---------------
set "OUTPUT=dist\googleTtsForNvda-!VERSION!.nvda-addon"
echo [6/6] Packaging add-on to !OUTPUT! ...

if not exist "dist" mkdir dist

:: Remove old build with same version if present
if exist "!OUTPUT!" del /f /q "!OUTPUT!"

:: Package the contents of googleTtsForNvda/ at the archive root (manifest.ini
:: must land at the top level) using Python's standard zipfile with DEFLATE
:: level 9 compression, excluding dotfiles/dotdirs and transient build junk.
python -B -c "import os, pathlib, sys, zipfile; root = pathlib.Path('googleTtsForNvda'); out = pathlib.Path(sys.argv[1]); tmp = out.with_name(f'.{out.name}.tmp'); skip_names = {'thumbs.db', 'ehthumbs.db', 'desktop.ini'}; skip_suffixes = {'.pyc', '.pyo', '.zvoice', '.pot', '.nvda-addon', '.tmp'}; doc_skip = {'.mo', '.po', '.md'}; tmp.unlink(missing_ok=True); exec('try:\n with zipfile.ZipFile(tmp, \"w\", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:\n  for p in sorted(root.rglob(\"*\")):\n   if not p.is_file(): continue\n   rel = p.relative_to(root)\n   if any(part.startswith(\".\") or part == \"__pycache__\" for part in rel.parts): continue\n   if p.suffix.lower() in skip_suffixes or p.name.lower() in skip_names or p.name.endswith(\"~\"): continue\n   if len(rel.parts) > 1 and rel.parts[0] == \"doc\" and p.suffix.lower() in doc_skip: continue\n   zf.write(p, rel.as_posix())\n os.replace(tmp, out)\nexcept BaseException:\n tmp.unlink(missing_ok=True)\n raise')" "!OUTPUT!"
if errorlevel 1 (
    echo [ERROR] Packaging failed.
    set "EXIT_CODE=1"
    goto cleanup_and_exit
)

:: Show file size
for %%A in ("!OUTPUT!") do (
    set "SIZE=%%~zA"
)
echo       Created: !OUTPUT!
echo       Size:    !SIZE! bytes
echo.

echo ============================================
echo   Build complete: !OUTPUT!
echo ============================================
set "EXIT_CODE=0"
goto cleanup_and_exit

:cleanup_and_exit
echo.
echo Cleaning __pycache__ before exit...
if exist "__pycache__" rmdir /s /q "__pycache__" 2>nul
for /d /r "tests" %%D in (__pycache__) do (
    if exist "%%D" rmdir /s /q "%%D" 2>nul
)
for /d /r "googleTtsForNvda" %%D in (__pycache__) do (
    if exist "%%D" rmdir /s /q "%%D" 2>nul
)
exit /b !EXIT_CODE!
