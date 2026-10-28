// Sandbox computer driver.
//
// Runs inside the runtime container on an Xvnc display (see start.sh). It
// exposes a token-protected HTTP API for the host API: navigate the browser,
// capture the whole desktop, inject pointer/keyboard input into the desktop,
// run shell commands, list files, and bridge the local VNC server over a
// WebSocket so people can watch or take control of the same desktop.
//
// The browser is not started at boot. Chromium is launched on demand with
// the exact command from the dock's launcher (so the dock recognises its
// windows), and the driver attaches to it over the DevTools port. Whether
// the bot or the person at the desk opened it, there is one browser.

import net from 'node:net';
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { createServer } from 'node:http';
import { createWriteStream, mkdirSync } from 'node:fs';
import { mkdir, readdir, readFile, stat, unlink, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { constants as osConstants } from 'node:os';
import { chromium } from 'playwright';
import { WebSocketServer } from 'ws';
import { createRecorder } from './recorder.mjs';

const execFileAsync = promisify(execFile);
const port = Number(process.env.PORT || 3000);
const token = process.env.COMPUTER_TOKEN || '';
const workspace = path.resolve(process.env.WORKSPACE || '/workspace');
const computerId = process.env.COMPUTER_ID || 'computer-runtime';
const width = Number(process.env.VIEWPORT_WIDTH || 1280);
const height = Number(process.env.VIEWPORT_HEIGHT || 720);
const display = process.env.DISPLAY || ':1';
const vncPort = 5900;
const CDP_PORT = Number(process.env.CHROME_CDP_PORT || 9222);
const CHROME_DESKTOP_FILE = '/usr/share/applications/cubicle-chromium.desktop';

let browser = null;
let context = null;
let page = null;
let launching = null;
let server;
let queue = Promise.resolve();

const desktopEnv = {
  PATH: process.env.PATH || '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
  HOME: process.env.HOME || '/home/pwuser',
  DISPLAY: display,
  LANG: 'C.UTF-8',
  DBUS_SESSION_BUS_ADDRESS: process.env.DBUS_SESSION_BUS_ADDRESS || '',
  XDG_RUNTIME_DIR: process.env.XDG_RUNTIME_DIR || '',
  XDG_CONFIG_HOME: process.env.XDG_CONFIG_HOME || '',
  GSETTINGS_BACKEND: process.env.GSETTINGS_BACKEND || '',
};

function json(response, statusCode, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(statusCode, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(body),
    'cache-control': 'no-store',
  });
  response.end(body);
}

// What a terminal command runs with: the container's environment (which
// carries the user's secrets as variables) plus the desktop settings, minus
// the driver's own token, which a shell has no business holding.
const { COMPUTER_TOKEN: _driverToken, ...inheritedEnv } = process.env;
const terminalEnv = { ...inheritedEnv, ...desktopEnv };

// Why a process could not start. Linux counts every thread against the
// container's pids limit and a browser alone holds a few hundred, so a spawn
// failing with EAGAIN means the computer is full, not that the program is
// broken. Reading the cgroup files needs no new process, so it still works.
async function pidsUsage() {
  const layouts = [
    ['/sys/fs/cgroup/pids.current', '/sys/fs/cgroup/pids.max'],
    ['/sys/fs/cgroup/pids/pids.current', '/sys/fs/cgroup/pids/pids.max'],
  ];
  for (const [current, max] of layouts) {
    try {
      return `${(await readFile(current, 'utf8')).trim()} of ${(await readFile(max, 'utf8')).trim()}`;
    } catch {
      // not this cgroup layout
    }
  }
  return null;
}

function outOfProcessSlots(error) {
  return Boolean(error) && error.code === 'EAGAIN' && typeof error.syscall === 'string' && error.syscall.startsWith('spawn');
}

async function describeError(error) {
  if (outOfProcessSlots(error)) {
    const usage = await pidsUsage();
    return `The computer has no free process slots (${usage ? `${usage} in use` : 'its pids limit is reached'}; every thread counts and the browser holds many), so nothing new can start. Close browser pages or wait for running commands to finish, then try again. The operator can raise COMPUTER_DOCKER_PIDS_LIMIT.`;
  }
  return error instanceof Error ? error.message : String(error);
}

async function errorPayload(error) {
  return { error: await describeError(error) };
}

function authorized(request) {
  return Boolean(token) && request.headers['x-computer-token'] === token;
}

async function readBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  if (!chunks.length) return {};
  const raw = Buffer.concat(chunks).toString('utf8');
  if (raw.length > 1024 * 1024) throw new Error('Request body is too large.');
  try {
    return JSON.parse(raw);
  } catch {
    throw new Error('Request body must be valid JSON.');
  }
}

function enqueue(operation) {
  const next = queue.then(operation, operation);
  queue = next.catch(() => {});
  return next;
}

function assertHttpUrl(value) {
  if (typeof value !== 'string' || value.length > 2048) {
    throw new Error('A browser URL is required and must be at most 2048 characters.');
  }
  const parsed = new URL(value);
  if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) {
    throw new Error('Browser navigation only accepts absolute HTTP(S) URLs.');
  }
  return parsed.toString();
}

