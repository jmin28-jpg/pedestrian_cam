import os
import sys
from pathlib import Path

APP_NAME = "OPAS-200"
DATA_ROOT_NAME = "OPAS-200_data"

def get_desktop_dir():
    return Path("/home/admin/Desktop")
#폴더명은 자유, 저장 이름은 항상 고정
def get_app_home():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return get_desktop_dir() / "OPAS-200"

def get_data_root():
    return get_app_home() / DATA_ROOT_NAME

def get_data_dir():
    return get_data_root() / "data"

def get_log_dir():
    return get_data_root() / "logs"

def get_config_path():
    return get_data_root() / "config.ini"

def get_state_path():
    return get_data_root() / "state.json"

def ensure_dirs():
    get_data_dir().mkdir(parents=True, exist_ok=True)
    get_log_dir().mkdir(parents=True, exist_ok=True)
