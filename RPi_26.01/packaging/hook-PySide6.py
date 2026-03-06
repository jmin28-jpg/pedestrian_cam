from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
)

# PySide6에서 제외할 플러그인/데이터 패턴 목록
# opas200.spec에 있던 필터링 로직을 훅으로 이동하여 중복 수집 및 필터링 우회를 방지합니다.
BAD_PYSIDE_PATTERNS = [
    "QtWebView",
    "WebEngine",          # QtWebEngine 폴더 및 libQt6WebEngine* 라이브러리 모두 포함
    "libQt6WebEngine",
    "QtDesigner",
    "QtQml",
    "QtQuick",
    "QtWayland",
    "QtMultimedia", # FFmpeg 의존성 경고 유발
    "QtTextToSpeech",
    "plugins/imageformats/libqwebp.so",   # WebP 이미지 포맷 플러그인 (libwebp.so.6 의존성 유발)
    "plugins/imageformats/libqtiff.so",   # TIFF 이미지 포맷 플러그인 (libtiff.so.5 의존성 유발)
    "plugins/designer",
    "plugins/multimedia",
    "plugins/wayland",
    "plugins/qml",
    "plugins/texttospeech",
]

def filter_collected_files(collected_files, bad_patterns):
    """주어진 패턴을 포함하는 파일을 수집 목록에서 제거합니다."""
    kept_files = []
    for item in collected_files:
        # item은 튜플(src, dest) 또는 문자열(src)일 수 있음
        src_path = item[0] if isinstance(item, tuple) else item
        is_bad = any(pattern in src_path.replace("\\", "/") for pattern in bad_patterns)
        if not is_bad:
            kept_files.append(item)
    return kept_files

# datas/binaries는 import를 유발하지 않는 수집 방식 사용
datas = collect_data_files("PySide6")
binaries = collect_dynamic_libs("PySide6")

# [최적화] hiddenimports 과수집 중단. 실제 사용하는 모듈만 명시적으로 포함.
hiddenimports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "shiboken6",
]

# 수집된 datas와 binaries에서 불필요한 파일 필터링
datas = filter_collected_files(datas, BAD_PYSIDE_PATTERNS)
binaries = filter_collected_files(binaries, BAD_PYSIDE_PATTERNS)

# hiddenimports는 필터링하지 않음.
# 필터링된 결과는 PyInstaller에 의해 자동으로 사용됩니다.