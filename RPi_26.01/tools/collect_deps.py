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
GI_TYPELIB_SEARCH_PATHS = [
    Path("/usr/lib/aarch64-linux-gnu/girepository-1.0"),
    Path("/usr/lib/girepository-1.0"),
]

# glibc 버전 호환성 문제를 피하기 위해 기본 시스템 라이브러리는 제외 (타겟 OS에 존재한다고 가정)
EXCLUDE_LIBS = {
    "libc.so.6", "libm.so.6", "libpthread.so.0", "libdl.so.2", "librt.so.1",
    "libresolv.so.2", "libutil.so.1", "ld-linux-aarch64.so.1", "libstdc++.so.6", "libgcc_s.so.1"
}

# [최적화] 번들링할 GStreamer 요소 목록
GST_ELEMENTS = [
  "rtspsrc",
  "udpsrc", # rtspsrc가 RTP/UDP 전송에 필요로 함
  "rtph264depay","rtph265depay",
  "h264parse","h265parse",
  "rtpjitterbuffer", # rtspsrc가 네트워크 지터/지연 관리에 사용
  "v4l2h264dec","v4l2h265dec",
  "omxh264dec","omxh265dec",
  "avdec_h264","avdec_h265",
  "decodebin",
  "queue","videorate","capsfilter","videoconvert","videoscale",
  "cairooverlay","appsink",
  "fakesink",
  "xvimagesink","glimagesink","ximagesink","autovideosink",
]

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

def build_element_to_plugin_map_from_plugins() -> dict[str, Path]:
    """
    시스템 플러그인 디렉터리의 모든 .so 파일을 검사하여
    element -> plugin_path 매핑을 구축합니다.
    """
    element_map = {}
    if not GST_PLUGIN_SYSTEM_DIR.exists():
        logger.error(f"Plugin system dir not found: {GST_PLUGIN_SYSTEM_DIR}")
        return element_map

    logger.info(f"Scanning plugins in {GST_PLUGIN_SYSTEM_DIR}...")
    
    # .so 파일 목록 가져오기
    plugin_files = list(GST_PLUGIN_SYSTEM_DIR.glob("*.so"))
    
    # 메타데이터 키 등 제외할 키워드
    metadata_keys = {
        "Name", "Description", "Filename", "Version", "License", 
        "Source module", "Binary package", "Origin URL", "Package"
    }

    for plugin_path in plugin_files:
        try:
            # 각 플러그인 파일에 대해 gst-inspect-1.0 실행
            proc = subprocess.run(
                ["gst-inspect-1.0", str(plugin_path)],
                text=True,
                capture_output=True
            )
            
            if proc.returncode != 0:
                # 실패 시 stderr 첫 줄만 로깅하고 건너뜀
                err_msg = proc.stderr.strip().split('\n')[0] if proc.stderr else "No stderr"
                # logger.debug(f"Skipping {plugin_path.name}: {err_msg}")
                continue

            # stdout 파싱
            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                
                # "Key: Value" 형태의 라인 찾기
                if ':' in line:
                    parts = line.split(':', 1)
                    key = parts[0].strip()
                    
                    # 메타데이터 키 제외
                    if key in metadata_keys:
                        continue
                    
                    # 공백이 포함된 키는 element 이름이 아닐 가능성이 높음 (예: "Total count")
                    if ' ' in key:
                        continue
                        
                    # 유효한 element 이름으로 간주하고 매핑에 추가
                    element_map[key] = plugin_path
                    
        except Exception as e:
            logger.warning(f"Error inspecting {plugin_path.name}: {e}")

    logger.info(f"Built map with {len(element_map)} elements from {len(plugin_files)} plugins.")
    return element_map

