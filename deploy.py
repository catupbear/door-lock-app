#!/usr/bin/env python3
"""
热更新部署脚本
用法: python3 deploy.py <版本号>
示例: python3 deploy.py HU-20260513
"""
import hashlib
import os
import sys
import subprocess

SERVER   = 'http://keyapi.wuhuxiche.com:5000'
TOKEN    = 'dl_admin_2026'   # 与服务器 UPLOAD_TOKEN 保持一致
SRC      = os.path.join(os.path.dirname(__file__), 'main.py')
OUT      = '/tmp/door_lock_main_hu.py'

def generate_hotupdate(version: str):
    """从 main.py 生成无加载器的热更新文件"""
    with open(SRC, 'r', encoding='utf-8') as f:
        content = f.read()
    marker = '\n_HOTUPDATE_VERSION = None  # 热更新文件会将此值覆盖为版本字符串\n'
    idx = content.index(marker) + len(marker)
    header = f'''"""
钥匙柜控制系统 v3.0
协议: 老铁 5字节帧协议
"""
# ─── 热更新版本 {version} ──────────────────────────────────────────────────
import os as _os, shutil as _shutil

_INTERNAL  = _os.path.join(_os.path.expanduser('~'), 'door_lock_main.py')
_LOG_PATH  = _os.path.join(_os.path.expanduser('~'), 'door_lock_loader.log')
_loader_messages = []
_HOTUPDATE_VERSION = "{version}"
'''
    result = header + content[idx:]
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write(result)
    md5 = hashlib.md5(result.encode()).hexdigest()
    print(f'生成热更新文件: {len(result.splitlines())} 行，MD5: {md5}')
    return OUT

def upload(version: str, filepath: str):
    """上传到服务器"""
    print(f'上传到 {SERVER} ...')
    cmd = [
        'curl', '-sf',
        '-F', f'file=@{filepath}',
        '-F', f'version={version}',
        '-H', f'X-Token: {TOKEN}',
        f'{SERVER}/api/admin/upload-script',
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f'上传失败: {result.stderr}')
        sys.exit(1)
    print(f'服务器响应: {result.stdout}')
    print(f'\n完成！设备点「检查更新」即可拉取 {version}')

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    version = sys.argv[1]
    fp = generate_hotupdate(version)
    upload(version, fp)
