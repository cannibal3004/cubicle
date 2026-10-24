// Syntax highlighting with highlight.js, limited to the languages the file
// viewers map to (lib/codeLanguages.js) so the chunk stays small. Imported
// dynamically: nothing here is in the main bundle.
import hljs from 'highlight.js/lib/core';
import awk from 'highlight.js/lib/languages/awk';
import bash from 'highlight.js/lib/languages/bash';
import c from 'highlight.js/lib/languages/c';
import cmake from 'highlight.js/lib/languages/cmake';
import cpp from 'highlight.js/lib/languages/cpp';
import csharp from 'highlight.js/lib/languages/csharp';
import css from 'highlight.js/lib/languages/css';
import dart from 'highlight.js/lib/languages/dart';
import diff from 'highlight.js/lib/languages/diff';
import dockerfile from 'highlight.js/lib/languages/dockerfile';
import dos from 'highlight.js/lib/languages/dos';
import elixir from 'highlight.js/lib/languages/elixir';
import erlang from 'highlight.js/lib/languages/erlang';
import go from 'highlight.js/lib/languages/go';
import graphql from 'highlight.js/lib/languages/graphql';
import groovy from 'highlight.js/lib/languages/groovy';
import haskell from 'highlight.js/lib/languages/haskell';
import ini from 'highlight.js/lib/languages/ini';
import java from 'highlight.js/lib/languages/java';
import javascript from 'highlight.js/lib/languages/javascript';
import json from 'highlight.js/lib/languages/json';
import kotlin from 'highlight.js/lib/languages/kotlin';
import latex from 'highlight.js/lib/languages/latex';
import less from 'highlight.js/lib/languages/less';
import lua from 'highlight.js/lib/languages/lua';
import makefile from 'highlight.js/lib/languages/makefile';
import markdown from 'highlight.js/lib/languages/markdown';
import nginx from 'highlight.js/lib/languages/nginx';
import perl from 'highlight.js/lib/languages/perl';
import pgsql from 'highlight.js/lib/languages/pgsql';
import php from 'highlight.js/lib/languages/php';
import plaintext from 'highlight.js/lib/languages/plaintext';
import powershell from 'highlight.js/lib/languages/powershell';
import properties from 'highlight.js/lib/languages/properties';
import protobuf from 'highlight.js/lib/languages/protobuf';
import python from 'highlight.js/lib/languages/python';
import r from 'highlight.js/lib/languages/r';
import ruby from 'highlight.js/lib/languages/ruby';
import rust from 'highlight.js/lib/languages/rust';
import scala from 'highlight.js/lib/languages/scala';
import scss from 'highlight.js/lib/languages/scss';
import shell from 'highlight.js/lib/languages/shell';
import sql from 'highlight.js/lib/languages/sql';
import swift from 'highlight.js/lib/languages/swift';
import typescript from 'highlight.js/lib/languages/typescript';
import vim from 'highlight.js/lib/languages/vim';
import x86asm from 'highlight.js/lib/languages/x86asm';
import xml from 'highlight.js/lib/languages/xml';
import yaml from 'highlight.js/lib/languages/yaml';

const LANGUAGES = {
  awk, bash, c, cmake, cpp, csharp, css, dart, diff, dockerfile, dos, elixir, erlang, go, graphql, groovy, haskell, ini,
  java, javascript, json, kotlin, latex, less, lua, makefile, markdown, nginx, perl, pgsql, php, plaintext, powershell,
  properties, protobuf, python, r, ruby, rust, scala, scss, shell, sql, swift, typescript, vim, x86asm, xml, yaml,
};
for (const [name, definition] of Object.entries(LANGUAGES)) hljs.registerLanguage(name, definition);
// Names a Markdown fence may use for the same languages.
hljs.registerAliases(['sh', 'zsh', 'console'], { languageName: 'bash' });
hljs.registerAliases(['py', 'python3'], { languageName: 'python' });
hljs.registerAliases(['js', 'jsx', 'node'], { languageName: 'javascript' });
hljs.registerAliases(['ts', 'tsx'], { languageName: 'typescript' });
hljs.registerAliases(['yml'], { languageName: 'yaml' });
hljs.registerAliases(['html', 'svg', 'vue'], { languageName: 'xml' });
hljs.registerAliases(['toml'], { languageName: 'ini' });
hljs.registerAliases(['ps', 'ps1', 'pwsh'], { languageName: 'powershell' });
hljs.registerAliases(['postgres', 'postgresql'], { languageName: 'pgsql' });
hljs.registerAliases(['text', 'txt'], { languageName: 'plaintext' });

// Past this size a file is shown without colours: highlighting it would
// hold up the page for little gain.
export const MAX_HIGHLIGHT_CHARS = 300_000;

// HTML with the tokens wrapped in hljs-* spans (highlight.js escapes the
// text itself), or null when the language is unknown or the text too long.
export function highlight(text, language) {
  const source = String(text || '');
  if (!language || language === 'plaintext' || source.length > MAX_HIGHLIGHT_CHARS || !hljs.getLanguage(language)) return null;
  try {
    return hljs.highlight(source, { language, ignoreIllegals: true }).value;
  } catch {
    return null;
  }
}

export function knows(language) {
  return Boolean(language && hljs.getLanguage(language));
}
