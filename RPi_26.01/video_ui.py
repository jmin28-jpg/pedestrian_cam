import os
import sys
import re
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from PySide6.QtWidgets import QWidget, QLabel, QFrame, QStackedLayout, QApplication, QSizePolicy
from PySide6.QtCore import Qt, Signal, Slot, QTimer, QPointF, QRectF
from PySide6.QtGui import QPainter, QPen, QColor, QBrush, QPolygonF
from log import get_logger
from log_rate_limit import should_log
import time

logger = get_logger(__name__)

# -----------------------
# GStreamer Import
# -----------------------
try:
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GstVideo", "1.0")
    from gi.repository import Gst, GstVideo
    Gst.init(None)
    HAS_GST = True
except Exception as e:
    HAS_GST = False
    allow, suppressed = should_log("gst_import_fail", 3600)
    if allow:
        logger.error(
            "[Camera] GStreamer import failed. Video disabled."
            + (f" (suppressed {suppressed})" if suppressed else "")
        )

# -----------------------
# Cairo Import (optional)
# -----------------------
HAS_PYCAIRO = False
PYCAIRO_ERR = None
try:
    import cairo
    HAS_PYCAIRO = True
except Exception as e:
    PYCAIRO_ERR = repr(e)
    HAS_PYCAIRO = False

def _roi_diag_enabled() -> bool:
    import os
    return os.environ.get("OPAS_ROI_DIAG", "").lower() in ("1","true","yes","on")


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name, "").strip()
    if not v:
        return default
    try:
        return int(v)
    except Exception:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _pick_best_sink(camera_key: str):
    """
    가용한 싱크를 우선순위에 따라 선택.
    Priority: CCTV_SINK(env) -> glimagesink -> xvimagesink -> ximagesink -> autovideosink
    """
    sink_name = os.environ.get("CCTV_SINK", "").strip()
    if sink_name:
        if Gst.ElementFactory.find(sink_name):
            s = Gst.ElementFactory.make(sink_name, f"sink_{camera_key}")
            if s:
                logger.info(f"[{camera_key}] Selected sink: {sink_name}")
                return s
        allow, suppressed = should_log(f"sink_env_fail_{camera_key}", 300)
        if allow:
            logger.warning(f"[{camera_key}] CCTV_SINK={sink_name} not found or create failed. fallback..." + (f" (suppressed {suppressed})" if suppressed > 0 else ""))

    # RPi 추천 순서
    # 영상 재생 안정성을 위해 xvimagesink를 1순위로 (RPi 검증됨)
    candidates = ["xvimagesink", "glimagesink", "ximagesink", "autovideosink"]
    for cand in candidates:
        if Gst.ElementFactory.find(cand):
            s = Gst.ElementFactory.make(cand, f"sink_{camera_key}")
            if s:
                logger.info(f"[{camera_key}] Selected sink: {cand}")
                return s
    
    allow, suppressed = should_log(f"sink_fallback_{camera_key}", 300)
    if allow:
        logger.warning(f"[{camera_key}] Fallback to autovideosink" + (f" (suppressed {suppressed})" if suppressed > 0 else ""))
    return Gst.ElementFactory.make("autovideosink", f"sink_{camera_key}")


def _rewrite_subtype(url: str, subtype: int) -> str:
    """
    rtsp url의 query에서 subtype을 subtype 값으로 강제.
    """
    try:
        u = urlparse(url)
        q = parse_qs(u.query, keep_blank_values=True)
        q["subtype"] = [str(subtype)]
        new_query = urlencode(q, doseq=True)
        return urlunparse((u.scheme, u.netloc, u.path, u.params, new_query, u.fragment))
    except Exception:
        # 단순 치환 fallback
        if "subtype=" in url:
            import re
            return re.sub(r"subtype=\d+", f"subtype={subtype}", url)
        sep = "&" if "?" in url else "?"
        return url + f"{sep}subtype={subtype}"


