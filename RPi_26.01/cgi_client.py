import requests
from requests.auth import HTTPDigestAuth
import time
import json
import re
import threading
from datetime import datetime
from PySide6.QtCore import QObject, Signal
from log import get_logger
from log_rate_limit import should_log

# 정규식 패턴 (Windows 버전 호환)
_PATTERN_ENABLE = re.compile(r"VideoAnalyseRule\[0\]\[(\d+)\]\.Enable\s*=\s*(true|false)", re.IGNORECASE)
_PATTERN_AREAID = re.compile(r"VideoAnalyseRule\[0\]\[(\d+)\]\.Config\.AreaID\s*=\s*(\d+)", re.IGNORECASE)
_PATTERN_B = re.compile(r"(?:table\s*\.)?VideoAnalyseRule\[\d+\]\[(\d+)\]\.Config\.DetectRegion\[\d+\]\[(\d+)\]\s*=\s*(\d+)\s*,\s*(\d+)", re.IGNORECASE)
_PATTERN_C = re.compile(r"(?:table\s*\.)?VideoAnalyseRule\[\d+\]\[(\d+)\]\.Config\.DetectRegion\[(\d+)\]\[(\d+)\]\s*=\s*(\d+)", re.IGNORECASE)
_PATTERN_ENABLE_IDX = re.compile(r"VideoAnalyseRule\[0\]\[(\d+)\]\.Enable\s*=\s*true", re.IGNORECASE)

logger = get_logger(__name__)


def build_rtsp_url(camera_config: dict) -> str:
    """
    카메라 설정 딕셔너리를 받아 Dahua RTSP URL을 생성합니다.
    Format: rtsp://user:password@ip:554/cam/realmonitor?channel=1&subtype=0
    """
    ip = camera_config.get('ip', '0.0.0.0')
    user = camera_config.get('username', 'admin')
    password = camera_config.get('password', 'admin')
    channel = camera_config.get('channel', '1')
    # RTSP 포트: 설정 없으면 554 기본값
    rtsp_port = camera_config.get('rtsp_port', '554')
    
    # main_stream이 true면 subtype=0 (Main), 아니면 1 (Sub)
    is_main = str(camera_config.get('main_stream', 'true')).lower() == 'true'
    subtype = 0 if is_main else 1
    
    url = f"rtsp://{user}:{password}@{ip}:{rtsp_port}/cam/realmonitor?channel={channel}&subtype={subtype}"
    return url

class _BaseCgiThread(QObject):
    """
    CGI 스트림 구독을 위한 공통 스레드 베이스 클래스.
    연결 관리, 백오프, 스레드 제어 로직을 통합.
    """
    # type: DEBUG, PEOPLE_COUNT, STAY_ALARM
    event_received = Signal(dict)

    def __init__(self, camera_key, ip, port, user, password):
        super().__init__()
        self.camera_key = camera_key
        self.ip = ip
        self.port = port
        self.user = user
        self.password = password
        self._stop_event = threading.Event()
        self._thread = None
        self.session = None
        self._last_rx_ts = time.time() # [추가] 마지막 데이터 수신 시간

    def _log_rate_limit(self, key, msg, interval=10):
        """스레드 내부용 레이트 리밋 로그 (Signal emit)"""
        unique_key = f"cgi_dbg_{self.camera_key}_{key}"
        allowed, suppressed = should_log(unique_key, interval)
        if allowed:
            suffix = f" (suppressed {suppressed})" if suppressed > 0 else ""
            self.event_received.emit({"type": "DEBUG", "message": f"[{self.camera_key}] {msg}{suffix}"})

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()

    def restart(self):
        """스레드를 안전하게 재시작합니다."""
        self.stop()
        self.wait(1000) # 1초 대기
        self.start()

    def stop(self):
        self._stop_event.set()
        # 블로킹된 I/O 강제 종료를 위해 세션 닫기
        if self.session:
            try:
                self.session.close()
            except Exception:
                pass
        
    def wait(self, timeout_ms=None):
        if self._thread:
            t = timeout_ms / 1000.0 if timeout_ms is not None else None
            self._thread.join(t)

    def isRunning(self):
        return self._thread is not None and self._thread.is_alive()

    def get_last_rx_ts(self):
        return self._last_rx_ts

    def _get_url(self):
        raise NotImplementedError

    def _get_log_prefix(self):
        """로그 메시지용 접두사 (예: PeopleCount, StayDetection)"""
        raise NotImplementedError

    def _get_url_log_key(self):
        """URL 로그용 키 (예: PEOPLE_URL, STAY_URL)"""
        raise NotImplementedError

    def _consume_stream(self, response):
        """스트림 응답 처리 (하위 클래스 구현)"""
        raise NotImplementedError

    def run(self):
        url = self._get_url()
        log_prefix = self._get_log_prefix()
        backoff = 1
        
        # 실제 사용 URL 로그
        self.event_received.emit({"type": "DEBUG", "message": f"[{self.camera_key}] {self._get_url_log_key()}={url}"})

        self.session = requests.Session()
        self.session.auth = HTTPDigestAuth(self.user, self.password)

        while not self._stop_event.is_set():
            try:
                # session.get 사용 (auth는 session에 설정됨)
                with self.session.get(
                    url, 
                    stream=True, 
                    timeout=(5, 15)  # ReadTimeout을 짧게 주어 종료 플래그 확인 유도
                ) as response:
                    
                    if response.status_code == 200:
                        backoff = 1
                        self.event_received.emit({"type": "DEBUG", "message": f"{log_prefix} Connected: {self.ip}"})
                        self._consume_stream(response)
                    else:
                        self.event_received.emit({"type": "DEBUG", "message": f"{log_prefix} HTTP {response.status_code}"})

            except requests.exceptions.ReadTimeout:
                # 타임아웃은 정상적인 흐름(데이터 없음)일 수 있음 -> 루프 계속
                self._last_rx_ts = time.time() # [Commit 15-1] 타임아웃도 생존 신호로 간주
                continue
            except Exception as e:
                # Stop 신호가 켜져있으면 에러 로그 무시 (의도된 종료)
                if not self._stop_event.is_set():
                    # 레이트 리밋 적용 (60초)
                    allow, suppressed = should_log(f"cgi_err_{self.camera_key}", 60)
                    if allow:
                        msg = f"{log_prefix} Exception: {str(e)}" + (f" (suppressed {suppressed})" if suppressed > 0 else "")
                        self.event_received.emit({"type": "DEBUG", "message": msg})
            
            if not self._stop_event.is_set():
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

