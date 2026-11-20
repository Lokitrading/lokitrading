/* Loki's memory (AI trading page, panel #mind).
 *
 * Reads /api/fusion/ai/memory: the Portfolio AI's remembered decisions graded against what prices did
 * a week later, the lessons (weak and strong spots), whether any lesson is proven on decisions it was
 * not learned from (only proven lessons change the plan), recent form, the weekly self-evolution, and
 * in the desktop app a permanent journal kept on this computer. Loads when the panel comes into view.
 */
'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const panel = $('mind');
  if (!panel) return;
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const pct = (v, d = 0) => (v === null || v === undefined || !Number.isFinite(Number(v)) ? '—' : `${Number(v).toFixed(d)}%`);
  let busy = false;

  function render(m) {
    const o = (m.lessons && m.lessons.overall) || {};
    const v = m.verdicts || {};
    const form = m.form || {};
    const cell = (label, value, sub) => `<div class="pf-stat"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(sub)}</small></div>`;
    $('mind-metrics').innerHTML =
      cell('Remembered', m.remembered ?? 0, `${m.from || '—'} to ${m.to || '—'}`) +
      cell('Graded', m.graded ?? 0, `${m.pending ?? 0} still waiting a week`) +
      cell('Right when holding', pct(o.right_pct), `average week ${o.mean_fwd_pct >= 0 ? '+' : ''}${o.mean_fwd_pct ?? '—'}%`) +
      cell('Lately', form.status === 'not enough yet' ? '—' : (form.status || '—'),
        form.recent ? `last ${form.recent.n}: ${pct(form.recent.right_pct)} vs ${pct(form.before.right_pct)} before` : 'needs more decisions');
    const total = (v.right || 0) + (v.mistake || 0) + (v.missed || 0) || 1;
    $('mind-bar').innerHTML = `<span class="ok" style="width:${(100 * (v.right || 0)) / total}%" title="right ${v.right || 0}"></span>` +
      `<span class="bad" style="width:${(100 * (v.mistake || 0)) / total}%" title="mistakes ${v.mistake || 0}"></span>` +
      `<span class="miss" style="width:${(100 * (v.missed || 0)) / total}%" title="missed moves ${v.missed || 0}"></span>`;
    $('mind-bar').setAttribute('aria-label', `${v.right || 0} right, ${v.mistake || 0} mistakes, ${v.missed || 0} missed moves`);
    const list = (rows, none) => (rows && rows.length ? rows.slice(0, 5).map((r) => `<li>${esc(r.lesson)}</li>`).join('') : `<li class="muted">${esc(none)}</li>`);
    $('mind-weak').innerHTML = list(m.lessons && m.lessons.weak_spots, 'No clear weak spot yet.');
    $('mind-strong').innerHTML = list(m.lessons && m.lessons.strong_spots, 'No clear strong spot yet.');
    const proof = m.proof || {};
    const ev = proof.evidence || {};
    const head = { proven: 'Lesson proven and in use', 'not proven': 'Lessons shown, not used', learning: 'Still learning', 'no weak spots': 'Nothing to correct yet' }[proof.status] || 'Lessons';
    $('mind-proof').innerHTML = `<p><strong>${esc(head)}.</strong> ${esc(proof.why || '')}</p>` +
      (proof.status === 'proven' ? `<p>Now holding half as much when: ${esc((proof.adopted || []).join(', '))}.</p>` : '') +
      (ev.judged_decisions ? `<p class="caption">Learned on decisions before ${esc(ev.learned_before)}; judged on ${esc(ev.judged_decisions)} later ones` +
        `${ev.flagged_judged ? ` (${esc(ev.flagged_judged)} in the flagged setups, right ${pct(ev.flagged_judged_stats && ev.flagged_judged_stats.right_pct)})` : ''}. ` +
        `Beat ${pct(ev.beats_shuffles_pct)} of randomly relabelled groups (needs 90%).</p>` : '');
    $('mind-mistakes').innerHTML = (m.recent_mistakes || []).slice().reverse().map((d) =>
      `<tr><td>${esc(d.d)}</td><td>${esc(d.s)}</td><td>${esc(String(d.regime || '').replace(/_/g, ' ').toLowerCase())} · score ${esc(d.score ?? '—')}</td>` +
      `<td class="negative">${pct(100 * d.fwd, 1)}</td></tr>`).join('') || '<tr><td colspan="4" class="muted">No graded mistakes yet.</td></tr>';
    const evo = m.evolution || {};
    const j = m.journal;
    $('mind-foot').textContent = [
      m.how,
      evo.generations ? `Self-evolution: ${evo.generations} weekly generation${evo.generations === 1 ? '' : 's'}, ${evo.promotions || 0} promotion${evo.promotions === 1 ? '' : 's'}.` : '',
      j ? `On this computer: a permanent journal of ${j.decisions} live decisions since ${j.since || 'today'} (${j.graded} graded).` : '',
      'Grades use real prices; the decisions are the simulated Portfolio AI\'s. Research, not advice.',
    ].filter(Boolean).join(' ');
    $('mind-body').hidden = false;
  }

  async function load() {
    if (busy) return;
    busy = true;
    const msg = $('mind-message');
    msg.textContent = 'Studying its memory…';
    try {
      const res = await fetch('/api/fusion/ai/memory', { cache: 'no-store', signal: AbortSignal.timeout(90000) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'The memory could not be studied right now.');
      render(data);
      msg.textContent = '';
    } catch (error) {
      msg.textContent = error.name === 'TimeoutError' ? 'The study took too long; try again shortly.' : error.message;
    } finally { busy = false; }
  }

  $('mind-refresh').addEventListener('click', load);
  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver(([e]) => { if (e.isIntersecting) { io.disconnect(); load(); } });
    io.observe(panel);
  } else load();
})();
