"""
16路门锁控制系统
硬件: 杭州三郎 16门锁板
协议: RS485 / Modbus-RTU
"""
from __future__ import annotations
import struct
import threading
import time

try:
    import serial
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False

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


# ─── Modbus 帧构造 ─────────────────────────────────────────────────────────────

def crc16_modbus(data: bytes) -> bytes:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return struct.pack('<H', crc)  # 低字节在前


def build_open_cmd(board_addr: int, lock_num: int) -> bytes:
    """FC05 开锁：FF 00"""
    raw = bytes([board_addr, 0x05, 0x00, lock_num, 0xFF, 0x00])
    return raw + crc16_modbus(raw)


def build_close_cmd(board_addr: int, lock_num: int) -> bytes:
    """FC05 关锁：00 00"""
    raw = bytes([board_addr, 0x05, 0x00, lock_num, 0x00, 0x00])
    return raw + crc16_modbus(raw)


def build_read_control_cmd(board_addr: int) -> bytes:
    """FC01 读16路控制输出状态"""
    raw = bytes([board_addr, 0x01, 0x00, 0x01, 0x00, 0x0C])
    return raw + crc16_modbus(raw)


def build_read_feedback_cmd(board_addr: int) -> bytes:
    """FC02 读16路反馈信号（物理锁状态）"""
    raw = bytes([board_addr, 0x02, 0x00, 0x01, 0x00, 0x0C])
    return raw + crc16_modbus(raw)


def parse_16_bits(resp: bytes, fc: int):
    """
    解析 FC01/FC02 返回的 16 路位状态。
    返回长度为16的列表，states[0]=锁1，states[15]=锁16。
    第一字节 bit0=锁1…bit7=锁8；第二字节 bit0=锁9…bit7=锁16。
    """
    if not resp or len(resp) < 7:
        return None
    if resp[1] != fc or resp[2] != 2:
        return None
    d1, d2 = resp[3], resp[4]
    return [bool(d1 & (1 << i)) for i in range(8)] + \
           [bool(d2 & (1 << i)) for i in range(8)]


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

    def _send(self, cmd: bytes) -> bytes | None:
        if not self.connected:
            return None
        with self._lock:
            try:
                self._ser.reset_input_buffer()
                self._ser.write(cmd)
                self._ser.flush()
                time.sleep(0.3)
                resp = self._ser.read(32)  # 不用in_waiting，直接读（Android兼容）
                self.last_error = f'发:{cmd.hex()} 收:{resp.hex() if resp else "空"}'
                return resp or None
            except Exception as e:
                self.last_error = str(e)
                return None

    def open_lock(self, addr: int, lock_num: int) -> bool:
        resp = self._send(build_open_cmd(addr, lock_num))
        return bool(resp and len(resp) >= 6)

    def close_lock(self, addr: int, lock_num: int) -> bool:
        resp = self._send(build_close_cmd(addr, lock_num))
        return bool(resp and len(resp) >= 6)

    def read_feedback(self, addr: int):
        resp = self._send(build_read_feedback_cmd(addr))
        return parse_16_bits(resp, 0x02)

    def read_control(self, addr: int):
        resp = self._send(build_read_control_cmd(addr))
        return parse_16_bits(resp, 0x01)


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
        self._build_action_bar()
        self._build_grid()
        self._build_log()

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
            return max(1, int(self.inp_addr.text or '1'))
        except ValueError:
            return 1

    def _update_cards(self, states):
        for i, card in enumerate(self.cards):
            card.set_status(states[i] if i < len(states) else None)

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
                self._refresh()
            else:
                self._log(f'连接失败: {msg}')

    def _open_lock(self, num: int):
        if not self.ctrl.connected:
            self._log('未连接')
            return

        def _task():
            ok = self.ctrl.open_lock(self._addr, num)
            self._log(f'锁{num:02d} 开锁{"成功" if ok else "失败"}')
            if ok:
                time.sleep(0.15)
                self._do_refresh()

        threading.Thread(target=_task, daemon=True).start()

    def _all_open(self):
        if not self.ctrl.connected:
            self._log('未连接')
            return

        def _task():
            addr = self._addr
            for i in range(1, 17):
                self.ctrl.open_lock(addr, i)
                time.sleep(0.04)
            self._log('全部开锁指令已发送')
            time.sleep(0.3)
            self._do_refresh()

        threading.Thread(target=_task, daemon=True).start()

    def _refresh(self):
        if not self.ctrl.connected:
            self._log('未连接，无法刷新')
            return
        threading.Thread(target=self._do_refresh, daemon=True).start()

    def _do_refresh(self):
        states = self.ctrl.read_feedback(self._addr)
        if states:
            Clock.schedule_once(lambda _: self._update_cards(states))
            self._log('状态已刷新')
        else:
            self._log(f'读取失败 | {self.ctrl.last_error}')

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
        return MainLayout()


if __name__ == '__main__':
    DoorLockApp().run()