class PeopleCountThread(_BaseCgiThread):
    """
    Dahua videoStatServer.cgi 스트림을 구독하여 인원수 데이터를 수신하는 스레드.
    """
    def __init__(self, camera_key, ip, port, user, password, channel=1):
        super().__init__(camera_key, ip, port, user, password)
        self.channel = channel

    def _get_url(self):
        return f"http://{self.ip}:{self.port}/cgi-bin/videoStatServer.cgi?action=attach&channel={self.channel}&heartbeat=1"

    def _get_log_prefix(self):
        return "PeopleCount"

    def _get_url_log_key(self):
        return "PEOPLE_URL"

    def _consume_stream(self, response):
        current_area_id = None
        for line in response.iter_lines():
            self._last_rx_ts = time.time() # Update RX timestamp
            if self._stop_event.is_set():
                break
            if line:
                try:
                    decoded_line = line.decode('utf-8', errors='ignore').strip()
                except Exception:
                    continue
                
                # 1121 파싱 로직
                if decoded_line.startswith("summary.AreaID="):
                    try:
                        current_area_id = int(decoded_line.split("=")[1])
                    except Exception as e:
                        current_area_id = None
                        self._log_rate_limit("pc_area_id", f"AreaID parse error: {e} in {decoded_line[:40]}")
                elif decoded_line.startswith("summary.InsideSubtotal.Total=") and current_area_id is not None:
                    try:
                        count = int(decoded_line.split("=")[1])
                        now_epoch = int(time.time())
                        self.event_received.emit({
                            "type": "PEOPLE_COUNT",
                            "camera_key": self.camera_key,
                            "area_id": current_area_id,
                            "count": count,
                            "ts": datetime.fromtimestamp(now_epoch).strftime("%Y-%m-%d %H:%M:%S"),
                            "ts_epoch": now_epoch
                        })
                    except Exception as e:
                        self._log_rate_limit("pc_count", f"Count parse error: {e} in {decoded_line[:40]}")

