# Build on Windows: pyinstaller clipwatch.spec
# PyInstaller does not cross-compile — this cannot be built from macOS.
a = Analysis(
    ["clipwatch/main.py"],
    pathex=[],
    binaries=[],
    datas=[("games.toml", "."), ("config.example.toml", ".")],
    hiddenimports=["win32clipboard", "win32gui", "win32process", "win32con"],
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
