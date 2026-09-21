[app]
title = Delta Dual Trader Mobile
package.name = deltadualtrader
package.domain = com.deltadualtrader
source.dir = .
source.include_exts = py,kv,png,jpg,json
version = 1.0.0
requirements = python3,kivy==2.3.1,requests,websocket-client
orientation = portrait
fullscreen = 0
android.permissions = INTERNET,ACCESS_NETWORK_STATE
android.api = 35
android.minapi = 23
android.archs = arm64-v8a,armeabi-v7a

[buildozer]
log_level = 2
warn_on_root = 1
