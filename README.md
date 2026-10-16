# boxen

One Docker container per project on an Ubuntu VM, each running a Claude Code
[Remote Control](https://code.claude.com/docs/en/remote-control) server that
you drive from the Claude app's Code tab. Every container shares one
`~/.claude` (login, settings, CLAUDE.md, skills, memory), so you sign in once.

Needs an Ubuntu 24.04 VM with sudo, and a Claude Pro, Max, Team or Enterprise
plan signed in with a claude.ai account (on Team/Enterprise an Owner must
enable Remote Control first).

## Install

```bash
sudo apt install -y gh git
gh auth login                                   # this repo is private
gh repo clone theycallmeboxy/boxen ~/boxen
~/boxen/install.sh
```

`install.sh` is safe to re-run: Docker CE, a few tools, the `docker` group,
`boxen` in `~/.local/bin`, the base image. Then log out and back in if
`boxen` isn't on your PATH yet, run `boxen github`, and create a project.

## Connect GitHub

```bash
boxen github
```

Containers never get a long-lived GitHub token. A private GitHub App that you
own mints a 1-hour token per project, scoped to that project's repo only; the
app's private key stays on the VM, cron refreshes the tokens, and git and `gh`
inside the container pick them up. The walkthrough takes about 3 minutes:

1. It prints a link that pre-fills a new GitHub App (private, no webhook,
   permissions: Contents, Pull requests, Issues write; Actions, Checks,
   Statuses read). Click **Create GitHub App**, copy the **Client ID**, and
   click **Generate a private key** (a `.pem` downloads).
2. Paste the Client ID, then the `.pem` file's contents (not echoed; a `*`
   per line). If the file is on the VM: `boxen github --key /path/to/file.pem`.
3. It prints the app's install link. Choose **All repositories** so repos you
   create later work too.

The app only installs on accounts you control, so a project cloned from
someone else's repo gets no token: pulls work if it's public, pushes don't.
To push, fork it and point `origin` at your fork. It also sets the commit
identity for containers to your GitHub name and no-reply address
(`ID+username@users.noreply.github.com`), unless `GIT_AUTHOR_EMAIL` is
already in `boxen.env`; running projects are restarted to pick it up.

## Create a project

```bash
boxen new <name> [git-url]
```

`<name>` becomes the folder `~/src/<name>`, the container `boxen-<name>` and
the name in the Claude app: lowercase letters and digits, single `-` or `_`
between them, up to 40 characters.

| You have | Run | Result |
|---|---|---|
| A repo on GitHub | `boxen new myproj https://github.com/you/myproj.git` | Cloned into `~/src/myproj` |
| Nothing yet | `boxen new myproj` | Empty `~/src/myproj`; `git init` and add a remote later |
| Code already in `~/src/myproj` | `boxen new myproj` | Uses the folder as-is |

You'll be asked to sign in to Claude (first project only: open the URL on any
device, paste the code back), then, with your terminal attached to the new
server, **y** to trust the folder and (first time ever) **y** to enable Remote
Control. boxen detaches on its own once the server is up, and the project
appears in the app's Code tab.

## Day to day

- `boxen ls` — every project: container, server state, GitHub token.
- `boxen up <name|all>` — start; also applies env/compose changes, and if the
  server is waiting on a question, lets you answer it.
- `boxen down <name|all>` — stop.
- `boxen restart <name|all>` — recreate the container (picks up env changes).
- `boxen logs <name> [-f]` — the server's output, as plain text.
- `boxen shell <name>` — bash inside the container.
- `boxen claude <name> [args]` — a terminal Claude session inside it.
- `boxen rm <name>` — remove container, image and config; keeps `~/src/<name>`
  and its Claude history.

`boxen update` rebuilds the base image from a fresh Ubuntu with the newest
stable Claude Code and then every project; `boxen update <name>` rebuilds one
project. `boxen self-update` pulls the latest boxen, re-runs its installer and
applies template changes to every project; servers restart, app sessions
reconnect on their own. Running both monthly is reasonable.

## Old conversations

