import time
import threading
from collections import OrderedDict

class LogRateLimiter:
    def __init__(self, max_keys=1000):
        self._lock = threading.Lock()
        self._last_log_time = OrderedDict() # key: timestamp
        self._suppressed_counts = {} # key: count
        self._max_keys = max_keys

    def should_log(self, key, interval_sec):
        """
        키별로 로깅 허용 여부를 반환합니다.
        Returns: (allowed: bool, suppressed_count: int)
        """
        with self._lock:
            now = time.time()
            last_time = self._last_log_time.get(key, 0)
            
            if now - last_time < interval_sec:
                self._suppressed_counts[key] = self._suppressed_counts.get(key, 0) + 1
                return False, 0
            
            # 허용됨
            suppressed = self._suppressed_counts.pop(key, 0)
            self._last_log_time[key] = now
            self._last_log_time.move_to_end(key) # LRU 갱신
            
            # 키가 너무 많으면 오래된 것 삭제 (메모리 누수 방지)
            if len(self._last_log_time) > self._max_keys:
                self._last_log_time.popitem(last=False)
                
            return True, suppressed

# 전역 인스턴스
_limiter = LogRateLimiter()

def should_log(key, interval_sec=60):
    """전역 레이트 리미터 헬퍼 함수"""
    return _limiter.should_log(key, interval_sec)
