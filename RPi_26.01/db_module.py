import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path
import time
import threading
import queue
from log import get_logger
from log_rate_limit import should_log
import app_paths

logger = get_logger(__name__)

# [Commit REC-2] 경로 표준화
app_paths.ensure_dirs()
DATA_DIR = app_paths.get_data_dir()
DB_FILE = DATA_DIR / 'events.db'

def _connect_db():
    """DB 연결 및 PRAGMA 설정 (WAL 모드, 타임아웃)"""
    conn = sqlite3.connect(DB_FILE, timeout=5.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
    except Exception as e:
        logger.warning(f"[DB] PRAGMA apply failed: {e}")
    return conn

def init_db():
    """DB 테이블 초기화 및 마이그레이션"""
    migration_msg = None
    

    with _connect_db() as conn:
        # ts_epoch 컬럼 확인 및 추가
        cursor = conn.execute("PRAGMA table_info(events)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'ts_epoch' not in columns:
            try:
                conn.execute('ALTER TABLE events ADD COLUMN ts_epoch INTEGER')
                # 기존 데이터 Backfill (YYYY-MM-DD HH:MM:SS -> Epoch)
                conn.execute("UPDATE events SET ts_epoch = CAST(strftime('%s', ts) AS INTEGER) WHERE ts_epoch IS NULL")
                conn.commit()
            except Exception as e:
                logger.error(f"[DB] Column add failed: {e}")

        conn.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                camera_key TEXT,
                event_type TEXT,
                area_id TEXT,
                prev_value INTEGER,
                curr_value INTEGER,
                message TEXT,
                payload_json TEXT,
                ts_epoch INTEGER
            )
        ''')
        
        # [Commit 21-fix] 신규 테이블 생성
        conn.execute('''
            CREATE TABLE IF NOT EXISTS people_delta_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                ts_epoch INTEGER,
                camera_key TEXT,
                area_id INTEGER,
                delta INTEGER NOT NULL,
                payload_json TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS event_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                ts_epoch INTEGER,
                camera_key TEXT,
                event_type TEXT,
                area_id INTEGER,
                message TEXT,
                payload_json TEXT
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_people_delta ON people_delta_events (camera_key, area_id, ts_epoch)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_event_logs ON event_logs (camera_key, ts_epoch)')
        
        conn.commit()
    return str(DB_FILE), migration_msg

def get_db_path():
    """DB 파일의 절대 경로 반환"""
    return str(DB_FILE)

# ---------------------------------------------------------
# Async DB Writer
# ---------------------------------------------------------
_db_queue = queue.Queue()
_db_thread = None
_db_running = False

def _db_writer_loop():
    """백그라운드에서 큐의 이벤트를 DB에 기록"""
    while _db_running:
        try:
            # 1초 대기하며 이벤트 가져오기
            job_data = _db_queue.get(timeout=1.0)

            # 작업 유형에 따라 분기
            if isinstance(job_data, dict) and job_data.get('job_type') == 'PURGE':
                retention_days = job_data.get('retention_days')
                callback = job_data.get('callback')
                try:
                    deleted_count = purge_old_events(retention_days)
                    if callback:
                        callback(deleted_count, retention_days, None)
                except Exception as e:
                    logger.error(f"[DB] Purge Job Error: {e}")
                    if callback:
                        callback(0, retention_days, e)
            else:
                insert_event(job_data)

            _db_queue.task_done()
        except queue.Empty:
            continue
        except Exception as e:
            allow, suppressed = should_log("db_writer_loop_error", 60)
            if allow:
                msg = f"[DB] Writer Loop Error: {e}" + (f" (suppressed {suppressed})" if suppressed > 0 else "")
                logger.error(msg)

def init_db_worker():
    """DB 쓰기 워커 시작"""
    global _db_thread, _db_running
    if _db_running:
        return
    _db_running = True
    _db_thread = threading.Thread(target=_db_writer_loop, daemon=True)
    _db_thread.start()

def stop_db_worker(flush=False):
    """DB 쓰기 워커 종료"""
    global _db_running
    
    if flush and _db_running:
        # 큐가 비거나 타임아웃(2초) 될 때까지 대기
        deadline = time.time() + 2.0
        while not _db_queue.empty() and time.time() < deadline:
            time.sleep(0.1)
            
    _db_running = False
    if _db_thread:
        _db_thread.join(timeout=2.0)
        if _db_thread.is_alive():
            logger.warning("[DB] Worker thread did not exit cleanly")

def enqueue_event(event_data):
    """이벤트를 큐에 추가 (비동기 저장)"""
    if _db_running:
        _db_queue.put(event_data)
    else:
        # 워커가 안 돌면 동기 저장 (Fallback)
        insert_event(event_data)

def enqueue_purge(retention_days, callback=None):
    """DB 정리 작업을 큐에 추가 (비동기 실행)"""
    job = {
        'job_type': 'PURGE',
        'retention_days': retention_days,
        'callback': callback
    }
    if _db_running:
        _db_queue.put(job)
    else:
        # 워커가 안 돌면 동기 실행 (Fallback)
        try:
            deleted_count = purge_old_events(retention_days)
            if callback:
                callback(deleted_count, retention_days, None)
        except Exception as e:
            if callback:
                callback(0, retention_days, e)

def insert_event(event_data):
    """이벤트 데이터를 DB에 저장"""
    event_type = event_data.get('type', 'UNKNOWN')
    
    # [Commit 21-fix] DEBUG 로그는 DB에 저장하지 않음
    if event_type == 'DEBUG':
        return

    try:
        # [Commit 24-1] ts/ts_epoch 정규화 (날짜 누락 방지)
        ts_epoch = event_data.get('ts_epoch')
        
        if ts_epoch is None:
            ts_input = event_data.get('ts')
            if ts_input:
                try:
                    dt = datetime.strptime(ts_input, "%Y-%m-%d %H:%M:%S")
                    ts_epoch = int(dt.timestamp())
                except ValueError:
                    ts_epoch = int(time.time())
            else:
                ts_epoch = int(time.time())

        # ts 문자열 표준화 (YYYY-MM-DD HH:MM:SS)
        ts = datetime.fromtimestamp(ts_epoch).strftime("%Y-%m-%d %H:%M:%S")

        # payload_json 내부의 ts/ts_epoch도 표준화된 값으로 통일
        event_data['ts'] = ts
        event_data['ts_epoch'] = ts_epoch

        camera_key = event_data.get('camera_key', '')
        
        area_id_raw = event_data.get('area_id')
        try:
            area_id = int(area_id_raw) if area_id_raw is not None else None
        except:
            area_id = None
            
        message = event_data.get('message', '')
        
        # JSON 직렬화 (실패 시 빈 객체)
        try:
            payload_json = json.dumps(event_data, ensure_ascii=False)
        except:
            payload_json = "{}"

        with _connect_db() as conn:
            # [Commit 21-fix] 이벤트 타입에 따라 테이블 분기
            if event_type == 'PEOPLE_COUNT':
                # [Commit 22-1] 품질 가드: Area ID 필수
                if area_id is None:
                    return

                delta = event_data.get('delta')
                # delta가 없으면 계산 시도 (fallback)
                if delta is None:
                    prev = event_data.get('prev_value')
                    curr = event_data.get('count')
                    if prev is not None and curr is not None:
                        delta = curr - prev
                
                # delta > 0 인 경우에만 people_delta_events에 저장
                if delta is not None and delta > 0:
                    conn.execute('INSERT INTO people_delta_events (ts, ts_epoch, camera_key, area_id, delta, payload_json) VALUES (?, ?, ?, ?, ?, ?)', (ts, ts_epoch, camera_key, area_id, delta, payload_json))
            else:
                # 그 외 모든 이벤트는 event_logs에 저장
                conn.execute('INSERT INTO event_logs (ts, ts_epoch, camera_key, event_type, area_id, message, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)', (ts, ts_epoch, camera_key, event_type, area_id, message, payload_json))
            
            conn.commit()
    except Exception as e:
        allow, suppressed = should_log("db_insert_error", 60)
        if allow:
            msg = f"[DB] Insert Error: {e}" + (f" (suppressed {suppressed})" if suppressed > 0 else "")
            logger.error(msg)

def get_recent_events(limit=200):
    """최근 이벤트 조회 (최신순)"""
    events = []
    try:
        with _connect_db() as conn:
            conn.row_factory = sqlite3.Row
            # [Commit 21-fix] event_logs 테이블에서 조회
            cursor = conn.execute('SELECT * FROM event_logs ORDER BY id DESC LIMIT ?', (limit,))
            rows = cursor.fetchall()
            for row in rows:
                events.append(dict(row))
    except Exception as e:
        logger.error(f"[DB] Select Error: {e}")
    return events

def get_last_lifecycle_event():
    """
    마지막 Application Lifecycle 이벤트(START/STOP)를 조회합니다.
    Returns: dict or None
    """
    try:
        with _connect_db() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT event_type, ts, ts_epoch FROM event_logs WHERE event_type IN ('APP_START', 'APP_STOP') ORDER BY ts_epoch DESC LIMIT 1"
            )
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
    except Exception as e:
        logger.warning(f"[DB] Failed to get last lifecycle event: {e}")
        return None

