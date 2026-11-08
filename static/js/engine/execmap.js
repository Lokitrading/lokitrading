/* HOLOGRAPHIC EXECUTION MAP — a live 3D topology of where Loki's traffic goes.
 *
 * Your device sits in the middle; every venue it talks to (Binance, Polymarket
 * odds and order books, Alpaca paper/live, Tradovate and Yahoo through Loki's
 * relay, the Loki server itself) orbits around it. Every real request lights a
 * pulse travelling out and back, coloured by the answer (green ok, amber slow,
 * red failed); pending orders orbit their venue as small cubes until they fill.
 * Nothing here is decorative data: it is drawn from the page's own network tap
 * (hosts, timings and status codes only; never bodies, headers or keys).
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
  ];
  const stats = Object.fromEntries(VENUES.map((v) => [v.id, { n: 0, ms: 0, last: null, errors: 0 }]));
  const queue = [];
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
    return null;
  }

  Loki.on('net', (e) => {
    const id = venueOf(e.host, e.path || '');
    if (!id) return;
    if (e.phase === 'end') {
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
  async function mount(root) {
    if (!root) return;
    running = true;
    if (started) return;
    started = true;
    root.innerHTML = `<div class="xm-stage"><canvas id="xm-canvas" aria-label="3D map of live connections"></canvas><div class="xm-scan" aria-hidden="true"></div></div>
      <div class="xm-side"><p class="eyebrow">LIVE CONNECTIONS FROM THIS DEVICE</p><table class="xm-table"><thead><tr><th>Venue</th><th>Calls</th><th>Avg ms</th><th>Last</th></tr></thead><tbody id="xm-rows"></tbody></table>
      <p class="caption">Drag to rotate, scroll to zoom. Pulses are real requests this page made: green answered, amber slow (&gt;1.5 s), red failed. Cubes are orders (paper or live) until they settle.</p></div>`;
    try {
      three = await import('/static/vendor/three/three.module.min.js');
    } catch (error) {
      root.querySelector('.xm-stage').innerHTML = `<p class="form-message">3D view unavailable on this device (${F.esc(error.message)}). The table still updates live.</p>`;
      tableLoop(root);
      return;
    }
    build(root);
    tableLoop(root);
  }

  function tableLoop(root) {
    const rows = root.querySelector('#xm-rows');
    const paint = () => {
      if (!document.body.contains(rows)) return;
      rows.innerHTML = VENUES.map((v) => { const s = stats[v.id]; const cls = !s.n ? '' : s.last && s.last < 400 ? (s.ms > 1500 ? 'slow' : 'ok') : 'bad';
        return `<tr class="${cls}"><td><i style="background:#${v.color.toString(16).padStart(6, '0')}"></i>${F.esc(v.label)}</td><td>${s.n}</td><td>${s.n ? Math.round(s.ms) : '—'}</td><td>${s.last == null ? '—' : s.last || 'fail'}</td></tr>`; }).join('');
      setTimeout(paint, 1000);
    };
    paint();
  }

  function build(root) {
    const T = three, canvas = root.querySelector('#xm-canvas');
    const renderer = new T.WebGLRenderer({ canvas, antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
    const scene = new T.Scene();
    scene.fog = new T.FogExp2(0x060c12, 0.035);
    const camera = new T.PerspectiveCamera(50, 1, 0.1, 200);
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    let yaw = 0.6, pitch = 0.42, dist = 17;

    const grid = new T.GridHelper(40, 40, 0x1f4a4a, 0x10262c);
    grid.position.y = -3.2; grid.material.transparent = true; grid.material.opacity = 0.35; scene.add(grid);
    scene.add(new T.AmbientLight(0x88aabb, 0.6));
    const glow = (color, size) => {
      const c = document.createElement('canvas'); c.width = c.height = 64;
      const g = c.getContext('2d'), grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
      grad.addColorStop(0, 'rgba(255,255,255,1)'); grad.addColorStop(0.25, 'rgba(255,255,255,.55)'); grad.addColorStop(1, 'rgba(255,255,255,0)');
      g.fillStyle = grad; g.fillRect(0, 0, 64, 64);
      const sprite = new T.Sprite(new T.SpriteMaterial({ map: new T.CanvasTexture(c), color, transparent: true, blending: T.AdditiveBlending, depthWrite: false }));
      sprite.scale.set(size, size, 1); return sprite;
    };
    const label = (text, color) => {
      const c = document.createElement('canvas'); c.width = 512; c.height = 96;
      const g = c.getContext('2d'); g.font = '600 38px Inter, system-ui, sans-serif'; g.fillStyle = '#' + color.toString(16).padStart(6, '0'); g.textAlign = 'center'; g.fillText(text, 256, 60);
      const sprite = new T.Sprite(new T.SpriteMaterial({ map: new T.CanvasTexture(c), transparent: true, depthWrite: false }));
      sprite.scale.set(4.2, 0.8, 1); return sprite;
    };

    const core = new T.Mesh(new T.IcosahedronGeometry(0.9, 2), new T.MeshBasicMaterial({ color: 0xa8f0c6, wireframe: true, transparent: true, opacity: 0.85 }));
    scene.add(core); scene.add(glow(0xa8f0c6, 5));
    const coreLabel = label('Your device · Loki engine', 0xa8f0c6); coreLabel.position.set(0, -1.7, 0); scene.add(coreLabel);

    const nodes = {};
    VENUES.forEach((v, k) => {
      const a = (k / VENUES.length) * Math.PI * 2, r = 7.2, y = Math.sin(k * 1.7) * 1.4;
      const pos = new T.Vector3(Math.cos(a) * r, y, Math.sin(a) * r);
      const mesh = new T.Mesh(new T.SphereGeometry(0.36, 20, 20), new T.MeshBasicMaterial({ color: v.color }));
      mesh.position.copy(pos); scene.add(mesh);
      const halo = glow(v.color, 1.9); halo.position.copy(pos); scene.add(halo);
      const tag = label(v.label, v.color); tag.position.copy(pos).add(new T.Vector3(0, 0.95, 0)); scene.add(tag);
      const lineGeo = new T.BufferGeometry().setFromPoints([new T.Vector3(0, 0, 0), pos]);
      const line = new T.Line(lineGeo, new T.LineBasicMaterial({ color: v.color, transparent: true, opacity: 0.18, blending: T.AdditiveBlending }));
      scene.add(line);
      nodes[v.id] = { pos, mesh, halo, line, heat: 0 };
    });

    const pulses = [], cubes = new Map();
    const pulseGeo = new T.SphereGeometry(0.12, 10, 10);
    function spawn(item) {
      const node = nodes[item.id]; if (!node) return;
      const color = item.phase === 'start' ? 0xe7f0ff : !item.status || item.status >= 400 ? 0xff6b5b : item.ms > 1500 ? 0xffd27a : 0x6fe3a1;
      const m = new T.Mesh(pulseGeo, new T.MeshBasicMaterial({ color, transparent: true, blending: T.AdditiveBlending }));
      scene.add(m);
      pulses.push({ m, node, out: item.phase === 'start', t: 0, speed: item.phase === 'start' ? 1.6 : 1.1 });
      node.heat = Math.min(1, node.heat + 0.5);
    }

    const resize = () => { const w = canvas.clientWidth || 600, h = canvas.clientHeight || 420; renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix(); };
    resize(); window.addEventListener('resize', resize);
    let drag = null;
    canvas.addEventListener('pointerdown', (e) => { drag = { x: e.clientX, y: e.clientY, yaw, pitch }; canvas.setPointerCapture(e.pointerId); });
    canvas.addEventListener('pointermove', (e) => { if (!drag) return; yaw = drag.yaw + (e.clientX - drag.x) * 0.006; pitch = Math.max(-0.2, Math.min(1.2, drag.pitch + (e.clientY - drag.y) * 0.004)); });
    canvas.addEventListener('pointerup', () => { drag = null; });
    canvas.addEventListener('wheel', (e) => { e.preventDefault(); dist = Math.max(9, Math.min(30, dist + e.deltaY * 0.01)); }, { passive: false });

    let last = performance.now();
    function frame(now) {
      if (!running) { requestAnimationFrame(frame); return; }
      const dt = Math.min(0.05, (now - last) / 1000); last = now;
      if (!drag) yaw += dt * (reduce ? 0.02 : 0.08);
      camera.position.set(Math.cos(yaw) * Math.cos(pitch) * dist, Math.sin(pitch) * dist, Math.sin(yaw) * Math.cos(pitch) * dist);
      camera.lookAt(0, 0, 0);
      core.rotation.y += dt * 0.4; core.rotation.x += dt * 0.15;
      while (queue.length) spawn(queue.shift());
      for (let i = pulses.length - 1; i >= 0; i--) {
        const p = pulses[i]; p.t += dt * p.speed;
        const k = p.out ? p.t : 1 - p.t;
        p.m.position.copy(p.node.pos).multiplyScalar(Math.max(0, Math.min(1, k)));
        if (p.t >= 1) { scene.remove(p.m); p.m.material.dispose(); pulses.splice(i, 1); }
      }
      for (const v of VENUES) { const n = nodes[v.id]; n.heat *= 0.96; n.line.material.opacity = 0.15 + n.heat * 0.7; n.halo.scale.setScalar(1.9 + n.heat * 1.6); }
      orders = orders.filter((o) => now - o.born < o.life);
      const alive = new Set();
      orders.forEach((o, k) => {
        const key = o.born + ':' + k; alive.add(key);
        let cube = cubes.get(key);
        if (!cube) { cube = new T.Mesh(new T.BoxGeometry(0.28, 0.28, 0.28), new T.MeshBasicMaterial({ color: 0xffd27a, wireframe: true })); scene.add(cube); cubes.set(key, cube); }
        const node = nodes[o.id], a = (now - o.born) / 600 + k;
        cube.position.set(node.pos.x + Math.cos(a) * 0.9, node.pos.y + Math.sin(a * 1.3) * 0.4, node.pos.z + Math.sin(a) * 0.9);
        cube.rotation.x += dt * 2; cube.rotation.y += dt * 3;
      });
      for (const [key, cube] of cubes) if (!alive.has(key)) { scene.remove(cube); cube.geometry.dispose(); cube.material.dispose(); cubes.delete(key); }
      renderer.render(scene, camera);
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  Loki.execmap = { mount, pause() { running = false; }, resume() { running = true; }, stats: () => stats };
})();
