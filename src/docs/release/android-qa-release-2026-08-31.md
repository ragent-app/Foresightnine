# Android QA release evidence — 2026-08-31

## Scope

This is a local QA release for the current source commit `239d06d`. It is not
a Google Play upload and it is not signed with the production release key.
The build used the existing Expo/RN project, generated workspace `dist`
outputs, CMake 3.30.5, and the x86_64 ABI required by the local API 36 Play
emulator.

## Reproduction

Run from a short same-drive clone of `D:\Foresightnine\src` on Windows. The
short clone is required because the repository's pnpm virtual-store paths make
the Windows CMake/Ninja manifest unstable.

```powershell
corepack pnpm install --frozen-lockfile --force --config.node-linker=hoisted
node node_modules/typescript/bin/tsc -b packages/contracts/tsconfig.json --pretty false --force
node node_modules/typescript/bin/tsc -b packages/domain/tsconfig.json --pretty false --force
node node_modules/typescript/bin/tsc -b packages/fm-engine/tsconfig.json --pretty false --force
node node_modules/typescript/bin/tsc -b packages/market-data/tsconfig.json --pretty false --force
node node_modules/typescript/bin/tsc -b packages/api-client/tsconfig.json --pretty false --force
node node_modules/typescript/bin/tsc -b packages/asset-registry/tsconfig.json --pretty false --force

Set-Location apps/mobile/android
./gradlew.bat :app:generateAutolinkingPackageList :app:generateAutolinkingNewArchitectureFiles `
  --no-daemon --rerun-tasks --console plain
./gradlew.bat :app:assembleRelease :app:bundleRelease `
  --no-daemon --console plain `
  --project-prop android.cmakeVersion=3.30.5 `
  --project-prop reactNativeArchitectures=x86_64 `
  -x :app:verifyForesightNineReleaseSigning
```

The final `-x` is only for producing an unsigned QA candidate. A production
build must omit it and provide all four `FORESIGHTNINE_ANDROID_KEY*` values.

## Artifacts and hashes

Artifacts are retained locally under
`src/artifacts/android-qa/2026-08-31/` and are intentionally ignored by Git.

| Artifact | SHA-256 | Status |
| --- | --- | --- |
| `foresightnine-0.1.0-x86_64-release-unsigned.apk` | `824C8209123DBA5BE2E714F4A1D623C55DEB84F4EB7E94664A9F2C47AF461F74` | unsigned build output |
| `foresightnine-0.1.0-x86_64-release-unsigned.aab` | `B3F6789FB1B6E9582A2789DB72140A92414E510C44714FABE04C221B301DAC1C` | QA bundle before signing |
| `foresightnine-0.1.0-x86_64-release-qa-signed.apk` | `FDAFC883AA88DAD08FB24D34324B92D1CD7D7884CD19F16D56E664CC7C5EE769` | signed with disposable local QA key; emulator only |
| `foresightnine-0.1.0-x86_64-release-qa-signed.aab` | `234D60E996BAB131BC5D8F35DCCE8DAC066EE120B029B5BBABAEF9D85A6AB739` | JAR-signed with disposable local QA key; not Play release |

The APK was verified with APK Signature Scheme v2/v3. The AAB was verified by
`jarsigner`; its certificate is intentionally self-signed for local QA.

## Emulator evidence

- Target: `emulator-5554`, `sdk_gphone64_x86_64`, API 36 Play image.
- Package installed after removing the prior debug-signed package to avoid
  signature mismatch.
- Cold start reached `com.foresightnine.mobile/.MainActivity`.
- Tapping the language control changed the visible UI to English.
- Tapping `Start the challenge` reached the Challenge screen and displayed the
  server-backed structured response from the staging API.
- No `FATAL EXCEPTION` or `ReactNativeJS:E` entries were present in the
  filtered post-flow logcat output.
- Screenshots reviewed with `view_image`:
  - `android-release-cold-start.png`
  - `android-release-language-toggle.png`
  - `android-release-challenge.png`

## Remaining release gates

- No physical Android device was connected; emulator evidence is not a
  substitute for the required physical-device run.
- HTTPS App Links still require the production domain `assetlinks.json` and a
  production certificate fingerprint.
- A production keystore must be injected through protected CI/server secrets;
  the disposable QA keystore must never be reused or uploaded.
- The QA build used x86_64 only. Play production should build the approved ABI
  set and use Play App Signing/upload-key policy.
