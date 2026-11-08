/* PROOF-OF-STRATEGY — a tamper-evident, signed log of every trade execution.
 *
 * Every paper or live execution is appended automatically at the moment it
 * happens (nobody chooses which trades go in). Each entry carries the SHA-256
 * hash of the previous one, so editing, deleting or reordering any entry
 * breaks every hash after it. Each hash is signed with an ECDSA P-256 key
 * generated on this device; the private key is non-extractable (the browser
 * will not reveal it, not even to Loki) and the public key travels with every
 * export, so anyone can re-verify a log with standard WebCrypto.
 * Performance figures on the badge are recomputed from the signed entries
 * alone. Broker orders also store the broker's order id, so they can be
 * reconciled against Alpaca/Tradovate, an independent third party.
 * Limits, stated plainly: a device owner can wipe the log and start a new
 * chain (it would show a new genesis date and key), and the log proves what
 * this device recorded, not what happened elsewhere.
 */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki || Loki.audit) return;
  const LOG_KEY = 'loki_audit_log_v1';
  const DB = 'loki-audit', STORE = 'keys';
  const subtle = window.crypto && window.crypto.subtle;
  const enc = new TextEncoder();
  const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
  const b64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)));
  const unb64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
  /** Canonical JSON: sorted keys, so the same entry always hashes the same. */
  const canon = (v) => (v === null || typeof v !== 'object') ? JSON.stringify(v) : Array.isArray(v) ? `[${v.map(canon).join(',')}]`
    : `{${Object.keys(v).sort().filter((k) => v[k] !== undefined).map((k) => `${JSON.stringify(k)}:${canon(v[k])}`).join(',')}}`;
  const sha = async (text) => hex(await subtle.digest('SHA-256', enc.encode(text)));

  function idb() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB, 1);
      req.onupgradeneeded = () => req.result.createObjectStore(STORE);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }
  async function idbGet(key) { const db = await idb(); return new Promise((res, rej) => { const r = db.transaction(STORE).objectStore(STORE).get(key); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); }); }
  async function idbPut(key, value) { const db = await idb(); return new Promise((res, rej) => { const tx = db.transaction(STORE, 'readwrite'); tx.objectStore(STORE).put(value, key); tx.oncomplete = () => res(); tx.onerror = () => rej(tx.error); }); }

  let keysPromise = null;
  async function deviceKeys() {
    if (!subtle) throw new Error('This browser has no WebCrypto, so the log cannot be signed here.');
    if (!keysPromise) {
      keysPromise = (async () => {
        let pair = await idbGet('signing').catch(() => null);
        if (!pair) {
          pair = await subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign', 'verify']);
          await idbPut('signing', pair);
        }
        const jwk = await subtle.exportKey('jwk', pair.publicKey);
        const publicJwk = { kty: jwk.kty, crv: jwk.crv, x: jwk.x, y: jwk.y };
        return { pair, publicJwk, fingerprint: (await sha(publicJwk.x + publicJwk.y)).slice(0, 16) };
      })();
    }
    return keysPromise;
  }

  const read = () => Loki.store.get(LOG_KEY, []);
  let queue = Promise.resolve();

  /** Append one execution. Resolves to the stored entry. */
  function append(data) {
    queue = queue.then(async () => {
      const keys = await deviceKeys();
      const log = read(), prev = log.length ? log[log.length - 1].hash : 'genesis';
      const body = { seq: log.length + 1, ts: new Date().toISOString(), data, prev, key: keys.fingerprint };
      const hash = await sha(canon(body));
      const sig = b64(await subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, keys.pair.privateKey, enc.encode(hash)));
      const entry = { ...body, hash, sig };
      log.push(entry);
      Loki.store.set(LOG_KEY, log);
      Loki.emit('audit:append', entry);
      return entry;
    }).catch((error) => { console.warn('[loki audit]', error); return null; });
    return queue;
  }

  /** Re-check every hash, link and signature. Works on this device's log or an imported export. */
  async function verify(exported) {
    const log = exported ? exported.entries : read();
    const jwk = exported ? exported.public_key : (await deviceKeys()).publicJwk;
    if (!log.length) return { ok: true, count: 0, note: 'nothing recorded yet' };
    const pub = await subtle.importKey('jwk', { ...jwk, ext: true }, { name: 'ECDSA', namedCurve: 'P-256' }, false, ['verify']);
    let prev = 'genesis';
    for (let i = 0; i < log.length; i++) {
      const e = log[i];
      const body = { seq: e.seq, ts: e.ts, data: e.data, prev: e.prev, key: e.key };
      if (e.seq !== i + 1 || e.prev !== prev) return { ok: false, count: log.length, broken_at: i + 1, why: 'an entry was removed, inserted or reordered' };
      if ((await sha(canon(body))) !== e.hash) return { ok: false, count: log.length, broken_at: i + 1, why: 'an entry was edited after it was recorded' };
      if (!(await subtle.verify({ name: 'ECDSA', hash: 'SHA-256' }, pub, unb64(e.sig), enc.encode(e.hash)))) return { ok: false, count: log.length, broken_at: i + 1, why: 'a signature does not match the device key' };
      prev = e.hash;
    }
    return { ok: true, count: log.length, genesis: log[0].ts, head: log[log.length - 1].hash, fingerprint: log[0].key, metrics: metrics(log) };
  }

  /** Figures computed only from signed entries. */
  function metrics(log = read()) {
    const by = (kind) => log.filter((e) => e.data && e.data.kind === kind);
    const pnl = (rows) => rows.reduce((a, e) => a + (Number.isFinite(e.data.pnl) ? e.data.pnl : 0), 0);
    const settled = log.filter((e) => e.data && Number.isFinite(e.data.pnl));
    return {
      executions: log.filter((e) => e.data && ['buy', 'sell', 'hedge', 'order'].includes(e.data.action)).length,
      paper: by('paper').length, live: by('live').length,
      realised_pnl: pnl(settled), wins: settled.filter((e) => e.data.pnl > 0).length, closed: settled.length,
      venues: [...new Set(log.map((e) => e.data && e.data.venue).filter(Boolean))],
    };
  }

  async function exportLog() {
    const keys = await deviceKeys();
    return { format: 'loki-proof-of-strategy/1', exported_at: new Date().toISOString(), public_key: keys.publicJwk, fingerprint: keys.fingerprint, entries: read() };
  }

  Loki.audit = { append, verify, metrics, export: exportLog, entries: () => read().slice().reverse(), fingerprint: async () => (await deviceKeys()).fingerprint };
})();
