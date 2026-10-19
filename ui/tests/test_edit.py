"""boxen_ui.edit: the private copy, and writing it back without following
links or clobbering a change made meanwhile."""
import json
import os
import stat
import subprocess
import sys

import pytest
from conftest import UI_DIR

from boxen_ui import edit, files


@pytest.fixture
def proj(tmp_path):
    """A project folder (~/src/a) with its .boxen/, which its agent writes."""
    p = tmp_path / "src" / "a"
    (p / ".boxen").mkdir(parents=True)
    (p / ".boxen" / "services.yaml").write_text("services: {}\n")
    pdir = tmp_path / "projects" / "a"
    pdir.mkdir(parents=True)
    secret = tmp_path / "app.pem"
    secret.write_text("SECRET KEY")
    return {"src": str(p), "env": p / ".boxen", "pdir": str(pdir), "secret": secret, "tmp": tmp_path,
            "rt": {"XDG_RUNTIME_DIR": str(tmp_path / "nonexistent")}}


def spec_for(proj, name="services.yaml"):
    return edit.prepare("a", ".boxen/" + name, proj["src"], [".boxen", name], ["vim"], env=proj["rt"])


def test_prepare_copies_privately(proj):
    s = spec_for(proj)
    assert open(s["copy"]).read() == "services: {}\n"
    assert s["target"] == str(proj["env"] / "services.yaml")
    assert stat.S_IMODE(os.stat(s["dir"]).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(s["copy"]).st_mode) == 0o600
    assert not s["copy"].startswith(proj["src"])
    assert edit.editor_cmd(["vim"], s["copy"]) == ["vim", "--cmd", "set nomodeline", s["copy"]]
    assert edit.editor_cmd(["nano", "-w"], "f") == ["nano", "-w", "f"]


def test_prepare_refuses_symlinks(proj):
    os.unlink(proj["env"] / "services.yaml")
    os.symlink(proj["secret"], proj["env"] / "services.yaml")
    with pytest.raises(files.Refused):
        spec_for(proj)


def test_prepare_refuses_a_linked_boxen_folder(proj):
    real = proj["tmp"] / "elsewhere"
    os.rename(proj["env"], real)
    os.symlink(real, proj["env"])
    with pytest.raises(files.Refused):
        spec_for(proj)
    assert files.state_under(proj["src"], [".boxen", "services.yaml"]) == ".boxen/ is a symlink or not a folder"


def test_unchanged_and_written(proj):
    s = spec_for(proj)
    assert edit.write_back(s, b"services: {}\n")["status"] == "unchanged"
    r = edit.write_back(s, b"services:\n  db: {image: postgres}\n")
    assert r["status"] == "written"
    assert (proj["env"] / "services.yaml").read_text().startswith("services:\n  db")
    assert not [f for f in os.listdir(proj["env"]) if f.startswith(".boxen-edit-")]


def test_conflict_when_changed_meanwhile(proj):
    s = spec_for(proj)
    (proj["env"] / "services.yaml").write_text("changed by the container\n")
    r = edit.write_back(s, b"mine\n")
    assert r["status"] == "conflict"
    assert (proj["env"] / "services.yaml").read_text() == "changed by the container\n"


def test_symlink_planted_meanwhile_is_not_followed(proj):
    s = spec_for(proj)
    os.unlink(proj["env"] / "services.yaml")
    os.symlink(proj["secret"], proj["env"] / "services.yaml")
    r = edit.write_back(s, b"mine\n")
    assert r["status"] == "refused"
    assert proj["secret"].read_text() == "SECRET KEY"


def test_boxen_folder_swapped_for_a_link(proj):
    s = spec_for(proj)
    os.rename(proj["env"], proj["tmp"] / "env-real")
    os.symlink(proj["tmp"], proj["env"])
    r = edit.write_back(s, b"mine\n")
    assert r["status"] == "refused"
    assert not (proj["tmp"] / "services.yaml").exists()


def test_new_file(proj):
    target = os.path.join(proj["pdir"], "repos")
    s = edit.prepare("a", "repos", proj["pdir"], ["repos"], ["vim"], env=proj["rt"])
    assert s["orig_sha"] is None and open(s["copy"]).read() == ""
    assert edit.write_back(s, b"")["status"] == "unchanged"
    assert edit.write_back(s, b"you/repo\n")["status"] == "written"
    assert open(target).read() == "you/repo\n"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o644


