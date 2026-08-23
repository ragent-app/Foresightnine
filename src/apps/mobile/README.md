# ForesightNine Android client

This is the Expo SDK 57 Android MVP for the viewpoint → challenge → evidence →
worldline → local FM → portfolio → privacy-safe archive loop.

The app is deliberately mock-only: it has no provider, search, market, API key,
broker, ordering, or execution integration. Labels such as “mock / unvalidated”
and “API search unavailable” are part of the product state, not placeholder copy.

## Commands

From `D:\Foresightnine\src`:

```powershell
corepack pnpm --filter @foresightnine/mobile typecheck
corepack pnpm --filter @foresightnine/mobile test
corepack pnpm --filter @foresightnine/mobile export:android
```

For a native Android build, install Android Studio, Android SDK 36, a local JDK
compatible with Expo SDK 57, and an emulator or device, then run:

```powershell
corepack pnpm --filter @foresightnine/mobile android
```

The Node tests intentionally do not claim native AES execution. They verify the
archive envelope and restore/privacy decision path; `src/adapters/native-archive.ts`
is the Android adapter that uses Expo AES-GCM, SecureStore, and FileSystem.
