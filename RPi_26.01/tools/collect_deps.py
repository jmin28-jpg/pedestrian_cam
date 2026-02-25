import os
import sys
import shutil
import subprocess
import re
from pathlib import Path

# 설정: 수집할 경로 및 제외할 시스템 라이브러리
BUILD_BUNDLE_DIR = Path("build_bundle")
SYSTEM_LIB_DIRS = [
    "/usr/lib/aarch64-linux-gnu",
    "/lib/aarch64-linux-gnu",
    "/usr/lib",
    "/lib"
]
GST_PLUGIN_SYSTEM_DIR = Path("/usr/lib/aarch64-linux-gnu/gstreamer-1.0")
GI_TYPELIB_SYSTEM_DIR = Path("/usr/lib/aarch64-linux-gnu/girepository-1.0")

# glibc 버전 호환성 문제를 피하기 위해 기본 시스템 라이브러리는 제외 (타겟 OS에 존재한다고 가정)
EXCLUDE_LIBS = {
    "libc.so.6", "libm.so.6", "libpthread.so.0", "libdl.so.2", "librt.so.1",
    "libresolv.so.2", "libutil.so.1", "ld-linux-aarch64.so.1", "libstdc++.so.6", "libgcc_s.so.1"
}

def get_logger():
    import logging
    logging.basicConfig(level=logging.INFO, format='[Deps] %(message)s')
    return logging.getLogger("Deps")

logger = get_logger()

def find_library_path(lib_name):
    """라이브러리 이름으로 시스템 경로를 검색"""
    for d in SYSTEM_LIB_DIRS:
        p = Path(d) / lib_name
        if p.exists():
            return p
        # 버전 번호가 붙은 파일 검색 (예: libname.so.0)
        candidates = list(Path(d).glob(f"{lib_name}*"))
        if candidates:
            # 가장 짧은 이름(심볼릭 링크 등) 우선, 혹은 정렬
            candidates.sort(key=lambda x: len(str(x)))
            return candidates[0]
    return None

def get_dependencies(lib_path):
    """ldd를 사용하여 의존성 라이브러리 목록 추출"""
    deps = set()
    try:
        output = subprocess.check_output(["ldd", str(lib_path)], text=True)
        for line in output.splitlines():
            line = line.strip()
            # Match: libname.so => /path/to/libname.so (0x...)
            m = re.search(r'(.+?) => (.+) \(0x', line)
            if m:
                name, path = m.groups()
                if path and path != "not found":
                    deps.add(Path(path))
            else:
                # Match: /path/to/libname.so (0x...) (e.g. ld-linux)
                m2 = re.search(r'(.+) \(0x', line)
                if m2 and os.path.isabs(m2.group(1)):
                    deps.add(Path(m2.group(1)))
    except Exception as e:
        logger.warning(f"ldd failed for {lib_path}: {e}")
    return deps

def collect_deps():
    # 초기화
    if BUILD_BUNDLE_DIR.exists():
        shutil.rmtree(BUILD_BUNDLE_DIR)
    
    (BUILD_BUNDLE_DIR / "lib").mkdir(parents=True)
    (BUILD_BUNDLE_DIR / "gst_plugins").mkdir(parents=True)
    (BUILD_BUNDLE_DIR / "gi_typelib").mkdir(parents=True)
    (BUILD_BUNDLE_DIR / "bin").mkdir(parents=True)

    libs_to_process = set()

    # 1. GStreamer Plugins 수집
    logger.info(f"Collecting GStreamer plugins from {GST_PLUGIN_SYSTEM_DIR}")
    if GST_PLUGIN_SYSTEM_DIR.exists():
        for f in GST_PLUGIN_SYSTEM_DIR.glob("*.so"):
            # [Commit GST-FIX-REMOVE] Exclude gstshark/tracer plugins
            if "shark" in f.name.lower() or "tracer" in f.name.lower():
                continue
            
            dest = BUILD_BUNDLE_DIR / "gst_plugins" / f.name
            shutil.copy2(f, dest)
            libs_to_process.add(f)

    # 2. GI Typelibs 수집
    logger.info(f"Collecting GI Typelibs from {GI_TYPELIB_SYSTEM_DIR}")
    if GI_TYPELIB_SYSTEM_DIR.exists():
        for f in GI_TYPELIB_SYSTEM_DIR.glob("*.typelib"):
            dest = BUILD_BUNDLE_DIR / "gi_typelib" / f.name
            shutil.copy2(f, dest)

    # 3. gst-plugin-scanner 바이너리 찾기 및 복사
    scanner_path = None
    possible_paths = [
        Path("/usr/lib/aarch64-linux-gnu/gstreamer1.0/gstreamer-1.0/gst-plugin-scanner"),
        Path("/usr/lib/aarch64-linux-gnu/gstreamer-1.0/gst-plugin-scanner"),
        Path("/usr/libexec/gstreamer-1.0/gst-plugin-scanner")
    ]
    for p in possible_paths:
        if p.exists():
            scanner_path = p
            break
    
    if scanner_path:
        logger.info(f"Found gst-plugin-scanner: {scanner_path}")
        dest = BUILD_BUNDLE_DIR / "bin" / "gst-plugin-scanner"
        shutil.copy2(scanner_path, dest)
        libs_to_process.add(scanner_path)
    else:
        logger.warning("gst-plugin-scanner not found! Video might fail.")

    # 4. Core GStreamer/GLib 라이브러리 추가
    core_libs = [
        "libgstreamer-1.0.so.0", "libgstbase-1.0.so.0", "libgstvideo-1.0.so.0",
        "libgstapp-1.0.so.0", "libgstpbutils-1.0.so.0", "libgobject-2.0.so.0",
        "libglib-2.0.so.0", "libgio-2.0.so.0", "libgmodule-2.0.so.0", 
        "libgirepository-1.0.so.1", "libcairo.so.2", "libcairo-gobject.so.2"
    ]
    
    for lib_name in core_libs:
        p = find_library_path(lib_name)
        if p:
            libs_to_process.add(p)
        else:
            logger.warning(f"Core lib not found: {lib_name}")

    # 5. 의존성 재귀적 처리 (ldd)
    logger.info("Processing dependencies...")
    processed_libs = set()
    
    while libs_to_process:
        current_lib = libs_to_process.pop()
        if current_lib in processed_libs:
            continue
        
        processed_libs.add(current_lib)
        
        # 플러그인이나 스캐너 자체가 아닌 경우, lib 폴더로 복사
        is_plugin = current_lib.parent == GST_PLUGIN_SYSTEM_DIR
        is_scanner = current_lib == scanner_path
        
        if not is_plugin and not is_scanner:
            if current_lib.name in EXCLUDE_LIBS:
                continue
            
            dest = BUILD_BUNDLE_DIR / "lib" / current_lib.name
            if not dest.exists():
                # 심볼릭 링크인 경우 원본 내용을 복사 (follow_symlinks=True 기본값)
                shutil.copy2(current_lib, dest)

        # 의존성 찾기
        deps = get_dependencies(current_lib)
        for dep in deps:
            if dep not in processed_libs and dep.name not in EXCLUDE_LIBS:
                libs_to_process.add(dep)

    size_mb = sum(f.stat().st_size for f in BUILD_BUNDLE_DIR.rglob('*') if f.is_file()) / 1024 / 1024
    logger.info(f"Collection complete. Bundle size: {size_mb:.2f} MB")

if __name__ == "__main__":
    collect_deps()
