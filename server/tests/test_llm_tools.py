import unittest

from app.services.action_gateway import ActionInvocation
from app.services.llm_tools import (
    ToolCallError,
    available_tools,
    build_invocation,
    describe_tools,
    openai_tool_definitions,
    parse_arguments,
    without_duplicate_routes,
)
from app.services.workspace_service import WorkspaceToolCall


class ToolCatalogTests(unittest.TestCase):
    def test_tool_groups_follow_what_is_configured(self):
        names = lambda specs: [s.name for s in specs]  # noqa: E731
        base = names(available_tools(computer=False, github=False))
        self.assertEqual(base, ["shared_folder_list", "shared_folder_read", "shared_folder_write", "shared_folder_delete", "share_file", "memory_read", "memory_write", "memory_edit", "work_notes_read", "work_notes_write", "work_notes_edit", "project_log", "project_log_read", "projects_list", "project_create", "project_join", "todo_list", "todo_add", "todo_update", "schedule_list", "schedule_create", "schedule_update", "schedule_delete", "ask_user", "mcp_list_servers", "mcp_add_server", "mcp_test_server", "mcp_remove_server"])
        with_computer = names(available_tools(computer=True, github=False))
        self.assertIn("computer_screenshot", with_computer)
        self.assertNotIn("github_list_issues", with_computer)
        everything = names(available_tools(computer=True, github=True))
        self.assertIn("github_create_issue", everything)

    def test_openai_definitions_are_well_formed(self):
        definitions = openai_tool_definitions(available_tools(computer=True, github=True))
        for definition in definitions:
            self.assertEqual(definition["type"], "function")
            function = definition["function"]
            self.assertRegex(function["name"], r"^[a-zA-Z0-9_-]+$")
            self.assertTrue(function["description"])
            self.assertEqual(function["parameters"]["type"], "object")
            self.assertFalse(function["parameters"]["additionalProperties"])

    def test_shared_folder_tools_step_aside_when_the_computer_has_the_folder(self):
        names = lambda specs: [s.name for s in specs]  # noqa: E731
        everything = available_tools(computer=True, github=False)
        slim = without_duplicate_routes(everything, shared_in_computer=True)
        self.assertNotIn("shared_folder_write", names(slim))
        self.assertIn("write_file", names(slim))
        self.assertIn("share_file", names(slim))
        # Without the mount, or without a computer, the host-side tools stay.
        self.assertEqual(without_duplicate_routes(everything, shared_in_computer=False), everything)
        no_computer = available_tools(computer=False, github=False)
        self.assertEqual(without_duplicate_routes(no_computer, shared_in_computer=True), no_computer)

    def test_description_tells_the_computer_what_shared_is(self):
        specs = available_tools(computer=True, github=False)
        with_mount = describe_tools(specs, shared_in_computer=True)
        self.assertIn("/shared is the user's shared folder", with_mount)
        self.assertIn("/workspace is your own project space", with_mount)
        self.assertNotIn("/shared", describe_tools(specs, shared_in_computer=False))

    def test_description_explains_approvals_and_the_computer(self):
        text = describe_tools(available_tools(computer=True, github=False))
        self.assertIn("starts itself the first time you use it", text)
        self.assertIn("computer_request_takeover", text)
        self.assertIn("Tools that need approval:", text)
        self.assertIn("shared_folder_write", text)
        self.assertNotIn("github", text)
        self.assertEqual(describe_tools([]), "")


class ArgumentParsingTests(unittest.TestCase):
    def test_arguments_accept_json_text_or_objects(self):
        self.assertEqual(parse_arguments('{"path": "notes"}'), {"path": "notes"})
        self.assertEqual(parse_arguments({"a": 1}), {"a": 1})
        self.assertEqual(parse_arguments(""), {})
        self.assertEqual(parse_arguments(None), {})
        with self.assertRaisesRegex(ToolCallError, "not valid JSON"):
            parse_arguments("{oops")
        with self.assertRaisesRegex(ToolCallError, "JSON object"):
            parse_arguments("[1, 2]")


