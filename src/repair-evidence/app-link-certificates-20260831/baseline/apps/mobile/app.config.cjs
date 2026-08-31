/**
 * Build-time Android App Link configuration. Production OAuth must use an
 * allowlisted HTTPS callback whose domain publishes assetlinks.json. The
 * custom foresightnine:// scheme remains available for local/QA builds.
 */
module.exports = ({ config }) => {
  const raw = process.env.EXPO_PUBLIC_FORESIGHTNINE_OAUTH_RETURN_URI;
  const production = process.env.FORESIGHTNINE_ENV?.trim().toLowerCase() === "production" || process.env.NODE_ENV === "production";
  let intentFilters = config.android?.intentFilters ?? [];
  let callback = null;
  if (typeof raw === "string" && raw.trim()) {
    try {
      const url = new URL(raw.trim());
      if (url.protocol === "https:" && url.hostname && !url.port && !url.username && !url.password && !url.search && !url.hash && url.pathname) {
        callback = url;
      }
    } catch {
      // Production throws below; local builds retain only the configured scheme.
    }
  }

  if (production) {
    const appLinkHost = process.env.EXPO_PUBLIC_FORESIGHTNINE_OAUTH_APP_LINK_HOST?.trim();
    const appLinkPackage = process.env.EXPO_PUBLIC_FORESIGHTNINE_OAUTH_APP_LINK_PACKAGE?.trim();
    const certificateFingerprint = process.env.EXPO_PUBLIC_FORESIGHTNINE_OAUTH_APP_LINK_SHA256_CERT_FINGERPRINT?.trim();
    const appLinkVerified = process.env.EXPO_PUBLIC_FORESIGHTNINE_OAUTH_APP_LINK_VERIFIED?.trim().toLowerCase() === "true";
    const fingerprintPattern = /^(?:[0-9a-f]{2}:){31}[0-9a-f]{2}$/i;
    if (!callback || !appLinkVerified || appLinkHost !== callback.hostname || appLinkPackage !== "com.foresightnine.mobile" || !certificateFingerprint || !fingerprintPattern.test(certificateFingerprint)) {
      throw new Error("Production Android OAuth requires an exact HTTPS callback and verified App Link inputs.");
    }
    intentFilters = intentFilters.filter((filter) => !filter.data?.some((entry) => entry.scheme === "foresightnine"));
  }

  if (callback) {
    intentFilters = [...intentFilters, {
      action: "VIEW",
      autoVerify: true,
      category: ["BROWSABLE", "DEFAULT"],
      data: [{ scheme: "https", host: callback.hostname, pathPrefix: callback.pathname }],
    }];
  }

  return {
    ...config,
    // A production HTTPS App Link must not also advertise the interceptable
    // custom scheme. Local emulator builds may explicitly retain it.
    scheme: callback ? undefined : config.scheme,
    android: { ...config.android, intentFilters },
  };
};
