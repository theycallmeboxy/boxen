"""A stand-in for tmux in boxen ui's tests (and for `boxen ui`'s): records
argv in $FAKE_DIR/tmux.jsonl; keeps windows in $FAKE_DIR/windows.json
([{"id", "name", "dead", "done"}]); capture-pane prints $FAKE_DIR/capture.txt;
no client is attached while $FAKE_DIR/detached exists; `-V` says 3.4
(or $FAKE_DIR/version)."""
import json
import os
import sys

d = os.environ["FAKE_DIR"]
argv = sys.argv[1:]
if argv == ["-V"]:
    v = os.path.join(d, "version")
    print(open(v).read().strip() if os.path.exists(v) else "tmux 3.4")
    sys.exit(0)
with open(os.path.join(d, "tmux.jsonl"), "a") as f:
    f.write(json.dumps({"argv": argv}) + "\n")

wpath = os.path.join(d, "windows.json")
wins = json.load(open(wpath)) if os.path.exists(wpath) else []
args = argv[2:] if argv[:1] == ["-L"] else argv
cmd = args[0] if args else ""


def opt(name):
    return args[args.index(name) + 1] if name in args else None


attached = 0 if os.path.exists(os.path.join(d, "detached")) else 1
if cmd == "list-windows":
    for i, w in enumerate(wins):
        if opt("-F") == "#{window_name}":
            print(w["name"])
        else:
            print("%s\t%d\t%d\t%d\t%d\t%s\t%s" % (w["id"], i, attached, 1 if i == 0 else 0,
                                                1 if w.get("dead") else 0, "1" if w.get("done") else "", w["name"]))
elif cmd == "kill-window":
    wins = [w for w in wins if w["id"] != opt("-t")]
    json.dump(wins, open(wpath, "w"))
elif cmd == "display-message":
    print("/dev/pts/7")
elif cmd == "new-window":
    n = max([int(w["id"][1:]) for w in wins] + [0]) + 1
    wins.append({"id": "@%d" % n, "name": opt("-n")})
    json.dump(wins, open(wpath, "w"))
elif cmd == "select-window":
    t = opt("-t")
    sys.exit(0 if any(w["id"] == t or t in ("boxen:" + w["name"], "boxen:=" + w["name"]) for w in wins) else 1)
elif cmd == "capture-pane":
    p = os.path.join(d, "capture.txt")
    if os.path.exists(p):
        sys.stdout.write(open(p).read())
elif cmd == "has-session":
    sys.exit(0 if os.path.exists(os.path.join(d, "has-session")) else 1)
sys.exit(0)
