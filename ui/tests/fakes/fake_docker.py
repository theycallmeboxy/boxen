"""A stand-in for the docker CLI in boxen ui's tests: `exec boxen-<p>
boxen-carry list` prints $FAKE_DIR/carry-<p>.tsv; `logs ... <container>`
prints $FAKE_DIR/dlogs-<container>.txt. Calls go to $FAKE_DIR/docker.jsonl."""
import json
import os
import sys

d = os.environ["FAKE_DIR"]
args = sys.argv[1:]
with open(os.path.join(d, "docker.jsonl"), "a") as f:
    f.write(json.dumps({"args": args}) + "\n")


def cat(name):
    p = os.path.join(d, name)
    if not os.path.exists(p):
        sys.stderr.write("Error: No such container\n")
        sys.exit(1)
    with open(p) as f:
        sys.stdout.write(f.read())
    sys.exit(0)


if args[:1] == ["exec"] and args[2:] == ["boxen-carry", "list"]:
    cat("carry-%s.tsv" % args[1][len("boxen-"):])
if args[:1] == ["logs"]:
    cat("dlogs-%s.txt" % args[-1])
sys.stderr.write("fake docker: unhandled %r\n" % args)
sys.exit(1)
