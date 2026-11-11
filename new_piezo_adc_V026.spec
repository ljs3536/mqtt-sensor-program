# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['new_piezo_adc_V026_mems_g_per_lsb.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['scipy.signal', 'scipy.fft', 'serial.tools.list_ports', 'pyqtgraph'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='new_piezo_adc_V026',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
