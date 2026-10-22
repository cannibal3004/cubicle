// Demonstration recorder: what the person at the desk does in the browser,
// as steps a model can read.
//
// While the user drives the desktop over VNC, Chromium is still attached to
// the driver over CDP. A small script in every page reports clicks, typing,
// selections and the few keys that matter (Enter, Tab, Escape, shortcuts),
// each with the control it happened on described the way a model would
// describe it: role, label, page. The driver adds navigations it sees, and
// keeps a rolling desktop frame so each step comes with the screen as it
// looked just before the action. Video would cost thousands of tokens per
// second and still leave the model guessing what was clicked; this is a
// few lines per step.
//
// Passwords are never recorded: a step on a password field says only that
// something was typed there.

const MAX_STEPS = 200;
const MAX_KEYFRAMES = 40;
const ROLLING_FRAME_MS = 700;
const FRAME_FRESH_MS = 2500;
const VALUE_MAX = 200;

// Runs in every frame of every page. Reports through the exposed binding
// window.__cubicleRecord; the driver ignores reports while not recording.
export const PAGE_SCRIPT = String.raw`(() => {
  if (window.__cubicleRecorderInstalled) return;
  window.__cubicleRecorderInstalled = true;
  const post = (event) => { try { if (window.__cubicleRecord) window.__cubicleRecord(event); } catch (e) {} };
  const trim = (value, max) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, max || 80);
  const textOf = (el) => trim(el.innerText || el.textContent || '');
  const nameOf = (el) => {
    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria) return trim(aria);
    const by = el.getAttribute && el.getAttribute('aria-labelledby');
    if (by) {
      const text = by.split(/\s+/).map((id) => document.getElementById(id)).filter(Boolean).map(textOf).join(' ');
      if (text) return trim(text);
    }
    if (el.labels && el.labels.length) {
      const text = textOf(el.labels[0]);
      if (text) return text;
    }
    if (el.placeholder) return trim(el.placeholder);
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' && ['submit', 'button', 'reset'].includes((el.type || '').toLowerCase()) && el.value) return trim(el.value);
    if (tag === 'img' && el.alt) return trim(el.alt);
    if (tag === 'select' && el.selectedOptions && el.selectedOptions.length) {
      const label = el.closest('label');
      if (label) return textOf(label).replace(textOf(el.selectedOptions[0]), '').trim() || trim(el.name);
    }
    if (el.title) return trim(el.title);
    if (!['input', 'textarea', 'select'].includes(tag)) {
      const text = textOf(el);
      if (text) return text;
      const img = el.querySelector && el.querySelector('img[alt]');
      if (img && img.alt) return trim(img.alt);
      const labelled = el.querySelector && el.querySelector('[aria-label]');
      if (labelled) return trim(labelled.getAttribute('aria-label'));
    }
    if (el.name) return trim(el.name);
    if (el.id) return trim(el.id);
    return '';
  };
  const roleOf = (el) => {
    const explicit = el.getAttribute && el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return el.href ? 'link' : 'text';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'dropdown';
    if (tag === 'textarea') return 'text area';
    if (tag === 'input') {
      const type = (el.type || 'text').toLowerCase();
      if (['button', 'submit', 'reset'].includes(type)) return 'button';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio button';
      if (type === 'password') return 'password field';
      if (type === 'file') return 'file chooser';
      if (['text', 'email', 'search', 'url', 'tel', 'number'].includes(type)) return 'text field';
      return type + ' field';
    }
    if (el.isContentEditable) return 'editor';
    if (tag === 'option') return 'option';
    if (tag === 'summary') return 'disclosure';
    if (tag === 'label') return 'label';
    if (tag === 'img') return 'image';
    if (/^h[1-6]$/.test(tag)) return 'heading';
    if (tag === 'li') return 'list item';
    if (tag === 'td' || tag === 'th') return 'table cell';
    return tag;
  };
  const INTERACTIVE = 'button, a[href], input, select, textarea, [role="button"], [role="link"], [role="menuitem"], [role="menuitemcheckbox"], [role="tab"], [role="option"], [role="checkbox"], [role="radio"], [role="switch"], [role="combobox"], [role="treeitem"], label, summary, [contenteditable="true"]';
  const interactive = (el) => (el.closest && el.closest(INTERACTIVE)) || el;
  const selectorOf = (el) => {
    if (el.id && /^[A-Za-z][\w-]*$/.test(el.id)) return '#' + el.id;
    const tag = el.tagName.toLowerCase();
    const testId = el.getAttribute && (el.getAttribute('data-testid') || el.getAttribute('data-test-id') || el.getAttribute('data-test'));
    if (testId) return '[data-testid="' + testId + '"]';
    if (el.name) return tag + '[name="' + el.name + '"]';
    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria) return tag + '[aria-label="' + aria + '"]';
    return '';
  };
  const describe = (el) => {
    const target = interactive(el);
    const type = (target.type || '').toLowerCase();
    return {
      tag: target.tagName.toLowerCase(),
      role: roleOf(target),
      name: nameOf(target),
      selector: selectorOf(target),
      secret: type === 'password',
      frame: window !== window.top ? location.href : '',
    };
  };
  // Where the viewport sits on the screen: a click's page coordinates plus
  // this is where it shows on a desktop screenshot.
  const offset = () => {
    let x = window.screenX + Math.max(0, (window.outerWidth - window.innerWidth) / 2);
    let y = window.screenY + Math.max(0, window.outerHeight - window.innerHeight);
    try {
      if (window !== window.top) {
        let frame = window.frameElement;
        if (frame) {
          const box = frame.getBoundingClientRect();
          x += box.left;
          y += box.top;
        }
      }
    } catch (e) {}
    return { x: Math.round(x), y: Math.round(y) };
  };
  const valueOf = (el) => {
    if (el.isContentEditable) return trim(el.textContent, ${VALUE_MAX});
    return trim(el.value, ${VALUE_MAX});
  };
  document.addEventListener('click', (event) => {
    const el = event.target instanceof Element ? event.target : null;
    if (!el) return;
    post({ type: 'click', button: event.button, x: event.clientX, y: event.clientY, offset: offset(), target: describe(el) });
  }, true);
  document.addEventListener('input', (event) => {
    const el = event.target instanceof Element ? event.target : null;
    if (!el) return;
    const tag = el.tagName.toLowerCase();
    if (!(tag === 'input' || tag === 'textarea' || el.isContentEditable)) return;
    const type = (el.type || '').toLowerCase();
    if (['checkbox', 'radio', 'file', 'button', 'submit', 'reset', 'range', 'color'].includes(type)) return;
    const target = describe(el);
    post({ type: 'type', target, value: target.secret ? '' : valueOf(el) });
  }, true);
  document.addEventListener('change', (event) => {
    const el = event.target instanceof Element ? event.target : null;
    if (!el) return;
    const tag = el.tagName.toLowerCase();
    const type = (el.type || '').toLowerCase();
    if (tag === 'select') {
      const chosen = Array.from(el.selectedOptions || []).map(textOf).join(', ');
      post({ type: 'select', target: describe(el), value: trim(chosen, ${VALUE_MAX}) });
    } else if (type === 'checkbox' || type === 'radio') {
      post({ type: 'check', target: describe(el), checked: Boolean(el.checked) });
    } else if (type === 'file') {
      const names = Array.from(el.files || []).map((f) => f.name).join(', ');
      post({ type: 'file', target: describe(el), value: trim(names, ${VALUE_MAX}) });
    }
  }, true);
  document.addEventListener('keydown', (event) => {
    const key = event.key;
    const modifiers = [event.ctrlKey ? 'ctrl' : '', event.altKey ? 'alt' : '', event.metaKey ? 'meta' : ''].filter(Boolean);
    const special = ['Enter', 'Tab', 'Escape'].includes(key);
    if (!special && !(modifiers.length && key.length === 1)) return;
    if (modifiers.length && key.length === 1 && ['Shift', 'Control', 'Alt', 'Meta'].includes(key)) return;
    const el = event.target instanceof Element ? event.target : document.activeElement;
    const combo = modifiers.concat([event.shiftKey && special ? 'shift' : '', key.length === 1 ? key.toLowerCase() : key]).filter(Boolean).join('+');
    post({ type: 'key', key: combo, target: el ? describe(el) : null });
  }, true);
})();`;