def test_new_file_from_a_template(proj):
    tpl = b"# example\n# services:\n"
    target = os.path.join(proj["pdir"], "compose.override.yaml")
    s = edit.prepare("a", "compose.override.yaml", proj["pdir"], ["compose.override.yaml"], ["vim"],
                     env=proj["rt"], template=tpl)
    assert open(s["copy"], "rb").read() == tpl and s["orig_sha"] is None
    assert edit.write_back(s, tpl)["status"] == "unchanged"         # quitting without a change writes nothing
    assert not os.path.exists(target)
    assert edit.write_back(s, b"services:\n")["status"] == "written"
    assert open(target).read() == "services:\n"


def test_missing_boxen_folder_is_refused_not_created(proj):
    s = spec_for(proj)
    import shutil
    shutil.rmtree(proj["env"])
    r = edit.write_back(s, b"mine\n")
    assert r["status"] == "refused" and not proj["env"].exists()


def test_main_runs_editor_and_reports(proj, tmp_path):
    ed = tmp_path / "ed"
    ed.write_text("#!/bin/sh\n# args: --cmd 'set nomodeline' FILE\necho \"$@\" > %s/args\necho '# edited' >> \"$3\"\n"
                  % tmp_path)
    ed.chmod(0o755)
    s = spec_for(proj)
    s["editor"] = [str(tmp_path / "vim")]
    os.symlink(ed, tmp_path / "vim")
    with open(os.path.join(s["dir"], "spec.json"), "w") as f:
        json.dump(s, f)
    out = subprocess.run([sys.executable, "-P", "-m", "boxen_ui.edit", os.path.join(s["dir"], "spec.json")],
                         env=dict(os.environ, PYTHONPATH=UI_DIR), capture_output=True, text=True, cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr
    assert (tmp_path / "args").read_text().startswith("--cmd set nomodeline ")
    assert (proj["env"] / "services.yaml").read_text() == "services: {}\n# edited\n"
    assert json.load(open(s["result"]))["status"] == "written"


ATTACKER = r"""
import os, sys, time
d, secret, stop = sys.argv[1], sys.argv[2], sys.argv[3]
while not os.path.exists(stop):
    try:
        names = os.listdir(d)
    except OSError:
        continue
    for n in names:
        if n.startswith(".boxen-edit-") and not n.endswith(".moved"):
            try:
                os.rename(os.path.join(d, n), os.path.join(d, n + ".moved"))
                os.symlink(secret, os.path.join(d, n))
            except OSError:
                pass
"""


def test_write_back_race_in_container_writable_folder(tmp_path):
    """The reviewer's race: in .boxen/ (which the agent writes) the temp
    file is swapped for a link to a secret between the write and the rename. 3000 tries: the secret's mode and content never
    change, nothing is written through a link, and a write is only reported
    when the target really is the file written."""
    src = tmp_path / "src" / "p"
    src.mkdir(parents=True)
    pdir = tmp_path / "projects" / "p"
    pdir.mkdir(parents=True)
    secret = tmp_path / "secret.pem"
    secret.write_text("SECRET")
    secret.chmod(0o600)
    target = src / ".boxen" / "Dockerfile"
    target.parent.mkdir()
    stop = tmp_path / "stop"
    atk = subprocess.Popen([sys.executable, "-c", ATTACKER, str(target.parent), str(secret), str(stop)])
    counts = {}
    bad = 0
    try:
        for i in range(3000):
            target.write_text("v%d\n" % i)
            if target.is_symlink():
                target.unlink(missing_ok=True)
                target.write_text("v%d\n" % i)
            s = edit.prepare("p", ".boxen/Dockerfile", str(src), [".boxen", "Dockerfile"], ["vim"],
                             env={"XDG_RUNTIME_DIR": str(tmp_path / "none")})
            r = edit.write_back(s, b"edited %d\n" % i)
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            if r["status"] == "written":
                if target.is_symlink() or target.read_text() != "edited %d\n" % i:
                    bad += 1
            if target.is_symlink():
                target.unlink(missing_ok=True)
            for x in target.parent.iterdir():
                if x.name.startswith(".boxen-edit-"):
                    x.unlink(missing_ok=True)
            import shutil as _sh
            _sh.rmtree(s["dir"], ignore_errors=True)
    finally:
        stop.write_text("")
        atk.wait(10)
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert secret.read_text() == "SECRET"
    assert bad == 0, counts
    assert counts.get("refused", 0) > 0, counts            # the attacker did get in sometimes
