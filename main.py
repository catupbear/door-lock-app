"""
钥匙柜控制系统 v3.0
协议: 老铁 5字节帧协议
"""
# ─── 热更新加载器（必须最先执行） ──────────────────────────────────────────────
import os as _os, runpy as _runpy, shutil as _shutil
_INTERNAL = _os.path.join(_os.path.expanduser('~'), 'door_lock_main.py')
_USB_PATHS = [
    '/sdcard/door_lock_main.py',
    '/storage/emulated/0/door_lock_main.py',
    '/storage/self/primary/door_lock_main.py',
]
for _src in _USB_PATHS:
    if _os.path.exists(_src):
        _shutil.copy2(_src, _INTERNAL)
        break
if _os.path.exists(_INTERNAL):
    _runpy.run_path(_INTERNAL, run_name='__main__')
    raise SystemExit

# ─── 标准库 ───────────────────────────────────────────────────────────────────
import base64
import hashlib
import hmac
import json
import os
import shutil
import sys
import threading
import time

_BACKUP_SCRIPT = _INTERNAL + '.bak'

# ─── 可选依赖 ─────────────────────────────────────────────────────────────────
try:
    import serial
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False

try:
    import requests as _req
    REQUESTS_AVAILABLE = True
except ImportError:
    _req = None
    REQUESTS_AVAILABLE = False

# ─── Kivy ─────────────────────────────────────────────────────────────────────
os.environ.setdefault('KIVY_NO_ENV_CONFIG', '1')

from kivy.app import App
from kivy.clock import Clock
from kivy.core.text import LabelBase
from kivy.core.window import Window
from kivy.graphics import Color, Rectangle
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.image import Image
from kivy.uix.label import Label
from kivy.uix.screenmanager import NoTransition, Screen, ScreenManager
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput

for _font in [
    'chinese_font.ttf',
    '/System/Library/Fonts/Hiragino Sans GB.ttc',
    '/System/Library/Fonts/STHeiti Medium.ttc',
    '/system/fonts/NotoSansCJK-Regular.ttc',
    '/system/fonts/DroidSansFallback.ttf',
]:
    if os.path.exists(_font):
        try:
            LabelBase.register(name='Roboto', fn_regular=_font)
            break
        except Exception:
            pass

# ─── 配置加密（XOR+Base64，防止明文篡改） ─────────────────────────────────────
_CFG_KEY = b'DoorLockKiosk@2024!'


def _xor(data: bytes) -> bytes:
    k = _CFG_KEY
    return bytes(data[i] ^ k[i % len(k)] for i in range(len(data)))


def _cfg_encode(d: dict) -> bytes:
    return base64.b64encode(_xor(json.dumps(d, ensure_ascii=False).encode()))


def _cfg_decode(raw: bytes) -> dict:
    stripped = raw.lstrip()
    if stripped.startswith(b'{'):
        return json.loads(raw)          # 旧格式明文 JSON，向后兼容
    return json.loads(_xor(base64.b64decode(raw)))


# ─── 配置管理 ─────────────────────────────────────────────────────────────────
_CFG: dict = {}
_CFG_FILE: str = ''
_CFG_LOCK = threading.Lock()


def _cfg_load(data_dir: str):
    global _CFG, _CFG_FILE
    _CFG_FILE = os.path.join(data_dir, 'config.json')
    if os.path.exists(_CFG_FILE):
        try:
            with open(_CFG_FILE, 'rb') as f:
                _CFG = _cfg_decode(f.read())
        except Exception:
            _CFG = {}


def _cfg_save():
    if _CFG_FILE:
        with _CFG_LOCK:
            try:
                tmp = _CFG_FILE + '.tmp'
                with open(tmp, 'wb') as f:
                    f.write(_cfg_encode(_CFG))
                os.replace(tmp, _CFG_FILE)
            except Exception:
                pass


def cfg(key: str, default=None):
    return _CFG.get(key, default)


def cfg_set(key: str, value):
    _CFG[key] = value
    _cfg_save()


# ─── 老铁帧协议 ───────────────────────────────────────────────────────────────
def _laotie_frame(board: int, lock: int) -> bytes:
    frame = bytearray([0x8A, board & 0xFF, lock & 0xFF, 0x11])
    xor = 0
    for b in frame:
        xor ^= b
    frame.append(xor & 0xFF)
    return bytes(frame)


# ─── 串口控制器 ───────────────────────────────────────────────────────────────
class LockController:
    def __init__(self):
        self._ser = None
        self._lock = threading.Lock()
        self.last_error = ''

    def connect(self, port: str, baudrate: int = 9600):
        if not SERIAL_AVAILABLE:
            return False, 'pyserial 未安装'
        try:
            self._ser = serial.Serial(
                port=port, baudrate=baudrate,
                bytesize=8, parity=serial.PARITY_NONE,
                stopbits=1, timeout=0.5,
            )
            return True, '连接成功'
        except Exception as e:
            self._ser = None
            return False, str(e)

    def disconnect(self):
        if self._ser and self._ser.is_open:
            self._ser.close()
        self._ser = None

    @property
    def connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def _send(self, cmd: bytes, read_len: int = 5):
        if not self.connected:
            return None
        with self._lock:
            try:
                self._ser.reset_input_buffer()
                self._ser.write(cmd)
                self._ser.flush()
                time.sleep(0.3)
                resp = self._ser.read(read_len)
                self.last_error = f'发:{cmd.hex()} 收:{resp.hex() if resp else "空"}'
                return resp or None
            except Exception as e:
                self.last_error = str(e)
                return None

    def open_lock(self, addr: int, lock_num: int) -> bool:
        resp = self._send(_laotie_frame(addr, lock_num))
        if not resp or len(resp) < 5:
            return False
        return resp[3] in (0x11, 0x00)

    def query_status(self, addr: int):
        if not self.connected:
            return None
        with self._lock:
            try:
                cmd = _laotie_frame(addr, 0x00)
                self._ser.reset_input_buffer()
                self._ser.write(cmd)
                self._ser.flush()
                time.sleep(0.5)
                raw = self._ser.read(5 * 16)
                self.last_error = f'状态 收:{raw.hex() if raw else "空"}'
                if not raw or len(raw) < 5:
                    return None
                states = {}
                for i in range(0, len(raw) - 4, 5):
                    pkt = raw[i:i + 5]
                    if pkt[0] in (0x8A, 0x80) and pkt[1] == addr:
                        states[pkt[2]] = pkt[3] == 0x11
                return states or None
            except Exception as e:
                self.last_error = str(e)
                return None


# ─── API 客户端 ───────────────────────────────────────────────────────────────
class ApiClient:
    @property
    def _base(self):
        return cfg('api_base', 'http://192.168.1.100').rstrip('/')

    def _get(self, path, **params):
        if not REQUESTS_AVAILABLE:
            return None
        try:
            r = _req.get(f'{self._base}{path}', params=params, timeout=5)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    def _post(self, path, body):
        if not REQUESTS_AVAILABLE:
            return None
        try:
            r = _req.post(f'{self._base}{path}', json=body, timeout=5)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    def init_device(self, mac='', android_id=''):
        return self._post('/api/device/init', {
            'mac_address': mac, 'android_id': android_id,
            'model': 'RK3288', 'os_version': 'Android 10', 'app_version': '3.0.0',
        })

    def heartbeat(self, network_type='wifi'):
        return self._post('/api/device/heartbeat', {
            'device_id': cfg('device_id', ''), 'online': True, 'network_type': network_type,
        })

    def get_config(self):
        return self._get('/api/device/config', device_id=cfg('device_id', ''))

    def get_posters(self):
        return self._get('/api/poster/list', device_id=cfg('device_id', ''))

    def verify_password(self, password):
        return self._post('/api/password/verify', {
            'device_id': cfg('device_id', ''), 'password': password,
        })

    def poll_command(self):
        return self._get('/api/cabinet/cmd', did=cfg('device_id', 'cabinet_001'))

    def ack_command(self, cmd_id, ok, msg=''):
        self._post('/api/cabinet/ack', {'id': cmd_id, 'ok': ok, 'msg': msg})

    def report_open_result(self, lock, ok, action_type):
        self._post('/api/cabinet/open-result', {
            'device_id': cfg('device_id', ''), 'lock': lock, 'ok': ok, 'type': action_type,
        })

    def upload_logs(self, lines: list):
        self._post('/api/device/log', {'device_id': cfg('device_id', ''), 'logs': lines})

    def check_update(self, version: str):
        return self._get('/api/update/check', device_id=cfg('device_id', ''), version=version)


