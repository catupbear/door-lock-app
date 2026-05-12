[app]
title = 16路门锁控制
package.name = doorlock
package.domain = org.doorlock
source.dir = .
source.include_exts = py,ttf,ttc,otf
source.include_patterns = chinese_font.ttf
version = 1.0
requirements = python3==3.11.11,hostpython3==3.11.11,kivy==2.3.0,pyserial,requests

orientation = landscape
fullscreen = 0

android.api = 31
android.minapi = 21
android.ndk = 25
android.archs = armeabi-v7a

# 串口访问权限
android.permissions = READ_EXTERNAL_STORAGE, WRITE_EXTERNAL_STORAGE, INTERNET


[buildozer]
log_level = 2
warn_on_root = 0
