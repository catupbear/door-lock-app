[app]
title = 16路门锁控制
package.name = doorlock
package.domain = org.doorlock
source.dir = .
source.include_exts = py,ttf,ttc,otf
source.include_patterns = chinese_font.ttf
version = 1.0
requirements = python3==3.11.11,hostpython3==3.11.11,kivy==2.3.0,pyserial

orientation = landscape
fullscreen = 0

android.api = 29
android.minapi = 21
android.ndk = 27
android.archs = arm64-v8a

# 串口访问权限
android.permissions = READ_EXTERNAL_STORAGE, WRITE_EXTERNAL_STORAGE


[buildozer]
log_level = 2
warn_on_root = 0
