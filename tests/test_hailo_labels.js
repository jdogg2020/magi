'use strict';
const assert = require('assert');
const labels = require('../src/magi/web/hailo_labels.js');

assert.equal(
  labels.formatAdvisory({event_label: 'aircraft', confidence: 0.82}),
  'Hailo advisory: aircraft (82%) — advisory only, not a human label');
assert.equal(
  labels.formatAdvisory({event_label: null}),
  'Hailo advisory: unresolved — advisory only, not a human label');
assert.equal(
  labels.formatAdvisory({advisory_text: 'Hailo advisory: cloud (50%) — advisory only, not a human label'}),
  'Hailo advisory: cloud (50%) — advisory only, not a human label');
assert.equal(labels.formatAdvisory(null), null, 'Missing summary must not throw or invent a label');
assert.equal(labels.formatAdvisory({}), null, 'Empty summary must not invent a label');
assert.equal(labels.formatAdvisory({event_label: 'aircraft', confidence: 'high'}), null,
  'Non-numeric confidence must not produce a label');
assert.equal(labels.formatAdvisory({event_label: 42}), null, 'Non-string label must not produce a label');
console.log('Hailo advisory labels passed: advisory-only text, unresolved handling, and damaged-input safety.');
