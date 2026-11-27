# ForesightNine Android client

The [product direction](../../docs/product/fusion-finance-ai-macro-plan-2026-11-11.md) covers ongoing company comparisons and personal portfolio decisions. This Expo SDK 57 Android client implements the earlier simulation workflow from viewpoint → challenge → evidence →
worldline → local FM → portfolio → privacy-safe archive loop.

The app deliberately keeps provider credentials, market retrieval, broker, ordering,
and execution out of the device. It can query the deployer-configured public
ForesightNine project-search route for bounded, unverified candidates; if that route
is absent or unavailable it falls back to browser share/paste of a
`SearchEvidenceBundle`. Candidate URLs remain inert display data and are never
opened or fetched by the app. Labels such as “mock / unvalidated” and “API search
unavailable” are part of the product state, not placeholder copy.

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