// File tools reach the places that persist between starts: /workspace, the
// home directory, and the user's shared folder when the host mounted it
// (SHARED). The shell can go anywhere the user can; these tools are for
// project files, so they stay where the files belong.
const home = path.resolve(process.env.HOME || '/home/pwuser');
const shared = process.env.SHARED ? path.resolve(process.env.SHARED) : null;
const fileRoots = shared ? [workspace, shared, home] : [workspace, home];
const fileRootsText = fileRoots.join(', ').replace(/, ([^,]*)$/, ' or $1');
const MAX_FILE_BYTES = 512 * 1024;
const MAX_WRITE_CHARS = 200000;
const TERMINAL_COMMAND_MAX = 60000;
// One limit for a terminal command and a page navigation, set by the API so
// the two sides agree; the API waits this long plus a margin.
const COMMAND_TIMEOUT_MS = Number(process.env.COMMAND_TIMEOUT_MS) > 0 ? Number(process.env.COMMAND_TIMEOUT_MS) : 60000;
const COMMAND_TIMEOUT_MAX_MS = Math.max(COMMAND_TIMEOUT_MS, Number(process.env.COMMAND_TIMEOUT_MAX_MS) > 0 ? Number(process.env.COMMAND_TIMEOUT_MAX_MS) : 600000);
const NAVIGATE_TIMEOUT_MS = Number(process.env.NAVIGATE_TIMEOUT_MS) > 0 ? Number(process.env.NAVIGATE_TIMEOUT_MS) : 30000;

function workspacePath(value) {
  const requested = typeof value === 'string' && value.trim() ? value.trim() : '/workspace';
  if (requested.length > 512 || !requested.startsWith('/')) {
    throw new Error('Computer file paths must be absolute and at most 512 characters.');
  }
  const resolved = path.resolve(requested);
  const inside = fileRoots.some((root) => {
    const relative = path.relative(root, resolved);
    return !relative.startsWith('..') && !path.isAbsolute(relative);
  });
  if (!inside) {
    throw new Error(`Computer file paths must stay inside ${fileRootsText}.`);
  }
  return resolved;
}

function textStats(content) {
  return { bytes: Buffer.byteLength(content, 'utf8'), lines: content ? content.split('\n').length : 0 };
}

async function readTextFile(requestedPath) {
  const target = workspacePath(requestedPath);
  const info = await stat(target).catch(() => null);
  if (!info) throw new Error(`No file at ${target}.`);
  if (!info.isFile()) throw new Error(`${target} is not a file.`);
  if (info.size > MAX_FILE_BYTES) throw new Error(`${target} is ${info.size} bytes; files over ${MAX_FILE_BYTES} bytes cannot be read with this tool.`);
  const buffer = await readFile(target);
  if (buffer.includes(0)) throw new Error(`${target} is not a text file.`);
  const content = buffer.toString('utf8');
  return { path: target, content, ...textStats(content) };
}

async function writeTextFile(requestedPath, content) {
  const target = workspacePath(requestedPath);
  if (typeof content !== 'string') throw new Error('File content must be text.');
  if (content.length > MAX_WRITE_CHARS) throw new Error(`File content must be at most ${MAX_WRITE_CHARS} characters.`);
  const existing = await stat(target).catch(() => null);
  if (existing && !existing.isFile()) throw new Error(`${target} exists and is not a file.`);
  await mkdir(path.dirname(target), { recursive: true });
  await writeFile(target, content, 'utf8');
  return { path: target, created: !existing, ...textStats(content) };
}

async function editTextFile(requestedPath, oldText, newText) {
  if (typeof oldText !== 'string' || !oldText) throw new Error('old_text is the exact passage to replace and cannot be empty.');
  if (newText == null) newText = '';
  if (typeof newText !== 'string') throw new Error('new_text must be text.');
  const current = await readTextFile(requestedPath);
  const count = current.content.split(oldText).length - 1;
  if (count === 0) throw new Error(`The passage to replace is not in ${current.path}. Read the file and copy it exactly.`);
  if (count > 1) throw new Error(`The passage to replace appears ${count} times in ${current.path}; include more surrounding text so it matches once.`);
  const at = current.content.indexOf(oldText);
  const updated = current.content.slice(0, at) + newText + current.content.slice(at + oldText.length);
  if (updated.length > MAX_WRITE_CHARS) throw new Error(`The edited file would exceed ${MAX_WRITE_CHARS} characters.`);
  await writeFile(current.path, updated, 'utf8');
  const line = current.content.slice(0, at).split('\n').length;
  return { path: current.path, line, replaced: oldText.length, inserted: newText.length, ...textStats(updated) };
}

async function listFiles(requestedPath) {
  const target = workspacePath(requestedPath);
  const entries = await readdir(target, { withFileTypes: true });
  return entries.slice(0, 500).map((entry) => ({
    name: entry.name,
    kind: entry.isDirectory() ? 'directory' : entry.isFile() ? 'file' : 'other',
  }));
}

// What a terminal command runs with, for one call: the computer's own
// environment plus the user's secrets as they are now. The API sends the
// current secrets with every command (`env`) and the names of ones since
// deleted (`unset`), so a secret added or changed after the computer started
// works without a restart. The runtime's own variables cannot be replaced.
const PROTECTED_ENV = new Set(['COMPUTER_TOKEN', 'COMMAND_TIMEOUT_MS', 'COMMAND_TIMEOUT_MAX_MS', 'NAVIGATE_TIMEOUT_MS', 'COMPUTER_ID', 'PORT', 'WORKSPACE', 'SHARED', 'CUBICLE_PACKAGES', 'HOME', 'PATH', 'DISPLAY']);
const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;

