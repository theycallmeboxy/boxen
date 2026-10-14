# boxen

One Docker container per project on an Ubuntu VM, each running a Claude Code
[Remote Control](https://code.claude.com/docs/en/remote-control) server. You
drive every project from the Claude app's Code tab; the VM does the work.

All containers share one `~/.claude` (login, settings, CLAUDE.md, skills,
memory) through the `claude-config` Docker volume, so you sign in once.

## Install

On a fresh Ubuntu VM, as your normal user (needs sudo):

```bash
git clone https://github.com/theycallmeboxy/boxen.git ~/boxen
~/boxen/install.sh
```

`install.sh` is safe to re-run. It:

1. installs Docker CE + the compose plugin from Docker's apt repo,
2. adds you to the `docker` group,
3. links `boxen` into `~/.local/bin`,
4. runs `boxen setup`: creates `~/.config/boxen/boxen.env`, the
   `claude-config` volume, and the `boxen-base` image.

Then log out and back in, fill in `~/.config/boxen/boxen.env` (GitHub token,
git identity), and create your first project.

You need a Pro, Max, Team or Enterprise Claude plan; Remote Control doesn't
work with API keys.

## Create a project

```bash
boxen new myproj https://github.com/you/myproj.git   # or omit the URL
```

This builds the project image, clones into `~/src/myproj`, signs you in
(first project only), and runs two one-time prompts. Answer **y** to "trust
this folder" and (first time ever) "enable Remote Control", then press
**Ctrl+C** once the session URL/QR appears. The container then starts on its
own and shows up in the Claude app as `myproj`.

Use HTTPS clone URLs: the `GH_TOKEN` in `boxen.env` authenticates git to
github.com through the GitHub CLI.

## Layout

```
~/boxen/                          this repo (script, base image, templates)
~/.config/boxen/boxen.env         shared env for all containers (chmod 600)
~/.config/boxen/projects/<name>/  compose.yaml + Dockerfile per project
~/src/<name>/                     the code, mounted at the same path inside
docker volume: claude-config      shared ~/.claude
```

## Commands

| Command | What it does |
|---|---|
| `boxen ls` | Projects and container status |
| `boxen logs <name> [-f]` | Remote Control server output |
| `boxen shell <name>` | Bash inside the container |
| `boxen claude <name>` | Terminal Claude session inside the container |
| `boxen up / down / restart <name>` | Container lifecycle |
| `boxen trust <name>` | Re-run the one-time prompts |
| `boxen rebuild <name>` | Apply edits to that project's Dockerfile/compose |
| `boxen update` | Fresh base image (Ubuntu patches + newest stable Claude Code), rebuild all |
| `boxen self-update` | `git pull` this repo |
| `boxen rm <name>` | Remove container/image/config; keeps `~/src/<name>` |

## Lifecycle

Containers run indefinitely but are disposable. What matters lives outside
them: code in `~/src`, Claude state in the `claude-config` volume, toolchains
in the Dockerfiles. Anything installed by hand inside a container (including
by Claude via sudo) is gone after a rebuild, so put permanent additions in
`base/Dockerfile` (every project) or
`~/.config/boxen/projects/<name>/Dockerfile` (one project).

`restart: unless-stopped` brings containers back after VM reboots and after
the Remote Control server exits during a long network outage. Sessions
reconnect if the server restarts in the same folder within about 4 hours.

Claude Code comes from Anthropic's signed apt repo (stable channel) and
doesn't auto-update inside containers. Upgrade with:

```bash
boxen self-update && boxen update
```

Monthly is a reasonable cadence. Proxmox backups of the VM cover the volume
and `~/src`.

## Network egress (VLAN allowlist)

Claude Code: `api.anthropic.com`, `claude.ai`, `claude.com`,
`platform.claude.com`, `downloads.claude.ai`. Optional:
`mcp-proxy.anthropic.com` (claude.ai connectors), `raw.githubusercontent.com`
(release notes), `code.claude.com` (doc lookups). Telemetry hosts are optional.
See [network configuration](https://code.claude.com/docs/en/network-config).

Install and image builds: `download.docker.com`, Ubuntu mirrors,
`cli.github.com`, Docker Hub. Work: `github.com` plus your projects' package
registries.

Don't set `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`: it disables Remote Control.
