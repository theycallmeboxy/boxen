"""A stand-in for bin/boxen in boxen ui's tests.

Records each call (argv after "boxen", and the folder it ran in) as a JSON
line in $FAKE_DIR/boxen.jsonl, then answers from $FAKE_DIR/boxen.json:

    {"rules": [{"args": ["up", "caulker"], "out": ["line", ...], "delay": 0.1, "rc": 3}, ...]}

The first rule whose "args" equals the call's arguments (or, with
"prefix": true, starts them) wins; a rule with "once" is used up. Without
a rule: `ls --json` prints $FAKE_DIR/ls.json (exit 1 with a message if
$FAKE_DIR/ls.fail exists); `df --json` prints $FAKE_DIR/df.json (exit 1
if $FAKE_DIR/df.fail exists; with neither, a report with no projects);
`repos NAME --suggest` prints nothing; anything else prints one line and
exits 0.
"""
import json
import os
import sys
import time

d = os.environ["FAKE_DIR"]
args = sys.argv[1:]
with open(os.path.join(d, "boxen.jsonl"), "a") as f:
    f.write(json.dumps({"args": args, "cwd": os.getcwd(), "pid": os.getpid(),
                        "docker_host": os.environ.get("DOCKER_HOST")}) + "\n")

rules_path = os.path.join(d, "boxen.json")
rules = []
if os.path.exists(rules_path):
    with open(rules_path) as f:
        rules = json.load(f).get("rules", [])
for i, r in enumerate(rules):
    want = r["args"]
    if args == want or (r.get("prefix") and args[:len(want)] == want):
        if r.get("once"):
            if r.get("used"):
                continue
            r["used"] = True
            with open(rules_path, "w") as f:
                json.dump({"rules": rules}, f)
        for line in r.get("out", []):
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
            if r.get("delay"):
                time.sleep(r["delay"])
        if r.get("sleep"):
            time.sleep(r["sleep"])
        sys.exit(r.get("rc", 0))

if args == ["ls", "--json"]:
    if os.path.exists(os.path.join(d, "ls.fail")):
        sys.stderr.write("boxen: something broke in ls\n")
        sys.exit(1)
    with open(os.path.join(d, "ls.json")) as f:
        sys.stdout.write(f.read())
    sys.exit(0)

if args == ["df", "--json"]:
    if os.path.exists(os.path.join(d, "df.fail")):
        sys.stderr.write("boxen: something broke in df\n")
        sys.exit(1)
    p = os.path.join(d, "df.json")
    sys.stdout.write(open(p).read() if os.path.exists(p) else
                     json.dumps({"version": 1, "docker": True, "projects": []}))
    sys.exit(0)

if args[:1] == ["repos"] and args[-1:] == ["--suggest"]:
    sys.exit(0)

print("fake boxen: %s" % " ".join(args))