class StayDetectionThread(_BaseCgiThread):
    """
    Dahua eventManager.cgi 스트림을 구독하여 체류(StayDetection) 이벤트를 수신하는 스레드.
    """
    def __init__(self, camera_key, ip, port, user, password):
        super().__init__(camera_key, ip, port, user, password)

    def _get_url(self):
        return f"http://{self.ip}:{self.port}/cgi-bin/eventManager.cgi?action=attach&codes=[StayDetection]"

    def _get_log_prefix(self):
        return "StayDetection"

    def _get_url_log_key(self):
        return "STAY_URL"

    def _consume_stream(self, response):
        buffer = b''
        boundary = b'--myboundary'
        
        for chunk in response.iter_content(chunk_size=1024):
            self._last_rx_ts = time.time() # Update RX timestamp
            if self._stop_event.is_set():
                break
            if not chunk: continue
            buffer += chunk
            while True:
                idx = buffer.find(boundary)
                if idx == -1: break
                part = buffer[:idx]
                buffer = buffer[idx+len(boundary):]
                text = part.decode(errors='ignore')
                
                if 'Code=StayDetection' in text:
                    self._parse_stay_event(text)

    def _parse_stay_event(self, text):
        try:
            action_match = re.search(r'action=(Start|Stop)', text)
            data_match = re.search(r'data=({.*?})', text, re.DOTALL)
            
            if action_match and data_match:
                action = action_match.group(1)
                data = json.loads(data_match.group(1))
                area_id = data.get('AreaID')
                
                now_epoch = int(time.time())
                self.event_received.emit({
                    "type": "STAY_ALARM",
                    "camera_key": self.camera_key,
                    "action": action,
                    "area_id": area_id,
                    "ts": datetime.fromtimestamp(now_epoch).strftime("%Y-%m-%d %H:%M:%S"),
                    "ts_epoch": now_epoch
                })
        except Exception as e:
            self._log_rate_limit("stay_parse", f"Stay parse error: {e}")

def fetch_region_data(ip: str, user: str, password: str) -> str | None:
    """
    VideoAnalyseRule CGI에서 영역 좌표 설정 원문을 가져옵니다. (1121 이식)
    """
    url = f"http://{ip}/cgi-bin/configManager.cgi?action=getConfig&name=VideoAnalyseRule"
    try:
        resp = requests.get(url, auth=HTTPDigestAuth(user, password), timeout=5)
        if resp.status_code == 200 and resp.text:
            return resp.text
        else:
            if should_log(f"fetch_{ip}", 60)[0]:
                logger.error(f"[CGI] fetch_region_data failed: HTTP {resp.status_code}")
            return None
    except Exception as e:
        if should_log(f"fetch_{ip}", 60)[0]:
            logger.error(f"[CGI] fetch_region_data error: {e}")
        return None

def get_roi_raw_data(ip: str, user: str, password: str) -> str | None:
    """ROI 설정을 위한 원문 데이터를 가져옵니다. (fetch_region_data 래퍼)"""
    return fetch_region_data(ip, user, password)

def parse_region_count(cgi_text: str) -> int:
    """CGI 텍스트에서 활성화된 영역 개수를 파싱합니다. (1121 이식)"""
    if not cgi_text:
        return 0
    rule_indices = set(_PATTERN_ENABLE_IDX.findall(cgi_text))
    count = 0
    for idx in rule_indices:
        areaid_pattern = rf'VideoAnalyseRule\[0\]\[{idx}\]\.Config\.AreaID='
        region_pattern = rf'VideoAnalyseRule\[0\]\[{idx}\]\.Config\.DetectRegion\[0\]\[0\]='
        if re.search(areaid_pattern, cgi_text, re.IGNORECASE) and re.search(region_pattern, cgi_text, re.IGNORECASE):
            count += 1
    return count

def get_roi_config(ip, user, password):
    """
    VideoAnalyseRule 설정을 가져와서 AreaID별 활성화 상태를 반환함.
    Returns: { area_id (int): { 'index': int, 'enable': bool }, ... }
    """
    text = fetch_region_data(ip, user, password)
    if not text:
        return {}
    
    config = {}
    # Regex to find indices e.g. table.VideoAnalyseRule[0][0]
    indices = set(re.findall(r'VideoAnalyseRule\[0\]\[(\d+)\]', text))
    
    for idx in indices:
        # Extract AreaID and Enable status
        area_id_match = re.search(rf'VideoAnalyseRule\[0\]\[{idx}\]\.Config\.AreaID=(\d+)', text)
        enable_match = re.search(rf'VideoAnalyseRule\[0\]\[{idx}\]\.Enable=(true|false)', text, re.IGNORECASE)
        
        if area_id_match:
            area_id = int(area_id_match.group(1))
            enable = (enable_match.group(1).lower() == 'true') if enable_match else False
            
            config[area_id] = {
                'index': int(idx),
                'enable': enable
            }
    return config

def set_roi_enable(ip, user, password, updates):
    """
    updates: list of (index, enable_bool)
    """
    if not updates:
        return True
        
    params = []
    for idx, enable in updates:
        val = "true" if enable else "false"
        params.append(f"VideoAnalyseRule[0][{idx}].Enable={val}")
    
    query = "&".join(params)
    url = f"http://{ip}/cgi-bin/configManager.cgi?action=setConfig&{query}"
    
    try:
        resp = requests.get(url, auth=HTTPDigestAuth(user, password), timeout=5)
        return resp.status_code == 200 and "OK" in resp.text
    except Exception as e:
        if should_log(f"set_roi_{ip}", 60)[0]:
            logger.error(f"[CGI] set_roi_enable error: {e}")
        return False

