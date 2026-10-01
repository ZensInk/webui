#!/usr/bin/env node
/* ZensInk WebUI launcher — finds python3, spawns the pure-stdlib server,
 * waits for /api/status, then opens the browser. Zero npm dependencies. */
'use strict';
const { spawn, exec } = require('child_process');
const net = require('net');
const http = require('http');
const path = require('path');

const args = process.argv.slice(2);
const argVal = (name) => {
  const i = args.indexOf(name);
  return i > -1 && args[i + 1] && !args[i + 1].startsWith('--') ? args[i + 1] : null;
};
const argPort = parseInt(argVal('--port') || process.env.ZENSINK_PORT || '8390', 10);
const noOpen = args.includes('--no-open') || process.env.ZENSINK_NO_OPEN === '1';

const SERVER_PY = path.join(__dirname, '..', 'server.py');
const PY = process.env.ZENSINK_PYTHON || 'python3';

function isFree(port, cb) {
  const s = net.connect(port, '127.0.0.1');
  s.once('connect', () => { s.destroy(); cb(false); });
  s.once('error', () => { s.destroy(); cb(true); });
}

function start(port) {
  const child = spawn(PY, [SERVER_PY, String(port)], { stdio: 'inherit' });
  child.on('error', (e) => {
    console.error(`\u2715 failed to start "${PY}": ${e.message}`);
    console.error('  python3 (3.9+) is required. Or set ZENSINK_PYTHON=/path/to/python3');
    process.exit(1);
  });
  const die = () => { child.kill('SIGTERM'); process.exit(0); };
  process.on('SIGINT', die);
  process.on('SIGTERM', die);
  child.on('exit', (code) => process.exit(code == null ? 1 : code));

  if (!noOpen) waitForUp(port);
}

function waitForUp(port, tries) {
  tries = tries || 0;
  if (tries > 60) return; // ~24s; server prints its own URL anyway
  const req = http.get({ host: '127.0.0.1', port, path: '/api/status', timeout: 1500 }, (res) => {
    res.resume();
    res.on('end', () => openBrowser(port));
  });
  req.on('error', () => setTimeout(() => waitForUp(port, tries + 1), 400));
  req.on('timeout', () => { req.destroy(); setTimeout(() => waitForUp(port, tries + 1), 400); });
}

function openBrowser(port) {
  const url = `http://127.0.0.1:${port}/`;
  const cmd = process.platform === 'darwin' ? `open ${url}`
    : process.platform === 'win32' ? `start "" ${url}`
    : `xdg-open ${url}`;
  exec(cmd, () => {});
}

(function findPort(port, hops) {
  if (hops > 20) { console.error('no free port found after 8390'); process.exit(1); }
  isFree(port, (free) => (free ? start(port) : findPort(port + 1, hops + 1)));
})(argPort, 0);