# ─── 本地日志 ─────────────────────────────────────────────────────────────────
class LocalLogger:
    MAX_LINES = 3000
    APP_VERSION = '3.0.0'

    def __init__(self, log_file: str):
        self._file = log_file
        self._lock = threading.Lock()
        self._pending: list = []
        self._write_count = 0

    def _write(self, level: str, msg: str):
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        line = f'[{ts}][{level}] {msg}'
        with self._lock:
            self._pending.append(line)
            self._write_count += 1
            do_trim = self._write_count % 100 == 0
        try:
            with open(self._file, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass
        if do_trim:
            self._trim()

    def info(self, msg: str):  self._write('INFO', msg)
    def warn(self, msg: str):  self._write('WARN', msg)
    def error(self, msg: str): self._write('ERROR', msg)

    def _trim(self):
        try:
            with open(self._file, encoding='utf-8') as f:
                lines = f.readlines()
            if len(lines) > self.MAX_LINES:
                with open(self._file, 'w', encoding='utf-8') as f:
                    f.writelines(lines[-self.MAX_LINES:])
        except Exception:
            pass

    def upload_pending(self, api_client: 'ApiClient'):
        with self._lock:
            if not self._pending:
                return
            batch = list(self._pending)
            self._pending.clear()
        try:
            api_client.upload_logs(batch)
        except Exception:
            with self._lock:
                self._pending = batch + self._pending

    def tail(self, n: int = 80) -> list:
        try:
            with open(self._file, encoding='utf-8') as f:
                lines = f.readlines()
            return [l.rstrip() for l in lines[-n:]]
        except Exception:
            return []


# ─── 断网自动重启管理器 ───────────────────────────────────────────────────────
class NetworkRebootManager:
    def __init__(self, state_file: str, log_list_file: str):
        self._state_file = state_file
        self._log_file = log_list_file
        self._offline_since = None
        self._reboot_count = 0
        self._cooldown_until = 0.0
        self._running = False
        self._load()

    def _load(self):
        try:
            with open(self._state_file, encoding='utf-8') as f:
                d = json.load(f)
            self._reboot_count = d.get('count', 0)
            self._cooldown_until = d.get('cooldown_until', 0.0)
        except Exception:
            pass

    def _save(self, reason: str = ''):
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        entry = {'time': ts, 'reason': reason, 'count': self._reboot_count}
        try:
            with open(self._log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        except Exception:
            pass
        try:
            with open(self._state_file, 'w', encoding='utf-8') as f:
                json.dump({'count': self._reboot_count, 'cooldown_until': self._cooldown_until}, f)
        except Exception:
            pass

    def start(self):
        self._running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._running = False

    def reset_count(self):
        self._reboot_count = 0
        self._cooldown_until = 0.0
        self._save('手动重置计数')

    def get_log(self, n: int = 30) -> list:
        try:
            with open(self._log_file, encoding='utf-8') as f:
                lines = f.readlines()
            return [json.loads(l) for l in lines[-n:] if l.strip()]
        except Exception:
            return []

    def status(self) -> dict:
        return {
            'count': self._reboot_count,
            'offline_since': self._offline_since,
            'cooldown_until': self._cooldown_until,
            'in_cooldown': time.time() < self._cooldown_until,
        }

    def _loop(self):
        while self._running:
            interval = cfg('network_check_interval', 60)
            time.sleep(interval)
            if not cfg('offline_reboot_enabled', True):
                self._offline_since = None
                continue
            online = _check_network()
            if online:
                self._offline_since = None
                continue
            if self._offline_since is None:
                self._offline_since = time.time()
                logger.warn('检测到断网，开始计时')
            offline_secs = time.time() - self._offline_since
            delay_secs = cfg('offline_reboot_delay', 10) * 60
            if offline_secs < delay_secs:
                continue
            now = time.time()
            if now < self._cooldown_until:
                continue
            max_count = cfg('max_reboot_count', 5)
            if self._reboot_count >= max_count:
                cooldown_secs = cfg('reboot_cooldown', 60) * 60
                self._cooldown_until = now + cooldown_secs
                self._reboot_count = 0
                self._save(f'达到重启上限{max_count}次，进入冷却')
                logger.warn(f'重启次数达上限，冷却{cfg("reboot_cooldown",60)}分钟')
                continue
            self._reboot_count += 1
            self._offline_since = None
            self._save(f'断网{int(offline_secs // 60)}分钟触发第{self._reboot_count}次重启')
            logger.warn(f'断网重启，第{self._reboot_count}次')
            Clock.schedule_once(lambda _: App.get_running_app().restart_app(), 0)


# ─── 远程配置下发 ─────────────────────────────────────────────────────────────
class RemoteConfigManager:
    def __init__(self):
        self._running = False

    def start(self):
        self._running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._running = False

    def _loop(self):
        time.sleep(10)
        while self._running:
            self._fetch()
            time.sleep(300)

    def _fetch(self):
        resp = api.get_config()
        if not resp or resp.get('code') != 0:
            return
        data = resp.get('data', {})
        poster = data.get('poster', {})
        if 'interval' in poster:
            cfg_set('poster_interval', poster['interval'])
        pwd_cfg = data.get('password', {})
        for k in ('max_error_count', 'lock_duration', 'offline_enabled'):
            if k in pwd_cfg:
                cfg_set(k, pwd_cfg[k])
        nr = data.get('network_reboot', {})
        key_map = {
            'enabled': 'offline_reboot_enabled',
            'check_interval': 'network_check_interval',
            'reboot_delay': 'offline_reboot_delay',
            'max_reboot_count': 'max_reboot_count',
            'reboot_cooldown': 'reboot_cooldown',
        }
        for src, dst in key_map.items():
            if src in nr:
                cfg_set(dst, nr[src])
        for k in ('idle_timeout', 'result_page_duration'):
            if k in data:
                cfg_set(k, data[k])
        logger.info('远程配置已同步')

    def check_script_update(self):
        """检查并下载新版Python脚本，重启后生效"""
        resp = api.check_update(LocalLogger.APP_VERSION)
        if not resp or resp.get('code') != 0:
            return
        d = resp.get('data', {})
        if not d.get('has_update'):
            return
        url = d.get('url', '')
        md5 = d.get('md5', '')
        version = d.get('version', '')
        if not url or not REQUESTS_AVAILABLE:
            return
        try:
            r = _req.get(url, timeout=30)
            if r.status_code != 200:
                return
            content = r.content
            if md5 and hashlib.md5(content).hexdigest() != md5:
                logger.error(f'远程包MD5校验失败 v{version}')
                return
            if os.path.exists(_INTERNAL):
                try:
                    shutil.copy2(_INTERNAL, _BACKUP_SCRIPT)
                except Exception:
                    pass
            with open(_INTERNAL, 'wb') as f:
                f.write(content)
            logger.info(f'远程包已下载 v{version}，重启后生效')
            Clock.schedule_once(lambda _: App.get_running_app().restart_app(), 2)
        except Exception as e:
            logger.error(f'远程包下载失败: {e}')


# ─── 离线密码引擎 ─────────────────────────────────────────────────────────────
def verify_offline_password(password: str, lock_no: int, window_size: int = 1800) -> bool:
    secret = cfg('device_secret', '')
    device_id = cfg('device_id', '')
    if not secret or not device_id:
        return False
    now = int(time.time())
    for offset in (-1, 0, 1):
        window = (now + offset * window_size) // window_size
        msg = f'{device_id}{lock_no}{window}'.encode()
        digest = hmac.new(secret.encode(), msg, hashlib.sha256).digest().hex()
        expected = ''.join(c for c in digest if c.isdigit())[-6:]
        if password == expected:
            return True
    return False


# ─── 海报管理器（支持定时投放） ───────────────────────────────────────────────
class PosterManager:
    def __init__(self, cache_dir: str, api_client: ApiClient):
        self._dir = cache_dir
        self._api = api_client
        self._items: list = []   # [{'path': str, 'start': 'HH:MM', 'end': 'HH:MM'}]
        self._rlock = threading.Lock()
        os.makedirs(cache_dir, exist_ok=True)
        self._load_cached()

    @property
    def posters(self) -> list:
        with self._rlock:
            now = time.strftime('%H:%M')
            active = [i['path'] for i in self._items
                      if i.get('start', '00:00') <= now <= i.get('end', '23:59')]
            return active if active else [i['path'] for i in self._items]

    def refresh(self):
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self):
        resp = self._api.get_posters()
        if not resp or resp.get('code') != 0:
            return
        data = resp.get('data', {})
        cfg_set('poster_interval', data.get('interval', 5))
        server_items = data.get('list', [])
        known_ids = {item.get('id') for item in server_items}
        new_items = []
        for item in server_items:
            pid  = item.get('id', '')
            url  = item.get('url', '')
            md5  = item.get('md5', '')
            sched = item.get('schedule', {})
            if not pid or not url or not REQUESTS_AVAILABLE:
                continue
            local = os.path.join(self._dir, f'{pid}.jpg')
            if not (os.path.exists(local) and self._md5(local) == md5):
                try:
                    r = _req.get(url, timeout=15)
                    if r.status_code == 200:
                        with open(local, 'wb') as f:
                            f.write(r.content)
                    else:
                        continue
                except Exception:
                    if not os.path.exists(local):
                        continue
            new_items.append({
                'path': local,
                'start': sched.get('start', '00:00'),
                'end':   sched.get('end', '23:59'),
            })
        for fname in os.listdir(self._dir):
            fid = fname.rsplit('.', 1)[0]
            if fid not in known_ids:
                try:
                    os.remove(os.path.join(self._dir, fname))
                except Exception:
                    pass
        if new_items:
            with self._rlock:
                self._items = new_items
            logger.info(f'海报已更新，共{len(new_items)}张')

    def _load_cached(self):
        files = sorted(
            os.path.join(self._dir, f)
            for f in os.listdir(self._dir)
            if f.lower().endswith(('.jpg', '.png'))
        )
        with self._rlock:
            self._items = [{'path': f, 'start': '00:00', 'end': '23:59'} for f in files]

    @staticmethod
    def _md5(path: str) -> str:
        h = hashlib.md5()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()


# ─── 全局单例 ─────────────────────────────────────────────────────────────────
ctrl        = LockController()
api         = ApiClient()
logger:     LocalLogger          = None  # type: ignore
reboot_mgr: NetworkRebootManager = None  # type: ignore
cfg_mgr:    RemoteConfigManager  = None  # type: ignore
poster_mgr: PosterManager        = None  # type: ignore


def _check_network() -> bool:
    if not REQUESTS_AVAILABLE:
        return False
    try:
        _req.get(cfg('api_base', 'http://192.168.1.100'), timeout=2)
        return True
    except Exception:
        return False


def _dark_bg(widget, r=0.08, g=0.08, b=0.10):
    with widget.canvas.before:
        Color(r, g, b, 1)
        rect = Rectangle(size=widget.size, pos=widget.pos)
    widget.bind(
        size=lambda *_: setattr(rect, 'size', widget.size),
        pos=lambda *_: setattr(rect, 'pos', widget.pos),
    )


# ─── 设备信息与 Android 系统工具 ─────────────────────────────────────────────

def _get_device_mac() -> str:
    for iface in ['eth0', 'wlan0', 'eth1']:
        try:
            with open(f'/sys/class/net/{iface}/address') as f:
                mac = f.read().strip()
            if mac and mac not in ('', '00:00:00:00:00:00'):
                return mac
        except Exception:
            pass
    return ''


def _get_android_id() -> str:
    try:
        from jnius import autoclass
        Settings = autoclass('android.provider.Settings$Secure')
        activity = autoclass('org.kivy.android.PythonActivity').mActivity
        return Settings.getString(activity.getContentResolver(), 'android_id') or ''
    except Exception:
        return ''


def _get_ip() -> str:
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ''


def _get_firmware() -> str:
    try:
        from jnius import autoclass
        Build = autoclass('android.os.Build')
        return f'{Build.MANUFACTURER} {Build.MODEL} / Android {Build.VERSION.RELEASE}'
    except Exception:
        import platform
        return platform.platform()[:60]


def _set_immersive(enable: bool):
    """切换沉浸式全屏（隐藏状态栏+导航栏）。Android 11 以下用 FLAG 方式，12+ 仍兼容。"""
    try:
        from jnius import autoclass
        View = autoclass('android.view.View')
        activity = autoclass('org.kivy.android.PythonActivity').mActivity
        dv = activity.getWindow().getDecorView()
        if enable:
            flags = (View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY |
                     View.SYSTEM_UI_FLAG_LAYOUT_STABLE |
                     View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION |
                     View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN |
                     View.SYSTEM_UI_FLAG_HIDE_NAVIGATION |
                     View.SYSTEM_UI_FLAG_FULLSCREEN)
        else:
            flags = View.SYSTEM_UI_FLAG_VISIBLE
        dv.setSystemUiVisibility(flags)
    except Exception:
        pass


def _system_reboot():
    """重启整个 Android 设备（需 REBOOT 系统权限或 root）。"""
    try:
        from jnius import autoclass
        activity = autoclass('org.kivy.android.PythonActivity').mActivity
        pm = activity.getSystemService('power')
        pm.reboot(None)
    except Exception:
        try:
            os.system('reboot')
        except Exception:
            pass


def _open_wifi_settings():
    try:
        from jnius import autoclass
        Intent = autoclass('android.content.Intent')
        Settings = autoclass('android.provider.Settings')
        activity = autoclass('org.kivy.android.PythonActivity').mActivity
        activity.startActivity(Intent(Settings.ACTION_WIFI_SETTINGS))
    except Exception:
        pass


def _open_ethernet_settings():
    try:
        from jnius import autoclass
        Intent = autoclass('android.content.Intent')
        Settings = autoclass('android.provider.Settings')
        activity = autoclass('org.kivy.android.PythonActivity').mActivity
        try:
            activity.startActivity(Intent('android.settings.ETHERNET_SETTINGS'))
        except Exception:
            activity.startActivity(Intent(Settings.ACTION_WIRELESS_SETTINGS))
    except Exception:
        pass


def rollback_script() -> tuple:
    """将远程包回滚到上一个备份版本。"""
    if not os.path.exists(_BACKUP_SCRIPT):
        return False, '无备份文件'
    try:
        shutil.copy2(_BACKUP_SCRIPT, _INTERNAL)
        try:
            logger.info('脚本已回滚，重启后生效')
        except Exception:
            pass
        return True, '回滚成功，重启后生效'
    except Exception as e:
        return False, f'回滚失败: {e}'


# ─── 初始化等待页 ─────────────────────────────────────────────────────────────
class InitWaitScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        root = FloatLayout()
        _dark_bg(root, 0.08, 0.08, 0.10)
        self.lbl_title = Label(
            text='⏳ 设备初始化中', font_size=dp(28), bold=True,
            pos_hint={'center_x': 0.5, 'center_y': 0.62},
            size_hint=(None, None), size=(dp(500), dp(55)), halign='center',
        )
        self.lbl_hint = Label(
            text='请确保设备已连接网络', font_size=dp(18),
            pos_hint={'center_x': 0.5, 'center_y': 0.52},
            size_hint=(None, None), size=(dp(500), dp(40)), halign='center',
            color=(0.75, 0.75, 0.75, 1),
        )
        self.lbl_net = Label(
            text='网络状态：检测中...', font_size=dp(16),
            pos_hint={'center_x': 0.5, 'center_y': 0.43},
            size_hint=(None, None), size=(dp(500), dp(35)), halign='center',
        )
        self.lbl_cd = Label(
            text='', font_size=dp(15),
            pos_hint={'center_x': 0.5, 'center_y': 0.34},
            size_hint=(None, None), size=(dp(400), dp(30)), halign='center',
            color=(0.55, 0.55, 0.55, 1),
        )
        for w in (self.lbl_title, self.lbl_hint, self.lbl_net, self.lbl_cd):
            root.add_widget(w)
        self.add_widget(root)

    def on_enter(self):
        self._countdown = 30
        self._ticker = Clock.schedule_interval(self._tick, 1)
        self._try_init()

    def on_leave(self):
        if hasattr(self, '_ticker'):
            self._ticker.cancel()

    def _tick(self, dt):
        self._countdown -= 1
        self.lbl_cd.text = f'重试倒计时：{self._countdown}秒'
        if self._countdown <= 0:
            self._countdown = 30
            self._try_init()

    def _try_init(self):
        threading.Thread(target=self._do_init, daemon=True).start()

    def _do_init(self):
        mac = _get_device_mac()
        android_id = _get_android_id()
        resp = api.init_device(mac, android_id)
        if resp and resp.get('code') == 0:
            d = resp['data']
            cfg_set('device_id', d['device_id'])
            cfg_set('device_secret', d.get('device_secret', ''))
            logger.info(f'设备初始化成功: {d["device_id"]}')
            Clock.schedule_once(lambda _: App.get_running_app().go_poster())
            return
        online = _check_network()
        def _upd(_):
            if online:
                self.lbl_net.text = '网络状态：✅ 已连接（服务器无响应）'
                self.lbl_net.color = (0.9, 0.75, 0.2, 1)
            else:
                self.lbl_net.text = '网络状态：❌ 未连接'
                self.lbl_net.color = (0.9, 0.35, 0.35, 1)
        Clock.schedule_once(_upd)


# ─── 海报轮播页 ───────────────────────────────────────────────────────────────
class PosterScreen(Screen):
    _ADMIN_HOLD = 5

    def __init__(self, **kw):
        super().__init__(**kw)
        self._idx = 0
        self._touch_x = 0
        self._admin_ev = None

        root = FloatLayout()
        _dark_bg(root, 0.05, 0.05, 0.07)

        self.img = Image(
            source='', allow_stretch=True, keep_ratio=True,
            size_hint=(1, 1), pos_hint={'x': 0, 'y': 0},
        )
        root.add_widget(self.img)

        self.lbl_empty = Label(
            text='点击屏幕进入密码输入', font_size=dp(22),
            pos_hint={'center_x': 0.5, 'center_y': 0.5},
            size_hint=(None, None), size=(dp(500), dp(50)), halign='center',
            color=(0.55, 0.55, 0.55, 1),
        )
        root.add_widget(self.lbl_empty)

        self.lbl_did = Label(
            text='', font_size=dp(13),
            pos_hint={'right': 0.99, 'y': 0.01},
            size_hint=(None, None), size=(dp(200), dp(28)),
            halign='right', color=(0.6, 0.6, 0.6, 1),
        )
        root.add_widget(self.lbl_did)

        self.lbl_net = Label(
            text='●', font_size=dp(22),
            pos_hint={'right': 0.99, 'top': 0.99},
            size_hint=(None, None), size=(dp(50), dp(40)),
            color=(0.5, 0.5, 0.5, 1),
        )
        root.add_widget(self.lbl_net)

        self.lbl_dots = Label(
            text='', font_size=dp(14),
            pos_hint={'center_x': 0.5, 'y': 0.01},
            size_hint=(None, None), size=(dp(300), dp(28)),
            halign='center', color=(0.8, 0.8, 0.8, 1),
        )
        root.add_widget(self.lbl_dots)
        self.add_widget(root)

    def on_enter(self):
        _set_immersive(True)
        self.lbl_did.text = cfg('device_id', '--')
        self._reload()
        self._auto_ev = Clock.schedule_interval(self._advance, cfg('poster_interval', 5))
        self._net_ev = Clock.schedule_interval(self._net_check, 15)
        self._net_check(0)
        poster_mgr.refresh()

    def on_leave(self):
        for ev in ('_auto_ev', '_net_ev'):
            if hasattr(self, ev):
                getattr(self, ev).cancel()

    def _reload(self):
        p = poster_mgr.posters
        self.lbl_empty.opacity = 0 if p else 1
        if p:
            self._idx = self._idx % len(p)
            self.img.source = p[self._idx]
        self._dots()

    def _dots(self):
        p = poster_mgr.posters
        n = len(p)
        self.lbl_dots.text = '' if n <= 1 else ''.join(
            '●' if i == self._idx % n else '○' for i in range(n)
        )

    def _advance(self, dt):
        p = poster_mgr.posters
        if not p:
            return
        self._idx = (self._idx + 1) % len(p)
        self.img.source = p[self._idx]
        self._dots()

    def _net_check(self, dt):
        threading.Thread(target=self._do_net, daemon=True).start()

    def _do_net(self):
        online = _check_network()
        Clock.schedule_once(lambda _: setattr(
            self.lbl_net, 'color',
            (0.2, 0.9, 0.4, 1) if online else (0.9, 0.3, 0.3, 1)
        ))

    def on_touch_down(self, touch):
        self._touch_x = touch.x
        w, h = Window.size
        if touch.x > w * 0.85 and touch.y < h * 0.15:
            self._admin_ev = Clock.schedule_once(self._go_admin, self._ADMIN_HOLD)
        return super().on_touch_down(touch)

    def on_touch_up(self, touch):
        if self._admin_ev:
            self._admin_ev.cancel()
            self._admin_ev = None
        dx = touch.x - self._touch_x
        p = poster_mgr.posters
        if p and abs(dx) > dp(80):
            self._idx = (self._idx + (1 if dx < 0 else -1)) % len(p)
            self.img.source = p[self._idx]
            self._dots()
            if hasattr(self, '_auto_ev'):
                self._auto_ev.cancel()
                self._auto_ev = Clock.schedule_interval(self._advance, cfg('poster_interval', 5))
        elif abs(dx) < dp(20):
            App.get_running_app().go_password()
        return super().on_touch_up(touch)

    def _go_admin(self, dt):
        App.get_running_app().sm.current = 'admin_auth'


# ─── 密码输入页 ───────────────────────────────────────────────────────────────
class PasswordScreen(Screen):
    _MAX = 8

    def __init__(self, **kw):
        super().__init__(**kw)
        self._pwd = ''
        self._errors = 0
        self._locked_until = 0.0

        root = FloatLayout()
        _dark_bg(root, 0.10, 0.10, 0.13)

        btn_back = Button(
            text='← 返回', font_size=dp(16),
            size_hint=(None, None), size=(dp(110), dp(42)),
            pos_hint={'x': 0.02, 'top': 0.97},
            background_color=(0.28, 0.28, 0.33, 1), background_normal='',
        )
        btn_back.bind(on_press=lambda _: App.get_running_app().go_poster())
        root.add_widget(btn_back)

        root.add_widget(Label(
            text='请输入开柜密码', font_size=dp(24), bold=True,
            pos_hint={'center_x': 0.5, 'top': 0.88},
            size_hint=(None, None), size=(dp(500), dp(50)), halign='center',
        ))

        self.lbl_pwd = Label(
            text='_ _ _ _ _ _', font_size=dp(38), bold=True,
            pos_hint={'center_x': 0.5, 'top': 0.74},
            size_hint=(None, None), size=(dp(500), dp(58)), halign='center',
            color=(0.95, 0.95, 0.95, 1),
        )
        root.add_widget(self.lbl_pwd)

        self.lbl_err = Label(
            text='', font_size=dp(15),
            pos_hint={'center_x': 0.5, 'top': 0.63},
            size_hint=(None, None), size=(dp(500), dp(35)), halign='center',
            color=(0.95, 0.33, 0.33, 1),
        )
        root.add_widget(self.lbl_err)

        pad = GridLayout(
            cols=3, spacing=dp(10),
            size_hint=(None, None), size=(dp(320), dp(270)),
            pos_hint={'center_x': 0.5, 'y': 0.03},
        )
        for key in ['1', '2', '3', '4', '5', '6', '7', '8', '9', '⌫', '0', '✓']:
            c = (
                (0.80, 0.20, 0.20, 1) if key == '⌫' else
                (0.18, 0.62, 0.28, 1) if key == '✓' else
                (0.22, 0.22, 0.28, 1)
            )
            b = Button(text=key, font_size=dp(26), background_normal='', background_color=c)
            b.bind(on_press=lambda btn, k=key: self._key(k))
            pad.add_widget(b)
        root.add_widget(pad)

        self.lbl_to = Label(
            text='', font_size=dp(13),
            pos_hint={'right': 0.99, 'y': 0.01},
            size_hint=(None, None), size=(dp(180), dp(28)),
            halign='right', color=(0.5, 0.5, 0.5, 1),
        )
        root.add_widget(self.lbl_to)
        self.add_widget(root)

    def on_enter(self):
        self._pwd = ''
        self._update_disp()
        self.lbl_err.text = ''
        self._remaining = cfg('idle_timeout', 60)
        self._idle_ev = Clock.schedule_interval(self._idle_tick, 1)

    def on_leave(self):
        if hasattr(self, '_idle_ev') and self._idle_ev:
            self._idle_ev.cancel()
            self._idle_ev = None

    def _idle_tick(self, dt):
        self._remaining -= 1
        self.lbl_to.text = f'{self._remaining}秒后自动返回'
        if self._remaining <= 0:
            App.get_running_app().go_poster()

    def _key(self, k: str):
        self._remaining = cfg('idle_timeout', 60)
        if k == '⌫':
            self._pwd = self._pwd[:-1]
            self._update_disp()
        elif k == '✓':
            self._submit()
        elif len(self._pwd) < self._MAX:
            self._pwd += k
            self._update_disp()

    def _update_disp(self):
        n = len(self._pwd)
        self.lbl_pwd.text = ('●' * n + '_ ' * max(0, 6 - n)).strip()

    def _submit(self):
        if not self._pwd:
            return
        if time.time() < self._locked_until:
            secs = int(self._locked_until - time.time())
            self.lbl_err.text = f'已锁定，请{secs}秒后重试'
            return
        if len(self._pwd) < 6:
            self.lbl_err.text = '密码至少6位'
            return
        pwd = self._pwd
        self._pwd = ''
        self._update_disp()
        threading.Thread(target=self._verify, args=(pwd,), daemon=True).start()

    def _verify(self, pwd: str):
        resp = api.verify_password(pwd)
        if resp and resp.get('code') == 0:
            d = resp['data']
            self._errors = 0
            logger.info(f'在线验证成功，锁{d["lock"]}')
            Clock.schedule_once(lambda _: self._do_open(d['lock'], d.get('type', 1)))
            return
        for lock_no in range(1, 17):
            if verify_offline_password(pwd, lock_no):
                self._errors = 0
                logger.info(f'离线验证成功，锁{lock_no}')
                Clock.schedule_once(lambda _, ln=lock_no: self._do_open(ln, 1))
                return
        self._errors += 1
        max_e = cfg('max_error_count', 5)
        logger.warn(f'密码错误，已失败{self._errors}次')
        if self._errors >= max_e:
            lock_d = cfg('lock_duration', 180)
            self._locked_until = time.time() + lock_d
            Clock.schedule_once(lambda _: setattr(
                self.lbl_err, 'text', f'错误过多，锁定{lock_d // 60}分钟'
            ))
        else:
            left = max_e - self._errors
            Clock.schedule_once(lambda _: setattr(
                self.lbl_err, 'text', f'密码错误，还可尝试{left}次'
            ))

    def _do_open(self, lock: int, action_type: int):
        if not ctrl.connected:
            App.get_running_app().go_result(lock=lock, ok=False, msg='柜门故障', atype=action_type)
            return
        threading.Thread(target=self._exec_open, args=(lock, action_type), daemon=True).start()

    def _exec_open(self, lock: int, action_type: int):
        ok = ctrl.open_lock(cfg('board_addr', 1), lock)
        api.report_open_result(lock, ok, action_type)
        logger.info(f'开锁 锁{lock} {"成功" if ok else "失败"}')
        msg = '' if ok else '柜门故障'
        Clock.schedule_once(lambda _: App.get_running_app().go_result(
            lock=lock, ok=ok, msg=msg, atype=action_type
        ))


# ─── 开柜结果页 ───────────────────────────────────────────────────────────────
class ResultScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self._p = {}
        root = FloatLayout()
        _dark_bg(root, 0.08, 0.10, 0.08)
        self.lbl_icon = Label(
            text='', font_size=dp(72),
            pos_hint={'center_x': 0.5, 'center_y': 0.68},
            size_hint=(None, None), size=(dp(200), dp(110)), halign='center',
        )
        self.lbl_main = Label(
            text='', font_size=dp(32), bold=True,
            pos_hint={'center_x': 0.5, 'center_y': 0.52},
            size_hint=(None, None), size=(dp(560), dp(58)), halign='center',
        )
        self.lbl_sub = Label(
            text='', font_size=dp(20),
            pos_hint={'center_x': 0.5, 'center_y': 0.40},
            size_hint=(None, None), size=(dp(560), dp(42)), halign='center',
            color=(0.75, 0.75, 0.75, 1),
        )
        self.lbl_cd = Label(
            text='', font_size=dp(15),
            pos_hint={'center_x': 0.5, 'y': 0.04},
            size_hint=(None, None), size=(dp(300), dp(30)), halign='center',
            color=(0.5, 0.5, 0.5, 1),
        )
        for w in (self.lbl_icon, self.lbl_main, self.lbl_sub, self.lbl_cd):
            root.add_widget(w)
        self.add_widget(root)

    def set_params(self, lock, ok, msg, atype):
        self._p = {'lock': lock, 'ok': ok, 'msg': msg, 'atype': atype}

    def on_enter(self):
        lock = self._p.get('lock', 0)
        ok   = self._p.get('ok', False)
        msg  = self._p.get('msg', '')
        atype = self._p.get('atype', 1)
        if ok:
            self.lbl_icon.text  = '✅'
            self.lbl_main.text  = '开柜成功'
            self.lbl_main.color = (0.2, 0.9, 0.4, 1)
            self.lbl_sub.text   = f'{lock:02d}号柜门已开  {"请取走钥匙" if atype == 1 else "请存放钥匙"}'
        elif msg == '柜门故障':
            self.lbl_icon.text  = '⚠️'
            self.lbl_main.text  = '柜门故障'
            self.lbl_main.color = (0.9, 0.6, 0.2, 1)
            self.lbl_sub.text   = '请联系管理员'
        elif msg == '无可用柜门':
            self.lbl_icon.text  = '🔒'
            self.lbl_main.text  = '暂无可用柜门'
            self.lbl_main.color = (0.9, 0.60, 0.20, 1)
            self.lbl_sub.text   = '请稍后再试或联系管理员'
        else:
            self.lbl_icon.text  = '❌'
            self.lbl_main.text  = '开柜失败'
            self.lbl_main.color = (0.9, 0.3, 0.3, 1)
            self.lbl_sub.text   = msg or '密码错误，请重试'
        self._remaining = cfg('result_page_duration', 10)
        self._ev = Clock.schedule_interval(self._tick, 1)

    def on_leave(self):
        if hasattr(self, '_ev'):
            self._ev.cancel()

    def _tick(self, dt):
        self._remaining -= 1
        self.lbl_cd.text = f'{self._remaining}秒后自动返回'
        if self._remaining <= 0:
            App.get_running_app().go_poster()

    def on_touch_up(self, touch):
        App.get_running_app().go_poster()
        return super().on_touch_up(touch)


# ─── 管理员验证页 ─────────────────────────────────────────────────────────────
class AdminAuthScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self._pwd = ''
        root = FloatLayout()
        _dark_bg(root, 0.08, 0.08, 0.10)
        btn_back = Button(
            text='← 取消', font_size=dp(15),
            size_hint=(None, None), size=(dp(100), dp(40)),
            pos_hint={'x': 0.02, 'top': 0.97},
            background_color=(0.28, 0.28, 0.33, 1), background_normal='',
        )
        btn_back.bind(on_press=lambda _: App.get_running_app().go_poster())
        root.add_widget(btn_back)
        root.add_widget(Label(
            text='管理员验证', font_size=dp(22), bold=True,
            pos_hint={'center_x': 0.5, 'top': 0.88},
            size_hint=(None, None), size=(dp(400), dp(50)), halign='center',
        ))
        self.lbl_pwd = Label(
            text='_ _ _ _ _ _', font_size=dp(30), bold=True,
            pos_hint={'center_x': 0.5, 'top': 0.75},
            size_hint=(None, None), size=(dp(400), dp(55)), halign='center',
        )
        root.add_widget(self.lbl_pwd)
        self.lbl_err = Label(
            text='', font_size=dp(15),
            pos_hint={'center_x': 0.5, 'top': 0.64},
            size_hint=(None, None), size=(dp(400), dp(32)), halign='center',
            color=(0.9, 0.3, 0.3, 1),
        )
        root.add_widget(self.lbl_err)
        pad = GridLayout(
            cols=3, spacing=dp(8),
            size_hint=(None, None), size=(dp(290), dp(240)),
            pos_hint={'center_x': 0.5, 'y': 0.04},
        )
        for key in ['1', '2', '3', '4', '5', '6', '7', '8', '9', '⌫', '0', '✓']:
            c = (
                (0.75, 0.20, 0.20, 1) if key == '⌫' else
                (0.18, 0.52, 0.22, 1) if key == '✓' else
                (0.22, 0.22, 0.28, 1)
            )
            b = Button(text=key, font_size=dp(22), background_normal='', background_color=c)
            b.bind(on_press=lambda btn, k=key: self._key(k))
            pad.add_widget(b)
        root.add_widget(pad)
        self.add_widget(root)

    def on_enter(self):
        self._pwd = ''
        self.lbl_err.text = ''
        self._disp()

    def _disp(self):
        n = len(self._pwd)
        self.lbl_pwd.text = ('●' * n + '_ ' * (6 - n)).strip()

    def _key(self, k: str):
        if k == '⌫':
            self._pwd = self._pwd[:-1]
        elif k == '✓':
            if self._pwd == cfg('admin_password', '888888'):
                App.get_running_app().sm.current = 'admin'
            else:
                self.lbl_err.text = '密码错误'
                self._pwd = ''
        elif len(self._pwd) < 6:
            self._pwd += k
        self._disp()


# ─── 管理员主界面 ─────────────────────────────────────────────────────────────
class AdminScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self._immersive = False
        root = BoxLayout(orientation='vertical', padding=dp(10), spacing=dp(5))
        _dark_bg(root, 0.10, 0.10, 0.13)

        # ── 顶部工具栏 ────────────────────────────────────────────────────────
        top = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        btn_back = Button(
            text='← 返回', font_size=dp(14), size_hint_x=0.12,
            background_color=(0.28, 0.28, 0.33, 1), background_normal='',
        )
        btn_back.bind(on_press=lambda _: App.get_running_app().go_poster())
        top.add_widget(btn_back)
        top.add_widget(Label(text='内部管理', font_size=dp(18), bold=True))
        btn_reboot_log = Button(
            text='重启日志', font_size=dp(13), size_hint_x=0.16,
            background_color=(0.35, 0.25, 0.45, 1), background_normal='',
        )
        btn_reboot_log.bind(on_press=lambda _: setattr(
            App.get_running_app().sm, 'current', 'admin_reboot'
        ))
        top.add_widget(btn_reboot_log)
        btn_log = Button(
            text='操作日志', font_size=dp(13), size_hint_x=0.16,
            background_color=(0.25, 0.35, 0.45, 1), background_normal='',
        )
        btn_log.bind(on_press=lambda _: setattr(
            App.get_running_app().sm, 'current', 'admin_log'
        ))
        top.add_widget(btn_log)
        root.add_widget(top)

        # ── 设备信息（异步填充） ─────────────────────────────────────────────
        self.lbl_dev1 = Label(
            text='MAC: --  IP: --  设备ID: --',
            font_size=dp(12), halign='left', valign='middle',
            size_hint_y=None, height=dp(22),
            color=(0.65, 0.85, 0.65, 1),
        )
        self.lbl_dev1.bind(size=self.lbl_dev1.setter('text_size'))
        root.add_widget(self.lbl_dev1)
        self.lbl_dev2 = Label(
            text='Android ID: --  固件: --',
            font_size=dp(12), halign='left', valign='middle',
            size_hint_y=None, height=dp(20),
            color=(0.55, 0.75, 0.55, 1),
        )
        self.lbl_dev2.bind(size=self.lbl_dev2.setter('text_size'))
        root.add_widget(self.lbl_dev2)

        # ── 串口配置 ──────────────────────────────────────────────────────────
        s = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(6))
        s.add_widget(Label(text='端口:', size_hint_x=0.09, font_size=dp(13)))
        self.inp_port = TextInput(
            text=cfg('port', '/dev/ttyS1'), multiline=False,
            font_size=dp(13), size_hint_x=0.24,
        )
        s.add_widget(self.inp_port)
        s.add_widget(Label(text='波特率:', size_hint_x=0.10, font_size=dp(13)))
        self.spn_baud = Spinner(
            text=str(cfg('baudrate', 9600)),
            values=['4800', '9600', '19200', '38400', '115200'],
            font_size=dp(13), size_hint_x=0.14,
        )
        s.add_widget(self.spn_baud)
        s.add_widget(Label(text='板号:', size_hint_x=0.07, font_size=dp(13)))
        self.inp_addr = TextInput(
            text=str(cfg('board_addr', 1)), multiline=False,
            input_filter='int', font_size=dp(13), size_hint_x=0.07,
        )
        s.add_widget(self.inp_addr)
        self.btn_conn = Button(
            text='连接', font_size=dp(13), size_hint_x=0.14,
            background_color=(0.2, 0.55, 0.95, 1), background_normal='',
        )
        self.btn_conn.bind(on_press=self._toggle_conn)
        s.add_widget(self.btn_conn)
        self.lbl_conn = Label(
            text='未连接', font_size=dp(13), size_hint_x=0.14,
            color=(0.9, 0.3, 0.3, 1),
        )
        s.add_widget(self.lbl_conn)
        root.add_widget(s)

        # ── API配置 ───────────────────────────────────────────────────────────
        a = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(6))
        a.add_widget(Label(text='API:', size_hint_x=0.06, font_size=dp(13)))
        self.inp_api = TextInput(
            text=cfg('api_base', 'http://192.168.1.100'),
            multiline=False, font_size=dp(13), size_hint_x=0.42,
        )
        a.add_widget(self.inp_api)
        a.add_widget(Label(text='ID:', size_hint_x=0.04, font_size=dp(13)))
        self.inp_did = TextInput(
            text=cfg('device_id', ''), multiline=False,
            font_size=dp(13), size_hint_x=0.26,
        )
        a.add_widget(self.inp_did)
        btn_save = Button(
            text='保存', font_size=dp(13), size_hint_x=0.10,
            background_color=(0.2, 0.48, 0.2, 1), background_normal='',
        )
        btn_save.bind(on_press=self._save_api)
        a.add_widget(btn_save)
        root.add_widget(a)

        # ── 系统控制行 ────────────────────────────────────────────────────────
        sys_row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(5))
        self.btn_immersive = Button(
            text='全屏', font_size=dp(12),
            background_color=(0.25, 0.40, 0.55, 1), background_normal='',
        )
        self.btn_immersive.bind(on_press=self._toggle_immersive)
        sys_row.add_widget(self.btn_immersive)

        btn_wifi = Button(
            text='WiFi设置', font_size=dp(12),
            background_color=(0.20, 0.45, 0.30, 1), background_normal='',
        )
        btn_wifi.bind(on_press=lambda _: _open_wifi_settings())
        sys_row.add_widget(btn_wifi)

        btn_eth = Button(
            text='以太网', font_size=dp(12),
            background_color=(0.20, 0.35, 0.45, 1), background_normal='',
        )
        btn_eth.bind(on_press=lambda _: _open_ethernet_settings())
        sys_row.add_widget(btn_eth)

        btn_chk_upd = Button(
            text='检查更新', font_size=dp(12),
            background_color=(0.38, 0.28, 0.50, 1), background_normal='',
        )
        btn_chk_upd.bind(on_press=self._manual_update)
        sys_row.add_widget(btn_chk_upd)

        btn_rollback = Button(
            text='版本回滚', font_size=dp(12),
            background_color=(0.50, 0.28, 0.12, 1), background_normal='',
        )
        btn_rollback.bind(on_press=self._rollback)
        sys_row.add_widget(btn_rollback)

        btn_reboot_dev = Button(
            text='重启设备', font_size=dp(12),
            background_color=(0.65, 0.12, 0.12, 1), background_normal='',
        )
        btn_reboot_dev.bind(on_press=self._reboot_device)
        sys_row.add_widget(btn_reboot_dev)

        root.add_widget(sys_row)

        # ── 16路锁测试 ────────────────────────────────────────────────────────
        sv = ScrollView()
        grid = GridLayout(
            cols=4, spacing=dp(6), padding=dp(2),
            size_hint_y=None, row_default_height=dp(65), row_force_default=True,
        )
        grid.bind(minimum_height=grid.setter('height'))
        for i in range(1, 17):
            btn = Button(
                text=f'锁{i:02d}\n测试', font_size=dp(14),
                background_color=(0.22, 0.50, 0.70, 1), background_normal='',
            )
            btn.bind(on_press=lambda b, n=i: self._test(n))
            grid.add_widget(btn)
        sv.add_widget(grid)
        root.add_widget(sv)

        self.lbl_log = Label(
            text='就绪', size_hint_y=None, height=dp(24),
            font_size=dp(12), halign='left', valign='middle',
            color=(0.70, 0.70, 0.70, 1),
        )
        self.lbl_log.bind(size=self.lbl_log.setter('text_size'))
        root.add_widget(self.lbl_log)

        bot = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(8))
        btn_rst = Button(
            text='重启App', font_size=dp(13), size_hint_x=0.22,
            background_color=(0.65, 0.30, 0.08, 1), background_normal='',
        )
        btn_rst.bind(on_press=lambda _: App.get_running_app().restart_app())
        bot.add_widget(btn_rst)
        root.add_widget(bot)
        self.add_widget(root)

    def on_enter(self):
        _set_immersive(False)
        self._immersive = False
        self.btn_immersive.text = '全屏'
        # refresh inputs from current config
        self.inp_port.text = cfg('port', '/dev/ttyS1')
        self.spn_baud.text = str(cfg('baudrate', 9600))
        self.inp_addr.text = str(cfg('board_addr', 1))
        self.inp_api.text  = cfg('api_base', 'http://192.168.1.100')
        self.inp_did.text  = cfg('device_id', '')
        self.lbl_log.text  = f'v{LocalLogger.APP_VERSION}'
        if ctrl.connected:
            self.btn_conn.text = '断开'
            self.btn_conn.background_color = (0.70, 0.35, 0.08, 1)
            self.lbl_conn.text = '已连接'
            self.lbl_conn.color = (0.2, 0.85, 0.35, 1)
        else:
            self.btn_conn.text = '连接'
            self.btn_conn.background_color = (0.2, 0.55, 0.95, 1)
            self.lbl_conn.text = '未连接'
            self.lbl_conn.color = (0.9, 0.3, 0.3, 1)
        threading.Thread(target=self._load_dev_info, daemon=True).start()

    def _load_dev_info(self):
        mac = _get_device_mac()
        ip  = _get_ip()
        aid = _get_android_id()
        fw  = _get_firmware()
        did = cfg('device_id', '--')
        def _upd(_):
            self.lbl_dev1.text = f'MAC: {mac or "--"}  IP: {ip or "--"}  设备ID: {did}'
            self.lbl_dev2.text = f'Android ID: {aid or "--"}  固件: {fw}'
        Clock.schedule_once(_upd)

    def _toggle_immersive(self, *_):
        self._immersive = not self._immersive
        _set_immersive(self._immersive)
        self.btn_immersive.text = '退出全屏' if self._immersive else '全屏'
        self.lbl_log.text = ('已进入全屏' if self._immersive else '已退出全屏')

    def _manual_update(self, *_):
        self.lbl_log.text = '正在检查更新...'
        def _run():
            cfg_mgr.check_script_update()
            Clock.schedule_once(lambda _: setattr(self.lbl_log, 'text', '更新检查完成'))
        threading.Thread(target=_run, daemon=True).start()

    def _rollback(self, *_):
        ok, msg = rollback_script()
        self.lbl_log.text = msg
        if ok:
            Clock.schedule_once(lambda _: App.get_running_app().restart_app(), 1.5)

    def _reboot_device(self, *_):
        logger.warn('管理员触发重启设备')
        self.lbl_log.text = '设备重启中...'
        Clock.schedule_once(lambda _: _system_reboot(), 0.5)

    def _toggle_conn(self, *_):
        if ctrl.connected:
            ctrl.disconnect()
            self.btn_conn.text = '连接'
            self.btn_conn.background_color = (0.2, 0.55, 0.95, 1)
            self.lbl_conn.text = '未连接'
            self.lbl_conn.color = (0.9, 0.3, 0.3, 1)
        else:
            if not SERIAL_AVAILABLE:
                self.lbl_log.text = '错误: pyserial 未安装'
                return
            port = self.inp_port.text.strip()
            baud = int(self.spn_baud.text)
            addr = int(self.inp_addr.text or '1')
            ok, msg = ctrl.connect(port, baud)
            cfg_set('port', port)
            cfg_set('baudrate', baud)
            cfg_set('board_addr', addr)
            if ok:
                self.btn_conn.text = '断开'
                self.btn_conn.background_color = (0.70, 0.35, 0.08, 1)
                self.lbl_conn.text = '已连接'
                self.lbl_conn.color = (0.2, 0.85, 0.35, 1)
                self.lbl_log.text = f'已连接 {port} @ {baud}'
                logger.info(f'串口连接: {port} @ {baud}')
            else:
                self.lbl_log.text = f'连接失败: {msg}'

    def _test(self, num: int):
        if not ctrl.connected:
            self.lbl_log.text = '未连接串口'
            return
        def _t():
            ok = ctrl.open_lock(cfg('board_addr', 1), num)
            msg = f'锁{num:02d} {"成功" if ok else "失败"} | {ctrl.last_error}'
            logger.info(f'[管理测试] {msg}')
            Clock.schedule_once(lambda _: setattr(self.lbl_log, 'text', msg))
        threading.Thread(target=_t, daemon=True).start()

    def _save_api(self, *_):
        cfg_set('api_base', self.inp_api.text.strip().rstrip('/'))
        cfg_set('device_id', self.inp_did.text.strip())
        self.lbl_log.text = 'API配置已保存'
        logger.info('API配置已更新')


