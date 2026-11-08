/* Page-side Monte Carlo API: Loki.mc.run(plan) -> Promise<result>.
 * Uses the background worker when the browser allows it, otherwise runs inline. */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki || Loki.mc) return;
  let worker = null, seq = 0;
  const waiting = new Map();
  function getWorker() {
    if (worker !== null) return worker;
    try {
      worker = new Worker('/static/js/engine/mc_worker.js');
      worker.onmessage = (e) => { const job = waiting.get(e.data.id); if (!job) return; waiting.delete(e.data.id); e.data.ok ? job.resolve(e.data.result) : job.reject(new Error(e.data.error)); };
      worker.onerror = () => { worker = false; for (const [, job] of waiting) job.fallback(); waiting.clear(); };
    } catch { worker = false; }
    return worker;
  }
  function run(plan) {
    const started = performance.now();
    const finish = (result) => ({ ...result, ms: Math.round(performance.now() - started) });
    const inline = () => {
      if (!window.LokiMC) throw new Error('Monte Carlo engine not loaded');
      return finish(window.LokiMC.simulate(plan));
    };
    const w = getWorker();
    if (!w) return Promise.resolve().then(inline);
    return new Promise((resolve, reject) => {
      const id = ++seq;
      waiting.set(id, { resolve: (r) => resolve(finish(r)), reject, fallback: () => { try { resolve(inline()); } catch (e) { reject(e); } } });
      w.postMessage({ id, plan });
      setTimeout(() => { const job = waiting.get(id); if (job) { waiting.delete(id); job.fallback(); } }, 15000);
    });
  }
  /** Small SVG histogram of simulated P&L, losses red and profits green. */
  function histogramSvg(result, width = 320, height = 70) {
    const { lo, hi, counts } = result.histogram, max = Math.max(...counts) || 1, bw = width / counts.length;
    const zero = hi > lo ? (0 - lo) / (hi - lo) * width : -1;
    return `<svg class="mc-hist" viewBox="0 0 ${width} ${height}" role="img" aria-label="Distribution of simulated profit and loss">` +
      counts.map((c, i) => { const x = i * bw, h = (c / max) * (height - 4), mid = lo + (i + 0.5) / counts.length * (hi - lo);
        return `<rect x="${x.toFixed(1)}" y="${(height - h).toFixed(1)}" width="${(bw - 1).toFixed(1)}" height="${h.toFixed(1)}" class="${mid >= 0 ? 'win' : 'loss'}"/>`; }).join('') +
      (zero >= 0 && zero <= width ? `<line x1="${zero.toFixed(1)}" x2="${zero.toFixed(1)}" y1="0" y2="${height}" class="zero"/>` : '') + '</svg>';
  }
  Loki.mc = { run, histogramSvg };
})();