function commandEnv(extra, unset) {
  const env = { ...terminalEnv };
  if (Array.isArray(unset)) {
    for (const name of unset) {
      if (typeof name === 'string' && ENV_NAME.test(name) && !PROTECTED_ENV.has(name)) delete env[name];
    }
  }
  if (extra && typeof extra === 'object') {
    for (const [name, value] of Object.entries(extra)) {
      if (ENV_NAME.test(name) && !PROTECTED_ENV.has(name) && typeof value === 'string') env[name] = value;
    }
  }
  return env;
}

// Output longer than OUTPUT_INLINE_BYTES is saved in full to a file under
// the home directory (which survives a stop and start), and the reply carries
// the start and end of each stream with the file's path, so the bot can search
// or read the rest instead of running the command again. A command whose
// output passes OUTPUT_FILE_MAX_BYTES is stopped.
const OUTPUT_INLINE_BYTES = 20000;
const OUTPUT_HEAD_BYTES = 4000;
const OUTPUT_TAIL_BYTES = 8000;
const OUTPUT_FILE_MAX_BYTES = 50 * 1024 * 1024;
const OUTPUT_DIR = path.join(home, '.cubicle-output');
// Saved outputs kept, by count and in all; the oldest go first.
const OUTPUT_FILES_KEPT = 30;
const OUTPUT_DIR_MAX_BYTES = 200 * 1024 * 1024;
// After the shell exits, how long to wait for its output to close. A process
// the command left running in the background (`server &`) keeps the output
// open for as long as it runs; the command itself is finished.
const OUTPUT_GRACE_MS = 1500;

function signalNumber(signal) {
  return (signal && osConstants.signals[signal]) || 15;
}

// One stream's output as the reply shows it: its first bytes and its last ones.
function capture() {
  return { head: [], headSize: 0, tail: [], tailSize: 0, size: 0 };
}

function keep(stream, chunk) {
  stream.size += chunk.length;
  if (stream.headSize < OUTPUT_HEAD_BYTES) {
    const part = chunk.subarray(0, OUTPUT_HEAD_BYTES - stream.headSize);
    stream.head.push(part);
    stream.headSize += part.length;
    chunk = chunk.subarray(part.length);
  }
  if (!chunk.length) return;
  stream.tail.push(chunk);
  stream.tailSize += chunk.length;
  while (stream.tail.length > 1 && stream.tailSize - stream.tail[0].length >= OUTPUT_TAIL_BYTES) {
    stream.tailSize -= stream.tail.shift().length;
  }
}

function shortened(stream) {
  let head = Buffer.concat(stream.head);
  let tail = Buffer.concat(stream.tail);
  if (tail.length > OUTPUT_TAIL_BYTES) tail = tail.subarray(tail.length - OUTPUT_TAIL_BYTES);
  if (stream.size - head.length - tail.length <= 0) return Buffer.concat([head, tail]).toString('utf8');
  // Whole lines on both sides of the gap, unless a line is longer than the room.
  const lastBreak = head.lastIndexOf(10);
  if (lastBreak > head.length / 2) head = head.subarray(0, lastBreak + 1);
  const firstBreak = tail.indexOf(10);
  if (firstBreak !== -1 && firstBreak < tail.length / 2) tail = tail.subarray(firstBreak + 1);
  const skipped = stream.size - head.length - tail.length;
  return `${head.toString('utf8')}[... ${skipped} bytes left out here; all of it is in the output file ...]\n${tail.toString('utf8')}`;
}

