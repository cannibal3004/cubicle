// Helpers for a bot's `tool_settings`:
//   { groups: {id: bool}, toolkits: {slug: bool}, tools: {name: bool} }
// Only explicit `false` disables something; anything absent is enabled, so
// the helpers delete a key when turning it back on. Connected apps are the
// exception: an app switched off in Plugins is off by default, and a bot
// gets it only with an explicit `true`. A bot's choice that matches the
// default is not stored, so it follows the default if that changes.

function section(settings, key) {
  const value = settings && settings[key];
  return value && typeof value === 'object' ? value : {};
}

export function isGroupOn(settings, groupId) {
  return section(settings, 'groups')[groupId] !== false;
}

export function isToolkitOn(settings, slug, defaultOn = true) {
  const choice = section(settings, 'toolkits')[slug];
  return choice === undefined ? defaultOn : choice !== false;
}

export function isToolOn(settings, name) {
  return section(settings, 'tools')[name] !== false;
}

function withFlag(settings, key, id, on) {
  const current = { ...section(settings, key) };
  if (on) delete current[id];
  else current[id] = false;
  const next = { ...(settings || {}) };
  if (Object.keys(current).length) next[key] = current;
  else delete next[key];
  return next;
}

export const withGroup = (settings, groupId, on) => withFlag(settings, 'groups', groupId, on);
export function withToolkit(settings, slug, on, defaultOn = true) {
  const current = { ...section(settings, 'toolkits') };
  if (on === defaultOn) delete current[slug];
  else current[slug] = on;
  const next = { ...(settings || {}) };
  if (Object.keys(current).length) next.toolkits = current;
  else delete next.toolkits;
  return next;
}

export const withTool = (settings, name, on) => withFlag(settings, 'tools', name, on);

// Whether a connected app's tools are on for bots that have not chosen.
export const toolkitDefaultOn = (catalog, slug) => !(catalog?.disabled_toolkits || []).includes(slug);

// A bot's `trusted_tools`: names that run without an approval card.
export function isTrusted(trusted, name) {
  return Array.isArray(trusted) && trusted.includes(name);
}

export function withTrusted(trusted, name, on) {
  const current = Array.isArray(trusted) ? trusted.filter((item) => item !== name) : [];
  return on ? [...current, name] : current;
}

// Count what the model will actually be offered for this bot.
export function countEnabled(catalog, settings) {
  if (!catalog) return { on: 0, total: 0 };
  let on = 0;
  let total = 0;
  for (const group of catalog.groups || []) {
    for (const tool of group.tools) {
      total += 1;
      if (isGroupOn(settings, group.id) && isToolOn(settings, tool.name)) on += 1;
    }
  }
  for (const toolkit of catalog.toolkits || []) {
    for (const tool of toolkit.tools) {
      total += 1;
      if (isToolkitOn(settings, toolkit.slug, toolkitDefaultOn(catalog, toolkit.slug)) && isToolOn(settings, tool.name)) on += 1;
    }
  }
  return { on, total };
}
