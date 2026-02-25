from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QStackedLayout,
                               QLabel, QListWidgetItem, QFrame, QMessageBox, QApplication)
from PySide6.QtCore import Qt, Slot, QTimer, QSize, Signal, QThreadPool, QRunnable, QObject, QEvent
from PySide6.QtGui import QFont, QPixmap
from config_module import ConfigManager
from state_manager import StateManager
from video_ui import VideoWidget
from cgi_client import build_rtsp_url, PeopleCountThread, StayDetectionThread, fetch_region_data, parse_region_count
from gpio_bridge import GpioBridge
import db_module
import cgi_client
try:
    from window_ui import WindowUI, CameraListItem
except ImportError:
    # 패키지 형태로 실행될 경우를 대비한 상대 경로 임포트
    from .window_ui import WindowUI, CameraListItem
import copy
from log import get_logger, cleanup_old_logs, check_and_rotate_log
from log_rate_limit import should_log
import time
import re
import hashlib
import socket

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

logger = get_logger(__name__)

class StatusWorkerSignals(QObject):
    result = Signal(str, bool, int) # key, connected, count

class CameraStatusWorker(QRunnable):
    """백그라운드에서 카메라 연결 상태와 영역 설정을 확인하는 워커"""
    def __init__(self, key, ip, user, pw):
        super().__init__()
        self.key = key
        self.ip = ip
        self.user = user
        self.pw = pw
        self.signals = StatusWorkerSignals()

    def run(self):
        connected = False
        count = 0
        try:
            text = fetch_region_data(self.ip, self.user, self.pw)
            if text:
                connected = True
                count = parse_region_count(text)
        except Exception as e:
            logger.debug(f"[StatusWorker] Check failed for {self.key}: {e}")
        self.signals.result.emit(self.key, connected, count)

class RoiWorkerSignals(QObject):
    result = Signal(object, object, object) # key, {area_id: points}, [enabled_area_ids] (Use object to avoid QVariant conversion issues)

class RoiLoadWorker(QRunnable):
    """백그라운드에서 ROI 좌표를 로드하는 워커"""
    def __init__(self, key, ip, user, pw):
        super().__init__()
        self.key = key
        self.ip = ip
        self.user = user
        self.pw = pw
        self.signals = RoiWorkerSignals()

    def run(self):
        data = {}
        enabled_areas = set()
        try:
            text = cgi_client.get_roi_raw_data(self.ip, self.user, self.pw)
            txt_len = len(text) if text else 0
            
            if text:
                data = cgi_client.parse_regions_by_area_raw(text)
                
                # Enable 상태 파싱 (정규식 직접 사용 또는 cgi_client 활용)
                # cgi_client.get_roi_config는 내부에서 fetch를 또 하므로, 
                # 여기서는 text 기반으로 직접 파싱하거나 cgi_client에 파서가 있다면 사용.
                # cgi_client.py에 get_roi_config가 있지만 text를 인자로 받지 않음.
                # 간단히 정규식으로 Enable=true인 인덱스를 찾아 매핑.
                import re
                # VideoAnalyseRule[0][idx].Config.AreaID=id AND .Enable=true
                # cgi_client.get_roi_config 로직을 참고하여 구현 (여기서는 fetch 없이 text 파싱)
                # (간소화: parse_region_count가 활성 영역 개수를 세므로, 여기서는 모든 영역 로드 후 기본 활성 간주하거나,
                #  정확히 하려면 cgi_client.get_roi_config를 호출하는게 낫음. 
                #  하지만 성능상 fetch 1번이 좋으므로, 일단 모든 영역을 enabled로 가정하거나, 
                #  추후 cgi_client에 parse_roi_config(text) 추가 권장.
                #  현재는 data에 있는 키들을 모두 enabled로 처리 (좌표가 있으면 표시))
                enabled_areas = set(data.keys())
                logger.debug(f"[RoiLoadWorker] {self.key}: Assuming all {len(enabled_areas)} loaded areas are ENABLED.")
                
                # 상세 로그: 각 Area별 포인트 개수 확인
                area_summary = {k: len(v) for k, v in data.items()}
                logger.info(f"[ROI] Loaded {self.key}: len={txt_len}, areas={area_summary}, enabled={enabled_areas}")
            else:
                logger.warning(f"[RoiLoadWorker] {self.key} fetch returned empty/None")
        except Exception as e:
            logger.error(f"[RoiLoadWorker] Error {self.key}: {e}")
        # Set을 List로 변환하여 전송 (Qt 메타타입 변환 문제 방지)
        self.signals.result.emit(self.key, data, list(enabled_areas))