# ─── 断网重启管理页 ───────────────────────────────────────────────────────────
class AdminRebootScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        root = BoxLayout(orientation='vertical', padding=dp(10), spacing=dp(8))
        _dark_bg(root, 0.10, 0.10, 0.13)

        top = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        btn_back = Button(
            text='← 返回', font_size=dp(14), size_hint_x=0.18,
            background_color=(0.28, 0.28, 0.33, 1), background_normal='',
        )
        btn_back.bind(on_press=lambda _: setattr(App.get_running_app().sm, 'current', 'admin'))
        top.add_widget(btn_back)
        top.add_widget(Label(text='断网自动重启', font_size=dp(18), bold=True))
        root.add_widget(top)

        # 配置显示
        self.lbl_cfg = Label(
            text='', font_size=dp(14), halign='left', valign='top',
            size_hint_y=None, height=dp(120),
            color=(0.8, 0.8, 0.8, 1),
        )
        self.lbl_cfg.bind(size=self.lbl_cfg.setter('text_size'))
        root.add_widget(self.lbl_cfg)

        # 状态显示
        self.lbl_status = Label(
            text='', font_size=dp(14), halign='left', valign='top',
            size_hint_y=None, height=dp(80),
            color=(0.7, 0.9, 0.7, 1),
        )
        self.lbl_status.bind(size=self.lbl_status.setter('text_size'))
        root.add_widget(self.lbl_status)

        btn_reset = Button(
            text='重置重启计数', font_size=dp(14),
            size_hint_y=None, height=dp(44),
            background_color=(0.5, 0.25, 0.1, 1), background_normal='',
        )
        btn_reset.bind(on_press=self._reset)
        root.add_widget(btn_reset)

        root.add_widget(Label(text='重启历史记录（最近30条）：', font_size=dp(13),
                               size_hint_y=None, height=dp(28), halign='left'))

        sv = ScrollView()
        self.lbl_hist = Label(
            text='', font_size=dp(12), halign='left', valign='top',
            size_hint_y=None, height=dp(400),
            color=(0.65, 0.65, 0.65, 1),
        )
        self.lbl_hist.bind(size=self.lbl_hist.setter('text_size'))
        sv.add_widget(self.lbl_hist)
        root.add_widget(sv)
        self.add_widget(root)

    def on_enter(self):
        self._refresh()

    def _refresh(self):
        self.lbl_cfg.text = (
            f'检测间隔: {cfg("network_check_interval", 60)}秒\n'
            f'断网重启延迟: {cfg("offline_reboot_delay", 10)}分钟\n'
            f'最大重启次数: {cfg("max_reboot_count", 5)}次\n'
            f'冷却时间: {cfg("reboot_cooldown", 60)}分钟\n'
            f'功能开关: {"开启" if cfg("offline_reboot_enabled", True) else "关闭"}'
        )
        if reboot_mgr:
            st = reboot_mgr.status()
            cooldown_str = ''
            if st['in_cooldown']:
                left = int(st['cooldown_until'] - time.time())
                cooldown_str = f'  冷却中，剩余{left}秒'
            self.lbl_status.text = (
                f'当前重启计数: {st["count"]}次{cooldown_str}\n'
                f'断网开始时间: {time.strftime("%H:%M:%S", time.localtime(st["offline_since"])) if st["offline_since"] else "网络正常"}'
            )
            hist = reboot_mgr.get_log(30)
            if hist:
                lines = [f'{h["time"]}  #{h["count"]}  {h["reason"]}' for h in reversed(hist)]
                self.lbl_hist.text = '\n'.join(lines)
            else:
                self.lbl_hist.text = '暂无重启记录'

    def _reset(self, *_):
        if reboot_mgr:
            reboot_mgr.reset_count()
        self._refresh()


