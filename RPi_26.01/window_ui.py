from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, 
                               QLabel, QPushButton, QListWidget, QListWidgetItem, QStatusBar, QFrame, 
                               QTabWidget, QGroupBox, QFormLayout, QLineEdit, QTextEdit, 
                               QProgressBar, QCheckBox, QMessageBox, QSizePolicy)
from PySide6.QtCore import Qt, Signal, QSize

RIGHT_PANEL_WIDTH = 380

# ─────────────────────────────────────────────────────────────────────────────
#  PROFESSIONAL MONOCHROME  ·  Light Theme
#  배경은 순백에 가까운 화이트, 계층은 명도 차이로만 구분,
#  강조는 진한 차콜/블랙으로 처리 — 잉크처럼 명료한 인터페이스
# ─────────────────────────────────────────────────────────────────────────────
PROFESSIONAL_QSS = """
/* ═══════════════════════════════════════════════════════════════════════════
   OPAS-200  ·  Professional Dashboard UI
   ─────────────────────────────────────────────────────────────────────────
   Palette
     BG_PAGE    #F0F2F5   페이지 배경 (연한 블루그레이)
     BG_SURFACE #FFFFFF   카드·패널 표면
     BG_SUBTLE  #F7F9FC   섹션 내 보조 배경
     NAVY       #1E3A5F   주요 강조 (네이비)
     NAVY_LT    #2E5F9E   보조 강조 (밝은 네이비)
     NAVY_PALE  #EBF1FB   선택/호버 강조 배경
     BORDER     #DDE3EC   기본 테두리
     BORDER_STR #B0BDD0   진한 테두리
     TEXT_H     #0D1B2A   헤딩 텍스트
     TEXT_B     #2D3748   본문 텍스트
     TEXT_S     #64748B   보조 텍스트
     TEXT_D     #A0AEC0   비활성 텍스트
     SUCCESS    #1A7F4B   연결됨
     DANGER     #C0392B   오류·경고
     WARN_BG    #FFF8E1   경고 배경
═══════════════════════════════════════════════════════════════════════════ */

/* ── 기반 ─────────────────────────────────────────────────────────────── */
QMainWindow, QDialog {
    background-color: #F0F2F5;
}
QWidget {
    background-color: #F0F2F5;
    color: #2D3748;
    font-family: "Segoe UI", "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
    font-size: 9pt;
}

/* ── 탭 바 ────────────────────────────────────────────────────────────── */
QTabWidget::pane {
    background-color: #F0F2F5;
    border: none;
}
QTabBar {
    background-color: transparent;
}
QTabBar::tab {
    background-color: transparent;
    color: #64748B;
    border: none;
    border-bottom: 3px solid transparent;
    padding: 10px 24px;
    font-size: 9pt;
    font-weight: 600;
    margin-right: 4px;
}
QTabBar::tab:selected {
    color: #1E3A5F;
    border-bottom: 3px solid #1E3A5F;
    background-color: transparent;
}
QTabBar::tab:hover:!selected {
    color: #2D3748;
    border-bottom: 3px solid #B0BDD0;
}

/* ── 그룹박스 (카드) ──────────────────────────────────────────────────── */
QGroupBox {
    background-color: #FFFFFF;
    border: 1px solid #DDE3EC;
    border-radius: 8px;
    margin-top: 20px;
    padding: 12px 10px 10px 10px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    top: -1px;
    padding: 0 6px;
    color: #64748B;
    background-color: #FFFFFF;
    font-size: 7.5pt;
    font-weight: 700;
    letter-spacing: 0.8px;
    text-transform: uppercase;
}

/* ── 레이블 ───────────────────────────────────────────────────────────── */
QLabel {
    color: #2D3748;
    background-color: transparent;
}

/* ── 입력 필드 ────────────────────────────────────────────────────────── */
QLineEdit {
    background-color: #FFFFFF;
    color: #0D1B2A;
    border: 1.5px solid #DDE3EC;
    border-radius: 6px;
    padding: 5px 10px;
    font-size: 9pt;
    min-height: 20px;
    selection-background-color: #1E3A5F;
    selection-color: #FFFFFF;
}
QLineEdit:focus {
    border: 1.5px solid #2E5F9E;
    background-color: #FAFCFF;
}
QLineEdit:read-only {
    background-color: #F7F9FC;
    color: #64748B;
    border: 1.5px solid #EEF2F7;
}
QLineEdit::placeholder {
    color: #A0AEC0;
}

QTextEdit {
    background-color: #F7F9FC;
    color: #2D3748;
    border: 1px solid #DDE3EC;
    border-radius: 6px;
    padding: 6px 8px;
    font-size: 8.5pt;
    font-family: "Consolas", "D2Coding", "Courier New", monospace;
    line-height: 1.5;
    selection-background-color: #1E3A5F;
    selection-color: #FFFFFF;
}
QTextEdit:focus {
    border: 1.5px solid #2E5F9E;
}

/* ── 버튼 ─────────────────────────────────────────────────────────────── */
QPushButton {
    background-color: #FFFFFF;
    color: #2D3748;
    border: 1.5px solid #DDE3EC;
    border-radius: 6px;
    padding: 6px 16px;
    font-size: 9pt;
    font-weight: 600;
    min-height: 20px;
}
QPushButton:hover {
    background-color: #F0F2F5;
    border-color: #B0BDD0;
    color: #0D1B2A;
}
QPushButton:pressed {
    background-color: #E8EDF5;
    border-color: #2E5F9E;
    color: #1E3A5F;
}
QPushButton:disabled {
    background-color: #F7F9FC;
    color: #A0AEC0;
    border-color: #EEF2F7;
}

/* 주요 액션 — 모니터링 시작 */
QPushButton#btn_mon {
    background-color: #1E3A5F;
    color: #FFFFFF;
    border: 1.5px solid #1E3A5F;
    border-radius: 6px;
}
QPushButton#btn_mon:hover {
    background-color: #2E5F9E;
    border-color: #2E5F9E;
}
QPushButton#btn_mon:pressed {
    background-color: #152D4A;
}

/* 위험 버튼 — Stop */
QPushButton#btn_stop {
    background-color: #FFFFFF;
    color: #C0392B;
    border: 1.5px solid #E8A09A;
    border-radius: 6px;
    font-weight: 700;
    font-size: 9.5pt;
}
QPushButton#btn_stop:hover {
    background-color: #FDF2F2;
    border-color: #C0392B;
}
QPushButton#btn_stop:pressed {
    background-color: #C0392B;
    color: #FFFFFF;
}

/* ── 체크박스 ─────────────────────────────────────────────────────────── */
QCheckBox {
    color: #2D3748;
    spacing: 7px;
    background-color: transparent;
}
QCheckBox::indicator {
    width: 15px;
    height: 15px;
    border: 1.5px solid #B0BDD0;
    border-radius: 3px;
    background-color: #FFFFFF;
}
QCheckBox::indicator:hover {
    border-color: #2E5F9E;
    background-color: #F0F5FF;
}
QCheckBox::indicator:checked {
    background-color: #1E3A5F;
    border-color: #1E3A5F;
}
QCheckBox::indicator:checked:hover {
    background-color: #2E5F9E;
}

/* ── 리스트 위젯 ──────────────────────────────────────────────────────── */
QListWidget {
    background-color: #FFFFFF;
    border: 1px solid #DDE3EC;
    border-radius: 8px;
    color: #2D3748;
    outline: none;
}
QListWidget::item {
    padding: 2px 0px;
    border-bottom: 1px solid #F0F2F5;
}
QListWidget::item:selected {
    background-color: transparent;
    color: #2D3748;
}
QListWidget::item:hover:!selected {
    background-color: #F7F9FC;
}

/* ── 프로그레스바 ─────────────────────────────────────────────────────── */
QProgressBar {
    background-color: #EEF2F7;
    border: none;
    border-radius: 4px;
    color: #2D3748;
    text-align: center;
    font-size: 8pt;
    font-weight: 600;
    height: 18px;
}
QProgressBar::chunk {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #1E3A5F, stop:1 #2E5F9E);
    border-radius: 4px;
}

/* ── 상태바 ───────────────────────────────────────────────────────────── */
QStatusBar {
    background-color: #1E3A5F;
    border-top: none;
    color: #A8C4E0;
    font-size: 8pt;
    padding: 0 8px;
}
QStatusBar::item { border: none; }

/* ── 스크롤바 ─────────────────────────────────────────────────────────── */
QScrollBar:vertical {
    background-color: transparent;
    width: 7px;
    border: none;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background-color: #C8D3E0;
    border-radius: 3px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover { background-color: #8FA3BF; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal {
    background-color: transparent;
    height: 7px;
    border: none;
}
QScrollBar::handle:horizontal {
    background-color: #C8D3E0;
    border-radius: 3px;
    min-width: 24px;
}
QScrollBar::handle:horizontal:hover { background-color: #8FA3BF; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

/* ── 프레임 ───────────────────────────────────────────────────────────── */
QFrame {
    background-color: #FFFFFF;
    border: none;
}

/* ── 메시지박스 ───────────────────────────────────────────────────────── */
QMessageBox { background-color: #FFFFFF; }
QMessageBox QLabel { color: #2D3748; }
"""

