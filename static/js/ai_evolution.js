/* AI EVOLUTION — the weekly champion-vs-challenger record (GET /api/fusion/ai/evolution).
 *
 * Loads once when the AI trading view first opens. Every value comes from the
 * evolution log written by evolve.py; nothing here is estimated or invented.
 */
'use strict';
(() => {
  const root = document.getElementById('ai-evolution');
  if (!root) return;
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = (v, d = 3) => (Number.isFinite(Number(v)) ? Number(v).toFixed(d) : '—');
  const pct = (v) => (Number.isFinite(Number(v)) ? `${Number(v) >= 0 ? '+' : ''}${Number(v).toFixed(1)}%` : '—');
  let loaded = false;

  function scoreRow(label, card) {
    if (!card) return '';
    return `<div class="evo-contender"><span class="evo-role">${esc(label)}</span><strong>${esc(card.label)}</strong>` +
      `<span class="evo-scores"><span title="Training windows: the worse of 2005-2018 and 2019-2022">training ${esc(num(card.rank_score))}</span>` +
      `<span title="2023 onward">2023+ ${esc(num(card.holdout_score))}</span>` +
      `<span title="Last 12 months">last 12m ${esc(num(card.recent_score))}</span></span></div>`;
  }

  function kindCard(title, k) {
    if (!k) return '';
    const m = k.mistakes || {};
    const hp = k.holdout_pooled || {};
    const filter = m.kept
      ? `Learn-from-mistakes filter: on, retrained on ${esc(m.lessons ?? '—')} closed trades (skips setups below a ${esc(Math.round((m.threshold || 0) * 100))}% historical win rate).`
      : `Learn-from-mistakes filter: off this week — ${esc(m.reason || 'it did not improve unseen results')}.`;
    return `<article class="evo-card ${k.promoted ? 'promoted' : 'kept'}">` +
      `<header><h3>${esc(title)}</h3><span class="tag ${k.promoted ? 'evo-tag-new' : ''}">${k.promoted ? '🧬 New champion' : 'Champion kept'}</span></header>` +
      `<p class="evo-reason">${esc(k.reason)}</p>` +
      scoreRow('Champion', k.champion) +
      (k.best_challenger ? scoreRow(`Best of ${k.challengers_tested} challengers`, k.best_challenger) : '') +
      `<p class="caption">${filter}</p>` +
      `<p class="caption">2023 onward (hypothetical): ${esc(pct(hp.mean_cagr_pct))} a year on average, Sharpe ${esc(num(hp.mean_sharpe, 2))}, profitable on ${esc(hp.profitable_symbols ?? '—')} of ${esc(hp.symbols ?? '—')} markets.</p>` +
      `</article>`;
  }

  function render(data) {
    const latest = data.latest;
    if (!latest) {
      $('evo-status').textContent = 'No generations yet';
      $('evo-latest').innerHTML = '<p class="muted">The first weekly generation has not run on this server yet.</p>';
      return;
    }
    $('evo-status').textContent = `Generation ${latest.generation} · ${latest.date}`;
    const p = latest.portfolio || {};
    $('evo-latest').innerHTML =
      `<p class="evo-summary">Generation <strong>${esc(latest.generation)}</strong> ran on ${esc(latest.date)} with data through ${esc(latest.data_through)}. ` +
      `${esc(data.promotions || 0)} challenger${data.promotions === 1 ? '' : 's'} promoted so far.` +
      (p.sharpe != null ? ` Portfolio AI on 2023-onward data (hypothetical): ${esc(pct(p.cagr_pct))} a year, Sharpe ${esc(num(p.sharpe, 2))}, worst drop ${esc(pct(p.max_dd_pct))}.` : '') +
      `</p><div class="evo-grid">${kindCard('Stocks & ETFs', latest.equity)}${kindCard('Crypto', latest.crypto)}</div>`;
    $('evo-timeline').innerHTML = (data.history || []).slice().reverse().map((g) => {
      const chip = (label, k) => `<span class="evo-chip ${k && k.promoted ? 'promoted' : ''}">${esc(label)}: ${k && k.promoted ? 'new champion' : 'kept'}${k && k.rank_score != null ? ` · ${esc(num(k.rank_score))}` : ''}</span>`;
      return `<li><strong>Gen ${esc(g.generation)}</strong> <span class="muted">${esc(g.date)}</span> ${chip('Stocks', g.equity)} ${chip('Crypto', g.crypto)}</li>`;
    }).join('') || '<li class="muted">No history yet.</li>';
  }

  async function load() {
    if (loaded) return;
    loaded = true;
    try {
      const res = await fetch('/api/fusion/ai/evolution', { cache: 'no-store', signal: AbortSignal.timeout(15000) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Evolution log unavailable');
      render(data);
    } catch (error) {
      loaded = false; // try again next time the view opens
      $('evo-status').textContent = 'Unavailable';
      $('evo-latest').innerHTML = `<p class="form-message">${esc(error.message || 'Evolution log unavailable')} — try again shortly.</p>`;
    }
  }

  const view = root.closest('.workspace-view');
  const visible = () => !view || !view.hidden;
  document.addEventListener('loki:view', () => { if (visible()) load(); });
  if (visible()) load();
})();
