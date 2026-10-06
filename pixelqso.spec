# PyInstaller spec. Build this file on each target OS; binaries are native to
# the build host, so a Windows executable cannot be produced from Linux.
import os
import sys

platform_icon = None
if sys.platform == "win32":
    platform_icon = os.path.join(SPECPATH, "icon.ico")
elif sys.platform == "darwin":
    platform_icon = os.path.join(SPECPATH, "icon.icns")

include_data2g_host = True
bundled_datas = [(os.path.join(SPECPATH, "icon.png"), ".")]
bundled_binaries = []
bundled_hidden = []
if include_data2g_host:
    from PyInstaller.utils.hooks import collect_all
    data2g_datas, data2g_binaries, data2g_hidden = collect_all("data2g")
    pyaudio_datas, pyaudio_binaries, pyaudio_hidden = collect_all("pyaudio")
    bundled_datas.extend(data2g_datas)
    bundled_datas.extend(pyaudio_datas)
    bundled_binaries.extend(data2g_binaries)
    bundled_binaries.extend(pyaudio_binaries)
    bundled_hidden.extend(data2g_hidden)
    bundled_hidden.extend(pyaudio_hidden)
hidden = [
    # Local modules are not reliably discovered by the frozen app analysis
    # when the spec is invoked from outside this folder.
    "cardmodem",
    "card_backends",
    "data2g_transport",
    "data2g_runtime",
    "card_transfer",
    "weak_signal_modem",
    "weak_signal_ldpc",
    "weak_signal_ldpc_data",
    "experimental_burst_modem",
    "experimental_fec",
    "webserver",
    "PySide6.QtNetwork",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
]
hidden.extend(bundled_hidden)

a = Analysis(
    ["app.py"],
    pathex=[SPECPATH],
    binaries=bundled_binaries,
    datas=bundled_datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # CPU experimental modes ship in this single app and remain UI-gated.
    # Never bundle heavyweight Data2G training/GPU runtimes.
    excludes=["torch", "triton", "nvidia"],
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
    icon=platform_icon,
    codesign_identity=None,
    entitlements_file=None,
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="PixelQSO.app",
        icon=platform_icon,
        bundle_identifier="org.pixelqso.desktop",
        info_plist={"NSHighResolutionCapable": "True"},
    )
