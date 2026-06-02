#!/usr/bin/env python3
"""
热更新部署脚本（SSH直传，无需开放额外端口）
用法: python3 deploy.py <版本号>
示例: python3 deploy.py HU-20260513
"""
import hashlib
import json
import os
import sys

SERVER_HOST = '43.136.92.192'
SERVER_USER = 'root'
SERVER_PASS = 'B1Y#6Undefgt'
SERVER_DIR  = '/root/door_lock_server'

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'main.py')


def generate_hotupdate(version: str) -> bytes:
    with open(SRC, 'r', encoding='utf-8') as f:
        content = f.read()
    marker = '\n_HOTUPDATE_VERSION = None  # 热更新文件会将此值覆盖为版本字符串\n'
    idx = content.index(marker) + len(marker)
    header = f'''"""
钥匙柜控制系统 v3.0
协议: 老铁 5字节帧协议
"""
# ─── 热更新版本 {version} ──────────────────────────────────────────────────
import os as _os, shutil as _shutil, time as _time

_APP_DATA_DIR = '/data/data/org.doorlock.doorlock/files'
_BASE_DIR     = _APP_DATA_DIR if _os.path.isdir(_APP_DATA_DIR) else _os.path.expanduser('~')
_INTERNAL  = _os.path.join(_BASE_DIR, 'door_lock_main.py')
_LOG_PATH  = _os.path.join(_BASE_DIR, 'door_lock_loader.log')
_loader_messages = []
_HOTUPDATE_VERSION = "{version}"

def _loader_log(msg):
    _ts = _time.strftime('%H:%M:%S')
    _line = f'[{{_ts}}] {{msg}}'
    _loader_messages.append(_line)
    try:
        with open(_LOG_PATH, 'a', encoding='utf-8') as _f:
            _f.write(_line + '\\n')
    except Exception:
        pass
'''
    result = (header + content[idx:]).encode('utf-8')
    print(f'生成热更新文件: {len(result.splitlines())} 行，MD5: {hashlib.md5(result).hexdigest()}')
    return result


def deploy(version: str):
    try:
        import paramiko
    except ImportError:
        print('请先安装 paramiko: pip3 install paramiko')
        sys.exit(1)

    script = generate_hotupdate(version)
    md5 = hashlib.md5(script).hexdigest()
    meta = json.dumps({'version': version, 'md5': md5, 'size': len(script)},
                      ensure_ascii=False)

    print(f'连接服务器 {SERVER_HOST} ...')
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(SERVER_HOST, username=SERVER_USER, password=SERVER_PASS, timeout=15)

    sftp = ssh.open_sftp()
    with sftp.open(f'{SERVER_DIR}/door_lock_main.py', 'wb') as f:
        f.write(script)
    with sftp.open(f'{SERVER_DIR}/door_lock_meta.json', 'w') as f:
        f.write(meta)
    sftp.close()
    ssh.close()

    print(f'部署完成！版本: {version}')
    print(f'设备进管理界面点「检查更新」即可自动更新')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    deploy(sys.argv[1])
