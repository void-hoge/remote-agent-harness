# remote-agent-harness

`remote-agent-harness` mounts a remote work directory with SSHFS and runs
commands on the SSH host with the `remote` prefix. The harness stores all of
its configuration locally and does not install or write anything on the remote
host.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- `ssh`, `sshfs`, and `fusermount3` on the local machine
- An SSH host alias or host name that accepts key-based authentication

Install the command entry points locally:

```sh
uv tool install .
```

## Setup

From a control directory, create a configuration for an existing empty local
mount directory and an existing remote directory:

```sh
remote-agent-harness <host> <local-dir>:<remote-dir>
```

For example, `remote-agent-harness build-host /work/project:project` mounts the
remote `~/project` directory at `/work/project` when requested below.

This creates `./.harness/config.toml` in the current directory. The final path
is relative to the remote user's home directory; absolute remote paths are also
accepted. It validates the SSH connection and both directory prerequisites, but
does not mount the filesystem or create files on the remote host.

## Work session

Start a session from the same control directory. Pass the configured local
mount directory to select the target configuration:

```sh
remote-mount <local-dir>
```

Work under the local mount point. Prefix commands that must run remotely with
`remote`:

```sh
cd <local-dir>
remote whoami
```

`remote` finds the SSHFS mount containing the current directory and runs the
command at the matching remote path. For example, from local `src/`, `remote
pytest` runs on the host from remote `src/`.

Pass SSH options before `--`. This forces an SSH pseudo-terminal for commands
that require an interactive terminal, such as `sudo`:

```sh
remote -tt -- sudo apt update
```

For example, X11 forwarding is available as `remote -X -- xclock`. SSH options
require `--` before the remote command; ordinary commands need no separator.
A password prompt still requires an interactive terminal capable of supplying
the password.

Finish a session from the control directory:

```sh
remote-unmount <local-dir>
```

Do not run `remote-unmount` while the shell is inside the mounted directory;
leave the mount first.

Unmounting stops the SSHFS connection and its keepalive traffic. While mounted,
the connection uses SSHFS reconnect and keepalive options; a connection loss
can still interrupt in-progress file writes or commands.

`uv` manages this local tool. It does not activate an environment merely when
entering a directory, and the harness does not install `uv` on the remote host.

## Multiple mounts

One control directory can contain multiple mount configurations. Add each one
with a distinct local mount directory:

```sh
remote-agent-harness host-a /work/project-a:project-a
remote-agent-harness host-b /work/project-b:project-b
```

Select a mount by its local directory. Both may be mounted at the same time,
and `remote` selects the correct host and remote path from the current SSHFS
mount.

Show all configured mounts and their status from the control directory:

```sh
remote-status
```
