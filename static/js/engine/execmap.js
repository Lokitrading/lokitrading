/* 3D ENGINE MAP: a live, calm 3D topology of where Loki's traffic goes.
 *
 * Your device sits in the middle; every venue it talks to (Binance, Polymarket odds and order books,
 * Alpaca paper/live, Tradovate and Yahoo through Loki's relay, the Loki server itself) sits on one
 * ring around it, and the engine's own sub-agents (council, swarm, consensus gate, evolution, Monte
 * Carlo risk, risk matrix, execution monitor, arbitrage engine, Day Trader) on an inner ring. Every
 * real request is a comet travelling out and back, coloured by the answer (green ok, amber slow, red
 * failed); pending orders orbit their venue as small cubes. Only venues and agents that have actually
 * done something are lit and labelled, so the map shows what is happening and nothing else. Data
 * comes from the page's own network tap (hosts, timings and status codes only; never bodies, headers
 * or keys). Hover a node for its numbers, click to fly to it, double-click to reset, drag to orbit,
 * scroll or pinch to zoom. Reduced motion slows everything right down. Three.js loads only here.
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
    { id: 'daytrader', label: 'Day Trader', color: 0x6fe3a1, events: ['daytrader:trade', 'daytrader:plan'] },
  ];
  const agentState = Object.fromEntries(AGENTS.map((a) => [a.id, { n: 0, at: 0, memory: '—' }]));
  const agentQueue = [];
  const memoryOf = {
    'consensus:vote': (r) => `${r.approved ? 'approved' : 'blocked'} · score ${r.score >= 0 ? '+' : ''}${(r.score || 0).toFixed(2)}`,
    'evolve:gen': (r) => { const h = (r.history || [])[(r.history || []).length - 1]; return h ? `gen ${r.gen} · unseen-data fitness ${h.val.toFixed(3)}` : String(r.status || 'evolving').slice(0, 40); },
    'evolve:deploy': () => 'champion deployed',
    'daytrader:trade': (r) => `${r.action} ${r.symbol}${r.pnl != null ? ` · ${r.pnl >= 0 ? '+' : ''}${Number(r.pnl).toFixed(2)}` : ''}`,
    'daytrader:plan': (r) => `${r.mode} · ${r.open ? `${r.long} long, ${r.short} short, ${r.skipped} skipped` : 'market closed, flat'}`,
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
    root.innerHTML = `<div class="xm-main">
        <div class="xm-stage" id="xm-stage">
          <canvas id="xm-canvas" aria-label="3D map of live connections and engine agents"></canvas>
          <div class="xm-hud xm-hud-tr"><button type="button" class="xm-btn" id="xm-spin" aria-pressed="true" title="Pause the slow rotation" aria-label="Auto-rotate">⟳</button><button type="button" class="xm-btn" id="xm-reset" title="Reset the view (or double-click)" aria-label="Reset the view">⌖</button><button type="button" class="xm-btn" id="xm-full" title="Fullscreen" aria-label="Fullscreen">⛶</button></div>
          <div class="xm-tip" id="xm-tip" hidden></div>
        </div>
        <div class="xm-bar"><span class="xm-live"><i></i>Live</span><span id="xm-kpis" class="xm-kpis" aria-live="polite"></span></div>
        <div class="xm-legend"><span><i class="ok"></i>answered</span><span><i class="slow"></i>slow</span><span><i class="bad"></i>failed</span><span><i class="out"></i>sent</span><span><i class="agent"></i>agent</span><span class="xm-hint">Only active venues and agents are labelled. Drag to turn, scroll to zoom, click a node to fly to it.</span></div>
      </div>
      <div class="xm-side"><div class="xm-side-head"><p class="eyebrow">CONNECTIONS FROM THIS DEVICE</p><button type="button" class="xm-link" id="xm-all" aria-pressed="false">Show all</button></div>
      <table class="xm-table"><thead><tr><th>Venue</th><th>Calls</th><th>Avg</th><th>Last</th></tr></thead><tbody id="xm-rows"></tbody></table>
      <p class="eyebrow">ENGINE AGENTS</p><table class="xm-table"><thead><tr><th>Agent</th><th>Events</th><th>State</th></tr></thead><tbody id="xm-agents"></tbody></table>
      <p class="caption">Comets are real requests this page made; cubes are orders. Hosts, timings and status codes only, never bodies, headers or keys.</p></div>`;
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

  let showAll = false;
  const isActiveVenue = (id) => stats[id].n > 0;
  const isActiveAgent = (id) => agentState[id].n > 0;
  function tableLoop(root) {
    const rows = root.querySelector('#xm-rows');
    const allBtn = root.querySelector('#xm-all');
    allBtn.addEventListener('click', () => { showAll = !showAll; allBtn.setAttribute('aria-pressed', String(showAll)); allBtn.textContent = showAll ? 'Active only' : 'Show all'; });
    const paint = () => {
      if (!document.body.contains(rows)) return;
      const venues = VENUES.filter((v) => showAll || isActiveVenue(v.id));
      rows.innerHTML = venues.length ? venues.map((v) => { const s = stats[v.id]; const cls = !s.n ? 'idle' : s.last && s.last < 400 ? (s.ms > 1500 ? 'slow' : 'ok') : 'bad';
        return `<tr class="${cls}"><td><i style="background:${hex(v.color)}"></i>${F.esc(v.label)}</td><td>${s.n}</td><td>${s.n ? Math.round(s.ms) + ' ms' : '—'}</td><td>${s.last == null ? '—' : s.last || 'fail'}</td></tr>`; }).join('')
        : '<tr class="idle"><td colspan="4">No calls yet. Open Trade, Live markets or AI trading to see requests flow.</td></tr>';
      const ag = root.querySelector('#xm-agents');
      const agents = AGENTS.filter((a) => showAll || isActiveAgent(a.id));
      if (ag) ag.innerHTML = agents.length ? agents.map((a) => { const st = agentState[a.id], hot = Date.now() - st.at < 4000;
        return `<tr class="${hot ? 'ok' : st.n ? '' : 'idle'}"><td><i style="background:${hex(a.color)}"></i>${F.esc(a.label)}</td><td>${st.n}</td><td>${F.esc(st.memory)}</td></tr>`; }).join('')
        : '<tr class="idle"><td colspan="3">No agent has fired yet in this tab.</td></tr>';
      const kp = root.querySelector('#xm-kpis');
      if (kp) {
        const now = Date.now(), last = recent.filter((r) => now - r.t < 60000), errs = last.filter((r) => !r.ok).length;
        const avg = last.length ? Math.round(last.reduce((a, r) => a + r.ms, 0) / last.length) : null;
        const active = AGENTS.filter((a) => now - agentState[a.id].at < 10000).length;
        kp.innerHTML = `<b>${last.length}</b> requests a minute · <b>${avg == null ? '—' : avg + ' ms'}</b> average · <b class="${errs ? 'bad' : ''}">${errs}</b> failed · <b>${active}</b> of ${AGENTS.length} agents active`;
      }
      setTimeout(paint, 1000);
    };
    paint();
  }

  /* A calm scene: one core, one ring of venues, one ring of agents. Only venues and agents that have
   * actually done something are lit and labelled; everything idle is a small dim dot. */
  function build(root) {
    const T = three, canvas = root.querySelector('#xm-canvas'), stage = root.querySelector('#xm-stage'), tip = root.querySelector('#xm-tip');
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const renderer = new T.WebGLRenderer({ canvas, antialias: true, alpha: false, powerPreference: 'high-performance' });
    const small = Math.min(window.innerWidth, window.innerHeight) < 520;
    let quality = 0; // quality governor: 0 full, 1 lower resolution, 2 no glow pass (stepped down when frames run slow)
    const ratioFor = (q) => Math.min(q === 0 ? (small ? 1.25 : 1.5) : 1, window.devicePixelRatio || 1);
    renderer.setPixelRatio(ratioFor(0));
    renderer.setClearColor(0x050a10, 1);
    if ('outputColorSpace' in renderer) renderer.outputColorSpace = T.SRGBColorSpace;
    renderer.toneMapping = T.ACESFilmicToneMapping; renderer.toneMappingExposure = 1.05;
    const scene = new T.Scene();
    scene.fog = new T.FogExp2(0x050a10, 0.018);
    const camera = new T.PerspectiveCamera(48, 1, 0.5, 300);
    let fit = 1; // pulls the camera back on tall, narrow stages so the whole map stays in frame
    const ADD = T.AdditiveBlending;

    const radial = (stops) => { const c = document.createElement('canvas'); c.width = c.height = 128; const g = c.getContext('2d'), gr = g.createRadialGradient(64, 64, 0, 64, 64, 64); stops.forEach(([o, col]) => gr.addColorStop(o, col)); g.fillStyle = gr; g.fillRect(0, 0, 128, 128); const t = new T.CanvasTexture(c); t.colorSpace = T.SRGBColorSpace; return t; };
    const glowTex = radial([[0, 'rgba(255,255,255,1)'], [0.2, 'rgba(255,255,255,.55)'], [0.5, 'rgba(255,255,255,.12)'], [1, 'rgba(255,255,255,0)']]);
    const glow = (color, size, opacity = 1) => { const s = new T.Sprite(new T.SpriteMaterial({ map: glowTex, color, transparent: true, opacity, blending: ADD, depthWrite: false })); s.scale.set(size, size, 1); return s; };
    const label = (text, scale = 1) => {
      const c = document.createElement('canvas'); c.width = 512; c.height = 96; const g = c.getContext('2d');
      g.font = '500 34px Inter, system-ui, sans-serif'; g.textAlign = 'center'; g.fillStyle = 'rgba(5,10,16,.55)';
      const w = Math.min(500, g.measureText(text).width + 36); g.beginPath(); g.roundRect ? g.roundRect(256 - w / 2, 18, w, 58, 29) : g.rect(256 - w / 2, 18, w, 58); g.fill();
      g.fillStyle = '#e4f1f4'; g.fillText(text, 256, 59);
      const t = new T.CanvasTexture(c); t.colorSpace = T.SRGBColorSpace;
      const s = new T.Sprite(new T.SpriteMaterial({ map: t, transparent: true, depthWrite: false, opacity: 0 })); s.scale.set(3.6 * scale, 0.68 * scale, 1); return s;
    };
    const ringMat = (c, o) => new T.MeshBasicMaterial({ color: c, transparent: true, opacity: o, side: T.DoubleSide, depthWrite: false });
    const ring = (r, w, c, o, y) => { const m = new T.Mesh(new T.RingGeometry(r, r + w, 160), ringMat(c, o)); m.rotation.x = -Math.PI / 2; m.position.y = y; scene.add(m); return m; };
    const orbitRing = ring(8, 0.04, 0x8bdad4, 0.22, -0.2);
    const agentRing = ring(3.6, 0.03, 0xc3a6ff, 0.18, 1.6);
    ring(12, 0.02, 0x2a4152, 0.35, -0.2);

    // the core: one soft sphere and a halo that breathe with real traffic
    const coreGroup = new T.Group(); scene.add(coreGroup);
    const heart = new T.Mesh(new T.SphereGeometry(0.7, 40, 40), new T.MeshBasicMaterial({ color: 0x9fe8e0 })); coreGroup.add(heart);
    const coreHalo = glow(0x8bdad4, 5.5, 0.45); coreGroup.add(coreHalo);
    const coreLabel = label('Your device · Loki', 1.3); coreLabel.position.set(0, -1.6, 0); coreLabel.material.opacity = 0.9; scene.add(coreLabel);

    const pickables = [], nodes = {};
    VENUES.forEach((v, k) => {
      const a = (k / VENUES.length) * Math.PI * 2, r = 8;
      const p = new T.Vector3(Math.cos(a) * r, 0, Math.sin(a) * r);
      const mid = p.clone().multiplyScalar(0.5); mid.y += 1.6;
      const curve = new T.QuadraticBezierCurve3(new T.Vector3(0, 0, 0), mid, p);
      const line = new T.Mesh(new T.TubeGeometry(curve, 48, 0.012, 5, false), new T.MeshBasicMaterial({ color: v.color, transparent: true, opacity: 0.04, blending: ADD, depthWrite: false })); scene.add(line);
      const node = new T.Mesh(new T.SphereGeometry(0.28, 20, 20), new T.MeshBasicMaterial({ color: v.color, transparent: true, opacity: 0.35 })); node.position.copy(p); scene.add(node);
      const halo = glow(v.color, 1.6, 0); halo.position.copy(p); scene.add(halo);
      const tag = label(v.label, 1.3); tag.position.copy(p).add(new T.Vector3(0, 0.95, 0)); scene.add(tag);
      node.userData = { kind: 'venue', id: v.id, label: v.label }; pickables.push(node);
      nodes[v.id] = { pos: p, node, halo, tag, line, curve, heat: 0, lit: 0, color: new T.Color(v.color) };
    });
    const agentNodes = {};
    AGENTS.forEach((ag, k) => {
      const a = (k / AGENTS.length) * Math.PI * 2 + 0.3, r = 3.6;
      const p = new T.Vector3(Math.cos(a) * r, 1.6, Math.sin(a) * r);
      const solid = new T.Mesh(new T.OctahedronGeometry(0.17, 0), new T.MeshBasicMaterial({ color: ag.color, transparent: true, opacity: 0.3 })); solid.position.copy(p); scene.add(solid);
      const halo = glow(ag.color, 1, 0); halo.position.copy(p); scene.add(halo);
      const tag = label(ag.label, 1.05); tag.position.copy(p).add(new T.Vector3(0, 0.55, 0)); scene.add(tag);
      const curve = new T.QuadraticBezierCurve3(new T.Vector3(0, 0, 0), p.clone().multiplyScalar(0.5).add(new T.Vector3(0, 0.9, 0)), p);
      solid.userData = { kind: 'agent', id: ag.id, label: ag.label }; pickables.push(solid);
      agentNodes[ag.id] = { pos: p, solid, halo, tag, curve, heat: 0, lit: 0, color: new T.Color(ag.color) };
    });

    // comets (real requests) and order cubes
    const comets = [], cubes = new Map(), cometGeo = new T.SphereGeometry(0.09, 10, 10);
    const COLORS = { out: 0xe7f0ff, ok: 0x6fe3a1, slow: 0xffd27a, bad: 0xff6b5b };
    function spawn(item) {
      const n = nodes[item.id]; if (!n) return;
      const kind = item.phase === 'start' ? 'out' : !item.status || item.status >= 400 ? 'bad' : item.ms > 1500 ? 'slow' : 'ok';
      const head = new T.Mesh(cometGeo, new T.MeshBasicMaterial({ color: COLORS[kind], transparent: true, blending: ADD }));
      head.add(glow(COLORS[kind], 0.7, 0.8));
      const trail = []; for (let i = 0; i < 3; i++) { const t = glow(COLORS[kind], 0.4 - i * 0.08, 0.45 - i * 0.12); scene.add(t); trail.push(t); }
      scene.add(head);
      comets.push({ head, trail, curve: n.curve, out: kind === 'out', t: 0, speed: kind === 'out' ? 0.9 : 0.7 });
      n.heat = Math.min(1, n.heat + 0.5);
    }
    function agentPulse(id) {
      const n = agentNodes[id]; if (!n) return;
      n.heat = Math.min(1, n.heat + 0.7);
      const head = new T.Mesh(cometGeo, new T.MeshBasicMaterial({ color: 0xe7d8ff, transparent: true, blending: ADD })); head.add(glow(0xc3a6ff, 0.6, 0.8)); scene.add(head);
      comets.push({ head, trail: [], curve: n.curve, out: false, t: 0, speed: 1.2 });
    }

    // camera controls: orbit, zoom, pinch, focus
    let yaw = 0.7, pitch = 0.5, dist = reduce ? 21 : 40, target = new T.Vector3(0, 0.4, 0), autoSpin = true;
    let goal = reduce ? null : { target: new T.Vector3(0, 0.4, 0), dist: 21 };
    let post = null;
    try {
      const float = renderer.capabilities.isWebGL2 && renderer.extensions.has('EXT_color_buffer_float');
      const opt = (depth) => ({ minFilter: T.LinearFilter, magFilter: T.LinearFilter, type: float ? T.HalfFloatType : T.UnsignedByteType, depthBuffer: depth, samples: depth && !small ? 2 : 0 });
      const sceneRT = new T.WebGLRenderTarget(4, 4, opt(true)), rtA = new T.WebGLRenderTarget(4, 4, opt(false)), rtB = new T.WebGLRenderTarget(4, 4, opt(false));
      const QVS = 'varying vec2 vUv; void main(){ vUv = uv; gl_Position = vec4(position.xy, 0.0, 1.0); }';
      const pass = (fragmentShader, uniforms) => new T.ShaderMaterial({ vertexShader: QVS, fragmentShader, uniforms, depthTest: false, depthWrite: false });
      const bright = pass('uniform sampler2D tMap; varying vec2 vUv; void main(){ vec3 c = texture2D(tMap, vUv).rgb; float l = max(c.r, max(c.g, c.b)); gl_FragColor = vec4(c * smoothstep(0.6, 1.1, l), 1.0); }', { tMap: { value: sceneRT.texture } });
      const blur = pass('uniform sampler2D tMap; uniform vec2 uDir; varying vec2 vUv; void main(){ vec3 c = texture2D(tMap, vUv).rgb * 0.227; c += (texture2D(tMap, vUv + uDir * 1.385).rgb + texture2D(tMap, vUv - uDir * 1.385).rgb) * 0.316; c += (texture2D(tMap, vUv + uDir * 3.231).rgb + texture2D(tMap, vUv - uDir * 3.231).rgb) * 0.070; gl_FragColor = vec4(c, 1.0); }', { tMap: { value: null }, uDir: { value: new T.Vector2() } });
      const final = pass('uniform sampler2D tScene; uniform sampler2D tBloom; varying vec2 vUv; vec3 aces(vec3 x){ return clamp((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14), 0.0, 1.0); } void main(){ vec2 d = vUv - 0.5; vec3 c = texture2D(tScene, vUv).rgb + texture2D(tBloom, vUv).rgb * 0.55; c = aces(c); c *= mix(1.0, 0.7, smoothstep(0.2, 0.7, dot(d, d))); gl_FragColor = vec4(pow(c, vec3(1.0 / 2.2)), 1.0); }', { tScene: { value: sceneRT.texture }, tBloom: { value: null } });
      const quad = new T.Mesh(new T.PlaneGeometry(2, 2), bright), qScene = new T.Scene(), qCam = new T.OrthographicCamera(-1, 1, 1, -1, 0, 1); quad.frustumCulled = false; qScene.add(quad);
      const run = (material, targetRT) => { quad.material = material; renderer.setRenderTarget(targetRT); renderer.render(qScene, qCam); };
      post = {
        size(w, h) { const pr = renderer.getPixelRatio(); sceneRT.setSize(Math.round(w * pr), Math.round(h * pr)); const bw = Math.max(2, Math.round(w * pr / 3)), bh = Math.max(2, Math.round(h * pr / 3)); rtA.setSize(bw, bh); rtB.setSize(bw, bh); this.px = [1 / bw, 1 / bh]; },
        render() {
          renderer.toneMapping = T.NoToneMapping;
          renderer.setRenderTarget(sceneRT); renderer.render(scene, camera);
          run(bright, rtA);
          for (let i = 0; i < 2; i++) { const sc = 1 + i * 1.6; blur.uniforms.tMap.value = rtA.texture; blur.uniforms.uDir.value.set(this.px[0] * sc, 0); run(blur, rtB); blur.uniforms.tMap.value = rtB.texture; blur.uniforms.uDir.value.set(0, this.px[1] * sc); run(blur, rtA); }
          final.uniforms.tBloom.value = rtA.texture; run(final, null);
        },
      };
    } catch (error) { post = null; }
    const resize = () => { const w = canvas.clientWidth || 600, h = canvas.clientHeight || 420; renderer.setSize(w, h, false); camera.aspect = w / h; fit = Math.max(1, Math.min(2.2, 1.25 / camera.aspect)); camera.updateProjectionMatrix(); if (post) post.size(w, h); };
    resize(); window.addEventListener('resize', resize); document.addEventListener('fullscreenchange', () => setTimeout(resize, 50));
    const pointers = new Map(); let drag = null, pinch = null, moved = false, hovered = null;
    canvas.addEventListener('pointerdown', (e) => { pointers.set(e.pointerId, e); canvas.setPointerCapture(e.pointerId); moved = false;
      if (pointers.size === 2) { const [a, b] = [...pointers.values()]; pinch = { d: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY), dist }; drag = null; }
      else drag = { x: e.clientX, y: e.clientY, yaw, pitch }; });
    canvas.addEventListener('pointermove', (e) => {
      if (pointers.has(e.pointerId)) pointers.set(e.pointerId, e);
      if (pinch && pointers.size === 2) { const [a, b] = [...pointers.values()]; const d = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY); dist = Math.max(8, Math.min(34, pinch.dist * pinch.d / Math.max(1, d))); goal = null; return; }
      if (drag) { const dx = e.clientX - drag.x, dy = e.clientY - drag.y; if (Math.abs(dx) + Math.abs(dy) > 3) moved = true; yaw = drag.yaw + dx * 0.006; pitch = Math.max(0.05, Math.min(1.3, drag.pitch + dy * 0.004)); goal = null; }
      hover(e);
    });
    const up = (e) => { pointers.delete(e.pointerId); if (pointers.size < 2) pinch = null; if (!pointers.size) { if (!moved) click(e); drag = null; } };
    canvas.addEventListener('pointerup', up); canvas.addEventListener('pointercancel', up);
    canvas.addEventListener('pointerleave', () => { tip.hidden = true; hovered = null; });
    canvas.addEventListener('wheel', (e) => { e.preventDefault(); dist = Math.max(8, Math.min(34, dist + e.deltaY * 0.012)); goal = null; }, { passive: false });
    canvas.addEventListener('dblclick', () => resetView());
    const ray = new T.Raycaster(), ndc = new T.Vector2();
    const pick = (e) => { const r = canvas.getBoundingClientRect(); ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1); ray.setFromCamera(ndc, camera); const hit = ray.intersectObjects(pickables, false)[0]; return hit ? hit.object.userData : null; };
    function hover(e) {
      const d = pick(e); hovered = d ? d.id : null;
      if (!d) { tip.hidden = true; canvas.style.cursor = drag ? 'grabbing' : 'grab'; return; }
      canvas.style.cursor = 'pointer';
      const r = canvas.getBoundingClientRect();
      if (d.kind === 'venue') { const s = stats[d.id]; tip.innerHTML = `<b>${F.esc(d.label)}</b><span>${s.n} calls · ${s.n ? Math.round(s.ms) + ' ms average' : 'no calls yet'}${s.errors ? ` · ${s.errors} failed` : ''}</span>`; }
      else { const st = agentState[d.id]; tip.innerHTML = `<b>${F.esc(d.label)}</b><span>${st.n} events · ${st.at ? Math.round((Date.now() - st.at) / 1000) + ' s ago' : 'not fired yet'}</span><span>${F.esc(st.memory)}</span>`; }
      tip.style.left = `${Math.min(r.width - 220, e.clientX - r.left + 14)}px`; tip.style.top = `${Math.max(8, e.clientY - r.top - 10)}px`; tip.hidden = false;
    }
    function click(e) {
      const d = pick(e); if (!d) return;
      const p = d.kind === 'venue' ? nodes[d.id].pos : agentNodes[d.id].pos;
      goal = { target: p.clone(), dist: 10 }; autoSpin = false; syncSpin();
    }
    function resetView() { goal = { target: new T.Vector3(0, 0.4, 0), dist: 21 }; autoSpin = !reduce; syncSpin(); }
    const spinBtn = root.querySelector('#xm-spin');
    const syncSpin = () => { spinBtn.setAttribute('aria-pressed', String(autoSpin)); spinBtn.classList.toggle('on', autoSpin); };
    spinBtn.addEventListener('click', () => { autoSpin = !autoSpin; syncSpin(); });
    root.querySelector('#xm-reset').addEventListener('click', resetView);
    root.querySelector('#xm-full').addEventListener('click', () => { if (document.fullscreenElement) document.exitFullscreen(); else if (stage.requestFullscreen) stage.requestFullscreen().catch(() => {}); });
    if (reduce) autoSpin = false; syncSpin();

    let last = performance.now(), slowFor = 0;
    const ease = (cur, goalV, k) => cur + (goalV - cur) * k;
    const IDLE = new T.Color(0x3a4b58); // idle venues and agents are muted grey dots
    function frame(now) {
      requestAnimationFrame(frame);
      if (!running || document.hidden) { last = now; return; }
      const dt = Math.min(0.05, (now - last) / 1000), time = now / 1000, slow = reduce ? 0.25 : 1; last = now;
      if (goal) { target.lerp(goal.target, 0.06); dist += (goal.dist - dist) * 0.06; if (target.distanceTo(goal.target) < 0.01 && Math.abs(goal.dist - dist) < 0.05) goal = null; }
      if (autoSpin && !drag && !pinch) yaw += dt * 0.05 * slow;
      const far = dist * fit;
      camera.position.set(target.x + Math.cos(yaw) * Math.cos(pitch) * far, target.y + Math.sin(pitch) * far, target.z + Math.sin(yaw) * Math.cos(pitch) * far);
      camera.lookAt(target);
      const wall = Date.now(), rate = recent.filter((r) => wall - r.t < 10000).length;
      heart.scale.setScalar(1 + Math.sin(time * 1.6) * 0.03 * slow + Math.min(0.15, rate * 0.01));
      coreHalo.scale.setScalar(5 + Math.min(3, rate * 0.2));
      // quality governor: if frames stay slow, drop resolution, then the glow pass
      slowFor = dt > 0.034 ? slowFor + dt : Math.max(0, slowFor - dt * 0.5);
      if (slowFor > 2.5 && quality < 2) { quality += 1; slowFor = 0; renderer.setPixelRatio(ratioFor(quality)); if (quality === 2) { post = null; renderer.setRenderTarget(null); renderer.toneMapping = T.ACESFilmicToneMapping; } resize(); }
      // venues and agents: lit (and labelled) only once they have done something, or while hovered
      for (const v of VENUES) {
        const n = nodes[v.id]; n.heat *= Math.pow(0.35, dt);
        const active = isActiveVenue(v.id); n.lit = ease(n.lit, active ? 1 : 0, 0.08);
        n.node.material.opacity = 0.35 + n.lit * 0.65; n.node.material.color.copy(IDLE).lerp(n.color, n.lit); n.halo.material.opacity = n.lit * (0.35 + n.heat * 0.5); n.halo.scale.setScalar(1.6 + n.heat * 1.6);
        n.line.material.opacity = 0.03 + n.lit * 0.14 + n.heat * 0.5;
        n.tag.material.opacity = ease(n.tag.material.opacity, active || hovered === v.id ? 0.95 : 0, 0.12);
      }
      for (const ag of AGENTS) {
        const n = agentNodes[ag.id]; n.heat *= Math.pow(0.3, dt);
        const active = isActiveAgent(ag.id); n.lit = ease(n.lit, active ? 1 : 0, 0.08);
        n.solid.material.opacity = 0.3 + n.lit * 0.7; n.solid.material.color.copy(IDLE).lerp(n.color, n.lit); n.solid.rotation.y += dt * (0.4 + n.heat * 4) * slow;
        n.halo.material.opacity = n.lit * (0.25 + n.heat * 0.6); n.halo.scale.setScalar(1 + n.heat * 1.4);
        const recentFire = wall - agentState[ag.id].at < 15000;
        n.tag.material.opacity = ease(n.tag.material.opacity, recentFire || hovered === ag.id ? 0.9 : 0, 0.12);
      }
      orbitRing.material.opacity = 0.18 + Math.min(0.2, rate * 0.01); agentRing.material.opacity = 0.14;
      let budget = 12; while (queue.length && budget--) spawn(queue.shift());
      while (agentQueue.length) agentPulse(agentQueue.shift());
      for (let i = comets.length - 1; i >= 0; i--) {
        const c = comets[i]; c.t += dt * c.speed;
        const u = Math.max(0, Math.min(1, c.out ? c.t : 1 - c.t)); c.curve.getPoint(u, c.head.position);
        c.trail.forEach((g, j) => { const uu = Math.max(0, Math.min(1, c.out ? u - (j + 1) * 0.03 : u + (j + 1) * 0.03)); c.curve.getPoint(uu, g.position); });
        if (c.t >= 1) { scene.remove(c.head); c.head.material.dispose(); c.trail.forEach((g) => { scene.remove(g); g.material.dispose(); }); comets.splice(i, 1); }
      }
      orders = orders.filter((o) => now - o.born < o.life);
      const alive = new Set();
      orders.forEach((o, k) => {
        const key = o.born + ':' + k; alive.add(key);
        let cube = cubes.get(key);
        if (!cube) { cube = new T.Mesh(new T.BoxGeometry(0.26, 0.26, 0.26), new T.MeshBasicMaterial({ color: 0xffd27a, transparent: true, opacity: 0.9 })); scene.add(cube); cubes.set(key, cube); }
        const n = nodes[o.id] || nodes.loki, a = (now - o.born) / 700 + k;
        cube.position.set(n.pos.x + Math.cos(a) * 0.9, n.pos.y + 0.2, n.pos.z + Math.sin(a) * 0.9);
        cube.rotation.y += dt * 2;
      });
      for (const [key, cube] of cubes) if (!alive.has(key)) { scene.remove(cube); cube.geometry.dispose(); cube.material.dispose(); cubes.delete(key); }
      if (post) { try { post.render(); } catch (error) { post = null; renderer.setRenderTarget(null); renderer.toneMapping = T.ACESFilmicToneMapping; } }
      else renderer.render(scene, camera);
    }
    requestAnimationFrame(frame);
  }

  Loki.execmap = { mount, pause() { running = false; if (document.fullscreenElement && document.fullscreenElement.id === 'xm-stage') document.exitFullscreen().catch(() => {}); }, resume() { running = true; }, stats: () => stats };
})();
