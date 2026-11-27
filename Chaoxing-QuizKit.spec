# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = []
datas += collect_data_files('docx')
datas += collect_data_files('textual')
datas += collect_data_files('reportlab')

hiddenimports = [
    'tui_app',
    'config',
    'exporter',
    'chaoxing_client',
    'lxml._elementpath',
    'lxml.etree',
    'Crypto.Cipher.DES',
    'Crypto.Util.Padding',
]
hiddenimports += collect_submodules('rich._unicode_data')
hiddenimports += collect_submodules('textual.widgets')
hiddenimports += collect_submodules('textual.drivers')
hiddenimports += collect_submodules('textual.theme')

excludes = [
    'selenium',
    'tkinter',
    'matplotlib',
    'numpy',
    'scipy',
    'pandas',
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Chaoxing-QuizKit',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
