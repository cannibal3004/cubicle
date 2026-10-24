"""What a shell command actually runs, and the per-bot rules that let it run unasked.

The approval card used to show "Run in the computer: bash -c ..." and the
user approved blind. This module reads the script the model wrote the way
a shell would, enough to answer two questions: which programs does it run,
and does every one of them match a rule the user has already allowed for
this bot?

Parsing is deliberately conservative. Quotes and escapes are honoured so a
";" inside a string does not split a command; wrappers that only run another
command (sh -c, bash -lc, env, nohup, timeout, time, exec, xargs) are looked
through so the rule applies to what finally runs; command substitution and
subshells are parsed recursively so every piece is checked. Anything the
parser cannot read with confidence (heredocs, eval, a variable or glob as
the program, sudo) is reported as unsupported, and an unsupported script
always asks.

A rule is a word prefix: "git status" matches `git status -sb`, "npm test"
matches `npm test -- --watch`, "cat" matches any cat. A script is allowed
only when every simple command in it matches some rule.

Loops and conditionals are read as such: in `for h in a b; do ssh $h uptime;
done` the commands are ssh (and uptime is its argument), not for/do/done.
A few shell builtins that change nothing outside the shell (cd, pwd, test,
read, true, sleep...) need no rule. Setting PATH, LD_PRELOAD and the like
anywhere in a script always asks: it changes which program a name runs,
so `PATH=/tmp ls` must not pass a rule for ls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

MAX_RULES = 500
MAX_RULE_CHARS = 200
_RULE_WORD = re.compile(r"^[^\s]+$")

# Wrappers whose job is to run the command that follows them.
_PASS_THROUGH = {"nohup", "time", "exec", "command", "builtin", "stdbuf", "nice", "ionice", "unbuffer", "caffeinate"}
_PASS_THROUGH_WITH_FLAGS = {"nohup", "time", "exec", "command", "builtin", "nice", "ionice", "stdbuf"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
# Programs whose first word is a subcommand worth a rule of its own ("git status"
# rather than all of git). Anything else is offered as the bare program.
_SUBCOMMAND_PROGRAMS = {
    "git", "gh", "npm", "npx", "pnpm", "yarn", "bun", "pip", "pip3", "uv", "poetry", "pipx", "conda", "docker", "podman",
    "kubectl", "helm", "terraform", "cargo", "rustup", "go", "dotnet", "make", "apt", "apt-get", "brew", "gem", "bundle",
    "rails", "composer", "flutter", "az", "aws", "gcloud", "systemctl", "service", "pm2", "rclone", "gradle", "mvn",
}
# Never looked through, never matched by a broad rule by accident.
_UNSUPPORTED_PROGRAMS = {"eval", "sudo", "su", "doas", "source", "."}
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Variables that decide which program or code a name runs.
_PROGRAM_VARIABLES = {
    "PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "BASH_ENV", "ENV", "IFS", "SHELLOPTS", "BASHOPTS",
    "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "NODE_OPTIONS", "NODE_PATH", "PERL5LIB", "PERL5OPT", "RUBYLIB", "RUBYOPT",
    "GIT_EXEC_PATH", "GIT_SSH", "GIT_SSH_COMMAND", "PROMPT_COMMAND",
}
# Shell words that open or continue a loop or conditional; what follows them
# is a command (`then rm x`). The closing ones stand alone. `for`/`select`
# take a list of words, which are data, not commands.
_LEADING_KEYWORDS = {"if", "then", "else", "elif", "while", "until", "do", "!", "{"}
_CLOSING_KEYWORDS = {"done", "fi", "}"}
_LIST_KEYWORDS = {"for", "select"}
# Builtins that change nothing outside the shell: no rule needed.
_HARMLESS_BUILTINS = {
    "cd", "pwd", "true", "false", ":", "[", "[[", "test", "read", "sleep", "break", "continue", "exit",
    "return", "wait", "shift", "set", "export", "local", "declare", "typeset", "readonly", "unset",
}
# A bare redirection operator takes the next word as its target; one with the
# target attached (">file", "2>&1") is complete on its own.
_REDIRECT_NEEDS_TARGET = re.compile(r"^(\d*>>?|<|&>>?|<<<)$")
_REDIRECT_COMPLETE = re.compile(r"^(\d*>&\d+|\d*<&\d+|&>>?\S+|\d*>>?\S+|<\S+|<<<\S+)$")


class ScriptError(ValueError):
    """The script cannot be read with confidence; the card must ask."""


@dataclass
class SimpleCommand:
    argv: List[str]
    # A harmless builtin (cd, test, read...): allowed without a rule, never offered as one.
    implicit: bool = False

    @property
    def program(self) -> str:
        return self.argv[0] if self.argv else ""

    @property
    def shape(self) -> str:
        """The rule the card offers: the program, plus its subcommand for tools that have them."""
        if not self.argv:
            return ""
        base = self.argv[0].rsplit("/", 1)[-1]
        if base in _SUBCOMMAND_PROGRAMS and len(self.argv) > 1 and re.fullmatch(r"[a-z][a-z0-9:-]*", self.argv[1]):
            return f"{self.argv[0]} {self.argv[1]}"
        return self.argv[0]


@dataclass
class ParsedScript:
    commands: List[SimpleCommand] = field(default_factory=list)
    unsupported: Optional[str] = None  # why the script cannot be judged

    @property
    def shapes(self) -> List[str]:
        seen: List[str] = []
        for command in self.commands:
            if command.implicit:
                continue
            if command.shape and command.shape not in seen:
                seen.append(command.shape)
        return seen


# ---- tokenizing -----------------------------------------------------------------------------

_OPERATORS = ("&&", "||", "|&", ";;", ";", "|", "&", "\n", "(", ")")


def _tokenize(script: str) -> List[Tuple[str, str]]:
    """Split into ("word", text) and ("op", text) tokens, honouring quotes.

    A word keeps its quotes removed. Command substitution `$(...)` and
    backticks are kept inside the word as ("subst", inner) markers handled by
    the caller: the word's text gets the placeholder, and the substitution's
    script is parsed on its own.
    """
    tokens: List[Tuple[str, str]] = []
    i, n = 0, len(script)
    word: List[str] = []
    have_word = False
    substitutions: List[str] = []

    def flush():
        nonlocal word, have_word
        if have_word:
            tokens.append(("word", "".join(word)))
        word, have_word = [], False

    while i < n:
        c = script[i]
        if c == "#" and not have_word:
            # A comment runs to the end of the line.
            while i < n and script[i] != "\n":
                i += 1
            continue
        if c == "\\":
            if i + 1 < n:
                if script[i + 1] == "\n":
                    i += 2  # line continuation
                    continue
                word.append(script[i + 1])
                have_word = True
                i += 2
                continue
            raise ScriptError("a trailing backslash")
        if c == "'":
            end = script.find("'", i + 1)
            if end == -1:
                raise ScriptError("an unterminated single quote")
            word.append(script[i + 1:end])
            have_word = True
            i = end + 1
            continue
        if c == '"':
            i += 1
            buf: List[str] = []
            while i < n and script[i] != '"':
                if script[i] == "\\" and i + 1 < n and script[i + 1] in '"\\$`\n':
                    buf.append(script[i + 1])
                    i += 2
                    continue
                if script[i] == "$" and i + 1 < n and script[i + 1] == "(":
                    inner, i = _read_balanced(script, i + 1, "(", ")")
                    substitutions.append(inner)
                    buf.append(f"\x00SUBST{len(substitutions) - 1}\x00")
                    continue
                if script[i] == "`":
                    end = script.find("`", i + 1)
                    if end == -1:
                        raise ScriptError("an unterminated backtick")
                    substitutions.append(script[i + 1:end])
                    buf.append(f"\x00SUBST{len(substitutions) - 1}\x00")
                    i = end + 1
                    continue
                buf.append(script[i])
                i += 1
            if i >= n:
                raise ScriptError("an unterminated double quote")
            word.append("".join(buf))
            have_word = True
            i += 1
            continue
        if c == "$" and i + 1 < n and script[i + 1] == "(":
            if i + 2 < n and script[i + 2] == "(":
                raise ScriptError("arithmetic expansion")
            inner, i = _read_balanced(script, i + 1, "(", ")")
            substitutions.append(inner)
            word.append(f"\x00SUBST{len(substitutions) - 1}\x00")
            have_word = True
            continue
        if c == "`":
            end = script.find("`", i + 1)
            if end == -1:
                raise ScriptError("an unterminated backtick")
            substitutions.append(script[i + 1:end])
            word.append(f"\x00SUBST{len(substitutions) - 1}\x00")
            have_word = True
            i = end + 1
            continue
        if c == "<" and script.startswith("<<", i) and not script.startswith("<<<", i):
            raise ScriptError("a heredoc")
        if c == "$" and i + 1 < n and script[i + 1] == "{":
            # ${VAR}: a parameter, kept whole inside the word.
            inner, i = _read_balanced(script, i + 1, "{", "}")
            word.append("${" + inner + "}")
            have_word = True
            continue
        if c == "&" and ((have_word and word and word[-1] in "<>") or script.startswith("&>", i)):
            # Part of a redirection ("2>&1", "&>file"), not a background operator.
            word.append(c)
            have_word = True
            i += 1
            continue
        if c in " \t":
            flush()
            i += 1
            continue
        matched = next((op for op in _OPERATORS if script.startswith(op, i)), None)
        if matched is not None:
            flush()
            tokens.append(("op", matched))
            i += len(matched)
            continue
        word.append(c)
        have_word = True
        i += 1
    flush()
    tokens.append(("subst", "\x00".join(substitutions)))  # carried to the caller
    return tokens


def _read_balanced(script: str, start: int, open_ch: str, close_ch: str) -> Tuple[str, int]:
    """script[start] is open_ch; return (inner text, index after the close)."""
    depth = 0
    i = start
    in_single = in_double = False
    while i < len(script):
        c = script[i]
        if in_single:
            if c == "'":
                in_single = False
        elif in_double:
            if c == "\\":
                i += 1
            elif c == '"':
                in_double = False
        elif c == "\\":
            i += 1
        elif c == "'":
            in_single = True
        elif c == '"':
            in_double = True
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return script[start + 1:i], i + 1
        i += 1
    raise ScriptError("an unbalanced substitution")


# ---- parsing ------------------------------------------------------------------------------


def parse_script(script: str, *, _depth: int = 0) -> ParsedScript:
    """Every simple command the script runs, or why it cannot be read."""
    if not isinstance(script, str) or not script.strip():
        return ParsedScript(unsupported="an empty command")
    if _depth > 6:
        return ParsedScript(unsupported="nesting too deep")
    try:
        tokens = _tokenize(script)
    except ScriptError as exc:
        return ParsedScript(unsupported=str(exc))
    substitutions = tokens.pop()[1].split("\x00") if tokens and tokens[-1][0] == "subst" else []
    if substitutions == [""]:
        substitutions = []

    parsed = ParsedScript()
    current: List[str] = []

    def close_command():
        nonlocal current
        if not current:
            return
        try:
            for command in _simple_commands(current, _depth):
                parsed.commands.append(command)
        except ScriptError as exc:
            parsed.unsupported = parsed.unsupported or str(exc)
        current = []

    for kind, text in tokens:
        if kind == "op":
            close_command()
            continue
        current.append(text)
    close_command()

    # Whatever a substitution runs is part of the script too.
    for inner in substitutions:
        sub = parse_script(inner, _depth=_depth + 1)
        if sub.unsupported:
            parsed.unsupported = parsed.unsupported or f"in a substitution: {sub.unsupported}"
        parsed.commands.extend(sub.commands)

    if parsed.unsupported is None and not parsed.commands:
        parsed.unsupported = "no command"
    return parsed


def _check_assignment(word: str) -> None:
    name = word.split("=", 1)[0]
    if name in _PROGRAM_VARIABLES:
        raise ScriptError(f"it sets {name}, which changes what programs run")


def _simple_commands(words: List[str], depth: int) -> Iterable[SimpleCommand]:
    """One simple command's words, with wrappers looked through."""
    words = list(words)
    # Loop and conditional syntax: keep the command a keyword introduces.
    while words and words[0] in _LEADING_KEYWORDS:
        words = words[1:]
    if not words:
        return []
    if words[0] == "case" or words[0] == "esac" or words[0] == "function":
        raise ScriptError(f"{words[0]} statements are not read by the rules")
    if words[0] in _CLOSING_KEYWORDS:
        return []  # done, fi, } (and any redirection of the whole block)
    if words[0] in _LIST_KEYWORDS:
        return []  # for NAME in WORDS: the words are data (substitutions are parsed apart)
    for word in words:
        if _ENV_ASSIGNMENT.match(word):
            _check_assignment(word)
    argv: List[str] = []
    skip_next = False
    for word in words:
        if skip_next:
            skip_next = False
            continue
        if _REDIRECT_NEEDS_TARGET.fullmatch(word):
            skip_next = True  # the redirection's target
            continue
        if _REDIRECT_COMPLETE.fullmatch(word):
            continue  # ">file", "2>&1"
        if not argv and (word in ("{", "}", "!") or _ENV_ASSIGNMENT.match(word)):
            continue  # grouping braces, negation, FOO=bar before the program
        if word in ("{", "}") and argv:
            continue
        argv.append(word)
    if not argv:
        return []
    program = argv[0]
    if program not in ("[", "[[") and ("\x00SUBST" in program or program.startswith("$") or any(ch in program for ch in "*?[") or "~" in program):
        raise ScriptError(f"the program is not a plain name ({program[:40]})")
    if program in _UNSUPPORTED_PROGRAMS:
        raise ScriptError(f"{program} is never allowed by rule")
    base = program.rsplit("/", 1)[-1]
    # Wrappers: the rule is about what finally runs.
    if base in _PASS_THROUGH:
        rest = argv[1:]
        if base in _PASS_THROUGH_WITH_FLAGS:
            while rest and rest[0].startswith("-"):
                rest = rest[1:]
        return _simple_commands(rest, depth) if rest else []
    if base == "env":
        rest = argv[1:]
        while rest and (rest[0].startswith("-") or _ENV_ASSIGNMENT.match(rest[0])):
            if _ENV_ASSIGNMENT.match(rest[0]):
                _check_assignment(rest[0])
            rest = rest[1:]
        return _simple_commands(rest, depth) if rest else []
    if base == "timeout":
        rest = argv[1:]
        while rest and rest[0].startswith("-"):
            rest = rest[1:]
        rest = rest[1:]  # the duration
        return _simple_commands(rest, depth) if rest else []
    if base == "xargs":
        rest = argv[1:]
        while rest and rest[0].startswith("-"):
            rest = rest[1:]
        return [SimpleCommand(argv=["xargs"])] + (list(_simple_commands(rest, depth)) if rest else [])
    if base in _SHELLS:
        # sh -c "script", bash -lc 'script': parse the script itself.
        rest = argv[1:]
        flags = ""
        while rest and rest[0].startswith("-"):
            flags += rest[0]
            rest = rest[1:]
        if "c" in flags and rest:
            inner = parse_script(rest[0], _depth=depth + 1)
            if inner.unsupported:
                raise ScriptError(f"in {base} -c: {inner.unsupported}")
            return inner.commands
        if rest:
            raise ScriptError(f"{base} running a script file")
        return [SimpleCommand(argv=[program])]
    if base in _HARMLESS_BUILTINS and "/" not in program:
        return [SimpleCommand(argv=argv, implicit=True)]
    return [SimpleCommand(argv=argv)]


