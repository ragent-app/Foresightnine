# Phase 9 Android Metro smoke evidence

## Boundary

This is a debug/dev-client emulator smoke record. It deliberately describes the environment that was actually used: Expo Metro was running and the emulator had an `adb reverse tcp:8081` mapping. It is not evidence for an independently self-contained APK, a signed release artifact, or Android public distribution.

## Environment and result

- Device: `emulator-5554`, API 36 Google Play.
- APK: `apps/mobile/android/app/build/outputs/apk/debug/app-debug.apk`.
- Metro status during capture: `packager-status:running` from `http://127.0.0.1:8081/status`.
- Reverse mapping during capture: `host-16 tcp:8081 tcp:8081`.
- Package flags: `DEBUGGABLE HAS_CODE ALLOW_CLEAR_USER_DATA ALLOW_BACKUP`.
- Launch: `com.foresightnine.mobile/.MainActivity` returned `Status: ok`, `LaunchState: COLD`.
- Current visual/UI-tree evidence: `phase9-current-home.png` and `phase9-current-home.xml`; the screenshot was inspected with `view_image`.
- Filtered log tail contained no `FATAL EXCEPTION`, `ReactNativeJS`, or `RedBox` error. The unrelated AndroidRuntime VM-shutdown debug line was not treated as a fatal application error.

## Reproduction

From `D:\Foresightnine\src`:

```powershell
$adb = 'C:\Users\accel\AppData\Local\Android\Sdk\platform-tools\adb.exe'
& $adb -s emulator-5554 reverse tcp:8081 tcp:8081
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8081/status
& $adb -s emulator-5554 install -r apps/mobile/android/app/build/outputs/apk/debug/app-debug.apk
& $adb -s emulator-5554 shell am force-stop com.foresightnine.mobile
& $adb -s emulator-5554 shell am start -W -n com.foresightnine.mobile/.MainActivity
& $adb -s emulator-5554 shell uiautomator dump /sdcard/phase9-current-home.xml
& $adb -s emulator-5554 pull /sdcard/phase9-current-home.xml docs/qa/android/phase9-current-home.xml
```

An independent/no-Metro APK test requires a non-debuggable bundle and owner-controlled release signing; those are not silently substituted by this QA record.
