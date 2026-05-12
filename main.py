"""
16路门锁控制系统
协议: 老铁 5字节帧协议（逆向自智能柜_1.6.2.349.apk）
帧格式: 8A [board] [lock] 11 [XOR(前4字节)]
响应格式: [0x8A或0x80] [board] [lock] [0x11=已锁/0x00=开锁] [XOR]
状态查询: 8A [board] 00 11 [XOR]，板卡逐锁回包（最多16个）
"""
# 支持从U盘/SD卡热更新脚本，自动持久化到内部存储
import os as _os, runpy as _runpy, shutil as _shutil
_INTERNAL = _os.path.join(_os.path.expanduser('~'), 'door_lock_main.py')
_USB_PATHS = [
    '/sdcard/door_lock_main.py',
    '/storage/emulated/0/door_lock_main.py',
    '/storage/self/primary/door_lock_main.py',
]
for _src in _USB_PATHS:
    if _os.path.exists(_src):
        _shutil.copy2(_src, _INTERNAL)  # 复制到内部，覆盖旧版
        break
if _os.path.exists(_INTERNAL):
    _runpy.run_path(_INTERNAL, run_name='__main__')
    raise SystemExit

import threading
import time

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

_POLL_INTERVAL = 2.0

import os

# 必须在其他 kivy 模块之前设置环境
os.environ.setdefault('KIVY_NO_ENV_CONFIG', '1')

from kivy.core.text import LabelBase
from kivy.app import App

# 注册中文字体（优先使用 ASCII+中文 均完整的字体）
_CHINESE_FONTS = [
    'chinese_font.ttf',                                    # 本地字体（最优先）
    '/System/Library/Fonts/Hiragino Sans GB.ttc',          # macOS，含 Latin+CJK
    '/System/Library/Fonts/STHeiti Medium.ttc',            # macOS，含 Latin+CJK
    '/system/fonts/NotoSansCJK-Regular.ttc',               # Android
    '/system/fonts/DroidSansFallback.ttf',                 # 旧版 Android
    '/System/Library/Fonts/Supplemental/NISC18030.ttf',   # 仅 CJK，备用
]
for _font in _CHINESE_FONTS:
    if os.path.exists(_font):
        try:
            LabelBase.register(name='Roboto', fn_regular=_font)
            print(f'[字体] 加载成功: {_font}')
            break
        except Exception as e:
            print(f'[字体] 加载失败 {_font}: {e}')
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput
from kivy.uix.scrollview import ScrollView
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp


# ─── 老铁帧协议 ───────────────────────────────────────────────────────────────
# 逆向自智能柜_1.6.2.349.apk（d4/e.java: e.f4200a.b(board, lock)）
# 帧: 8A [board] [lock] 11 [XOR(前4字节)]

def _laotie_frame(board: int, lock: int) -> bytes:
    frame = bytearray([0x8A, board & 0xFF, lock & 0xFF, 0x11])
    xor = 0
    for b in frame:
        xor ^= b
    frame.append(xor & 0xFF)
    return bytes(frame)


def build_open_cmd(board_addr: int, lock_num: int) -> bytes:
    return _laotie_frame(board_addr, lock_num)


def build_status_cmd(board_addr: int) -> bytes:
    return _laotie_frame(board_addr, 0x00)


