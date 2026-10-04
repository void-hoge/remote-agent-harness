"""Command-line entry points for remote-agent-harness."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import shlex
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass


CONFIG_PATH = Path(".harness/config.toml")
FSNAME_PREFIX = "remote-agent-harness@"


@dataclass(frozen=True)
class Config:
    host: str
    local_dir: Path
    remote_dir: PurePosixPath


@dataclass(frozen=True)
class Mount:
    mountpoint: Path
    filesystem: str
    source: str


def error(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def command_exists(command: str) -> None:
    if shutil.which(command) is None:
        raise ValueError(f"{command} is not installed or is not on PATH")


def parse_mapping(value: str) -> tuple[Path, PurePosixPath]:
    if ":" not in value:
        raise ValueError("mapping must be <local-dir>:<remote-dir>")

    local_value, remote_value = value.split(":", 1)
    if not local_value or not remote_value:
        raise ValueError("mapping must contain both local and remote directories")

    local_dir = Path(local_value).expanduser().absolute()
    remote_dir = PurePosixPath(remote_value)
    return local_dir, remote_dir


def resolve_remote_dir(host: str, remote_dir: PurePosixPath) -> PurePosixPath:
    if remote_dir.is_absolute():
        return remote_dir

    relative_dir = remote_dir
    if str(remote_dir) == "~":
        relative_dir = PurePosixPath(".")
    elif str(remote_dir).startswith("~/"):
        relative_dir = PurePosixPath(str(remote_dir)[2:])
    if ".." in relative_dir.parts:
        raise ValueError("remote directory must not contain '..'")

    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, "printf '%s\\n' \"$HOME\""],
        check=False,
        capture_output=True,
        text=True,
    )
    remote_home = PurePosixPath(result.stdout.strip())
    if result.returncode != 0 or not remote_home.is_absolute():
        raise ValueError(f"cannot determine the home directory on {host}")
    return remote_home / relative_dir


def config_path(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()) / CONFIG_PATH


def parse_configurations(path: Path) -> dict[Path, Config]:
    try:
        with path.open("rb") as file:
            raw = tomllib.load(file)
    except (KeyError, OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"invalid configuration: {path}") from exc
    raw_mounts = raw.get("mounts")
    if raw_mounts is None:
        # Read configurations written by versions that used environment names.
        raw_environments = raw.get("environments")
        if not isinstance(raw_environments, dict):
            raise ValueError(f"invalid configuration: {path}")
        raw_mounts = list(raw_environments.values())
    if not isinstance(raw_mounts, list):
        raise ValueError(f"invalid configuration: {path}")

    configurations: dict[Path, Config] = {}
    for raw_mount in raw_mounts:
        if not isinstance(raw_mount, dict):
            raise ValueError(f"invalid configuration: {path}")
        host = raw_mount.get("host")
        local_dir = raw_mount.get("local_dir")
        remote_dir = raw_mount.get("remote_dir")
        if not all(isinstance(value, str) and value for value in (host, local_dir, remote_dir)):
            raise ValueError(f"invalid configuration: {path}")
        parsed_remote = PurePosixPath(remote_dir)
        if not parsed_remote.is_absolute():
            raise ValueError(f"invalid configuration: {path}")
        parsed_local = Path(local_dir).expanduser().absolute()
        if parsed_local in configurations:
            raise ValueError(f"duplicate local directory in configuration: {parsed_local}")
        configurations[parsed_local] = Config(host, parsed_local, parsed_remote)
    return configurations


def write_config(config: Config, cwd: Path | None = None) -> Path:
    path = config_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    configurations = parse_configurations(path) if path.exists() else {}
    configurations[config.local_dir] = config
    content = ""
    for local_dir in sorted(configurations, key=str):
        configuration = configurations[local_dir]
        content += (
            "[[mounts]]\n"
            "host = " + json.dumps(configuration.host) + "\n"
            "local_dir = " + json.dumps(str(configuration.local_dir)) + "\n"
            "remote_dir = " + json.dumps(str(configuration.remote_dir)) + "\n\n"
        )
    path.write_text(content, encoding="utf-8")
    return path


def load_config(local_dir: Path, cwd: Path | None = None) -> Config:
    path = config_path(cwd)
    if not path.is_file():
        raise ValueError(f"configuration not found: {path}")
    configurations = parse_configurations(path)
    try:
        return configurations[local_dir]
    except KeyError as exc:
        available = ", ".join(str(directory) for directory in sorted(configurations, key=str)) or "none"
        raise ValueError(f"local directory is not configured: {local_dir} (available: {available})") from exc


def validate_setup(config: Config) -> None:
    command_exists("sshfs")
    command_exists("fusermount3")
    if not config.local_dir.is_dir():
        raise ValueError(f"local directory does not exist: {config.local_dir}")
    if any(config.local_dir.iterdir()):
        raise ValueError(f"local directory must be empty before mounting: {config.local_dir}")

    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", config.host, "test", "-d", str(config.remote_dir)],
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(
            f"cannot connect to {config.host} or remote directory does not exist: {config.remote_dir}"
        )


def decode_mount_path(value: str) -> str:
    for escaped, character in ((r"\040", " "), (r"\011", "\t"), (r"\012", "\n"), (r"\134", "\\")):
        value = value.replace(escaped, character)
    return value


def list_mounts() -> list[Mount]:
    mounts: list[Mount] = []
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError("cannot read Linux mount information") from exc

    for line in lines:
        before, separator, after = line.partition(" - ")
        fields = before.split()
        after_fields = after.split()
        if not separator or len(fields) < 5 or len(after_fields) < 2:
            continue
        mounts.append(
            Mount(
                mountpoint=Path(decode_mount_path(fields[4])),
                filesystem=after_fields[0],
                source=decode_mount_path(after_fields[1]),
            )
        )
    return mounts


def harness_mount(config: Config) -> Mount | None:
    for mount in list_mounts():
        if mount.mountpoint == config.local_dir:
            return mount
    return None


def mount_source(config: Config) -> str:
    return f"{FSNAME_PREFIX}{config.host}:{config.remote_dir}"


def mount(config: Config) -> None:
    command_exists("sshfs")
    existing = harness_mount(config)
    if existing is not None:
        if existing.filesystem == "fuse.sshfs" and existing.source == mount_source(config):
            print(f"already mounted: {config.local_dir}")
            return
        raise ValueError(f"local directory is already a mount point: {config.local_dir}")
    if not config.local_dir.is_dir():
        raise ValueError(f"local directory does not exist: {config.local_dir}")
    if any(config.local_dir.iterdir()):
        raise ValueError(f"local directory must be empty before mounting: {config.local_dir}")

    options = ",".join(
        (
            f"fsname={mount_source(config)}",
            "reconnect",
            "ServerAliveInterval=15",
            "ServerAliveCountMax=3",
            "ConnectTimeout=10",
        )
    )
    subprocess.run(
        ["sshfs", f"{config.host}:{config.remote_dir}", str(config.local_dir), "-o", options],
        check=True,
    )
    print(f"mounted {config.host}:{config.remote_dir} at {config.local_dir}")


def unmount(config: Config) -> None:
    command_exists("fusermount3")
    existing = harness_mount(config)
    if existing is None:
        print(f"not mounted: {config.local_dir}")
        return
    if existing.filesystem != "fuse.sshfs" or existing.source != mount_source(config):
        raise ValueError(f"refusing to unmount an unrelated filesystem: {config.local_dir}")
    try:
        Path.cwd().resolve().relative_to(config.local_dir)
    except ValueError:
        pass
    else:
        raise ValueError(f"leave the mounted directory before unmounting: {config.local_dir}")
    subprocess.run(["fusermount3", "-u", str(config.local_dir)], check=True)
    print(f"unmounted {config.local_dir}")


def parse_harness_source(source: str) -> tuple[str, PurePosixPath] | None:
    if not source.startswith(FSNAME_PREFIX):
        return None
    host, separator, remote_dir = source[len(FSNAME_PREFIX) :].partition(":")
    if not separator or not host or not remote_dir:
        return None
    remote_path = PurePosixPath(remote_dir)
    if not remote_path.is_absolute():
        return None
    return host, remote_path


def remote_target(cwd: Path) -> tuple[str, PurePosixPath]:
    cwd = cwd.resolve()
    candidates: list[tuple[Mount, str, PurePosixPath]] = []
    for mount in list_mounts():
        parsed = parse_harness_source(mount.source)
        if mount.filesystem != "fuse.sshfs" or parsed is None:
            continue
        try:
            relative = cwd.relative_to(mount.mountpoint)
        except ValueError:
            continue
        host, remote_root = parsed
        candidates.append((mount, host, remote_root / relative.as_posix()))
    if not candidates:
        raise ValueError("current directory is not inside a remote-agent-harness sshfs mount")
    _, host, remote_dir = max(candidates, key=lambda item: len(item[0].mountpoint.parts))
    return host, remote_dir


def mounted_config(local_dir: Path) -> Config:
    for mount in list_mounts():
        if mount.mountpoint != local_dir or mount.filesystem != "fuse.sshfs":
            continue
        parsed = parse_harness_source(mount.source)
        if parsed is not None:
            host, remote_dir = parsed
            return Config(host, local_dir, remote_dir)
    raise ValueError(f"local directory is not configured or mounted: {local_dir}")


def instruction_config(local_dir: Path) -> Config:
    path = config_path()
    if path.is_file():
        configurations = parse_configurations(path)
        if local_dir in configurations:
            return configurations[local_dir]
    return mounted_config(local_dir)


def render_control_instructions(configurations: dict[Path, Config]) -> str:
    lines = [
        "# Remote Agent Harness Control Directory",
        "",
        "This directory contains the local `.harness/config.toml` configuration.",
        "Do not edit that file directly. Add or update a mount with:",
        "",
        "```sh",
        "remote-agent-harness <host> <local-dir>:<remote-dir>",
        "```",
        "",
        "## Configured Mounts",
        "",
    ]
    if configurations:
        for local_dir, config in sorted(configurations.items(), key=lambda item: str(item[0])):
            lines.append(f"- `{local_dir}` maps to `{config.host}:{config.remote_dir}`")
    else:
        lines.append("- No mounts are configured.")
    lines.extend(
        (
            "",
            "## Workflow",
            "",
            "- Use `remote-status` to inspect mount state.",
            "- Use `remote-mount <local-dir>` before working in a configured mount.",
            "- To run a command on a remote environment, use `remote <command>` in the mounted local directory or its subdirectories.",
            "- Use `remote-unmount <local-dir>` only after leaving the mounted directory.",
        )
    )
    return "\n".join(lines) + "\n"


def render_working_instructions(config: Config) -> str:
    return "\n".join(
        (
            "# Remote Agent Harness Working Directory",
            "",
            f"This directory is an SSHFS mount of `{config.host}:{config.remote_dir}`.",
            "Edit files normally, but run commands that operate on the project on the remote host.",
            "",
            "## Remote Commands",
            "",
            "- Prefix commands with `remote`, for example `remote pytest` or `remote git status`.",
            "- Pass SSH options before `--`, for example `remote -X -- xclock`.",
            "- Use `remote -tt -- <command>` when the command requires an interactive terminal.",
            "- `remote` works only from this mounted directory or one of its subdirectories.",
            "",
            "## Mount Lifecycle",
            "",
            "Mount and unmount this directory from its control directory with `remote-mount` and `remote-unmount`.",
        )
    ) + "\n"


def run_remote(command: list[str], cwd: Path | None = None, *, ssh_options: list[str] | None = None) -> None:
    if not command:
        raise ValueError("usage: remote [ssh-options...] -- <command> [args...]")
    host, remote_dir = remote_target(cwd or Path.cwd())
    remote_command = f"cd -- {shlex.quote(str(remote_dir))} && exec {shlex.join(command)}"
    ssh_command = ["ssh", *(ssh_options or [])]
    ssh_command.extend((host, remote_command))
    result = subprocess.run(ssh_command, check=False)
    if result.returncode:
        raise SystemExit(result.returncode)


def setup_main() -> int:
    if len(sys.argv) != 3:
        return error("usage: remote-agent-harness <host> <local-dir>:<remote-dir>")
    try:
        local_dir, remote_dir = parse_mapping(sys.argv[2])
        config = Config(host=sys.argv[1], local_dir=local_dir, remote_dir=remote_dir)
        if not config.host or config.host.startswith("-"):
            raise ValueError("host must be an SSH host name or alias")
        config = Config(config.host, config.local_dir, resolve_remote_dir(config.host, config.remote_dir))
        validate_setup(config)
        path = write_config(config)
        print(f"wrote {path}")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return error(str(exc))
    return 0


def mount_main() -> int:
    if len(sys.argv) != 2:
        return error("usage: remote-mount <local-dir>")
    try:
        local_dir = Path(sys.argv[1]).expanduser().absolute()
        mount(load_config(local_dir))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return error(str(exc))
    return 0


def unmount_main() -> int:
    if len(sys.argv) != 2:
        return error("usage: remote-unmount <local-dir>")
    try:
        local_dir = Path(sys.argv[1]).expanduser().absolute()
        unmount(load_config(local_dir))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return error(str(exc))
    return 0


def remote_main() -> int:
    if len(sys.argv) < 2:
        return error("usage: remote [ssh-options...] -- <command> [args...]")
    arguments = sys.argv[1:]
    if "--" in arguments:
        separator = arguments.index("--")
        ssh_options = arguments[:separator]
        command = arguments[separator + 1 :]
    else:
        ssh_options = []
        command = arguments
        if command[0].startswith("-"):
            return error("SSH options require '--' before the remote command")
    try:
        run_remote(command, ssh_options=ssh_options)
    except SystemExit as exc:
        return int(exc.code)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return error(str(exc))
    return 0


def status_main() -> int:
    if len(sys.argv) != 1:
        return error("usage: remote-status")
    try:
        path = config_path()
        if not path.is_file():
            raise ValueError(f"configuration not found: {path}")
        print(f"Configuration: {path}")
        for local_dir, config in sorted(parse_configurations(path).items(), key=lambda item: str(item[0])):
            existing = harness_mount(config)
            if existing is None:
                state = "unmounted"
            elif existing.filesystem == "fuse.sshfs" and existing.source == mount_source(config):
                state = "mounted"
            else:
                state = "occupied"
            print(f"{state:<10} {local_dir}  {config.host}:{config.remote_dir}")
    except (OSError, ValueError) as exc:
        return error(str(exc))
    return 0


def instructions_main() -> int:
    if len(sys.argv) > 2:
        return error("usage: remote-agent-instructions [local-dir]")
    try:
        if len(sys.argv) == 1:
            path = config_path()
            if not path.is_file():
                raise ValueError(f"configuration not found: {path}")
            print(render_control_instructions(parse_configurations(path)), end="")
        else:
            local_dir = Path(sys.argv[1]).expanduser().absolute()
            print(render_working_instructions(instruction_config(local_dir)), end="")
    except (OSError, ValueError) as exc:
        return error(str(exc))
    return 0
