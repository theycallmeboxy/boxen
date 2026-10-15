# boxen

One Docker container per project on an Ubuntu VM, each running a Claude Code
[Remote Control](https://code.claude.com/docs/en/remote-control) server. You
drive every project from the Claude app's Code tab; the VM does the work.

All containers share one `~/.claude` (login, settings, CLAUDE.md, skills,
memory) through the `claude-config` Docker volume, so you sign in once.

## Requirements

- An Ubuntu VM (24.04 tested against), your normal user with sudo.
- A Claude **Pro, Max, Team or Enterprise** plan, signed in with your claude.ai
  account. Remote Control doesn't work with API keys or `claude setup-token`
  tokens. On Team/Enterprise, an Owner must first turn on the Remote Control
  toggle in the Claude Code admin settings.

## Install

```bash
sudo apt install -y gh git
gh auth login                                   # this repo is private
gh repo clone theycallmeboxy/boxen ~/boxen
~/boxen/install.sh
```

`install.sh` is safe to re-run. It:

1. installs Docker CE + the compose plugin from Docker's apt repo (and stops
   with instructions if Docker is installed as a snap, which can't work here),
2. adds you to the `docker` group,
3. links `boxen` into `~/.local/bin`,
4. runs `boxen setup`: creates `~/.config/boxen/boxen.env`, the
   `claude-config` volume, and the base image.

Then log out and back in, fill in `~/.config/boxen/boxen.env` (GitHub token,
git identity, timezone), and create your first project.

## Create a project

```bash
boxen new <name> [git-url]
```

`<name>` is the project's name: its folder (`~/src/<name>`), its container
(`boxen-<name>`), and the session name you'll see in the Claude app. Use
lowercase letters and digits, with single `-` or `_` between them, up to 40
characters.

`[git-url]` is optional. It's the repo whose code you want in the project.
Pick the case that matches what you have:

| You have | Run | Result |
|---|---|---|
| A repo on GitHub | `boxen new myproj https://github.com/you/myproj.git` | Cloned into `~/src/myproj` |
| Nothing yet (new project) | `boxen new myproj` | Empty `~/src/myproj`; `git init` and add a remote later |
| Code already in `~/src/myproj` (e.g. copied from another machine) | `boxen new myproj` | Uses the folder as-is |

boxen refuses to clone into a folder that already has files, so it never
overwrites existing code.

Use HTTPS URLs (`https://github.com/...`), not SSH (`git@github.com:...`).
The clone runs inside the container and authenticates with the `GH_TOKEN` in
`boxen.env` through the GitHub CLI, which is what makes private repos work.
The containers have no SSH keys.

What `boxen new` does, in order:

1. Builds the project image.
2. Clones the repo, if you gave a URL.
3. Signs you in to Claude (first project only; every project shares the login).
   Open the URL it prints on any device, sign in, paste the code back.
4. Runs two one-time prompts. Answer **y** to "trust this folder" and (first
   time ever) "enable Remote Control", then press **Ctrl+C** once the session
   URL/QR appears.
5. Starts the container and checks it stays up. The project shows up in the
   Claude app's Code tab as `<name>`.

If anything fails or you interrupt it, `new` cleans up after itself so you
can simply run it again.

## Your Claude config

Everything in the shared `~/.claude` applies to every project.

- **Bring your personal config from another machine.** Copy your old
  `~/.claude` folder to the VM (for example `scp -r ~/.claude vm:old-claude`),
  then run `boxen import ~/old-claude`. It copies `CLAUDE.md`, `rules/`,
  `skills/`, `agents/` and `commands/`. It deliberately skips `settings.json`
  (it may hold Mac-specific paths or hooks; copy settings across by hand) and
  per-project auto memory (that's keyed to the old machine's paths).
- **Edit it later:** `boxen config` opens a shell in the shared `~/.claude`.
- **Phone notifications** when a task finishes or Claude needs a decision:
  run `boxen claude <name>`, then `/config`, and turn on **Push when Claude
  decides** and/or **Push when actions required**. It's stored in the shared
  config, so once covers every project. The Claude app on your phone must be
  signed in to the same account.

## Commands

| Command | What it does |
|---|---|
| `boxen ls` | Projects, container status and restart counts |
| `boxen logs <name> [-f]` | Remote Control server output |
| `boxen shell <name>` | Bash inside the container |
| `boxen claude <name>` | Terminal Claude session inside the container |
| `boxen up <name>` | Start, or apply changes to `boxen.env` / `project.env` / `compose.yaml` |
| `boxen down <name>` | Stop |
| `boxen restart <name>` | Recreate the container (also picks up env changes) |
| `boxen trust <name>` | Re-run the one-time prompts |
| `boxen login` | Sign in again, then restart running projects |
| `boxen import <dir>` / `boxen config` | Manage the shared `~/.claude` |
| `boxen rebuild <name>` | Apply edits to that project's Dockerfile |
| `boxen regen <name>` | Replace its `compose.yaml` with the current template (old one kept as `.bak`) |
| `boxen update` | Fresh base image (Ubuntu patches + newest stable Claude Code), rebuild all |
| `boxen self-update` | `git pull` this repo |
| `boxen rm <name>` | Remove container, image and config; keeps the code and its Claude history |

## Customizing

| To change | Edit | Then |
|---|---|---|
| Tools for every project | `~/.config/boxen/base.local.Dockerfile` | `boxen update` |
| Tools for one project | `~/.config/boxen/projects/<name>/Dockerfile` | `boxen rebuild <name>` |
| Env for every project | `~/.config/boxen/boxen.env` | `boxen restart <name>` per project |
| Env for one project (e.g. its own `GH_TOKEN`) | `~/.config/boxen/projects/<name>/project.env` | `boxen restart <name>` |
| Container settings (limits, Remote Control flags) | `~/.config/boxen/projects/<name>/compose.yaml` | `boxen up <name>` |

`base.local.Dockerfile` must start with `FROM boxen-base:core`. Containers
have no sudo, so install as root there and switch back to your user:

```dockerfile
FROM boxen-base:core
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs npm \
 && rm -rf /var/lib/apt/lists/*
USER your-username
```

Don't edit files in this repo to customize; that would block
`boxen self-update`. Template changes from later boxen versions reach
existing projects only through `boxen regen <name>`.

## Lifecycle and updates

Containers run indefinitely but are disposable. What matters lives outside
them: code in `~/src`, Claude state in the `claude-config` volume, and
toolchains in the Dockerfiles. Anything installed by hand inside a container
is gone after a rebuild.

`restart: unless-stopped` brings containers back after VM reboots and after
the Remote Control server exits during a long network outage. Sessions
reconnect if the server restarts in the same folder within about 4 hours.

Claude Code comes from Anthropic's signed apt repo (stable channel) and
doesn't auto-update inside containers. Upgrade monthly or when you want a
new release:

```bash
boxen self-update && boxen update
```

`update` keeps going if one project fails to rebuild and lists the failures
at the end. It also prunes old build cache.

Backups: Proxmox backups of the VM cover `~/src` and the `claude-config`
volume. They also contain your GitHub token and Claude login, so treat them
as secrets.

## Security model

The VM is the hard boundary. Inside it:

- **Containers run as your UID with no sudo and `no-new-privileges`.** They
  can't become root, so they can't plant setuid binaries in `~/src` (your home
  on the VM). They can't reach the Docker socket.
- **The shared `claude-config` volume isn't isolated between projects.** Any
  project's container can read the Claude login (including its refresh token)
  and every project's transcripts and memory. That's the trade for signing in
  once. Don't run code you don't trust here; use a separate VM for that.
- **`GH_TOKEN` is visible to everything in the container**: Claude, build
  scripts, dependencies' install scripts. Use a fine-grained token with only
  the permissions in `boxen.env`, or a per-repo token in `project.env`.
- **Containers can reach anything the VM can**, including your LAN (NAS,
  router admin, Proxmox UI). Put the VM on its own VLAN and block RFC1918
  destinations there except what you need (DNS, any internal services you
  want Claude to use).
- **Cloned repos can carry hooks and settings** (`.claude/settings.json`,
  `.mcp.json`). That's what the per-folder trust prompt is for; only trust
  repos you'd run yourself.

## Troubleshooting

- **A project doesn't show in the app:** `boxen ls`. A climbing restart count
  means the server keeps exiting; `boxen logs <name>` says why.
- **Sign-in expired or wrong account:** `boxen login`.
- **"Workspace not trusted" in the logs:** `boxen trust <name>`.
- **Env change didn't take effect:** `boxen restart <name>` (a plain Docker
  restart doesn't re-read env files).
- **Clone failed:** check `GH_TOKEN` in `boxen.env` covers that repo, then
  run `boxen new` again.

## Network egress (VLAN allowlist)

Claude Code: `api.anthropic.com`, `claude.ai`, `claude.com`,
`platform.claude.com`, `downloads.claude.ai`. Optional:
`mcp-proxy.anthropic.com` (claude.ai connectors), `registry.npmjs.org`
(plugins, npx MCP servers), `raw.githubusercontent.com` (release notes),
`code.claude.com` (doc lookups). Telemetry hosts are optional. See
[network configuration](https://code.claude.com/docs/en/network-config).

GitHub: `github.com`, `api.github.com` (gh CLI).

Install and image builds: `download.docker.com`, `archive.ubuntu.com`,
`security.ubuntu.com` (or `ports.ubuntu.com` on arm64), `cli.github.com`,
Docker Hub (`registry-1.docker.io`, `auth.docker.io`,
`production.cloudflare.docker.com`).

Plus whatever package registries your projects use.

Don't set `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`: it disables Remote
Control. `boxen.env` lists the other settings that do.