# ─── 串口控制器 ────────────────────────────────────────────────────────────────

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
                stopbits=1, timeout=0.5
            )
            return True, "连接成功"
        except Exception as e:
            self._ser = None
            return False, str(e)

    def disconnect(self):
        if self._ser and self._ser.is_open:
            self._ser.close()
        self._ser = None

    @property
    def connected(self):
        return self._ser is not None and self._ser.is_open

    def _send(self, cmd: bytes, read_len: int = 5) -> bytes | None:
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
        resp = self._send(build_open_cmd(addr, lock_num))
        if not resp or len(resp) < 5:
            return False
        # result[3] == 0x11 或 0x00 均表示执行成功
        return resp[3] in (0x11, 0x00)

    def query_status(self, addr: int) -> dict[int, bool] | None:
        """读取板卡各锁状态，返回 {lock_num: is_locked} 或 None"""
        cmd = build_status_cmd(addr)
        if not self.connected:
            return None
        with self._lock:
            try:
                self._ser.reset_input_buffer()
                self._ser.write(cmd)
                self._ser.flush()
                time.sleep(0.5)
                # 板卡逐锁回包，每包5字节，最多16锁
                raw = self._ser.read(5 * 16)
                self.last_error = f'状态查询 发:{cmd.hex()} 收:{raw.hex() if raw else "空"}'
                if not raw or len(raw) < 5:
                    return None
                states: dict[int, bool] = {}
                for i in range(0, len(raw) - 4, 5):
                    pkt = raw[i:i+5]
                    if pkt[0] in (0x8A, 0x80) and pkt[1] == addr:
                        lock_no = pkt[2]
                        locked = pkt[3] == 0x11
                        states[lock_no] = locked
                return states if states else None
            except Exception as e:
                self.last_error = str(e)
                return None


# ─── API 远程轮询 ─────────────────────────────────────────────────────────────
#
# 接口规范（服务端需实现）：
#
# 1. 取指令
#    GET {api_base}/api/cabinet/cmd?did={device_id}
#    响应（无指令）: {"code": 0, "data": null}
#    响应（有指令）: {"code": 0, "data": {"id": "唯一ID", "lock": 5, "type": 1}}
#      type: 1=取钥匙  2=还钥匙（当前阶段两者均执行开锁）
#
# 2. 执行回调
#    POST {api_base}/api/cabinet/ack
#    Body: {"id": "唯一ID", "ok": true, "msg": ""}
#

class ApiPoller:
    def __init__(self):
        self.api_base  = 'http://192.168.1.100'
        self.device_id = 'cabinet_001'
        self._running    = False
        self._last_id    = None
        self._ctrl       = None
        self._get_addr   = None
        self._log        = None
        self._set_status = None

    def configure(self, ctrl, get_addr, log_fn, set_status):
        self._ctrl       = ctrl
        self._get_addr   = get_addr
        self._log        = log_fn
        self._set_status = set_status

    def start(self):
        if self._running or not REQUESTS_AVAILABLE:
            return
        self._running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            self._poll()
            time.sleep(_POLL_INTERVAL)

    def _poll(self):
        try:
            r = _req.get(
                f'{self.api_base}/api/cabinet/cmd',
                params={'did': self.device_id},
                timeout=3
            )
            if r.status_code == 200:
                Clock.schedule_once(lambda _: self._set_status(True))
                data = r.json().get('data')
                if data and data.get('id') != self._last_id:
                    threading.Thread(target=self._execute, args=(data,), daemon=True).start()
        except Exception:
            Clock.schedule_once(lambda _: self._set_status(False))

    def _execute(self, data):
        cmd_id      = data.get('id', '')
        lock        = int(data.get('lock', 0))
        action_type = int(data.get('type', 1))
        label       = '取钥匙' if action_type == 1 else '还钥匙'
        self._last_id = cmd_id

        if not (self._ctrl and self._ctrl.connected):
            self._log(f'[远程] {label} 锁{lock:02d} — 串口未连接')
            self._ack(cmd_id, False, '串口未连接')
            return

        addr = self._get_addr() if self._get_addr else 1
        ok   = self._ctrl.open_lock(addr, lock)
        self._log(f'[远程] {label} 锁{lock:02d} {"成功" if ok else "失败"} | {self._ctrl.last_error}')
        self._ack(cmd_id, ok, '' if ok else self._ctrl.last_error)

    def _ack(self, cmd_id, ok, msg=''):
        try:
            _req.post(
                f'{self.api_base}/api/cabinet/ack',
                json={'id': cmd_id, 'ok': ok, 'msg': msg},
                timeout=3
            )
        except Exception:
            pass


# ─── 单个门锁卡片 ──────────────────────────────────────────────────────────────

