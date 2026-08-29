const { AndroidConfig, withAndroidManifest } = require('@expo/config-plugins');

const GENERATED_TAG = 'expo-sharing-intent-filters';
const MULTIPLE_ACTION = 'android.intent.action.SEND_MULTIPLE';

function actionName(action) {
  return action && action.$ && action.$['android:name'];
}

function isEmptyGeneratedMultipleShareFilter(intentFilter) {
  const generated = intentFilter && intentFilter.$ && intentFilter.$[GENERATED_TAG] === 'true';
  const actions = Array.isArray(intentFilter && intentFilter.action) ? intentFilter.action : [];
  const isMultiple = actions.some((action) => actionName(action) === MULTIPLE_ACTION);
  const data = intentFilter && intentFilter.data;
  const hasData = Array.isArray(data) ? data.length > 0 : Boolean(data);
  return generated && isMultiple && !hasData;
}

function removeEmptyGeneratedMultipleShareFilters(manifest) {
  const application = manifest && manifest.application;
  const mainActivity = application && application[0]
    ? AndroidConfig.Manifest.getMainActivityOrThrow({ manifest })
    : null;
  if (!mainActivity || !Array.isArray(mainActivity['intent-filter'])) {
    return manifest;
  }
  mainActivity['intent-filter'] = mainActivity['intent-filter'].filter(
    (intentFilter) => !isEmptyGeneratedMultipleShareFilter(intentFilter),
  );
  return manifest;
}

function withSingleShareIntent(config) {
  return withAndroidManifest(config, (modConfig) => {
    removeEmptyGeneratedMultipleShareFilters(modConfig.modResults.manifest);
    return modConfig;
  });
}

module.exports = withSingleShareIntent;
module.exports.isEmptyGeneratedMultipleShareFilter = isEmptyGeneratedMultipleShareFilter;
module.exports.removeEmptyGeneratedMultipleShareFilters = removeEmptyGeneratedMultipleShareFilters;