__all__ = ['WindowUI', 'CameraListItem']

class CameraListItem(QWidget):
    """설정 탭의 카메라 리스트 아이템용 커스텀 위젯 (1121 스타일)"""
    sig_area_changed = Signal(str, int, bool)

    def __init__(self, cam_data, state_mgr, parent=None):
        super().__init__(parent)
        self.cam_data = cam_data
        self.key = cam_data.get('key', '')
        self._selected = False

        # 카드 기본 스타일 (미선택)
        self.setAutoFillBackground(True)
        self._apply_card_style(selected=False)

        # 메인 레이아웃 (가로)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)
        
        # 1. 모니터링 체크박스
        self.chk_monitor = QCheckBox()
        self.chk_monitor.setFixedWidth(20)
        # 상태 복원 및 저장 연결
        if state_mgr:
            self.chk_monitor.setChecked(state_mgr.get_monitor_enabled(self.key))
            self.chk_monitor.stateChanged.connect(lambda state: state_mgr.set_monitor_enabled(self.key, state == Qt.CheckState.Checked.value))
        layout.addWidget(self.chk_monitor)
        
        # 2. 정보 (IP, 이름, 연결상태) - 세로 배치
        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)
        info_layout.setAlignment(Qt.AlignVCenter)
        
        ip_text = cam_data.get('ip', '0.0.0.0')
        self.lbl_ip = QLabel(f"{ip_text}")
        self.lbl_ip.setStyleSheet("color: #2D3748; font-size: 9pt; font-weight: 600; font-family: 'Consolas', monospace;")
        
        display_name = cam_data.get('name') or cam_data.get('key', '')
        self.lbl_name = QLabel(f"{display_name}")
        self.lbl_name.setStyleSheet("font-weight: 500; font-size: 9pt; color: #64748B;")
        
        is_connected = cam_data.get('connected', False)
        self.lbl_status = QLabel()
        self.set_status(is_connected)
        
        info_layout.addWidget(self.lbl_ip)
        info_layout.addWidget(self.lbl_name)
        info_layout.addWidget(self.lbl_status)
        layout.addLayout(info_layout, stretch=2)
        
        # 3. 중앙 영역 (횡단대기자 라벨 + 체크박스)
        center_layout = QVBoxLayout()
        center_layout.setAlignment(Qt.AlignCenter)
        center_layout.setSpacing(5)
        
        # "횡단대기자" 라벨
        lbl_center = QLabel("횡단대기자")
        lbl_center.setStyleSheet(
            "font-weight: 700; color: #64748B; font-size: 7.5pt; "
            "letter-spacing: 0.5px; text-transform: uppercase;"
        )
        lbl_center.setAlignment(Qt.AlignCenter)
        center_layout.addWidget(lbl_center)
        
        # 영역 체크박스 (가로)
        area_chk_layout = QHBoxLayout()
        area_chk_layout.setAlignment(Qt.AlignCenter)
        area_chk_layout.setSpacing(15)
        self.area_checks = []
        
        for i in range(4):
            area_id = i + 1
            chk = QCheckBox(f"{area_id}")
            # 상태 복원
            if state_mgr:
                chk.setChecked(state_mgr.get_area_enabled(self.key, area_id))
            
            chk.stateChanged.connect(lambda state, aid=area_id: self.sig_area_changed.emit(self.key, aid, state == Qt.CheckState.Checked.value))
            self.area_checks.append(chk)
            area_chk_layout.addWidget(chk)
            
        center_layout.addLayout(area_chk_layout)
        layout.addLayout(center_layout, stretch=3)

        # 4. 우측 열 (설정된 영역 정보 + LED)
        right_layout = QVBoxLayout()
        right_layout.setAlignment(Qt.AlignCenter)
        right_layout.setSpacing(5)
        
        self.lbl_area_info = QLabel("설정된 영역: -개")
        self.lbl_area_info.setAlignment(Qt.AlignRight)
        self.lbl_area_info.setStyleSheet("color: #64748B; font-size: 7.5pt; font-weight: 600;")
        right_layout.addWidget(self.lbl_area_info)
        
        # LED (가로) - 체크박스와 분리됨
        led_layout = QHBoxLayout()
        led_layout.setAlignment(Qt.AlignRight)
        led_layout.setSpacing(8)
        self.area_leds = {} # {area_id: QLabel}
        
        for i in range(4):
            area_id = i + 1
            led = QLabel()
            led.setFixedSize(10, 10)
            # 초기 상태: OFF
            led.setStyleSheet("background-color: #DDE3EC; border-radius: 5px; border: 1px solid #B0BDD0;")
            led.setToolTip(f"Area {area_id} Status")
            self.area_leds[area_id] = led
            led_layout.addWidget(led)
            
        right_layout.addLayout(led_layout)
        layout.addLayout(right_layout, stretch=2)

    def set_area_led(self, area_id: int, is_on: bool):
        """특정 영역의 LED 상태를 설정합니다."""
        if area_id in self.area_leds:
            led = self.area_leds[area_id]
            if is_on:
                # ON: 네이비 블루
                led.setStyleSheet("background-color: #2E5F9E; border-radius: 5px; border: 1px solid #1E3A5F;")
            else:
                # OFF: 연한 블루그레이
                led.setStyleSheet("background-color: #DDE3EC; border-radius: 5px; border: 1px solid #B0BDD0;")

    def set_area_count(self, area_id: int, count: int | None):
        """인원수에 따라 LED 상태를 갱신합니다."""
        # count가 None이 아니고 1 이상이면 ON
        is_on = (count is not None and count > 0)
        self.set_area_led(area_id, is_on)

    def update_device_info(self, connected, area_count):
        """카메라 장비 정보 업데이트 (연결상태, 설정된 영역 수)"""
        self.set_status(connected)
        count_str = f"{area_count}" if connected else "-"
        self.lbl_area_info.setText(f"설정된 영역: {count_str}개")

    def set_connected(self, connected: bool):
        """연결 상태에 따라 텍스트와 색상을 갱신합니다."""
        self.set_status(connected)

    def set_counts_visible(self, visible: bool):
        """연결 상태에 따라 인원수 관련 UI(LED 등)의 가시성을 제어합니다."""
        # 연결이 끊기면 LED를 모두 끄거나(회색), 숨길 수 있음.
        # 여기서는 LED를 OFF(회색) 상태로 초기화하여 '표시하지 않음' 효과를 냄
        if not visible:
            for led in self.area_leds.values():
                led.setStyleSheet("background-color: #DDE3EC; border-radius: 5px; border: 1px solid #B0BDD0;")

    def set_status(self, connected):
        if connected:
            self.lbl_status.setText("● 연결됨")
            self.lbl_status.setStyleSheet(
                "color: #1A7F4B; font-weight: 700; font-size: 8pt; background-color: transparent;"
            )
        else:
            self.lbl_status.setText("○ 연결 안됨")
            self.lbl_status.setStyleSheet(
                "color: #C0392B; font-weight: 600; font-size: 8pt; background-color: transparent;"
            )
    
    def update_area_count(self, count):
        """동적 발견된 영역 수 업데이트"""
        # 기존 로직 유지: 연결 상태가 True일 때만 숫자가 의미가 있을 수 있으나,
        # 여기서는 단순히 텍스트만 갱신
        current_text = self.lbl_area_info.text()
        if "연결" in self.lbl_status.text(): # 연결된 상태라면
             self.lbl_area_info.setText(f"설정된 영역: {count}개")

    def set_selected(self, selected: bool):
        """QListWidget 선택 상태를 카드 자체에 반영합니다."""
        self._selected = selected
        self._apply_card_style(selected)

    def _apply_card_style(self, selected: bool):
        """선택 여부에 따라 카드 배경/테두리를 바꿉니다."""
        if selected:
            self.setStyleSheet(f"""
                CameraListItem {{
                    background-color: #EBF1FB;
                    border-left: 4px solid #1E3A5F;
                    border-top: 1px solid #C0D0E8;
                    border-right: 1px solid #C0D0E8;
                    border-bottom: 1px solid #C0D0E8;
                }}
                CameraListItem QLabel {{ background-color: transparent; }}
                CameraListItem QCheckBox {{ background-color: transparent; }}
            """)
        else:
            self.setStyleSheet(f"""
                CameraListItem {{
                    background-color: #FFFFFF;
                    border-left: 4px solid transparent;
                    border-top: 1px solid #DDE3EC;
                    border-right: 1px solid #DDE3EC;
                    border-bottom: 1px solid #DDE3EC;
                }}
                CameraListItem QLabel {{ background-color: transparent; }}
                CameraListItem QCheckBox {{ background-color: transparent; }}
            """)

