import tempfile
import unittest
from pathlib import Path

from app.services.computer_provider import ComputerProviderError
from app.services.docker_computer_provider import DockerComputerProvider


class FakeDockerCommand:
    def __init__(self):
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append((tuple(args), timeout))
        if args[0] == "run":
            return "container-test" if "--entrypoint" not in args else ""
        if args[0] == "port":
            return "127.0.0.1:45678"
        if args[0] == "rm":
            # No stale container exists in these tests, like a real daemon says.
            raise ComputerProviderError("Error response from daemon: No such container")
        if args[0] == "exec":
            return "Reading package lists...\nSetting up " + " ".join(args[5:]) + "\n"
        return ""

    def run_args(self):
        return next(call[0] for call in self.calls if call[0][0] == "run" and "--entrypoint" not in call[0])

    def calls_of(self, verb):
        return [call[0] for call in self.calls if call[0][0] == verb]


class DockerComputerProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.docker = FakeDockerCommand()
        self.provider = DockerComputerProvider(
            image="cubicle-computer:test",
            workspace_root=self.root / "computers",
            seccomp_profile=self.root / "missing-seccomp.json",
            start_timeout=0.5,
            docker_command=self.docker,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_start_builds_a_restricted_per_bot_container(self):
        status = self.provider.get_or_create("bot-test")

        async def ready(_record):
            return {"status": "healthy", "width": 1280, "height": 720}

        self.provider._wait_until_ready = ready
        started = await self.provider.start(status.computer_id)

        self.assertEqual(started.state, "running")
        self.assertEqual(started.provider, "docker-playwright")
        self.assertTrue((self.root / "computers" / status.computer_id).is_dir())
        run_args = self.docker.run_args()
        self.assertEqual(run_args[0], "run")
        # The root filesystem is writable (apt installs into it) but the
        # sandbox user is dropped to with no capabilities; the root stage at
        # boot keeps only what dpkg needs.
        self.assertNotIn("--read-only", run_args)
        self.assertNotIn("--user", run_args)
        self.assertIn("--cap-drop", run_args)
        self.assertIn("ALL", run_args)
        added = [run_args[i + 1] for i, a in enumerate(run_args) if a == "--cap-add"]
        self.assertEqual(added, ["CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "SETGID", "SETUID", "SETPCAP"])
        self.assertIn("--security-opt", run_args)
        self.assertIn("no-new-privileges:true", run_args)
        self.assertIn("--pids-limit", run_args)
        self.assertIn("/tmp:rw,nosuid,size=2g", run_args)
        # Home and apt cache are bind mounts beside the workspace, created up front.
        mounts = [run_args[i + 1] for i, a in enumerate(run_args) if a == "--mount"]
        self.assertTrue(any(m.endswith(",dst=/home/pwuser") and f"{status.computer_id}.home" in m for m in mounts))
        self.assertTrue(any(m.endswith(",dst=/var/lib/cubicle/apt") and f"{status.computer_id}.apt" in m for m in mounts))
        self.assertTrue((self.root / "computers" / f"{status.computer_id}.home").is_dir())
        self.assertTrue((self.root / "computers" / f"{status.computer_id}.apt").is_dir())
        self.assertIn("CUBICLE_PACKAGES=", run_args)
        self.assertIn("--publish", run_args)
        self.assertIn("127.0.0.1::3000", run_args)
        self.assertEqual(started.packages, [])
        self.assertNotIn("COMPUTER_TOKEN", started.to_dict())
        self.assertNotIn("token", started.to_dict())

    async def test_tmp_size_is_configurable(self):
        provider = DockerComputerProvider(
            image="cubicle-computer:test",
            workspace_root=self.root / "computers",
            seccomp_profile=self.root / "missing-seccomp.json",
            start_timeout=0.5,
            tmp_size="4g",
            docker_command=self.docker,
        )
        status = provider.get_or_create("bot-tmp")

        async def ready(_record):
            return {"status": "healthy"}

        provider._wait_until_ready = ready
        await provider.start(status.computer_id)
        self.assertIn("/tmp:rw,nosuid,size=4g", self.docker.run_args())

    async def test_installed_packages_are_remembered_and_restored_at_the_next_start(self):
        status = self.provider.get_or_create("bot-pkg")

        async def ready(_record):
            return {"status": "healthy"}

        self.provider._wait_until_ready = ready
        await self.provider.start(status.computer_id)

        result = await self.provider.install_packages(status.computer_id, ["ffmpeg", "Libpq-dev", "ffmpeg"])
        self.assertEqual(result["installed"], ["ffmpeg", "libpq-dev"])
        self.assertEqual(result["packages"], ["ffmpeg", "libpq-dev"])
        self.assertIn("Setting up ffmpeg libpq-dev", result["output"])
        exec_call = self.docker.calls_of("exec")[0]
        self.assertEqual(exec_call[:4], ("exec", "--user", "root", "container-test"))
        self.assertEqual(exec_call[4], "/opt/cubicle-computer/install-packages.sh")
        self.assertEqual(exec_call[5:], ("ffmpeg", "libpq-dev"))
        self.assertEqual(
            (self.root / "computers" / f"{status.computer_id}.packages").read_text(encoding="utf-8"),
            "ffmpeg\nlibpq-dev\n",
        )
        self.assertEqual(self.provider.describe("bot-pkg").packages, ["ffmpeg", "libpq-dev"])

        # A second install adds to the list; the list is given to the next container.
        await self.provider.install_packages(status.computer_id, ["jq"])
        await self.provider.stop(status.computer_id)
        await self.provider.start(status.computer_id)
        last_run = self.docker.calls_of("run")[-1]
        self.assertIn("CUBICLE_PACKAGES=ffmpeg libpq-dev jq", last_run)

        # A fresh provider (API restart) reads the list back from disk.
        again = DockerComputerProvider(
            image="cubicle-computer:test",
            workspace_root=self.root / "computers",
            seccomp_profile=self.root / "missing-seccomp.json",
            docker_command=self.docker,
        )
        self.assertEqual(again.get_or_create("bot-pkg").packages, ["ffmpeg", "libpq-dev", "jq"])

    async def test_package_names_are_validated_before_anything_runs(self):
        status = self.provider.get_or_create("bot-pkg")

        async def ready(_record):
            return {"status": "healthy"}

        self.provider._wait_until_ready = ready
        await self.provider.start(status.computer_id)
        for bad in (["ffmpeg; rm -rf /"], ["--reinstall"], [], ["x" * 120], "", ["../etc"]):
            with self.assertRaises(ComputerProviderError):
                await self.provider.install_packages(status.computer_id, bad)
        self.assertEqual(self.docker.calls_of("exec"), [])
        with self.assertRaisesRegex(ComputerProviderError, "At most 50"):
            await self.provider.install_packages(status.computer_id, [f"pkg{i}" for i in range(51)])

    async def test_a_failed_install_is_reported_and_not_remembered(self):
        def docker(args, timeout):
            if args[0] == "exec":
                raise ComputerProviderError("Docker command failed: E: Unable to locate package nosuchpkg")
            return self.docker(args, timeout)

        provider = DockerComputerProvider(
            image="cubicle-computer:test",
            workspace_root=self.root / "computers",
            seccomp_profile=self.root / "missing-seccomp.json",
            start_timeout=0.5,
            docker_command=docker,
        )
        status = provider.get_or_create("bot-pkg")

        async def ready(_record):
            return {"status": "healthy"}

        provider._wait_until_ready = ready
        await provider.start(status.computer_id)
        with self.assertRaisesRegex(ComputerProviderError, "Unable to locate package"):
            await provider.install_packages(status.computer_id, ["nosuchpkg"])
        self.assertEqual(status.packages, [])
        self.assertFalse((self.root / "computers" / f"{status.computer_id}.packages").exists())

    async def test_reset_empties_the_home_directory_and_the_package_list(self):
        status = self.provider.get_or_create("bot-reset")

        async def ready(_record):
            return {"status": "healthy"}

        self.provider._wait_until_ready = ready
        await self.provider.start(status.computer_id)
        await self.provider.install_packages(status.computer_id, ["ffmpeg"])
        home = self.root / "computers" / f"{status.computer_id}.home"
        self.assertTrue(home.is_dir())

        reset = await self.provider.reset(status.computer_id)
        self.assertEqual(reset.state, "stopped")
        self.assertEqual(reset.generation, 2)
        self.assertEqual(reset.packages, [])
        self.assertFalse((self.root / "computers" / f"{status.computer_id}.packages").exists())
        # The files belong to the sandbox user, so a throwaway container
        # over the same mount does the deleting, with no network.
        wipe = next(c for c in self.docker.calls_of("run") if "--entrypoint" in c)
        self.assertIn("find", wipe)
        self.assertIn("none", wipe)
        self.assertIn("--rm", wipe)
        self.assertNotIn("--network", wipe[wipe.index("--mount"):])
        mount = wipe[wipe.index("--mount") + 1]
        self.assertTrue(mount.startswith("type=bind,src="))
        self.assertIn(f"{status.computer_id}.home", mount)
        self.assertTrue(mount.endswith(",dst=/wipe"))
        self.assertEqual(wipe[-4:], ("/wipe", "-mindepth", "1", "-delete"))
        # The workspace is untouched.
        self.assertTrue((self.root / "computers" / status.computer_id).is_dir())

    async def test_browser_terminal_files_input_and_screenshot_use_scoped_runtime(self):
        status = self.provider.get_or_create("bot-test")

        async def ready(_record):
            return {"status": "healthy"}

        async def request(_record, method, route, payload=None):
            if route == "/health":
                return {"status": "healthy"}
            if route == "/navigate":
                return {"url": payload["url"], "title": "Example"}
            if route == "/terminal":
                return {"exit_code": 0, "stdout": "ok", "stderr": ""}
            if route == "/files":
                return {"path": payload["path"], "entries": []}
            if route == "/input":
                return {"accepted": True, "type": payload["event"]["type"]}
            if route == "/screenshot":
                return {
                    "frame_id": "frame-real-1",
                    "format": "jpeg",
                    "width": 1280,
                    "height": 720,
                    "data": "base64-jpeg",
                }
            raise AssertionError(route)

        self.provider._wait_until_ready = ready
        self.provider._request = request
        await self.provider.start(status.computer_id)

        navigated = await self.provider.browser_navigate(status.computer_id, "https://example.test")
        self.assertEqual(navigated["title"], "Example")
        terminal = await self.provider.terminal_execute(status.computer_id, "printf safe")
        self.assertEqual(terminal["stdout"], "ok")
        files = await self.provider.files_list(status.computer_id, "/workspace")
        self.assertEqual(files["entries"], [])
        input_result = await self.provider.send_input(
            status.computer_id,
            {"type": "click", "x": 4, "y": 5},
        )
        self.assertTrue(input_result["accepted"])
        screen = await self.provider.screenshot(status.computer_id)
        self.assertTrue(screen["available"])
        self.assertEqual(screen["frame_id"], "frame-real-1")
        self.assertEqual(self.provider.describe("bot-test").frame_id, "frame-real-1")

    async def test_a_stale_container_with_the_same_name_is_removed_before_launch(self):
        status = self.provider.get_or_create("bot-test")

        async def ready(_record):
            return {"status": "healthy"}

        self.provider._wait_until_ready = ready
        await self.provider.start(status.computer_id)

        first = self.docker.calls[0][0]
        self.assertEqual(first[:2], ("rm", "-f"))
        self.assertEqual(first[2], f"cubicle-computer-{status.computer_id[-70:]}")
        self.assertEqual(self.docker.calls[1][0][0], "run")

    async def test_network_mode_joins_a_docker_network_instead_of_publishing(self):
        provider = DockerComputerProvider(
            image="cubicle-computer:test",
            workspace_root=self.root / "computers",
            seccomp_profile=self.root / "missing-seccomp.json",
            start_timeout=0.5,
            network="cubicle-computers",
            docker_command=self.docker,
        )
        status = provider.get_or_create("bot-net")

        async def ready(_record):
            return {"status": "healthy"}

        provider._wait_until_ready = ready
        await provider.start(status.computer_id)

        run_args = self.docker.run_args()
        self.assertIn("--network", run_args)
        self.assertIn("cubicle-computers", run_args)
        self.assertNotIn("--publish", run_args)
        self.assertNotIn("port", [call[0][0] for call in self.docker.calls])

        record = provider._runtimes[status.computer_id]
        self.assertEqual(record.host, f"cubicle-computer-{status.computer_id[-70:]}")
        self.assertEqual(record.port, 3000)
        self.assertEqual(
            provider._runtime_url(record, "/health"),
            f"http://cubicle-computer-{status.computer_id[-70:]}:3000/health",
        )

        await provider.stop(status.computer_id)
        self.assertIsNone(record.host)
        self.assertIsNone(record.port)

    async def test_publish_mode_connects_to_loopback_port(self):
        status = self.provider.get_or_create("bot-test")

        async def ready(_record):
            return {"status": "healthy"}

        self.provider._wait_until_ready = ready
        await self.provider.start(status.computer_id)
        record = self.provider._runtimes[status.computer_id]
        self.assertEqual(record.host, "127.0.0.1")
        self.assertEqual(record.port, 45678)
        self.assertEqual(
            self.provider._runtime_url(record, "/screenshot"), "http://127.0.0.1:45678/screenshot"
        )

    async def test_volume_workspace_mode_mounts_a_named_volume(self):
        provider = DockerComputerProvider(
            image="cubicle-computer:test",
            workspace_root=self.root / "computers",
            seccomp_profile=self.root / "missing-seccomp.json",
            start_timeout=0.5,
            workspace_mode="volume",
            docker_command=self.docker,
        )
        status = provider.get_or_create("bot-vol")

        async def ready(_record):
            return {"status": "healthy"}

        provider._wait_until_ready = ready
        await provider.start(status.computer_id)

        run_args = self.docker.run_args()
        mounts = [run_args[i + 1] for i, a in enumerate(run_args) if a == "--mount"]
        self.assertEqual(
            mounts,
            [
                f"type=volume,src=cubicle-computer-ws-{status.computer_id[-60:]},dst=/workspace",
                f"type=volume,src=cubicle-computer-home-{status.computer_id[-60:]},dst=/home/pwuser",
                f"type=volume,src=cubicle-computer-apt-{status.computer_id[-60:]},dst=/var/lib/cubicle/apt",
            ],
        )
        # No host directories are created for the home or apt cache in volume mode.
        self.assertFalse((self.root / "computers" / f"{status.computer_id}.home").exists())

        # Reset removes the home volume instead of running a wipe container.
        await provider.reset(status.computer_id)
        self.assertEqual(
            self.docker.calls_of("volume"),
            [("volume", "rm", "-f", f"cubicle-computer-home-{status.computer_id[-60:]}")],
        )
        self.assertEqual([c for c in self.docker.calls_of("run") if "--entrypoint" in c], [])

    async def test_bind_mode_translates_to_the_host_path_when_configured(self):
        provider = DockerComputerProvider(
            image="cubicle-computer:test",
            workspace_root=self.root / "computers",
            seccomp_profile=self.root / "missing-seccomp.json",
            start_timeout=0.5,
            host_workspace_root="/srv/cubicle/computers",
            docker_command=self.docker,
        )
        status = provider.get_or_create("bot-bind")

        async def ready(_record):
            return {"status": "healthy"}

        provider._wait_until_ready = ready
        await provider.start(status.computer_id)

        run_args = self.docker.run_args()
        mounts = [run_args[i + 1] for i, a in enumerate(run_args) if a == "--mount"]
        self.assertEqual(len(mounts), 3)
        for mount, destination in zip(mounts, ("/workspace", "/home/pwuser", "/var/lib/cubicle/apt")):
            self.assertTrue(mount.startswith("type=bind,src="))
            self.assertIn(status.computer_id, mount)
            self.assertIn("cubicle", mount.replace("\\", "/"))
            self.assertNotIn(str(self.root), mount)
            self.assertTrue(mount.endswith(f",dst={destination}"))

    async def test_user_control_blocks_bot_input_and_navigation_until_handed_back(self):
        status = self.provider.get_or_create("bot-test")

        async def ready(_record):
            return {"status": "healthy"}

        async def request(_record, method, route, payload=None):
            return {"status": "healthy", "accepted": True, "url": "https://example.test", "title": "x"}

        self.provider._wait_until_ready = ready
        self.provider._request = request
        await self.provider.start(status.computer_id)

        self.assertEqual(self.provider.vnc_target(status.computer_id)["port"], 45678)
        self.assertIn("vnc", status.capabilities)

        takeover = await self.provider.request_takeover(status.computer_id, "Please log in to the site.")
        self.assertTrue(takeover["requested"])
        self.assertEqual(status.takeover_request["reason"], "Please log in to the site.")

        await self.provider.set_control(status.computer_id, "user")
        self.assertEqual(status.controlled_by, "user")
        with self.assertRaisesRegex(ComputerProviderError, "user currently has control"):
            await self.provider.send_input(status.computer_id, {"type": "click", "x": 1, "y": 1})
        with self.assertRaisesRegex(ComputerProviderError, "user currently has control"):
            await self.provider.browser_navigate(status.computer_id, "https://example.test")
        # Looking is still allowed while the user drives.
        self.assertTrue((await self.provider.screenshot(status.computer_id))["available"])

        await self.provider.set_control(status.computer_id, "bot")
        self.assertEqual(status.controlled_by, "bot")
        self.assertIsNone(status.takeover_request)
        self.assertTrue((await self.provider.send_input(status.computer_id, {"type": "click", "x": 1, "y": 1}))["accepted"])

        with self.assertRaisesRegex(ComputerProviderError, "'bot' or 'user'"):
            await self.provider.set_control(status.computer_id, "me")
        await self.provider.stop(status.computer_id)
        self.assertIsNone(self.provider.vnc_target(status.computer_id))

    async def test_configured_wallpaper_is_mounted_read_only_over_the_default(self):
        provider = DockerComputerProvider(
            image="cubicle-computer:test",
            workspace_root=self.root / "computers",
            seccomp_profile=self.root / "missing-seccomp.json",
            start_timeout=0.5,
            wallpaper="/srv/wallpaper.jpg",
            docker_command=self.docker,
        )
        status = provider.get_or_create("bot-wall")

        async def ready(_record):
            return {"status": "healthy"}

        provider._wait_until_ready = ready
        await provider.start(status.computer_id)
        run_args = self.docker.run_args()
        self.assertIn("type=bind,src=/srv/wallpaper.jpg,dst=/opt/cubicle-computer/wallpaper,readonly", run_args)

        # Without a wallpaper nothing extra is mounted.
        plain = [a for a in self.docker.run_args() if "wallpaper" in a]
        self.assertEqual(len(plain), 1)

    def test_unknown_workspace_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            DockerComputerProvider(workspace_root=self.root / "computers", workspace_mode="nfs")

    async def test_docker_failure_is_reported_and_marks_runtime_unhealthy(self):
        def failing_docker(_args, _timeout):
            raise ComputerProviderError("Docker daemon is unavailable.")

        provider = DockerComputerProvider(
            workspace_root=self.root / "computers",
            docker_command=failing_docker,
        )
        status = provider.get_or_create("bot-test")
        with self.assertRaisesRegex(ComputerProviderError, "daemon is unavailable"):
            await provider.start(status.computer_id)
        self.assertEqual(provider.describe("bot-test").state, "error")
        self.assertEqual(provider.describe("bot-test").health, "unhealthy")


if __name__ == "__main__":
    unittest.main()
