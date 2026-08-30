# Android OAuth App Link deployment gate

The mobile OAuth client is now wired to the server redirect route and stores only the exchanged local session in `expo-secure-store`. The app does not bundle provider secrets or put access tokens in URLs.

Production must use an HTTPS App Link, for example:

```text
https://<public-domain>/oauth/mobile-callback
```

The same exact URI must be present in the API `FORESIGHTNINE_OAUTH_RETURN_URI_ALLOWLIST` and in the mobile build variable `EXPO_PUBLIC_FORESIGHTNINE_OAUTH_RETURN_URI`. The mobile build generates an `autoVerify` Android intent filter from that URI. The public domain must serve `/.well-known/assetlinks.json` containing the release package name `com.foresightnine.mobile` and the SHA-256 fingerprint of the release signing certificate.

The `foresightnine://oauth/complete` URI is retained for local emulator experiments only. It is not an acceptable public production callback because custom schemes can be claimed by another installed Android application. When an HTTPS callback is supplied to the build, the app config removes the custom scheme from that production manifest; a release build must fail the release checklist unless the HTTPS URI and matching asset-link fingerprint have been verified.

Required verification sequence:

1. Confirm the HTTPS callback is in the API allowlist and provider console.
2. Publish and fetch `/.well-known/assetlinks.json` over HTTPS without redirects.
3. Build with the release signing key and capture its SHA-256 fingerprint.
4. Install the signed APK, verify the HTTPS link resolves to the app, and complete provider success, cancellation, replay, logout, and account deletion tests.