class WindowSum(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OPAS-200 - ver 1.0 (RPi)")
        self._closing = False
        self.resize(1280, 800)
        
        # 설정 매니저 초기화 및 로드
        self.cfg_mgr = ConfigManager()
        self.config = self.cfg_mgr.load_or_create()
        
        # [추가] 로그 및 DB 보존 정책 실행 (앱 시작 시 1회)
        log_retention = self.config.getint('app', 'log_retention_days', fallback=30)
        db_retention = self.config.getint('app', 'db_retention_days', fallback=30)
        
        # 로그 파일 정리
        cleanup_old_logs(log_retention)
        
        # 상태 매니저 초기화 (운영 상태 로드)
        self.state_mgr = StateManager()
        
        # GPIO 브리지 초기화
        self.gpio_bridge = GpioBridge(self.cfg_mgr)
        
        # UI 초기화 (WindowUI 사용)
        self.ui = WindowUI()
        self.ui.setup_ui(self)
        self._update_gpio_status_ui()
        
        # UI 시그널 연결
        self._connect_ui_signals()
        
        # UI 변수 초기화 (reload_cameras 등에서 참조)
        self.tiles = {}  # {camera_key: {'frame': QFrame, 'video': VideoWidget, 'label': QLabel, 'layout': QStackedLayout}}
        self.camera_items = {} # {camera_key: CameraListItem} - UI 제어용
        self.threadpool = QThreadPool()
        
        # 이벤트 관련 상태
        self.event_threads = {} # {camera_key: {"people": t1, "stay": t2}}
        self.total_events = 0
        self._last_people_total = {} # {camera_key: {area_id: count}}
        self.stay_states = {} # {(camera_key, area_id): True | False}
        self.last_event_timestamps = {} # {(camera_key, area_id): timestamp}
        self.gpio_last_trigger_ts = {} # {(camera_key, area_id, type): timestamp} [Commit 17-1]
        self.stay_last_emit = {} # {(camera_key, area_id, action): timestamp}
        self._stay_clear_timers = {} # {(camera_key, area_id): QTimer}
        self.discovered_areas = {} # {camera_key: set(area_ids)}
        self.realtime_counts = {} # {camera_key: {area_id: count}}
        self.camera_conn_status = {} # {camera_key: bool} - 연결 상태 추적용
        self.event_cooldown_sec = self.config.getint('event', 'cooldown_sec', fallback=2)
        self.stay_cooldown_sec = self.config.getint('event', 'stay_cooldown_sec', fallback=10)
        self.stay_hold_ms = self.config.getint('event', 'stay_hold_ms', fallback=10000)
        self.log_load_limit = self.config.getint('event', 'log_load_limit', fallback=200)
        self._last_restart_time_event = {} # {camera_key: timestamp}
        self._last_restart_time_video = {} # {camera_key: timestamp}
        self._pc_restart_inflight = {} # [FIX-2] PeopleCount 재시작 중복 방지 플래그
        self._rebuilding_grid = False
        self._pending_grid_cameras = None # [CRITICAL FIX v3] Pending cameras for async build
        self._starting_monitor = False
        
        # 초기 상태 복원 (AppState)
        self._restore_app_state()
        
        # DB 초기화 및 최근 이벤트 로드
        db_path, mig_msg = db_module.init_db()
        
        # DB 비동기 워커 시작
        db_module.init_db_worker()
        
        # [Commit 27-2] 비정상 종료 감지
        last_event = db_module.get_last_lifecycle_event()
        if last_event and last_event.get("event_type") == "APP_START":
            last_ts = last_event.get("ts_epoch", 0)
            warned_ts = self.state_mgr.get("last_crash_warned_ts", 0)
            
            if last_ts != warned_ts:
                ts_str = last_event.get("ts", "N/A")
                msg = f"[Main] 이전 실행이 비정상 종료로 추정됩니다 (last=APP_START @ {ts_str})"
                self.add_event_log(msg)
                logger.warning(msg)
                self.state_mgr.set("last_crash_warned_ts", last_ts)
                self.state_mgr.save_state()
        
        # [Commit 23-2] APP_START 로그 DB 저장 (event_logs)
        db_module.enqueue_event({
            "type": "APP_START",
            "message": "Application started",
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ts_epoch": int(time.time())
        })
        
        # [수정] DB 정리 작업을 비동기로 큐에 추가
        logger.debug("[System] Scheduling DB purge...")
        db_module.enqueue_purge(db_retention, self.on_purge_completed)

        # DB_PATH 로그 강제 표시 (앱 시작 시 확인용)
        self.add_event_log(f"[DEBUG] DB_PATH: {db_module.get_db_path()}")
        self.add_event_log(f"[DEBUG] cooldown_sec={self.event_cooldown_sec}, stay_cooldown={self.stay_cooldown_sec}, stay_hold={self.stay_hold_ms}ms")
        
        if mig_msg:
            self.add_event_log(f"[DEBUG] {mig_msg}")
        
        self.load_recent_events()
        
        # 초기 카메라 목록 로드 (UI 구성 완료 후 호출)
        self.reload_cameras()
        
        # UI 갱신 타이머 (1초 주기, 이벤트 발생 시 dirty 플래그로 갱신)
        self.ui_dirty = True # 초기 표시를 위해 True로 시작
        self.ui_update_timer = QTimer(self)
        self.ui_update_timer.timeout.connect(self.update_monitoring_tables)
        self.ui_update_timer.start(1000)
        
        # GPIO UI 초기 상태 갱신
        self._update_gpio_status_ui()
        if self.gpio_bridge.is_connected:
            self.add_gpio_log("[System] GPIO Initialized (Connected)")

        # 진단 로그 타이머 (10초 주기)
        self.stats_log_timer = QTimer(self)
        self.stats_log_timer.timeout.connect(self.log_stats_debug)
        self.stats_log_timer.start(10000)
        self.add_event_log(f"[DEBUG] PeopleCount epoch now={int(time.time())}, tz={time.tzname}")

        # [New] 헬스체크 타이머 (10초 주기)
        self.health_timer = QTimer(self)
        self.health_timer.timeout.connect(self.check_thread_health)
        self.health_timer.start(10000)

        # [New] 시스템 상태 갱신 타이머 (5초 주기)
        self.sys_status_timer = QTimer(self)
        self.sys_status_timer.timeout.connect(self.update_system_status)
        self.sys_status_timer.start(5000)
        
        # ROI Edit State
        self.is_video_maximized = False
        self.maximized_camera_key = None
        self.maximized_tile_info = None # {widget, row, col}
        self.current_roi_area = 1 # Default area to show
        self.roi_mode = "monitor" # [추가] 초기 모드 설정 (monitor | view | edit)
        self.roi_cache = {} # {camera_key: {'norm': dict, 'enabled': set}}
        self.roi_backup_cache = {} # For cancel revert {camera_key: {'norm': ..., 'enabled': ...}}
        self._rebind_timer = QTimer(self) # 디바운스용 타이머
        self._rebind_timer.setSingleShot(True)
        self._rebind_timer.timeout.connect(self._perform_rebind_visible)
        self._connect_roi_signals()
        self.ui.tabs.currentChanged.connect(self.on_tab_changed)
        
        # [추가] 로그 자정 롤오버 체크 타이머 (1분 주기)
        self.log_rotate_timer = QTimer(self)
        self.log_rotate_timer.timeout.connect(check_and_rotate_log)
        self.log_rotate_timer.start(60000) # 60초마다 체크

        # [Commit M1-2] Idle Monitor 설정 및 초기화
        self.idle_stop_enable = self.config.getboolean('monitor', 'idle_stop_enable', fallback=True)
        self.idle_stop_sec = self.config.getint('monitor', 'idle_stop_sec', fallback=300)
        self._last_user_activity_ts = time.time()
        self._auto_stop_fired = False
        
        QApplication.instance().installEventFilter(self)
        self.idle_timer = QTimer(self)
        self.idle_timer.timeout.connect(self._check_idle_stop)
        self.idle_timer.start(10000) # 10초마다 체크

    def on_purge_completed(self, deleted_count, retention_days, error):
        """DB 정리 완료 시 호출되는 콜백 (DB 워커 스레드에서 실행됨)"""
        if error:
            msg = f"[System] DB Purge failed: {error}"
        else:
            msg = f"[System] DB Purge: {deleted_count} rows deleted (older than {retention_days} days)"
        
        # UI 업데이트를 메인 스레드에서 안전하게 실행
        QTimer.singleShot(0, lambda: self.add_event_log(msg))
        
    def _connect_ui_signals(self):
        """UI 요소의 시그널을 슬롯에 연결"""
        self.ui.camera_list.currentItemChanged.connect(self.on_camera_list_selected)
        self.ui.btn_add.clicked.connect(self.on_btn_add_clicked)
        self.ui.btn_mod.clicked.connect(self.on_btn_modify_clicked)
        self.ui.btn_del.clicked.connect(self.on_btn_delete_clicked)
        self.ui.btn_ref.clicked.connect(self.reload_cameras)
        self.ui.btn_mon.clicked.connect(self.on_btn_start_clicked)
        self.ui.btn_stop.clicked.connect(self.on_btn_stop_clicked)
        self.ui.chk_show_debug.stateChanged.connect(self.reload_recent_events_filter)
        self.ui.chk_keep_watching.stateChanged.connect(self.on_keep_watching_changed)

        # GPIO 버튼 연결
        if hasattr(self.ui, 'btn_conn'):
            self.ui.btn_conn.clicked.connect(self.on_gpio_connect_clicked)
        if hasattr(self.ui, 'btn_disc'):
            self.ui.btn_disc.clicked.connect(self.on_gpio_disconnect_clicked)
        if hasattr(self.ui, 'btn_test'):
            self.ui.btn_test.clicked.connect(self.on_gpio_test_clicked)

    def on_keep_watching_changed(self, state):
        status = "ON" if state == Qt.CheckState.Checked.value else "OFF"
        logger.info(f"[Main] Keep Watching toggled: {status}")

    def _connect_roi_signals(self):
        self.ui.btn_roi_area1.clicked.connect(lambda: self.on_roi_area_clicked(1))
        self.ui.btn_roi_area2.clicked.connect(lambda: self.on_roi_area_clicked(2))
        self.ui.btn_roi_area3.clicked.connect(lambda: self.on_roi_area_clicked(3))
        self.ui.btn_roi_area4.clicked.connect(lambda: self.on_roi_area_clicked(4))
        self.ui.btn_roi_save.clicked.connect(self.on_roi_save)
        self.ui.btn_roi_cancel.clicked.connect(self.on_roi_cancel)

    def reload_cameras(self):
        """설정을 다시 로드하고 카메라 목록을 갱신합니다."""
        # 현재 선택된 카메라 키 저장 (갱신 후 복원용)
        current_item = self.ui.camera_list.currentItem()
        selected_key = current_item.data(Qt.UserRole) if current_item else None

        # 방어 코드: UI 초기화 전 호출 방지
        if not hasattr(self.ui, "camera_list"):
            return

        # 1. 안전하게 모든 작업 중지
        self.stop_events()
        self.stop_all_streams()
        
        # [수정] 스트리밍이 중지되므로 모니터링 체크 상태도 초기화 (스트리밍 OFF = 체크 OFF)
        self.state_mgr.clear_all_monitor_enabled()
        
        self.cfg_mgr.reload()
        cameras = self.cfg_mgr.get_cameras()

        # 시그널 차단 (갱신 중 불필요한 이벤트 방지)
        self.ui.camera_list.blockSignals(True)
        self.ui.camera_list.clear()
        self.camera_items.clear()
        
        self.discovered_areas.clear()
        self._last_people_total.clear()
        
        if not cameras:
            item = QListWidgetItem("No Camera")
            item.setData(Qt.UserRole, None)
            self.ui.camera_list.addItem(item)
        else:
            for cam in cameras:
                # 커스텀 위젯 사용
                # CameraListItem은 이제 window_ui에서 import
                item = QListWidgetItem(self.ui.camera_list)
                item.setData(Qt.UserRole, cam['key'])
                item.setSizeHint(QSize(0, 80)) # 높이 지정
                
                widget = CameraListItem(cam, self.state_mgr)
                widget.sig_area_changed.connect(self.on_card_area_changed)
                
                self.camera_items[cam['key']] = widget
                self.ui.camera_list.setItemWidget(item, widget)
                
                # 비동기 상태 확인 시작
                worker = CameraStatusWorker(cam['key'], cam['ip'], cam['username'], cam['password'])
                worker.signals.result.connect(self.on_camera_status_update)
                self.threadpool.start(worker)
        
        # 그리드는 모니터링 시작 시 구성하므로 여기서는 생략 가능하지만,
        # 초기화 차원에서 전체 목록으로 구성해둘 수도 있음. (여기선 생략)
        
        # 선택 상태 복원 (삭제되지 않았다면)
        target_key = selected_key or self.state_mgr.get("last_camera_key")
        if target_key:
            for i in range(self.ui.camera_list.count()):
                item = self.ui.camera_list.item(i)
                if item.data(Qt.UserRole) == target_key:
                    self.ui.camera_list.setCurrentItem(item)
                    break
        
        self.ui.camera_list.blockSignals(False)
        
        # 상태바 업데이트 (존재할 경우)
        if hasattr(self.ui, 'status_bar'):
            keys = [c["key"] for c in cameras]
            config_path_str = str(self.cfg_mgr.config_file)
            self.ui.status_bar.showMessage(f"Cameras: {len(cameras)} | Keys: {','.join(keys)} | Config: {config_path_str}")
            
        # CRUD 확인용 로그
        self.add_event_log(f"[INFO] Reloaded cameras: {len(cameras)}, keys={','.join([c['key'] for c in cameras])}")
            
        # 이벤트 리스너 시작 (모니터링 여부와 무관하게 항상 실행)
        self.start_events()

    def cleanup_camera_resources(self, camera_key, reason="Unknown", stop_video=False):
        """
        단일 카메라에 대한 모든 리소스(스레드, 파이프라인, UI 상태)를 정리합니다.
        삭제, 모니터링 해제, 앱 종료 시 호출됩니다.
        stop_video=True일 때만 영상 파이프라인을 해제합니다.
        """
        logger.info(f"[Cleanup] Cleaning up camera {camera_key} (Reason: {reason}, Video={stop_video})")

        # 1. 이벤트 스레드 정리
        if camera_key in self.event_threads:
            threads = self.event_threads.pop(camera_key)
            for t_name, t in threads.items():
                if t:
                    t.stop()
                    # UI 스레드 블로킹 방지를 위해 짧게 대기하거나, 
                    # 스레드 내부에서 종료 플래그를 확인하도록 유도
                    t.wait(500) 
            logger.debug(f"[Cleanup] Event threads stopped for {camera_key}")

        # 2. 비디오 타일 정리 (파이프라인 정지 및 해제)
        if stop_video and camera_key in self.tiles:
            tile_data = self.tiles[camera_key]
            # 타일 딕셔너리에서는 제거하지 않고(그리드 재구성 시 처리), 리소스만 해제
            # 단, 삭제 시에는 타일도 의미가 없어지지만 rebuild_grid가 처리함.
            if tile_data.get('video'):
                tile_data['video'].stop()
                tile_data['video'].release()
            logger.debug(f"[Cleanup] Video resources released for {camera_key}")

    @Slot(str, bool, int)
    def on_camera_status_update(self, key, connected, count):
        """카메라 상태 워커 결과 처리"""
        if self._closing:
            return
            
        # 상태 저장
        self.camera_conn_status[key] = connected
        self.ui_dirty = True # 테이블 갱신 트리거

        for i in range(self.ui.camera_list.count()):
            item = self.ui.camera_list.item(i)
            if item.data(Qt.UserRole) == key:
                widget = self.ui.camera_list.itemWidget(item)
                if widget:
                    widget.update_device_info(connected, count)
                    widget.set_counts_visible(connected) # 연결 끊기면 LED 초기화
                break

    def _apply_grid_stretch(self, split_mode):
        """분할 모드에 따라 그리드 스트레치를 설정합니다."""
        # 1. 모든 스트레치 초기화 (잔여 설정 제거 - 범위를 넉넉하게 10까지)
        # 간헐적 깨짐 방지를 위해 명시적으로 0으로 초기화
        for r in range(10):
            self.ui.video_grid.setRowStretch(r, 0)
        for c in range(10):
            self.ui.video_grid.setColumnStretch(c, 0)

        # 2. 모드별 스트레치 적용
        if split_mode == 1:
            # 1분할: (0,0)이 있는 첫 번째 행/열에 비중 부여
            self.ui.video_grid.setRowStretch(0, 1)
            self.ui.video_grid.setColumnStretch(0, 1)
        else:
            # 4분할: 2x2 균등 분할
            self.ui.video_grid.setRowStretch(0, 1)
            self.ui.video_grid.setRowStretch(1, 1)
            self.ui.video_grid.setColumnStretch(0, 1)
            self.ui.video_grid.setColumnStretch(1, 1)

    def rebuild_grid(self, cameras):
        """카메라 수에 따라 그리드를 재구성합니다."""
        if self._rebuilding_grid:
            logger.warning("[Layout] rebuild_grid skipped (already running)")
            return
        self._rebuilding_grid = True

        # [CRITICAL FIX v3] Teardown phase (Build is delayed)
        try:
            # 1. 모든 스트림 정지
            self.stop_all_streams()
            QApplication.processEvents()

            # 2. 모든 VideoWidget pipeline NULL 상태까지 완전 종료
            for tile in list(self.tiles.values()):
                video = tile.get("video")
                if video:
                    video.safe_shutdown()

            QApplication.processEvents()

            # 3. Qt 위젯은 deleteLater 사용
            for i in reversed(range(self.ui.video_grid.count())):
                item = self.ui.video_grid.itemAt(i)
                widget = item.widget()
                if widget:
                    self.ui.video_grid.removeWidget(widget)
                    widget.setParent(None)
                    widget.deleteLater()

            self.tiles.clear()
            
            QApplication.processEvents()

            # 4. Build 예약 (다음 이벤트 루프에서 실행)
            self._pending_grid_cameras = cameras
            QTimer.singleShot(0, self._build_grid_after_teardown)

        except Exception as e:
            logger.error(f"[Layout] rebuild_grid error: {e}")
            self._rebuilding_grid = False

    def _build_grid_after_teardown(self):
        """지연 실행된 그리드 생성 (Segfault 방지)"""
        try:
            cameras = self._pending_grid_cameras or []
            self._pending_grid_cameras = None

            # 분할 모드 결정(기존 정책 유지)
            count = len(cameras)
            split_mode = 1 if count <= 1 else 4
            self.state_mgr.set("split_mode", str(split_mode))

            target_subtype = 0 if split_mode == 1 else 1
            self._apply_grid_stretch(split_mode)

            # 여기서부터 실제 타일 생성
            if split_mode == 1:
                if count > 0:
                    self._add_video_tile(cameras[0], 0, 0, row_span=2, col_span=2, force=True)
                else:
                    self._add_empty_tile(0, 0, row_span=2, col_span=2)
            else:
                positions = [(0,0), (0,1), (1,0), (1,1)]
                for i, pos in enumerate(positions):
                    if i < count:
                        self._add_video_tile(cameras[i], *pos, subtype=target_subtype, force=True)
                    else:
                        self._add_empty_tile(*pos)
            
            # [추가] 생성된 타일에 대해 ROI 적용 및 스트림 시작
            for key in self.tiles:
                if key in self.roi_cache:
                    self.roi_apply_to_video(key)
            
            self.start_all_streams()

        finally:
            self._rebuilding_grid = False

    def _add_video_tile(self, cam_data, row, col, subtype=None, row_span=1, col_span=1, force=False):
        if self._rebuilding_grid and not force:
            logger.debug("[Layout] tile creation delayed during rebuild")
            return

        key = cam_data['key']
        
        # 1. 타일 컨테이너 (테두리 담당)
        tile_frame = QFrame()
        tile_frame.setFrameShape(QFrame.Shape.Box)
        tile_frame.setLineWidth(1)
        tile_frame.setStyleSheet("border: 1px solid gray;")
        
        # 2. 메인 스택 (VideoContainer vs StatusLabel)
        main_stack = QStackedLayout(tile_frame)
        main_stack.setStackingMode(QStackedLayout.StackingMode.StackOne)
        
        # 3. Video Widget
        # ROI Overlay는 이제 VideoWidget 내부의 cairooverlay가 담당하므로 별도 위젯 불필요
        
        video_widget = VideoWidget(parent=tile_frame) # [CRITICAL FIX v3] Parent 지정
        video_widget.clicked.connect(self.on_video_tile_clicked)
        video_widget.doubleClicked.connect(self.on_video_double_clicked)
        
        # 4. 상태 라벨
        status_label = QLabel("STOPPED")
        status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status_label.setStyleSheet("background-color: black; color: white; font-size: 16px; font-weight: bold; border: none;")
        
        main_stack.addWidget(video_widget)
        main_stack.addWidget(status_label)
        main_stack.setCurrentIndex(1)  # 초기 상태는 STOPPED

        self.ui.video_grid.addWidget(tile_frame, row, col, row_span, col_span)
        
        self.tiles[key] = {
            'frame': tile_frame,
            'video': video_widget,
            'label': status_label,
            'layout': main_stack
        }
        
        # [Commit ROI-FIX-1] VideoWidget 생성 직후 ROI 캐시 적용 (레이스 해결)
        if key in self.roi_cache:
            try:
                self.roi_apply_to_video(key)
                logger.info(f"[ROI] Applied cached ROI to {key} during tile creation")
            except Exception as e:
                logger.warning(f"[ROI] Failed to apply cached ROI for {key}: {e}")
        
        # 서브타입 설정 (set_media 호출 전)
        if subtype is not None:
            video_widget.set_subtype(subtype)
            
        # 미디어 설정 (재생 준비)
        if self.config.has_section(key):
            cam_cfg = dict(self.config[key])
            url = build_rtsp_url(cam_cfg)
            # 실제 사용 URL 로그 (1회성)
            if not video_widget.is_ready: # 최초 설정 시에만 로그
                self.add_event_log(f"[DEBUG] [{key}] RTSP_URL={url}")
            video_widget.set_media(url, key)

    def _add_empty_tile(self, row, col, row_span=1, col_span=1):
        # 빈 타일도 모양 통일
        tile_frame = QFrame()
        tile_frame.setStyleSheet("border: 1px solid #333; background-color: black;")
        layout = QVBoxLayout(tile_frame)
        layout.setContentsMargins(0,0,0,0)
        
        label = QLabel("NO CAMERA")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: gray; border: none;")
        
        layout.addWidget(label)
        self.ui.video_grid.addWidget(tile_frame, row, col, row_span, col_span)

    def reset_video_grid_layout(self, reason="Unknown"):
        """
        레이아웃 상태를 강제로 초기화하고 복원합니다. (비율 깨짐 방지용 단일 API)
        """
        # 진단 로그
        if self.ui.chk_show_debug.isChecked():
            row_stretches = [self.ui.video_grid.rowStretch(r) for r in range(3)]
            col_stretches = [self.ui.video_grid.columnStretch(c) for c in range(3)]
            logger.info(f"[Layout] Reset reason={reason}, maximized={self.is_video_maximized}, key={self.maximized_camera_key}")
            logger.info(f"[Layout] Stretches before: rows={row_stretches}, cols={col_stretches}")
        else:
            logger.debug(f"[Layout] Resetting grid layout. Reason: {reason}")

        # 1. 확대 상태라면 복원 (기존 로직 활용)
        if self.is_video_maximized:
            # toggle_maximize_video가 내부적으로 복원 로직(remove->add)을 수행함
            self.toggle_maximize_video(self.maximized_camera_key)
            QApplication.processEvents()
            time.sleep(0.1)
            
        # 2. 상태 변수 강제 초기화 (안전장치)
        self.is_video_maximized = False
        self.maximized_camera_key = None
        self.maximized_tile_info = None
        self.roi_mode = "monitor"
        
        # 3. 모든 타일 보이기 및 ROI 편집 종료
        for key, tile in self.tiles.items():
            tile['frame'].setVisible(True)
            # 편집 모드 해제
            tile['video'].set_roi_edit(None, False)
            self.roi_apply_to_video(key)

        # 4. ROI UI 숨김
        self.ui.group_roi_edit.setVisible(False)

        # 5. 그리드 스트레치 완전 초기화 및 재설정
        raw = self.state_mgr.get("split_mode", "4")
        try:
            split_mode = int(raw)
        except (ValueError, TypeError):
            split_mode = 4
        if split_mode not in (1, 4):
            split_mode = 4
        self._apply_grid_stretch(split_mode)
        
        # 6. 레이아웃 갱신 강제
        self.ui.video_grid.invalidate()

    # ---------------------------------------------------------
    # ROI Editing Logic
    # ---------------------------------------------------------
    def on_video_tile_clicked(self, camera_key):
        # 일반 클릭은 선택 강조 등 (현재는 기능 없음)
        pass

    def on_video_double_clicked(self, camera_key):
        """영상 더블클릭 시 확대/복원 토글"""
        self.toggle_maximize_video(camera_key)

    def _schedule_rebind_visible(self):
        """보이는 타일들에 대해 윈도우 핸들 재바인딩을 예약 (디바운스)"""
        self._rebind_timer.start(100) # 100ms 후 실행 (디바운스 시간 확보)

    def _perform_rebind_visible(self):
        """실제 재바인딩 수행"""
        for tile in self.tiles.values():
            if tile['frame'].isVisible():
                tile['video'].rebind_window_handle()

    def toggle_maximize_video(self, camera_key):
        if self.is_video_maximized:
            # 복원
            if self.maximized_tile_info:
                # [중요] 복원 시에는 저장된 위치 정보를 사용하여 정확히 복구
                # removeWidget -> addWidget 순서 준수
                # stretch는 _apply_grid_stretch에서 일괄 처리하므로 여기서는 위젯 배치만 복구
                widget = self.maximized_tile_info['widget']
                row = self.maximized_tile_info['row']
                col = self.maximized_tile_info['col']
                rowSpan = self.maximized_tile_info.get('rowSpan', 1)
                colSpan = self.maximized_tile_info.get('colSpan', 1)
                
                # 그리드에서 제거 후 원래 위치로 복원
                self.ui.video_grid.removeWidget(widget)
                self.ui.video_grid.addWidget(widget, row, col, rowSpan, colSpan)

            # 모든 타일 보이기
            for k, tile in self.tiles.items():
                tile['frame'].setVisible(True)
            
            # 그리드 스트레치 복원 (현재 분할 모드에 맞게)
            raw = self.state_mgr.get("split_mode", "4")
            try:
                split_mode = int(raw)
            except (ValueError, TypeError):
                split_mode = 4
            if split_mode not in (1, 4):
                split_mode = 4
            self._apply_grid_stretch(split_mode)

            # ROI UI 숨김
            self.ui.group_roi_edit.setVisible(False)
            
            self.is_video_maximized = False
            self.maximized_camera_key = None
            self.maximized_tile_info = None
            self.roi_mode = "monitor"
            
            # 모든 비디오 모니터링 모드로 복귀
            for k in self.tiles.keys():
                self.roi_exit_edit(k, commit=False)
            
            # Rebind window handles (디바운스 적용)
            self._schedule_rebind_visible()
            # 상태바 갱신
            self.update_status_bar()
            
            QApplication.processEvents()
            time.sleep(0.1)

        else:
            # 확대
            if camera_key not in self.tiles: return
            
            tile_frame = self.tiles[camera_key]['frame']
            
            # 현재 위치 찾기
            idx = self.ui.video_grid.indexOf(tile_frame)
            row, col, rowSpan, colSpan = self.ui.video_grid.getItemPosition(idx)
            
            self.maximized_tile_info = {
                'widget': tile_frame,
                'row': row,
                'col': col,
                'rowSpan': rowSpan,
                'colSpan': colSpan
            }
            
            # 다른 위젯 숨기기 (removeWidget 대신 hide 사용)
            for k, tile in self.tiles.items():
                if k != camera_key:
                    tile['frame'].setVisible(False)
            
            # [수정] 타겟 타일을 그리드 전체(0,0,10,10)로 재배치하여 여백 제거
            self.ui.video_grid.removeWidget(tile_frame)
            self.ui.video_grid.addWidget(tile_frame, 0, 0, 10, 10)
            
            # 타겟 타일 보이기 (이미 보이지만 확실히)
            tile_frame.setVisible(True)

            # 스트레치 조정 (0,0에만 부여)
            for r in range(self.ui.video_grid.rowCount()):
                self.ui.video_grid.setRowStretch(r, 1 if r == 0 else 0)
            for c in range(self.ui.video_grid.columnCount()):
                self.ui.video_grid.setColumnStretch(c, 1 if c == 0 else 0)
            
            # ROI UI 표시
            cam = next((c for c in self.cfg_mgr.get_cameras() if c['key'] == camera_key), None)
            name = cam['name'] if cam else camera_key
            self.ui.lbl_roi_target.setText(f"Target: {name}")
            self.ui.group_roi_edit.setVisible(True)
            
            self.is_video_maximized = True
            self.maximized_camera_key = camera_key
            self.roi_mode = "view"
            
            # [수정] 확대 시 View 모드 진입 (전체 표시, 편집 불가)
            self.roi_apply_to_video(camera_key) # view 모드 적용 (전체 ROI 표시)
            self.tiles[camera_key]['video'].set_roi_edit(None, False) # 편집 불가 명시
            
            # Rebind window handle (디바운스 적용)
            self._schedule_rebind_visible()
            
            self.update_status_bar()

    def on_roi_area_clicked(self, area_id):
        # 확대 상태가 아니면 무시 (방어 코드)
        if not self.is_video_maximized:
            return
            
        self.current_roi_area = area_id
        
        # 확대 상태라면 해당 카메라의 편집 영역 변경 (저장 없이 전환)
        if self.is_video_maximized and self.maximized_camera_key:
            # [수정] 버튼 클릭 시 Edit 모드 진입
            self.roi_mode = "edit"
            self.roi_enter_edit(self.maximized_camera_key, area_id)
            self.roi_apply_to_video(self.maximized_camera_key) # 화면 갱신

    @Slot(object, object, object)
    def on_roi_loaded(self, key, data_8192, enabled_areas_list):
        """비동기 ROI 로드 완료 핸들러"""
        if self._closing:
            return
            
        # 데이터 타입 및 내용 진단 로그
        logger.info(f"[ROI][slot] recv key={key} type(data)={type(data_8192)} type(enabled)={type(enabled_areas_list)}")
        if isinstance(data_8192, dict):
            logger.info(f"[ROI][slot] keys={list(data_8192.keys())[:5]} counts={{k:len(v) for k,v in data_8192.items()}}")
        
        # List -> Set 복원
        enabled_areas = set(enabled_areas_list) if isinstance(enabled_areas_list, list) else set()

        # 8192 -> Normalized 변환하여 캐시 저장
        norm_by_area = {}
        for aid, pts in data_8192.items():
            norm_pts = []
            for x, y in pts:
                norm_pts.append((x / 8192.0, y / 8192.0))
            norm_by_area[aid] = norm_pts
            
        self.roi_cache[key] = {
            'norm': norm_by_area,
            'enabled': enabled_areas
        }
        
        # 오버레이 갱신 시도
        if key in self.tiles:
            self.roi_apply_to_video(key)

    # ---------------------------------------------------------
    # ROI Control API (Unified)
    # ---------------------------------------------------------
    def roi_apply_to_video(self, camera_key):
        """캐시된 ROI 데이터를 VideoWidget에 반영 (모니터링 모드)"""
        if camera_key not in self.tiles or camera_key not in self.roi_cache:
            return

        cache = self.roi_cache[camera_key]
        norm_by_area = cache.get('norm', {})
        enabled_by_area = cache.get('enabled', set())
        
        # [수정] 모드에 따른 표시 데이터 필터링
        display_norm = norm_by_area
        display_enabled = enabled_by_area
        
        if self.roi_mode == "edit":
            # Edit 모드: 현재 선택된 Area만 표시
            target_area = self.current_roi_area
            display_norm = {target_area: norm_by_area.get(target_area, [])}
            # 편집 중인 영역은 무조건 보이게 처리 (enabled 목록에 없어도)
            display_enabled = {target_area}
            
        video = self.tiles[camera_key]['video']
        video.set_roi_regions(display_norm, display_enabled)
        video.set_roi_visible(True)
        
        # 검증 로그
        if self.ui.chk_show_debug.isChecked():
            r_norm, r_en = video.get_roi_regions()
            logger.debug(f"[ROI] Applied to {camera_key} (Mode={self.roi_mode}): areas={list(r_norm.keys())}")

    def roi_enter_edit(self, camera_key, area_id):
        """편집 모드 진입: 백업 생성 후 VideoWidget 편집 활성화"""
        if camera_key not in self.tiles: return
        
        # 캐시가 없으면 빈 상태로 초기화
        if camera_key not in self.roi_cache:
            self.roi_cache[camera_key] = {'norm': {}, 'enabled': set()}
            
        # 백업 생성 (편집 진입 시점의 상태 저장)
        # 이미 백업이 있고 편집 중이라면 덮어쓰지 않음 (Area 전환 시 백업 유지)
        # 하지만 여기서는 단순화를 위해 진입 시마다 백업하거나, 
        # toggle_maximize에서 진입 시 1회 백업하는 것이 좋음.
        # 현재 구조상 toggle_maximize에서 호출되므로, 백업이 없으면 생성.
        if camera_key not in self.roi_backup_cache:
            import copy
            self.roi_backup_cache[camera_key] = copy.deepcopy(self.roi_cache[camera_key])

        video = self.tiles[camera_key]['video']
        video.set_roi_edit(area_id, True)

    def roi_exit_edit(self, camera_key, commit=False):
        """편집 모드 종료: 저장(commit) 또는 취소(rollback)"""
        if camera_key not in self.tiles: return
        
        if not commit:
            # 롤백: 백업된 데이터로 캐시 복원
            if camera_key in self.roi_backup_cache:
                self.roi_cache[camera_key] = self.roi_backup_cache[camera_key]
                del self.roi_backup_cache[camera_key]
        else:
            # 커밋: 백업 삭제 (현재 상태 확정)
            if camera_key in self.roi_backup_cache:
                del self.roi_backup_cache[camera_key]
        
        # VideoWidget에 최종 상태 적용 및 편집 모드 해제
        video = self.tiles[camera_key]['video']
        video.set_roi_edit(None, False)
        self.roi_apply_to_video(camera_key)

    def on_roi_save(self):
        if not self.is_video_maximized or not self.maximized_camera_key:
            return
            
        cam_key = self.maximized_camera_key
        cam = next((c for c in self.cfg_mgr.get_cameras() if c['key'] == cam_key), None)
        if not cam: return
        
        video = self.tiles[cam_key]['video']
        
        # Get Normalized Points from current edit area
        points_norm = video.get_roi_edit_points_norm()
        
        # [방어 로직] 포인트가 없거나 너무 적으면 저장 중단
        if not points_norm or len(points_norm) < 3:
            logger.warning(f"[ROI] Save aborted: no points loaded/edited (len={len(points_norm)})")
            QMessageBox.warning(self, "ROI", "영역 좌표가 유효하지 않습니다.")
            return
        
        # Convert Normalized -> 8192
        points_8192 = []
        for nx, ny in points_norm:
            x = max(0, min(8192, int(nx * 8192.0)))
            y = max(0, min(8192, int(ny * 8192.0)))
            points_8192.append((x, y))
            
        # Find Rule Index & Set
        rule_idx = cgi_client.get_rule_index_for_area(cam['ip'], cam['username'], cam['password'], self.current_roi_area)
        if rule_idx is None:
            QMessageBox.warning(self, "ROI", "Rule Index를 찾을 수 없습니다.")
            return
        
        # CGI 호출
        ok = cgi_client.set_detect_region(cam['ip'], cam['username'], cam['password'], rule_idx, points_8192)
        
        if ok:
            # 1. 캐시 업데이트 (현재 편집된 좌표를 캐시에 반영)
            if cam_key in self.roi_cache:
                self.roi_cache[cam_key]['norm'][self.current_roi_area] = list(points_norm)
            
            # 2. 모드 전환 (View) 및 편집 종료 (Commit)
            # 저장 성공 시 View 모드로 복귀하며 전체 ROI를 표시해야 함
            self.roi_mode = "view"
            self.roi_exit_edit(cam_key, commit=True)
            
            # 3. UI 즉시 갱신 보장 (메시지 박스 뜨기 전)
            QApplication.processEvents()
            
            QMessageBox.information(self, "ROI", "적용되었습니다.")
        else:
            QMessageBox.warning(self, "ROI", "적용 실패")

    def on_roi_cancel(self):
        if self.is_video_maximized and self.maximized_camera_key:
            cam_key = self.maximized_camera_key
            
            # 모드 전환 (View) 및 편집 종료 (Rollback)
            # 취소 시 View 모드로 복귀하며 원래 상태(전체 ROI)로 되돌림
            self.roi_mode = "view"
            self.roi_exit_edit(cam_key, commit=False)

    def _restore_app_state(self):
        # 1. 마지막 선택 카메라 복원
        last_camera_key = self.state_mgr.get("last_camera_key")
        if last_camera_key:
            # 리스트 선택 복원 (이벤트 발생 -> 하이라이트 처리됨)
            items = self.ui.camera_list.findItems(last_camera_key, Qt.MatchFlag.MatchContains) # MatchContains is loose, but key is in UserRole
            # UserRole 검색이 정확하므로 순회
            for i in range(self.ui.camera_list.count()):
                item = self.ui.camera_list.item(i)
                if item.data(Qt.UserRole) == last_camera_key:
                    self.ui.camera_list.setCurrentItem(item)
                    break
        
        # 앱 시작 시 자동 재생 (옵션)
        # Windows UX: 앱 시작 시에는 모니터링 자동 시작 안 함 (사용자가 Start 눌러야 함)
        # 단, 이벤트 수집은 항상 실행
        
        # 2. 상태바 업데이트
        self.update_status_bar()

    def on_camera_list_selected(self, current, previous):
        """카메라 선택 시 해당 타일 강조"""
        if not current:
            return
        
        camera_key = current.data(Qt.UserRole)
        if camera_key:
            # 상태 저장 (선택된 카메라)
            self.state_mgr.set("last_camera_key", camera_key)
            # 타일 강조 (1121 스타일: 영상 타일 선택 표시 안 함)
            
            # 우측 정보 패널 채우기
            cameras = self.cfg_mgr.get_cameras()
            cam = next((c for c in cameras if c['key'] == camera_key), None)
            if cam:
                self.ui.edit_name.setText(cam.get('name', ''))
                self.ui.edit_ip.setText(cam.get('ip', ''))
                self.ui.edit_port.setText(str(cam.get('http_port', '')))
                self.ui.edit_id.setText(cam.get('username', ''))
                self.ui.edit_pw.setText(cam.get('password', ''))
        
        self.update_status_bar()

    def highlight_tile(self, target_key):
        """특정 키의 위젯만 테두리 강조"""
        # 1121 스타일: 영상 타일에는 선택 테두리를 표시하지 않음
        pass

    def get_selected_monitor_cameras(self):
        """체크된 모니터링 대상 카메라 목록 반환"""
        selected = []
        all_cameras = self.cfg_mgr.get_cameras()
        
        # UI 리스트 위젯을 순회하며 체크 상태 확인
        for i in range(self.ui.camera_list.count()):
            item = self.ui.camera_list.item(i)
            widget = self.ui.camera_list.itemWidget(item)
            if widget and hasattr(widget, 'chk_monitor') and widget.chk_monitor.isChecked():
                key = item.data(Qt.UserRole)
                cam = next((c for c in all_cameras if c['key'] == key), None)
                if cam:
                    selected.append(cam)
        return selected

    def on_tab_changed(self, index):
        """탭 변경 시 호출"""
        # Monitoring 탭(인덱스 1)으로 진입 시 레이아웃 리셋
        if index == 1:
            self.reset_video_grid_layout("TabEnter_Monitoring")

    def on_btn_start_clicked(self):
        """모니터링 시작: 체크된 카메라만 수집하여 시작"""
        if getattr(self, "_starting_monitor", False):
            return
        self._starting_monitor = True

        try:
            # [Critical Stability Patch] 확대 상태 완전 해제 (GStreamer 안정화)
            if self.is_video_maximized and self.maximized_camera_key:
                self.toggle_maximize_video(self.maximized_camera_key)
                QApplication.processEvents()
                time.sleep(0.2)
            
            # 안전을 위해 한번 더 처리
            QApplication.processEvents()

            self.stop_all_streams()
            
            # 체크된 카메라 수집
            target_cameras = self.get_selected_monitor_cameras()
            if not target_cameras:
                logger.info("[Main] No cameras selected for monitoring.")
                self.add_event_log("[Main] No cameras selected for monitoring.")
                return
            
            # [추가] 시작 전 레이아웃 초기화 (깨짐 방지)
            self.reset_video_grid_layout("StartMonitoring")

            # [수정] 현재 선택된 카메라만 모니터링 상태로 저장 (나머지는 해제)
            selected_keys = [cam['key'] for cam in target_cameras]
            self.state_mgr.set_monitor_enabled_bulk(selected_keys)
            
            self.rebuild_grid(target_cameras)
            # self.start_all_streams() # [CRITICAL FIX v3] rebuild_grid 내부(지연 실행)로 이동됨
            self.ui.tabs.setCurrentIndex(1) # 모니터링 탭으로 이동
            
            # ROI 데이터 비동기 로드 시작
            self.roi_cache.clear()
            for cam in target_cameras:
                worker = RoiLoadWorker(cam['key'], cam['ip'], cam['username'], cam['password'])
                worker.signals.result.connect(self.on_roi_loaded)
                self.threadpool.start(worker)
        finally:
            self._starting_monitor = False

    def on_btn_stop_clicked(self):
        # [수정] 정지 시 레이아웃 완전 초기화 (확대 복원 포함)
        self.reset_video_grid_layout("StopMonitoring")

        self.stop_all_streams()
        # 그리드 초기화 (STOPPED 상태 표시를 위해 빈 타일이나 현재 상태 유지)
        # 여기서는 stop_all_streams가 STOPPED 라벨을 보여주므로 그대로 둠
        
        # [수정] 모니터링 상태 해제 및 UI 체크박스 즉시 해제
        self.state_mgr.clear_all_monitor_enabled()
        
        # [수정] 이벤트 스레드는 유지 (영상만 정지, 이벤트 수신/DB 저장은 계속)
        # self.stop_events() 호출 제거

        for i in range(self.ui.camera_list.count()):
            item = self.ui.camera_list.item(i)
            widget = self.ui.camera_list.itemWidget(item)
            if widget and hasattr(widget, 'chk_monitor'):
                # 시그널 차단하여 불필요한 개별 저장 방지 (이미 clear_all 했으므로)
                widget.chk_monitor.blockSignals(True)
                widget.chk_monitor.setChecked(False)
                widget.chk_monitor.blockSignals(False)
        
        # [Commit M2-3] Stop 시 Keep Watching 리셋 (자동 종료 방지 옵션 해제)
        if self.ui.chk_keep_watching.isChecked():
            self.ui.chk_keep_watching.blockSignals(True)
            self.ui.chk_keep_watching.setChecked(False)
            self.ui.chk_keep_watching.blockSignals(False)

        self.update_status_bar()
        
        self.ui.tabs.setCurrentIndex(0) # 설정 탭으로 이동

    def on_btn_add_clicked(self):
        info = {
            'name': self.ui.edit_name.text(),
            'ip': self.ui.edit_ip.text(),
            'port': self.ui.edit_port.text(),
            'id': self.ui.edit_id.text(),
            'pw': self.ui.edit_pw.text()
        }
        self.cfg_mgr.add_camera(info)
        self.reload_cameras()

    def on_btn_modify_clicked(self):
        current = self.ui.camera_list.currentItem()
        if not current: return
        key = current.data(Qt.UserRole)
        info = {
            'name': self.ui.edit_name.text(),
            'ip': self.ui.edit_ip.text(),
            'port': self.ui.edit_port.text(),
            'id': self.ui.edit_id.text(),
            'pw': self.ui.edit_pw.text()
        }
        self.cfg_mgr.update_camera(key, info)
        self.reload_cameras()

    def on_btn_delete_clicked(self):
        current = self.ui.camera_list.currentItem()
        if not current: return
        key = current.data(Qt.UserRole)
        
        # [추가] 삭제 전 리소스 확실히 정리
        self.cleanup_camera_resources(key, reason="Delete", stop_video=True)

        # [수정] 데이터 및 상태 즉시 제거 (재시작 없이 반영)
        if key in self.realtime_counts: del self.realtime_counts[key]
        if key in self._last_people_total: del self._last_people_total[key]
        if key in self.discovered_areas: del self.discovered_areas[key]
        if key in self.camera_conn_status: del self.camera_conn_status[key]
        if key in self.camera_items: del self.camera_items[key]
        
        # UI 리스트에서 즉시 제거
        row = self.ui.camera_list.row(current)
        self.ui.camera_list.takeItem(row)
        
        self.cfg_mgr.delete_camera(key)
        
        # 상태 정리 (Area 체크 등)
        self.state_mgr.cleanup_camera_state(key)
        
        # 테이블 즉시 갱신 (삭제된 카메라가 안 나오도록)
        self.ui_dirty = True
        self.update_monitoring_tables()

        # 입력창 초기화
        self.ui.edit_name.clear()
        self.ui.edit_ip.clear()
        self.ui.edit_port.clear()
        self.ui.edit_id.clear()
        self.ui.edit_pw.clear()
        
        # reload_cameras() 호출 제거: 위에서 수동으로 정리했으므로 불필요한 전체 리로드 방지

    def start_all_streams(self):
        """모든 타일의 스트림 재생 시작"""
        # 1. 영상 재생
        for tile_data in self.tiles.values():
            video = tile_data['video']
            layout = tile_data['layout']
            
            layout.setCurrentIndex(0)  # 영상 표시
            try:
                video.play()
            except Exception as e:
                logger.error(f"[Main] Start Error: {e}")
        
        # 2. 이벤트 구독 시작 (설정 확인)
        # 이벤트 스레드는 앱 실행 시 이미 시작되었으므로 여기서는 건드리지 않음
            
        self.update_status_bar()

    def start_events(self):
        """활성화된 카메라에 대해 이벤트 구독 스레드 시작"""
        if not self.config.getboolean('event', 'enable', fallback=True):
            return

        count = 0
        # 타일(영상) 여부와 상관없이 모든 설정된 카메라에 대해 이벤트 수집
        cameras = self.cfg_mgr.get_cameras()
        
        for cam_cfg in cameras:
            key = cam_cfg['key']
            
            # 스레드 관리 딕셔너리 확보
            if key not in self.event_threads:
                self.event_threads[key] = {}
            
            threads = self.event_threads[key]
        
            ip = cam_cfg.get('ip')
            port = cam_cfg.get('http_port', 80)
            user = cam_cfg.get('username')
            password = cam_cfg.get('password')
            channel = cam_cfg.get('channel', 1)
            
            # 1. People Count Thread
            pt = threads.get("people")
            if pt and pt.isRunning():
                pass # 이미 실행 중
            elif pt:
                # 존재하지만 종료됨 -> 재시작
                logger.debug(f"[Main] Restarting dead PeopleCountThread for {key}")
                pt.restart()
            else:
                # 신규 생성
                pc_thread = PeopleCountThread(key, ip, port, user, password, channel)
                pc_thread.event_received.connect(self.on_new_event)
                pc_thread.start()
                threads["people"] = pc_thread
            
            # 2. Stay Detection Thread
            st = threads.get("stay")
            if st and st.isRunning():
                pass
            elif st:
                logger.debug(f"[Main] Restarting dead StayDetectionThread for {key}")
                st.restart()
            else:
                stay_thread = StayDetectionThread(key, ip, port, user, password)
                stay_thread.event_received.connect(self.on_new_event)
                stay_thread.start()
                threads["stay"] = stay_thread
            
            count += 1
        
        if count > 0:
            self.add_event_log(f"[DEBUG] Event threads check/start complete for {count} cameras")

    def stop_all_streams(self):
        """모든 타일 정지"""
        # 영상 정지
        for tile_data in self.tiles.values():
            video = tile_data['video']
            layout = tile_data['layout']
            
            video.stop()
            layout.setCurrentIndex(1)  # STOPPED 라벨 표시

    def stop_events(self):
        """모든 이벤트 스레드 중지"""
        if not self.event_threads:
            return
            
        # cleanup_camera_resources를 활용하여 전체 정리
        keys = list(self.event_threads.keys())
        count = len(keys)
        for key in keys:
            # 내부에서 event_threads.pop 수행
            self.cleanup_camera_resources(key, reason="StopAllEvents", stop_video=False)
            
        # 혹시 남아있는 항목이 있다면 클리어
        self.event_threads.clear()
        self.add_event_log(f"[DEBUG] Stopped event threads for {count} cameras")
        self.add_event_log("[DEBUG] THREAD CLEANUP OK")

    def register_discovered_area(self, camera_key, area_id):
        """새로운 Area ID가 발견되면 UI에 추가하고 상태를 복원합니다."""
        if not camera_key or area_id is None:
            return

        # 초기화
        if camera_key not in self.discovered_areas:
            self.discovered_areas[camera_key] = set()

        # 이미 등록된 Area면 무시
        if area_id in self.discovered_areas[camera_key]:
            return

        self.discovered_areas[camera_key].add(area_id)

        # CameraListItem의 라벨 업데이트
        for i in range(self.ui.camera_list.count()):
            item = self.ui.camera_list.item(i)
            if item.data(Qt.UserRole) == camera_key:
                widget = self.ui.camera_list.itemWidget(item)
                if widget:
                    widget.update_area_count(len(self.discovered_areas[camera_key]))
                break

    @Slot(str, int, bool)
    def on_card_area_changed(self, camera_key, area_id, checked):
        """카메라 리스트 아이템의 Area 체크박스 변경 처리"""
        self.state_mgr.set_area_enabled(camera_key, area_id, checked)
        # [수정] 체크박스 변경은 "설정된 영역" 개수(카메라 설정)에 영향을 주지 않음

    def is_area_checked(self, camera_key, area_id):
        """영역 체크 여부 확인 (Windows 규칙: Checked=Stay, Unchecked=People)"""
        return self.state_mgr.get_area_enabled(camera_key, area_id)

    @Slot(dict)
    def on_new_event(self, event_data):
        """이벤트 수신 처리 슬롯"""
        evt_type = event_data.get('type')
        
        display_msg = None
        should_log_to_db = False
        cam_key = event_data.get('camera_key')
        
        
        # 1. DEBUG 메시지 처리
        if evt_type == "DEBUG":
            # DEBUG 로그는 DB 저장 생략 (필요시 주석 해제)
            # db_module.enqueue_event(event_data) 
            if not self.ui.chk_show_debug.isChecked():
                return
                

            display_msg = f"[DEBUG] {event_data.get('message')}"
            self.add_event_log(display_msg)
            return

        # 공통 타임스탬프 (Epoch)
        if 'ts_epoch' not in event_data:
            event_data['ts_epoch'] = int(time.time())

        if evt_type == "PEOPLE_COUNT":
            self._handle_people_count(event_data)
        elif evt_type == "STAY_ALARM":
            self._handle_stay_alarm(event_data)

    def _handle_people_count(self, event_data):
        """PeopleCount 이벤트 처리 (Windows 규칙 적용)"""
        cam_key = event_data.get('camera_key')
        area_id = event_data.get('area_id')
        current_count = event_data.get('count')
        
        # 1. 캐시 초기화 및 조회
        if cam_key not in self._last_people_total:
            self._last_people_total[cam_key] = {}
        
        # 유효성 검사
        if current_count is None or current_count < 0:
            return

        last_total = self._last_people_total[cam_key].get(area_id)
        
        # 2. UI 실시간 카운트 갱신 (항상 수행)
        if cam_key not in self.realtime_counts:
            self.realtime_counts[cam_key] = {}
        self.realtime_counts[cam_key][area_id] = current_count
        self.ui_dirty = True
        
        # 최초 수신(None)이면 트리거/DB기록 없이 리턴 (기준점 설정)
        if last_total is None:
            self._last_people_total[cam_key][area_id] = current_count
            # [Commit 23-1] Init 이벤트는 DB에 저장하지 않음 (기준점만 설정)
            if self.ui.chk_show_debug.isChecked():
                self.add_event_log(f"[DEBUG] [{cam_key}] Area {area_id} People Init ({current_count}) - Not saved to DB")
            return

        delta = current_count - last_total
        
        # 4. DB 기록 및 UI 로그 (값 변화가 있을 때만)
        if delta != 0:
            event_data['prev_value'] = last_total
            event_data['count'] = current_count
            event_data['delta'] = delta # [Commit 22-2] 명시적 delta 전달
            
            # 메시지 생성
            if delta > 0:
                event_data['message'] = f"[{cam_key}] Area {area_id} People +{delta} ({current_count})"
            else:
                event_data['message'] = f"[{cam_key}] Area {area_id} People {delta} ({current_count})"
            
            db_module.enqueue_event(event_data)
            
            # UI 로그 (증가 시에만, 쿨다운 적용)
            if delta > 0:
                state_key = (cam_key, area_id)
                now = time.time()
                last_emit_ts = self.last_event_timestamps.get(state_key, 0)
                
                if (now - last_emit_ts) >= self.event_cooldown_sec:
                    self.last_event_timestamps[state_key] = now
                    self.add_event_log(event_data['message'])
                    self.total_events += 1
                    self.update_status_bar()
                    
                    if self.ui.chk_show_debug.isChecked():
                        self.add_event_log(f"[DEBUG] PEOPLE {cam_key} A{area_id} raw={current_count} prev={last_total} delta={delta}")

        # 5. GPIO 트리거 (Windows 규칙)
        # 정책: Area 체크 OFF일 때 인원수 증가(delta>0) 시 GPIO17 펄스
        if delta > 0:
            if self._closing:
                return
            
            is_checked = self.is_area_checked(cam_key, area_id)
            # 체크 OFF일 때만 동작
            if not is_checked:
                if self._check_gpio_debounce(cam_key, area_id, "people", 0.3):
                    # 포맷: ip / AreaN / Δx / now=y
                    cam_ip = self._get_camera_ip(cam_key)
                    msg = f"{cam_ip} / Area{area_id} / Δ{delta} / now={current_count}"
                    self.add_gpio_log(msg)
                    self.gpio_bridge.trigger_pulse(int(area_id))

        # 7. UI LED 업데이트 (카메라 카드)
        if cam_key in self.camera_items:
            self.camera_items[cam_key].set_area_count(area_id, current_count)
            
        # 6. 캐시 업데이트
        self._last_people_total[cam_key][area_id] = current_count

    def _handle_stay_alarm(self, event_data):
        """StayDetection 이벤트 처리 (Windows 규칙 적용)"""
        cam_key = event_data.get('camera_key')
        area_id = event_data.get('area_id')
        action = event_data.get('action')
        
        # 1. GPIO 트리거 (Windows 규칙)
        # 정책: Area 체크 ON일 때 체류알람 Start 시 GPIO17 펄스
        if action == 'Start':
            if self._closing:
                return

            is_checked = self.is_area_checked(cam_key, area_id)
            # 체크 ON일 때만 동작
            if is_checked:
                if self._check_gpio_debounce(cam_key, area_id, "stay", 0.3):
                    # 포맷: ip / AreaN / now=y / [횡단 대기]
                    cam_ip = self._get_camera_ip(cam_key)
                    # Stay 이벤트에는 현재 인원수가 없으므로 캐시된 값 사용
                    curr_cnt = self.realtime_counts.get(cam_key, {}).get(area_id, 0)
                    msg = f"{cam_ip} / Area{area_id} / now={curr_cnt} / [횡단 대기]"
                    self.add_gpio_log(msg)
                    self.gpio_bridge.trigger_pulse(int(area_id))

        # 2. 로그 및 상태 관리 (기존 로직 유지)
        if action == 'Start':
            state_key = (cam_key, area_id)
            cooldown_key = (cam_key, area_id, action)
            now = time.time()

            # 로그/DB 기록 (쿨다운 적용)
            last_emit = self.stay_last_emit.get(cooldown_key, 0)
            if (now - last_emit) >= self.stay_cooldown_sec:
                msg = f"체류 감지 이벤트 수신: {cam_key} Area {area_id} Action:Start"
                event_data['message'] = msg
                db_module.enqueue_event(event_data)
                self.add_event_log(msg)
                self.total_events += 1
                self.update_status_bar()
                self.stay_last_emit[cooldown_key] = now

            # 상태 변경 (항상 True)
            self.stay_states[state_key] = True

            # 자동 해제 타이머 설정
            if state_key in self._stay_clear_timers:
                self._stay_clear_timers[state_key].stop()
            
            timer = QTimer(self)
            timer.setSingleShot(True)
            # lambda를 사용하여 인자 전달
            timer.timeout.connect(lambda sk=state_key: self._clear_stay_state(sk[0], sk[1]))
            timer.start(self.stay_hold_ms)
            self._stay_clear_timers[state_key] = timer

    def _get_camera_ip(self, key):
        """카메라 키로 IP 조회"""
        cam = next((c for c in self.cfg_mgr.get_cameras() if c['key'] == key), None)
        return cam['ip'] if cam else "Unknown"

    @Slot(str, int)
    def _clear_stay_state(self, cam_key, area_id):
        """STAY 상태를 자동으로 OFF로 변경하는 타이머 콜백"""
        state_key = (cam_key, area_id)
        if self.stay_states.get(state_key, False):
            self.stay_states[state_key] = False
            # UI/DB에 기록하지 않음 (노이즈 제거)
            # 타이머 객체 참조 제거
            if state_key in self._stay_clear_timers:
                del self._stay_clear_timers[state_key]

    def _check_gpio_debounce(self, cam_key, area_id, event_type, debounce_sec=0.3):
        """GPIO 중복 트리거 방지 (짧은 디바운스)"""
        key = (cam_key, area_id, event_type)
        now = time.time()
        last_ts = self.gpio_last_trigger_ts.get(key, 0)
        if (now - last_ts) < debounce_sec:
            return False
        self.gpio_last_trigger_ts[key] = now
        return True

    def on_gpio_test_clicked(self):
        """GPIO 테스트 버튼 클릭 시 Area 1 (GPIO 17) 펄스 발생"""
        try:
            # 무조건 GPIO 17 출력 (내부에서 area_id 무시됨)
            self.gpio_bridge.trigger_pulse(1) 
            self.add_gpio_log("[User] Test Pulse Triggered (GPIO17)")
        except Exception as e:
            self.add_gpio_log(f"[User] Test Pulse Failed: {e}")

    def on_gpio_disconnect_clicked(self):
        """GPIO 연결 해제 버튼"""
        self.gpio_bridge.cleanup()
        self._update_gpio_status_ui()
        self.add_gpio_log("[User] GPIO Disconnected")

    def on_gpio_connect_clicked(self):
        """GPIO 재연결 버튼"""
        self.gpio_bridge.setup()
        self._update_gpio_status_ui()
        if self.gpio_bridge.is_connected:
            self.add_gpio_log("[User] GPIO Connected")
        else:
            self.add_gpio_log("[User] GPIO Connection Failed")

    def _update_gpio_status_ui(self):
        """GPIO 연결 상태 라벨 갱신"""
        if not hasattr(self.ui, 'lbl_gpio_status'): return
        
        if not self.gpio_bridge.has_gpio:
             self.ui.lbl_gpio_status.setText("GPIO: Mock (No HW)")
             self.ui.lbl_gpio_status.setStyleSheet("color: orange; font-weight: bold;")
             return

        if self.gpio_bridge.is_connected:
            self.ui.lbl_gpio_status.setText("GPIO: Connected")
            self.ui.lbl_gpio_status.setStyleSheet("color: green; font-weight: bold;")
        else:
            self.ui.lbl_gpio_status.setText("GPIO: Disconnected")
            self.ui.lbl_gpio_status.setStyleSheet("color: red; font-weight: bold;")

    def add_gpio_log(self, msg):
        """GPIO 전용 로그 박스에 메시지 추가"""
        if not hasattr(self.ui, 'gpio_text') or not self.ui.gpio_text:
            return
        
        # [수정] 스크롤바가 맨 아래에 있는지 확인 (오차 범위 10px)
        scrollbar = self.ui.gpio_text.verticalScrollBar()
        was_at_bottom = scrollbar.value() >= (scrollbar.maximum() - 10)

        ts = time.strftime("%H:%M:%S")
        log_line = f"[{ts}] {msg}"
        self.ui.gpio_text.append(log_line)
        
        # [수정] 사용자가 맨 아래를 보고 있었을 때만 자동 스크롤
        if was_at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def on_area_item_changed(self, item, column):
        """Area 체크박스 상태 변경 시 호출"""
        parent = item.parent()
        if parent: # 카메라는 parent가 없음 (Top level)
            camera_key = parent.data(0, Qt.UserRole)
            area_id = item.data(0, Qt.UserRole)
            is_checked = (item.checkState(0) == Qt.Checked)
                        
            self.state_mgr.set_area_enabled(camera_key, area_id, is_checked)

    def add_event_log(self, msg, ts=None, write_file_log=True):
        """로그 리스트에 추가하고 최대 개수 유지"""
        # DEBUG 필터링 (UI 표시용)
        if msg.startswith("[DEBUG]") and not self.ui.chk_show_debug.isChecked():

            return

        if ts is None:
            ts = time.strftime("%H:%M:%S")
        
        # DB에서 불러온 전체 날짜시간 문자열인 경우 시간만 표시
        display_ts = ts
        if len(ts) > 10 and ' ' in ts:
            display_ts = ts.split(' ')[1]
            
        # [수정] 스크롤바가 맨 아래에 있는지 확인 (오차 범위 10px)
        scrollbar = self.ui.list_events.verticalScrollBar()
        was_at_bottom = scrollbar.value() >= (scrollbar.maximum() - 10)

        item = QListWidgetItem(f"[{display_ts}] {msg}")
        
        # 알람 종류에 따라 색상 (선택)
        if "ALARM" in msg:
            item.setForeground(Qt.GlobalColor.red)
            
        # [수정] 최신 로그를 아래에 추가 (append)
        self.ui.list_events.addItem(item)
        
        # [수정] 최대 개수 초과 시 가장 오래된(맨 위) 항목 제거
        while self.ui.list_events.count() > 200:
            self.ui.list_events.takeItem(0)
            
        # [수정] 사용자가 맨 아래를 보고 있었을 때만 자동 스크롤
        if was_at_bottom:
            self.ui.list_events.scrollToBottom()
            
        # 시스템 로그에도 기록 (DEBUG 레벨)
        if write_file_log:
            # [Commit REC-5] GUI_LOG rate-limit + 중요도 분리
            if msg.startswith("[DEBUG]"):
                # [FIX] 메시지 템플릿 기반 키 생성 (뭉개짐 방지)
                context_key = "global"
                match = re.search(r'(?:camera[=\s]|\[)(camera\d+)', msg)
                if match:
                    context_key = match.group(1)
                
                # 숫자를 #으로 치환하여 메시지 템플릿 생성
                template = re.sub(r'\d', '#', msg)
                template_hash = hashlib.sha1(template.encode('utf-8')).hexdigest()[:8]

                rate_limit_key = f"gui_log_{context_key}_{template_hash}"

                allow, suppressed = should_log(rate_limit_key, 10)
                if allow:
                    suffix = f" (suppressed {suppressed})" if suppressed > 0 else ""
                    logger.debug(f"[GUI_LOG] {msg}{suffix}")
            else:
                # 중요 메시지([Main], [System] 등)는 제한 없이 기록
                logger.debug(f"[GUI_LOG] {msg}")

    def load_recent_events(self):
        """DB에서 최근 이벤트를 불러와 UI에 표시"""

        limit = self.log_load_limit
        events = db_module.get_recent_events(limit)
        
        self.add_event_log(f"[DEBUG] Loaded last {len(events)} events (Limit: {limit})")
        
        # get_recent_events는 최신순(DESC)으로 반환함. (최신=인덱스0)
        # add_event_log가 아래로 추가(append)하므로,
        # 과거 데이터부터 순서대로 넣어야 시간순(과거->최신)으로 쌓임.
        for evt in reversed(events):
            msg = evt.get('message')
            ts = evt.get('ts')
            if msg:
                self.add_event_log(msg, ts=ts, write_file_log=False)

    def reload_recent_events_filter(self, state):
        """DEBUG 체크박스 변경 시 리스트 갱신"""


        self.ui.list_events.clear()
        # 현재 메모리에 있는 로그를 다시 필터링해서 보여주는 것이 아니라,
        # DB에서 다시 로드하여 필터를 적용함
        self.load_recent_events()

    def update_status_bar(self):
        cam_count = self.ui.camera_list.count()
        if cam_count == 1 and self.ui.camera_list.item(0).text() == "No Camera":
            cam_count = 0
            
        split_mode = self.state_mgr.get("split_mode", "4")
        current = self.ui.camera_list.currentItem()
        selected_cam = current.data(Qt.UserRole) if current else None
        
        # 스트리밍 상태 확인 (하나라도 재생 중이면 ON)
        is_streaming = "OFF"
        for tile_data in self.tiles.values():
            widget = tile_data['video']
            if widget.is_ready and widget.is_playing():
                is_streaming = "ON"
                break

        msg = f"Cameras: {cam_count} | Split: {split_mode} | Streaming: {is_streaming} | Events: {self.total_events}"
        self.ui.status_bar.showMessage(msg)

    def _set_html_keep_scroll(self, widget, html):
        """HTML을 설정하되 스크롤 위치를 유지합니다."""
        scrollbar = widget.verticalScrollBar()
        current_value = scrollbar.value()
        is_at_bottom = (current_value == scrollbar.maximum())
        
        widget.setHtml(html)
        
        # 내용이 변경되어 스크롤 범위가 바뀔 수 있으므로 처리
        if is_at_bottom:
            scrollbar.setValue(scrollbar.maximum())
        else:
            scrollbar.setValue(min(current_value, scrollbar.maximum()))

    def update_monitoring_tables(self):
        """모니터링 탭의 표(집계/실시간)를 갱신합니다."""
        if not self.ui_dirty:
            return
            
        cameras = self.cfg_mgr.get_cameras()
        if not cameras:
            return

        # 1121 스타일 (monitoring_manager.py / config_module.py get_css_classes 참조)
        style = """
        <style>
        .table-header { 
            border: 1px solid #cccccc; padding: 2px 4px; text-align: center; 
            font-weight: 600; background-color: #f0f0f0; color: #333333; font-size: 11px;
        }
        .table-cell-left { 
            border: 1px solid #cccccc; padding: 2px 4px; text-align: left; 
            font-weight: 500; color: #333333; background-color: white; font-size: 11px;
        }
        .table-cell-center { 
            border: 1px solid #cccccc; padding: 2px 4px; text-align: center; 
            font-weight: 500; color: #333333; background-color: white; font-size: 11px;
        }
        </style>
        """
        
        # 1. People Count Summary (DB 집계)
        sum_html = style + '<table width="100%" cellspacing="0" cellpadding="0" style="border-collapse: collapse;">'
        # 헤더: 카메라 정보 + Area 1~4 (총 5열)
        sum_html += '<tr><th class="table-header" style="width:300px;">카메라 정보</th>'
        for i in range(4):
            sum_html += f'<th class="table-header">Area {i+1}</th>'
        sum_html += '</tr>'
        
        for cam in cameras:
            key = cam['key']
            name = cam.get('name', '')
            ip = cam.get('ip', '')
            label_text = f"{ip} / {name}" if name else ip
            is_connected = self.camera_conn_status.get(key, False)
            
            # 카메라 정보 행 (colspan=5)
            sum_html += f'<tr><td colspan="5" class="table-cell-left" style="font-weight:bold;">{label_text}</td></tr>'
            
            # 기간별 집계 (1h, 24h, Total)
            periods = [("1시간", 1), ("24시간", 24), ("전체", None)]
            for label, hours in periods:
                stats = db_module.get_people_count_stats(key, hours)
                sum_html += f'<tr><td class="table-cell-left">{label}</td>'
                for i in range(4):
                    aid = i + 1
                    # [정책] 연결됨: 값 표시 / 연결안됨: 숨김('-')
                    if is_connected:
                        val = stats.get(aid, 0)
                    else:
                        val = "-"
                    sum_html += f'<td class="table-cell-center">{val}</td>'
                sum_html += '</tr>'
        sum_html += '</table>'
        
        # 2. Realtime Count (메모리 캐시)
        rt_html = style + '<table width="100%" cellspacing="0" cellpadding="0" style="border-collapse: collapse;">'
        # 헤더: Area 1~4 (총 4열) - 1121 Realtime 표는 카메라 정보 헤더가 없음
        rt_html += '<tr>'
        for i in range(4):
            rt_html += f'<th class="table-header" style="width:90px;">Area {i+1}</th>'
        rt_html += '</tr>'
        
        for cam in cameras:
            key = cam['key']
            name = cam.get('name', '')
            ip = cam.get('ip', '')
            label_text = f"{ip} / {name}" if name else ip
            is_connected = self.camera_conn_status.get(key, False)
            
            # 카메라 정보 행 (colspan=4)
            rt_html += f'<tr><td colspan="4" class="table-cell-left" style="font-weight:bold;">{label_text}</td></tr>'
            rt_html += '<tr>'
            
            cam_counts = self.realtime_counts.get(key, {})
            for i in range(4):
                aid = i + 1
                # [정책] 연결됨: 값 표시 / 연결안됨: 숨김('-')
                if is_connected:
                    val = cam_counts.get(aid, 0)
                else:
                    val = "-"
                rt_html += f'<td class="table-cell-center">{val}</td>'
            rt_html += '</tr>'
        rt_html += '</table>'

        # UI 업데이트 (스크롤 유지 로직은 생략하고 단순 갱신)
        self._set_html_keep_scroll(self.ui.people_summary, sum_html)
        self._set_html_keep_scroll(self.ui.personnel_count_label, rt_html)
        
        self.ui_dirty = False

    def log_stats_debug(self):
        """주기적으로 집계 상태를 디버그 로그로 출력"""
        if not self.ui.chk_show_debug.isChecked():
            return
            
        cameras = self.cfg_mgr.get_cameras()
        for cam in cameras:
            key = cam['key']
            # 1h, 24h, Total 집계 및 행 수 조회
            s1h, r1h = db_module.get_people_count_stats_debug(key, 1)
            s24h, r24h = db_module.get_people_count_stats_debug(key, 24)
            stot, rtot = db_module.get_people_count_stats_debug(key, None)
            
            sum1h = sum(s1h.values())
            sum24h = sum(s24h.values())
            sumtot = sum(stot.values())
            
            self.add_event_log(f"[DEBUG] STATS camera={key} rows1h={r1h} sum1h={sum1h} rows24h={r24h} sum24h={sum24h} total={sumtot}")

    def check_thread_health(self):
        """스레드 및 비디오 상태 모니터링 및 자동 복구"""
        now = time.time()
        
        # 1. 이벤트 스레드 점검
        for key, threads in self.event_threads.items():
            # 쿨다운 체크 (60초)
            last_ts = self._last_restart_time_event.get(key, 0)
            if now - last_ts < 60:
                continue

            restarted = False
            
            # PeopleCountThread
            pt = threads.get("people")
            if pt:
                # [Commit H1-1] Stall 체크 제거 (정상 Idle 오인 방지), Dead 상태만 복구
                if not pt.isRunning():
                    # [Commit H1-1] 카메라 연결 상태 확인 (연결 끊김 시 복구 시도 안 함)
                    # [Commit H1-3] 연결 상태 미확정(default) 시 재시작 방지 (False)
                    is_connected = self.camera_conn_status.get(key, False)
                    
                    if is_connected:
                        # [FIX-2] 재시작이 이미 진행 중이면 건너뛰기
                        if self._pc_restart_inflight.get(key, False):
                            continue

                        self._pc_restart_inflight[key] = True
                        try:
                            allow, suppressed = should_log(f"health_pc_recovery_{key}", 300)
                            if allow:
                                msg = f"[Recovery] Restarting PeopleCount thread for {key} (reason=dead)" + (f" (suppressed {suppressed})" if suppressed > 0 else "")
                                logger.info(msg)
                            pt.restart()
                            restarted = True
                        finally:
                            self._pc_restart_inflight[key] = False
                    else:
                        # [Commit H1-2] 연결 끊김으로 인한 스킵 로그 (1시간 제한)
                        allow_skip, _ = should_log(f"health_pc_skip_disconnected_{key}", 3600)
                        if allow_skip:
                            logger.info(f"[Recovery] Skipping PeopleCount restart for {key} (Camera disconnected)")
            
            # StayDetectionThread
            st = threads.get("stay")
            if st:
                if not st.isRunning():
                    allow, suppressed = should_log(f"health_restart_stay_{key}", 300)
                    if allow:
                        msg = f"[Recovery] Restarting StayDetection thread for {key}" + (f" (suppressed {suppressed})" if suppressed > 0 else "")
                        logger.info(msg)
                    st.restart()
                    restarted = True
                # [Commit 19-1] StayDetection은 정상 Idle이 길 수 있으므로 Stall 체크 제외
            
            if restarted:
                self._last_restart_time_event[key] = now
        
        # 2. 비디오 위젯 점검
        for key, tile_data in self.tiles.items():
            # [Commit M1-4] 모니터링 ON 상태(체크됨)가 아니면 자동 재시작 안 함
            if not self.state_mgr.get_monitor_enabled(key):
                continue

            # 비디오도 동일한 쿨다운 적용
            last_ts = self._last_restart_time_video.get(key, 0)
            if now - last_ts < 60:
                continue

            video = tile_data['video']
            # 사용자가 정지하지 않았는데 재생 중이 아니면 재시작
            if not video.is_stopping and not video.is_playing():
                allow, suppressed = should_log(f"health_restart_video_{key}", 300)
                if allow:
                    msg = f"[Recovery] Restarting VideoWidget for {key}" + (f" (suppressed {suppressed})" if suppressed > 0 else "")
                    logger.info(msg)
                video.restart()
                self._last_restart_time_video[key] = now

    def update_system_status(self):
        """시스템 CPU/MEM 사용량 갱신"""
        cpu = 0
        mem = 0
        
        if HAS_PSUTIL:
            try:
                cpu = psutil.cpu_percent()
                mem = psutil.virtual_memory().percent
            except Exception:
                pass
        else:
            # Fallback: psutil 없을 경우 임의의 값 (테스트용)
            import random
            cpu = random.randint(10, 30)
            mem = random.randint(30, 60)
            
        if hasattr(self.ui, 'cpu_bar'):
            self.ui.cpu_bar.setValue(int(cpu))
            self.ui.cpu_bar.setFormat(f"CPU: {cpu}%")
            
        if hasattr(self.ui, 'mem_bar'):
            self.ui.mem_bar.setValue(int(mem))
            self.ui.mem_bar.setFormat(f"MEM: {mem}%")

    def closeEvent(self, event):
        # 종료 시 운영 상태 저장 (AppState)
        self._closing = True
        
        # 타이머 정지
        if self.health_timer.isActive(): self.health_timer.stop()
        if self.sys_status_timer.isActive(): self.sys_status_timer.stop()
        if self.stats_log_timer.isActive(): self.stats_log_timer.stop()
        if self.ui_update_timer.isActive(): self.ui_update_timer.stop()
        if self.log_rotate_timer.isActive(): self.log_rotate_timer.stop()
        logger.info("[Main] Timers stopped")

        # 현재 선택된 카메라 키 저장
        current = self.ui.camera_list.currentItem()
        if current:
            self.state_mgr.set("last_camera_key", current.data(Qt.UserRole))
            
        # 정상 종료 시 크래시 경고 플래그 초기화
        self.state_mgr.set("last_crash_warned_ts", 0)
        
        logger.info("[Main] Application closing...")
        self.state_mgr.save_state()
        
        # 1. 이벤트 스레드 종료
        # stop_events 내부에서 cleanup_camera_resources 호출
        self.stop_events()
        
        # 2. 비디오 리소스 해제
        # stop_events에서 비디오까지 정리하지는 않으므로(cleanup은 개별 호출용),
        # 전체 타일에 대해 release 수행
        for key, tile_data in self.tiles.items():
            tile_data['video'].release()
            
        # 3. GPIO 정리
        if hasattr(self, 'gpio_bridge'):
            self.gpio_bridge.cleanup()
            logger.info("[Main] GPIO cleaned up")
            
        # [Commit 23-2] APP_STOP 로그 DB 저장 (event_logs)
        db_module.enqueue_event({
            "type": "APP_STOP",
            "message": "Application stopping",
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ts_epoch": int(time.time())
        })
            
        # 4. DB 워커 종료
        db_module.stop_db_worker(flush=True)
        
        event.accept()

    # ---------------------------------------------------------
    # Idle / Auto Stop Logic
    # ---------------------------------------------------------
    def eventFilter(self, obj, event):
        """애플리케이션 전체 이벤트 필터링 (사용자 활동 감지)"""
        # [Commit M2-3] MouseMove 제외 (노이즈 방지)
        if event.type() in (QEvent.Type.MouseButtonPress, 
                            QEvent.Type.KeyPress, QEvent.Type.Wheel):
            self._last_user_activity_ts = time.time()
            self._auto_stop_fired = False
        return super().eventFilter(obj, event)

    def _is_video_playing(self) -> bool:
        """현재 영상이 하나라도 재생 중인지 확인"""
        for tile in self.tiles.values():
            if tile.get('video') and tile['video'].is_playing():
                return True
        return False

    def _check_idle_stop(self):
        """Idle 상태 체크 및 자동 종료"""
        if not self.idle_stop_enable:
            return
        
        # 영상 송출 중이 아니면 자동 종료 판단 금지
        if not self._is_video_playing():
            return
            
        if self.ui.chk_keep_watching.isChecked():
            return
            
        if self._auto_stop_fired:
            return

        now = time.time()
        if now - self._last_user_activity_ts >= self.idle_stop_sec:
            msg = "[Main] Auto-stopped monitoring due to inactivity while video playing"
            logger.info(msg)
            self.add_event_log("[Main] Auto-stopped monitoring due to inactivity")
            self.on_btn_stop_clicked()
            self._auto_stop_fired = True
