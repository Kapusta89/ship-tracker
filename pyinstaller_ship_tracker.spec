# -*- mode: python ; coding: utf-8 -*-
# Спецификация PyInstaller для Ship Tracker.

# Если вашей системы нет PyInstaller, установите его:
#   pip install pyinstaller

block_cipher = None

a = Analysis(
    ['ship_tracker/ship_tracker.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('ship_tracker/data', 'ship_tracker/data'),  # конфиги и база
    ],
    hiddenimports=[
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        'tkinter.simpledialog',
        'tkinter.filedialog',
        'tkinter.colorchooser',
        'tkinter.scrolledtext',
        'tkinter.font',
        'tkinter.commondialog',
        'tkinter.dialog',
        'tkinter.constants',
    ],
    hookscollected=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Ship Tracker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # окно Tk, без консоли
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join('ship_tracker', 'ship_tracker.ico') if os.path.exists(os.path.join('ship_tracker', 'ship_tracker.ico')) else None,
)
