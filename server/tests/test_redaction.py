import unittest

from app.services.action_gateway import redact_sensitive


class RedactionTests(unittest.TestCase):
    def test_labelled_secrets_are_still_redacted(self):
        self.assertEqual(redact_sensitive("token=abc123 and password: hunter2"), "token=[REDACTED] and password=[REDACTED]")
        self.assertEqual(redact_sensitive({"api_key": "k", "path": "a.txt"}), {"api_key": "[REDACTED]", "path": "a.txt"})

    def test_bare_token_literals_are_redacted_but_keep_their_prefix(self):
        cases = {
            "github_pat_11AAAAAAA0" + "q" * 70: "github_pat_[REDACTED]",
            "ghp_" + "A" * 36: "ghp_[REDACTED]",
            "sk-ant-api03-" + "b" * 40: "sk-ant-[REDACTED]",
            "sk-" + "c" * 48: "sk-[REDACTED]",
            "xoxb-1234567890-abcdefghij": "xoxb-[REDACTED]",
            "AKIA" + "A" * 16: "AKIA[REDACTED]",
            "AIza" + "d" * 35: "AIza[REDACTED]",
            "sk_live_" + "e" * 24: "sk_live_[REDACTED]",
            "eyJ" + "f" * 20 + ".eyJ" + "g" * 20 + "." + "h" * 20: "eyJ[REDACTED]",
        }
        for literal, expected in cases.items():
            with self.subTest(literal=literal[:12]):
                out = redact_sensitive(f"printf '%s' {literal} > /tmp/t")
                self.assertNotIn(literal, out)
                self.assertEqual(out, f"printf '%s' {expected} > /tmp/t")

    def test_private_key_blocks_are_redacted(self):
        text = "cat > k <<EOF\n-----BEGIN OPENSSH PRIVATE KEY-----\nabc\ndef\n-----END OPENSSH PRIVATE KEY-----\nEOF"
        out = redact_sensitive(text)
        self.assertNotIn("abc", out)
        self.assertIn("[REDACTED PRIVATE KEY]", out)

    def test_ordinary_text_is_left_alone(self):
        for text in (
            "git push origin feature/login",
            "the task-list and desk-side notes, sk-8 is a bus route",
            "AKIAO is not a key because it is short",
            "export PATH=/usr/local/bin:$PATH",
            "Ask about the token count in the usage panel",
        ):
            with self.subTest(text=text):
                self.assertEqual(redact_sensitive(text), text)


if __name__ == "__main__":
    unittest.main()
