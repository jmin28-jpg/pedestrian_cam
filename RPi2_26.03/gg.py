# gui.py
# 1단계: UI 레이아웃 전용
# 기능 연결 없음

import sys
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget,
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QTextEdit, QComboBox, QGridLayout,
    QTableWidget
)
from PySide6.QtCore import Qt


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("이륜차 검지 시스템")
        self.setMinimumSize(1300, 850)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.camera_tab = QWidget()
        self.search_tab = QWidget()

        self.tabs.addTab(self.camera_tab, "카메라 탭")
        self.tabs.addTab(self.search_tab, "조회 탭")

        self.init_camera_tab()
        self.init_search_tab()

    # =============================
    # 카메라 탭 UI
    # =============================
    def init_camera_tab(self):

        main_layout = QVBoxLayout()

        # -------------------------
        # 상단 카메라 설정 영역
        # -------------------------
        top_layout = QHBoxLayout()

        self.camera_select = QComboBox()
        self.camera_select.addItem("카메라 선택")

        self.ip_input = QLineEdit("카메라 IP 입력")
        self.id_input = QLineEdit("카메라 ID 입력")
        self.pw_input = QLineEdit("카메라 PW 입력")

        self.confirm_btn = QPushButton("확인")
        self.delete_btn = QPushButton("삭제")

        self.save_path_btn = QPushButton("저장 경로")
        self.save_path_display = QLineEdit()
        self.save_path_display.setReadOnly(True)

        self.start_btn = QPushButton("시작")
        self.stop_btn = QPushButton("정지")

        top_layout.addWidget(self.camera_select)
        top_layout.addWidget(self.ip_input)
        top_layout.addWidget(self.id_input)
        top_layout.addWidget(self.pw_input)
        top_layout.addWidget(self.confirm_btn)
        top_layout.addWidget(self.delete_btn)
        top_layout.addWidget(self.save_path_btn)
        top_layout.addWidget(self.save_path_display)
        top_layout.addWidget(self.start_btn)
        top_layout.addWidget(self.stop_btn)

        # -------------------------
        # 중앙 영역
        # -------------------------
        center_layout = QHBoxLayout()

        # 영상 표시 영역
        self.video_area = QLabel("영상 표시 영역")
        self.video_area.setAlignment(Qt.AlignCenter)
        self.video_area.setStyleSheet("background-color: black; color: white;")

        # 상태 패널
        status_layout = QVBoxLayout()

        self.detect_label = QLabel("검지 상태 : -")
        self.save_label = QLabel("사진 저장 : -")
        self.plate_label = QLabel("번호판 인식 : -")
        self.coord_label = QLabel("좌표 : -")

        status_layout.addWidget(self.detect_label)
        status_layout.addWidget(self.save_label)
        status_layout.addWidget(self.plate_label)
        status_layout.addWidget(self.coord_label)
        status_layout.addStretch()

        center_layout.addWidget(self.video_area, 3)
        center_layout.addLayout(status_layout, 1)

        # -------------------------
        # 하단 로그 영역
        # -------------------------
        log_title = QLabel("로그 / 이벤트 출력 영역")
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)

        # -------------------------
        # 전체 레이아웃 조립
        # -------------------------
        main_layout.addLayout(top_layout)
        main_layout.addLayout(center_layout)
        main_layout.addWidget(log_title)
        main_layout.addWidget(self.log_output)

        self.camera_tab.setLayout(main_layout)

    # =============================
    # 조회 탭 UI
    # =============================
    def init_search_tab(self):

        main_layout = QHBoxLayout()

        # -------------------------
        # 좌측 이벤트 목록
        # -------------------------
        self.event_table = QTableWidget()
        self.event_table.setColumnCount(3)
        self.event_table.setHorizontalHeaderLabels(["시간", "카메라", "이벤트ID"])

        # -------------------------
        # 중앙 영역 (차량 + 번호판)
        # -------------------------
        center_layout = QVBoxLayout()

        self.vehicle_image = QLabel("차량 사진")
        self.vehicle_image.setAlignment(Qt.AlignCenter)
        self.vehicle_image.setStyleSheet("background-color: gray;")

        self.plate_image = QLabel("번호판 사진")
        self.plate_image.setAlignment(Qt.AlignCenter)
        self.plate_image.setStyleSheet("background-color: gray;")

        center_layout.addWidget(self.vehicle_image)
        center_layout.addWidget(self.plate_image)

        # -------------------------
        # 우측 영역 (주행사진 8장 + 단속정보)
        # -------------------------
        right_layout = QVBoxLayout()

        grid_layout = QGridLayout()

        self.run_image_labels = []
        for i in range(8):
            label = QLabel(f"{i+1}")
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet("background-color: lightgray;")
            self.run_image_labels.append(label)
            grid_layout.addWidget(label, i // 4, i % 4)

        self.enforce_info = QLabel("단속 정보\n번호판:\n좌표:")
        self.enforce_info.setAlignment(Qt.AlignTop)

        right_layout.addLayout(grid_layout)
        right_layout.addWidget(self.enforce_info)

        # -------------------------
        # 전체 배치
        # -------------------------
        main_layout.addWidget(self.event_table, 1)
        main_layout.addLayout(center_layout, 2)
        main_layout.addLayout(right_layout, 2)

        self.search_tab.setLayout(main_layout)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
