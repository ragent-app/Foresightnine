/**
 * Build-time Android App Link configuration. Production OAuth must use an
 * allowlisted HTTPS callback whose domain publishes assetlinks.json. The
 * custom foresightnine:// scheme remains available only when explicitly
 * selected for local emulator development.
 */
module.exports = ({ config }) => {
  const raw = process.env.EXPO_PUBLIC_FORESIGHTNINE_OAUTH_RETURN_URI;
  let intentFilters = config.android?.intentFilters ?? [];
  let verifiedHttpsCallback = false;
  if (typeof raw === "string" && raw.trim()) {
    try {
      const url = new URL(raw.trim());
      if (url.protocol === "https:" && url.hostname && !url.username && !url.password && !url.hash) {
        verifiedHttpsCallback = true;
        intentFilters = [...intentFilters, {
          action: "VIEW",
          autoVerify: true,
          category: ["BROWSABLE", "DEFAULT"],
          data: [{ scheme: "https", host: url.hostname, pathPrefix: url.pathname || "/" }],
        }];
      }
    } catch {
      // The runtime client fails closed; no intent filter is generated for a
      // malformed callback URI.
    }
  }
  return {
    ...config,
    // A production HTTPS App Link must not also advertise the interceptable
    // custom scheme. Local emulator builds may explicitly retain it.
    scheme: verifiedHttpsCallback ? undefined : config.scheme,
    android: { ...config.android, intentFilters },
  };
};
