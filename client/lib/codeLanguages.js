// Which language a file is written in, from its name or its first line.
// Kept apart from the highlighter itself (lib/highlight.js), which is large
// and loaded only when a code file is opened; the file viewers use this
// small table to decide what can be previewed as text at all.

const BY_EXTENSION = {
  sh: 'bash', bash: 'bash', zsh: 'bash', ksh: 'bash', bashrc: 'bash', profile: 'bash', env: 'bash',
  py: 'python', pyw: 'python', pyi: 'python', ipy: 'python',
  js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'javascript',
  ts: 'typescript', tsx: 'typescript', mts: 'typescript', cts: 'typescript',
  json: 'json', jsonc: 'json', json5: 'json', ipynb: 'json', webmanifest: 'json', map: 'json',
  yaml: 'yaml', yml: 'yaml',
  toml: 'ini', ini: 'ini', cfg: 'ini', conf: 'ini', service: 'ini', desktop: 'ini', editorconfig: 'ini', gitconfig: 'ini',
  properties: 'properties',
  html: 'xml', htm: 'xml', xml: 'xml', svg: 'xml', xhtml: 'xml', plist: 'xml', csproj: 'xml', xsd: 'xml', xsl: 'xml', vue: 'xml',
  css: 'css', scss: 'scss', sass: 'scss', less: 'less',
  md: 'markdown', markdown: 'markdown', mdx: 'markdown',
  sql: 'sql', psql: 'pgsql', pgsql: 'pgsql',
  go: 'go', rs: 'rust', java: 'java', kt: 'kotlin', kts: 'kotlin', scala: 'scala', groovy: 'groovy', gradle: 'groovy',
  c: 'c', h: 'c', cc: 'cpp', cpp: 'cpp', cxx: 'cpp', hpp: 'cpp', hh: 'cpp', hxx: 'cpp', ino: 'cpp',
  cs: 'csharp', php: 'php', rb: 'ruby', rake: 'ruby', gemspec: 'ruby', swift: 'swift', dart: 'dart',
  lua: 'lua', pl: 'perl', pm: 'perl', r: 'r', ex: 'elixir', exs: 'elixir', erl: 'erlang', hs: 'haskell',
  ps1: 'powershell', psm1: 'powershell', psd1: 'powershell', bat: 'dos', cmd: 'dos',
  diff: 'diff', patch: 'diff',
  graphql: 'graphql', gql: 'graphql', proto: 'protobuf', awk: 'awk', vim: 'vim', tex: 'latex', asm: 'x86asm', s: 'x86asm',
  cmake: 'cmake', mk: 'makefile', mak: 'makefile', dockerfile: 'dockerfile', containerfile: 'dockerfile', nginx: 'nginx',
  txt: 'plaintext', log: 'plaintext', csv: 'plaintext', tsv: 'plaintext', text: 'plaintext', lock: 'plaintext', out: 'plaintext',
};

// Whole file names that say what they are.
const BY_NAME = {
  dockerfile: 'dockerfile', containerfile: 'dockerfile', makefile: 'makefile', gnumakefile: 'makefile', cmakelists: 'cmake',
  'cmakelists.txt': 'cmake', gemfile: 'ruby', rakefile: 'ruby', vagrantfile: 'ruby', procfile: 'bash', jenkinsfile: 'groovy',
  '.bashrc': 'bash', '.bash_profile': 'bash', '.profile': 'bash', '.zshrc': 'bash', '.env': 'bash', '.gitignore': 'bash',
  '.dockerignore': 'bash', '.gitconfig': 'ini', '.editorconfig': 'ini', '.npmrc': 'ini', 'nginx.conf': 'nginx',
  'requirements.txt': 'plaintext', license: 'plaintext', readme: 'plaintext', authors: 'plaintext', changelog: 'plaintext',
};

// Interpreters on a "#!" first line.
const BY_SHEBANG = [
  [/\b(ba|z|k|da)?sh\b/, 'bash'],
  [/\bpython[0-9.]*\b/, 'python'],
  [/\b(node|deno|bun)\b/, 'javascript'],
  [/\bruby\b/, 'ruby'],
  [/\bperl\b/, 'perl'],
  [/\bphp\b/, 'php'],
  [/\blua\b/, 'lua'],
  [/\bRscript\b/, 'r'],
  [/\bpwsh\b/, 'powershell'],
];

function baseName(name) {
  return String(name || '').split('/').pop().toLowerCase();
}

// The language for a file name, or null when the name says nothing.
export function languageForName(name) {
  const base = baseName(name);
  if (!base) return null;
  if (BY_NAME[base]) return BY_NAME[base];
  if (base.startsWith('dockerfile.') || base.endsWith('.dockerfile')) return 'dockerfile';
  if (base.startsWith('.env.')) return 'bash';
  const dot = base.lastIndexOf('.');
  if (dot < 0) return null;
  return BY_EXTENSION[base.slice(dot + 1)] || null;
}

// The language for a file, from its name and (for a script without an
// extension) its first line. 'plaintext' when nothing says otherwise.
export function languageFor(name, text = '') {
  const named = languageForName(name);
  if (named) return named;
  const first = String(text || '').slice(0, 200).split('\n', 1)[0];
  if (first.startsWith('#!')) {
    for (const [pattern, language] of BY_SHEBANG) if (pattern.test(first)) return language;
  }
  return 'plaintext';
}

// A file with no extension (a script in bin/, a LICENSE): shown as text
// unless its bytes turn out to be binary (see looksBinary).
export function hasNoExtension(name) {
  const base = baseName(name);
  return Boolean(base) && !base.slice(1).includes('.');
}

// NUL bytes near the start: an executable or other binary, not text.
export function looksBinary(text) {
  return String(text || '').slice(0, 8000).includes('\u0000');
}

// Whether a file name is one of the text or code formats the viewers show.
export function isTextName(name) {
  return languageForName(name) !== null;
}
