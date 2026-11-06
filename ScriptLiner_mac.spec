# -*- mode: python ; coding: utf-8 -*-
# macOS build - run on a Mac (or via .github/workflows/build-mac.yml).
# Unlike the Windows .spec files, this needs the extra COLLECT+BUNDLE
# steps below to produce a real double-clickable ScriptLiner.app instead
# of a bare Unix executable.

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('assets', 'assets'), ('Sample Scripts/ScriptLiner Guide.fadein', 'Sample Scripts')],
    hiddenimports=[],
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
    [],
    exclude_binaries=True,
    name='ScriptLiner',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
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
    upx=True,
    name='ScriptLiner',
)

app = BUNDLE(
    coll,
    name='ScriptLiner.app',
    icon='assets/ScriptLinerLogo_Square.icns',
    bundle_identifier='com.scriptstudio.scriptliner',
    info_plist={
        'CFBundleShortVersionString': '0.17',
        'NSHighResolutionCapable': True,
    },
)
