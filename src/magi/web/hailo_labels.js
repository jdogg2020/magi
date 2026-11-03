'use strict';
/* Advisory Hailo labels for the Magi dashboard.

This formatter is intentionally pure: it never hides, reorders, or
otherwise changes an event list. It only turns a validated
magi-hailo-v1 sidecar summary (see magi/hailo.py advisory_summary)
into display text, or null when the summary is unusable. A damaged
advisory file must never hide an event, so every failure here
returns null instead of throwing. The label is always marked
advisory only; it is never a human review label. */
const MagiHailoLabels = (() => {
  function formatAdvisory(summary) {
    if (!summary || typeof summary !== 'object') return null;
    if (typeof summary.advisory_text === 'string' && summary.advisory_text) {
      return summary.advisory_text;
    }
    if (!('event_label' in summary)) return null;
    const label = summary.event_label;
    if (typeof label === 'string' && label) {
      const confidence = summary.confidence;
      if (typeof confidence !== 'number' || !Number.isFinite(confidence)) return null;
      return `Hailo advisory: ${label} (${Math.round(confidence * 100)}%) — advisory only, not a human label`;
    }
    if (label === null || label === undefined) {
      return 'Hailo advisory: unresolved — advisory only, not a human label';
    }
    return null;
  }
  return {formatAdvisory};
})();
if (typeof module !== 'undefined' && module.exports) module.exports = MagiHailoLabels;
