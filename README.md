# Delta Dual Trader — Android project

This is the Android/Kivy conversion project based on the latest desktop GUI.

## Included
- Main account + multiple enabled sub-accounts
- BTC option expiry selection (keeps all discovered expiries and defaults to nearest upcoming)
- CALL / PUT
- BUY / SELL
- AUTO quantity formula: `((208 / (MARK PRICE - 10) * 1000) * 2.5)` rounded to whole contracts
- MANUAL quantity
- Mark-price option filter: > $184 and closest to $200
- Section 4 live positions with Mark Price and UPNL
- Close individual position / Close all positions
- SL by Mark Price
- Target trigger + optional target limit
- One-time scheduled order in 24-hour HH:MM:SS
- Mobile scrollable UI

## Important
This app can send REAL orders. Use only with API keys you intend to trade with, and test carefully.

The Android version does not use Windows DPAPI. Credentials are entered into the app session and are not stored by this project.

## Build APK
The included `buildozer.spec` is ready for a Linux/WSL build environment with Buildozer and Android SDK/NDK. A GitHub Actions workflow is also included for building the APK in the cloud.
