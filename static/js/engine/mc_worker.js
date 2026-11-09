/* Background worker: runs Monte Carlo plans off the main thread so the page stays smooth. */
'use strict';
importScripts('/static/js/engine/mc_core.js');
self.onmessage = (event) => {
  const { id, plan, kind } = event.data || {};
  try {
    self.postMessage({ id, ok: true, result: kind === 'portfolio' ? self.LokiMC.simulatePortfolio(plan) : self.LokiMC.simulate(plan) });
  } catch (error) {
    self.postMessage({ id, ok: false, error: String((error && error.message) || error) });
  }
};
