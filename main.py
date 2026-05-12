"""
钥匙柜控制系统 v2.0
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
import hashlib
import hmac
import json
import os
import threading
import time

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

# ─── 中文字体 ─────────────────────────────────────────────────────────────────
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

# ─── 配置管理 ─────────────────────────────────────────────────────────────────
_CFG: dict = {}
_CFG_FILE: str = ''


def _cfg_load(data_dir: str):
    global _CFG, _CFG_FILE
    _CFG_FILE = os.path.join(data_dir, 'config.json')
    if os.path.exists(_CFG_FILE):
        try:
            with open(_CFG_FILE, encoding='utf-8') as f:
                _CFG = json.load(f)
        except Exception:
            _CFG = {}


def _cfg_save():
    if _CFG_FILE:
        try:
            with open(_CFG_FILE, 'w', encoding='utf-8') as f:
                json.dump(_CFG, f, ensure_ascii=False, indent=2)
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
            'mac_address': mac,
            'android_id': android_id,
            'model': 'RK3288',
            'os_version': 'Android 10',
            'app_version': '2.0.0',
        })

    def heartbeat(self, network_type='wifi'):
        return self._post('/api/device/heartbeat', {
            'device_id': cfg('device_id', ''),
            'online': True,
            'network_type': network_type,
        })

    def get_posters(self):
        return self._get('/api/poster/list', device_id=cfg('device_id', ''))

    def verify_password(self, password):
        return self._post('/api/password/verify', {
            'device_id': cfg('device_id', ''),
            'password': password,
        })

    def poll_command(self):
        return self._get('/api/cabinet/cmd', did=cfg('device_id', 'cabinet_001'))

    def ack_command(self, cmd_id, ok, msg=''):
        self._post('/api/cabinet/ack', {'id': cmd_id, 'ok': ok, 'msg': msg})

    def report_open_result(self, lock, ok, action_type):
        self._post('/api/cabinet/open-result', {
            'device_id': cfg('device_id', ''),
            'lock': lock,
            'ok': ok,
            'type': action_type,
        })


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


# ─── 海报管理器 ───────────────────────────────────────────────────────────────
class PosterManager:
    def __init__(self, cache_dir: str, api_client: ApiClient):
        self._dir = cache_dir
        self._api = api_client
        self._posters: list = []
        self._rlock = threading.Lock()
        os.makedirs(cache_dir, exist_ok=True)
        self._load_cached()

    @property
    def posters(self):
        with self._rlock:
            return list(self._posters)

    def refresh(self):
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self):
        resp = self._api.get_posters()
        if not resp or resp.get('code') != 0:
            return
        data = resp.get('data', {})
        cfg_set('poster_interval', data.get('interval', 5))
        items = data.get('list', [])
        local_paths = []
        known_ids = set()
        for item in items:
            pid = item.get('id', '')
            url = item.get('url', '')
            md5 = item.get('md5', '')
            known_ids.add(pid)
            if not url:
                continue
            local = os.path.join(self._dir, f'{pid}.jpg')
            if os.path.exists(local) and self._md5(local) == md5:
                local_paths.append(local)
                continue
            try:
                r = _req.get(url, timeout=15)
                if r.status_code == 200:
                    with open(local, 'wb') as f:
                        f.write(r.content)
                    local_paths.append(local)
            except Exception:
                if os.path.exists(local):
                    local_paths.append(local)
        for fname in os.listdir(self._dir):
            fid = fname.rsplit('.', 1)[0]
            if fid not in known_ids:
                try:
                    os.remove(os.path.join(self._dir, fname))
                except Exception:
                    pass
        if local_paths:
            with self._rlock:
                self._posters = local_paths

    def _load_cached(self):
        files = sorted(
            os.path.join(self._dir, f)
            for f in os.listdir(self._dir)
            if f.lower().endswith(('.jpg', '.png'))
        )
        with self._rlock:
            self._posters = files

    @staticmethod
    def _md5(path):
        h = hashlib.md5()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()


# ─── 全局单例 ─────────────────────────────────────────────────────────────────
ctrl = LockController()
api = ApiClient()
poster_mgr: PosterManager = None  # type: ignore


def _check_network() -> bool:
    if not REQUESTS_AVAILABLE:
        return False
    try:
        _req.get(cfg('api_base', 'http://192.168.1.100'), timeout=2)
        return True
    except Exception:
        return False


# ─── 背景辅助 ─────────────────────────────────────────────────────────────────
def _dark_bg(widget, r=0.08, g=0.08, b=0.10):
    with widget.canvas.before:
        col = Color(r, g, b, 1)
        rect = Rectangle(size=widget.size, pos=widget.pos)
    widget.bind(
        size=lambda *a: setattr(rect, 'size', widget.size),
        pos=lambda *a: setattr(rect, 'pos', widget.pos),
    )


# ─── 初始化等待页 ─────────────────────────────────────────────────────────────
class InitWaitScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        root = FloatLayout()
        _dark_bg(root, 0.08, 0.08, 0.10)

        self.lbl_title = Label(
            text='⏳ 设备初始化中',
            font_size=dp(28), bold=True,
            pos_hint={'center_x': 0.5, 'center_y': 0.62},
            size_hint=(None, None), size=(dp(500), dp(55)),
            halign='center',
        )
        self.lbl_hint = Label(
            text='请确保设备已连接网络',
            font_size=dp(18),
            pos_hint={'center_x': 0.5, 'center_y': 0.52},
            size_hint=(None, None), size=(dp(500), dp(40)),
            halign='center',
            color=(0.75, 0.75, 0.75, 1),
        )
        self.lbl_net = Label(
            text='网络状态：检测中...',
            font_size=dp(16),
            pos_hint={'center_x': 0.5, 'center_y': 0.43},
            size_hint=(None, None), size=(dp(500), dp(35)),
            halign='center',
        )
        self.lbl_cd = Label(
            text='',
            font_size=dp(15),
            pos_hint={'center_x': 0.5, 'center_y': 0.34},
            size_hint=(None, None), size=(dp(400), dp(30)),
            halign='center',
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
        resp = api.init_device()
        if resp and resp.get('code') == 0:
            d = resp['data']
            cfg_set('device_id', d['device_id'])
            cfg_set('device_secret', d.get('device_secret', ''))
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
            source='',
            allow_stretch=True, keep_ratio=True,
            size_hint=(1, 1), pos_hint={'x': 0, 'y': 0},
        )
        root.add_widget(self.img)

        self.lbl_empty = Label(
            text='点击屏幕进入密码输入',
            font_size=dp(22),
            pos_hint={'center_x': 0.5, 'center_y': 0.5},
            size_hint=(None, None), size=(dp(500), dp(50)),
            halign='center',
            color=(0.55, 0.55, 0.55, 1),
        )
        root.add_widget(self.lbl_empty)

        self.lbl_did = Label(
            text='',
            font_size=dp(13),
            pos_hint={'right': 0.99, 'y': 0.01},
            size_hint=(None, None), size=(dp(200), dp(28)),
            halign='right', color=(0.6, 0.6, 0.6, 1),
        )
        root.add_widget(self.lbl_did)

        self.lbl_net = Label(
            text='●',
            font_size=dp(22),
            pos_hint={'right': 0.99, 'top': 0.99},
            size_hint=(None, None), size=(dp(50), dp(40)),
            color=(0.5, 0.5, 0.5, 1),
        )
        root.add_widget(self.lbl_net)

        self.lbl_dots = Label(
            text='',
            font_size=dp(14),
            pos_hint={'center_x': 0.5, 'y': 0.01},
            size_hint=(None, None), size=(dp(300), dp(28)),
            halign='center', color=(0.8, 0.8, 0.8, 1),
        )
        root.add_widget(self.lbl_dots)

        self.add_widget(root)

    def on_enter(self):
        self.lbl_did.text = cfg('device_id', '--')
        self._reload()
        self._auto_ev = Clock.schedule_interval(self._advance, cfg('poster_interval', 5))
        self._net_ev = Clock.schedule_interval(self._check_net, 15)
        self._check_net(0)
        poster_mgr.refresh()

    def on_leave(self):
        if hasattr(self, '_auto_ev'):
            self._auto_ev.cancel()
        if hasattr(self, '_net_ev'):
            self._net_ev.cancel()

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
        if n <= 1:
            self.lbl_dots.text = ''
        else:
            self.lbl_dots.text = ''.join('●' if i == self._idx % n else '○' for i in range(n))

    def _advance(self, dt):
        p = poster_mgr.posters
        if not p:
            return
        self._idx = (self._idx + 1) % len(p)
        self.img.source = p[self._idx]
        self._dots()

    def _check_net(self, dt):
        threading.Thread(target=self._do_net_check, daemon=True).start()

    def _do_net_check(self):
        online = _check_network()
        def _upd(_):
            self.lbl_net.color = (0.2, 0.9, 0.4, 1) if online else (0.9, 0.3, 0.3, 1)
        Clock.schedule_once(_upd)

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
            n = len(p)
            self._idx = (self._idx + (1 if dx < 0 else -1)) % n
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
    _TIMEOUT = 60

    def __init__(self, **kw):
        super().__init__(**kw)
        self._pwd = ''
        self._errors = 0
        self._locked_until = 0.0
        self._idle_ev = None

        root = FloatLayout()
        _dark_bg(root, 0.10, 0.10, 0.13)

        btn_back = Button(
            text='← 返回', font_size=dp(16),
            size_hint=(None, None), size=(dp(110), dp(42)),
            pos_hint={'x': 0.02, 'top': 0.97},
            background_color=(0.28, 0.28, 0.33, 1),
            background_normal='',
        )
        btn_back.bind(on_press=lambda _: App.get_running_app().go_poster())
        root.add_widget(btn_back)

        root.add_widget(Label(
            text='请输入开柜密码',
            font_size=dp(24), bold=True,
            pos_hint={'center_x': 0.5, 'top': 0.88},
            size_hint=(None, None), size=(dp(500), dp(50)),
            halign='center',
        ))

        self.lbl_pwd = Label(
            text='_ _ _ _ _ _',
            font_size=dp(38), bold=True,
            pos_hint={'center_x': 0.5, 'top': 0.74},
            size_hint=(None, None), size=(dp(500), dp(58)),
            halign='center',
            color=(0.95, 0.95, 0.95, 1),
        )
        root.add_widget(self.lbl_pwd)

        self.lbl_err = Label(
            text='',
            font_size=dp(15),
            pos_hint={'center_x': 0.5, 'top': 0.63},
            size_hint=(None, None), size=(dp(500), dp(35)),
            halign='center',
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
            text='',
            font_size=dp(13),
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
        self._remaining = self._TIMEOUT
        self._idle_ev = Clock.schedule_interval(self._idle_tick, 1)

    def on_leave(self):
        if self._idle_ev:
            self._idle_ev.cancel()
            self._idle_ev = None

    def _idle_tick(self, dt):
        self._remaining -= 1
        self.lbl_to.text = f'{self._remaining}秒后自动返回'
        if self._remaining <= 0:
            App.get_running_app().go_poster()

    def _key(self, k: str):
        self._remaining = self._TIMEOUT
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
            Clock.schedule_once(lambda _: self._do_open(d['lock'], d.get('type', 1), True))
            return
        for lock_no in range(1, 17):
            if verify_offline_password(pwd, lock_no):
                self._errors = 0
                Clock.schedule_once(lambda _: self._do_open(lock_no, 1, False))
                return
        self._errors += 1
        max_e = cfg('max_error_count', 5)
        lock_d = cfg('lock_duration', 180)
        if self._errors >= max_e:
            self._locked_until = time.time() + lock_d
            Clock.schedule_once(lambda _: setattr(
                self.lbl_err, 'text', f'错误过多，锁定{lock_d//60}分钟'
            ))
        else:
            left = max_e - self._errors
            Clock.schedule_once(lambda _: setattr(
                self.lbl_err, 'text', f'密码错误，还可尝试{left}次'
            ))

    def _do_open(self, lock: int, action_type: int, online: bool):
        if not ctrl.connected:
            App.get_running_app().go_result(lock=lock, ok=False, msg='柜门故障', atype=action_type)
            return
        threading.Thread(target=self._exec_open, args=(lock, action_type), daemon=True).start()

    def _exec_open(self, lock: int, action_type: int):
        ok = ctrl.open_lock(cfg('board_addr', 1), lock)
        api.report_open_result(lock, ok, action_type)
        msg = '' if ok else '柜门故障'
        Clock.schedule_once(lambda _: App.get_running_app().go_result(
            lock=lock, ok=ok, msg=msg, atype=action_type
        ))


# ─── 开柜结果页 ───────────────────────────────────────────────────────────────
class ResultScreen(Screen):
    _AUTO = 10

    def __init__(self, **kw):
        super().__init__(**kw)
        self._p = {}

        root = FloatLayout()
        _dark_bg(root, 0.08, 0.10, 0.08)

        self.lbl_icon = Label(
            text='', font_size=dp(72),
            pos_hint={'center_x': 0.5, 'center_y': 0.68},
            size_hint=(None, None), size=(dp(200), dp(110)),
            halign='center',
        )
        self.lbl_main = Label(
            text='', font_size=dp(32), bold=True,
            pos_hint={'center_x': 0.5, 'center_y': 0.52},
            size_hint=(None, None), size=(dp(560), dp(58)),
            halign='center',
        )
        self.lbl_sub = Label(
            text='', font_size=dp(20),
            pos_hint={'center_x': 0.5, 'center_y': 0.40},
            size_hint=(None, None), size=(dp(560), dp(42)),
            halign='center',
            color=(0.75, 0.75, 0.75, 1),
        )
        self.lbl_cd = Label(
            text='', font_size=dp(15),
            pos_hint={'center_x': 0.5, 'y': 0.04},
            size_hint=(None, None), size=(dp(300), dp(30)),
            halign='center', color=(0.5, 0.5, 0.5, 1),
        )
        for w in (self.lbl_icon, self.lbl_main, self.lbl_sub, self.lbl_cd):
            root.add_widget(w)
        self.add_widget(root)

    def set_params(self, lock, ok, msg, atype):
        self._p = {'lock': lock, 'ok': ok, 'msg': msg, 'atype': atype}

    def on_enter(self):
        lock  = self._p.get('lock', 0)
        ok    = self._p.get('ok', False)
        msg   = self._p.get('msg', '')
        atype = self._p.get('atype', 1)
        if ok:
            self.lbl_icon.text  = '✅'
            self.lbl_main.text  = '开柜成功'
            self.lbl_main.color = (0.2, 0.9, 0.4, 1)
            action = '请取走钥匙' if atype == 1 else '请存放钥匙'
            self.lbl_sub.text = f'{lock:02d}号柜门已开  {action}'
        elif msg == '柜门故障':
            self.lbl_icon.text  = '⚠️'
            self.lbl_main.text  = '柜门故障'
            self.lbl_main.color = (0.9, 0.6, 0.2, 1)
            self.lbl_sub.text   = '请联系管理员'
        else:
            self.lbl_icon.text  = '❌'
            self.lbl_main.text  = '开柜失败'
            self.lbl_main.color = (0.9, 0.3, 0.3, 1)
            self.lbl_sub.text   = msg or '密码错误，请重试'
        self._remaining = self._AUTO
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
            text='管理员验证',
            font_size=dp(22), bold=True,
            pos_hint={'center_x': 0.5, 'top': 0.88},
            size_hint=(None, None), size=(dp(400), dp(50)),
            halign='center',
        ))

        self.lbl_pwd = Label(
            text='_ _ _ _ _ _', font_size=dp(30), bold=True,
            pos_hint={'center_x': 0.5, 'top': 0.75},
            size_hint=(None, None), size=(dp(400), dp(55)),
            halign='center',
        )
        root.add_widget(self.lbl_pwd)

        self.lbl_err = Label(
            text='', font_size=dp(15),
            pos_hint={'center_x': 0.5, 'top': 0.64},
            size_hint=(None, None), size=(dp(400), dp(32)),
            halign='center', color=(0.9, 0.3, 0.3, 1),
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

        root = BoxLayout(orientation='vertical', padding=dp(10), spacing=dp(6))
        _dark_bg(root, 0.10, 0.10, 0.13)

        # 顶部
        top = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        btn_back = Button(
            text='← 返回', font_size=dp(14),
            size_hint_x=0.14,
            background_color=(0.28, 0.28, 0.33, 1), background_normal='',
        )
        btn_back.bind(on_press=lambda _: App.get_running_app().go_poster())
        top.add_widget(btn_back)
        top.add_widget(Label(text='内部管理', font_size=dp(18), bold=True))
        root.add_widget(top)

        # 串口配置
        s = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        s.add_widget(Label(text='端口:', size_hint_x=0.09, font_size=dp(13)))
        self.inp_port = TextInput(
            text=cfg('port', '/dev/ttyS1'),
            multiline=False, font_size=dp(13), size_hint_x=0.24,
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
            text=str(cfg('board_addr', 1)),
            multiline=False, input_filter='int', font_size=dp(13), size_hint_x=0.07,
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

        # API配置
        a = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(6))
        a.add_widget(Label(text='API:', size_hint_x=0.06, font_size=dp(13)))
        self.inp_api = TextInput(
            text=cfg('api_base', 'http://192.168.1.100'),
            multiline=False, font_size=dp(13), size_hint_x=0.40,
        )
        a.add_widget(self.inp_api)
        a.add_widget(Label(text='ID:', size_hint_x=0.04, font_size=dp(13)))
        self.inp_did = TextInput(
            text=cfg('device_id', ''),
            multiline=False, font_size=dp(13), size_hint_x=0.26,
        )
        a.add_widget(self.inp_did)
        btn_save = Button(
            text='保存', font_size=dp(13), size_hint_x=0.10,
            background_color=(0.2, 0.48, 0.2, 1), background_normal='',
        )
        btn_save.bind(on_press=self._save_api)
        a.add_widget(btn_save)
        self.lbl_api_st = Label(
            text='', font_size=dp(12), size_hint_x=0.14,
            color=(0.6, 0.6, 0.6, 1),
        )
        a.add_widget(self.lbl_api_st)
        root.add_widget(a)

        # 16路锁测试
        sv = ScrollView()
        grid = GridLayout(
            cols=4, spacing=dp(8), padding=dp(2),
            size_hint_y=None,
            row_default_height=dp(75),
            row_force_default=True,
        )
        grid.bind(minimum_height=grid.setter('height'))
        for i in range(1, 17):
            btn = Button(
                text=f'锁{i:02d}\n测试',
                font_size=dp(15),
                background_color=(0.22, 0.50, 0.70, 1),
                background_normal='',
            )
            btn.bind(on_press=lambda b, n=i: self._test(n))
            grid.add_widget(btn)
        sv.add_widget(grid)
        root.add_widget(sv)

        # 日志 + 底部操作
        self.lbl_log = Label(
            text='就绪',
            size_hint_y=None, height=dp(26),
            font_size=dp(12), halign='left', valign='middle',
            color=(0.70, 0.70, 0.70, 1),
        )
        self.lbl_log.bind(size=self.lbl_log.setter('text_size'))
        root.add_widget(self.lbl_log)

        bot = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(8))
        self.lbl_info = Label(
            text='', font_size=dp(11), halign='left',
            color=(0.55, 0.55, 0.55, 1),
        )
        bot.add_widget(self.lbl_info)
        btn_rst = Button(
            text='重启App', font_size=dp(13), size_hint_x=0.18,
            background_color=(0.65, 0.30, 0.08, 1), background_normal='',
        )
        btn_rst.bind(on_press=lambda _: App.get_running_app().stop())
        bot.add_widget(btn_rst)
        root.add_widget(bot)

        self.add_widget(root)

    def on_enter(self):
        self.lbl_info.text = f'设备ID: {cfg("device_id","--")}  API: {cfg("api_base","--")}'
        if ctrl.connected:
            self.btn_conn.text = '断开'
            self.btn_conn.background_color = (0.70, 0.35, 0.08, 1)
            self.lbl_conn.text = '已连接'
            self.lbl_conn.color = (0.2, 0.85, 0.35, 1)

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
            else:
                self.lbl_log.text = f'连接失败: {msg}'

    def _test(self, num: int):
        if not ctrl.connected:
            self.lbl_log.text = '未连接串口'
            return
        def _t():
            ok = ctrl.open_lock(cfg('board_addr', 1), num)
            Clock.schedule_once(lambda _: setattr(
                self.lbl_log, 'text',
                f'锁{num:02d} {"成功" if ok else "失败"} | {ctrl.last_error}'
            ))
        threading.Thread(target=_t, daemon=True).start()

    def _save_api(self, *_):
        cfg_set('api_base', self.inp_api.text.strip().rstrip('/'))
        cfg_set('device_id', self.inp_did.text.strip())
        self.lbl_api_st.text = '已保存'
        self.lbl_info.text = f'设备ID: {cfg("device_id","--")}  API: {cfg("api_base","--")}'


# ─── 后台服务 ─────────────────────────────────────────────────────────────────
class BackgroundServices:
    def __init__(self):
        self._running = False
        self._last_cmd_id = None

    def start(self):
        self._running = True
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        threading.Thread(target=self._cmd_loop, daemon=True).start()

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
                        threading.Thread(
                            target=self._exec_cmd, args=(data,), daemon=True
                        ).start()
            except Exception:
                pass
            time.sleep(2)

    def _exec_cmd(self, data: dict):
        cmd_id = data.get('id', '')
        lock   = int(data.get('lock', 0))
        atype  = int(data.get('type', 1))
        if not ctrl.connected:
            api.ack_command(cmd_id, False, '串口未连接')
            return
        ok = ctrl.open_lock(cfg('board_addr', 1), lock)
        api.ack_command(cmd_id, ok, '' if ok else ctrl.last_error)
        api.report_open_result(lock, ok, atype)


# ─── App 主入口 ───────────────────────────────────────────────────────────────
class DoorLockApp(App):
    def build(self):
        Window.clearcolor = (0.08, 0.08, 0.10, 1)
        _cfg_load(self.user_data_dir)

        global poster_mgr
        poster_mgr = PosterManager(
            os.path.join(self.user_data_dir, 'posters'),
            api,
        )

        self.sm = ScreenManager(transition=NoTransition())
        self.sm.add_widget(InitWaitScreen(name='init'))
        self.sm.add_widget(PosterScreen(name='poster'))
        self.sm.add_widget(PasswordScreen(name='password'))
        self.sm.add_widget(ResultScreen(name='result'))
        self.sm.add_widget(AdminAuthScreen(name='admin_auth'))
        self.sm.add_widget(AdminScreen(name='admin'))

        self.sm.current = 'poster' if cfg('device_id') else 'init'

        self._svc = BackgroundServices()
        self._svc.start()

        if cfg('port') and SERIAL_AVAILABLE:
            threading.Thread(target=self._auto_connect, daemon=True).start()

        return self.sm

    def _auto_connect(self):
        time.sleep(1)
        ctrl.connect(cfg('port', '/dev/ttyS1'), cfg('baudrate', 9600))

    def on_stop(self):
        self._svc.stop()
        ctrl.disconnect()

    def go_poster(self):
        self.sm.current = 'poster'

    def go_password(self):
        self.sm.current = 'password'

    def go_result(self, lock: int, ok: bool, msg: str, atype: int):
        self.sm.get_screen('result').set_params(lock, ok, msg, atype)
        self.sm.current = 'result'


if __name__ == '__main__':
    DoorLockApp().run()
