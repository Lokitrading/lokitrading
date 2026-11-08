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
  document.addEventListener('loki:view', onView);
  window.addEventListener('hashchange', () => setTimeout(onView, 0));
  onView();
})();