# ─── 操作日志查看页 ───────────────────────────────────────────────────────────
class AdminLogScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        root = BoxLayout(orientation='vertical', padding=dp(10), spacing=dp(8))
        _dark_bg(root, 0.10, 0.10, 0.13)

        top = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        btn_back = Button(
            text='← 返回', font_size=dp(14), size_hint_x=0.18,
            background_color=(0.28, 0.28, 0.33, 1), background_normal='',
        )
        btn_back.bind(on_press=lambda _: setattr(App.get_running_app().sm, 'current', 'admin'))
        top.add_widget(btn_back)
        top.add_widget(Label(text='操作日志', font_size=dp(18), bold=True))
        btn_refresh = Button(
            text='刷新', font_size=dp(13), size_hint_x=0.15,
            background_color=(0.2, 0.4, 0.2, 1), background_normal='',
        )
        btn_refresh.bind(on_press=lambda _: self._load())
        top.add_widget(btn_refresh)
        root.add_widget(top)

        sv = ScrollView()
        self.lbl_log = Label(
            text='加载中...', font_size=dp(12), halign='left', valign='top',
            size_hint_y=None, height=dp(600),
            color=(0.75, 0.75, 0.75, 1),
        )
        self.lbl_log.bind(size=self.lbl_log.setter('text_size'))
        sv.add_widget(self.lbl_log)
        root.add_widget(sv)
        self.add_widget(root)

    def on_enter(self):
        self._load()

    def _load(self, *_):
        if logger:
            lines = logger.tail(100)
            self.lbl_log.text = '\n'.join(reversed(lines)) if lines else '暂无日志'
        else:
            self.lbl_log.text = '日志未初始化'


