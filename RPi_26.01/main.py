import sys
import faulthandler
from PySide6.QtWidgets import QApplication
from window_main import WindowSum

def main():
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
    main()