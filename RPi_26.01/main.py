import sys
import os
import app_paths

def _bootstrap_runtime_defaults():
    """
    PyInstaller onefile 환경에서 실행될 때의 기본값을 설정합니다.
    - 더블클릭 실행 시 터미널 실행과 동일한 환경을 보장합니다.
    """
    # 1. onefile/frozen 환경인지 확인
    is_frozen = getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')
    if not is_frozen:
        return

    # 2. 작업 디렉터리 변경 (로그/임시파일 경로 보장)
    try:
        app_paths.ensure_dirs()
        data_root = app_paths.get_data_root()
        os.chdir(data_root)
    except Exception:
        pass # 로거 사용 전이므로 실패해도 조용히 넘어감

    # 3. Qt 오버레이 기본값 강제 (더블클릭 실행 시 cairooverlay 방지)
    os.environ.setdefault('OPAS_QT_OVERLAY', '1')

def main():
    # 지연된 임포트 (CWD 변경 후 로드하여 dist 폴더 오염 방지)
    import faulthandler
    from PySide6.QtWidgets import QApplication
    from window_main import WindowSum

    # Segfault 디버깅을 위한 핸들러 활성화
    faulthandler.enable()
    
    # QApplication 인스턴스 생성
    app = QApplication(sys.argv)
    
    # 메인 윈도우 생성 및 표시
    window = WindowSum()
    window.show()
    
    # 이벤트 루프 실행 및 종료 처리
    sys.exit(app.exec())

if __name__ == "__main__":
    # 앱 환경 기본값 설정 (onefile 실행 대응) - 모듈 로드 전 실행
    _bootstrap_runtime_defaults()
    main()