class LockCard(BoxLayout):
    COLOR_LOCKED   = (0.85, 0.25, 0.25, 1)
    COLOR_OPEN     = (0.20, 0.75, 0.35, 1)
    COLOR_UNKNOWN  = (0.45, 0.45, 0.45, 1)

    def __init__(self, num: int, cb_open, **kw):
        super().__init__(orientation='vertical', padding=dp(4), spacing=dp(4), **kw)
        self.num = num

        self.title = Label(
            text=f'锁 {num:02d}',
            font_size=dp(16), bold=True,
            size_hint_y=0.30,
            color=(0.95, 0.95, 0.95, 1)
        )

        self.status = Label(
            text='─',
            font_size=dp(13),
            size_hint_y=0.25,
            color=self.COLOR_UNKNOWN
        )

        btn_open = Button(
            text='开锁', font_size=dp(16),
            size_hint_y=0.45,
            background_color=self.COLOR_OPEN,
            background_normal=''
        )
        btn_open.bind(on_press=lambda _: cb_open(num))

        self.add_widget(self.title)
        self.add_widget(self.status)
        self.add_widget(btn_open)

    def set_status(self, locked: bool | None):
        if locked is None:
            self.status.text = '─'
            self.status.color = self.COLOR_UNKNOWN
        elif locked:
            self.status.text = '已上锁'
            self.status.color = self.COLOR_LOCKED
        else:
            self.status.text = '已开锁'
            self.status.color = self.COLOR_OPEN


# ─── 主界面 ────────────────────────────────────────────────────────────────────

