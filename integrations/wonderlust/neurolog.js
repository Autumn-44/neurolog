'use strict';

// Request metadata only: never collect URLs, queries, bodies, headers or user IDs.
function createLogger(options = {}) {
  const endpoint = options.endpoint || process.env.NEUROLOG_API_URL || 'http://127.0.0.1:5001/api/ingest';
  let key = options.key || process.env.NEUROLOG_INGEST_KEY;
  const transport = options.fetch || globalThis.fetch;
  const queue = [];
  let busy = false;
  let closed = false;
  let timer;
  let delay = 1000;
  const enabled = process.env.NEUROLOG_ENABLED !== 'false' &&
    (process.env.NODE_ENV !== 'production' || Boolean(process.env.NEUROLOG_API_URL));
  async function post(url, payload, headers = {}) {
    const response = await transport(url, {method: 'POST', redirect: 'error',
      headers: {'Content-Type': 'application/json', ...headers},
      body: JSON.stringify(payload), signal: AbortSignal.timeout(5000)});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }
  async function flush() {
    if (busy || closed || !queue.length) return;
    clearTimeout(timer);
    timer = undefined;
    busy = true;
    try {
      if (!key) {
        const url = new URL(endpoint);
        if (!['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) {
          throw new Error('Remote ingestion requires NEUROLOG_INGEST_KEY');
        }
        key = (await post(new URL('generate-key', url), {app_name: 'wonderlust'})).key;
      }
      while (queue.length && !closed) {
        await post(endpoint, queue[0], {'x-api-key': key});
        queue.shift();
      }
      delay = 1000;
    } catch {
      // Do not print credentials, payloads, or remote response bodies.
      console.warn('NeuroLog delivery unavailable; queued events will retry.');
      timer = setTimeout(flush, delay);
      timer.unref?.();
      delay = Math.min(delay * 2, 30000);
    } finally { busy = false; }
  }
  function log(message, severity = 6) {
    if (!enabled || closed) return;
    if (queue.length >= 1000) {
      console.warn('NeuroLog queue full; new event dropped.');
      return;
    }
    queue.push({source: 'wonderlust', severity_level: severity, message: message.slice(0, 10000)});
    if (!timer) void flush();
  }
  function middleware(req, res, next) {
    const start = performance.now();
    res.once('finish', () => {
      // Route templates replace actual listing/user identifiers. Unmatched paths are omitted.
      const route = typeof req.route?.path === 'string' ? req.route.path : '[unmatched]';
      log(`HTTP ${req.method} route=${route} status=${res.statusCode} duration_ms=${(performance.now() - start).toFixed(1)}`,
        res.statusCode >= 500 ? 3 : res.statusCode >= 400 ? 4 : 6);
    });
    next();
  }
  function errorMiddleware(err, req, res, next) {
    const name = /^[A-Za-z][A-Za-z0-9]{0,60}$/.test(err.name || '') ? err.name : 'Error';
    log(`Request error type=${name} status=${Number(err.status) || 500}`, 3);
    next(err);
  }
  return {log, middleware, errorMiddleware, flush, pending: () => queue.length,
    close() { closed = true; clearTimeout(timer); }};
}
module.exports = {createLogger};
