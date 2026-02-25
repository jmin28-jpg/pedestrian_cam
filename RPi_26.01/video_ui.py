import os
import sys
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from PySide6.QtWidgets import QWidget, QLabel, QFrame, QStackedLayout, QApplication
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
# [Commit ROI-VID-1] pycairo 의존 제거. cairooverlay element 존재 여부로 판단.
HAS_CAIRO = True


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


class VideoWidget(QWidget):
    update_label_signal = Signal(str)
    clicked = Signal(str) # camera_key
    doubleClicked = Signal(str) # camera_key

    def __init__(self, parent=None):
        super().__init__(parent)

        # Native Window 설정 (GStreamer Overlay 필수)
        self.setAttribute(Qt.WA_NativeWindow, True)
        self.setAttribute(Qt.WA_DontCreateNativeAncestors, True)

        # window_main.py 호환용
        self.is_ready = HAS_GST
        self.isready = self.is_ready  # 혹시 isready를 참조하는 코드가 있으면 같이 살려둠

        self.setStyleSheet("background-color: black;")

        self.camera_key = "Unknown"
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

        # UI
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

        self.update_label_signal.connect(self._update_label_text)

        if not HAS_GST:
            self._msg_label.setText("GStreamer Missing")
            self._msg_label.show()

    # -----------------------
    # Qt events
    # -----------------------
    def showEvent(self, event):
        super().showEvent(event)
        self._win_id = int(self.video_area.winId())
        # sink가 이미 있으면 handle 적용
        self._apply_video_overlay_handle()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_render_rect()

    def mousePressEvent(self, event):
        # [수정] 편집 모드가 아니면 마우스 이벤트 무시
        if not self.roi_edit_mode:
            super().mousePressEvent(event)
            return

        # ROI Edit Mode Logic
        if self.roi_edit_mode and self.roi_edit_area in self.roi_regions_norm:
            points = self.roi_regions_norm[self.roi_edit_area]
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
    def set_roi_edit(self, area_id: int | None, edit_mode: bool):
        """편집 모드 및 대상 영역 설정"""
        self.roi_edit_area = area_id
        self.roi_edit_mode = edit_mode
        self.roi_active_point_index = -1

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

    def set_highlight(self, active: bool):
        pass

    # -----------------------
    # Pipeline builder
    # -----------------------
    def _build_pipeline(self, rtsp_url: str):
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

        vc1 = Gst.ElementFactory.make("videoconvert", f"videoconvert1_{self.camera_key}")
        vs = Gst.ElementFactory.make("videoscale", f"videoscale_{self.camera_key}")

        down_filter = None
        max_w = os.environ.get("CCTV_MAX_WIDTH", "").strip()
        max_h = os.environ.get("CCTV_MAX_HEIGHT", "").strip()
        if max_w and max_h:
            down_filter = Gst.ElementFactory.make("capsfilter", f"downcap_{self.camera_key}")
            down_caps = Gst.Caps.from_string(f"video/x-raw,width={int(max_w)},height={int(max_h)}")
            down_filter.set_property("caps", down_caps)
            logger.info(f"[VideoWidget][{self.camera_key}] Downscale caps: video/x-raw,width={max_w},height={max_h}")

        # overlay (optional)
        # RPi에서는 cairooverlay를 사용하여 ROI를 그립니다.
        enable_overlay = True 
        vc_before_ov = None
        cairo_overlay = None
        vc_after_ov = None

        if enable_overlay:
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

        # sink
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

        if cairo_overlay:
            self._pipeline.add(vc_before_ov)
            self._pipeline.add(cairo_overlay)
            self._pipeline.add(vc_after_ov)

        self._pipeline.add(sink)

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

        if cairo_overlay:
            if not (tail_start.link(vc_before_ov) and vc_before_ov.link(cairo_overlay) and cairo_overlay.link(vc_after_ov) and vc_after_ov.link(self._sink)):
                raise RuntimeError("Failed to link overlay chain")
        else:
            if not tail_start.link(sink):
                raise RuntimeError("Failed to link tail_start -> sink")

        # dynamic pads
        src.connect("pad-added", self._on_rtspsrc_pad_added)

        # bus
        self._bus = self._pipeline.get_bus()
        self.bus_timer.start()

        # window handle
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

    def _on_draw_overlay(self, overlay, context, timestamp, duration):
        if not self.roi_visible:
            return
            
        # Draw 콜백 내 로그 출력 완전 제거 (성능/도배 방지)

        try:
            # 1. Get Dimensions
            # Try to get from surface first (most accurate for cairo context)
            draw_w, draw_h = 0.0, 0.0
            try:
                target = context.get_target()
                draw_w = float(target.get_width())
                draw_h = float(target.get_height())
            except Exception:
                pass
            
            # Fallback to caps width/height
            if draw_w <= 0 or draw_h <= 0:
                draw_w = float(self._src_width)
                draw_h = float(self._src_height)
            
            if draw_w <= 0 or draw_h <= 0:
                return
            
            # 유효한 크기일 때만 업데이트
            self.last_draw_w = draw_w
            self.last_draw_h = draw_h

            if draw_w <= 0 or draw_h <= 0:
                return

            # 2. Draw All Enabled Regions (Green Lines)
            context.set_line_width(2.0)
            
            # 녹색 라인 (Windows 스타일)
            context.set_source_rgba(0.0, 1.0, 0.0, 1.0) 

            for area_id, points in self.roi_regions_norm.items():
                # [수정] window_main에서 이미 필터링된 데이터를 주므로 여기서는 enabled 체크만 수행
                # (Edit 모드일 때는 window_main이 target_area를 enabled에 포함시켜서 보냄)
                if area_id not in self.roi_enabled_areas:
                    continue
                    
                if len(points) < 2: continue
                
                px_pts = [(p[0] * draw_w, p[1] * draw_h) for p in points]
                context.move_to(px_pts[0][0], px_pts[0][1])
                for i in range(1, len(px_pts)):
                    context.line_to(px_pts[i][0], px_pts[i][1])
                context.close_path()
                context.stroke()

            # 3. Draw Handles for Editing Area (Yellow Circles)
            if self.roi_edit_mode and self.roi_edit_area is not None and self.roi_edit_area in self.roi_regions_norm:
                context.set_source_rgba(1.0, 1.0, 0.0, 1.0) # Yellow
                radius = 5.0 # pixel radius
                
                points = self.roi_regions_norm.get(self.roi_edit_area, [])
                px_pts = [(p[0] * draw_w, p[1] * draw_h) for p in points]
                
                for i, (px, py) in enumerate(px_pts):
                    # Active point highlight
                    if i == self.roi_active_point_index:
                        context.set_source_rgba(1.0, 0.0, 0.0, 1.0) # Red
                    else:
                        context.set_source_rgba(1.0, 1.0, 0.0, 1.0) # Yellow
                        
                    context.arc(px, py, radius, 0, 2 * 3.14159)
                    context.fill()
                    
        except Exception:
            pass

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
        if not self._sink or not self._win_id:
            return
        try:
            GstVideo.VideoOverlay.set_window_handle(self._sink, self._win_id)
            # print(f"[VideoWidget][{self.camera_key}] set_window_handle called with {self._win_id}")
        except Exception:
            pass

    def _apply_render_rect(self):
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