function outputPath(command) {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const slug = command.replace(/[^A-Za-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || 'output';
  return path.join(OUTPUT_DIR, `${stamp}-${slug}.log`);
}

// The newest file (the one being written) always stays.
async function pruneOutputs() {
  const names = (await readdir(OUTPUT_DIR)).filter((name) => name.endsWith('.log')).sort().reverse();
  let total = 0;
  for (const [index, name] of names.entries()) {
    const file = path.join(OUTPUT_DIR, name);
    const size = await stat(file).then((info) => info.size, () => 0);
    total += size;
    if (index > 0 && (index >= OUTPUT_FILES_KEPT || total > OUTPUT_DIR_MAX_BYTES)) await unlink(file).catch(() => {});
  }
}

// Run one shell command and report what happened, telling apart: it
// finished; it hit the time limit (we stopped it); something else killed the
// shell (often `pkill -f` matching this very command); it finished but left
// a background process attached to its output; its output passed 50 MB.
function runShell(command, limit, env) {
  return new Promise((resolve, reject) => {
    let child;
    try {
      // Its own process group, so a timeout stops everything it started.
      child = spawn('/bin/sh', ['-lc', command], { cwd: workspace, env, stdio: ['ignore', 'pipe', 'pipe'], detached: true });
    } catch (error) {
      reject(error);
      return;
    }
    // Everything, in the order printed, until it is long enough to go to a file.
    const all = [];
    const out = capture();
    const err = capture();
    let size = 0;
    let lines = 0;
    let file = null;
    let filePath = null;
    let fileError = null;
    let overflow = false;
    let timedOut = false;
    let exit = null;
    let settled = false;
    let grace = null;
    const killGroup = (signal) => {
      try {
        process.kill(-child.pid, signal);
      } catch {
        try { child.kill(signal); } catch { /* gone */ }
      }
    };
    const startFile = () => {
      filePath = outputPath(command);
      try {
        mkdirSync(OUTPUT_DIR, { recursive: true });
        file = createWriteStream(filePath, { mode: 0o600 });
        file.on('error', (error) => { fileError = error.message; });
        for (const entry of all) file.write(entry.chunk);
        pruneOutputs().catch(() => {});
      } catch (error) {
        fileError = error.message;
      }
    };
    const take = (stream) => (chunk) => {
      if (overflow) return;
      size += chunk.length;
      for (let at = chunk.indexOf(10); at !== -1; at = chunk.indexOf(10, at + 1)) lines += 1;
      keep(stream, chunk);
      if (filePath) {
        if (file && !fileError) file.write(chunk);
      } else {
        all.push({ stream, chunk });
        if (size > OUTPUT_INLINE_BYTES) startFile();
      }
      if (size > OUTPUT_FILE_MAX_BYTES) {
        overflow = true;
        killGroup('SIGTERM');
      }
    };
    child.stdout.on('data', take(out));
    child.stderr.on('data', take(err));
    const timer = setTimeout(() => {
      timedOut = true;
      killGroup('SIGTERM');
      setTimeout(() => killGroup('SIGKILL'), 2000).unref();
    }, limit);

    // The reply: all of the output when short; otherwise the start and end
    // of each stream and where the whole of it was saved.
    const deliver = (result) => {
      if (!filePath) {
        resolve(result);
        return;
      }
      const kb = Math.round(size / 1024);
      const saved = fileError
        ? { output_bytes: size, output_lines: lines, output_note: `The output was ${kb} KB (${lines} lines), so only the start and end of each stream are shown; saving all of it failed (${fileError}). Send the output to a file yourself and read the part you need.` }
        : {
            output_file: filePath,
            output_bytes: size,
            output_lines: lines,
            output_note: `The output was ${kb} KB (${lines} lines), so only the start and end of each stream are shown. All of it, stdout and stderr in the order printed, is saved in ${filePath}. Search it (grep -n PATTERN ${filePath}), read a part (sed -n '100,200p' ${filePath}) or its end (tail -n 100 ${filePath}) instead of running the command again.`,
          };
      const done = () => resolve({ ...result, ...saved });
      if (file && !file.destroyed) {
        // 'close' follows the last write, or a failed one.
        file.once('close', done);
        file.end();
      } else {
        done();
      }
    };

    const finish = (how) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (grace) clearTimeout(grace);
      const text = (stream) => (filePath ? shortened(stream) : Buffer.concat(all.filter((entry) => entry.stream === stream).map((entry) => entry.chunk)).toString('utf8'));
      const stdout = text(out);
      let stderr = text(err);
      const code = exit ? exit.code : null;
      const signal = exit ? exit.signal : null;
      if (timedOut) {
        const seconds = Math.round(limit / 1000);
        deliver({
          exit_code: 124,
          stdout,
          stderr: `${stderr}\n[command timed out after ${seconds}s and was stopped, with everything it started; the output above is what it had produced. For a long job, run it with background: true and check its log.]`.trim(),
          timed_out: true,
        });
        return;
      }
      if (overflow) {
        deliver({
          exit_code: code ?? 1,
          stdout,
          stderr: `${stderr}\n[the output passed ${OUTPUT_FILE_MAX_BYTES / 1024 / 1024} MB and the command was stopped; what it printed until then is in the output file. Send output this long to a file of your own, or filter it in the command.]`.trim(),
          output_truncated: true,
        });
        return;
      }
      if (signal) {
        deliver({
          exit_code: 128 + signalNumber(signal),
          stdout,
          stderr: `${stderr}\n[the shell was killed by ${signal} before it finished; this was not a timeout. Something in the command killed it: \`pkill -f\` and \`killall\` also match this command itself when their pattern appears in it. Kill by PID (pgrep, then kill), or use a pattern that does not occur in the command.]`.trim(),
          killed_by: signal,
        });
        return;
      }
      if (how === 'held') {
        stderr = `${stderr}\n[the command finished, but a process it started in the background is still attached to its output and keeps running. Send a background process's output to a file (cmd > file 2>&1 &), or use background: true.]`.trim();
      }
      deliver({ exit_code: code ?? 0, stdout, stderr, ...(how === 'held' ? { background_attached: true } : {}) });
    };

    child.on('error', (error) => {
      clearTimeout(timer);
      if (!settled) {
        settled = true;
        reject(error);
      }
    });
    child.on('exit', (code, signal) => {
      exit = { code, signal };
      grace = setTimeout(() => finish('held'), OUTPUT_GRACE_MS);
    });
    child.on('close', () => finish('closed'));
  });
}

// A job that runs on after the call returns: its output goes to a log file
// and its exit code to a file next to it when it ends. Kept under the home
// directory, which survives a stop and start.
const JOBS_DIR = path.join(home, '.cubicle-jobs');

async function startBackground(command, env) {
  await mkdir(JOBS_DIR, { recursive: true });
  const id = `job-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
  const log = path.join(JOBS_DIR, `${id}.log`);
  const exitFile = path.join(JOBS_DIR, `${id}.exit`);
  const wrapper = '/bin/sh -lc "$CUBICLE_JOB_COMMAND" > "$CUBICLE_JOB_LOG" 2>&1; echo $? > "$CUBICLE_JOB_EXIT"';
  const child = spawn('/bin/sh', ['-c', wrapper], {
    cwd: workspace,
    env: { ...env, CUBICLE_JOB_COMMAND: command, CUBICLE_JOB_LOG: log, CUBICLE_JOB_EXIT: exitFile },
    stdio: 'ignore',
    detached: true,
  });
  await new Promise((resolve, reject) => {
    child.once('spawn', resolve);
    child.once('error', reject);
  });
  child.unref();
  return {
    exit_code: 0,
    background: true,
    job_id: id,
    pid: child.pid,
    log,
    exit_file: exitFile,
    stdout: '',
    stderr: '',
    check: `tail -n 40 ${log}; cat ${exitFile} 2>/dev/null || echo "still running (pid ${child.pid})"`,
    stop: `kill -- -${child.pid}`,
  };
}

async function executeCommand(command, timeoutMs, options = {}) {
  // The caller's limit (the model's timeout_seconds), clamped to the ceiling.
  const requested = Number(timeoutMs);
  const limit = requested > 0 ? Math.min(Math.max(requested, 1000), COMMAND_TIMEOUT_MAX_MS) : COMMAND_TIMEOUT_MS;
  if (typeof command !== 'string' || !command.trim()) throw new Error('A terminal command is required.');
  if (command.length > TERMINAL_COMMAND_MAX) throw new Error(`Terminal commands must be at most ${TERMINAL_COMMAND_MAX} characters.`);
  const env = commandEnv(options.env, options.unset);
  if (options.background) return startBackground(command, env);
  // The shell itself could not start (no free process slots, say): that is
  // a failed action, not a command that ran; runShell rejects with it.
  return runShell(command, limit, env);
}

// ---- desktop capture and input ---------------------------------------------

async function captureDesktop() {
  const file = '/tmp/shot.jpg';
  await execFileAsync('scrot', ['--overwrite', '--quality', '72', file], { env: desktopEnv, timeout: 10000 });
  const data = await readFile(file);
  return data.toString('base64');
}

async function xdotool(args) {
  await execFileAsync('xdotool', args, { env: desktopEnv, timeout: 15000 });
}

// Browser-style key names (what the model and the UI tend to use) to X keysyms.
const KEY_MAP = {
  Enter: 'Return',
  Return: 'Return',
  ArrowUp: 'Up',
  ArrowDown: 'Down',
  ArrowLeft: 'Left',
  ArrowRight: 'Right',
  Backspace: 'BackSpace',
  Escape: 'Escape',
  Esc: 'Escape',
  Tab: 'Tab',
  Delete: 'Delete',
  Home: 'Home',
  End: 'End',
  PageUp: 'Prior',
  PageDown: 'Next',
  Space: 'space',
  ' ': 'space',
  Control: 'ctrl',
  Ctrl: 'ctrl',
  Shift: 'shift',
  Alt: 'alt',
  Meta: 'super',
  Super: 'super',
};

function keyCombo(value) {
  const raw = String(value || '');
  if (!raw || raw.length > 40 || !/^[A-Za-z0-9+_\- ]+$/.test(raw)) {
    throw new Error('A key name such as Enter, Tab, ArrowDown, or ctrl+c is required.');
  }
  return raw
    .split('+')
    .map((part) => KEY_MAP[part] || KEY_MAP[part.trim()] || (part.length === 1 ? part : part.charAt(0).toUpperCase() + part.slice(1)))
    .join('+');
}

function coordinate(value, max, label) {
  const number = Number(value);
  if (!Number.isFinite(number)) throw new Error(`Input ${label} must be a number.`);
  return Math.max(0, Math.min(max - 1, Math.round(number)));
}

async function sendInput(event) {
  if (!event || typeof event !== 'object') throw new Error('Computer input must be a JSON object.');
  const type = String(event.type || '').toLowerCase();
  if (type === 'click') {
    const x = coordinate(event.x, width, 'x');
    const y = coordinate(event.y, height, 'y');
    const button = { left: '1', middle: '2', right: '3' }[event.button || 'left'];
    if (!button) throw new Error('Click button must be left, middle, or right.');
    await xdotool(['mousemove', '--sync', String(x), String(y), 'click', button]);
    return { x, y, button: event.button || 'left' };
  }
  if (type === 'keypress') {
    const combo = keyCombo(event.key);
    await xdotool(['key', '--clearmodifiers', combo]);
    return { key: combo };
  }
  if (type === 'type') {
    const text = String(event.text || '');
    if (!text) throw new Error('Typed input requires text.');
    if (text.length > 2000) throw new Error('Typed input must be at most 2000 characters.');
    await xdotool(['type', '--delay', '12', '--', text]);
    return { characters: text.length };
  }
  if (type === 'scroll') {
    const deltaY = Number(event.deltaY || 0);
    const deltaX = Number(event.deltaX || 0);
    const steps = (delta) => Math.min(20, Math.max(1, Math.round(Math.abs(delta) / 100)));
    if (deltaY) await xdotool(['click', '--repeat', String(steps(deltaY)), '--delay', '20', deltaY > 0 ? '5' : '4']);
    if (deltaX) await xdotool(['click', '--repeat', String(steps(deltaX)), '--delay', '20', deltaX > 0 ? '7' : '6']);
    return { deltaX, deltaY };
  }
  throw new Error('Supported input types are click, keypress, type, and scroll.');
}

// ---- browser ----------------------------------------------------------------

// The launcher's Exec line is the single definition of how Chromium starts.
async function chromeCommand() {
  const text = await readFile(CHROME_DESKTOP_FILE, 'utf8');
  const line = text.split('\n').find((entry) => entry.startsWith('Exec='));
  if (!line) throw new Error('The Chromium launcher has no Exec line.');
  const parts = line.slice('Exec='.length).trim().split(/\s+/).filter((part) => !part.startsWith('%'));
  return { command: parts[0], args: parts.slice(1) };
}

async function cdpReady() {
  try {
    const response = await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`, { signal: AbortSignal.timeout(1000) });
    return response.ok;
  } catch {
    return false;
  }
}