export function createRecorder({ ensureBrowser, getContext, captureDesktop, raiseBrowser, screen }) {
  let recording = null;
  const installed = new WeakSet();
  const pagesWatched = new WeakSet();
  let rolling = null; // { data, at }
  let timer = null;
  let capturing = null;

  const elapsed = () => Date.now() - recording.startedAt;

  function describeTarget(target) {
    if (!target) return null;
    return {
      role: String(target.role || ''),
      name: String(target.name || '').slice(0, 80),
      tag: String(target.tag || ''),
      selector: String(target.selector || '').slice(0, 200),
      secret: Boolean(target.secret),
      frame: String(target.frame || '').slice(0, 500),
    };
  }

  async function freshFrame() {
    // The rolling frame is the screen just before the action; only when it
    // is stale (the desktop was idle) is a new one taken.
    if (rolling && Date.now() - rolling.at < FRAME_FRESH_MS) return rolling.data;
    try {
      return await captureDesktop();
    } catch {
      return null;
    }
  }

  function addKeyframe(step, data) {
    if (!data || recording.keyframes.length >= MAX_KEYFRAMES) return;
    recording.keyframes.push({ step: step.n, data, width: screen.width, height: screen.height });
    step.keyframe = recording.keyframes.length - 1;
  }

  function pushStep(step) {
    if (recording.steps.length >= MAX_STEPS) {
      recording.dropped += 1;
      return null;
    }
    step.n = recording.steps.length + 1;
    step.at_ms = elapsed();
    recording.steps.push(step);
    return step;
  }

  function sameField(a, b) {
    if (!a || !b) return false;
    return a.role === b.role && a.name === b.name && a.selector === b.selector && a.frame === b.frame;
  }

  async function onReport(source, event) {
    if (!recording || !event || typeof event !== 'object') return;
    const page = source && source.page;
    const url = page ? page.url() : '';
    const target = describeTarget(event.target);
    const type = String(event.type || '');
    if (type === 'type') {
      // Keystrokes in one field are one step, holding what it finally said.
      const last = recording.steps[recording.steps.length - 1];
      if (last && last.type === 'type' && sameField(last.target, target) && last.url === url) {
        last.value = target.secret ? '' : String(event.value || '').slice(0, VALUE_MAX);
        last.at_ms = elapsed();
        return;
      }
      pushStep({ type, url, target, value: target && target.secret ? '' : String(event.value || '').slice(0, VALUE_MAX) });
      return;
    }
    if (type === 'click') {
      const frame = await freshFrame();
      const step = pushStep({
        type,
        url,
        target,
        button: event.button === 2 ? 'right' : event.button === 1 ? 'middle' : 'left',
        x: Number(event.x) || 0,
        y: Number(event.y) || 0,
        screen_x: Math.round((Number(event.x) || 0) + ((event.offset && Number(event.offset.x)) || 0)),
        screen_y: Math.round((Number(event.y) || 0) + ((event.offset && Number(event.offset.y)) || 0)),
      });
      if (step) addKeyframe(step, frame);
      return;
    }
    if (type === 'select' || type === 'file') {
      pushStep({ type, url, target, value: String(event.value || '').slice(0, VALUE_MAX) });
      return;
    }
    if (type === 'check') {
      pushStep({ type, url, target, checked: Boolean(event.checked) });
      return;
    }
    if (type === 'key') {
      const key = String(event.key || '').slice(0, 40);
      if (!key) return;
      const frame = key === 'Enter' ? await freshFrame() : null;
      const step = pushStep({ type, url, target, key });
      if (step && frame) addKeyframe(step, frame);
    }
  }

  function watchPage(page) {
    if (pagesWatched.has(page)) return;
    pagesWatched.add(page);
    page.on('framenavigated', (frame) => {
      if (!recording || frame !== page.mainFrame()) return;
      const url = frame.url();
      if (!url || url === 'about:blank') return;
      const last = recording.steps[recording.steps.length - 1];
      if (last && last.type === 'navigate' && last.url === url) return;
      pushStep({ type: 'navigate', url });
    });
  }

  async function install() {
    await ensureBrowser();
    const context = getContext();
    if (!installed.has(context)) {
      installed.add(context);
      await context.exposeBinding('__cubicleRecord', (source, event) => onReport(source, event));
      await context.addInitScript(PAGE_SCRIPT);
      context.on('page', (opened) => {
        watchPage(opened);
        if (recording) {
          opened.waitForLoadState('domcontentloaded', { timeout: 10000 }).catch(() => {}).then(() => {
            if (recording) pushStep({ type: 'newtab', url: opened.url() });
          });
        }
      });
    }
    for (const page of context.pages()) {
      watchPage(page);
      for (const frame of page.frames()) {
        await frame.evaluate(PAGE_SCRIPT).catch(() => {}); // a page that is navigating or closed: the init script covers its next document
      }
    }
  }

  async function captureRolling() {
    if (!recording || capturing) return;
    capturing = captureDesktop()
      .then((data) => {
        rolling = { data, at: Date.now() };
      })
      .catch(() => {})
      .finally(() => {
        capturing = null;
      });
    await capturing;
  }

  async function start() {
    if (recording) return { started_at: recording.startedIso, already: true };
    await install();
    await raiseBrowser();
    const now = Date.now();
    recording = { startedAt: now, startedIso: new Date(now).toISOString(), steps: [], keyframes: [], dropped: 0 };
    const context = getContext();
    const page = context.pages()[context.pages().length - 1];
    if (page && page.url() && page.url() !== 'about:blank') recording.steps.push({ n: 1, at_ms: 0, type: 'start', url: page.url() });
    rolling = null;
    await captureRolling();
    timer = setInterval(() => {
      captureRolling();
    }, ROLLING_FRAME_MS);
    return { started_at: recording.startedIso, already: false };
  }

  async function stop() {
    if (!recording) throw new Error('Nothing is being recorded.');
    const current = recording;
    if (timer) clearInterval(timer);
    timer = null;
    if (capturing) await capturing;
    recording = null;
    let final = null;
    try {
      final = await captureDesktop();
    } catch {
      final = rolling ? rolling.data : null;
    }
    if (final && current.keyframes.length < MAX_KEYFRAMES) current.keyframes.push({ step: null, data: final, width: screen.width, height: screen.height });
    rolling = null;
    return {
      started_at: current.startedIso,
      duration_ms: Date.now() - current.startedAt,
      width: screen.width,
      height: screen.height,
      steps: current.steps,
      keyframes: current.keyframes,
      dropped_steps: current.dropped,
    };
  }

  function status() {
    if (!recording) return { active: false, steps: 0 };
    return { active: true, steps: recording.steps.length, keyframes: recording.keyframes.length, started_at: recording.startedIso };
  }

  return { start, stop, status };
}
