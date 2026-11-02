const {test} = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const {createLogger} = require('./neurolog');
test('request logging excludes private request data and records actual status', async () => {
  const received = [];
  const logger = createLogger({key: 'test', fetch: async (url, options) => {
    received.push(JSON.parse(options.body));
    return {ok: true, json: async () => ({})};
  }});
  const res = new EventEmitter(); res.statusCode = 503;
  logger.middleware({method: 'GET', route: {path: '/:id'}, url: '/private?token=secret', body: {password: 'secret'}}, res, () => {});
  res.emit('finish');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(received[0].severity_level, 3);
  assert.match(received[0].message, /status=503/);
  assert.doesNotMatch(JSON.stringify(received), /private|secret|password/);
  logger.close();
});
test('failed delivery retains events and recovers without blocking request', async () => {
  let fail = true;
  const logger = createLogger({key: 'test', fetch: async () => {
    if (fail) throw new Error('offline');
    return {ok: true, json: async () => ({})};
  }});
  logger.log('Database connection failed', 3);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(logger.pending(), 1);
  fail = false;
  await logger.flush();
  assert.equal(logger.pending(), 0);
  logger.close();
});
