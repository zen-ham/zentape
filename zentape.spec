# -*- mode: python ; coding: utf-8 -*-
# PyInstaller --onedir spec for zentape.
#   datas: GUI assets, default settings, and bin/ (engine.exe + zentape_hook.dll
#          + ffmpeg.exe). main.py chdir's to sys._MEIPASS when frozen so the
#          relative 'assets/...' + settings paths resolve; video.py finds the
#          engine/ffmpeg under _MEIPASS/bin.
from PyInstaller.utils.hooks import collect_all

datas = [('assets', 'assets'), ('bin', 'bin'), ('zen_tape_settings.json', '.')]
binaries = []
hiddenimports = [
    'win32con', 'win32gui', 'win32process', 'win32ui',
    'keyboard', 'kthread', 'comtypes',
]

# collect_all gathers data/dynlibs/submodules for packages whose built-in hooks
# may miss things: bettercam (DXGI via comtypes), soundfile (libsndfile dll),
# av (PyAV's bundled ffmpeg libs).
for pkg in ('bettercam', 'soundfile', 'av', 'comtypes'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='zentape',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed GUI app (no console window)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='zentape',
)