def run_gst_bundle_selftest(build_bundle_dir: Path):
    logger.info("---------------------------------------------------")
    logger.info("[SELFTEST] Verifying collected GStreamer bundle...")
    
    # 2) self-test 환경 구성
    gst_plugins_dir = build_bundle_dir / "gst_plugins"
    lib_dir = build_bundle_dir / "lib"
    bin_dir = build_bundle_dir / "bin"
    gi_typelib_dir = build_bundle_dir / "gi_typelib"
    
    env = os.environ.copy()
    env["GST_PLUGIN_PATH_1_0"] = str(gst_plugins_dir.resolve())
    env["GST_PLUGIN_PATH"] = str(gst_plugins_dir.resolve())
    env["GST_PLUGIN_SYSTEM_PATH_1_0"] = ""
    env["GST_PLUGIN_SYSTEM_PATH"] = ""
    env["GST_PLUGIN_SCANNER"] = str((bin_dir / "gst-plugin-scanner").resolve())
    env["GI_TYPELIB_PATH"] = str(gi_typelib_dir.resolve())
    env["LD_LIBRARY_PATH"] = str(lib_dir.resolve())
    
    # 3) 필수 element 목록 검사
    required = [
        "rtspsrc",
        "udpsrc",
        "rtpjitterbuffer",
        "decodebin",
        "queue",
        "videoconvert",
        "videoscale",
        "appsink",
        "cairooverlay",
        "fakesink",
    ]
    
    missing = []
    
    # 4) 각 element 검사
    for element in required:
        try:
            proc = subprocess.run(
                ["gst-inspect-1.0", element],
                env=env,
                text=True,
                capture_output=True
            )
            
            if proc.returncode == 0:
                logger.info(f"[SELFTEST] OK: {element}")
            else:
                err_msg = proc.stderr.strip().split('\n')[0] if proc.stderr else "No stderr"
                logger.error(f"[SELFTEST] MISSING: {element} (Error: {err_msg})")
                missing.append(element)
        except Exception as e:
            logger.error(f"[SELFTEST] EXEC FAIL: {element} ({e})")
            missing.append(element)
            
    # 5) 결과 처리
    if missing:
        logger.error("---------------------------------------------------")
        logger.error(f"[SELFTEST] FAILED! Missing elements: {', '.join(missing)}")
        logger.error("The collected bundle is incomplete. Build aborted.")
        sys.exit(1)
    else:
        logger.info("[SELFTEST] SUCCESS! All required elements are present.")
        logger.info("---------------------------------------------------")

def collect_deps():
    # 초기화
    if BUILD_BUNDLE_DIR.exists():
        shutil.rmtree(BUILD_BUNDLE_DIR)
    
    (BUILD_BUNDLE_DIR / "lib").mkdir(parents=True)
    (BUILD_BUNDLE_DIR / "gst_plugins").mkdir(parents=True)
    (BUILD_BUNDLE_DIR / "gi_typelib").mkdir(parents=True)
    (BUILD_BUNDLE_DIR / "bin").mkdir(parents=True)

    libs_to_process = set()

    # 1. GStreamer Plugins 수집 (코드 기반 선별)
    logger.info("Collecting GStreamer plugins based on required elements...")
    
    element_to_plugin = build_element_to_plugin_map_from_plugins()
    
    selected_plugins = set()
    for element in GST_ELEMENTS:
        plugin_path = element_to_plugin.get(element)
        if plugin_path:
            if plugin_path.exists():
                selected_plugins.add(plugin_path)
            else:
                logger.warning(f"Plugin path from map does not exist: {plugin_path}")
        else:
            logger.warning(f"Element '{element}' not mapped to any plugin.")

    logger.info(f"Found {len(selected_plugins)} unique plugins for {len(GST_ELEMENTS)} elements.")
    if selected_plugins:
        plugin_names = sorted([p.name for p in selected_plugins])
        logger.info(f"Selected plugins: {', '.join(plugin_names[:30])}{'...' if len(plugin_names) > 30 else ''}")

    has_cairo_plugin = False
    for plugin_path in selected_plugins:
        # shark/tracer 제외 규칙 적용
        if "shark" in plugin_path.name.lower() or "tracer" in plugin_path.name.lower():
            continue

        if "cairo" in plugin_path.name.lower():
            has_cairo_plugin = True
        
        dest = BUILD_BUNDLE_DIR / "gst_plugins" / plugin_path.name
        if not dest.exists():
            shutil.copy2(plugin_path, dest)
        libs_to_process.add(plugin_path)

    if not has_cairo_plugin:
        logger.warning("WARNING: cairooverlay plugin was not found among selected elements!")

    # 2. GI Typelibs 수집
    logger.info("Collecting GI Typelibs...")
    found_cairo = False
    for d in GI_TYPELIB_SEARCH_PATHS:
        if d.exists():
            logger.info(f"Scanning {d}")
            for f in d.glob("*.typelib"):
                dest = BUILD_BUNDLE_DIR / "gi_typelib" / f.name
                if not dest.exists():
                    shutil.copy2(f, dest)
                if "cairo-1.0" in f.name:
                    found_cairo = True
                    logger.info(f"Found cairo typelib: {f}")
    
    if not found_cairo:
        logger.warning("WARNING: cairo-1.0.typelib NOT FOUND! ROI overlay might fail.")

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
        is_plugin = current_lib in selected_plugins
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

    # 6) 번들 완료 후 Self-Test 수행
    run_gst_bundle_selftest(BUILD_BUNDLE_DIR)

if __name__ == "__main__":
    collect_deps()
