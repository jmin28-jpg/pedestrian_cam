import json
import os
from log import get_logger
from log_rate_limit import should_log
import app_paths

logger = get_logger(__name__)

class StateManager:
    def __init__(self):
        self.state_file = str(app_paths.get_state_path())
        
        # 기본 상태 정의
        self.state = {
            "last_camera_key": None,
            "last_area_id": None,
            "last_gpio_port": None,
            "split_mode": "4",
            "enabled_areas": {},  # { "camera_key": { "area_id": bool } }
            "monitor_enabled": {}, # { "camera_key": bool }
            "last_crash_warned_ts": 0
        }
        self.load_state()

    def load_state(self):
        """JSON 파일에서 상태를 로드합니다."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.state.update(data)
            except Exception as e:
                allow, suppressed = should_log("state_load_fail", 300)
                if allow:
                    logger.warning(
                        f"[System] State load failed: {self.state_file} | {e}"
                        + (f" (suppressed {suppressed})" if suppressed else "")
                    )
        
        # [Commit FIX-1] split_mode 'auto' 보정
        raw_split = self.state.get("split_mode", "4")
        try:
            val = int(raw_split)
            if val not in (1, 4):
                self.state["split_mode"] = "4"
        except (ValueError, TypeError):
            self.state["split_mode"] = "4"

    def save_state(self):
        """현재 상태를 JSON 파일로 저장합니다."""
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, indent=4)
        except Exception as e:
            allow, suppressed = should_log("state_save_fail", 300)
            if allow:
                logger.warning(
                    f"[System] State save failed: {self.state_file} | {e}"
                    + (f" (suppressed {suppressed})" if suppressed else "")
                )

    def get(self, key, default=None):
        """상태 값을 가져옵니다."""
        return self.state.get(key, default)

    def set(self, key, value):
        """상태 값을 설정합니다 (저장은 별도 호출 필요)."""
        self.state[key] = value

    def get_area_enabled(self, camera_key, area_id):
        """특정 카메라 영역의 활성 상태를 반환합니다. (기본값 False)"""
        areas = self.state.get("enabled_areas", {}).get(camera_key, {})
        # 문자열 키로 저장되므로 변환하여 조회
        return areas.get(str(area_id), False)

    def set_area_enabled(self, camera_key, area_id, enabled):
        """특정 카메라 영역의 활성 상태를 설정하고 저장합니다."""
        if "enabled_areas" not in self.state:
            self.state["enabled_areas"] = {}
        
        if camera_key not in self.state["enabled_areas"]:
            self.state["enabled_areas"][camera_key] = {}
            
        self.state["enabled_areas"][camera_key][str(area_id)] = enabled
        self.save_state()

    def get_monitor_enabled(self, camera_key):
        """특정 카메라의 모니터링 체크 여부를 반환합니다. (기본값 False)"""
        return self.state.get("monitor_enabled", {}).get(camera_key, False)

    def set_monitor_enabled(self, camera_key, enabled):
        """특정 카메라의 모니터링 체크 여부를 설정하고 저장합니다."""
        if "monitor_enabled" not in self.state:
            self.state["monitor_enabled"] = {}
        self.state["monitor_enabled"][camera_key] = enabled
        self.save_state()

    def clear_all_monitor_enabled(self):
        """모든 카메라의 모니터링 체크 상태를 해제합니다."""
        self.state["monitor_enabled"] = {}
        self.save_state()

    def set_monitor_enabled_bulk(self, keys_true):
        """지정된 키 목록만 모니터링 체크를 True로 설정하고 나머지는 해제합니다."""
        self.state["monitor_enabled"] = {key: True for key in keys_true}
        self.save_state()

    def cleanup_camera_state(self, camera_key):
        """카메라 삭제 시 관련 상태(Area 설정, 마지막 선택 등)를 정리합니다."""
        # Area 설정 제거
        if "enabled_areas" in self.state and camera_key in self.state["enabled_areas"]:
            del self.state["enabled_areas"][camera_key]
        
        # 모니터링 설정 제거
        if "monitor_enabled" in self.state and camera_key in self.state["monitor_enabled"]:
            del self.state["monitor_enabled"][camera_key]

        # 마지막 선택 카메라가 삭제된 카메라면 초기화
        if self.state.get("last_camera_key") == camera_key:
            self.state["last_camera_key"] = None
            self.state["last_area_id"] = None # 관련 area 정보도 초기화 권장
            
        self.save_state()
