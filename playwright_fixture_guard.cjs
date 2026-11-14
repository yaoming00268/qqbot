// Offline Playwright fixtures: no child processes or connections to live services.
'use strict';
const net = require('node:net');
const child = require('node:child_process');
const allowed = new Set(JSON.parse(process.env.QQBOT_PLAYWRIGHT_TEST_PORTS || '[]'));
const connect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function (...args) {
  const values = Array.isArray(args[0]) ? args[0] : args;
  const opts = typeof values[0] === 'object' ? values[0] : {
    port: values[0], host: typeof values[1] === 'string' ? values[1] : 'localhost'
  };
  if (!allowed.has(Number(opts.port)) || !['127.0.0.1', 'localhost', '::1'].includes(opts.host || 'localhost'))
    throw new Error('Offline fixture driver may only connect to test-owned loopback ports');
  return connect.apply(this, args);
};
for (const name of ['spawn', 'spawnSync', 'exec', 'execSync', 'execFile', 'execFileSync', 'fork'])
  child[name] = () => { throw new Error('Offline fixture driver cannot launch applications'); };
