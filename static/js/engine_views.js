/* Mounts the replay sandbox and the 3D execution map when their views open,
 * and pauses the 3D renderer when it is off screen. */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki) return;
  function onView() {
    const replay = document.getElementById('replay');
    if (replay && !replay.hidden && Loki.replay) Loki.replay.mount(document.getElementById('replay-app'));
    const map = document.getElementById('execution-map');
    if (map && Loki.execmap) {
      if (!map.hidden) Loki.execmap.mount(document.getElementById('execmap-app'));
      else Loki.execmap.pause();
    }
  }
  // Panels that need the desktop launcher can only fail on the website: hide them there.
  fetch('/api/fusion/capabilities', { cache: 'no-store' }).then((r) => (r.ok ? r.json() : null)).then((caps) => {
    if (!caps || caps.local_actions_enabled) return;
    document.querySelectorAll('[data-local-only]').forEach((panel) => { panel.hidden = true; });
  }).catch(() => {});
  document.addEventListener('loki:view', onView);
  window.addEventListener('hashchange', () => setTimeout(onView, 0));
  onView();
})();