# ─── 后台服务 ─────────────────────────────────────────────────────────────────
class BackgroundServices:
    def __init__(self):
        self._running = False
        self._last_cmd_id = None

    def start(self):
        self._running = True
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        threading.Thread(target=self._cmd_loop, daemon=True).start()
        threading.Thread(target=self._log_upload_loop, daemon=True).start()

    def stop(self):
        self._running = False

    def _heartbeat_loop(self):
        while self._running:
            try:
                api.heartbeat()
            except Exception:
                pass
            time.sleep(60)

    def _cmd_loop(self):
        while self._running:
            try:
                resp = api.poll_command()
                if resp and resp.get('code') == 0:
                    data = resp.get('data')
                    if data and data.get('id') != self._last_cmd_id:
                        self._last_cmd_id = data['id']
                        threading.Thread(target=self._exec_cmd, args=(data,), daemon=True).start()
            except Exception:
                pass
            time.sleep(2)

    def _log_upload_loop(self):
        time.sleep(30)
        while self._running:
            try:
                if _check_network() and logger:
                    logger.upload_pending(api)
            except Exception:
                pass
            time.sleep(120)

    def _exec_cmd(self, data: dict):
        cmd_id = data.get('id', '')
        lock   = int(data.get('lock', 0))
        atype  = int(data.get('type', 1))
        logger.info(f'[远程指令] 锁{lock} type={atype}')
        if not ctrl.connected:
            api.ack_command(cmd_id, False, '串口未连接')
            return
        ok = ctrl.open_lock(cfg('board_addr', 1), lock)
        api.ack_command(cmd_id, ok, '' if ok else ctrl.last_error)
        api.report_open_result(lock, ok, atype)
        logger.info(f'[远程指令] 锁{lock} {"成功" if ok else "失败"}')