def purge_old_events(retention_days):
    """오래된 이벤트 삭제 (DB 내부 정리)"""
    deleted_count = 0
    try:
        # 현재 시간 기준 retention_days 이전의 epoch 계산
        cutoff_epoch = int(time.time()) - (retention_days * 24 * 3600)
        with _connect_db() as conn:
            # [Commit 21-fix] 신규 테이블 및 기존 테이블 모두 정리
            c1 = conn.execute("DELETE FROM people_delta_events WHERE ts_epoch < ?", (cutoff_epoch,))
            deleted_count += c1.rowcount
            
            c2 = conn.execute("DELETE FROM event_logs WHERE ts_epoch < ?", (cutoff_epoch,))
            deleted_count += c2.rowcount
            
            c3 = conn.execute("DELETE FROM events WHERE ts_epoch < ?", (cutoff_epoch,))
            deleted_count += c3.rowcount
            
            conn.commit()
    except Exception as e:
        logger.error(f"[DB] Purge Error: {e}")
    return deleted_count

def get_people_count_stats(camera_key, hours=None):
    """특정 카메라의 기간별 인원수 증가량 집계 (Area별)"""
    stats = {}
    try:
        # [Commit 21-fix] people_delta_events 테이블에서 delta 합산으로 변경
        query = "SELECT area_id, SUM(delta) FROM people_delta_events WHERE camera_key = ?"
        params = [camera_key]
        
        if hours is not None:
            now_epoch = int(time.time())
            cutoff_epoch = now_epoch - (hours * 3600)
            query += " AND ts_epoch >= ?"
            params.append(cutoff_epoch)
            
        query += " GROUP BY area_id"
        
        with _connect_db() as conn:
            cursor = conn.execute(query, tuple(params))
            for row in cursor.fetchall():
                # area_id가 문자열일 수 있으므로 변환
                try:
                    aid = int(row[0])
                    val = int(row[1]) if row[1] is not None else 0
                    stats[aid] = val
                except:
                    pass
    except Exception as e:
        logger.error(f"[DB] Stats Error: {e}")
    return stats

def get_people_count_stats_debug(camera_key, hours=None):
    """디버그용: 집계 + 행 수 반환"""
    stats = {}
    rows_scanned = 0
    try:
        # [Commit 21-fix] people_delta_events 테이블에서 delta 합산 및 카운트
        query = "SELECT area_id, SUM(delta), COUNT(*) FROM people_delta_events WHERE camera_key = ?"
        params = [camera_key]
        if hours is not None:
            now_epoch = int(time.time())
            cutoff = now_epoch - (hours * 3600)
            query += " AND ts_epoch >= ?"
            params.append(cutoff)
            
        query += " GROUP BY area_id"
        
        with _connect_db() as conn:
            cursor = conn.execute(query, tuple(params))
            for row in cursor.fetchall():
                try:
                    aid = int(row[0])
                    val = int(row[1]) if row[1] is not None else 0
                    cnt = int(row[2])
                    stats[aid] = val
                    rows_scanned += cnt
                except:
                    pass
    except Exception as e:
        logger.error(f"[DB] Stats Debug Error: {e}")
    return stats, rows_scanned
