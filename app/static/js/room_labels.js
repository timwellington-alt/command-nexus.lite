// Shared room-label helper. Loads /api/facility/room-labels once and
// exposes `roomLabel(bldg, code)` + `roomLabelHTML(...)`. Returns the
// raw room code when no display_name is set, so existing UIs keep
// working before/after a label is assigned. Idempotent — repeated
// loads return the same in-flight promise.
(function () {
  if (window.NEXUS_ROOM_LABELS_READY) return;
  window.NEXUS_ROOM_LABELS_READY = true;

  const cache = {};
  let loadPromise = null;

  function load() {
    if (loadPromise) return loadPromise;
    loadPromise = fetch('/api/facility/room-labels', {
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'CommandNexus' },
    })
      .then(r => r.ok ? r.json() : { labels: {} })
      .then(d => { Object.assign(cache, d.labels || {}); return cache; })
      .catch(() => cache);
    return loadPromise;
  }

  function roomLabel(bldg, code) {
    if (!code) return '';
    if (!bldg) return code;
    return cache[`${bldg}/${code}`] || code;
  }

  // For convenience inside template literals — escapes the result.
  // Includes single quote + backtick so values land safely inside
  // both single-quoted attributes and template-literal contexts.
  function roomLabelHTML(bldg, code) {
    const t = roomLabel(bldg, code);
    return String(t).replace(/[<>&"'`]/g, c => (
      { '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;',
        "'": '&#39;', '`': '&#96;' }[c]
    ));
  }

  window.roomLabel = roomLabel;
  window.roomLabelHTML = roomLabelHTML;
  window.loadRoomLabels = load;

  // Auto-kick on first import. Pages that need labels at render time
  // should `await window.loadRoomLabels()` before rendering.
  load();
})();
