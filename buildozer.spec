[app]
title = 车物家锁控
package.name = doorlock
package.domain = org.doorlock
source.dir = .
source.include_exts = py,ttf,ttc,otf
source.include_patterns = chinese_font.ttf, assets/offline_poster.png
icon.filename = icon.png
presplash.filename = presplash.png
presplash.color = #ffffff
version = 1.0
requirements = python3==3.11.11,hostpython3==3.11.11,kivy==2.3.0,pyserial,requests

orientation = landscape
fullscreen = 1

android.api = 31
android.minapi = 21
android.ndk = 25
android.archs = armeabi-v7a

# 设为系统桌面（Launcher），开机自启并作为默认 Home 应用
android.launcher = 1

# 权限说明:
#   READ/WRITE_EXTERNAL_STORAGE  - USB热更新读取 /sdcard/ 脚本
#   INTERNET                     - HTTP API访问
#   ACCESS_WIFI_STATE            - 读取WiFi状态
#   CHANGE_WIFI_STATE            - 打开WiFi设置
#   ACCESS_NETWORK_STATE         - 读取网络状态（以太网/WiFi）
#   CHANGE_NETWORK_STATE         - 以太网设置
#   REBOOT                       - 重启整个设备（需系统签名或root）
android.permissions = READ_EXTERNAL_STORAGE, WRITE_EXTERNAL_STORAGE, INTERNET, ACCESS_WIFI_STATE, CHANGE_WIFI_STATE, ACCESS_NETWORK_STATE, CHANGE_NETWORK_STATE, REBOOT, RECEIVE_BOOT_COMPLETED

# Android 9+ 默认禁止 HTTP 明文流量，必须显式允许；Android 10 需要 legacyExternalStorage 才能访问 /sdcard/
android.manifest_application_fields = android:usesCleartextTraffic="true" android:requestLegacyExternalStorage="true"
android.add_src = src
android.extra_manifest_application_arguments = <receiver android:name=".BootReceiver" android:exported="true"><intent-filter><action android:name="android.intent.action.BOOT_COMPLETED"/><category android:name="android.intent.category.DEFAULT"/></intent-filter></receiver>


[buildozer]
log_level = 2
warn_on_root = 0
