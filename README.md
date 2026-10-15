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

1. installs the few tools boxen uses on the VM (`jq`, `openssl`, `cron`, ...),
2. installs Docker CE + the compose plugin from Docker's apt repo (and stops
   with instructions if Docker is installed as a snap, which can't work here),
3. adds you to the `docker` group,
4. links `boxen` into `~/.local/bin`,
5. runs `boxen setup`: creates `~/.config/boxen/boxen.env`, the
   `claude-config` volume, and the base image.

Then log out and back in, run `boxen github` (next section), check the
timezone in `~/.config/boxen/boxen.env`, and create your first project.

## Connect GitHub

```bash
boxen github
```

Containers never get a long-lived GitHub token. Instead, a private GitHub App
that you own mints a **1-hour token for each project, scoped to that
project's repo only**. The app's private key stays on the VM; each container
sees only its own token, through a read-only mount, and cron replaces it
every 30 minutes. git and `gh` inside the container pick it up automatically.

`boxen github` walks you through it once (about 3 minutes):

1. It prints a link that pre-fills a new GitHub App (name `boxen-<you>`,
   private, no webhook, the permissions below). Click **Create GitHub App**,
   copy the **Client ID**, and click **Generate a private key** (a `.pem`
   downloads).
2. Paste the Client ID, then paste the `.pem` file's contents (or its path, if
   you've copied it to the VM).
3. It prints the app's install link. Choose **All repositories** so repos you
   create later work without coming back, or pick specific ones.

It also sets your commit identity for containers to your GitHub name and
**no-reply address** (`ID+username@users.noreply.github.com`), so your real
email stays out of commits. If you've already set `GIT_AUTHOR_EMAIL` in
`boxen.env`, it leaves yours alone. To make GitHub refuse any push that
would expose your private email, turn on "Block command line pushes that
expose my email" in GitHub's email settings.

App permissions: Contents, Pull requests, Issues (write); Actions, Checks,
Statuses (read), so Claude can push branches, open PRs, work with issues, and
read CI results. Changes under `.github/workflows/` need the **Workflows**
permission too; add it in the app's settings if you want Claude editing CI.

Good to know:

- Pushes, PRs and comments made with these tokens show as `boxen-<you>[bot]`.
  Commits themselves still carry your name and no-reply address.
- A project gets a token once its folder has a github.com `origin` remote.
  A brand-new project has no GitHub access until you add one (then
  `boxen tokens <name>`).
- Containers can't create new GitHub repos. Create them on the VM with
  `gh repo create`, then use the URL with `boxen new`.
- `boxen tokens` refreshes immediately and shows each project's repo and
  expiry. Cron problems are logged to `~/.config/boxen/tokens.log`.
- The VM's clock must be right (Ubuntu's default time sync is fine); GitHub
  rejects app requests from a clock that's off by more than a minute or so.
- Skipping the app: you can instead put a personal access token in
  `boxen.env` or a project's `project.env` as `GH_TOKEN`; git and `gh` fall
  back to it.

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
The clone runs inside the container with a token boxen mints for that repo
(see "Connect GitHub"), which is what makes private repos work. The
containers have no SSH keys.

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
| `boxen github [--reset]` | Connect GitHub (or redo it with a new app/key) |
| `boxen tokens [name...]` | Refresh GitHub tokens now and show their repos and expiry |
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
| Env for one project | `~/.config/boxen/projects/<name>/project.env` | `boxen restart <name>` |
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
volume. They also contain the GitHub App's private key and your Claude login,
so treat them as secrets.

## Security model

The VM is the hard boundary. Inside it:

- **Containers run as your UID with no sudo and `no-new-privileges`.** They
  can't become root, so they can't plant setuid binaries in `~/src` (your home
  on the VM). They can't reach the Docker socket.
- **The shared `claude-config` volume isn't isolated between projects.** Any
  project's container can read the Claude login (including its refresh token)
  and every project's transcripts and memory. That's the trade for signing in
  once. Don't run code you don't trust here; use a separate VM for that.
- **A project's GitHub token is visible to everything in its container**:
  Claude, build scripts, dependencies' install scripts. That's why it's
  limited to that one repo and expires within the hour. The GitHub App's
  private key, which can mint tokens for every repo the app is installed on,
  lives only on the VM (`~/.config/boxen/github/`) and is never mounted into
  containers. Anyone with a shell on the VM as you can use it, so guard the
  VM accordingly.
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
- **Clone, push or `gh` fails with an auth error:** `boxen tokens <name>`
  says why. Usually the app isn't installed on that repo (add it on the
  app's install page) or the repo needs a permission the app lacks (e.g.
  Workflows). Check `~/.config/boxen/tokens.log` for cron-time failures.

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
