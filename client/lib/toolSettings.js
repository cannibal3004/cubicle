// Helpers for a bot's `tool_settings`:
//   { groups: {id: bool}, toolkits: {slug: bool}, tools: {name: bool} }
// Only explicit `false` disables something; anything absent is enabled, so
// the helpers delete a key when turning it back on.

function section(settings, key) {
  const value = settings && settings[key];
  return value && typeof value === 'object' ? value : {};
}

export function isGroupOn(settings, groupId) {
  return section(settings, 'groups')[groupId] !== false;
}

export function isToolkitOn(settings, slug) {
  return section(settings, 'toolkits')[slug] !== false;
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
export const withToolkit = (settings, slug, on) => withFlag(settings, 'toolkits', slug, on);
export const withTool = (settings, name, on) => withFlag(settings, 'tools', name, on);

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
  const disabled = new Set(catalog.disabled_toolkits || []);
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
      if (!disabled.has(toolkit.slug) && isToolkitOn(settings, toolkit.slug) && isToolOn(settings, tool.name)) on += 1;
    }
  }
  return { on, total };
}
