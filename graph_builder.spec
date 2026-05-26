# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for graph-builder.exe
# Build: pyinstaller graph_builder.spec

a = Analysis(
    ['graph_builder.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'tree_sitter',
        'tree_sitter_python',
        'tree_sitter_javascript',
        'tree_sitter_typescript',
        'tree_sitter_go',
        'tree_sitter_rust',
        'tree_sitter_java',
        'tree_sitter_c',
        'tree_sitter_cpp',
        'tree_sitter_c_sharp',
        'tree_sitter_ruby',
        'tree_sitter_php',
        'tree_sitter_swift',
        'tree_sitter_kotlin',
        'graphify',
        'graphify.cli',
        'code_review_graph',
        'code_review_graph.cli',
    ],
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
    name='graph-builder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)