async function launchChrome() {
  const { command, args } = await chromeCommand();
  const child = spawn(command, args, { detached: true, stdio: 'ignore', env: { ...process.env, ...desktopEnv } });
  child.unref();
  for (let attempt = 0; attempt < 150; attempt += 1) {
    if (await cdpReady()) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error('Chromium did not start listening on its DevTools port.');
}

function resetBrowser() {
  browser = null;
  context = null;
  page = null;
}

function trackPages() {
  const pages = context.pages();
  page = pages[pages.length - 1] || null;
  context.on('page', (opened) => {
    page = opened;
    opened.on('close', () => {
      const remaining = context ? context.pages() : [];
      page = remaining[remaining.length - 1] || null;
    });
  });
}

async function ensureBrowser() {
  if (browser && browser.isConnected()) return browser;
  if (launching) return launching;
  launching = (async () => {
    resetBrowser();
    if (!(await cdpReady())) await launchChrome();
    browser = await chromium.connectOverCDP(`http://127.0.0.1:${CDP_PORT}`, { timeout: 15000 });
    browser.on('disconnected', resetBrowser);
    context = browser.contexts()[0] || (await browser.newContext());
    trackPages();
    return browser;
  })();
  try {
    return await launching;
  } finally {
    launching = null;
  }
}

async function ensurePage() {
  await ensureBrowser();
  if (!page || page.isClosed()) {
    const pages = context.pages();
    page = pages[pages.length - 1] || (await context.newPage());
  }
  return page;
}

function browserRunning() {
  return Boolean(browser && browser.isConnected());
}

function currentPage() {
  return browserRunning() && page && !page.isClosed() ? page : null;
}

// Chromium's toplevel window title ends with "Google Chrome for Testing".
const CHROME_WINDOW = 'Google Chrome';

async function raiseBrowser() {
  try {
    await xdotool(['search', '--name', CHROME_WINDOW, 'windowactivate', '--sync', '%1']);
  } catch {
    // The window manager may not be ready; the page still navigates.
  }
}

// The demonstration recorder: steps the person at the desk performs in the
// browser, with a frame of the screen before each click (recorder.mjs).
const recorder = createRecorder({
  ensureBrowser,
  getContext: () => context,
  captureDesktop,
  raiseBrowser,
  screen: { width, height },
});


// ---- MCP servers in the computer -------------------------------------------
// A bot can run MCP servers here, next to its files and packages, instead of
// in the host API. Each is a child process speaking JSON-RPC over stdio; the
// host API talks to it through POST /mcp/<id>, the streamable-HTTP shape its
// MCP client already speaks: a request gets its response back as JSON, a
// notification gets 202. There is no server-to-client stream (GET answers
// 405, which the protocol allows), so a server's own requests are refused.

const MCP_MAX_SERVERS = 8;
const MCP_CALL_TIMEOUT_MS = 120000;
const MCP_STDERR_KEEP = 4000;
const mcpServers = new Map();

function resolveMcpEnv(env) {
  // $NAME and ${NAME} stand for the computer's own environment variables,
  // which is where the user's secrets are; a missing one is a clear error.
  const out = {};
  const missing = [];
  for (const [name, raw] of Object.entries(env || {})) {
    const value = String(raw ?? '').replace(/\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)/g, (_, a, b) => {
      const key = a || b;
      if (process.env[key] === undefined) {
        missing.push(key);
        return '';
      }
      return process.env[key];
    });
    out[name] = value;
  }
  if (missing.length) throw new Error(`Secret${missing.length > 1 ? 's' : ''} ${missing.join(', ')} ${missing.length > 1 ? 'are' : 'is'} not set in this computer.`);
  return out;
}

