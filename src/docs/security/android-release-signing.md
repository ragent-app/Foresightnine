# Android release signing boundary

The committed Android project has two intentionally separate paths:

- `assembleDebug` is the emulator/QA path and uses the standard debug certificate. Its APK is never a public-distribution artifact.
- `assembleRelease` is fail-closed. It refuses to run until a release signing configuration is injected for that one process.

No keystore, key password, alias password, or signing configuration file belongs in the repository. The generated `debug.keystore` and all `*.keystore` files are ignored.

## Required release environment

Inject all of these outside source control, ideally from the CI secret store:

```text
FORESIGHTNINE_ANDROID_KEYSTORE_PATH
FORESIGHTNINE_ANDROID_KEYSTORE_PASSWORD
FORESIGHTNINE_ANDROID_KEY_ALIAS
FORESIGHTNINE_ANDROID_KEY_PASSWORD
```

The keystore path must identify a pre-provisioned owner-controlled upload/release key. The build rejects both a partial configuration and an absent configuration rather than falling back to the debug key.

## Commands

```powershell
# Emulator / technical QA only
$env:JAVA_HOME = 'C:\Program Files\Android\Android Studio\jbr'
$env:ANDROID_HOME = 'C:\Users\accel\AppData\Local\Android\Sdk'
$env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
$env:NODE_ENV = 'production'
Set-Location D:\Foresightnine\src\apps\mobile\android
.\gradlew.bat assembleDebug --init-script ..\..\..\scripts\android-short-cmake.init.gradle '-Dforesightnine.cxx.root=C:/fn-cxx'

# A release build succeeds only after the four environment values are injected.
.\gradlew.bat assembleRelease --init-script ..\..\..\scripts\android-short-cmake.init.gradle '-Dforesightnine.cxx.root=C:/fn-cxx'
```

Google Play signing, Play Console enrollment, Billing/Subscribing test tracks, data-license approval, and legal approval remain external public-release gates.
