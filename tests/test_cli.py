from __future__ import annotations

from contextlib import redirect_stdout
import io
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from remote_agent_harness import cli


class CliTests(unittest.TestCase):
    def test_parse_mapping(self) -> None:
        local, remote = cli.parse_mapping("~/mount:/home/mugi/project")
        self.assertEqual(local, Path("~/mount").expanduser().absolute())
        self.assertEqual(remote, PurePosixPath("/home/mugi/project"))

    def test_parse_mapping_requires_absolute_remote_path(self) -> None:
        local, remote = cli.parse_mapping("/tmp/mount:project")
        self.assertEqual(local, Path("/tmp/mount"))
        self.assertEqual(remote, PurePosixPath("project"))

    def test_resolve_remote_dir_uses_remote_home(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="/home/pi\n")
        with patch.object(cli.subprocess, "run", return_value=completed):
            resolved = cli.resolve_remote_dir("tstof", PurePosixPath("projects/app"))
        self.assertEqual(resolved, PurePosixPath("/home/pi/projects/app"))

    def test_write_and_load_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            expected = cli.Config("rpvai", Path("/tmp/mount"), PurePosixPath("/home/mugi/project"))
            cli.write_config(expected, cwd)
            self.assertEqual(cli.load_config(Path("/tmp/mount"), cwd), expected)

    def test_write_config_preserves_other_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            development = cli.Config("rpvai", Path("/tmp/dev"), PurePosixPath("/home/mugi/dev"))
            staging = cli.Config("staging", Path("/tmp/staging"), PurePosixPath("/srv/staging"))
            cli.write_config(development, cwd)
            cli.write_config(staging, cwd)
            self.assertEqual(cli.load_config(Path("/tmp/dev"), cwd), development)
            self.assertEqual(cli.load_config(Path("/tmp/staging"), cwd), staging)

    def test_named_configuration_is_read_for_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            path = cli.config_path(cwd)
            path.parent.mkdir()
            path.write_text(
                "[environments.old]\n"
                'host = "tstof"\n'
                'local_dir = "/tmp/old"\n'
                'remote_dir = "/home/pi"\n',
                encoding="utf-8",
            )
            expected = cli.Config("tstof", Path("/tmp/old"), PurePosixPath("/home/pi"))
            self.assertEqual(cli.load_config(Path("/tmp/old"), cwd), expected)

    def test_setup_adds_configurations_by_local_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            config_file = cli.config_path(cwd)
            with patch.object(cli, "config_path", return_value=config_file), patch.object(cli, "validate_setup"), patch.object(
                cli, "resolve_remote_dir", return_value=PurePosixPath("/home/pi")
            ), patch.object(cli.sys, "argv", ["remote-agent-harness", "tstof", "/tmp/one:."]):
                self.assertEqual(cli.setup_main(), 0)
            with patch.object(cli, "config_path", return_value=config_file), patch.object(cli, "validate_setup"), patch.object(
                cli, "resolve_remote_dir", return_value=PurePosixPath("/home/pi")
            ), patch.object(cli.sys, "argv", ["remote-agent-harness", "tstof", "/tmp/two:."]):
                self.assertEqual(cli.setup_main(), 0)
            self.assertEqual(len(cli.parse_configurations(config_file)), 2)

    def test_parse_harness_source(self) -> None:
        self.assertEqual(
            cli.parse_harness_source("remote-agent-harness@rpvai:/home/mugi/project"),
            ("rpvai", PurePosixPath("/home/mugi/project")),
        )

    def test_remote_target_translates_subdirectory(self) -> None:
        mount = cli.Mount(
            Path("/mnt/project"),
            "fuse.sshfs",
            "remote-agent-harness@rpvai:/home/mugi/project",
        )
        with patch.object(cli, "list_mounts", return_value=[mount]):
            host, remote = cli.remote_target(Path("/mnt/project/src"))
        self.assertEqual(host, "rpvai")
        self.assertEqual(remote, PurePosixPath("/home/mugi/project/src"))

    def test_run_remote_uses_remote_working_directory(self) -> None:
        with patch.object(
            cli,
            "remote_target",
            return_value=("rpvai", PurePosixPath("/home/mugi/project/src")),
        ), patch.object(cli.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            cli.run_remote(["whoami"], Path("/mnt/project/src"))
        self.assertEqual(
            run.call_args.args[0],
            ["ssh", "rpvai", "cd -- /home/mugi/project/src && exec whoami"],
        )

    def test_run_remote_forwards_ssh_options(self) -> None:
        with patch.object(
            cli,
            "remote_target",
            return_value=("tstof", PurePosixPath("/home/pi")),
        ), patch.object(cli.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            cli.run_remote(["sudo", "apt", "update"], Path("/mnt/project"), ssh_options=["-tt"])
        self.assertEqual(
            run.call_args.args[0],
            ["ssh", "-tt", "tstof", "cd -- /home/pi && exec sudo apt update"],
        )

    def test_remote_main_separates_ssh_options(self) -> None:
        with patch.object(cli, "run_remote") as run_remote, patch.object(
            cli.sys, "argv", ["remote", "-X", "-o", "ConnectTimeout=5", "--", "whoami"]
        ):
            self.assertEqual(cli.remote_main(), 0)
        self.assertEqual(run_remote.call_args.args[0], ["whoami"])
        self.assertEqual(run_remote.call_args.kwargs, {"ssh_options": ["-X", "-o", "ConnectTimeout=5"]})

    def test_instruction_config_falls_back_to_mounted_directory(self) -> None:
        mount = cli.Mount(Path("/mnt/project"), "fuse.sshfs", "remote-agent-harness@host:/srv/project")
        with patch.object(cli, "config_path", return_value=Path("/missing/config.toml")), patch.object(
            cli, "list_mounts", return_value=[mount]
        ):
            config = cli.instruction_config(Path("/mnt/project"))
        self.assertEqual(config, cli.Config("host", Path("/mnt/project"), PurePosixPath("/srv/project")))

    def test_render_working_instructions_contains_mount_details(self) -> None:
        content = cli.render_working_instructions(
            cli.Config("host", Path("/mnt/project"), PurePosixPath("/srv/project"))
        )
        self.assertIn("`host:/srv/project`", content)
        self.assertIn("`remote -tt -- <command>`", content)

    def test_instructions_main_generates_control_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            config_file = cli.config_path(cwd)
            cli.write_config(cli.Config("host", Path("/mnt/project"), PurePosixPath("/srv/project")), cwd)
            output = io.StringIO()
            with patch.object(cli, "config_path", return_value=config_file), patch.object(
                cli.sys, "argv", ["remote-agent-instructions"]
            ), redirect_stdout(output):
                self.assertEqual(cli.instructions_main(), 0)
            self.assertIn("# Remote Agent Harness Control Directory", output.getvalue())
            self.assertIn("`/mnt/project` maps to `host:/srv/project`", output.getvalue())

    def test_mount_main_attempts_all_configurations_after_failure(self) -> None:
        first = cli.Config("host-a", Path("/mnt/a"), PurePosixPath("/srv/a"))
        second = cli.Config("host-b", Path("/mnt/b"), PurePosixPath("/srv/b"))
        with patch.object(cli, "config_path", return_value=Path("/config.toml")), patch.object(
            Path, "is_file", return_value=True
        ), patch.object(cli, "parse_configurations", return_value={first.local_dir: first, second.local_dir: second}), patch.object(
            cli, "mount", side_effect=[ValueError("unavailable"), None]
        ) as mount, patch.object(cli.sys, "argv", ["remote-mount"]):
            self.assertEqual(cli.mount_main(), 1)
        self.assertEqual(mount.call_args_list[0].args, (first,))
        self.assertEqual(mount.call_args_list[1].args, (second,))

    def test_unmount_main_handles_all_configurations(self) -> None:
        first = cli.Config("host-a", Path("/mnt/a"), PurePosixPath("/srv/a"))
        second = cli.Config("host-b", Path("/mnt/b"), PurePosixPath("/srv/b"))
        with patch.object(cli, "config_path", return_value=Path("/config.toml")), patch.object(
            Path, "is_file", return_value=True
        ), patch.object(cli, "parse_configurations", return_value={first.local_dir: first, second.local_dir: second}), patch.object(
            cli, "unmount"
        ) as unmount, patch.object(cli.sys, "argv", ["remote-unmount"]):
            self.assertEqual(cli.unmount_main(), 0)
        self.assertEqual(unmount.call_args_list[0].args, (first,))
        self.assertEqual(unmount.call_args_list[1].args, (second,))

    def test_unmount_rejects_unrelated_mount(self) -> None:
        config = cli.Config("rpvai", Path("/mnt/project"), PurePosixPath("/home/mugi/project"))
        unrelated = cli.Mount(Path("/mnt/project"), "ext4", "/dev/sda1")
        with patch.object(cli, "command_exists"), patch.object(cli, "list_mounts", return_value=[unrelated]):
            with self.assertRaisesRegex(ValueError, "unrelated"):
                cli.unmount(config)

    def test_unmount_requires_leaving_mount(self) -> None:
        config = cli.Config("rpvai", Path.cwd(), PurePosixPath("/home/mugi/project"))
        mounted = cli.Mount(
            Path.cwd(),
            "fuse.sshfs",
            "remote-agent-harness@rpvai:/home/mugi/project",
        )
        with patch.object(cli, "command_exists"), patch.object(cli, "list_mounts", return_value=[mounted]):
            with self.assertRaisesRegex(ValueError, "leave the mounted"):
                cli.unmount(config)
