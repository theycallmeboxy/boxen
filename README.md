# boxen

Claude Code agents on an Ubuntu VM, one project folder each, with every
project's builds in a container of its own.

- **Agents run on the VM** as you, in `~/src/<project>`, inside Claude Code's
  [sandbox](https://code.claude.com/docs/en/sandboxing): a session's shell can
  write only its own project folder, reaches only GitHub, and can't use
  Docker. Its file tools ask before touching anything outside the project.
- **Builds run in containers**: each project has a build container
  (`boxen.<project>.env`, Ubuntu 24.04) and optional sidecars (databases,
  caches) under [rootless Docker](https://docs.docker.com/engine/security/rootless/),
  defined in the project's own `.boxen/` folder. Agents reach it with
  `boxen run`: root inside, which is you on the VM, so `apt` works and files
  land as yours. Toolchains never collide and never touch the VM.
- **Drive it from anywhere**: sessions are normal Claude Code sessions in
  boxen's tmux, so you can work in a terminal (`boxen claude <project>`,
  `boxen ui`, or plain `claude`) and in the Claude app at once: every
  session connects to [Remote Control](https://code.claude.com/docs/en/remote-control).

Needs an Ubuntu 24.04 VM with sudo, and a Claude Pro, Max, Team or
Enterprise plan signed in with a claude.ai account (on Team/Enterprise an
Owner must enable Remote Control).

## Install

```bash
sudo apt install -y gh git
gh auth login                                   # this repo is private
gh repo clone theycallmeboxy/boxen ~/boxen
~/boxen/install.sh
```

`install.sh` is safe to re-run. It installs Claude Code, the sandbox's
`bubblewrap` and `socat` (with the AppArmor profile Ubuntu 24.04 needs for
`bwrap`), Docker with rootless mode for your user (lingering on, cgroup
delegation for limits), the GitHub CLI, tmux and vim; links `boxen` into
`~/.local/bin`; runs `boxen setup` (the base image, boxen's instructions for
agents, git settings for `~/src`, the token timer); and installs boxen's
Claude Code sandbox rules as managed settings,
`/etc/claude-code/managed-settings.d/boxen.json` (see [Security](#security)).

Then: run `claude` once and `/login`, `boxen github`, `boxen new <name>`,
and `boxen doctor` to check the whole setup. Coming from boxen v1? See
[Moving from v1](#moving-from-v1).

## Layout

```
~/src/<project>/             the project: repos, worktrees, build output (the agent's only writable place)
~/src/<project>/.boxen/      its build container: Dockerfile, services.yaml (the agent may edit these)
~/.claude/                   Claude Code: login, settings, sessions, memory (shared by every project)
~/.config/boxen/             boxen's own: per-project compose files, repo lists, tokens (agents can't write here)
~/.local/share/docker/       rootless Docker: images and volumes, all named boxen.<project>.*
```

## Day to day

```bash
boxen new myproj https://github.com/you/myproj   # or no URL for an empty folder
boxen claude myproj        # a session in the project, in a tmux window (also in the app)
boxen ui                   # the dashboard: projects, sessions, files, terminals
boxen ls                   # build containers, sessions, serve, GitHub tokens
```

Sessions live in a tmux server of boxen's own (`tmux -L boxen`), so they keep
running when SSH drops: `boxen ui` or `boxen claude` again reattaches. Plain
`cd ~/src/myproj && claude` works too; `claude --resume` there finds the
project's earlier sessions.

Every interactive session also shows in the Claude app (boxen turns on
**Enable Remote Control for all sessions**, unless you've set it yourself).
To start new sessions from the app when nothing is running in a project,
keep a Remote Control server up for it:

```bash
boxen serve myproj         # a server in tmux window myproj~serve; back after reboots
boxen serve myproj --off
```

The first time, the server asks to trust the folder and to enable Remote
Control: answer in `boxen ui`.

### Commands

| Command | What it does |
|---|---|
| `new <name> [git-url]` | Create `~/src/<name>` (cloning the URL), its `.boxen/` and its build container |
| `ls [--json]` | Projects: build container, Claude sessions, serve, GitHub tokens |
| `claude <name> [args]` | A Claude session in the project (tmux window; in the app too) |
| `serve [<name> [--off]]` | A Remote Control server for the project, kept up |
| `ui [--restart]` | The dashboard |
| `shell <name>` | A root shell in the project's build container |
| `up <name\|all>` / `down <name\|all>` | Start (applying changes) / stop build containers and sidecars |
| `rm <name> [--yes]` | Remove the build container, image, sidecars and their data; the code stays |
| `repos <name> [add\|rm <owner/repo>...]` | The GitHub repos a project's tokens cover |
| `github` | Connect GitHub |
| `doctor [--lan <host:port>]` | Check rootless Docker, the sandbox and isolation |
| `migrate [--cleanup]` | Move from boxen v1 |
| `update` | Rebuild the base image and every build container |
| `self-update` | Update boxen and apply it |

## Build environments

A project's build container is defined in its own folder, so the agent can
set up its toolchain itself:

- `.boxen/Dockerfile`: `FROM boxen-env:latest` (Ubuntu 24.04 with
  build-essential, cmake, git, python3, curl). Built as root: `apt-get
  install` directly, no `USER` lines.
- `.boxen/services.yaml`: sidecars, each reachable from the build container
  at its service name. Image, environment, named volumes, healthcheck,
  depends_on and limits only: no ports, host paths, privileges or the Docker
  socket. It's parsed in a container that sees only that file, then checked
  by `bin/boxen-services` before anything starts.

From the project folder (this is what agents run):

```bash
boxen run make test            # in the build container, same folder, same path
boxen run -C api npm ci        # in a subfolder
boxen run -d web npm run dev   # in the background; --logs web, --stop web, --ps
boxen env apply                # rebuild after editing .boxen/ (errors are printed; nothing changes on a failure)
boxen env show                 # containers, sidecars, GitHub repos
boxen env logs postgres        # a sidecar's logs
```

Only the project folder and the download caches (`/root/.cache`, where pip,
npm, go and cargo's caches are pointed) survive a rebuild; everything else
in the container comes from the Dockerfiles, so what a project needs goes
there (rustup installs into `/opt/cargo`; set paths with `ENV`). Your own
changes go in
`~/.config/boxen/projects/<name>/compose.override.yaml` (service `env`, or a
sidecar's name; see the `.example` next to it): ports to the VM or LAN, host
folders, memory and CPU limits. Agents can't write there. Additions for every
project's base image: `~/.config/boxen/env.local.Dockerfile`, starting with
`FROM boxen-env:core`, then `boxen update`.

## Connect GitHub

```bash
boxen github
```

No agent gets a long-lived GitHub token. A private GitHub App that you own
mints 1-hour tokens per project, scoped to the repos on that project's list;
a systemd user timer refreshes them every 30 minutes. In a folder under
`~/src/<project>`, git (through boxen's credential helper, set up by an
`includeIf` in your `~/.gitconfig`) and `gh` (boxen's wrapper in
`~/.local/bin`) use that project's tokens and never your own login. The
app's private key is unreadable from the sandbox. The walkthrough takes
about 3 minutes:

1. It prints a link that pre-fills a new GitHub App (private, no webhook,
   permissions: Contents, Pull requests, Issues, Workflows write; Actions,
   Checks, Statuses read). Click **Create GitHub App**, copy the **Client
   ID**, and click **Generate a private key** (a `.pem` downloads). An app
   made by v1 has no Workflows permission, so pushes that touch
   `.github/workflows/` fail: add it in the app's settings on GitHub
   (Permissions, Workflows: Read and write) and accept it on the
   installation.
2. Paste the Client ID, then the `.pem` file's contents (not echoed; a `*`
   per line). If the file is on the VM: `boxen github --key /path/to/file.pem`.
3. It prints the app's install link. Choose **All repositories** so repos you
   create later work too.

It also sets the commit identity for repos under `~/src` to your GitHub name
and no-reply address (`ID+username@users.noreply.github.com`), unless one is
set already. Don't run `gh auth setup-git` on the VM: it puts gh's own
credential helper after boxen's; `boxen doctor` catches it and re-running
`install.sh` fixes it.

### Which repos a project can push to

Each project has a list of repos its tokens cover
(`~/.config/boxen/projects/<name>/repos`), changed only by you. It starts
with the repo `boxen new` clones (or the project's `origin` at the time);
after that a project can't widen its own access by changing its remotes: a
push to an unlisted repo fails with GitHub's usual auth error. A project can
list repos from several owners: boxen mints one token per owner.

```bash
boxen repos myproj                          # the list, each repo's state, and repos found unlisted
boxen repos myproj add you/fork other/lib   # owner/repo or https://github.com/owner/repo
boxen repos myproj rm other/lib
```

States: `ok` (the token covers it), `not your repo` (the app isn't installed
on that account: pulls work if it's public; to push, fork it and add the
fork), `no access` (the app is on that account but not on this repo: add it
to the app's installation on GitHub).

## Old conversations

In any session, send `/carry`. Claude lists the project's earlier
conversations, you pick some (`/carry all`, `/carry new`, `/carry 1 3`), and
it writes a handoff for each into the project's auto memory: goal, outcome,
decisions, open items, commands that worked. Long conversations are read in
full, in parts. Transcripts aren't changed, and boxen keeps them for 365
days (Claude Code's default is 30; a `cleanupPeriodDays` you've set is left
alone). For an exact replay instead, `claude --resume` in the project folder.

## Moving from v1

boxen v1 ran each project's Claude inside its container. To move:

```bash
cd ~/boxen && git pull && ./install.sh   # installs v2 beside v1 (the system Docker daemon stays on)
boxen migrate                            # stops v1, brings everything across
boxen doctor
boxen migrate --cleanup                  # later, once you're happy: deletes v1's leftovers
```

`migrate` copies v1's shared Claude config into `~/.claude` (login, settings
except permissions, every session and memory: v1 used the same paths, so
`claude --resume` in a project finds its sessions), turns each project's `env/Dockerfile` and
`services.yaml` into `~/src/<project>/.boxen/` (showing the diff: `FROM` now
names `boxen-env`, and `USER` lines go), copies sidecar volumes to rootless
Docker, and builds each project's container. Repo lists and tokens stay as
they are. Not copied: tool installs in v1's per-project home volumes (put
what a project needs in its Dockerfile) and v1's `compose.override.yaml`
(kept in `~/.config/boxen/projects/<project>/v1/`; v2's service is `env`).
The supervisor project is gone; its files stay until `--cleanup`, which also
turns the system Docker daemon off and takes you out of the `docker` group.

To bring Claude Code work over from a Mac, see `tools/mac-to-boxen --help`.

## Security

What boxen promises, and what it doesn't:

- **Between projects:** an agent's shell can't write outside its project
  folder or reach Docker, systemd or tmux (Claude Code's sandbox, enforced
  by the OS with bubblewrap); its file tools ask before writing outside it,
  and bypass mode is off. `boxen run` and `boxen env`, the sandbox's only
  exceptions, act only on the project of the folder they're run in. This
  stops accidents. It's not a wall against a hostile agent: all agents run
  as your user, can read each other's folders and tokens, and could ask you
  to approve something. Run untrusted code on another VM.
- **Inside a project:** the sandbox keeps the shell from writing a repo's
  `.git/config` and hooks or the project's `.claude/` settings, but
  `boxen run` (the build container, which mounts the project read-write)
  can. A build that sets `core.hooksPath` (husky) or `core.fsmonitor` makes
  git run those programs unsandboxed, for Claude Code and for you; look
  before trusting a repo that does.
- **The VM:** nothing an agent builds or installs lands on the VM: build
  containers run under rootless Docker, where root is your own user, so even
  a container escape isn't root. boxen never uses the system Docker daemon,
  and `migrate --cleanup` takes you out of the `docker` group (which is root
  in all but name).
- **Settings boxen keeps:** managed settings in
  `/etc/claude-code/managed-settings.d/boxen.json`, which outrank every
  settings file (yours, a project's, a migrated v1 config): sandbox on and
  required (`failIfUnavailable`), no unsandboxed retries, `boxen run *` and
  `boxen env *` excluded and allowed, network to GitHub only, nothing of
  `$XDG_RUNTIME_DIR` (rootless Docker, systemd, D-Bus), the tmux sockets or
  the system Docker socket, no `~/.ssh`, gh's login, the GitHub App's key or
  `GITHUB_TOKEN`/`GH_TOKEN` in a sandboxed command, and no
  `bypassPermissions` mode. In `~/.claude/settings.json` boxen only sets
  defaults you haven't (Remote Control for every session, a year of
  transcripts). `boxen doctor` warns about allow rules there broad enough
  to let the file tools edit outside a project without asking.
- **Sidecars and build containers** get the project folder (build container
  only), named volumes of their project, and its own network: no host paths,
  published ports, privileges, capabilities or devices unless you add them
  in your override. A symlinked `.boxen/` file is refused.
- **GitHub:** tokens last an hour and cover only the repos you listed.
- **Network:** build containers and sidecars reach anything the VM can,
  including your LAN. Give the VM its own VLAN and block RFC1918 except what
  you need; `boxen doctor --lan <host:port>` checks it from a build container.
- Cloned repos can carry hooks and settings (`.claude/settings.json`,
  `.mcp.json`); only trust repos you'd run yourself.

## Troubleshooting

Start with `boxen doctor`: it checks rootless Docker (and that files a
container writes are yours), the sandbox's packages and AppArmor profile,
boxen's Claude settings, git and token setup, and prints the one check that
needs a real session.

- **An agent says `boxen run` "ran inside Claude Code's sandbox":** it put
  something around the command (`cd x &&`, a pipe, a redirect). `boxen run`
  has to be the whole command line; the message and boxen's instructions
  say so.
- **A session doesn't show in the app:** Remote Control needs a claude.ai
  login (`/login` in `claude`). `claude` → `/config` → "Enable Remote Control
  for all sessions".
- **Claude won't start ("sandbox unavailable"):** `boxen doctor`; usually
  bubblewrap's AppArmor profile (re-run `install.sh`).
- **Clone, push or `gh` fails with an auth error:** `boxen repos <name>`
  shows whether the repo is listed and its state. `~/.config/boxen/tokens.log`
  has the timer's failures. The VM's clock must be right.
- **Limits (`mem_limit`, `cpus`) are ignored:** cgroup delegation applies
  after a reboot; `boxen doctor` shows the driver.

## Network egress (VLAN allowlist)

Claude Code: `api.anthropic.com`, `claude.ai`, `claude.com`,
`platform.claude.com`, `downloads.claude.ai`. Optional:
`mcp-proxy.anthropic.com` (claude.ai connectors), `registry.npmjs.org`
(plugins, npx MCP servers), `code.claude.com` (doc lookups). See
[network configuration](https://code.claude.com/docs/en/network-config).

GitHub: `github.com`, `api.github.com`, `codeload.github.com`,
`objects.githubusercontent.com`.

Install and image builds: `download.docker.com`, `archive.ubuntu.com`,
`security.ubuntu.com` (or `ports.ubuntu.com` on arm64), `cli.github.com`,
Docker Hub (`registry-1.docker.io`, `auth.docker.io`,
`production.cloudflare.docker.com`), and for `boxen ui`'s Python packages
`pypi.org` and `files.pythonhosted.org`. Plus whatever package registries
your projects' builds use.

Don't set `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`: it disables Remote
Control.
