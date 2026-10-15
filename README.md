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
2. Paste the Client ID, then paste the `.pem` file's contents. The key isn't
   shown as you paste (so it doesn't sit in your scrollback); a `*` appears
   for each line received. If you've copied the file to the VM instead, run
   `boxen github --key /path/to/file.pem`.
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

The clone runs inside the container with a token boxen mints for that repo
(see "Connect GitHub"), which is what makes private repos work. Containers
have no SSH keys, so GitHub remotes in SSH form (`git@github.com:...`) are
transparently used over HTTPS; either URL form works.

What `boxen new` does, in order:

1. Builds the project image.
2. Clones the repo, if you gave a URL.
3. Signs you in to Claude (first project only; every project shares the login).
   Open the URL it prints on any device, sign in, then paste the code back
   and press Enter. Nothing appears on screen while you paste; that's normal.
4. Starts the container with your terminal attached, so you can answer
   Claude's one-time prompts in the real server: **y** to "trust this folder"
   and (first time ever) **y** to "enable Remote Control". As soon as the
   server shows its session URL, boxen detaches on its own; the server keeps
   running. (If it ever doesn't detach, press Ctrl+P then Ctrl+Q. Don't press
   Ctrl+C, which stops the server.)
5. Checks the server stays up. The project shows up in the Claude app's Code
   tab as `<name>`.

If anything fails or you interrupt it, `new` cleans up after itself so you
can simply run it again.

## Your Claude config

Everything in the shared `~/.claude` applies to every project.

- **Bring it from a Mac:** see "Moving from a Mac" below (it can bring just
  the config with `--only config`).
- **Bring it from a folder:** `boxen import <dir>` copies `CLAUDE.md`,
  `rules/`, `skills/`, `agents/` and `commands/` from a copy of a `~/.claude`
  folder you've put on the VM.
- **Edit it later:** `boxen config` opens a shell in the shared `~/.claude`.
- **Phone notifications** when a task finishes or Claude needs a decision:
  run `boxen claude <name>`, then `/config`, and turn on **Push when Claude
  decides** and/or **Push when actions required**. It's stored in the shared
  config, so once covers every project. The Claude app on your phone must be
  signed in to the same account.

## Moving from a Mac

Everything you had in Claude Code on a Mac can come over in one go: each
project's code, Claude's auto memory for it, its session transcripts, and
your personal config. The Mac pushes over the SSH you already use to reach
the VM, so the VM never needs a connection to your LAN. Nothing on the Mac is
changed or deleted.

Set up boxen and run `boxen github` first, so projects get GitHub access as
they arrive. Then `boxen import-mac` on the VM prints these commands with your
details filled in. On the Mac:

```bash
scp you@vm:boxen/mac/boxen-export ~/boxen-export
bash ~/boxen-export you@vm                          # everything
bash ~/boxen-export you@vm --only memory,config     # only some kinds of data
bash ~/boxen-export you@vm --projects alpha,gamma   # only some projects
```

What happens:

1. It finds every folder Claude Code has been used in (from the Mac's
   `~/.claude.json`) and shows a **plan**: one project per line, named after
   its folder. Your home folder, folders that no longer exist, and plain
   folders that just contain several projects are left out. Folders inside a
   project (subfolders, worktrees) come along with that project.
2. You approve it, pick which projects this run covers (`[s]elect`, by number,
   range like `5-7`, or name; or `--projects` up front), or edit the plan on
   the VM (rename projects, drop ones for good, add skipped ones back). A
   selection is just for that run: the plan keeps every project and name, so
   you can move a few now and the rest later.
3. It rsyncs the code to `~/src/<name>`, including `.git` (unpushed commits,
   stashes) and untracked files like `.env`, but skipping `node_modules`,
   Python virtualenvs and caches; add more with `--exclude PATTERN`. Things
   built on the Mac won't run in Linux containers anyway, so reinstall
   dependencies on the VM.
4. It puts Claude's data where the VM's Claude will look for it. Claude Code
   files a folder's memory and sessions under its path, so they're re-filed
   under each project's new path (`/Users/you/code/foo` becomes
   `/home/you/src/foo`). Nothing already on the VM is overwritten.
5. It creates a boxen project for each one and offers to start them (one `y`
   per project for the trust prompt).

Scopes for `--only` (comma-separated):

| Scope | What comes over |
|---|---|
| `code` | Project folders, and a boxen project for each |
| `memory` | Claude's auto memory per project |
| `sessions` | Session transcripts per project (includes memory) |
| `config` | `CLAUDE.md`, rules, skills, agents, commands and output styles from `~/.claude`; `settings.json` saved as `settings.mac.json` for reference (not active: hooks and paths in it are often Mac-specific); a list of your MCP servers |

Good to know:

- **Re-running syncs again**, and offers to reuse your previous plan so code
  lands in the same projects. Without `code`, memory and sessions are filed
  under the plan's names, so a later `boxen new <same name> <url>` picks them up.