class VideoFrameLabel(QLabel):
    """
    Qt 기반 렌더링을 위한 커스텀 라벨.
    paintEvent에서 영상 프레임과 ROI 오버레이를 함께 그린다.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setScaledContents(False) # 직접 그리기 위해 False
        self._pixmap = None
        self._roi_callback = None # ROI 그리기 콜백 함수
        self._paint_logged = False # [STEP 9-3] 최초 1회 로그 플래그

    def set_frame_pixmap(self, pixmap):
        self._pixmap = pixmap
        self.update()

    def set_roi_callback(self, callback):
        self._roi_callback = callback

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # 1. 배경 (검정)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        
        # 2. 영상 프레임 (비율 유지 스케일링)
        if self._pixmap and not self._pixmap.isNull():
            # [STEP 9-3] PaintEvent 진단 로그 (최초 1회)
            if not self._paint_logged:
                self._paint_logged = True
                logger.warning(f"[QT-OVERLAY] paintEvent first draw: pixmap={self._pixmap.width()}x{self._pixmap.height()}")

            target_rect = self.rect()
            scaled_pixmap = self._pixmap.scaled(target_rect.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            
            # 중앙 정렬 계산
            x = (target_rect.width() - scaled_pixmap.width()) / 2
            y = (target_rect.height() - scaled_pixmap.height()) / 2
            painter.drawPixmap(int(x), int(y), scaled_pixmap)
            
            # 3. ROI 오버레이 (콜백 호출)
            if self._roi_callback:
                # 영상이 그려진 실제 영역(rect)을 전달하여 좌표 매핑
                draw_rect = QRectF(x, y, scaled_pixmap.width(), scaled_pixmap.height())
                self._roi_callback(painter, draw_rect)

class VideoWidget(QWidget):
    update_label_signal = Signal(str)
    clicked = Signal(str) # camera_key
    doubleClicked = Signal(str) # camera_key

    # [STEP 2] GStreamer 스레드 -> GUI 스레드로 프레임을 안전하게 전달하기 위한 시그널
    frame_ready = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        
        # [STEP 7-1] 렌더링 모드 결정
        self.use_qt_overlay = _env_bool("OPAS_QT_OVERLAY", False)
        self.camera_key = "Unknown" # 초기화 위치 이동 (로그 출력을 위해)

        # Native Window 설정 (GStreamer Overlay 필수)
        self.setAttribute(Qt.WA_NativeWindow, True)
        self.setAttribute(Qt.WA_DontCreateNativeAncestors, True)

        # window_main.py 호환용
        self.is_ready = HAS_GST
        self.isready = self.is_ready  # 혹시 isready를 참조하는 코드가 있으면 같이 살려둠

        self.setStyleSheet("background-color: black;")

        # [STEP 8-1] 모드 선택 로그 강제 출력
        logger.warning(f"[QT-OVERLAY] camera_key(init)={self.camera_key} OPAS_QT_OVERLAY={os.environ.get('OPAS_QT_OVERLAY')} use_qt_overlay={self.use_qt_overlay}")

        self.rtsp_url = None
        self.desired_subtype = None # None: URL 그대로, 0: Main, 1: Sub

        self._pipeline = None
        self._bus = None

        self._src_width = 0
        self._src_height = 0

        self._video_linked = False
        self._audio_linked = False

        self._sink = None
        self._win_id = None
        self._appsink = None # Qt Overlay 모드용

        # ROI
        # Data structure: { area_id (int): [(x_norm, y_norm), ...] }
        self.roi_regions_norm = {} 
        self.roi_enabled_areas = set()
        self.roi_edit_area = None
        self.roi_edit_mode = False
        self.roi_active_point_index = -1
        self.roi_visible = True
        self._draw_debug_once = False # Draw 디버그 로그 1회 제한용
        self._last_draw_log_ts = 0
        self._draw_log_interval = 5.0 # 5초마다 로그
        self.last_draw_w = 0.0
        self.last_draw_h = 0.0
        self._draw_calls = 0
        self._draw_errors = 0
        # roi_display_mode 제거: window_main에서 set_roi_regions로 데이터 자체를 제어함

        # reconnect
        self.is_stopping = False # 명시적 정지 중인지 여부
        self.retry_count = 0
        self.backoff_ms = 1000
        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.setSingleShot(True)
        self.reconnect_timer.timeout.connect(self._reconnect)

        # bus polling (GLib 의존 제거)
        self.bus_timer = QTimer(self)
        self.bus_timer.setInterval(50)  # 20fps 정도로 메시지 폴링
        self.bus_timer.timeout.connect(self._poll_bus)

        # UI 구성
        if self.use_qt_overlay:
            # Qt 렌더링용 커스텀 라벨
            self.video_area = VideoFrameLabel(self)
            self.video_area.set_roi_callback(self._draw_roi_qt)
        else:
            # GStreamer Overlay용 Native Window
            self.video_area = QFrame(self)
            self.video_area.setAttribute(Qt.WA_NativeWindow, True)
            self.video_area.setAttribute(Qt.WA_DontCreateNativeAncestors, True)
            self.video_area.setAutoFillBackground(False)
        
        self.video_area.setStyleSheet("background-color: black;")

        self._msg_label = QLabel(self)
        self._msg_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._msg_label.setStyleSheet(
            "color: white; font-size: 12px; background-color: rgba(0, 0, 0, 128);"
        )
        self._msg_label.hide()

        layout = QStackedLayout(self)
        layout.setStackingMode(QStackedLayout.StackingMode.StackAll)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.video_area)
        layout.addWidget(self._msg_label)

        # [STEP 2] frame_ready 시그널을 _on_frame_ready 슬롯에 연결
        if self.use_qt_overlay:
            self.frame_ready.connect(self._on_frame_ready)

        self.update_label_signal.connect(self._update_label_text)

        if not HAS_GST:
            self._msg_label.setText("GStreamer Missing")
            self._msg_label.show()
        elif not self.use_qt_overlay:
            # [Commit ROI-VID-2] cairooverlay factory 존재 여부 1회 로그
            allow, _ = should_log("cairooverlay_check", 3600)
            if allow:
                f = Gst.ElementFactory.find("cairooverlay")
                status = "FOUND" if f else "NOT FOUND"
                logger.info(f"[VideoWidget] cairooverlay factory check: {status}")

            # [STEP 1] ROI 진단 로그 삽입
            if _roi_diag_enabled():
                logger.warning(f"[ROI-DIAG] pycairo import={HAS_PYCAIRO} err={PYCAIRO_ERR}")

                try:
                    reg = Gst.Registry.get()
                    feat = reg.find_feature("cairooverlay", Gst.ElementFactory)
                    if feat:
                        plugin = feat.get_plugin()
                        logger.warning(f"[ROI-DIAG] cairooverlay plugin file={plugin.get_filename()}")
                    else:
                        logger.warning("[ROI-DIAG] cairooverlay feature NOT FOUND")
                except Exception as e:
                    logger.warning(f"[ROI-DIAG] registry error: {e!r}")

    # -----------------------
    # Qt events
    # -----------------------
    def showEvent(self, event):
        super().showEvent(event)
        if not self.use_qt_overlay:
            self._win_id = int(self.video_area.winId())
            # sink가 이미 있으면 handle 적용
            self._apply_video_overlay_handle()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self.use_qt_overlay:
            self._apply_render_rect()

    def mousePressEvent(self, event):
        # [수정] 편집 모드가 아니면 마우스 이벤트 무시
        if not self.roi_edit_mode:
            super().mousePressEvent(event)
            return

        # ROI Edit Mode Logic
        if self.roi_edit_mode and self.roi_edit_area in self.roi_regions_norm:
            points = self.roi_regions_norm[self.roi_edit_area]
            
            if self.use_qt_overlay:
                # Qt Overlay 모드에서는 VideoFrameLabel이 실제 그려진 영역을 알고 있음
                # 하지만 여기서는 단순화를 위해 VideoWidget 전체 크기 기준으로 계산하되,
                # paintEvent에서 계산된 rect를 가져오는 것이 정확함.
                # 일단 last_draw_w/h는 Qt 모드에서도 갱신되도록 처리 필요.
                w = self.video_area.width()
                h = self.video_area.height()
                fw = self.last_draw_w
                fh = self.last_draw_h
            else:
                w = self.video_area.width()
                h = self.video_area.height()
                fw = self.last_draw_w
                fh = self.last_draw_h

            # Widget 좌표 -> Frame 좌표 -> Normalized 좌표 변환 (레터박스 고려)
            if w > 0 and h > 0 and fw > 0 and fh > 0:
                # Scale & Offset 계산
                scale = min(w / fw, h / fh)
                disp_w = fw * scale
                disp_h = fh * scale
                offset_x = (w - disp_w) / 2
                offset_y = (h - disp_h) / 2

                mx = event.position().x()
                my = event.position().y()

                nx = (mx - offset_x) / disp_w
                ny = (my - offset_y) / disp_h
                
                # Find nearest point
                # Radius: 화면 픽셀 기준 12px -> Normalized 거리로 환산
                radius_px = 12.0
                # 가로/세로 스케일이 같으므로 disp_w 기준 (또는 disp_h)
                # 거리 비교 시 (nx-px)^2 + (ny-py)^2 < (radius_px / disp_w)^2
                # 하지만 간단히 유클리드 거리로 비교하되 threshold를 동적으로 계산
                
                # Normalized threshold
                threshold_norm = radius_px / disp_w 
                
                self.roi_active_point_index = -1
                min_dist_sq = threshold_norm * threshold_norm
                
                for i, (px, py) in enumerate(points):
                    dist_sq = (nx - px)**2 + (ny - py)**2
                    if dist_sq < min_dist_sq:
                        min_dist_sq = dist_sq
                        self.roi_active_point_index = i
        
        self.clicked.emit(self.camera_key)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # [수정] 편집 모드가 아니면 무시
        if not self.roi_edit_mode:
            super().mouseMoveEvent(event)
            return
            
        if self.roi_edit_mode and self.roi_active_point_index != -1 and self.roi_edit_area in self.roi_regions_norm:
            w = self.video_area.width()
            h = self.video_area.height()
            fw = self.last_draw_w
            fh = self.last_draw_h
            
            if w > 0 and h > 0 and fw > 0 and fh > 0:
                scale = min(w / fw, h / fh)
                disp_w = fw * scale
                disp_h = fh * scale
                offset_x = (w - disp_w) / 2
                offset_y = (h - disp_h) / 2

                mx = event.position().x()
                my = event.position().y()

                # Frame 좌표계로 변환
                nx = (mx - offset_x) / disp_w
                ny = (my - offset_y) / disp_h

                # Clamp to 0.0 ~ 1.0
                nx = max(0.0, min(1.0, nx))
                ny = max(0.0, min(1.0, ny))
                
                self.roi_regions_norm[self.roi_edit_area][self.roi_active_point_index] = (float(nx), float(ny))
                # GStreamer overlay updates automatically on next frame

                # Qt Overlay 모드일 경우 수동 갱신 요청
                if self.use_qt_overlay:
                    self.video_area.update()

    def mouseReleaseEvent(self, event):
        if not self.roi_edit_mode:
            super().mouseReleaseEvent(event)
            return
            
        self.roi_active_point_index = -1
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.doubleClicked.emit(self.camera_key)
        super().mouseDoubleClickEvent(event)

    def rebind_window_handle(self):
        """
        Forces a re-bind of the GStreamer window handle.
        Useful after layout changes that might confuse xvimagesink.
        """
        # 가드: 보이지 않거나 크기가 유효하지 않으면 스킵
        if self.use_qt_overlay:
            return
            
        if not self.isVisible():
            return
        if self.video_area.width() <= 10 or self.video_area.height() <= 10:
            return

        self._win_id = int(self.video_area.winId())
        self._apply_video_overlay_handle()
        self._apply_render_rect()
        # print(f"[VideoWidget][{self.camera_key}] Rebinding window handle.")

    # -----------------------
    # Public API (window_main.py 호환)
    # -----------------------
    def set_subtype(self, subtype: int):
        """분할 모드에 따른 서브타입 강제 설정 (0: Main, 1: Sub)"""
        self.desired_subtype = subtype

    def set_media(self, rtsp_url, camera_key=None, reset_backoff=True):
        if not HAS_GST:
            return

        self.stop()
        self.release()

        self.camera_key = camera_key if camera_key else "Unknown"
        self.rtsp_url = rtsp_url

        self._src_width = 0
        self._src_height = 0
        self._video_linked = False
        self._audio_linked = False

        if reset_backoff:
            self.retry_count = 0
            self.backoff_ms = 1000

        if not rtsp_url:
            return

        # 서브타입 정책 적용
        final_url = rtsp_url
        if self.desired_subtype is not None:
            final_url = _rewrite_subtype(rtsp_url, self.desired_subtype)
        elif _env_bool("CCTV_H265_USE_SUBSTREAM", False):
            final_url = _rewrite_subtype(rtsp_url, 1)
        
        self.rtsp_url = final_url # 재연결 시 사용

        logger.info(f"[VideoWidget][{self.camera_key}] Start media: {self.rtsp_url} (subtype={self.desired_subtype})")
        self.update_label_signal.emit("Connecting...")

        try:
            self._build_pipeline(self.rtsp_url)
            self.play()  # window_main이 따로 play() 호출해도 안전하게 동작하도록 play는 idempotent
        except Exception as e:
            logger.error(f"[VideoWidget][{self.camera_key}] Setup failed: {e}")
            self.update_label_signal.emit(f"Error: {e}")
            self._schedule_reconnect("Setup Exception")

    def play(self):
        self.is_stopping = False
        if self._pipeline:
            self._pipeline.set_state(Gst.State.PLAYING)

    def restart(self):
        """외부 요청(헬스체크 등)에 의한 강제 재시작"""
        logger.info(f"[VideoWidget][{self.camera_key}] Restart requested.")
        self.set_media(self.rtsp_url, self.camera_key, reset_backoff=True)

    def stop(self):
        self.is_stopping = True
        
        # 타이머 즉시 정지 (재연결 방지)
        if self.reconnect_timer.isActive():
            self.reconnect_timer.stop()
        self.reconnect_timer.stop()
        self.bus_timer.stop()

        if self._pipeline:
            try:
                self._pipeline.set_state(Gst.State.NULL)
                # 너무 길게 기다리면 UI가 멈출 수 있으니 짧게만 확인
                self._pipeline.get_state(2 * Gst.SECOND)
            except Exception:
                pass

    def safe_shutdown(self):
        """
        GStreamer pipeline을 완전 NULL 상태까지 내리고
        bus flush 후 안전하게 해제한다.
        """
        try:
            # Stop timers
            self.reconnect_timer.stop()
            self.bus_timer.stop()

            if self._pipeline:
                self._pipeline.set_state(Gst.State.NULL)
                self._pipeline.get_state(Gst.CLOCK_TIME_NONE)
                if self._bus:
                    self._bus.set_flushing(True)
                self._pipeline = None
                self._bus = None
                self._sink = None
        except Exception as e:
            logger.error(f"[VideoWidget] safe_shutdown error: {e}")

    def release(self):
        # 파이프라인/버스 레퍼런스 정리
        self.safe_shutdown()

    def is_playing(self):
        if self._pipeline:
            _, state, _ = self._pipeline.get_state(0)
            return state == Gst.State.PLAYING
        return False

    def set_roi_regions(self, norm_by_area: dict, enabled_by_area: set):
        """ROI 전체 데이터를 설정합니다."""
        # 데이터 복사하여 저장 (외부 참조 방지)
        # 키를 int로 강제 변환하여 저장 (문자열 키 문제 방지)
        self.roi_regions_norm = {}
        if norm_by_area:
            for k, v in norm_by_area.items():
                try:
                    self.roi_regions_norm[int(k)] = list(v)
                except ValueError:
                    pass
        
        self.roi_enabled_areas = set()
        if enabled_by_area:
            self.roi_enabled_areas = {int(k) for k in enabled_by_area if str(k).isdigit()}
        
        # 편집 중인 영역이 사라졌으면 편집 중단
        if self.roi_edit_area and self.roi_edit_area not in self.roi_regions_norm:
            self.roi_edit_area = None
        
        if _roi_diag_enabled():
            logger.warning(f"[ROI-DIAG][{self.camera_key}] set_roi_regions regions={len(self.roi_regions_norm)} enabled={len(self.roi_enabled_areas)} keys={sorted(self.roi_regions_norm.keys())}")
            
        # Qt Overlay 모드일 경우 즉시 갱신
        if self.use_qt_overlay:
            self.video_area.update()

    def set_roi_edit(self, area_id: int | None, edit_mode: bool):
        """편집 모드 및 대상 영역 설정"""
        self.roi_edit_area = area_id
        self.roi_edit_mode = edit_mode
        self.roi_active_point_index = -1

        if self.use_qt_overlay:
            self.video_area.update()

    def get_roi_edit_points_norm(self):
        """현재 편집 중인 영역의 좌표 반환"""
        if self.roi_edit_area in self.roi_regions_norm:
            return list(self.roi_regions_norm[self.roi_edit_area])
        return []

    def get_roi_regions(self):
        """전체 ROI 데이터 반환 (백업용)"""
        # Deep copy
        norm_copy = {k: list(v) for k, v in self.roi_regions_norm.items()}
        return norm_copy, set(self.roi_enabled_areas)

    def set_roi_visible(self, visible: bool):
        self.roi_visible = visible
        if self.use_qt_overlay:
            self.video_area.update()

    def set_highlight(self, active: bool):
        pass

    # -----------------------
    # Pipeline builder
    # -----------------------
    def _build_pipeline(self, rtsp_url: str):
        # [STEP 8-1] 파이프라인 빌드 로그
        logger.warning(f"[QT-OVERLAY] [{self.camera_key}] build_pipeline use_qt_overlay={self.use_qt_overlay}")

        self._pipeline = Gst.Pipeline.new(f"pipeline_{self.camera_key}_{_env_int('CCTV_PIPE_ID', 0)}")
        if not self._pipeline:
            raise RuntimeError("Failed to create pipeline")

        # source
        src = Gst.ElementFactory.make("rtspsrc", f"src_{self.camera_key}")
        if not src:
            raise RuntimeError("Failed to create rtspsrc")

        src.set_property("location", rtsp_url)
        # TCP(4) 우선, 필요시 UDP(1) 등. RPi에서는 TCP가 안정적.
        src.set_property("protocols", _env_int("CCTV_PROTOCOLS", 4)) 
        src.set_property("latency", _env_int("CCTV_LATENCY", 200)) # 200ms
        src.set_property("timeout", _env_int("CCTV_RTSP_TIMEOUT_US", 5000000)) # 5초
        src.set_property("tcp-timeout", 5000000) # 5초
        src.set_property("drop-on-latency", True)

        # tail (공통 비디오 처리 체인)
        # decoder 출력(raw) -> (videorate?) -> videoconvert -> videoscale -> (downscale caps?) -> (cairooverlay?) -> sink
        vq = Gst.ElementFactory.make("queue", f"vqueue_{self.camera_key}")
        vq.set_property("leaky", 2)  # downstream
        vq.set_property("max-size-buffers", _env_int("CCTV_Q_BUFS", 4))
        vq.set_property("max-size-bytes", 0)
        vq.set_property("max-size-time", 0)

        videorate = None
        fps_filter = None
        max_fps = os.environ.get("CCTV_MAX_FPS", "").strip()
        if max_fps:
            videorate = Gst.ElementFactory.make("videorate", f"videorate_{self.camera_key}")
            # 가능한 경우 drop-only로 CPU 최소화
            try:
                videorate.set_property("drop-only", True)
            except Exception:
                pass

            fps_filter = Gst.ElementFactory.make("capsfilter", f"fpscap_{self.camera_key}")
            fps_caps = Gst.Caps.from_string(f"video/x-raw,framerate<={max_fps}/1")
            fps_filter.set_property("caps", fps_caps)
            logger.debug(f"[VideoWidget][{self.camera_key}] Raw caps constraint: video/x-raw,framerate<={max_fps}/1")

        vc1 = Gst.ElementFactory.make("videoconvert", f"vc1_{self.camera_key}")
        vs = Gst.ElementFactory.make("videoscale", f"videoscale_{self.camera_key}")

        down_filter = None # 해상도 제한
        max_w = os.environ.get("CCTV_MAX_WIDTH", "").strip()
        max_h = os.environ.get("CCTV_MAX_HEIGHT", "").strip()
        if max_w and max_h:
            down_filter = Gst.ElementFactory.make("capsfilter", f"downcap_{self.camera_key}")
            down_caps = Gst.Caps.from_string(f"video/x-raw,width={int(max_w)},height={int(max_h)}")
            down_filter.set_property("caps", down_caps)
            logger.info(f"[VideoWidget][{self.camera_key}] Downscale caps: video/x-raw,width={max_w},height={max_h}")

        # overlay (optional) - Qt Overlay 모드에서는 cairooverlay 비활성화
        enable_overlay = not self.use_qt_overlay
        vc_before_ov = None
        cairo_overlay = None
        vc_after_ov = None

        if enable_overlay:
            # RPi에서는 cairooverlay를 사용하여 ROI를 그립니다.
            # videoconvert -> cairooverlay -> videoconvert 구조로 호환성 확보
            vc_before_ov = Gst.ElementFactory.make("videoconvert", f"vc_pre_ov_{self.camera_key}")
            cairo_overlay = Gst.ElementFactory.make("cairooverlay", f"overlay_{self.camera_key}")
            vc_after_ov = Gst.ElementFactory.make("videoconvert", f"vc_post_ov_{self.camera_key}")
            
            if cairo_overlay is None:
                logger.warning(f"[VideoWidget][{self.camera_key}] CairoOverlay element not available. ROI overlay disabled.")
                cairo_overlay = None
                vc_before_ov = None
                vc_after_ov = None
            else:
                cairo_overlay.connect("draw", self._on_draw_overlay)
                logger.info(f"[VideoWidget][{self.camera_key}] CairoOverlay created + inserted in video chain")
                if _roi_diag_enabled():
                    logger.warning(f"[VideoWidget][{self.camera_key}] [ROI-DIAG] CairoOverlay created + inserted in video chain")

        # sink 결정
        if self.use_qt_overlay:
            # [STEP 7-1] appsink 기반 Qt 렌더링
            self._appsink = Gst.ElementFactory.make("appsink", f"appsink_{self.camera_key}")
            if not self._appsink:
                raise RuntimeError("Failed to create appsink")
            
            self._appsink.set_property("emit-signals", True)
            self._appsink.set_property("sync", False)
            # [STEP C] 안정성: drop=True, max-buffers=1 유지
            self._appsink.set_property("drop", True) 
            self._appsink.set_property("max-buffers", 1)
            self._appsink.connect("new-sample", self._on_appsink_new_sample)
            
            # [STEP C] 스레드 분리용 큐 (Decoder Thread <-> AppSink Thread 분리)
            app_q = Gst.ElementFactory.make("queue", f"app_q_{self.camera_key}")
            app_q.set_property("leaky", 2) # downstream
            app_q.set_property("max-size-buffers", 1)

            # appsink용 capsfilter (BGRx 변환)
            # videoconvert -> capsfilter(BGRx) -> appsink
            app_vc = Gst.ElementFactory.make("videoconvert", f"app_vc_{self.camera_key}")
            app_caps = Gst.ElementFactory.make("capsfilter", f"app_caps_{self.camera_key}")
            
            # [STEP B] 픽셀 포맷 BGRx로 변경 (4byte align, QImage.Format_RGB32 대응)
            # RGB(3byte)는 stride 계산 문제 및 정렬 이슈로 검은 화면/크래시 유발 가능성 높음
            caps_str = "video/x-raw,format=BGRx"
            app_caps.set_property("caps", Gst.Caps.from_string(caps_str))
            
            self._sink = self._appsink # 파이프라인 연결용 참조
            
            # 파이프라인에 추가할 요소들
            self._pipeline.add(app_q)
            self._pipeline.add(app_vc)
            self._pipeline.add(app_caps)
            self._pipeline.add(self._appsink)
            
        else:
            # 기존 xvimagesink/autovideosink
            sink = _pick_best_sink(self.camera_key)
            if not sink:
                raise RuntimeError("No suitable sink (xvimagesink/ximagesink)")

            # sink props
            try:
                sink.set_property("sync", False)
            except Exception:
                pass
            try:
                sink.set_property("async", False)
            except Exception:
                pass
            try:
                sink.set_property("force-aspect-ratio", True)
            except Exception:
                pass
            
            self._sink = sink
            self._pipeline.add(sink)

        # add elements
        self._pipeline.add(src)
        self._pipeline.add(vq)

        if videorate:
            self._pipeline.add(videorate)
            self._pipeline.add(fps_filter)

        self._pipeline.add(vc1)
        self._pipeline.add(vs)

        if down_filter:
            self._pipeline.add(down_filter)


        # link tail (vq -> ...)
        if videorate:
            if not (vq.link(videorate) and videorate.link(fps_filter) and fps_filter.link(vc1)):
                raise RuntimeError("Failed to link vqueue -> videorate -> fpsfilter -> videoconvert")
        else:
            if not vq.link(vc1):
                raise RuntimeError("Failed to link vqueue -> videoconvert")

        if not vc1.link(vs):
            raise RuntimeError("Failed to link videoconvert -> videoscale")

        if down_filter:
            if not vs.link(down_filter):
                raise RuntimeError("Failed to link videoscale -> downscale caps")
            tail_start = down_filter
        else:
            tail_start = vs

        # Link to Sink
        if self.use_qt_overlay:
            # tail_start -> app_q -> app_vc -> app_caps -> appsink
            if not (tail_start.link(app_q) and app_q.link(app_vc) and app_vc.link(app_caps) and app_caps.link(self._appsink)):
                raise RuntimeError("Failed to link appsink chain")
            # [STEP 8-1] 링크 성공 로그
            logger.warning(f"[QT-OVERLAY] [{self.camera_key}] appsink chain linked OK")
        else:
            if cairo_overlay:
                self._pipeline.add(vc_before_ov)
                self._pipeline.add(cairo_overlay)
                self._pipeline.add(vc_after_ov)
                if not (tail_start.link(vc_before_ov) and vc_before_ov.link(cairo_overlay) and cairo_overlay.link(vc_after_ov) and vc_after_ov.link(self._sink)):
                    raise RuntimeError("Failed to link overlay chain")
            else:
                if not tail_start.link(self._sink):
                    raise RuntimeError("Failed to link tail_start -> sink")

        # dynamic pads
        src.connect("pad-added", self._on_rtspsrc_pad_added)

        # bus
        self._bus = self._pipeline.get_bus()
        self.bus_timer.start()

        # window handle
        if not self.use_qt_overlay:
            self._apply_video_overlay_handle()
            self._apply_render_rect()

    # -----------------------
    # Dynamic link: rtspsrc pad-added
    # -----------------------
    def _on_rtspsrc_pad_added(self, src, pad):
        caps = pad.query_caps(None)
        s = caps.get_structure(0)
        media_type = s.get_name()

        if media_type != "application/x-rtp":
            return

        media = s.get_value("media")
        encoding = s.get_value("encoding-name")
        payload = s.get_value("payload")

        logger.debug(f"[VideoWidget][{self.camera_key}] Pad Added: {media_type}, media={media}, encoding={encoding}, payload={payload}")

        if media == "audio":
            if self._audio_linked:
                return
            self._audio_linked = True
            self._link_audio_to_fakesink(pad)
            return

        if media != "video":
            return

        if self._video_linked:
            return

        # 명시적 디코더
        ok = self._link_explicit_video(pad, encoding)
        if not ok:
            # fallback (가능하면)
            logger.warning(f"[VideoWidget][{self.camera_key}] Fallback to decodebin")
            ok = self._link_decodebin_video(pad)

        if not ok:
            logger.error(f"[VideoWidget][{self.camera_key}] Video link failed. Scheduling reconnect.")
            self.update_label_signal.emit("Error")
            self._schedule_reconnect("Link Failed")
        else:
            self._video_linked = True

    def _link_audio_to_fakesink(self, pad):
        try:
            aq = Gst.ElementFactory.make("queue", f"aq_{self.camera_key}")
            fs = Gst.ElementFactory.make("fakesink", f"audiosink_{self.camera_key}")
            fs.set_property("sync", False)

            self._pipeline.add(aq)
            self._pipeline.add(fs)
            aq.sync_state_with_parent()
            fs.sync_state_with_parent()

            aq.link(fs)
            ret = pad.link(aq.get_static_pad("sink"))
            logger.debug(f"[{self.camera_key}] Linking audio pad to fakesink: {ret}")
        except Exception as e:
            logger.warning(f"[{self.camera_key}] audio link error: {e}")

    def _link_explicit_video(self, pad, encoding: str) -> bool:
        # 1. 디코더 후보군 선정 (HW 우선)
        candidates = []
        if encoding == "H264":
            depay_name = "rtph264depay"
            parse_name = "h264parse"
            candidates = ["v4l2h264dec", "omxh264dec", "avdec_h264"]
        elif encoding == "H265":
            depay_name = "rtph265depay"
            parse_name = "h265parse"
            candidates = ["v4l2h265dec", "omxh265dec", "avdec_h265"]
        else:
            return False

        # 2. 가용한 디코더 찾기
        dec_name = None
        for cand in candidates:
            if Gst.ElementFactory.find(cand):
                dec_name = cand
                break
        
        if not dec_name:
            logger.error(f"[{self.camera_key}] No decoder found for {encoding}")
            return False

        depay = Gst.ElementFactory.make(depay_name, f"depay_{self.camera_key}")
        parse = Gst.ElementFactory.make(parse_name, f"parse_{self.camera_key}")
        dec = Gst.ElementFactory.make(dec_name, f"dec_{self.camera_key}")

        if not depay or not parse or not dec:
            return False

        # threads tuning (특히 H265)
        if "avdec" in dec_name: # SW 디코더일 때만 threads 설정
            th = os.environ.get("CCTV_H265_THREADS", "").strip()
            if th:
                try:
                    dec.set_property("threads", int(th))
                except Exception:
                    pass

        self._pipeline.add(depay)
        self._pipeline.add(parse)
        self._pipeline.add(dec)

        depay.sync_state_with_parent()
        parse.sync_state_with_parent()
        dec.sync_state_with_parent()

        # depay -> parse -> dec -> vqueue
        if not depay.link(parse):
            return False
        if not parse.link(dec):
            return False
        if not dec.link(self._pipeline.get_by_name(f"vqueue_{self.camera_key}")):
            # 디버깅용
            logger.error(f"[VideoWidget][{self.camera_key}] Link fail: decoder -> vqueue")
            return False

        # 캡스(해상도) 추적: decoder src에 caps 이벤트 들어오면 기록
        try:
            dsrc = dec.get_static_pad("src")
            if dsrc:
                dsrc.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_caps_event, None)
        except Exception:
            pass

        # rtspsrc pad -> depay
        ret = pad.link(depay.get_static_pad("sink"))
        if ret != Gst.PadLinkReturn.OK:
            return False

        logger.info(f"[VideoWidget][{self.camera_key}] Explicit link success: {depay_name} -> {parse_name} -> {dec_name}")
        return True

    def _link_decodebin_video(self, pad) -> bool:
        try:
            dq = Gst.ElementFactory.make("queue", f"dvq_{self.camera_key}")
            decodebin = Gst.ElementFactory.make("decodebin", f"decodebin_{self.camera_key}")
            if not dq or not decodebin:
                return False

            self._pipeline.add(dq)
            self._pipeline.add(decodebin)
            dq.sync_state_with_parent()
            decodebin.sync_state_with_parent()

            dq.link(decodebin)

            # rtspsrc pad -> queue
            ret = pad.link(dq.get_static_pad("sink"))
            if ret != Gst.PadLinkReturn.OK:
                return False

            decodebin.connect("pad-added", self._on_decodebin_pad_added)
            return True
        except Exception:
            return False

    def _on_decodebin_pad_added(self, dbin, pad):
        try:
            caps = pad.query_caps(None)
            name = caps.get_structure(0).get_name()
            if not name.startswith("video/x-raw"):
                return

            vq = self._pipeline.get_by_name(f"vqueue_{self.camera_key}")
            sink_pad = vq.get_static_pad("sink")
            if sink_pad and not sink_pad.is_linked():
                ret = pad.link(sink_pad)
                logger.info(f"[{self.camera_key}] decodebin -> vqueue link: {ret}")
        except Exception as e:
            logger.error(f"[{self.camera_key}] decodebin pad-added error: {e}")

    # -----------------------
    # Caps/Overlay
    # -----------------------
    def _on_caps_event(self, pad, info, user_data):
        event = info.get_event()
        if event and event.type == Gst.EventType.CAPS:
            caps = event.parse_caps()
            s = caps.get_structure(0)
            w = s.get_value("width")
            h = s.get_value("height")
            if w and h:
                self._src_width = int(w)
                self._src_height = int(h)
        return Gst.PadProbeReturn.OK

    @Slot(object)
    def _on_frame_ready(self, img):
        """
        [STEP 2] GUI 스레드에서 실행되는 슬롯.
        QImage를 받아 QPixmap으로 변환 후 UI 위젯에 설정한다.
        """
        # 이 슬롯은 QueuedConnection으로 동작하여 GUI 스레드에서 안전하게 실행됨
        if not hasattr(self, "_frame_ready_logged"):
            self._frame_ready_logged = True
            logger.warning(f"[QT-OVERLAY] [{self.camera_key}] _on_frame_ready first call: img size={img.width()}x{img.height()}")

        from PySide6.QtGui import QPixmap
        self.video_area.set_frame_pixmap(QPixmap.fromImage(img))

    # -----------------------
    # AppSink Callback (Qt Overlay)
    # -----------------------
    def _on_appsink_new_sample(self, sink):
        sample = sink.emit("pull-sample")
        if not sample:
            return Gst.FlowReturn.ERROR

        caps = sample.get_caps()
        s = caps.get_structure(0)
        w = s.get_value("width")
        h = s.get_value("height")
        
        # Update src dimensions for ROI calculation
        self._src_width = w
        self._src_height = h
        self.last_draw_w = float(w)
        self.last_draw_h = float(h)

        buf = sample.get_buffer()
        result, map_info = buf.map(Gst.MapFlags.READ)
        if result:
            try:
                self._qt_frames = getattr(self, "_qt_frames", 0) + 1
                is_diag = _roi_diag_enabled() and (self._qt_frames % 60 == 0)

                # [STEP B] Stride 강제 계산 (BGRx = 4 bytes per pixel)
                # GstVideoInfo에 의존하지 않고, 32bit 포맷 특성을 이용해 직접 계산
                stride = w * 4
                data_len = len(map_info.data) # map_info.size 대신 len() 사용 권장
                expected_len = stride * h

                if is_diag:
                    fmt = s.get_value("format")
                    logger.warning(f"[QT-OVERLAY] [{self.camera_key}] caps={fmt} w={w} h={h} stride={stride} len={data_len}")

                # 버퍼 크기 검증 (Segfault 방지)
                if data_len < expected_len:
                    if is_diag:
                        logger.error(f"[QT-OVERLAY] Buffer too small! len={data_len} expected={expected_len}")
                    return Gst.FlowReturn.OK

                # QImage 생성 (Format_RGB888)
                # 데이터 복사 필수 (map_info는 unmap 후 유효하지 않음)
                from PySide6.QtGui import QImage, QPixmap
                
                if h > 0:
                    # [STEP B] Format_RGB32 사용 (BGRx 대응)
                    # 주의: map_info.data는 bytes-like object여야 함.
                    # copy()를 호출하여 GStreamer 메모리와 분리된 Deep Copy 생성
                    img = QImage(map_info.data, w, h, stride, QImage.Format_RGB32).copy()
                    
                    if is_diag:
                        logger.warning(f"[QT-OVERLAY] [{self.camera_key}] imgNull={img.isNull()} stride={stride}")

                    # [STEP 2] GStreamer 스레드에서 GUI 스레드로 직접 위젯을 건드리지 않고, 시그널을 통해 QImage 전달
                    self.frame_ready.emit(img)
            finally:
                buf.unmap(map_info)
        return Gst.FlowReturn.OK

    def _on_draw_overlay(self, overlay, context, timestamp, duration):
        if not self.roi_visible:
            return
            
        # Draw 콜백 내 로그 출력 완전 제거 (성능/도배 방지)

        try:
            self._draw_calls = getattr(self, "_draw_calls", 0) + 1

            # [STEP 5] onefile cairo context wrapping
            ctx = context
            is_native_pycairo = hasattr(ctx, "set_line_width")

            if not is_native_pycairo:
                # This path is likely taken in the onefile environment where the context
                # is a GBoxed object from GI, not a direct pycairo.Context.
                if _roi_diag_enabled() and self._draw_calls <= 3:
                    logger.warning(f"[ROI-DIAG] Context is not a pycairo.Context. Type: {type(ctx)}")
                    logger.warning(f"[ROI-DIAG] Attempting to wrap from native pointer...")
                    # [STEP 6-1] Enhanced pointer search
                    logger.warning(f"[ROI-DIAG] Context MRO: {ctx.__class__.__mro__}")
                    
                    candidates = [
                        "__gpointer__", "get_pointer", "get_target", "cairo_t", "ptr", "pointer",
                        "__gtype__", "gtype", "type", "get_type",
                        "to_pointer", "get_address", "address", "__int__", "__index__",
                        "get_boxed", "boxed", "_boxed", "_obj", "_pointer", "_pyobject"
                    ]
                    
                    for name in candidates:
                        if hasattr(ctx, name):
                            try:
                                attr_val = getattr(ctx, name)
                                if callable(attr_val): attr_val = attr_val()
                                logger.warning(f"[ROI-DIAG] context has attr {name}={attr_val!r}")
                            except Exception as e_attr:
                                logger.warning(f"[ROI-DIAG] failed to get attr {name}: {e_attr!r}")
                    
                    # Check repr for address
                    try:
                        repr_str = repr(ctx)
                        match = re.search(r"at (0x[0-9a-fA-F]+)", repr_str)
                        if match:
                            logger.warning(f"[ROI-DIAG] Found address in repr: {match.group(1)}")
                    except Exception:
                        pass

                # STEP 5-2: Attempt to wrap using the pointer
                if HAS_PYCAIRO and hasattr(ctx, "__gpointer__"):
                    try:
                        ptr = int(ctx.__gpointer__)
                        ctx = cairo.Context.from_address(ptr)
                    except Exception as e:
                        if _roi_diag_enabled():
                            logger.warning(f"[ROI-DIAG] Failed to wrap context from __gpointer__: {e!r}", exc_info=True)
                        return # Cannot draw
                else:
                    if _roi_diag_enabled():
                        logger.warning(f"[ROI-DIAG] Cannot wrap context: pycairo not loaded or context has no __gpointer__.")
                    return # Cannot draw

            # 1. Get Dimensions
            draw_w, draw_h = 0.0, 0.0
            try:
                target = ctx.get_target()
                draw_w = float(target.get_width())
                draw_h = float(target.get_height())
            except Exception:
                pass
            
            if draw_w <= 0 or draw_h <= 0:
                draw_w = float(self._src_width)
                draw_h = float(self._src_height)
            
            if draw_w <= 0 or draw_h <= 0: return
            
            self.last_draw_w = draw_w
            self.last_draw_h = draw_h

            # 2. Draw All Enabled Regions (Green Lines)
            ctx.set_line_width(2.0)
            ctx.set_source_rgba(0.0, 1.0, 0.0, 1.0) 

            for area_id, points in self.roi_regions_norm.items():
                if area_id not in self.roi_enabled_areas: continue
                if len(points) < 2: continue
                
                px_pts = [(p[0] * draw_w, p[1] * draw_h) for p in points]
                ctx.move_to(px_pts[0][0], px_pts[0][1])
                for i in range(1, len(px_pts)):
                    ctx.line_to(px_pts[i][0], px_pts[i][1])
                ctx.close_path()
                ctx.stroke()

            # 3. Draw Handles for Editing Area (Yellow Circles)
            if self.roi_edit_mode and self.roi_edit_area is not None and self.roi_edit_area in self.roi_regions_norm:
                ctx.set_source_rgba(1.0, 1.0, 0.0, 1.0) # Yellow
                radius = 5.0 # pixel radius
                
                points = self.roi_regions_norm.get(self.roi_edit_area, [])
                px_pts = [(p[0] * draw_w, p[1] * draw_h) for p in points]
                
                for i, (px, py) in enumerate(px_pts):
                    # Active point highlight
                    if i == self.roi_active_point_index:
                        ctx.set_source_rgba(1.0, 0.0, 0.0, 1.0) # Red
                    else:
                        ctx.set_source_rgba(1.0, 1.0, 0.0, 1.0) # Yellow
                        
                    ctx.arc(px, py, radius, 0, 2 * 3.14159)
                    ctx.fill()
                    
        except Exception:
            self._draw_errors = getattr(self, "_draw_errors", 0) + 1
            if _roi_diag_enabled():
                logger.warning(
                    f"[ROI-DIAG] draw exception calls={self._draw_calls} "
                    f"errors={self._draw_errors} pycairo={HAS_PYCAIRO}",
                    exc_info=True
                )
            return

        # 정상 draw 끝부분에 추가
        if _roi_diag_enabled() and self._draw_calls % 100 == 0:
            logger.warning(
                f"[ROI-DIAG] draw OK calls={self._draw_calls} "
                f"errors={getattr(self,'_draw_errors',0)}"
            )

    def _draw_roi_qt(self, painter: QPainter, draw_rect: QRectF):
        """Qt QPainter를 사용한 ROI 그리기 (VideoFrameLabel 콜백)"""
        # [STEP 8-4] ROI 그리기 진단 로그 (2초 주기)
        if _roi_diag_enabled():
            now = time.time()
            last_log = getattr(self, "_qt_roi_log_ts", 0)
            if now - last_log > 2.0:
                self._qt_roi_log_ts = now
                logger.warning(f"[QT-OVERLAY] [{self.camera_key}] draw_roi enabled={len(self.roi_enabled_areas)} rect={draw_rect} visible={self.roi_visible} edit={self.roi_edit_mode}")

        if not self.roi_visible:
            return

        draw_w = draw_rect.width()
        draw_h = draw_rect.height()
        offset_x = draw_rect.x()
        offset_y = draw_rect.y()

        # 1. Draw Enabled Regions (Green Lines)
        pen = QPen(QColor(0, 255, 0), 2)
        painter.setPen(pen)
        
        for area_id, points in self.roi_regions_norm.items():
            if area_id not in self.roi_enabled_areas: continue
            if len(points) < 2: continue
            
            poly_points = []
            for nx, ny in points:
                px = offset_x + nx * draw_w
                py = offset_y + ny * draw_h
                poly_points.append(QPointF(px, py))
            
            painter.drawPolygon(QPolygonF(poly_points))

        # 2. Draw Handles for Editing Area
        if self.roi_edit_mode and self.roi_edit_area is not None and self.roi_edit_area in self.roi_regions_norm:
            # Yellow for inactive points
            pen_yellow = QPen(QColor(255, 255, 0), 2)
            brush_yellow = QBrush(QColor(255, 255, 0))
            
            # Red for active point
            pen_red = QPen(QColor(255, 0, 0), 2)
            brush_red = QBrush(QColor(255, 0, 0))
            
            points = self.roi_regions_norm.get(self.roi_edit_area, [])
            radius = 5.0
            
            for i, (nx, ny) in enumerate(points):
                px = offset_x + nx * draw_w
                py = offset_y + ny * draw_h
                
                if i == self.roi_active_point_index:
                    painter.setPen(pen_red)
                    painter.setBrush(brush_red)
                else:
                    painter.setPen(pen_yellow)
                    painter.setBrush(brush_yellow)
                
                painter.drawEllipse(QPointF(px, py), radius, radius)

    # -----------------------
    # Bus polling (Qt timer)
    # -----------------------
    def _poll_bus(self):
        if not self._bus or not self._pipeline:
            return

        # 필요한 메시지만 폴링
        while True:
            msg = self._bus.pop_filtered(
                Gst.MessageType.ERROR
                | Gst.MessageType.EOS
                | Gst.MessageType.STATE_CHANGED
            )
            if not msg:
                break

            t = msg.type
            if t == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                allow, suppressed = should_log(f"gst_err_{self.camera_key}", 60)
                if allow:
                    log_msg = f"[VideoWidget][{self.camera_key}] Error: {err.message} | Debug: {debug}" + (f" (suppressed {suppressed})" if suppressed > 0 else "")
                    logger.error(log_msg)
                self.update_label_signal.emit(f"Error: {err.message}")
                self._schedule_reconnect("Bus Error")

            elif t == Gst.MessageType.EOS:
                logger.warning(f"[VideoWidget][{self.camera_key}] EOS")
                self.update_label_signal.emit("EOS")
                self._schedule_reconnect("EOS")

            elif t == Gst.MessageType.STATE_CHANGED:
                if msg.src == self._pipeline:
                    old, new, pending = msg.parse_state_changed()
                    if new == Gst.State.PLAYING:
                        # 재생 성공 시 백오프 리셋
                        if self.retry_count > 0:
                            logger.info(f"[{self.camera_key}] Recovered. Reset backoff.")
                            self.retry_count = 0
                            self.backoff_ms = 1000
                        self.update_label_signal.emit("")
                        self._apply_video_overlay_handle()
                        self._apply_render_rect()

    # -----------------------
    # VideoOverlay helpers
    # -----------------------
    def _apply_video_overlay_handle(self):
        if self.use_qt_overlay:
            return
        if not self._sink or not self._win_id:
            return
        try:
            GstVideo.VideoOverlay.set_window_handle(self._sink, self._win_id)
            # print(f"[VideoWidget][{self.camera_key}] set_window_handle called with {self._win_id}")
        except Exception:
            pass

    def _apply_render_rect(self):
        if self.use_qt_overlay:
            return
        if not self._sink or not self._win_id:
            return
        try:
            w = self.video_area.width()
            h = self.video_area.height()
            GstVideo.VideoOverlay.set_render_rectangle(self._sink, 0, 0, w, h)
        except Exception:
            pass

    # -----------------------
    # UI label
    # -----------------------
    @Slot(str)
    def _update_label_text(self, text):
        self._msg_label.setText(text)
        if text:
            self._msg_label.show()
            self._msg_label.raise_()
        else:
            self._msg_label.hide()

    # -----------------------
    # reconnect
    # -----------------------
    def _schedule_reconnect(self, reason):
        if self.is_stopping:
            return

        self.stop()
        
        self.retry_count += 1
        allow, suppressed = should_log(f"video_reconnect_{self.camera_key}", 60)
        if allow:
            msg = f"[VideoWidget][{self.camera_key}] Reconnecting({reason}) in {self.backoff_ms}ms... (Try {self.retry_count})" + (f" (suppressed {suppressed})" if suppressed > 0 else "")
            logger.info(msg)
            self.update_label_signal.emit(f"Retry in {self.backoff_ms/1000:.1f}s")
        
        self.reconnect_timer.start(self.backoff_ms)
        self.backoff_ms = min(self.backoff_ms * 2, 30000) # 최대 30초

    def _reconnect(self):
        self.set_media(self.rtsp_url, self.camera_key, reset_backoff=False)