# ---- rules ------------------------------------------------------------------------------


def clean_rules(value: Any) -> List[str]:
    """A bounded list of word-prefix rules, each once."""
    if not isinstance(value, list):
        raise ValueError("allowed_commands must be a list of command prefixes.")
    cleaned: List[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("allowed_commands must be a list of command prefixes.")
        rule = " ".join(item.split())
        if not rule or len(rule) > MAX_RULE_CHARS or "\n" in item:
            raise ValueError("allowed_commands must be a list of command prefixes.")
        if rule not in cleaned:
            cleaned.append(rule)
    if len(cleaned) > MAX_RULES:
        raise ValueError("allowed_commands has too many entries.")
    return cleaned


def rule_matches(command: SimpleCommand, rule: str) -> bool:
    words = rule.split()
    return bool(words) and command.argv[: len(words)] == words


def script_allowed(script: str, rules: Iterable[str]) -> Tuple[bool, ParsedScript]:
    """True when every command the script runs matches one of the rules."""
    parsed = parse_script(script)
    rules = [r for r in rules if isinstance(r, str) and r.strip()]
    if parsed.unsupported or not parsed.commands or not rules:
        return False, parsed
    return all(command.implicit or any(rule_matches(command, rule) for rule in rules) for command in parsed.commands), parsed


def describe_for_card(script: str) -> Dict[str, Any]:
    """What the approval card shows for a terminal command: the shapes it could remember."""
    parsed = parse_script(script)
    return {"command_shapes": parsed.shapes, "command_parse_error": parsed.unsupported}