class WindowUI:
    """메인 윈도우의 UI 구성을 담당하는 클래스"""
    def __init__(self):
        # UI 위젯 참조 변수 초기화
        self.tabs = None
        self.camera_list = None
        self.edit_name = None
        self.edit_ip = None
        self.edit_port = None
        self.edit_id = None
        self.edit_pw = None
        self.btn_add = None
        self.btn_mod = None
        self.btn_del = None
        self.btn_ref = None
        self.btn_mon = None
        self.chk_show_debug = None
        self.list_events = None
        self.lbl_gpio_status = None
        self.video_grid = None
        self.people_summary = None
        self.personnel_count_label = None
        self.btn_stop = None
        self.status_bar = None
        self.cpu_bar = None
        self.mem_bar = None
        self.chk_keep_watching = None  # 영상 계속 시청 체크박스
        # ROI Edit UI
        self.group_roi_edit = None
        self.btn_roi_area1 = None
        self.btn_roi_area2 = None
        self.btn_roi_area3 = None
        self.btn_roi_area4 = None
        self.btn_roi_save = None
        self.btn_roi_cancel = None
        self.lbl_roi_target = None
        self.gpio_text = None
        self.btn_conn = None
        self.btn_disc = None
        self.btn_test = None

    def setup_ui(self, main_window: QMainWindow):
        """메인 윈도우의 레이아웃을 구성합니다."""
        main_window.setStyleSheet(PROFESSIONAL_QSS)
        central_widget = QWidget()
        main_window.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)
        
        # 탭 구성
        self._setup_tab_settings()
        self._setup_tab_monitoring()
        self._setup_tab_info()
        
        # 상태 표시줄
        self.status_bar = QStatusBar()
        main_window.setStatusBar(self.status_bar)

    def _setup_tab_settings(self):
        tab = QWidget()
        main_layout = QGridLayout(tab)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(10)
        
        # --- Left Column ---
        # 1. Camera List (Top Left)
        left_group = QGroupBox("Camera List")
        left_layout = QVBoxLayout(left_group)
        self.camera_list = QListWidget()
        self.camera_list.setSelectionMode(QListWidget.SingleSelection)
        self.camera_list.setSpacing(2)
        # 선택 변경 시 카드 스타일 갱신
        self.camera_list.currentRowChanged.connect(self._on_camera_selection_changed)
        left_layout.addWidget(self.camera_list)
        main_layout.addWidget(left_group, 0, 0)
        
        # --- Right Column (Container for Info + GPIO) ---
        right_container = QWidget()
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        
        # 카메라 정보 입력
        info_group = QGroupBox("Camera Info")
        info_layout = QGridLayout(info_group)
        
        info_layout.addWidget(QLabel("설명"), 0, 0)
        self.edit_name = QLineEdit()
        info_layout.addWidget(self.edit_name, 0, 1)
        
        info_layout.addWidget(QLabel("IP 주소"), 1, 0)
        self.edit_ip = QLineEdit()
        info_layout.addWidget(self.edit_ip, 1, 1)
        
        info_layout.addWidget(QLabel("PORT"), 2, 0)
        self.edit_port = QLineEdit()
        info_layout.addWidget(self.edit_port, 2, 1)
        
        info_layout.addWidget(QLabel("ID"), 3, 0)
        self.edit_id = QLineEdit()
        info_layout.addWidget(self.edit_id, 3, 1)
        
        info_layout.addWidget(QLabel("Password"), 4, 0)
        self.edit_pw = QLineEdit()
        self.edit_pw.setEchoMode(QLineEdit.Password)
        info_layout.addWidget(self.edit_pw, 4, 1)
        
        # 버튼 그룹
        btn_grid = QGridLayout()
        self.btn_add = QPushButton("추가")
        self.btn_mod = QPushButton("수정")
        self.btn_del = QPushButton("삭제")
        self.btn_ref = QPushButton("새로고침")
        self.btn_mon = QPushButton("모니터링")
        self.btn_mon.setObjectName("btn_mon")
        
        btn_grid.addWidget(self.btn_add, 0, 0)
        btn_grid.addWidget(self.btn_mod, 0, 1)
        btn_grid.addWidget(self.btn_del, 0, 2)
        btn_grid.addWidget(self.btn_ref, 1, 0)
        btn_grid.addWidget(self.btn_mon, 1, 1, 1, 2)
        
        info_layout.addLayout(btn_grid, 5, 0, 1, 2)
        right_layout.addWidget(info_group, stretch=1)
        
        # GPIO Log (더미 UI 유지)
        gpio_group = QGroupBox("GPIO Log")
        gpio_layout = QVBoxLayout(gpio_group)
        self.gpio_text = QTextEdit()
        self.gpio_text.setReadOnly(True)
        self.gpio_text.setPlaceholderText("GPIO Log will appear here...")
        # [추가] 최대 라인 수 제한 (오래된 로그 자동 삭제)
        self.gpio_text.document().setMaximumBlockCount(200)
        gpio_layout.addWidget(self.gpio_text)
        
        # GPIO 하단 컨트롤
        gpio_ctrl_layout = QHBoxLayout()
        
        # 상태 라벨 및 고정 핀 정보
        self.lbl_gpio_status = QLabel("GPIO: Unknown")
        self.lbl_gpio_status.setStyleSheet("font-weight: 700; color: #64748B; font-size: 8.5pt;")
        gpio_ctrl_layout.addWidget(self.lbl_gpio_status)

        lbl_fixed = QLabel("Output: GPIO17 (BCM17 / Pin11) 고정")
        lbl_fixed.setStyleSheet(
            "border: 1px solid #DDE3EC; border-radius: 4px; padding: 5px 10px; "
            "background-color: #F7F9FC; color: #64748B; font-size: 8pt; "
            "font-family: 'Consolas', monospace;"
        )
        gpio_ctrl_layout.addWidget(lbl_fixed)
        
        gpio_btn_layout = QVBoxLayout()
        self.btn_conn = QPushButton("연결")
        self.btn_disc = QPushButton("해제")
        self.btn_test = QPushButton("테스트")
        gpio_btn_layout.addWidget(self.btn_conn)
        gpio_btn_layout.addWidget(self.btn_disc)
        gpio_btn_layout.addWidget(self.btn_test)
        
        gpio_ctrl_layout.addLayout(gpio_btn_layout)
        gpio_layout.addLayout(gpio_ctrl_layout)
        right_layout.addWidget(gpio_group, stretch=2)
        
        # Add right container to grid (0,1) spanning 2 rows
        main_layout.addWidget(right_container, 0, 1, 2, 1)
        
        # --- Left Column Bottom ---
        # 2. Event Log (Bottom Left)
        log_group = QGroupBox("Event Log")
        log_layout = QGridLayout(log_group)
        
        self.chk_show_debug = QCheckBox("Show DEBUG")
        self.chk_show_debug.setChecked(False)
        log_layout.addWidget(self.chk_show_debug, 0, 0, Qt.AlignRight)
        
        self.list_events = QListWidget()
        log_layout.addWidget(self.list_events, 1, 0)
        main_layout.addWidget(log_group, 1, 0)
        
        # Set Grid Stretches
        main_layout.setColumnStretch(0, 6)
        main_layout.setColumnStretch(1, 4)
        main_layout.setRowStretch(0, 6)
        main_layout.setRowStretch(1, 4)
        
        self.tabs.addTab(tab, "Settings")

    def _setup_tab_monitoring(self):
        tab = QWidget()
        layout = QHBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        
        # 좌측: 영상 그리드
        self.video_container = QWidget()
        # [수정] 영상 영역이 남은 공간을 모두 차지하도록 설정
        self.video_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_grid = QGridLayout(self.video_container)
        self.video_grid.setContentsMargins(0, 0, 0, 0)
        self.video_grid.setSpacing(2)
        layout.addWidget(self.video_container, stretch=1)
        
        # 우측: 상태 및 제어
        right_panel = QFrame()
        # [수정] 우측 패널 폭 고정 (ROI Edit 표시/숨김에 따른 폭 변화 방지)
        right_panel.setFixedWidth(RIGHT_PANEL_WIDTH)
        right_panel.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        right_layout = QVBoxLayout(right_panel)
        
        # 시스템 상태
        sys_group = QGroupBox("System Status")
        sys_layout = QVBoxLayout(sys_group)
        self.cpu_bar = QProgressBar()
        self.cpu_bar.setValue(15)
        self.cpu_bar.setFormat("CPU: 15%")
        self.mem_bar = QProgressBar()
        self.mem_bar.setValue(40)
        self.mem_bar.setFormat("MEM: 40%")
        sys_layout.addWidget(self.cpu_bar)
        sys_layout.addWidget(self.mem_bar)
        right_layout.addWidget(sys_group)
        
        # 인원수 집계
        sum_group = QGroupBox("People Count Summary")
        sum_layout = QVBoxLayout(sum_group)
        self.people_summary = QTextEdit()
        self.people_summary.setReadOnly(True)
        sum_layout.addWidget(self.people_summary)
        right_layout.addWidget(sum_group)
        
        # 실시간 인원수
        rt_group = QGroupBox("Realtime Count")
        rt_layout = QVBoxLayout(rt_group)
        self.personnel_count_label = QTextEdit()
        self.personnel_count_label.setReadOnly(True)
        rt_layout.addWidget(self.personnel_count_label)
        right_layout.addWidget(rt_group)

        # ROI 편집 (New)
        self.group_roi_edit = QGroupBox("ROI Edit")
        self.group_roi_edit.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        roi_layout = QVBoxLayout(self.group_roi_edit)
        
        self.lbl_roi_target = QLabel("Target: None")
        self.lbl_roi_target.setStyleSheet("color: #64748B; font-size: 8.5pt; font-family: 'Consolas', monospace;")
        roi_layout.addWidget(self.lbl_roi_target)
        
        # Area Buttons Container
        area_btn_container = QWidget()
        area_btn_layout = QHBoxLayout(area_btn_container)
        area_btn_layout.setContentsMargins(0, 0, 0, 0)
        area_btn_layout.setSpacing(6)
        
        self.btn_roi_area1 = QPushButton("1")
        self.btn_roi_area2 = QPushButton("2")
        self.btn_roi_area3 = QPushButton("3")
        self.btn_roi_area4 = QPushButton("4")
        
        for btn in [self.btn_roi_area1, self.btn_roi_area2, self.btn_roi_area3, self.btn_roi_area4]:
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            area_btn_layout.addWidget(btn, 1) # stretch=1로 균등 분배
            
        roi_layout.addWidget(area_btn_container)
        
        # Action Buttons Container
        action_btn_container = QWidget()
        action_btn_layout = QHBoxLayout(action_btn_container)
        action_btn_layout.setContentsMargins(0, 0, 0, 0)
        action_btn_layout.setSpacing(6)
        
        self.btn_roi_save = QPushButton("확인")
        self.btn_roi_cancel = QPushButton("취소")
        
        for btn in [self.btn_roi_save, self.btn_roi_cancel]:
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            action_btn_layout.addWidget(btn, 1) # stretch=1로 균등 분배
            
        roi_layout.addWidget(action_btn_container)
        
        self.group_roi_edit.setVisible(False) # 초기엔 숨김
        right_layout.addWidget(self.group_roi_edit)
        
        # 제어 버튼
        btn_group = QGroupBox("Control")
        btn_layout = QVBoxLayout(btn_group)
        
        # 영상 계속 시청 체크박스
        self.chk_keep_watching = QCheckBox("영상 계속 시청")
        self.chk_keep_watching.setChecked(False)
        btn_layout.addWidget(self.chk_keep_watching)
        
        self.btn_stop = QPushButton("Stop Monitoring")
        self.btn_stop.setObjectName("btn_stop")
        self.btn_stop.setMinimumHeight(40)
        btn_layout.addWidget(self.btn_stop)
        right_layout.addWidget(btn_group)
        right_layout.addStretch()
        
        layout.addWidget(right_panel)
        self.tabs.addTab(tab, "Monitoring")

    def _setup_tab_info(self):
        tab = QWidget()
        layout = QHBoxLayout(tab)
        
        left_layout = QVBoxLayout()
        grp_company = QGroupBox("회사 정보")
        form_comp = QFormLayout(grp_company)
        form_comp.addRow("회사명 :", QLabel("오디텍, ODTECH"))
        form_comp.addRow("주소 :", QLabel("전북특별자치도 완주군 봉동읍 용암리 814"))
        form_comp.addRow("홈페이지 :", QLabel("http://www.od-tech.com"))
        left_layout.addWidget(grp_company)
        
        grp_prog = QGroupBox("프로그램 정보")
        form_prog = QFormLayout(grp_prog)
        form_prog.addRow("버전 :", QLabel("ver 1.0 (RPi)"))
        left_layout.addWidget(grp_prog)
        
        left_layout.addStretch()
        layout.addLayout(left_layout, stretch=1)
        
        right_layout = QVBoxLayout()
        lbl_logo = QLabel("ODTECH")
        lbl_logo.setAlignment(Qt.AlignCenter)
        lbl_logo.setStyleSheet(
            "border: 1px solid #DDE3EC; border-radius: 8px; "
            "background-color: #FFFFFF; "
            "font-size: 26pt; font-weight: 700; color: #1E3A5F; "
            "letter-spacing: 6px; padding: 30px;"
        )
        right_layout.addWidget(lbl_logo)
        layout.addLayout(right_layout, stretch=1)
        
        self.tabs.addTab(tab, "Information")

    def _on_camera_selection_changed(self, current_row: int):
        """카메라 리스트 선택 변경 시 각 카드의 선택 스타일을 갱신합니다."""
        for i in range(self.camera_list.count()):
            item = self.camera_list.item(i)
            widget = self.camera_list.itemWidget(item)
            if isinstance(widget, CameraListItem):
                widget.set_selected(i == current_row)