class MainLayout(BoxLayout):
    def __init__(self, **kw):
        super().__init__(orientation='vertical', padding=dp(6), spacing=dp(6), **kw)
        self.ctrl = LockController()
        self.cards: list[LockCard] = []
        self._build_top_bar()
        self._build_api_bar()
        self._build_action_bar()
        self._build_grid()
        self._build_log()
        self._poller = ApiPoller()
        self._poller.configure(
            ctrl=self.ctrl,
            get_addr=lambda: self._addr,
            log_fn=self._log,
            set_status=self._on_api_status,
        )
        self._poller.start()

    # ── 顶部连接栏 ────────────────────────────────────────────────────────────

    def _build_top_bar(self):
        bar = BoxLayout(
            size_hint_y=None, height=dp(54),
            spacing=dp(8), padding=(dp(4), dp(4))
        )

        self.inp_port = TextInput(
            text='/dev/ttyS1', hint_text='串口路径',
            multiline=False, font_size=dp(14),
            size_hint_x=0.28
        )

        self.spn_baud = Spinner(
            text='9600',
            values=['4800', '9600', '19200', '38400', '115200'],
            font_size=dp(14), size_hint_x=0.14
        )

        bar.add_widget(Label(text='端口:', size_hint_x=0.08, font_size=dp(14)))
        bar.add_widget(self.inp_port)
        bar.add_widget(Label(text='波特率:', size_hint_x=0.10, font_size=dp(14)))
        bar.add_widget(self.spn_baud)

        bar.add_widget(Label(text='板号:', size_hint_x=0.07, font_size=dp(14)))
        self.inp_addr = TextInput(
            text='1', multiline=False, input_filter='int',
            font_size=dp(14), size_hint_x=0.07
        )
        bar.add_widget(self.inp_addr)

        self.btn_conn = Button(
            text='连接', font_size=dp(15),
            size_hint_x=0.13,
            background_color=(0.2, 0.55, 0.95, 1),
            background_normal=''
        )
        self.btn_conn.bind(on_press=self._toggle_conn)
        bar.add_widget(self.btn_conn)

        self.lbl_conn = Label(
            text='未连接', font_size=dp(14),
            size_hint_x=0.13,
            color=(0.9, 0.3, 0.3, 1)
        )
        bar.add_widget(self.lbl_conn)

        self.add_widget(bar)

    # ── API 配置栏 ────────────────────────────────────────────────────────────

    def _build_api_bar(self):
        bar = BoxLayout(
            size_hint_y=None, height=dp(46),
            spacing=dp(8), padding=(dp(4), dp(2))
        )

        bar.add_widget(Label(text='API:', size_hint_x=0.07, font_size=dp(13)))
        self.inp_api = TextInput(
            text='http://192.168.1.100', hint_text='服务器地址',
            multiline=False, font_size=dp(13), size_hint_x=0.40
        )
        bar.add_widget(self.inp_api)

        bar.add_widget(Label(text='ID:', size_hint_x=0.05, font_size=dp(13)))
        self.inp_did = TextInput(
            text='cabinet_001', hint_text='设备ID',
            multiline=False, font_size=dp(13), size_hint_x=0.22
        )
        bar.add_widget(self.inp_did)

        btn_apply = Button(
            text='应用', font_size=dp(13),
            size_hint_x=0.10,
            background_color=(0.25, 0.45, 0.25, 1),
            background_normal=''
        )
        btn_apply.bind(on_press=self._apply_api_config)
        bar.add_widget(btn_apply)

        self.lbl_api = Label(
            text='●离线', font_size=dp(13),
            size_hint_x=0.16,
            color=(0.9, 0.3, 0.3, 1)
        )
        bar.add_widget(self.lbl_api)
        self.add_widget(bar)

    def _apply_api_config(self, *_):
        self._poller.api_base  = self.inp_api.text.strip().rstrip('/')
        self._poller.device_id = self.inp_did.text.strip()
        self._log(f'API已更新: {self._poller.api_base}  ID={self._poller.device_id}')

    def _on_api_status(self, online: bool):
        if online:
            self.lbl_api.text  = '●在线'
            self.lbl_api.color = (0.2, 0.85, 0.35, 1)
        else:
            self.lbl_api.text  = '●离线'
            self.lbl_api.color = (0.9, 0.3, 0.3, 1)

    # ── 全部操作栏 ────────────────────────────────────────────────────────────

    def _build_action_bar(self):
        bar = BoxLayout(
            size_hint_y=None, height=dp(48),
            spacing=dp(10), padding=(dp(4), dp(2))
        )

        btn_all_open = Button(
            text='全部开锁', font_size=dp(15),
            background_color=(0.20, 0.70, 0.35, 1),
            background_normal=''
        )
        btn_all_open.bind(on_press=lambda _: self._all_open())

        btn_refresh = Button(
            text='刷新状态', font_size=dp(15),
            background_color=(0.35, 0.35, 0.55, 1),
            background_normal=''
        )
        btn_refresh.bind(on_press=lambda _: self._refresh())

        btn_scan = Button(
            text='扫描串口', font_size=dp(15),
            background_color=(0.55, 0.35, 0.65, 1),
            background_normal=''
        )
        btn_scan.bind(on_press=lambda _: self._scan_ports())

        bar.add_widget(btn_all_open)
        bar.add_widget(btn_refresh)
        bar.add_widget(btn_scan)
        self.add_widget(bar)

    # ── 16 路锁网格 ───────────────────────────────────────────────────────────

    def _build_grid(self):
        sv = ScrollView()
        grid = GridLayout(
            cols=4, spacing=dp(8), padding=dp(4),
            size_hint_y=None,
            row_default_height=dp(115),
            row_force_default=True
        )
        grid.bind(minimum_height=grid.setter('height'))

        for i in range(1, 17):
            card = LockCard(i, self._open_lock)
            self.cards.append(card)
            grid.add_widget(card)

        sv.add_widget(grid)
        self.add_widget(sv)

    # ── 日志栏 ────────────────────────────────────────────────────────────────

    def _build_log(self):
        self.lbl_log = Label(
            text='就绪',
            size_hint_y=None, height=dp(30),
            font_size=dp(13),
            halign='left', valign='middle',
            color=(0.75, 0.75, 0.75, 1)
        )
        self.lbl_log.bind(size=self.lbl_log.setter('text_size'))
        self.add_widget(self.lbl_log)

        self.scan_result = Label(
            text='',
            size_hint_y=None, height=dp(0),
            font_size=dp(12),
            halign='left', valign='top',
            color=(0.95, 0.85, 0.40, 1)
        )
        self.scan_result.bind(size=self.scan_result.setter('text_size'))
        self.add_widget(self.scan_result)

    # ── 工具方法 ──────────────────────────────────────────────────────────────

    def _log(self, msg: str):
        Clock.schedule_once(lambda _: setattr(self.lbl_log, 'text', msg))

    @property
    def _addr(self) -> int:
        try:
            return max(0, int(self.inp_addr.text or '0'))
        except ValueError:
            return 0

    # ── 事件处理 ──────────────────────────────────────────────────────────────

    def _toggle_conn(self, *_):
        if self.ctrl.connected:
            self.ctrl.disconnect()
            self.btn_conn.text = '连接'
            self.btn_conn.background_color = (0.2, 0.55, 0.95, 1)
            self.lbl_conn.text = '未连接'
            self.lbl_conn.color = (0.9, 0.3, 0.3, 1)
            self._log('已断开串口')
        else:
            if not SERIAL_AVAILABLE:
                self._log('错误: pyserial 未安装')
                return
            port = self.inp_port.text.strip()
            baud = int(self.spn_baud.text)
            ok, msg = self.ctrl.connect(port, baud)
            if ok:
                self.btn_conn.text = '断开'
                self.btn_conn.background_color = (0.75, 0.38, 0.08, 1)
                self.lbl_conn.text = '已连接'
                self.lbl_conn.color = (0.2, 0.85, 0.35, 1)
                self._log(f'已连接 {port} @ {baud}')
            else:
                self._log(f'连接失败: {msg}')

    def _open_lock(self, num: int):
        if not self.ctrl.connected:
            self._log('未连接')
            return

        def _task():
            ok = self.ctrl.open_lock(self._addr, num)
            status = '成功' if ok else '失败'
            self._log(f'锁{num:02d} 开锁{status} | {self.ctrl.last_error}')

        threading.Thread(target=_task, daemon=True).start()

    def _all_open(self):
        if not self.ctrl.connected:
            self._log('未连接')
            return

        def _task():
            addr = self._addr
            for i in range(1, 17):
                self.ctrl.open_lock(addr, i)
                time.sleep(0.1)
            self._log('全部开锁指令已发送')

        threading.Thread(target=_task, daemon=True).start()

    def _refresh(self):
        if not self.ctrl.connected:
            self._log('未连接')
            return

        def _task():
            self._log('正在查询锁状态...')
            states = self.ctrl.query_status(self._addr)
            if states is None:
                self._log(f'状态查询无响应 | {self.ctrl.last_error}')
                return
            def _update(_):
                for card in self.cards:
                    locked = states.get(card.num)
                    card.set_status(locked)
            Clock.schedule_once(_update)
            self._log(f'状态已更新，获取到 {len(states)} 个锁 | {self.ctrl.last_error}')

        threading.Thread(target=_task, daemon=True).start()

    def _scan_ports(self):
        import glob
        self._log('正在扫描串口...')
        def _task():
            found = []
            patterns = ['/dev/ttyS*', '/dev/ttyHS*', '/dev/ttyUSB*',
                        '/dev/ttyACM*', '/dev/ttyMSM*', '/dev/ttyRS485*']
            for p in patterns:
                found.extend(sorted(glob.glob(p)))
            if found:
                text = '发现串口:\n' + '\n'.join(found)
                height = dp(20 + 20 * len(found))
            else:
                text = '未发现任何串口'
                height = dp(40)
            def _update(_):
                self.scan_result.text = text
                self.scan_result.height = height
            Clock.schedule_once(_update)
            self._log(f'扫描完成，发现 {len(found)} 个串口')
        threading.Thread(target=_task, daemon=True).start()


# ─── App 入口 ──────────────────────────────────────────────────────────────────

class DoorLockApp(App):
    def build(self):
        self.title = '16路门锁控制'
        Window.clearcolor = (0.12, 0.12, 0.15, 1)
        self._layout = MainLayout()
        return self._layout

    def on_stop(self):
        self._layout._poller.stop()


if __name__ == '__main__':
    DoorLockApp().run()