- **Sessions:** Claude Code keeps transcripts for 30 days by default (longer
  for sessions started in the Desktop app), so older ones aren't there to
  move. Continue an imported one with `boxen resume <name>` (see "Sessions
  across restarts"). Their history still mentions Mac paths, and rewind checkpoints
  don't carry over. Stop working in a session on the Mac before exporting
  it; the export is a snapshot.
- **MCP servers** are listed (full settings in `~/.config/boxen/mac-mcp.json`)
  rather than installed, since they often run Mac programs. Re-add the ones
  you need. claude.ai connectors follow your account and need nothing.
- **Plugins** synced to your claude.ai account download by themselves; others
  you reinstall with `/plugin`.
- Requires `rsync` on the Mac (built in) and SSH from the Mac to the VM.

## Sessions across restarts

When a project restarts (`restart`, `rebuild`, `update`, a VM reboot), its
Remote Control server brings its sessions back in the Claude app by itself,
as long as it's back within about 4 hours. Restarts take seconds, so in
practice that's every time. The exception is a project left down longer,
such as the VM off overnight or a project stopped for the weekend. For those,
and for any other earlier session, use `boxen resume`.

### Picking up an earlier session

```bash
boxen resume myproj
```

```
Sessions in myproj, newest first:

   1) 25m ago  "add retry to the webhook handler"   may be open in the app already
   2) 3h ago   "fix the login redirect bug"
   3) 6d ago   "add CSV export"
Number to continue in the Claude app (Enter to cancel): 3
```

Pick one and it's in the Claude app with its history, with Remote Control
already on; there's nothing to attach to. That covers sessions imported from a
Mac, sessions from before a long downtime, and ones you started in a terminal
with `boxen claude <name>`. The list shows the first thing you typed in each.

- `boxen resume myproj 3` skips the list.
- A session marked "may be open in the app already" was used since the
  project started, so continue it in the app. boxen won't open it a second
  time (two copies would mix their messages) unless you add `--force`.
- A resumed session stays open until you close it with
  `boxen resume myproj 3 --stop` or the container restarts; after a restart,
  resume it again. `boxen ls` shows how many are open per project.
- `boxen resume myproj 3 --attach` shows it in your terminal (Ctrl+B then D
  to leave it running). Useful if one seems stuck, for example on a question
  Claude asks in the terminal when resuming a long session.

Saved sessions are kept for 30 days (Claude Code's default), so that's how
far back you can go.

## Commands

`up`, `down`, `restart`, `rebuild`, `regen` and `tokens` accept `all` in
place of a project name, for example `boxen regen all`. They carry on past a
project that fails and list the failures at the end. (`rm` deliberately
doesn't, and no project can be named `all`.)

| Command | What it does |
|---|---|
| `boxen ls` | Projects, container status and restart counts |
| `boxen github [--reset]` | Connect GitHub (or redo it with a new app/key) |
| `boxen tokens [name...\|all]` | Refresh GitHub tokens now and show their repos and expiry |
| `boxen logs <name> [-f]` | Remote Control server output |
| `boxen shell <name>` | Bash inside the container |
| `boxen claude <name>` | Terminal Claude session inside the container |
| `boxen resume <name> [N]` | Pick an earlier session from a list and continue it in the Claude app (`--stop`, `--attach`) |
| `boxen up <name\|all>` | Start, or apply changes to `boxen.env` / `project.env` / `compose.yaml` |
| `boxen down <name\|all>` | Stop |
| `boxen restart <name\|all>` | Recreate the container (also picks up env changes) |
| `boxen trust <name>` | Re-run the one-time prompts |
| `boxen login` | Sign in again, then restart running projects |
| `boxen import <dir>` / `boxen config` | Manage the shared `~/.claude` |
| `boxen import-mac` | How to move code, memory, sessions and config from a Mac (run on the Mac) |
| `boxen rebuild <name\|all>` | Apply edits to that project's Dockerfile |
| `boxen regen <name\|all>` | Replace its `compose.yaml` with the current template (old one kept as `.bak`) |
| `boxen update` | Fresh base image (Ubuntu patches + newest stable Claude Code), rebuild all |
| `boxen self-update` | Update boxen itself (pull + re-run the installer) and say what projects need |
| `boxen rm <name>` | Remove container, image and config; keeps the code and its Claude history |

## Customizing

| To change | Edit | Then |
|---|---|---|
| Tools for every project | `~/.config/boxen/base.local.Dockerfile` | `boxen update` |
| Tools for one project | `~/.config/boxen/projects/<name>/Dockerfile` | `boxen rebuild <name>` |
| Env for every project | `~/.config/boxen/boxen.env` | `boxen restart all` |
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
existing projects only through `boxen regen <name>` (or `boxen regen all`).

## Lifecycle and updates

Containers run indefinitely but are disposable. What matters lives outside
them: code in `~/src`, Claude state in the `claude-config` volume, and
toolchains in the Dockerfiles. Anything installed by hand inside a container
is gone after a rebuild.

`restart: unless-stopped` brings containers back after VM reboots and after
the Remote Control server exits during a long network outage. Sessions come
back too (see "Sessions across restarts").

There are two kinds of update:

- **`boxen self-update`** updates boxen itself. It pulls this repo, shows
  what changed, and re-runs `install.sh` (prerequisites, PATH link, base
  image). Then it tells you if your projects need `boxen regen all` (the
  compose template changed) or `boxen rebuild all` (the base image changed).
  It doesn't run those for you, since both restart containers and `regen`
  replaces any edits to `compose.yaml` (keeping a `.bak`).
- **`boxen update`** updates what runs in the containers: a fresh Ubuntu base
  with current patches and the newest stable Claude Code, then a rebuild of
  every project. Claude Code comes from Anthropic's signed apt repo and
  doesn't auto-update inside containers, so this is how you upgrade it.

Doing both monthly is reasonable:

```bash
boxen self-update && boxen update
```

`update` rebuilds every project, so after it you don't also need
`rebuild all`. It keeps going if one project fails, lists the failures at the
end, and prunes old build cache.

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
