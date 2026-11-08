/* Background worker: runs Monte Carlo plans off the main thread so the page stays smooth. */
'use strict';
importScripts('/static/js/engine/mc_core.js');
self.onmessage = (event) => {
  const { id, plan } = event.data || {};
  try {
    self.postMessage({ id, ok: true, result: self.LokiMC.simulate(plan) });
  } catch (error) {
    self.postMessage({ id, ok: false, error: String((error && error.message) || error) });
  }
};
