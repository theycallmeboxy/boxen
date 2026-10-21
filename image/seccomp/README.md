# Docker's default seccomp profile

`docker-default.json` is Docker's own default seccomp profile, unchanged:
`vendor/github.com/moby/profiles/seccomp/default.json` at moby tag
`docker-v29.9.1` (github.com/moby/profiles/seccomp v0.2.4, Apache License 2.0,
see LICENSE).

A project with non-root runs on (`boxen nonroot <name> on`) runs its build
container with this profile plus one rule, added by boxen when it writes
`~/.config/boxen/seccomp-userns.json`: `unshare` is allowed when its flags are
exactly `CLONE_NEWUSER` (a user namespace and nothing else; mount, network
and other namespaces stay blocked). That's what `boxen run --user` needs.
A custom profile replaces Docker's default rather than adding to it, which
is why the whole default is here. Refresh it from a newer Docker release
when Docker's changes; everything else in it should stay as Docker ships it.
