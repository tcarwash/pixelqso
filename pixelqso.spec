# PyInstaller spec. Build this file on each target OS; binaries are native to
# the build host, so a Windows executable cannot be produced from Linux.
import os

include_weak_signal = os.environ.get("PIXELQSO_FREEZE_WEAK_SIGNAL") == "1"
include_data2g_host = True
bundled_datas = [(os.path.join(SPECPATH, "icon.png"), ".")]
bundled_binaries = []
bundled_hidden = []
if include_data2g_host:
    from PyInstaller.utils.hooks import collect_all
    data2g_datas, data2g_binaries, data2g_hidden = collect_all("data2g")
    bundled_datas.extend(data2g_datas)
    bundled_binaries.extend(data2g_binaries)
    bundled_hidden.extend(data2g_hidden)
hidden = [
    # Local modules are not reliably discovered by the frozen app analysis
    # when the spec is invoked from outside this folder.
    "cardmodem",
    "card_backends",
    "data2g_transport",
    "data2g_runtime",
    "card_transfer",
    "webserver",
    "PySide6.QtNetwork",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
]
if include_weak_signal:
    hidden.extend(["weak_signal_modem", "weak_signal_ldpc", "weak_signal_ldpc_data"])
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
    # Never bundle heavyweight training/GPU runtimes. The base frozen app
    # includes the supported Data2G host runtime, but excludes its optional
    # training/GPU dependencies and the optional weak-signal modules.
    excludes=["torch", "triton", "nvidia"] + ([] if include_weak_signal else [
        "weak_signal_modem", "weak_signal_ldpc", "weak_signal_ldpc_data"]),
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
