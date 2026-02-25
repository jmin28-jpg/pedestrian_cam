import os
import sys

# PyInstaller는 실행 시 파일들을 sys._MEIPASS 경로에 압축 해제합니다.
base_dir = getattr(sys, '_MEIPASS', None)

if base_dir:
    bundle_dir = os.path.join(base_dir, 'build_bundle')
    lib_dir = os.path.join(bundle_dir, 'lib')
    gst_plugins_dir = os.path.join(bundle_dir, 'gst_plugins')
    gi_typelib_dir = os.path.join(bundle_dir, 'gi_typelib')
    bin_dir = os.path.join(bundle_dir, 'bin')

    # 1. LD_LIBRARY_PATH 설정 (번들된 라이브러리 우선)
    current_ld = os.environ.get('LD_LIBRARY_PATH', '')
    os.environ['LD_LIBRARY_PATH'] = f"{lib_dir}:{current_ld}" if current_ld else lib_dir

    # 2. GST_PLUGIN_PATH 설정 (번들된 플러그인만 사용 - 격리)
    # [Commit GST-1] 시스템 플러그인 스캔 방지 (Illegal instruction 방지)
    os.environ['GST_PLUGIN_PATH'] = gst_plugins_dir
    os.environ['GST_PLUGIN_PATH_1_0'] = gst_plugins_dir
    os.environ['GST_PLUGIN_SYSTEM_PATH'] = ""
    os.environ['GST_PLUGIN_SYSTEM_PATH_1_0'] = ""
    
    # 3. GST_PLUGIN_SCANNER 설정 (번들된 스캐너 사용)
    scanner_path = os.path.join(bin_dir, 'gst-plugin-scanner')
    os.environ['GST_PLUGIN_SCANNER'] = scanner_path

    # 4. GI_TYPELIB_PATH 설정 (번들된 Typelib 사용)
    current_gi = os.environ.get('GI_TYPELIB_PATH', '')
    os.environ['GI_TYPELIB_PATH'] = f"{gi_typelib_dir}:{current_gi}" if current_gi else gi_typelib_dir

    # 5. GST_REGISTRY 설정 (격리된 레지스트리 사용)
    # [Commit GST-1] 시스템 레지스트리와 섞이지 않도록 임시 디렉토리 내 파일 사용
    registry_path = os.path.join(base_dir, 'gst_registry.bin')
    os.environ['GST_REGISTRY'] = registry_path
    os.environ['GST_REGISTRY_1_0'] = registry_path
    
    # [Commit GST-1] 스캐너 재사용 방지 (안정성)
    os.environ['GST_REGISTRY_REUSE_PLUGIN_SCANNER'] = 'no'

    # [Commit GST-2] gstshark 폴더 생성 방지 (트레이서/덤프 관련 환경변수 초기화)
    for k in [
        "GST_TRACERS",
        "GST_DEBUG",
        "GST_DEBUG_DUMP_DOT_DIR",
        "GST_DEBUG_FILE",
        "GST_SHARK_CTF_PATH",
        "GST_SHARK_LOCATION",
        "GSTSHARK",
    ]:
        os.environ.pop(k, None)
