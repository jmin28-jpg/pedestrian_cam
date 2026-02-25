import time
import threading
import os
from log import get_logger
from log_rate_limit import should_log

logger = get_logger(__name__)

try:
    import RPi.GPIO as GPIO
    HAS_GPIO = True
except ImportError:
    HAS_GPIO = False

GPIO_OUTPUT_PIN = 17

class GpioBridge:
    def __init__(self, config_manager):
        self.cfg = config_manager
        self.lock = threading.Lock()
        # [수정] 단일 펄스 상태 관리 (area_id 구분 없음)
        self._pulse_end_time = 0.0
        self._worker_thread = None
        self._stop_worker = False
        self.is_connected = False
        self.has_gpio = HAS_GPIO
        
        # 콘솔 로그 출력 여부 결정 (환경변수 우선 > 설정파일 > 기본값 False)
        conf = self.cfg.get_gpio_config()
        env_log = os.environ.get("GPIO_CONSOLE_LOG", "").strip()
        if env_log == "1":
            self.console_log = True
        else:
            self.console_log = conf.get('console_log', False)
            
        self.setup()

    def _log(self, msg, level='info'):
        """로그 통합 처리: 파일 로그는 항상 기록(레벨별), 콘솔 출력은 설정(console_log)에 따름"""
        # 1. 파일 로그 (logger 사용)
        if level == 'error':
            logger.error(f"[GPIO] {msg}")
        elif level == 'warning':
            logger.warning(f"[GPIO] {msg}")
        elif level == 'info':
            logger.info(f"[GPIO] {msg}")
        else:
            logger.debug(f"[GPIO] {msg}") # 일반 정보는 디버그 레벨로 파일 저장

        # 2. 콘솔 출력 (토글 확인)
        if self.console_log and level != 'debug':
            print(f"[GPIO] {msg}")

    def setup(self):
        if not HAS_GPIO:
            self._log("RPi.GPIO module not found. Running in mock mode.", 'warning')
            return
        
        conf = self.cfg.get_gpio_config()
        if not conf['enable']:
            return

        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            
            # Force GPIO 17 (BCM) setup using constant
            GPIO.setup(GPIO_OUTPUT_PIN, GPIO.OUT, initial=GPIO.LOW)
            self.is_connected = True
            self._log(f"Setup complete. Fixed Pin: {GPIO_OUTPUT_PIN} (BCM)")
        except Exception as e:
            self._log(f"Setup failed: {e}", 'error')
            self.is_connected = False

    def trigger_pulse(self, area_id: int):
        """통합 펄스 트리거: 무조건 GPIO 17 사용, 단일 상태 머신"""
        conf = self.cfg.get_gpio_config()
        if not conf['enable']: return

        # Force Pin 17 using constant
        pin = GPIO_OUTPUT_PIN

        if not HAS_GPIO:
            return

        if not self.is_connected:
            return

        pulse_sec = conf['pulse_ms'] / 1000.0
        policy = conf.get('retrigger_policy', 'extend')
        now = time.time()
        new_end_time = now + pulse_sec

        with self.lock:
            # 현재 펄스가 진행 중인지 확인
            is_active = self._pulse_end_time > now
            
            if is_active:
                if policy == 'ignore':
                    allow, suppressed = should_log(f"gpio_pulse_ignored_area{area_id}", 60)
                    if allow:
                        self._log(f"Pulse ignored (Policy: ignore). Active until {self._pulse_end_time:.2f} (Req Area: {area_id})" + (f" (suppressed {suppressed})" if suppressed > 0 else ""))
                    return
                
                # Default 'extend' -> 'restart' (Reset end time to now + duration)
                # 기존 펄스가 진행 중이면 종료 시간을 연장(재설정)
                self._pulse_end_time = new_end_time
                
                # [안전장치] 펄스는 활성 상태이나 워커가 죽어있을 경우 재시작 (LOW 보장)
                if self._worker_thread is None or not self._worker_thread.is_alive():
                    self._stop_worker = False
                    self._worker_thread = threading.Thread(target=self._pulse_worker, args=(pin,), daemon=True)
                    self._worker_thread.start()
                    self._log(f"Pulse worker restarted for active pulse (Area {area_id})", level='warning')
                
                allow, suppressed = should_log(f"gpio_pulse_restarted_area{area_id}", 60)
                if allow:
                    self._log(f"Pulse restarted by Area {area_id}. New end: {self._pulse_end_time:.2f}" + (f" (suppressed {suppressed})" if suppressed > 0 else ""))
                return

            # 새로운 펄스 시작
            self._pulse_end_time = new_end_time
            try:
                GPIO.output(pin, GPIO.HIGH)
                self._log(f"Pulse STARTED (Area {area_id}, {pulse_sec}s)", level='info')
                
                # 워커 스레드가 없거나 죽었으면 새로 시작
                if self._worker_thread is None or not self._worker_thread.is_alive():
                    self._stop_worker = False
                    self._worker_thread = threading.Thread(target=self._pulse_worker, args=(pin,), daemon=True)
                    self._worker_thread.start()
            except Exception as e:
                self._log(f"Output error on pin {pin}: {e}", 'error')

    def _pulse_worker(self, pin):
        """단일 워커: 펄스 종료 시간을 감시하고 LOW로 내림"""
        while not self._stop_worker:
            with self.lock:
                remaining = self._pulse_end_time - time.time()
                if remaining <= 0:
                    try:
                        GPIO.output(pin, GPIO.LOW)
                        # [Commit 18-fix] Pulse END 로그 완전 제거 (정책 준수)
                    except Exception as e:
                        self._log(f"Low error: {e}", 'error')
                    self._pulse_end_time = 0.0
                    break # 펄스 종료 시 스레드 탈출
            
            # 남은 시간만큼 대기하되, 반응성을 위해 최대 0.05초 단위로 쪼개서 대기
            sleep_time = min(0.05, remaining) if remaining > 0 else 0.05
            time.sleep(max(0.01, sleep_time))

    def on_stay_event(self, ip, area_id, action="Start"):
        """(구) 체류 이벤트 호환용 래퍼 - trigger_pulse로 통합"""
        if action == "Start":
            self.trigger_pulse(int(area_id))

    def cleanup(self):
        if HAS_GPIO and self.is_connected:
            try:
                # 워커 종료 신호
                self._stop_worker = True

                # [안전] 모든 핀 LOW 우선 수행
                conf = self.cfg.get_gpio_config()
                if conf['enable']:
                    GPIO.output(GPIO_OUTPUT_PIN, GPIO.LOW)

                # 스레드 정리 (타임아웃 짧게)
                if self._worker_thread and self._worker_thread.is_alive():
                    self._worker_thread.join(timeout=0.2)
                
                # 상태 초기화
                with self.lock:
                    self._pulse_end_time = 0.0

                GPIO.cleanup(GPIO_OUTPUT_PIN)
                self._log("Cleanup done.")
            except Exception as e:
                self._log(f"Cleanup error: {e}", 'error')
            finally:
                self.is_connected = False
