"""Regression tests for local container verification orchestration."""

import importlib.util
import io
import os
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, call, patch


def load_local_module():
    configured = os.environ.get("TONNY_LOCAL_SCRIPT")
    path = (
        Path(configured)
        if configured
        else Path(__file__).resolve().parents[3] / "deploy/tonny/local.py"
    )
    spec = importlib.util.spec_from_file_location("tonny_local_tooling", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


local = load_local_module()


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class ArchitectureVerificationTests(unittest.TestCase):
    def test_matrix_uses_explicit_platforms_and_project_scoped_images(self):
        project = "tonny-local-check-deadbeef"
        with patch.dict(
            local.os.environ,
            {
                "DOCKER_DEFAULT_PLATFORM": "linux/s390x",
                **dict.fromkeys(local.KEYS, "must-not-leak"),
            },
        ):
            stacks = local.create_check_stacks(project, Path("/tmp/source"))

        self.assertEqual(tuple(stacks), ("amd64", "arm64"))
        for architecture, stack in stacks.items():
            self.assertEqual(stack.project, f"{project}-{architecture}")
            self.assertEqual(stack.env["DOCKER_DEFAULT_PLATFORM"], f"linux/{architecture}")
            self.assertEqual(stack.env["TONNY_MODE"], "loopback")
            for key in local.KEYS:
                self.assertEqual(stack.env[key], "")
            self.assertEqual(
                tuple(stack.image(service) for service in local.CHECK_SERVICES),
                tuple(f"{project}-{architecture}-{service}" for service in local.CHECK_SERVICES),
            )

    def test_required_build_and_inspection_commands_cover_every_service(self):
        services = ("gateway", "satellite", "browser-check")
        for architecture in ("amd64", "arm64"):
            with self.subTest(architecture=architecture):
                stack = local.Stack(f"tonny-local-check-deadbeef-{architecture}")
                inspection = completed(stdout=f"{architecture}\n" * 3)
                with patch.object(
                    local,
                    "run",
                    side_effect=[completed(), completed(), completed(), inspection],
                ) as execute:
                    local.verify_architecture(stack, architecture)

                self.assertEqual(execute.call_count, 4)
                for invocation, service in zip(
                    execute.call_args_list[:3], services, strict=True
                ):
                    command = invocation.args[0]
                    self.assertEqual(
                        command[:5],
                        ["docker", "buildx", "build", "--platform", f"linux/{architecture}"],
                    )
                    self.assertEqual(command[command.index("--tag") + 1], stack.image(service))
                    self.assertIn("--load", command)
                    self.assertEqual(command[-1], str(local.ROOT))
                    self.assertEqual(invocation.kwargs, {"cwd": local.ROOT})
                    dockerfile = (
                        "Dockerfile.satellite" if service == "satellite" else "Dockerfile.voice"
                    )
                    self.assertEqual(
                        command[command.index("--file") + 1],
                        str(local.ROOT / "deploy/tonny" / dockerfile),
                    )
                    if service == "satellite":
                        self.assertNotIn("--target", command)
                    else:
                        self.assertEqual(command[command.index("--target") + 1], service)
                self.assertEqual(
                    execute.call_args_list[-1],
                    call(
                        [
                            "docker", "image", "inspect", "--format", "{{.Architecture}}",
                            *(stack.image(service) for service in services),
                        ],
                        capture_output=True,
                        text=True,
                    ),
                )

    def test_required_architecture_failure_is_not_skipped(self):
        phases = ("gateway build", "satellite build", "browser-check build", "image inspection")
        for architecture in ("amd64", "arm64"):
            for index, phase in enumerate(phases):
                with self.subTest(architecture=architecture, phase=phase):
                    stack = local.Stack(f"tonny-local-check-deadbeef-{architecture}")
                    failure = subprocess.CalledProcessError(
                        19, ["docker"], stderr="exec format error"
                    )
                    output = io.StringIO()
                    with (
                        patch.object(
                            local, "run", side_effect=[completed()] * index + [failure]
                        ) as execute,
                        redirect_stdout(output),
                    ):
                        with self.assertRaisesRegex(
                            RuntimeError,
                            rf"Required linux/{architecture} image verification failed "
                            rf"during {phase} \(exit 19\); .* mandatory",
                        ) as raised:
                            local.verify_architecture(stack, architecture)
                    self.assertEqual(execute.call_count, index + 1)
                    self.assertIn("exec format error", str(raised.exception))
                    self.assertNotIn("SKIP", output.getvalue())

    def test_canonical_check_verifies_both_architectures_before_runtime(self):
        for native in ("amd64", "arm64"):
            with self.subTest(native=native):
                events = Mock()
                with (
                    patch.object(local.shutil, "copytree"),
                    patch.object(local, "docker_architecture", return_value=native),
                    patch.object(local, "verify_architecture", events.verify),
                    patch.object(local.Stack, "compose"),
                    patch.object(local.Stack, "cleanup", autospec=True) as cleanup,
                    patch.object(local, "run", events.runtime),
                ):
                    events.runtime.side_effect = RuntimeError("stop at first runtime check")
                    with self.assertRaisesRegex(RuntimeError, "stop at first runtime check"):
                        local.check()

                self.assertEqual(
                    [invocation.args[1] for invocation in events.verify.call_args_list],
                    ["amd64", "arm64"],
                )
                self.assertEqual(
                    [invocation[0] for invocation in events.mock_calls],
                    ["verify", "verify", "runtime"],
                )
                verified = [invocation.args[0] for invocation in events.verify.call_args_list]
                self.assertEqual(cleanup.call_args_list, [call(stack) for stack in reversed(verified)])

    def test_canonical_check_aborts_runtime_after_required_failure(self):
        for failed_architecture in ("amd64", "arm64"):
            with self.subTest(architecture=failed_architecture):
                def verify(stack, architecture):
                    if architecture == failed_architecture:
                        raise RuntimeError("required image verification failed")

                with (
                    patch.object(local.shutil, "copytree"),
                    patch.object(local, "docker_architecture", return_value="amd64"),
                    patch.object(local, "verify_architecture", side_effect=verify),
                    patch.object(local.Stack, "compose"),
                    patch.object(local.Stack, "cleanup") as cleanup,
                    patch.object(local, "run") as runtime,
                    patch.object(local, "arm64_runtime_available") as probe,
                ):
                    with self.assertRaisesRegex(RuntimeError, "required image verification failed"):
                        local.check()
                runtime.assert_not_called()
                probe.assert_not_called()
                self.assertEqual(cleanup.call_count, 2)

    def test_wrong_inspected_architecture_fails_verification(self):
        stack = local.Stack("tonny-local-check-deadbeef-arm64")
        for inspected in ("amd64\narm64\narm64\n", "arm64\narm64\n", ""):
            with self.subTest(inspected=inspected), patch.object(
                local,
                "run",
                side_effect=[completed(), completed(), completed(), completed(stdout=inspected)],
            ):
                with self.assertRaisesRegex(RuntimeError, "expected .*arm64.* got"):
                    local.verify_architecture(stack, "arm64")

    def test_arm64_probe_only_skips_a_missing_binfmt_handler(self):
        stack = local.Stack("tonny-local-check-deadbeef-arm64")
        missing_binfmt = completed(1, stderr="exec /bin/true: exec format error")
        output = io.StringIO()
        with (
            patch.object(local, "run", return_value=missing_binfmt) as execute,
            redirect_stdout(output),
        ):
            self.assertFalse(local.arm64_runtime_available(stack))
        self.assertIn("requires binfmt", output.getvalue())
        self.assertIn("required ARM64 image build still passed", output.getvalue())
        execute.assert_called_once_with(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--platform",
                "linux/arm64",
                "--entrypoint",
                "/bin/true",
                stack.image("gateway"),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        for unexpected in (
            completed(125, stderr="Cannot connect to the Docker daemon"),
            completed(126, stderr="permission denied"),
            completed(137),
            completed(139, stderr="segmentation fault"),
        ):
            with self.subTest(result=unexpected), patch.object(local, "run", return_value=unexpected):
                with self.assertRaisesRegex(
                    RuntimeError, rf"exit {unexpected.returncode}.*other than missing binfmt"
                ):
                    local.arm64_runtime_available(stack)

        with patch.object(local, "run", return_value=completed()):
            self.assertTrue(local.arm64_runtime_available(stack))

    def test_native_architecture_comes_from_the_docker_daemon(self):
        for reported, expected in (
            ("x86_64", "amd64"), ("amd64", "amd64"),
            ("aarch64", "arm64"), ("arm64", "arm64"),
        ):
            with self.subTest(reported=reported), patch.object(
                local, "run", return_value=completed(stdout=reported + "\n")
            ) as execute:
                self.assertEqual(local.docker_architecture(), expected)
                execute.assert_called_once_with(
                    ["docker", "info", "--format", "{{.Architecture}}"],
                    capture_output=True,
                    text=True,
                )


class ScopedCleanupTests(unittest.TestCase):
    def test_cleanup_targets_only_exact_temporary_project_images(self):
        stack = local.Stack("tonny-local-check-deadbeef-amd64")
        with patch.object(stack, "compose", return_value=completed()) as compose, patch.object(
            local, "run", return_value=completed()
        ) as execute:
            stack.cleanup()

        compose.assert_called_once_with(
            "down",
            "--remove-orphans",
            "--volumes",
            check=False,
            capture_output=True,
            text=True,
        )
        expected = [stack.image(service) for service in local.CHECK_SERVICES]
        execute.assert_called_once_with(
            ["docker", "image", "rm", *expected],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotIn("tonny-local-gateway", expected)
        self.assertNotIn("tonny-local-satellite", expected)
        self.assertNotIn("tonny-local-browser-check", expected)

    def test_cleanup_refuses_the_fixed_development_project(self):
        stack = local.Stack("tonny-local")
        with patch.object(stack, "compose") as compose, patch.object(local, "run") as execute:
            with self.assertRaisesRegex(ValueError, "Refusing temporary cleanup"):
                stack.cleanup()
        compose.assert_not_called()
        execute.assert_not_called()

    def test_failed_verification_still_runs_scoped_cleanup(self):
        stack = local.Stack("tonny-local-check-deadbeef-arm64")
        missing = "\n".join(
            f"Error response from daemon: No such image: {stack.image(service)}:latest"
            for service in local.CHECK_SERVICES
        )
        with patch.object(stack, "compose", return_value=completed()) as compose, patch.object(
            local, "run", return_value=completed(1, stderr=missing)
        ) as execute:
            with self.assertRaisesRegex(RuntimeError, "architecture build failed"):
                with local.managed_check_stacks((stack,)):
                    raise RuntimeError("architecture build failed")

        compose.assert_called_once()
        execute.assert_called_once()

    def test_cleanup_errors_are_not_treated_as_missing_images(self):
        stack = local.Stack("tonny-local-check-deadbeef-amd64")
        with (
            patch.object(stack, "compose", return_value=completed()),
            patch.object(
                local, "run", return_value=completed(1, stderr="image is used by running container")
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "image removal: image is used"):
                stack.cleanup()

    def test_cleanup_continues_after_one_project_fails_and_preserves_body_error(self):
        stacks = local.create_check_stacks("tonny-local-check-deadbeef", Path("/tmp/source"))
        with patch.object(
            local.Stack, "cleanup", autospec=True,
            side_effect=[RuntimeError("cleanup failed"), None],
        ) as cleanup:
            with self.assertRaisesRegex(RuntimeError, "browser failed.*cleanup failed"):
                with local.managed_check_stacks(tuple(stacks.values())):
                    raise RuntimeError("browser failed")
        self.assertEqual(cleanup.call_args_list, [call(stacks["arm64"]), call(stacks["amd64"])])


if __name__ == "__main__":
    unittest.main()
