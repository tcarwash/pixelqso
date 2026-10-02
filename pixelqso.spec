from PyInstaller.utils.hooks import collect_data_files

# PyInstaller spec. Build this file on each target OS; binaries are native to
# the build host, so a Windows executable cannot be produced from Linux.
hidden = [
    # Local modules are not reliably discovered by the frozen app analysis
    # when the spec is invoked from outside this folder.
    "cardmodem",
    "card_backends",
    "weak_signal_modem",
    "PySide6.QtNetwork",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
]

data2g_data = []
try:
    import data2g
except ImportError:
    pass
else:
    hidden.append("data2g")
    data2g_data = collect_data_files("data2g")

a = Analysis(
    ["app.py"],
    pathex=[SPECPATH],
    binaries=[],
    datas=data2g_data,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PixelQSO",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if __import__("sys").platform == "darwin":
    app = BUNDLE(
        exe,
        name="PixelQSO.app",
        bundle_identifier="org.pixelqso.desktop",
        info_plist={"NSHighResolutionCapable": "True"},
    )