function mcpStart(body) {
  const id = String(body.id || '').trim();
  if (!/^[A-Za-z0-9_-]{1,40}$/.test(id)) throw new Error('An MCP server id is required.');
  const command = String(body.command || '').trim();
  if (!command) throw new Error('An MCP server needs the command to run.');
  const args = Array.isArray(body.args) ? body.args.map((a) => String(a)) : [];
  const env = resolveMcpEnv(body.env);
  const signature = JSON.stringify([command, args, body.env || {}]);
  const existing = mcpServers.get(id);
  if (existing && existing.alive && existing.signature === signature) {
    return { started: false, running: true, pid: existing.child.pid };
  }
  if (existing) mcpStop(id);
  if (mcpServers.size >= MCP_MAX_SERVERS) throw new Error(`At most ${MCP_MAX_SERVERS} MCP servers can run in a computer.`);

  const child = spawn(command, args, { cwd: workspace, env: { ...terminalEnv, ...env }, stdio: ['pipe', 'pipe', 'pipe'] });
  const entry = { id, signature, child, alive: true, exitCode: null, stderr: '', buffer: '', pending: new Map(), startedAt: Date.now() };
  mcpServers.set(id, entry);
  child.stdout.setEncoding('utf8');
  child.stdout.on('data', (chunk) => {
    entry.buffer += chunk;
    let index;
    while ((index = entry.buffer.indexOf('\n')) >= 0) {
      const line = entry.buffer.slice(0, index).trim();
      entry.buffer = entry.buffer.slice(index + 1);
      if (line) mcpMessageFromChild(entry, line);
    }
  });
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', (chunk) => {
    entry.stderr = (entry.stderr + chunk).slice(-MCP_STDERR_KEEP);
  });
  const ended = (code) => {
    entry.alive = false;
    entry.exitCode = code;
    for (const waiter of entry.pending.values()) waiter.reject(new Error(mcpDeadMessage(entry)));
    entry.pending.clear();
  };
  child.on('error', (error) => {
    entry.stderr = (entry.stderr + `\n${error.message}`).slice(-MCP_STDERR_KEEP);
    ended(-1);
  });
  child.on('exit', (code, signal) => ended(code ?? (signal ? -1 : 0)));
  return { started: true, running: true, pid: child.pid };
}