# ─── App 主入口 ───────────────────────────────────────────────────────────────
class DoorLockApp(App):
    def build(self):
        Window.clearcolor = (0.08, 0.08, 0.10, 1)
        _cfg_load(self.user_data_dir)

        global logger, reboot_mgr, cfg_mgr, poster_mgr
        logger = LocalLogger(os.path.join(self.user_data_dir, 'app.log'))
        reboot_mgr = NetworkRebootManager(
            os.path.join(self.user_data_dir, 'reboot_state.json'),
            os.path.join(self.user_data_dir, 'reboot_log.jsonl'),
        )
        cfg_mgr = RemoteConfigManager()
        poster_mgr = PosterManager(os.path.join(self.user_data_dir, 'posters'), api)

        self.sm = ScreenManager(transition=NoTransition())
        for name, cls in [
            ('init',         InitWaitScreen),
            ('poster',       PosterScreen),
            ('password',     PasswordScreen),
            ('result',       ResultScreen),
            ('admin_auth',   AdminAuthScreen),
            ('admin',        AdminScreen),
            ('admin_reboot', AdminRebootScreen),
            ('admin_log',    AdminLogScreen),
        ]:
            self.sm.add_widget(cls(name=name))

        self.sm.current = 'poster' if cfg('device_id') else 'init'

        self._svc = BackgroundServices()
        self._svc.start()
        reboot_mgr.start()
        cfg_mgr.start()

        logger.info(f'App启动 v{LocalLogger.APP_VERSION} 设备ID={cfg("device_id","未初始化")}')

        if cfg('port') and SERIAL_AVAILABLE:
            threading.Thread(target=self._auto_connect, daemon=True).start()

        return self.sm

    def _auto_connect(self):
        time.sleep(1)
        ok, msg = ctrl.connect(cfg('port', '/dev/ttyS1'), cfg('baudrate', 9600))
        logger.info(f'自动连接串口: {msg}')

    def on_stop(self):
        self._svc.stop()
        reboot_mgr.stop()
        cfg_mgr.stop()
        ctrl.disconnect()
        logger.info('App停止')

    def restart_app(self):
        logger.info('正在重启App...')
        try:
            # Android：用系统 Intent 重启，os.execl 在 Android 上无效
            from jnius import autoclass
            Intent = autoclass('android.content.Intent')
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            activity = PythonActivity.mActivity
            intent = activity.getPackageManager().getLaunchIntentForPackage(
                activity.getPackageName()
            )
            intent.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP)
            activity.startActivity(intent)
            self.stop()
        except Exception:
            # 桌面端 fallback
            try:
                os.execl(sys.executable, sys.executable, *sys.argv)
            except Exception:
                self.stop()

    def go_poster(self):
        self.sm.current = 'poster'

    def go_password(self):
        self.sm.current = 'password'

    def go_result(self, lock: int, ok: bool, msg: str, atype: int):
        self.sm.get_screen('result').set_params(lock, ok, msg, atype)
        self.sm.current = 'result'


if __name__ == '__main__':
    DoorLockApp().run()
