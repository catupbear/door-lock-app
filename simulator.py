"""
锁板串口模拟器 — 用于在没有硬件的情况下测试 App。
运行后会打印出虚拟串口路径，把它填入 App 的端口框再点连接即可。
"""
import os
import pty
import struct
import threading
import time


def crc16_modbus(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return struct.pack('<H', crc)


def build_response(cmd: bytes, lock_states: list[bool]) -> bytes | None:
    """根据收到的 Modbus 命令构造回复帧。lock_states[i]=True 表示锁i已开锁。"""
    if len(cmd) < 6:
        return None

    addr, fc = cmd[0], cmd[1]

    if fc == 0x01:
        # FC01：读控制输出状态（bit=1 表示正在控制开锁）
        d1 = sum((1 << i) for i in range(8) if lock_states[i])
        d2 = sum((1 << i) for i in range(8) if lock_states[i + 8])
        r = bytes([addr, 0x01, 0x02, d1, d2])
        return r + crc16_modbus(r)

    elif fc == 0x02:
        # FC02：读反馈状态（bit=1 表示已上锁，bit=0 表示已开锁）
        d1 = sum((1 << i) for i in range(8) if not lock_states[i])
        d2 = sum((1 << i) for i in range(8) if not lock_states[i + 8])
        r = bytes([addr, 0x02, 0x02, d1, d2])
        return r + crc16_modbus(r)

    elif fc == 0x05:
        # FC05：写单个锁，FF00=开锁，0000=关锁
        lock_num = cmd[3]          # 1-16
        value    = cmd[4]          # 0xFF or 0x00
        if 1 <= lock_num <= 16:
            lock_states[lock_num - 1] = (value == 0xFF)
            action = '开锁' if value == 0xFF else '关锁'
            print(f'  → 锁 {lock_num:02d} {action}，当前状态: {["关闭","开启"][lock_states[lock_num-1]]}')
        r = cmd[:6]
        return r + crc16_modbus(r)

    return None


def run():
    # 创建虚拟串口对（master/slave）
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)

    print('=' * 50)
    print('锁板模拟器已启动')
    print(f'虚拟串口路径: {slave_path}')
    print('请将上面的路径填入 App 端口框，然后点【连接】')
    print('=' * 50)
    print('等待命令...\n')

    lock_states = [False] * 16   # 初始全部关锁

    buf = b''
    while True:
        try:
            chunk = os.read(master_fd, 256)
            buf += chunk

            # 所有 Modbus 请求帧固定 8 字节
            while len(buf) >= 8:
                frame = buf[:8]
                resp = build_response(frame, lock_states)
                if resp:
                    print(f'收到: {frame.hex(" ").upper()}')
                    print(f'回复: {resp.hex(" ").upper()}\n')
                    time.sleep(0.05)
                    os.write(master_fd, resp)
                buf = buf[8:]

        except OSError:
            break
        except KeyboardInterrupt:
            print('\n模拟器已停止')
            break


if __name__ == '__main__':
    run()