In any project session in the Claude app, send `/carry`. Claude lists that
project's earlier conversations (date, length, title, whether already
carried), you pick some (`/carry all`, `/carry new` for the ones not yet
carried, `/carry 1 3`), and it reads each one and writes a handoff into the
project's auto memory: goal, what was done, decisions and why, open items,
gotchas, key files. Auto memory is on by default here, so every later session
in that project loads it without being asked, and `/carry` ends with where
things stand. Transcripts aren't changed, and boxen keeps them for 365 days
(Claude Code's default is 30; a `cleanupPeriodDays` you've set is left alone).

For an exact replay instead, `boxen resume <name>` on the VM lists the same
sessions; pick one (or pass its number or ID) and it reopens in the Claude app
with its full history, under "Remote Control" rather than the project, as
`<name> · <title>`. `--force` opens one that's already open elsewhere,
`--attach` shows it in your terminal (Ctrl+B then D to leave it running),
`--stop` closes one you resumed. A restarted server brings its recent sessions
back by itself; `resume` is for older ones.

## Moving from a Mac

`boxen import-mac` prints these with your details filled in. On the Mac:

```bash
scp you@vm:boxen/mac/boxen-export ~/boxen-export
bash ~/boxen-export you@vm                          # everything
bash ~/boxen-export you@vm --only memory,config     # some scopes
bash ~/boxen-export you@vm --projects alpha,gamma   # some projects
```

Scopes: `code` (project folders, and a boxen project for each), `memory`
(auto memory per project), `sessions` (transcripts; includes memory) and
`config` (`CLAUDE.md`, rules, skills, agents, commands, output styles;
`settings.json` saved as `settings.mac.json` for reference; your MCP list).

- The Mac pushes over SSH (rsync); nothing on it is changed, and nothing
  already on the VM is overwritten. Run `boxen github` first.
- It shows a plan (one project per folder Claude Code was used in) to edit or
  narrow before anything is copied; re-running reuses it and syncs again.
- `node_modules`, virtualenvs and caches are skipped (`--exclude` for more).
- New projects are started as they arrive (trust prompt included). Imported
  sessions are continued with `boxen resume`.
- MCP servers are listed in `~/.config/boxen/mac-mcp.json`, not installed.

## Customizing

| To change | Edit | Then |
|---|---|---|
| Tools for every project | `~/.config/boxen/base.local.Dockerfile` (starts with `FROM boxen-base:core`) | `boxen update` |
| Tools for one project | `~/.config/boxen/projects/<name>/Dockerfile` | `boxen update <name>` |
| Env for every project | `~/.config/boxen/boxen.env` | `boxen restart all` |
| Env for one project | `~/.config/boxen/projects/<name>/project.env` | `boxen restart <name>` |
| Container settings (limits, `--spawn=worktree`) | `~/.config/boxen/projects/<name>/compose.override.yaml` (see the `.example`) | `boxen up <name>` |

Containers have no sudo, so Dockerfiles install as `root` and switch back to
your user. Don't edit files in this repo or a project's `compose.yaml`
(`self-update` rewrites it); `compose.override.yaml` is never touched.

## Security

- Containers run as your UID with no sudo and `no-new-privileges`; no Docker
  socket.
- The shared `claude-config` volume isn't isolated between projects: any
  container can read the login and every transcript. Untrusted code belongs
  on another VM.
- A project's GitHub token is visible to everything in its container, hence
  one repo and one hour. The app's private key never enters a container.
- Containers reach anything the VM can, including your LAN: give the VM its
  own VLAN and block RFC1918 except what you need.
- Cloned repos can carry hooks and settings (`.claude/settings.json`,
  `.mcp.json`); only trust repos you'd run yourself.

## Troubleshooting

- **A project doesn't show in the app:** `boxen ls`. The SERVER column says
  what to run: "waiting for an answer" or "not trusted" → `boxen up <name>`;
  "not signed in" → `boxen login`. A climbing restart count means the server
  keeps exiting; `boxen logs <name>` says why.
- **Sign-in expired or wrong account:** `boxen login`.
- **Env change didn't take effect:** `boxen restart <name>` (a plain Docker
  restart doesn't re-read env files).
- **Clone, push or `gh` fails with an auth error:** `boxen ls` shows the token
  state. Usually the app isn't installed on that repo, or lacks a permission
  (e.g. Workflows for `.github/workflows/`); `~/.config/boxen/tokens.log` has
  cron-time failures. The VM's clock must be right.

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
