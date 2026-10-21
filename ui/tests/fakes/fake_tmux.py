"""A stand-in for tmux in boxen ui's tests (and for `boxen ui`'s): records
argv in $FAKE_DIR/tmux.jsonl; keeps windows in $FAKE_DIR/windows.json
([{"id", "name", "dead", "done", "sessions"}]: sessions defaults to ["boxen"],
["serve"] is a hidden serve window, both is one linked into boxen; targets
like "=boxen:@3", "=serve:", "@3"); capture-pane prints $FAKE_DIR/capture.txt;
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


def sessions(w):
    return w.get("sessions") or ["boxen"]


def save():
    json.dump(wins, open(wpath, "w"))


def split_target(t):
    """"=boxen:@3" -> ("boxen", "@3"); "@3" -> (None, "@3"); "=boxen:" -> ("boxen", "")."""
    if t is None:
        return None, ""
    if ":" in t:
        sess, _, win = t.partition(":")
        return sess.lstrip("="), win
    return None, t


def find(sess, win):
    for w in wins:
        if (sess is None or sess in sessions(w)) and (w["id"] == win or win == "=" + w["name"]):
            return w
    return None


if cmd == "list-windows":
    sess, _ = split_target(opt("-t"))
    if sess not in ("boxen", "serve"):
        sys.stderr.write("can't find session: %s\n" % sess)
        sys.exit(1)
    mine = [w for w in wins if sess in sessions(w)]
    if not mine and sess != "boxen":
        sys.stderr.write("can't find session: %s\n" % sess)
        sys.exit(1)
    for i, w in enumerate(mine):
        if opt("-F") == "#{window_name}":
            print(w["name"])
        else:
            print("%s\t%d\t%d\t%d\t%d\t%s\t%s" % (w["id"], i, attached if sess == "boxen" else 0,
                                                1 if i == 0 else 0, 1 if w.get("dead") else 0,
                                                "1" if w.get("done") else "", w["name"]))
elif cmd == "kill-window":
    wins = [w for w in wins if w["id"] != split_target(opt("-t"))[1]]
    save()
elif cmd == "display-message":
    print("/dev/pts/7")
elif cmd == "new-window":
    n = max([int(w["id"][1:]) for w in wins] + [0]) + 1
    sess, _ = split_target(opt("-t"))
    wins.append({"id": "@%d" % n, "name": opt("-n"), "sessions": [sess or "boxen"]})
    save()
elif cmd == "select-window":
    sess, win = split_target(opt("-t"))
    sys.exit(0 if find(sess, win) is not None else 1)
elif cmd == "link-window":
    src = find(*split_target(opt("-s")))
    dst, _ = split_target(opt("-t"))
    if src is None:
        sys.stderr.write("can't find window: %s\n" % opt("-s"))
        sys.exit(1)
    if dst not in sessions(src):
        src["sessions"] = sessions(src) + [dst]
    save()
elif cmd == "unlink-window":
    sess, win = split_target(opt("-t"))
    w = find(sess, win)
    if w is None:
        sys.stderr.write("can't find window: %s\n" % win)
        sys.exit(1)
    if len(sessions(w)) < 2:
        sys.stderr.write("window only linked to one session\n")
        sys.exit(1)
    w["sessions"] = [x for x in sessions(w) if x != sess]
    save()
elif cmd == "capture-pane":
    p = os.path.join(d, "capture.txt")
    if os.path.exists(p):
        sys.stdout.write(open(p).read())
elif cmd == "has-session":
    sys.exit(0 if os.path.exists(os.path.join(d, "has-session")) else 1)
sys.exit(0)