function mcpDeadMessage(entry) {
  const tail = entry.stderr.trim().split('\n').slice(-5).join('\n');
  return `The MCP server exited${entry.exitCode !== null ? ` with code ${entry.exitCode}` : ''}.${tail ? ` Its last output:\n${tail}` : ''}`;
}

function mcpMessageFromChild(entry, line) {
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    entry.stderr = (entry.stderr + `\n[not JSON-RPC] ${line.slice(0, 300)}`).slice(-MCP_STDERR_KEEP);
    return;
  }
  const hasId = message && Object.prototype.hasOwnProperty.call(message, 'id') && message.id !== null;
  if (hasId && (message.result !== undefined || message.error !== undefined)) {
    const waiter = entry.pending.get(String(message.id));
    if (waiter) {
      entry.pending.delete(String(message.id));
      waiter.resolve(message);
    }
    return;
  }
  if (hasId && message.method) {
    // A request from the server (sampling, roots...): nothing here can answer it.
    entry.child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id: message.id, error: { code: -32601, message: `${message.method} is not available in this computer.` } }) + '\n');
  }
  // Notifications from the server are dropped: there is no stream to carry them.
}

function mcpSend(entry, message) {
  return new Promise((resolve, reject) => {
    if (!entry.alive) {
      reject(new Error(mcpDeadMessage(entry)));
      return;
    }
    const hasId = Object.prototype.hasOwnProperty.call(message, 'id') && message.id !== null;
    const isRequest = hasId && typeof message.method === 'string';
    if (isRequest) {
      const key = String(message.id);
      const timer = setTimeout(() => {
        entry.pending.delete(key);
        reject(new Error(`The MCP server did not answer ${message.method} within ${MCP_CALL_TIMEOUT_MS / 1000}s.`));
      }, MCP_CALL_TIMEOUT_MS);
      entry.pending.set(key, {
        resolve: (reply) => {
          clearTimeout(timer);
          resolve(reply);
        },
        reject: (error) => {
          clearTimeout(timer);
          reject(error);
        },
      });
    }
    entry.child.stdin.write(JSON.stringify(message) + '\n', (error) => {
      if (error) {
        if (isRequest) entry.pending.delete(String(message.id));
        reject(error);
      } else if (!isRequest) {
        resolve(null); // a notification or a client response: nothing comes back
      }
    });
  });
}

function mcpStop(id) {
  const entry = mcpServers.get(id);
  if (!entry) return { stopped: false };
  mcpServers.delete(id);
  if (entry.alive) {
    try {
      entry.child.kill('SIGTERM');
      setTimeout(() => {
        if (entry.alive) entry.child.kill('SIGKILL');
      }, 3000).unref();
    } catch {
      // already gone
    }
  }
  return { stopped: true };
}

function mcpStatuses() {
  const out = {};
  for (const [id, entry] of mcpServers) {
    out[id] = { running: entry.alive, pid: entry.child.pid, exit_code: entry.exitCode, started_at: entry.startedAt, stderr: entry.stderr.slice(-800) };
  }
  return out;
}

// POST /mcp/<id>: one JSON-RPC message in, its response (or 202) out.
async function mcpEndpoint(request, response, body) {
  const id = request.url.slice('/mcp/'.length).split('?')[0];
  const entry = mcpServers.get(id);
  if (!entry) {
    json(response, 404, { error: `No MCP server ${id} is running in this computer.` });
    return;
  }
  if (!entry.alive) {
    json(response, 503, { error: mcpDeadMessage(entry) });
    return;
  }
  const messages = Array.isArray(body) ? body : [body];
  try {
    const replies = [];
    for (const message of messages) {
      if (!message || typeof message !== 'object') throw new Error('Expected a JSON-RPC message.');
      const reply = await mcpSend(entry, message);
      if (reply) replies.push(reply);
    }
    if (!replies.length) {
      response.writeHead(202, { 'content-length': 0, 'cache-control': 'no-store' });
      response.end();
      return;
    }
    json(response, 200, Array.isArray(body) ? replies : replies[0]);
  } catch (error) {
    json(response, entry.alive ? 500 : 503, { error: error.message });
  }
}

// ---- HTTP API ------------------------------------------------------------------

// Routes that do not touch the browser run outside its queue, so a page that
// hangs never holds up a shell command and a long build never holds up a
// screenshot. Returns null for any other route.
async function unqueued(request, body) {
  if (request.method !== 'POST') return null;
  if (request.url === '/mcp/start') return { operation: 'mcp.start', ...mcpStart(body) };
  if (request.url === '/mcp/stop') return { operation: 'mcp.stop', ...mcpStop(String(body.id || '')) };
  if (request.url === '/terminal') {
    return {
      operation: 'terminal.exec',
      ...(await executeCommand(body.command, body.timeout_ms, { env: body.env, unset: body.unset, background: body.background === true })),
    };
  }
  if (request.url === '/files') {
    return { operation: 'files.list', path: workspacePath(body.path), entries: await listFiles(body.path) };
  }
  if (request.url === '/files/read') {
    return { operation: 'files.read', ...(await readTextFile(body.path)) };
  }
  if (request.url === '/files/write') {
    return { operation: 'files.write', ...(await writeTextFile(body.path, body.content)) };
  }
  if (request.url === '/files/edit') {
    return { operation: 'files.edit', ...(await editTextFile(body.path, body.old_text, body.new_text)) };
  }
  return null;
}

