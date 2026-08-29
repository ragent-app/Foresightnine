import { describe, expect, it } from 'vitest';
import plugin from '../plugins/with-single-share-intent.cjs';

const generatedEmptyMultiple = {
  $: { 'expo-sharing-intent-filters': 'true' },
  action: [{ $: { 'android:name': 'android.intent.action.SEND_MULTIPLE' } }],
  category: [{ $: { 'android:name': 'android.intent.category.DEFAULT' } }],
};

const generatedSingle = {
  $: { 'expo-sharing-intent-filters': 'true' },
  action: [{ $: { 'android:name': 'android.intent.action.SEND' } }],
  data: [{ $: { 'android:mimeType': 'text/plain' } }],
  category: [{ $: { 'android:name': 'android.intent.category.DEFAULT' } }],
};

describe('single-share Android config plugin', () => {
  it('recognises only the generated empty SEND_MULTIPLE filter', () => {
    expect(plugin.isEmptyGeneratedMultipleShareFilter(generatedEmptyMultiple)).toBe(true);
    expect(plugin.isEmptyGeneratedMultipleShareFilter(generatedSingle)).toBe(false);
    expect(plugin.isEmptyGeneratedMultipleShareFilter({
      ...generatedEmptyMultiple,
      data: [{ $: { 'android:mimeType': 'text/plain' } }],
    })).toBe(false);
  });

  it('removes the empty multiple filter and preserves single share plus unrelated filters', () => {
    const manifest = {
      application: [{
        activity: [{
          $: { 'android:name': '.MainActivity' },
          'intent-filter': [
            { action: [{ $: { 'android:name': 'android.intent.action.MAIN' } }] },
            generatedSingle,
            generatedEmptyMultiple,
          ],
        }],
      }],
    };
    plugin.removeEmptyGeneratedMultipleShareFilters(manifest);
    const filters = manifest.application[0].activity[0]['intent-filter'];
    expect(filters).toHaveLength(2);
    expect(filters[0].action[0].$['android:name']).toBe('android.intent.action.MAIN');
    expect(filters[1]).toBe(generatedSingle);
  });
});
