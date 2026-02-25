# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

# PyInstaller가 spec을 exec로 실행할 때 __file__이 없을 수 있으므로 cwd 기준으로 계산
SPEC_DIR = Path(os.getcwd()).resolve()
PROJECT_ROOT = (SPEC_DIR / "..").resolve()

block_cipher = None

# PySide6 관련 파일 자동 수집
datas_pyside, binaries_pyside, hiddenimports_pyside = collect_all('PySide6')

# GStreamer 및 GI 관련 Hidden Imports
hiddenimports = [
    'gi', 
    'gi.repository.Gst', 
    'gi.repository.GLib', 
    'gi.repository.GObject', 
    'gi.repository.Gio',
    'gi.repository.GstVideo',
    'cairo',
] + hiddenimports_pyside

# build_bundle 폴더를 실행파일 내부에 포함
datas = [
    (str(PROJECT_ROOT / 'config.ini'), 'defaults'),
    (str(PROJECT_ROOT / 'build_bundle'), 'build_bundle'),
] + datas_pyside

# 런타임 훅 등록 (Analysis에서 설정)

# [최적화] 불필요한 PySide6 모듈 제외 목록
excludes = [
    'PySide6.QtWebEngineCore',
    'PySide6.QtWebEngineWidgets',
    'PySide6.QtWebEngineQuick',
    'PySide6.QtWebView',
    'PySide6.QtPdf',
    'PySide6.QtPdfWidgets',
    'PySide6.QtDesigner',
    'PySide6.Qt3DAnimation',
    'PySide6.Qt3DCore',
    'PySide6.Qt3DRender',
    'PySide6.Qt3DExtras',
    'PySide6.Qt3DInput',
    'PySide6.Qt3DLogic',
    'PySide6.QtTextToSpeech',
    'PySide6.QtWaylandCompositor',
    'PySide6.QtMultimedia',
    'PySide6.QtMultimediaWidgets',
    # 추가적으로 사용하지 않는 것이 확실한 모듈들
    'PySide6.QtQuick3D',
    'PySide6.QtQuick3DUtils',
    'PySide6.QtQuick3DRuntimeRender',
    'PySide6.QtQuick3DAssetImport',
    'PySide6.QtCharts',
    'PySide6.QtDataVisualization',
    'PySide6.QtSensors',
    'PySide6.QtSerialPort',
    'PySide6.QtSerialBus',
    'PySide6.QtLocation',
    'PySide6.QtPositioning',
    'PySide6.QtNfc',
    'PySide6.QtBluetooth',
    'PySide6.QtRemoteObjects',
    'PySide6.QtScxml',
    'PySide6.QtStateMachine',
    'PySide6.QtXmlPatterns',
    'PySide6.QtHelp',
    'PySide6.QtUiTools',
    'PySide6.QtTest',
    'PySide6.QtSql',
    'PySide6.QtSvgWidgets'
]

a = Analysis(
    [str(PROJECT_ROOT / 'main.py')],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries_pyside,
    datas=datas,
    hiddenimports=hiddenimports + [
        "window_main","window_ui","video_ui","cgi_client","db_module",
        "config_module","gpio_bridge","state_manager","log","log_rate_limit","app_paths"
    ],
    hookspath=[str(SPEC_DIR)],
    hooksconfig={},
    runtime_hooks=[
        str(SPEC_DIR / 'hook_opas_runtime.py') # [PKG-RUN-2] Must run last to override env vars
    ],
    excludes=excludes,
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
    name='OPAS-200',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True, # 디버깅을 위해 콘솔 표시 (배포 시 False 가능)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
