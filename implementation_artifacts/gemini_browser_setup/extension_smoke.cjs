/* Run the shipped service worker against the real local connector with fake cookies. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function main() {
  const link = JSON.parse(fs.readFileSync(0, 'utf8'));
  global.crypto = require('node:crypto').webcrypto;
  const extensionId = 'a'.repeat(32);
  const handlers = {};
  const storage = {};
  let ts = 'extension-fixture-ts', gets = 0, posts = 0;
  global.chrome = {
    runtime: {id: extensionId, onMessage: {addListener: fn => handlers.message = fn},
      onStartup: {addListener: fn => handlers.startup = fn}},
    cookies: {
      get: async params => {
        assert.equal(params.url, 'https://gemini.google.com/');
        assert.equal(params.storeId, 'fixture-store');
        assert.ok(['__Secure-1PSID', '__Secure-1PSIDTS'].includes(params.name));
        gets++;
        return {name: params.name, value: params.name.endsWith('TS') ? ts : 'extension-fixture-psid',
          domain: '.google.com', path: '/', expirationDate: Math.floor(Date.now() / 1000) + 3600};
      },
      getAllCookieStores: async () => [{id: 'fixture-store', tabIds: [9]}],
      onChanged: {addListener: fn => handlers.changed = fn}
    },
    storage: {local: {
      set: async value => Object.assign(storage, value),
      get: async () => storage
    }},
    alarms: {create: async () => {}, onAlarm: {addListener: fn => handlers.alarm = fn}}
  };
  const nativeFetch = global.fetch;
  global.fetch = async (...args) => { posts++; return nativeFetch(...args); };
  vm.runInThisContext(fs.readFileSync(path.resolve(__dirname, '../../browser_extensions/gemini_connector/background.js'), 'utf8'));
  const message = {type: 'qqbot.gemini.connect', ...link};
  const sender = {id: extensionId, tab: {id: 9, incognito: false},
    url: link.endpoint + '/gemini-browser-link#' + link.token};
  assert.equal(handlers.message(message, {...sender, url: 'https://remote.example/'}, () => {}), false);
  assert.equal(handlers.message(message, {...sender, tab: {id: 9, incognito: true}}, () => {}), false);
  assert.equal(handlers.message({...message, endpoint: 'http://remote.example:1234'}, sender, () => {}), false);
  assert.equal(gets, 0);
  const result = await new Promise(resolve => assert.equal(handlers.message(message, sender, resolve), true));
  assert.equal(result.ok, true);
  assert.ok(!JSON.stringify(result).includes('extension-fixture-psid'));
  assert.ok(!JSON.stringify(storage).includes('extension-fixture-psid'));
  ts = 'extension-fixture-rotated-ts';
  await refresh();
  assert.equal(posts, 2);
  assert.equal(gets, 4);
  const content = fs.readFileSync(path.resolve(__dirname, '../../browser_extensions/gemini_connector/connect.js'), 'utf8');
  let sent = 0;
  const status = {textContent: ''};
  vm.runInNewContext(content, {
    location: {hostname: '127.0.0.1', pathname: '/gemini-browser-link', hash: '#' + link.token, origin: link.endpoint},
    document: {querySelector: () => true, getElementById: () => status},
    chrome: {runtime: {sendMessage: (value, callback) => {
      sent++;
      assert.equal(value.token, link.token);
      callback({ok: true, message: 'Connected'});
    }}}
  });
  assert.equal(sent, 1);
  assert.equal(status.textContent, 'Connected');
  process.stdout.write(JSON.stringify({passed: true, handshake: true, rotation: true, content_script: true, posts}));
}
main().catch(() => { process.stderr.write('Browser extension smoke failed.'); process.exitCode = 1; });