def parse_regions_by_area_raw(cgi_text: str, max_areas: int = 4) -> dict[int, list[tuple[int, int]]]:
    """
    룰 인덱스와 AreaID 매핑을 이용해 Area별 원시 좌표(0..8192)를 반환합니다.
    :return: {area_id: [(x,y), ...]} 딕셔너리
    """
    if not cgi_text:
        return {}
    
    try:
        # 1) 룰 인덱스별 AreaID 수집
        areaid_by_rule = {}
        for m in _PATTERN_AREAID.finditer(cgi_text):
            ridx = int(m.group(1)); aid = int(m.group(2))
            areaid_by_rule[ridx] = aid

        # 2) 룰 인덱스별 좌표 수집 (B, C 형식 모두 지원)
        points_by_rule = {}
        # B: ...DetectRegion[0][pointIdx]=x,y  → (x,y) 직접 저장
        for m in _PATTERN_B.finditer(cgi_text):
            try:
                ridx = int(m.group(1)); pidx = int(m.group(2))
                x = int(m.group(3)); y = int(m.group(4))
                points_by_rule.setdefault(ridx, {})[pidx] = (x, y)
            except Exception:
                continue
        # C: ...DetectRegion[pointIdx][xyIdx]=val → xyIdx 0/1로 결합
        if not points_by_rule:
            tmp_by_rule = {}
            for m in _PATTERN_C.finditer(cgi_text):
                try:
                    ridx = int(m.group(1)); pidx = int(m.group(2)); xy = int(m.group(3)); val = int(m.group(4))
                    tmp_by_rule.setdefault(ridx, {}).setdefault(pidx, {})[xy] = val
                except Exception:
                    continue
            for ridx, pmap in tmp_by_rule.items():
                for pidx, xy in pmap.items():
                    if 0 in xy and 1 in xy:
                        points_by_rule.setdefault(ridx, {})[pidx] = (xy[0], xy[1])

        # 3) 룰→AreaID 매핑을 이용해 AreaID별로 정리
        result = {}
        for ridx, pts_map in points_by_rule.items():
            aid = areaid_by_rule.get(ridx)
            if not aid or not (1 <= aid <= max_areas):
                continue
            ordered = [pts_map[i] for i in sorted(pts_map.keys()) if i in pts_map]
            if len(ordered) > 1:
                result[aid] = ordered
        
        # 디버그 로그 (파싱 결과 요약)
        # print(f"[CGI] Parsed Areas: {list(result.keys())}, TextLen: {len(cgi_text)}")
        return result
    except Exception as e:
        logger.error(f"[CGI] Parse Error: {e}")
        return {}

def get_rule_index_for_area(ip: str, user: str, password: str, area_id: int) -> int | None:
    """지정 카메라에서 AreaID에 대응하는 ruleIndex를 조회합니다."""
    try:
        text = fetch_region_data(ip, user, password)
        if not text:
            return None
        areaid_by_rule = {}
        for m in _PATTERN_AREAID.finditer(text):
            ridx = int(m.group(1)); aid = int(m.group(2))
            areaid_by_rule[ridx] = aid
        
        for ridx, aid in areaid_by_rule.items():
            if aid == area_id:
                return ridx
        return None
    except Exception as e:
        if should_log(f"get_rule_{ip}", 60)[0]:
            logger.error(f"[CGI] get_rule_index error: {e}")
        return None

def set_detect_region(ip: str, username: str, password: str, rule_index: int, points: list[tuple[int,int]]) -> bool:
    """Dahua 카메라의 DetectRegion 좌표를 설정합니다."""
    try:
        base_url = f"http://{ip}/cgi-bin/configManager.cgi?action=setConfig"
        params = {}
        for i, (x, y) in enumerate(points):
            params[f"VideoAnalyseRule[0][{int(rule_index)}].Config.DetectRegion[{i}][0]"] = str(int(x))
            params[f"VideoAnalyseRule[0][{int(rule_index)}].Config.DetectRegion[{i}][1]"] = str(int(y))
            
        resp = requests.get(base_url, params=params, auth=HTTPDigestAuth(username, password), timeout=5)
        if resp.status_code != 200:
            logger.error(f"[CGI] Set ROI Failed: {resp.status_code} - {resp.text[:100]}")
            return False
        return True
    except Exception as e:
        logger.error(f"[CGI] Set ROI Exception: {e}")
        return False