async function handle(request, response) {
  if (!authorized(request)) {
    json(response, 401, { error: 'Computer runtime authentication failed.' });
    return;
  }

  try {
    const body = request.method === 'POST' ? await readBody(request) : {};
    if (request.method === 'GET' && request.url === '/mcp/servers') {
      json(response, 200, { computer_id: computerId, servers: mcpStatuses() });
      return;
    }
    if (request.url.startsWith('/mcp/') && !['/mcp/start', '/mcp/stop'].includes(request.url)) {
      if (request.method === 'POST') {
        await mcpEndpoint(request, response, body);
      } else if (request.method === 'DELETE') {
        json(response, 200, { ok: true }); // no sessions to end
      } else {
        response.writeHead(405, { allow: 'POST, DELETE', 'content-length': 0 });
        response.end();
      }
      return;
    }
    if (request.method === 'GET' && request.url === '/health') {
      const current = currentPage();
      json(response, 200, {
        status: 'healthy',
        computer_id: computerId,
        url: current ? current.url() : null,
        browser_running: browserRunning(),
        width,
        height,
        desktop: true,
        vnc: true,
        recording: recorder.status().active,
      });
      return;
    }

    if (request.method === 'GET' && request.url === '/record') {
      json(response, 200, { computer_id: computerId, operation: 'record.status', ...recorder.status() });
      return;
    }

    const direct = await unqueued(request, body);
    if (direct) {
      json(response, 200, { computer_id: computerId, ...direct });
      return;
    }

    const result = await enqueue(async () => {
      if (request.method === 'POST' && request.url === '/navigate') {
        const url = assertHttpUrl(body.url);
        const target = await ensurePage();
        await target.bringToFront();
        await raiseBrowser();
        await target.goto(url, { waitUntil: 'domcontentloaded', timeout: NAVIGATE_TIMEOUT_MS });
        return { operation: 'browser.navigate', url: target.url(), title: await target.title() };
      }

      if (request.method === 'POST' && request.url === '/record/start') {
        return { operation: 'record.start', ...(await recorder.start()) };
      }

      if (request.method === 'POST' && request.url === '/record/stop') {
        return { operation: 'record.stop', ...(await recorder.stop()) };
      }

      if (request.method === 'POST' && request.url === '/screenshot') {
        const current = currentPage();
        return {
          operation: 'screenshot',
          format: 'jpeg',
          width,
          height,
          url: current ? current.url() : null,
          frame_id: `frame-${Date.now()}`,
          data: await captureDesktop(),
        };
      }

      if (request.method === 'POST' && request.url === '/input') {
        const detail = await sendInput(body.event);
        return { operation: 'input', accepted: true, type: String(body.event.type).toLowerCase(), ...detail };
      }

      if (request.method === 'GET' && request.url === '/state') {
        const current = currentPage();
        return {
          operation: 'state',
          browser_running: browserRunning(),
          url: current ? current.url() : null,
          title: current ? await current.title() : null,
        };
      }

      throw new Error('Runtime route not found.');
    });
    json(response, 200, { computer_id: computerId, ...result });
  } catch (error) {
    json(response, 400, await errorPayload(error));
  }
}

// ---- VNC bridge ---------------------------------------------------------------
// GET /vnc with the token upgrades to a WebSocket carrying raw RFB bytes to
// the loopback VNC server. noVNC in the browser speaks exactly this, and the
// host API relays it after checking the user's session.

const wss = new WebSocketServer({ noServer: true, maxPayload: 16 * 1024 * 1024 });

function bridgeVnc(socket) {
  const tcp = net.connect(vncPort, '127.0.0.1');
  tcp.on('data', (chunk) => {
    if (socket.readyState === socket.OPEN) socket.send(chunk);
  });
  tcp.on('close', () => socket.close());
  tcp.on('error', () => socket.close());
  socket.on('message', (message) => {
    if (!tcp.destroyed) tcp.write(message);
  });
  socket.on('close', () => tcp.destroy());
  socket.on('error', () => tcp.destroy());
}

function handleUpgrade(request, socket, head) {
  const url = (request.url || '').split('?')[0];
  if (!authorized(request) || url !== '/vnc') {
    socket.write('HTTP/1.1 401 Unauthorized\r\n\r\n');
    socket.destroy();
    return;
  }
  wss.handleUpgrade(request, socket, head, (ws) => bridgeVnc(ws));
}

// ---- lifecycle -----------------------------------------------------------------

async function main() {
  if (!token) throw new Error('COMPUTER_TOKEN is required.');
  await mkdir(workspace, { recursive: true });
  await stat(workspace);

  server = createServer((request, response) => handle(request, response));
  server.on('upgrade', handleUpgrade);
  server.listen(port, '0.0.0.0', () => {
    console.log(JSON.stringify({ ready: true, computer_id: computerId, port, width, height, desktop: true }));
  });
}

async function shutdown() {
  if (server) await new Promise((resolve) => server.close(resolve));
  if (browser) await browser.close().catch(() => {});
  process.exit(0);
}

process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
