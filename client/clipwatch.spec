# Build on Windows: pyinstaller clipwatch.spec
# PyInstaller does not cross-compile — this cannot be built from macOS.
a = Analysis(
    ["clipwatch/main.py"],
    pathex=[],
    binaries=[],
    datas=[("games.toml", ".")],
    # Only imports PyInstaller's static analysis cannot see: these two are
    # function-local in platform/windows.py. The top-level win32gui/process/con
    # are found automatically.
    hiddenimports=["win32clipboard", "windows_toasts"],
    hookspath=[],
    excludes=[],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    name="clipwatch",
    console=False,      # runs in the background; the clipboard is the signal
    upx=False,
)
