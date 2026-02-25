import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from logging import FileHandler
import app_paths

# 전역 파일 핸들러 및 날짜 관리 (싱글톤 패턴 적용)
_file_handler = None
_current_date_str = None

# 내부 디버그용 콘솔 출력 제어 (기본값 False)
LOG_INTERNAL_CONSOLE = os.environ.get("LOG_INTERNAL_CONSOLE") == "1"

class ConsoleFilter(logging.Filter):
    """
    콘솔 출력 정책:
    - WARNING 이상은 무조건 출력
    - INFO 이하는 특정 태그([Main], [Recovery], [DB], [Camera])가 포함된 경우만 출력
    """
    def filter(self, record):
        if record.levelno >= logging.WARNING:
            return True
        return any(tag in record.getMessage() for tag in ["[Main]", "[Recovery]", "[DB]", "[Camera]"])

def get_logger(name: str) -> logging.Logger:
    """
    프로젝트 표준 로거를 반환합니다.
    - 콘솔 출력 (INFO 이상)
    - 파일 출력 (logs/app.log, DEBUG 이상, Rotating)
    """
    global _file_handler
    logger = logging.getLogger(name)
    
    # [Commit REC-3] Propagate 차단 (상위 로거 전파 방지)
    logger.propagate = False

    logger.setLevel(logging.DEBUG)

    # 로그 포맷
    formatter = logging.Formatter(
        '[%(asctime)s][%(levelname)s][%(module)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 핸들러 중복 확인
    has_console = False
    has_file = False
    
    for h in logger.handlers:
        if isinstance(h, logging.FileHandler):
            has_file = True
        elif isinstance(h, logging.StreamHandler):
            has_console = True

    # 1. 콘솔 핸들러
    if not has_console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO) # 레벨은 INFO로 두되 필터로 제어
        console_handler.addFilter(ConsoleFilter())
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    # 2. 파일 핸들러
    if not has_file:
        if _file_handler is None:
            _setup_initial_file_handler(formatter)
        
        if _file_handler:
            logger.addHandler(_file_handler)

    return logger

def _setup_initial_file_handler(formatter):
    """최초 파일 핸들러 설정 (내부용)"""
    global _file_handler, _current_date_str
    try:
        log_dir = app_paths.get_log_dir()
        
        today_str = datetime.now().strftime("%y%m%d")
        log_filename = f"{today_str}_log.log"
        log_file = log_dir / log_filename

        handler = FileHandler(log_file, encoding='utf-8')
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(formatter)
        
        _file_handler = handler
        _current_date_str = today_str
    except Exception as e:
        if LOG_INTERNAL_CONSOLE:
            print(f"[log.py] Failed to setup file logging: {e}")

def check_and_rotate_log():
    """
    현재 날짜를 확인하여 날짜가 변경되었으면 로그 파일을 교체합니다.
    (자정 롤오버 구현: YYMMDD_log.log 형식 유지)
    """
    global _file_handler, _current_date_str
    
    if _file_handler is None:
        return

    today_str = datetime.now().strftime("%y%m%d")
    if _current_date_str == today_str:
        return # 날짜 변경 없음

    if LOG_INTERNAL_CONSOLE:
        print(f"[Log] Date changed: {_current_date_str} -> {today_str}. Rotating log file.")

    try:
        log_dir = app_paths.get_log_dir()
        log_filename = f"{today_str}_log.log"
        log_file = log_dir / log_filename
        
        formatter = logging.Formatter(
            '[%(asctime)s][%(levelname)s][%(module)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        new_handler = FileHandler(log_file, encoding='utf-8')
        new_handler.setLevel(logging.DEBUG)
        new_handler.setFormatter(formatter)
        
        old_handler = _file_handler
        
        # 1. 루트 로거 처리
        root = logging.getLogger()
        if old_handler in root.handlers:
            root.removeHandler(old_handler)
            root.addHandler(new_handler)
            
        # 2. 모든 활성 로거 순회하며 핸들러 교체
        for logger_name in logging.Logger.manager.loggerDict:
            logger = logging.getLogger(logger_name)
            # Placeholder 로거 제외
            if not isinstance(logger, logging.Logger):
                continue
            
            if old_handler in logger.handlers:
                logger.removeHandler(old_handler)
                logger.addHandler(new_handler)
        
        # 구형 핸들러 닫기
        old_handler.close()
        
        # 전역 상태 갱신
        _file_handler = new_handler
        _current_date_str = today_str
        
        if LOG_INTERNAL_CONSOLE:
            print(f"[Log] Log rotation complete: {log_filename}")
        
    except Exception as e:
        if LOG_INTERNAL_CONSOLE:
            print(f"[Log] Rotation failed: {e}")

def cleanup_old_logs(retention_days: int):
    """
    logs/ 디렉토리 내의 날짜 형식 로그 파일 중 보존 기한이 지난 파일을 삭제합니다.
    안전장치:
    1. logs/ 디렉토리 내부 파일만 대상
    2. 파일명 패턴(YYMMDD_log.log) 일치 필수
    3. 날짜 파싱 성공 필수
    """
    try:
        log_dir = app_paths.get_log_dir()
        if not log_dir.exists():
            return

        cutoff_date = datetime.now() - timedelta(days=retention_days)
        # 정규식: 숫자6자리_log.log (예: 260210_log.log)
        pattern = re.compile(r"^(\d{6})_log\.log$")

        if LOG_INTERNAL_CONSOLE:
            print(f"[LogCleanup] Checking logs older than {retention_days} days (Cutoff: {cutoff_date.strftime('%y%m%d')})")

        for file_path in log_dir.iterdir():
            if not file_path.is_file():
                continue
            
            # 1. 파일명 패턴 확인
            match = pattern.match(file_path.name)
            if not match:
                continue
            
            # 2. 날짜 파싱 확인
            try:
                date_str = match.group(1)
                file_date = datetime.strptime(date_str, "%y%m%d")
            except ValueError:
                continue
            
            # 3. 날짜 비교 (오래된 파일 삭제)
            if file_date < cutoff_date:
                try:
                    os.remove(file_path)
                    if LOG_INTERNAL_CONSOLE:
                        print(f"[LogCleanup] Deleted old log: {file_path.name}")
                except Exception as e:
                    if LOG_INTERNAL_CONSOLE:
                        print(f"[LogCleanup] Failed to delete {file_path.name}: {e}")
                    
    except Exception as e:
        if LOG_INTERNAL_CONSOLE:
            print(f"[LogCleanup] Error: {e}")
