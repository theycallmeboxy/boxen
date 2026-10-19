"""A stand-in for the docker CLI in boxen ui's tests. The dashboard never
runs it itself (it asks the Engine API, and boxen for the rest; a followed
log runs it in a tmux window, which is fake here too), so every call is
recorded in $FAKE_DIR/docker.jsonl for the tests to see there are none,
and fails."""
import json
import os
import sys

with open(os.path.join(os.environ["FAKE_DIR"], "docker.jsonl"), "a") as f:
    f.write(json.dumps({"args": sys.argv[1:]}) + "\n")
sys.stderr.write("fake docker: not expected to run\n")
sys.exit(1)
