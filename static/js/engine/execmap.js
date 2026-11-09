/* HOLOGRAPHIC EXECUTION MAP — a live 3D topology of where Loki's traffic goes.
 *
 * Your device sits in the middle; every venue it talks to (Binance, Polymarket
 * odds and order books, Alpaca paper/live, Tradovate and Yahoo through Loki's
 * relay, the Loki server itself) orbits around it. Every real request lights a
 * pulse travelling out and back, coloured by the answer (green ok, amber slow,
 * red failed); pending orders orbit their venue as small cubes until they fill.
 * Nothing here is decorative data: it is drawn from the page's own network tap
 * (hosts, timings and status codes only; never bodies, headers or keys).
 * The inner ring is the engine's own "neural flow": each sub-agent (council,
 * swarm, consensus gate, evolution, Monte Carlo risk, risk matrix, execution
 * monitor, arbitrage engine) lights up when it actually fires an event, and
 * the table shows its current memory state (weights, generation, VaR…).
 * Look: a reactor-style core, curved light arcs to every venue with ambient
 * particle flow (decorative, faint), bright comets for real requests (data),
 * a neural ring of agents, starfield, scan waves and a live HUD. Hover a node
 * for its numbers, click to fly to it, double-click to reset, drag to orbit,
 * scroll or pinch to zoom. Reduced-motion slows everything right down.
 * Three.js is loaded only when this view is opened.
 */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki || Loki.execmap) return;
  const F = Loki.fmt;
  const VENUES = [
    { id: 'loki', label: 'Loki server', color: 0x8bdad4 },
    { id: 'binance', label: 'Binance market data', color: 0xf3ba2f },
    { id: 'gamma', label: 'Polymarket odds', color: 0x9ec5ff },
    { id: 'clob', label: 'Polymarket order books', color: 0x6ea8ff },
    { id: 'alpaca-paper', label: 'Alpaca paper', color: 0xffd27a },
    { id: 'alpaca-live', label: 'Alpaca LIVE', color: 0xff7a66 },
    { id: 'tradovate', label: 'Tradovate (via Loki)', color: 0xc3a6ff },
    { id: 'yahoo', label: 'Yahoo (via Loki)', color: 0xb8f0d8 },
    { id: 'social', label: 'Social feeds', color: 0xffb3a7 },
    { id: 'coinbase', label: 'Coinbase (backup feed)', color: 0x5b8cff },
  ];
  // Sub-agents of the on-device engine, keyed by the events they emit.
  const AGENTS = [
    { id: 'council', label: 'Six-bot council', color: 0xa8f0c6, events: ['council:journal'] },
    { id: 'swarm', label: 'Sentiment swarm', color: 0xffd27a, events: ['swarm:update', 'swarm:event'] },
    { id: 'consensus', label: 'Consensus gate', color: 0xc3a6ff, events: ['consensus:vote'] },
    { id: 'evolve', label: 'Evolution', color: 0x7fe0ff, events: ['evolve:gen', 'evolve:deploy'] },
    { id: 'mcrisk', label: 'Monte Carlo risk', color: 0xff9db0, events: ['mcrisk:update'] },
    { id: 'risk', label: 'Risk matrix', color: 0xff7a66, events: ['risk:update', 'risk:tripped', 'risk:healed'] },
    { id: 'execq', label: 'Execution monitor', color: 0x9ec5ff, events: ['execq:route'] },
    { id: 'arb', label: 'Arbitrage engine', color: 0xf3ba2f, events: ['arb:update'] },
  ];
  const agentState = Object.fromEntries(AGENTS.map((a) => [a.id, { n: 0, at: 0, memory: '—' }]));
  const agentQueue = [];
  const memoryOf = {
    'consensus:vote': (r) => `${r.approved ? 'approved' : 'blocked'} · score ${r.score >= 0 ? '+' : ''}${(r.score || 0).toFixed(2)}`,
    'evolve:gen': (r) => { const h = (r.history || [])[(r.history || []).length - 1]; return h ? `gen ${r.gen} · unseen-data fitness ${h.val.toFixed(3)}` : String(r.status || 'evolving').slice(0, 40); },
    'evolve:deploy': () => 'champion deployed',
    'mcrisk:update': (r) => r && r.var95 != null ? `VaR95 ${F.usd(r.var95)} · ×${(r.multiplier || 1).toFixed(2)}` : `${(r && r.positions) || 0} positions`,
    'risk:update': (r) => `${String(r.level).toUpperCase()} · size ×${(r.sizeMultiplier || 0).toFixed(2)}`,
    'risk:tripped': () => 'BREAKER TRIPPED', 'risk:healed': () => 'healed, ramping back',
    'swarm:update': (r) => r && r.running ? `bias ${(r.sentiment || 0).toFixed(2)}${r.event ? ' · EVENT' : ''}` : 'idle',
    'swarm:event': () => 'volatility quorum',
    'execq:route': (r) => `spot via ${r.source}`,
    'arb:update': (r) => r && r.running ? `${r.source || 'binance'} · ${r.paper ? r.paper.fills : 0} fills` : 'stopped',
    'council:journal': () => 'learned from a resolved call',
  };
  for (const a of AGENTS) for (const ev of a.events) {
    Loki.on(ev, (payload) => {
      const st = agentState[a.id]; st.n += 1; st.at = Date.now();
      try { st.memory = (memoryOf[ev] || (() => 'active'))(payload || {}); } catch { st.memory = 'active'; }
      agentQueue.push(a.id); if (agentQueue.length > 100) agentQueue.splice(0, agentQueue.length - 100);
    });
  }
  const stats = Object.fromEntries(VENUES.map((v) => [v.id, { n: 0, ms: 0, last: null, errors: 0 }]));
  const queue = [], recent = [];
  let orders = [];

  function venueOf(host, path) {
    if (host === 'loki') {
      if (path.startsWith('/api/broker/tradovate')) return 'tradovate';
      if (path.startsWith('/api/replay') || path.startsWith('/api/fusion/ai/bars') || path.startsWith('/api/markets')) return 'yahoo';
      return 'loki';
    }
    if (host.includes('binance')) return 'binance';
    if (host.startsWith('gamma-api')) return 'gamma';
    if (host.startsWith('clob') || host.startsWith('ws-subscriptions')) return 'clob';
    if (host.startsWith('paper-api.alpaca')) return 'alpaca-paper';
    if (host.includes('alpaca')) return 'alpaca-live';
    if (host.includes('reddit') || host.includes('algolia')) return 'social';
    if (host.includes('coinbase')) return 'coinbase';
    return null;
  }

  Loki.on('net', (e) => {
    const id = venueOf(e.host, e.path || '');
    if (!id) return;
    if (e.phase === 'end') {
      recent.push({ t: Date.now(), ms: e.ms, ok: Boolean(e.status && e.status < 400) }); if (recent.length > 400) recent.splice(0, recent.length - 400);
      const s = stats[id]; s.n += 1; s.ms = s.ms ? s.ms * 0.8 + e.ms * 0.2 : e.ms; s.last = e.status; if (!e.status || e.status >= 400) s.errors += 1;
    }
    queue.push({ id, phase: e.phase, status: e.status, ms: e.ms });
    if (queue.length > 200) queue.splice(0, queue.length - 200);
  });
  Loki.on('order', (o) => {
    const id = o.venue === 'polymarket' ? 'clob' : o.venue === 'alpaca' ? (o.live ? 'alpaca-live' : 'alpaca-paper') : o.venue === 'tradovate' ? 'tradovate' : 'loki';
    orders.push({ id, label: `${o.side || ''} ${o.qty || ''} ${o.symbol || ''}`.trim(), born: performance.now(), life: o.pending ? 20000 : 6000 });
  });

  let started = false, running = false, three = null;
  const hex = (c) => '#' + c.toString(16).padStart(6, '0');

  async function mount(root) {
    if (!root) return;
    running = true;
    if (started) return;
    started = true;
    root.innerHTML = `<div class="xm-stage" id="xm-stage">
        <canvas id="xm-canvas" aria-label="3D map of live connections and engine agents"></canvas>
        <div class="xm-vignette" aria-hidden="true"></div><div class="xm-scan" aria-hidden="true"></div>
        <div class="xm-hud xm-hud-tl" aria-live="polite"><span class="xm-live"><i></i>LIVE</span><div id="xm-kpis" class="xm-kpis"></div></div>
        <div class="xm-hud xm-hud-tr"><button type="button" class="xm-btn" id="xm-spin" aria-pressed="true" title="Pause the slow rotation">⟳ Auto-orbit</button><button type="button" class="xm-btn" id="xm-reset" title="Reset the camera (or double-click)">⌖ Reset</button><button type="button" class="xm-btn" id="xm-full" title="Fullscreen">⛶</button></div>
        <div class="xm-hud xm-hud-bl xm-legend"><span><i class="ok"></i>answered</span><span><i class="slow"></i>slow &gt;1.5 s</span><span><i class="bad"></i>failed</span><span><i class="out"></i>sent</span><span><i class="agent"></i>agent fired</span></div>
        <div class="xm-tip" id="xm-tip" hidden></div>
      </div>
      <div class="xm-side"><p class="eyebrow">LIVE CONNECTIONS FROM THIS DEVICE</p><table class="xm-table"><thead><tr><th>Venue</th><th>Calls</th><th>Avg ms</th><th>Last</th></tr></thead><tbody id="xm-rows"></tbody></table>
      <p class="eyebrow">ENGINE SUB-AGENTS · MEMORY STATE</p><table class="xm-table"><thead><tr><th>Agent</th><th>Events</th><th>State</th></tr></thead><tbody id="xm-agents"></tbody></table>
      <p class="caption">Bright comets are real requests this page made (green answered, amber slow, red failed, white on the way out); cubes are orders. Faint drifting particles are ambient decoration. Hover a node for its numbers, click to fly to it, double-click to reset. Hosts, timings and status codes only — never bodies, headers or keys.</p></div>`;
    tableLoop(root);
    try {
      three = await import('/static/vendor/three/three.module.min.js');
    } catch (error) {
      root.querySelector('.xm-stage').innerHTML = `<p class="form-message">3D view unavailable on this device (${F.esc(error.message)}). The tables still update live.</p>`;
      return;
    }
    try { build(root); } catch (error) {
      root.querySelector('.xm-stage').innerHTML = `<p class="form-message">This device could not start WebGL (${F.esc(error.message)}). The tables still update live.</p>`;
    }
  }

  function tableLoop(root) {
    const rows = root.querySelector('#xm-rows');
    const paint = () => {
      if (!document.body.contains(rows)) return;
      rows.innerHTML = VENUES.map((v) => { const s = stats[v.id]; const cls = !s.n ? '' : s.last && s.last < 400 ? (s.ms > 1500 ? 'slow' : 'ok') : 'bad';
        return `<tr class="${cls}"><td><i style="background:${hex(v.color)}"></i>${F.esc(v.label)}</td><td>${s.n}</td><td>${s.n ? Math.round(s.ms) : '—'}</td><td>${s.last == null ? '—' : s.last || 'fail'}</td></tr>`; }).join('');
      const ag = root.querySelector('#xm-agents');
      if (ag) ag.innerHTML = AGENTS.map((a) => { const st = agentState[a.id], hot = Date.now() - st.at < 4000;
        return `<tr class="${hot ? 'ok' : ''}"><td><i style="background:${hex(a.color)}"></i>${F.esc(a.label)}</td><td>${st.n}</td><td>${F.esc(st.memory)}</td></tr>`; }).join('');
      const kp = root.querySelector('#xm-kpis');
      if (kp) {
        const now = Date.now(), last = recent.filter((r) => now - r.t < 60000), errs = last.filter((r) => !r.ok).length;
        const avg = last.length ? Math.round(last.reduce((a, r) => a + r.ms, 0) / last.length) : null;
        const active = AGENTS.filter((a) => now - agentState[a.id].at < 10000).length;
        kp.innerHTML = `<div><b>${last.length}</b><span>requests / min</span></div><div><b>${avg == null ? '—' : avg + 'ms'}</b><span>avg latency</span></div><div><b class="${errs ? 'bad' : ''}">${errs}</b><span>errors / min</span></div><div><b>${active}/${AGENTS.length}</b><span>agents active</span></div>`;
      }
      setTimeout(paint, 1000);
    };
    paint();
  }

  function build(root) {
    const T = three, canvas = root.querySelector('#xm-canvas'), stage = root.querySelector('#xm-stage'), tip = root.querySelector('#xm-tip');
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const renderer = new T.WebGLRenderer({ canvas, antialias: true, alpha: false, powerPreference: 'high-performance' });
    renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
    renderer.setClearColor(0x03070c, 1);
    if ('outputColorSpace' in renderer) renderer.outputColorSpace = T.SRGBColorSpace;
    renderer.toneMapping = T.ACESFilmicToneMapping; renderer.toneMappingExposure = 1.15;
    const scene = new T.Scene();
    scene.fog = new T.FogExp2(0x03070c, 0.022);
    const camera = new T.PerspectiveCamera(52, 1, 0.1, 400);
    const ADD = T.AdditiveBlending;

    /* ---------- textures made on the fly (no image files) ---------- */
    const radial = (stops) => { const c = document.createElement('canvas'); c.width = c.height = 128; const g = c.getContext('2d'), gr = g.createRadialGradient(64, 64, 0, 64, 64, 64); stops.forEach(([o, col]) => gr.addColorStop(o, col)); g.fillStyle = gr; g.fillRect(0, 0, 128, 128); const t = new T.CanvasTexture(c); t.colorSpace = T.SRGBColorSpace; return t; };
    const glowTex = radial([[0, 'rgba(255,255,255,1)'], [0.18, 'rgba(255,255,255,.65)'], [0.45, 'rgba(255,255,255,.18)'], [1, 'rgba(255,255,255,0)']]);
    const dotTex = radial([[0, 'rgba(255,255,255,1)'], [0.4, 'rgba(255,255,255,.5)'], [1, 'rgba(255,255,255,0)']]);
    const glow = (color, size, opacity = 1) => { const s = new T.Sprite(new T.SpriteMaterial({ map: glowTex, color, transparent: true, opacity, blending: ADD, depthWrite: false })); s.scale.set(size, size, 1); return s; };
    const label = (text, color, scale = 1) => {
      const c = document.createElement('canvas'); c.width = 640; c.height = 112; const g = c.getContext('2d');
      g.font = '600 40px Inter, system-ui, sans-serif'; g.textAlign = 'center'; g.shadowColor = hex(color); g.shadowBlur = 18; g.fillStyle = '#eafcff'; g.fillText(text, 320, 66);
      const t = new T.CanvasTexture(c); t.colorSpace = T.SRGBColorSpace;
      const s = new T.Sprite(new T.SpriteMaterial({ map: t, transparent: true, depthWrite: false, opacity: 0.92 })); s.scale.set(4.6 * scale, 0.8 * scale, 1); return s;
    };

    /* ---------- backdrop: stars, nebula, floor ---------- */
    const starGeo = new T.BufferGeometry(), N = 1800, pos = new Float32Array(N * 3), col = new Float32Array(N * 3);
    const palette = [new T.Color(0x8bdad4), new T.Color(0xc3a6ff), new T.Color(0xffffff), new T.Color(0x6ea8ff)];
    for (let i = 0; i < N; i++) {
      const r = 60 + Math.random() * 90, th = Math.random() * Math.PI * 2, ph = Math.acos(2 * Math.random() - 1);
      pos.set([r * Math.sin(ph) * Math.cos(th), r * Math.cos(ph) * 0.6 + 10, r * Math.sin(ph) * Math.sin(th)], i * 3);
      const cc = palette[i % palette.length]; col.set([cc.r, cc.g, cc.b], i * 3);
    }
    starGeo.setAttribute('position', new T.BufferAttribute(pos, 3)); starGeo.setAttribute('color', new T.BufferAttribute(col, 3));
    const stars = new T.Points(starGeo, new T.PointsMaterial({ size: 0.55, map: dotTex, vertexColors: true, transparent: true, opacity: 0.85, blending: ADD, depthWrite: false, fog: false }));
    scene.add(stars);
    const nebA = glow(0x1d6f6a, 120, 0.35); nebA.position.set(-40, 18, -70); nebA.material.fog = false; scene.add(nebA);
    const nebB = glow(0x4b2a7a, 110, 0.32); nebB.position.set(45, 8, -60); nebB.material.fog = false; scene.add(nebB);
    const floor = new T.PolarGridHelper(15, 24, 10, 96, 0x1f6f6a, 0x0f2f33); floor.position.y = -3.2; floor.material.transparent = true; floor.material.opacity = 0.45; scene.add(floor);
    const ringMat = (c, o) => new T.MeshBasicMaterial({ color: c, transparent: true, opacity: o, blending: ADD, side: T.DoubleSide, depthWrite: false });
    const orbitRing = new T.Mesh(new T.RingGeometry(7.95, 8.05, 160), ringMat(0x8bdad4, 0.35)); orbitRing.rotation.x = -Math.PI / 2; orbitRing.position.y = -0.2; scene.add(orbitRing);
    const innerRing = new T.Mesh(new T.RingGeometry(3.55, 3.62, 120), ringMat(0xc3a6ff, 0.35)); innerRing.rotation.x = -Math.PI / 2; innerRing.position.y = 2.1; scene.add(innerRing);
    const waves = [];
    const spawnWave = (color = 0x8bdad4) => { const m = new T.Mesh(new T.RingGeometry(0.98, 1, 96), ringMat(color, 0.6)); m.rotation.x = -Math.PI / 2; m.position.y = -3.15; scene.add(m); waves.push({ m, t: 0 }); };

    /* ---------- the core: a small reactor ---------- */
    const coreGroup = new T.Group(); scene.add(coreGroup);
    const heart = new T.Mesh(new T.SphereGeometry(0.55, 32, 32), new T.MeshBasicMaterial({ color: 0xc8fff0 })); coreGroup.add(heart);
    const shell = new T.Mesh(new T.IcosahedronGeometry(1.05, 1), new T.MeshBasicMaterial({ color: 0x8bdad4, wireframe: true, transparent: true, opacity: 0.55, blending: ADD })); coreGroup.add(shell);
    const shell2 = new T.Mesh(new T.IcosahedronGeometry(1.35, 0), new T.MeshBasicMaterial({ color: 0xc3a6ff, wireframe: true, transparent: true, opacity: 0.25, blending: ADD })); coreGroup.add(shell2);
    const torus = (r, c, tilt) => { const m = new T.Mesh(new T.TorusGeometry(r, 0.025, 8, 160), new T.MeshBasicMaterial({ color: c, transparent: true, opacity: 0.8, blending: ADD })); m.rotation.set(tilt[0], tilt[1], 0); coreGroup.add(m); return m; };
    const rings = [torus(1.7, 0x8bdad4, [1.2, 0.2]), torus(1.95, 0xc3a6ff, [0.4, 1.1]), torus(2.2, 0x6ea8ff, [2.0, 0.6])];
    const coreHalo = glow(0x8bdad4, 7, 0.9); coreGroup.add(coreHalo);
    const coreLabel = label('Your device · Loki engine', 0x8bdad4); coreLabel.position.set(0, -2.1, 0); scene.add(coreLabel);

    /* ---------- venues on the outer orbit, with curved light arcs ---------- */
    const pickables = [], nodes = {};
    const arcPoints = 64;
    VENUES.forEach((v, k) => {
      const a = (k / VENUES.length) * Math.PI * 2, r = 8, y = Math.sin(k * 1.7) * 1.2;
      const p = new T.Vector3(Math.cos(a) * r, y, Math.sin(a) * r);
      const mid = p.clone().multiplyScalar(0.5); mid.y += 2.6 + (k % 3) * 0.5;
      const curve = new T.QuadraticBezierCurve3(new T.Vector3(0, 0, 0), mid, p);
      const tube = new T.Mesh(new T.TubeGeometry(curve, arcPoints, 0.018, 6, false), new T.MeshBasicMaterial({ color: v.color, transparent: true, opacity: 0.22, blending: ADD, depthWrite: false }));
      scene.add(tube);
      const node = new T.Mesh(new T.SphereGeometry(0.34, 24, 24), new T.MeshBasicMaterial({ color: v.color })); node.position.copy(p); scene.add(node);
      const cage = new T.Mesh(new T.OctahedronGeometry(0.62, 0), new T.MeshBasicMaterial({ color: v.color, wireframe: true, transparent: true, opacity: 0.5, blending: ADD })); cage.position.copy(p); scene.add(cage);
      const halo = glow(v.color, 2.2, 0.85); halo.position.copy(p); scene.add(halo);
      const tag = label(v.label, v.color, 0.82); tag.position.copy(p).add(new T.Vector3(0, 0.95, 0)); scene.add(tag);
      // ambient flow: a few faint particles drifting along the arc (decorative only)
      const fg = new T.BufferGeometry(), fp = new Float32Array(5 * 3); fg.setAttribute('position', new T.BufferAttribute(fp, 3));
      const flow = new T.Points(fg, new T.PointsMaterial({ size: 0.22, map: dotTex, color: v.color, transparent: true, opacity: 0.55, blending: ADD, depthWrite: false })); scene.add(flow);
      node.userData = { kind: 'venue', id: v.id, label: v.label };
      pickables.push(node);
      nodes[v.id] = { base: p.clone(), pos: p, node, cage, halo, tag, tube, curve, flow, phase: Math.random(), heat: 0, k };
    });

    /* ---------- neural ring of engine agents ---------- */
    const agentNodes = {};
    AGENTS.forEach((ag, k) => {
      const a = (k / AGENTS.length) * Math.PI * 2 + 0.3, r = 3.6;
      const p = new T.Vector3(Math.cos(a) * r, 2.1 + Math.cos(k * 2.1) * 0.35, Math.sin(a) * r);
      const solid = new T.Mesh(new T.OctahedronGeometry(0.16, 0), new T.MeshBasicMaterial({ color: ag.color })); solid.position.copy(p); scene.add(solid);
      const wire = new T.Mesh(new T.OctahedronGeometry(0.32, 0), new T.MeshBasicMaterial({ color: ag.color, wireframe: true, transparent: true, opacity: 0.6, blending: ADD })); wire.position.copy(p); scene.add(wire);
      const halo = glow(ag.color, 1.2, 0.7); halo.position.copy(p); scene.add(halo);
      const tag = label(ag.label, ag.color, 0.6); tag.position.copy(p).add(new T.Vector3(0, 0.52, 0)); scene.add(tag);
      const curve = new T.QuadraticBezierCurve3(new T.Vector3(0, 0, 0), p.clone().multiplyScalar(0.4).add(new T.Vector3(0, 1.4, 0)), p);
      const line = new T.Mesh(new T.TubeGeometry(curve, 32, 0.01, 4, false), new T.MeshBasicMaterial({ color: ag.color, transparent: true, opacity: 0.15, blending: ADD, depthWrite: false })); scene.add(line);
      solid.userData = { kind: 'agent', id: ag.id, label: ag.label }; pickables.push(solid);
      agentNodes[ag.id] = { pos: p, solid, wire, halo, line, curve, heat: 0 };
    });
    const synapses = [['council', 'consensus'], ['swarm', 'consensus'], ['mcrisk', 'risk'], ['risk', 'consensus'], ['execq', 'consensus'], ['evolve', 'council'], ['swarm', 'risk'], ['execq', 'arb']].map(([a, b]) => {
      const A = agentNodes[a].pos, B = agentNodes[b].pos, m = A.clone().add(B).multiplyScalar(0.5); m.y += 0.8;
      const c = new T.QuadraticBezierCurve3(A, m, B);
      const mesh = new T.Mesh(new T.TubeGeometry(c, 24, 0.008, 4, false), new T.MeshBasicMaterial({ color: 0xc3a6ff, transparent: true, opacity: 0.14, blending: ADD, depthWrite: false }));
      scene.add(mesh); return { a, b, curve: c, mesh, heat: 0 };
    });

    /* ---------- comets (real requests) and order cubes ---------- */
    const comets = [], cubes = new Map(), cometGeo = new T.SphereGeometry(0.11, 12, 12);
    const COLORS = { out: 0xe7f0ff, ok: 0x6fe3a1, slow: 0xffd27a, bad: 0xff6b5b };
    function spawn(item) {
      const n = nodes[item.id]; if (!n) return;
      const kind = item.phase === 'start' ? 'out' : !item.status || item.status >= 400 ? 'bad' : item.ms > 1500 ? 'slow' : 'ok';
      const head = new T.Mesh(cometGeo, new T.MeshBasicMaterial({ color: COLORS[kind], transparent: true, blending: ADD }));
      const h = glow(COLORS[kind], 0.9, 0.9); head.add(h);
      const trail = []; for (let i = 0; i < 6; i++) { const t = glow(COLORS[kind], 0.55 - i * 0.07, 0.55 - i * 0.08); scene.add(t); trail.push(t); }
      scene.add(head);
      comets.push({ head, trail, curve: n.curve, out: kind === 'out', t: 0, speed: kind === 'out' ? 0.9 : 0.7, node: n });
      n.heat = Math.min(1, n.heat + 0.55);
      if (kind === 'bad') spawnWave(0xff6b5b);
    }
    function agentPulse(id) {
      const n = agentNodes[id]; if (!n) return;
      n.heat = Math.min(1, n.heat + 0.7);
      const head = new T.Mesh(cometGeo, new T.MeshBasicMaterial({ color: 0xe7d8ff, transparent: true, blending: ADD })); head.add(glow(0xc3a6ff, 0.8, 0.9)); scene.add(head);
      comets.push({ head, trail: [], curve: n.curve, out: false, t: 0, speed: 1.2, node: null });
      synapses.forEach((s) => { if (s.a === id || s.b === id) s.heat = 1; });
    }

    /* ---------- camera controls: orbit, zoom, pinch, focus ---------- */
    let yaw = 0.7, pitch = 0.38, dist = 19, target = new T.Vector3(0, 0.6, 0), autoSpin = true;
    let goal = null; // {target, dist}
    const resize = () => { const w = canvas.clientWidth || 600, h = canvas.clientHeight || 420; renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix(); };
    resize(); window.addEventListener('resize', resize); document.addEventListener('fullscreenchange', () => setTimeout(resize, 50));
    const pointers = new Map(); let drag = null, pinch = null, moved = false;
    canvas.addEventListener('pointerdown', (e) => { pointers.set(e.pointerId, e); canvas.setPointerCapture(e.pointerId); moved = false;
      if (pointers.size === 2) { const [a, b] = [...pointers.values()]; pinch = { d: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY), dist }; drag = null; }
      else drag = { x: e.clientX, y: e.clientY, yaw, pitch }; });
    canvas.addEventListener('pointermove', (e) => {
      if (pointers.has(e.pointerId)) pointers.set(e.pointerId, e);
      if (pinch && pointers.size === 2) { const [a, b] = [...pointers.values()]; const d = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY); dist = Math.max(7, Math.min(34, pinch.dist * pinch.d / Math.max(1, d))); goal = null; return; }
      if (drag) { const dx = e.clientX - drag.x, dy = e.clientY - drag.y; if (Math.abs(dx) + Math.abs(dy) > 3) moved = true; yaw = drag.yaw + dx * 0.006; pitch = Math.max(-0.15, Math.min(1.25, drag.pitch + dy * 0.004)); goal = null; }
      hover(e);
    });
    const up = (e) => { pointers.delete(e.pointerId); if (pointers.size < 2) pinch = null; if (!pointers.size) { if (!moved) click(e); drag = null; } };
    canvas.addEventListener('pointerup', up); canvas.addEventListener('pointercancel', up);
    canvas.addEventListener('pointerleave', () => { tip.hidden = true; });
    canvas.addEventListener('wheel', (e) => { e.preventDefault(); dist = Math.max(7, Math.min(34, dist + e.deltaY * 0.012)); goal = null; }, { passive: false });
    canvas.addEventListener('dblclick', () => resetView());
    const ray = new T.Raycaster(), ndc = new T.Vector2();
    const pick = (e) => { const r = canvas.getBoundingClientRect(); ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1); ray.setFromCamera(ndc, camera); const hit = ray.intersectObjects(pickables, false)[0]; return hit ? hit.object.userData : null; };
    function hover(e) {
      const d = pick(e); if (!d) { tip.hidden = true; canvas.style.cursor = drag ? 'grabbing' : 'grab'; return; }
      canvas.style.cursor = 'pointer';
      const r = canvas.getBoundingClientRect();
      if (d.kind === 'venue') { const s = stats[d.id]; tip.innerHTML = `<b>${F.esc(d.label)}</b><span>${s.n} calls · ${s.n ? Math.round(s.ms) + ' ms avg' : 'no calls yet'}${s.errors ? ` · ${s.errors} failed` : ''}</span><span>last answer: ${s.last == null ? '—' : s.last || 'failed'}</span>`; }
      else { const st = agentState[d.id]; tip.innerHTML = `<b>${F.esc(d.label)}</b><span>${st.n} events · ${st.at ? Math.round((Date.now() - st.at) / 1000) + ' s ago' : 'not fired yet'}</span><span>${F.esc(st.memory)}</span>`; }
      tip.style.left = `${Math.min(r.width - 220, e.clientX - r.left + 14)}px`; tip.style.top = `${Math.max(8, e.clientY - r.top - 10)}px`; tip.hidden = false;
    }
    function click(e) {
      const d = pick(e); if (!d) return;
      const p = d.kind === 'venue' ? nodes[d.id].pos : agentNodes[d.id].pos;
      goal = { target: p.clone(), dist: 9 }; autoSpin = false; syncSpin();
    }
    function resetView() { goal = { target: new T.Vector3(0, 0.6, 0), dist: 19 }; autoSpin = !reduce; syncSpin(); }
    const spinBtn = root.querySelector('#xm-spin');
    const syncSpin = () => { spinBtn.setAttribute('aria-pressed', String(autoSpin)); spinBtn.classList.toggle('on', autoSpin); };
    spinBtn.addEventListener('click', () => { autoSpin = !autoSpin; syncSpin(); });
    root.querySelector('#xm-reset').addEventListener('click', resetView);
    root.querySelector('#xm-full').addEventListener('click', () => { if (document.fullscreenElement) document.exitFullscreen(); else if (stage.requestFullscreen) stage.requestFullscreen().catch(() => {}); });
    if (reduce) autoSpin = false; syncSpin();

    /* ---------- frame loop ---------- */
    let last = performance.now(), waveClock = 0;
    const tmp = new T.Vector3();
    function frame(now) {
      requestAnimationFrame(frame);
      if (!running || document.hidden) { last = now; return; }
      const dt = Math.min(0.05, (now - last) / 1000), time = now / 1000, slow = reduce ? 0.25 : 1; last = now;
      if (goal) { target.lerp(goal.target, 0.06); dist += (goal.dist - dist) * 0.06; if (target.distanceTo(goal.target) < 0.01 && Math.abs(goal.dist - dist) < 0.05) goal = null; }
      if (autoSpin && !drag && !pinch) yaw += dt * 0.07 * slow;
      camera.position.set(target.x + Math.cos(yaw) * Math.cos(pitch) * dist, target.y + Math.sin(pitch) * dist + Math.sin(time * 0.3) * 0.15 * slow, target.z + Math.sin(yaw) * Math.cos(pitch) * dist);
      camera.lookAt(target);
      // activity drives the core's heartbeat
      const wall = Date.now(), rate = recent.filter((r) => wall - r.t < 10000).length; // requests in the last 10 s
      const beat = 1 + Math.sin(time * (2 + Math.min(6, rate * 0.4))) * 0.06 * slow;
      heart.scale.setScalar(beat); coreHalo.scale.setScalar(6.5 + Math.min(4, rate * 0.25) + Math.sin(time * 2) * 0.3 * slow);
      shell.rotation.y += dt * 0.35 * slow; shell.rotation.x += dt * 0.12 * slow; shell2.rotation.y -= dt * 0.2 * slow;
      rings[0].rotation.z += dt * 0.6 * slow; rings[1].rotation.z -= dt * 0.45 * slow; rings[2].rotation.z += dt * 0.3 * slow;
      stars.rotation.y += dt * 0.004 * slow;
      // scan waves across the floor
      waveClock += dt * slow; if (!reduce && waveClock > 3.5) { waveClock = 0; spawnWave(); }
      for (let i = waves.length - 1; i >= 0; i--) { const w = waves[i]; w.t += dt * 0.35; const s = 1 + w.t * 15; w.m.scale.set(s, s, 1); w.m.material.opacity = Math.max(0, 0.55 * (1 - w.t)); if (w.t >= 1) { scene.remove(w.m); w.m.geometry.dispose(); w.m.material.dispose(); waves.splice(i, 1); } }
      // venues: bob, glow with heat, ambient flow
      for (const v of VENUES) {
        const n = nodes[v.id]; n.heat *= Math.pow(0.35, dt);
        const bob = Math.sin(time * 0.8 + n.k) * 0.12 * slow; n.node.position.y = n.base.y + bob; n.cage.position.y = n.base.y + bob; n.halo.position.y = n.base.y + bob;
        n.cage.rotation.y += dt * (0.4 + n.heat * 3); n.cage.rotation.x += dt * 0.2;
        n.halo.scale.setScalar(2.2 + n.heat * 2.4); n.tube.material.opacity = 0.18 + n.heat * 0.6;
        const fp = n.flow.geometry.attributes.position;
        for (let j = 0; j < 5; j++) { const u = (n.phase + time * 0.06 * slow + j / 5) % 1; n.curve.getPoint(u, tmp); fp.setXYZ(j, tmp.x, tmp.y, tmp.z); }
        fp.needsUpdate = true;
      }
      for (const id in agentNodes) { const n = agentNodes[id]; n.heat *= Math.pow(0.3, dt); n.wire.rotation.y += dt * (0.6 + n.heat * 5); n.wire.rotation.x += dt * 0.3; n.halo.scale.setScalar(1.2 + n.heat * 1.8); n.line.material.opacity = 0.12 + n.heat * 0.7; }
      for (const s of synapses) { s.heat *= Math.pow(0.3, dt); s.mesh.material.opacity = 0.12 + s.heat * 0.7; }
      innerRing.rotation.z += dt * 0.15 * slow; orbitRing.material.opacity = 0.28 + Math.sin(time) * 0.06;
      // new events
      let budget = 12; while (queue.length && budget--) spawn(queue.shift());
      while (agentQueue.length) agentPulse(agentQueue.shift());
      // comets with trails
      for (let i = comets.length - 1; i >= 0; i--) {
        const c = comets[i]; c.t += dt * c.speed;
        const u = Math.max(0, Math.min(1, c.out ? c.t : 1 - c.t)); c.curve.getPoint(u, c.head.position);
        c.trail.forEach((g, j) => { const uu = Math.max(0, Math.min(1, c.out ? u - (j + 1) * 0.025 : u + (j + 1) * 0.025)); c.curve.getPoint(uu, g.position); });
        if (c.t >= 1) { scene.remove(c.head); c.head.material.dispose(); c.trail.forEach((g) => { scene.remove(g); g.material.dispose(); }); comets.splice(i, 1); }
      }
      // order cubes orbit their venue
      orders = orders.filter((o) => now - o.born < o.life);
      const alive = new Set();
      orders.forEach((o, k) => {
        const key = o.born + ':' + k; alive.add(key);
        let cube = cubes.get(key);
        if (!cube) { cube = new T.Mesh(new T.BoxGeometry(0.3, 0.3, 0.3), new T.MeshBasicMaterial({ color: 0xffd27a, wireframe: true, transparent: true, blending: ADD })); cube.add(glow(0xffd27a, 1, 0.7)); scene.add(cube); cubes.set(key, cube); }
        const n = nodes[o.id] || nodes.loki, a = (now - o.born) / 600 + k;
        cube.position.set(n.node.position.x + Math.cos(a) * 1, n.node.position.y + Math.sin(a * 1.3) * 0.45, n.node.position.z + Math.sin(a) * 1);
        cube.rotation.x += dt * 2; cube.rotation.y += dt * 3;
      });
      for (const [key, cube] of cubes) if (!alive.has(key)) { scene.remove(cube); cube.geometry.dispose(); cube.material.dispose(); cubes.delete(key); }
      renderer.render(scene, camera);
    }
    requestAnimationFrame(frame);
  }

  Loki.execmap = { mount, pause() { running = false; if (document.fullscreenElement && document.fullscreenElement.id === 'xm-stage') document.exitFullscreen().catch(() => {}); }, resume() { running = true; }, stats: () => stats };
})();
