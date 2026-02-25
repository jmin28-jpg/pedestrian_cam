import configparser
import os
import re
import shutil
import sys
from pathlib import Path
from PySide6.QtCore import QByteArray
from log import get_logger
import app_paths

logger = get_logger(__name__)

class ConfigManager:
    def __init__(self):
        app_paths.ensure_dirs()
        # [Commit CFG-2] Use user home config (Persistent)
        self.config_file = self._get_user_config_path()
        self._ensure_config_exists()
        
        self.config = configparser.ConfigParser()

    def _get_user_config_path(self):
        home = Path.home()
        cfg_dir = home / ".opas200"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        return cfg_dir / "config.ini"

    def _get_embedded_default_config(self):
        if getattr(sys, "_MEIPASS", None):
            p = Path(sys._MEIPASS) / "defaults" / "config.ini"
            if p.exists(): return p
        # dev fallback:
        return Path(__file__).resolve().parent / "config.ini"

    def _ensure_config_exists(self):
        src = self._get_embedded_default_config()

        # 1) 파일이 없으면 기본 복사
        if not self.config_file.exists():
            if src and src.exists():
                try:
                    shutil.copy2(src, self.config_file)
                    logger.info(f"[Config] Initialized user config from {src} to {self.config_file}")
                except Exception as e:
                    logger.error(f"[Config] Failed to copy default config: {e}")
            return

        # 2) 파일이 존재하지만 camera 섹션이 하나도 없으면 재초기화 (Corrupted or empty config fix)
        try:
            tmp_cfg = configparser.ConfigParser()
            tmp_cfg.read(str(self.config_file), encoding="utf-8")

            has_camera = any(s.lower().startswith("camera") for s in tmp_cfg.sections())

            if not has_camera:
                logger.warning("[Config] No camera sections found. Reinitializing from defaults.")
                if src and src.exists():
                    shutil.copy2(src, self.config_file)
        except Exception as e:
            logger.error(f"[Config] Validation error: {e}")

    def load_or_create(self):
        """설정 파일이 없으면 기본값을 생성하고, 있으면 로드합니다."""
        if not self.config_file.exists():
            self._create_default()
        else:
            self.config.read(str(self.config_file), encoding='utf-8')
            # 파일이 존재하더라도 필수 섹션이 누락되었을 수 있으므로 확인 및 생성
            self._create_default()
        return self.config

    def _create_default(self):
        """기본 설정 파일 생성 (필수 섹션만 생성)"""
        if not self.config.has_section('app'):
            self.config.add_section('app')
            
        # App 섹션 기본값 보장
        app_defaults = {'split_mode': 'auto', 'last_camera_index': '0', 'log_retention_days': '30', 'db_retention_days': '30'}
        for key, val in app_defaults.items():
            if not self.config.has_option('app', key):
                self.config.set('app', key, val)

        if not self.config.has_section('window'):
            self.config.add_section('window')
        if not self.config.has_option('window', 'geometry'):
            self.config.set('window', 'geometry', '')

        if not self.config.has_section('event'):
            self.config.add_section('event')
        event_defaults = {
            'enable': 'true',
            'heartbeat': '60',
            'connect_timeout': '5',
            'read_timeout': '65',
            'backoff_min': '1',
            'backoff_max': '30',
            'cooldown_sec': '2',
            'stay_cooldown_sec': '2',
            'stay_hold_ms': '10000',
            'log_load_limit': '200'
        }
        for key, val in event_defaults.items():
            if not self.config.has_option('event', key):
                self.config.set('event', key, val)

        if not self.config.has_section('gpio'):
            self.config.add_section('gpio')
        gpio_defaults = {
            'enable': 'true',
            'pulse_ms': '500',
            'retrigger_policy': 'extend',
            'console_log': 'false'
        }
        for key, val in gpio_defaults.items():
            if not self.config.has_option('gpio', key):
                self.config.set('gpio', key, val)
        
        if not self.config.has_section('monitor'):
            self.config.add_section('monitor')
        monitor_defaults = {
            'idle_stop_enable': 'true',
            'idle_stop_sec': '300'
        }
        for key, val in monitor_defaults.items():
            if not self.config.has_option('monitor', key):
                self.config.set('monitor', key, val)
            
        self._save_to_file()

    def reload(self):
        """설정 파일을 다시 로드합니다."""
        self.config.read(str(self.config_file), encoding='utf-8')
        return self.config

    def get_gpio_config(self):
        """GPIO 설정 반환"""
        return {
            'enable': self.config.getboolean('gpio', 'enable', fallback=True),
            'pulse_ms': self.config.getint('gpio', 'pulse_ms', fallback=500),
            'retrigger_policy': self.config.get('gpio', 'retrigger_policy', fallback='extend'),
            'console_log': self.config.getboolean('gpio', 'console_log', fallback=False)
        }

    def _get_int_safe(self, section, key, default):
        """안전한 정수 파싱: 값이 없거나 오류 시 기본값 반환 및 로그 출력"""
        val = self.config.get(section, key, fallback=str(default)).strip()
        if not val:
            return default
        try:
            return int(val)
        except ValueError:
            logger.debug(f"Invalid {key} for {section} ('{val}'), fallback to {default}")
            return default

    def get_cameras(self):
        """설정에서 카메라 목록을 파싱하여 반환합니다."""
        cameras = []
        # 정규식: camera + 숫자 (대소문자 무시, 개수 제한 없음)
        pattern = re.compile(r"^camera(\d+)$", re.IGNORECASE)
        
        for section in self.config.sections():
            match = pattern.match(section)
            if match:
                # IP가 없거나 비어있어도 키는 포함시킴
                ip = self.config.get(section, 'ip', fallback=None)

                cam = {
                    'key': section,
                    'num': int(match.group(1)),
                    'name': self.config.get(section, 'name', fallback=''),
                    'ip': ip, # ip can be None or empty string
                    'http_port': self._get_int_safe(section, 'http_port', 80),
                    'rtsp_port': self._get_int_safe(section, 'rtsp_port', 554),
                    'username': self.config.get(section, 'username', fallback='admin'),
                    'password': self.config.get(section, 'password', fallback='admin'),
                    'channel': str(self._get_int_safe(section, 'channel', 1)),
                    'main_stream': self.config.get(section, 'main_stream', fallback='true')
                }
                # subtype: main=0, sub=1
                is_main = str(cam['main_stream']).lower() == 'true'
                cam['subtype'] = 0 if is_main else 1
                cameras.append(cam)
        
        # camera 번호 오름차순 정렬
        cameras.sort(key=lambda x: x.get('num', 999))
        return cameras

    def save_window_geometry(self, geometry: QByteArray):
        """윈도우 Geometry(QByteArray)를 Base64 문자열로 변환하여 저장"""
        if not self.config.has_section('window'):
            self.config.add_section('window')
        
        # QByteArray -> Base64 String 변환
        b64_str = geometry.toBase64().data().decode('utf-8')
        self.config.set('window', 'geometry', b64_str)
        self._save_to_file()

    def get_window_geometry(self) -> QByteArray:
        """저장된 Base64 문자열을 QByteArray로 복원하여 반환"""
        if self.config.has_option('window', 'geometry'):
            b64_str = self.config.get('window', 'geometry')
            if b64_str:
                return QByteArray.fromBase64(b64_str.encode('utf-8'))
        return QByteArray()

    def save_app_state(self, last_index, split_mode):
        """앱 상태(마지막 카메라 인덱스, 분할 모드) 저장"""
        if not self.config.has_section('app'):
            self.config.add_section('app')
        self.config.set('app', 'last_camera_index', str(last_index))
        self.config.set('app', 'split_mode', str(split_mode))
        self._save_to_file()

    def _save_to_file(self):
        with open(self.config_file, 'w', encoding='utf-8') as f:
            self.config.write(f)

    def add_camera(self, info):
        """새 카메라 섹션 추가"""
        # 빈 번호 찾기 (camera1, camera2...)
        i = 1
        while True:
            key = f"camera{i}"
            if not self.config.has_section(key):
                break
            i += 1
        
        self.config.add_section(key)
        self.update_camera(key, info)
        return key

    def update_camera(self, key, info):
        """카메라 설정 갱신"""
        if not self.config.has_section(key):
            return
        
        if 'name' in info: self.config.set(key, 'name', info['name'])
        if 'ip' in info: self.config.set(key, 'ip', info['ip'])
        if 'port' in info: self.config.set(key, 'http_port', str(info['port']))
        if 'id' in info: self.config.set(key, 'username', info['id'])
        if 'pw' in info: self.config.set(key, 'password', info['pw'])
        
        self._save_to_file()

    def delete_camera(self, key):
        """카메라 섹션 삭제"""
        if self.config.has_section(key):
            self.config.remove_section(key)
            self._save_to_file()
