/* Shared reads: when several panels ask for the same read-only data at once (the radar, the
 * daily briefing and the Portfolio AI all read the portfolio on load), one request answers all of
 * them. Each caller gets its own copy of the response; a copy is reused for at most 15 seconds, so
 * every periodic refresh still fetches fresh data. Only the GET endpoints listed here are shared;
 * failed answers are never reused. Loaded first, before any panel script.
 */
'use strict';
(() => {
  if (!window.fetch || !window.Response || !Response.prototype.clone) return;
  const SHARED = new Set([
    '/api/fusion/capabilities', '/api/fusion/ai/portfolio', '/api/fusion/research-book',
    '/api/fusion/portfolio', '/api/fx/rate', '/api/markets/live', '/api/calendar/macro',
  ]);
  const KEEP_MS = 15000;
  const native = window.fetch.bind(window);
  const entries = new Map(); // path+query -> { at, promise<Response> }

  const untilAbort = (promise, signal) => (!signal ? promise : new Promise((resolve, reject) => {
    if (signal.aborted) { reject(signal.reason); return; }
    const onAbort = () => reject(signal.reason);
    signal.addEventListener('abort', onAbort, { once: true });
    promise.then((v) => { signal.removeEventListener('abort', onAbort); resolve(v); },
      (e) => { signal.removeEventListener('abort', onAbort); reject(e); });
  }));

  window.fetch = function sharedFetch(input, init) {
    let url;
    try {
      const method = String((init && init.method) || (input instanceof Request ? input.method : 'GET')).toUpperCase();
      url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url, location.href);
      if (method !== 'GET' || url.origin !== location.origin || !SHARED.has(url.pathname) || (init && init.body)) return native(input, init);
    } catch { return native(input, init); }
    const key = url.pathname + url.search;
    const now = Date.now();
    let entry = entries.get(key);
    if (!entry || now - entry.at > KEEP_MS) {
      // The shared request carries its own time limit; each caller's own signal only abandons its wait.
      const promise = native(key, { cache: 'no-store', credentials: 'same-origin', signal: AbortSignal.timeout ? AbortSignal.timeout(60000) : undefined })
        .then((response) => { if (!response.ok) entries.delete(key); return response; },
          (error) => { entries.delete(key); throw error; });
      entry = { at: now, promise };
      entries.set(key, entry);
    }
    return untilAbort(entry.promise.then((response) => response.clone()), init && init.signal);
  };
})();