class BuildInvocationTests(unittest.TestCase):
    def test_workspace_calls_become_workspace_tool_calls(self):
        listing = build_invocation("shared_folder_list", {}, "bot-1")
        self.assertIsInstance(listing, WorkspaceToolCall)
        self.assertEqual((listing.name, listing.path), ("workspace.list", "."))

        write = build_invocation("shared_folder_write", {"path": "notes/a.md", "content": "hi"}, "bot-1")
        self.assertEqual((write.name, write.path, write.content), ("workspace.write", "notes/a.md", "hi"))

        delete = build_invocation("shared_folder_delete", {"path": "old/report.md"}, "bot-1")
        self.assertEqual((delete.name, delete.path), ("workspace.delete", "old/report.md"))

        with self.assertRaisesRegex(ToolCallError, "'path' is required"):
            build_invocation("shared_folder_read", {}, "bot-1")
        with self.assertRaisesRegex(ToolCallError, "'path' is required"):
            build_invocation("shared_folder_delete", {}, "bot-1")
        with self.assertRaisesRegex(ToolCallError, "must be a string"):
            build_invocation("shared_folder_read", {"path": 5}, "bot-1")

    def test_computer_calls_are_scoped_to_the_bot(self):
        nav = build_invocation("computer_browser_navigate", {"url": "https://example.com"}, "bot-1")
        self.assertIsInstance(nav, ActionInvocation)
        self.assertEqual(nav.name, "computer.browser_navigate")
        self.assertEqual(nav.arguments["bot_id"], "bot-1")
        self.assertEqual(nav.arguments["computer_id"], nav.target["computer_id"])
        self.assertEqual(nav.arguments["url"], "https://example.com")

        with self.assertRaisesRegex(ToolCallError, "http or https"):
            build_invocation("computer_browser_navigate", {"url": "file:///etc/passwd"}, "bot-1")

        click = build_invocation("computer_send_input", {"type": "click", "x": 10, "y": 20.5}, "bot-1")
        self.assertEqual(click.arguments["event"], {"type": "click", "x": 10.0, "y": 20.5, "button": "left"})
        typed = build_invocation("computer_send_input", {"type": "type", "text": "hello"}, "bot-1")
        self.assertEqual(typed.arguments["event"], {"type": "type", "text": "hello"})
        with self.assertRaisesRegex(ToolCallError, "'x' is required"):
            build_invocation("computer_send_input", {"type": "click"}, "bot-1")
        with self.assertRaisesRegex(ToolCallError, "one of click"):
            build_invocation("computer_send_input", {"type": "drag"}, "bot-1")

        files = build_invocation("list_files", {}, "bot-1")
        self.assertEqual(files.arguments["path"], "/workspace")

        read = build_invocation("read_file", {"path": "/workspace/app/main.py"}, "bot-1")
        self.assertEqual((read.name, read.arguments["path"]), ("computer.files_read", "/workspace/app/main.py"))
        write = build_invocation("write_file", {"path": "/workspace/a.txt", "content": "hello\n"}, "bot-1")
        self.assertEqual(write.name, "computer.files_write")
        self.assertIn("Write 6 characters to /workspace/a.txt", write.preview)
        edit = build_invocation("edit_file", {"path": "/workspace/a.txt", "old_text": "hello", "new_text": ""}, "bot-1")
        self.assertEqual(edit.arguments["new_text"], "")
        self.assertIn("5 characters become 0", edit.preview)
        # Relative paths resolve under /workspace before the call is made.
        relative = build_invocation("read_file", {"path": "app/main.py"}, "bot-1")
        self.assertEqual(relative.arguments["path"], "/workspace/app/main.py")
        self.assertEqual(build_invocation("write_file", {"path": "~/report.md", "content": "x"}, "bot-1").arguments["path"], "/home/pwuser/report.md")
        self.assertEqual(build_invocation("list_files", {"path": "src"}, "bot-1").arguments["path"], "/workspace/src")
        with self.assertRaises(ToolCallError):
            build_invocation("computer_read_file", {"path": "  "}, "bot-1")
        with self.assertRaisesRegex(ToolCallError, "'old_text' is required"):
            build_invocation("computer_edit_file", {"path": "/workspace/a.txt", "new_text": "x"}, "bot-1")
        # A long heredoc-style command is accepted now; files should not need it anyway.
        long_command = build_invocation("computer_terminal_execute", {"command": "x" * 50000}, "bot-1")
        self.assertEqual(len(long_command.arguments["command"]), 50000)
        with self.assertRaisesRegex(ToolCallError, "longer than 60000"):
            build_invocation("computer_terminal_execute", {"command": "x" * 60001}, "bot-1")

        timed = build_invocation("computer_terminal_execute", {"command": "pytest -q", "timeout_seconds": 300}, "bot-1")
        self.assertEqual(timed.arguments["timeout_seconds"], 300)
        self.assertIn("(up to 5 min)", timed.preview)
        plain = build_invocation("computer_terminal_execute", {"command": "ls"}, "bot-1")
        self.assertNotIn("timeout_seconds", plain.arguments)
        self.assertNotIn("up to", plain.preview)
        from app.config import settings

        capped = build_invocation("computer_terminal_execute", {"command": "x", "timeout_seconds": 99999}, "bot-1")
        self.assertEqual(capped.arguments["timeout_seconds"], int(max(settings.COMPUTER_DOCKER_COMMAND_TIMEOUT, settings.COMPUTER_DOCKER_COMMAND_TIMEOUT_MAX)))
        with self.assertRaisesRegex(ToolCallError, "number of seconds"):
            build_invocation("computer_terminal_execute", {"command": "x", "timeout_seconds": "soon"}, "bot-1")
        install = build_invocation("computer_install_packages", {"packages": ["ffmpeg", "Libpq-dev", "ffmpeg"]}, "bot-1")
        self.assertEqual(install.name, "computer.install_packages")
        self.assertEqual(install.arguments["packages"], ["ffmpeg", "libpq-dev"])
        self.assertIn("ffmpeg, libpq-dev", install.preview)
        self.assertEqual(build_invocation("computer_install_packages", {"packages": "jq curl"}, "bot-1").arguments["packages"], ["jq", "curl"])
        with self.assertRaisesRegex(ToolCallError, "Invalid package name"):
            build_invocation("computer_install_packages", {"packages": ["ffmpeg; rm -rf /"]}, "bot-1")
        with self.assertRaisesRegex(ToolCallError, "list of package names is required"):
            build_invocation("computer_install_packages", {"packages": []}, "bot-1")
        self.assertEqual(build_invocation("computer_screenshot", {}, "bot-1").name, "computer.screenshot")
        self.assertEqual(build_invocation("computer_start", {}, "bot-1").name, "computer.start")

        takeover = build_invocation("computer_request_takeover", {"reason": "Please solve the CAPTCHA."}, "bot-1")
        self.assertEqual(takeover.name, "computer.request_takeover")
        self.assertEqual(takeover.arguments["reason"], "Please solve the CAPTCHA.")
        with self.assertRaisesRegex(ToolCallError, "'reason' is required"):
            build_invocation("computer_request_takeover", {}, "bot-1")

    def test_github_calls_match_the_slash_command_shape(self):
        listing = build_invocation("github_list_issues", {"owner": "octo", "repo": "demo"}, "bot-1")
        self.assertEqual(listing.name, "connector.github_list_issues")
        self.assertEqual(listing.arguments, {"owner": "octo", "repo": "demo", "state": "open", "per_page": 10})

        created = build_invocation(
            "github_create_issue", {"owner": "octo", "repo": "demo", "title": "Bug", "body": "details"}, "bot-1"
        )
        self.assertEqual(created.name, "connector.github_create_issue")
        self.assertEqual(created.arguments_for_display["body_bytes"], 7)
        self.assertNotIn("body", created.arguments_for_display)

        with self.assertRaisesRegex(ToolCallError, "letters, numbers"):
            build_invocation("github_list_issues", {"owner": "bad owner", "repo": "demo"}, "bot-1")
        with self.assertRaisesRegex(ToolCallError, "open, closed, or all"):
            build_invocation("github_list_issues", {"owner": "octo", "repo": "demo", "state": "stale"}, "bot-1")

    def test_unknown_tools_are_rejected(self):
        with self.assertRaisesRegex(ToolCallError, "Unknown tool"):
            build_invocation("translator", {}, "bot-1")


if __name__ == "__main__":
    unittest.main()
