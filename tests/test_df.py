"""`boxen df`: disk use per project, shared, leftovers, free space."""
import json
import re


def img(repo, tag, iid, size, unique, containers="0"):
    return {"Repository": repo, "Tag": tag, "ID": "sha256:" + iid * 8, "Size": size, "UniqueSize": unique,
            "SharedSize": "0B", "Containers": containers}


def ctr(name, image, size):
    return {"Names": name, "Image": image, "Size": size, "State": "running"}


def vol(name, size):
    return {"Name": name, "Size": size, "Links": "1"}


SYSTEM_DF = {
    "Images": [
        img("boxen-env", "latest", "a", "1.5GB", "200MB"),
        img("boxen-env", "core", "b", "1.3GB", "10MB"),
        img("boxen-env-alpha", "latest", "c", "2GB", "500MB", "1"),
        img("postgres", "16", "d", "430MB", "430MB", "1"),
        img("boxen-env-gone", "latest", "e", "1.6GB", "100MB"),
        img("<none>", "<none>", "f", "1.4GB", "300MB"),
        img("ubuntu", "24.04", "g", "78MB", "0B"),
        img("nginx", "latest", "h", "190MB", "190MB"),
    ],
    "Containers": [
        ctr("boxen.alpha.env", "boxen-env-alpha:latest", "12MB"),
        ctr("boxen.alpha.db", "docker.io/library/postgres:16", "1kB"),
        ctr("boxen.gone.env", "boxen-env-gone", "3MB"),
        ctr("web", "nginx", "2kB"),
    ],
    "Volumes": [
        vol("boxen.alpha.cache", "800MB"),
        vol("boxen.alpha.pgdata", "50MB"),
        vol("boxen.gone.cache", "1.2GB"),
        vol("unrelated", "N/A"),
    ],
    "BuildCache": [
        {"ID": "x", "Size": "2GB", "Shared": "false", "InUse": "false"},
        {"ID": "y", "Size": "1GB", "Shared": "true", "InUse": "false"},
    ],
}


def setup_disk(vm):
    vm.set_docker(system_df=SYSTEM_DF)
    (vm.src / "alpha" / "build").mkdir(parents=True, exist_ok=True)
    (vm.src / "alpha" / "build" / "big").write_bytes(b"x" * 300_000)
    (vm.src / "alpha" / "small").mkdir(exist_ok=True)
    (vm.src / "alpha" / "small" / "f").write_bytes(b"x" * 5000)
    hist = vm.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(vm.src / "alpha"))
    hist.mkdir(parents=True)
    (hist / "s.jsonl").write_bytes(b"y" * 100_000)
    return hist


def test_df_json(vm):
    hist = setup_disk(vm)
    doc = json.loads(vm.ok("df", "--json").stdout)
    assert doc["version"] == 1 and doc["docker"] is True
    alpha, beta = doc["projects"]
    assert alpha["name"] == "alpha" and alpha["path"] == str(vm.src / "alpha")
    assert alpha["code"] >= 305_000 and alpha["claude"] >= 100_000 and alpha["claude_path"] == str(hist)
    assert alpha["image"] == {"name": "boxen-env-alpha:latest", "size": 500_000_000}
    assert alpha["containers"] == [{"name": "boxen.alpha.env", "size": 12_000_000},
                                   {"name": "boxen.alpha.db", "size": 1000}]
    assert sorted(alpha["volumes"], key=lambda v: v["name"]) == [
        {"name": "boxen.alpha.cache", "size": 800_000_000}, {"name": "boxen.alpha.pgdata", "size": 50_000_000}]
    assert alpha["total"] == (alpha["code"] + alpha["claude"] + 500_000_000 + 12_001_000 + 850_000_000)
    assert "top" not in alpha
    assert beta["image"] is None and beta["containers"] == [] and beta["volumes"] == []
    assert beta["claude"] is None
    sh = doc["shared"]
    assert sh["base"] == {"names": ["boxen-env:core", "boxen-env:latest"], "size": 1_510_000_000}
    assert sh["sidecar_images"] == [{"name": "postgres:16", "size": 430_000_000, "projects": ["alpha"]}]
    assert sh["build_cache"] == 2_000_000_000
    assert sh["old_images"] == {"count": 1, "size": 300_000_000}
    assert doc["other"] == {"images": {"count": 2, "size": 190_000_000},
                            "containers": {"count": 1, "size": 2000},
                            "volumes": {"count": 1, "size": 0}}
    assert sorted((x["kind"], x["name"], x["size"]) for x in doc["leftovers"]) == [
        ("container", "boxen.gone.env", 3_000_000), ("image", "boxen-env-gone:latest", 100_000_000),
        ("volume", "boxen.gone.cache", 1_200_000_000)]
    assert doc["filesystems"] and all({"mount", "size", "free", "for"} <= set(f) for f in doc["filesystems"])
    assert "projects" in sum((f["for"] for f in doc["filesystems"]), [])
    # read-only: nothing but the two reads
    assert [c[:2] for c in vm.docker_calls()] == [["system", "df"], ["info", "-f"]]


def test_df_total_exact(vm):
    setup_disk(vm)
    doc = json.loads(vm.ok("df", "--json").stdout)
    a, b = doc["projects"]
    leftovers = 3_000_000 + 100_000_000 + 1_200_000_000
    assert doc["total"] == a["total"] + b["total"] + 1_510_000_000 + 430_000_000 + 2_000_000_000 \
        + 300_000_000 + leftovers


def test_df_plain(vm):
    setup_disk(vm)
    out = vm.ok("df").stdout
    lines = out.splitlines()
    assert lines[0].split() == ["PROJECT", "TOTAL", "CODE", "IMAGE", "CHANGES", "VOLUMES", "CLAUDE"]
    row = lines[1].split()
    assert row[0] == "alpha" and row[3] == "476M" and row[4] == "11M" and row[5] == "810M"
    assert lines[2].split()[:1] == ["beta"] and lines[2].split()[3] == "-"
    assert re.search(r"^  sidecar image +410M   postgres:16 \(alpha\)$", out, re.M)
    assert re.search(r"^  build cache +1.8G   docker builder prune frees it$", out, re.M)
    assert "1 left by rebuilds: docker image prune" in out
    assert re.search(r"^  volume +1.1G   boxen.gone.cache$", out, re.M)
    assert "Not boxen's (other things in your rootless Docker): 2 image(s) 181M, 1 container(s) 1.9K, 1 volume(s) 0B" in out
    assert re.search(r"^Free: \S+ of \S+ on .* \(.*projects.*\)$", out, re.M)


def test_df_one_project(vm):
    setup_disk(vm)
    out = vm.ok("df", "alpha").stdout
    assert out.startswith("alpha: ")
    assert re.search(r"^  code +\S+   %s$" % re.escape(str(vm.src / "alpha")), out, re.M)
    assert re.search(r"^ +\S+     build/$", out, re.M) and re.search(r"^ +\S+     small/$", out, re.M)
    assert out.index("build/") < out.index("small/")        # biggest first
    assert re.search(r"^  image +476M   boxen-env-alpha:latest \(beyond the base image\)$", out, re.M)
    assert re.search(r"^  volume +762M   boxen.alpha.cache$", out, re.M)
    assert re.search(r"^  changes +11M   boxen.alpha.env ", out, re.M)
    doc = json.loads(vm.ok("df", "alpha", "--json").stdout)
    assert [p["name"] for p in doc["projects"]] == ["alpha"]
    top = [t["name"] for t in doc["projects"][0]["top"]]
    assert top[0] == "build" and top.index("build") < top.index("small")
    assert vm.boxen("df", "nope").returncode == 1
    assert vm.boxen("df", "alpha", "beta").returncode == 1
    assert vm.boxen("df", "--bogus").returncode == 1


def test_df_docker_down(vm):
    setup_disk(vm)
    vm.set_docker(down=True)
    r = vm.ok("df")
    assert "couldn't reach rootless Docker" in r.stderr
    row = r.stdout.splitlines()[1].split()
    assert row[0] == "alpha" and row[3:6] == ["?", "?", "?"]
    assert "Docker: couldn't reach it" in r.stdout
    doc = json.loads(vm.ok("df", "--json").stdout)
    assert doc["docker"] is False and doc["projects"][0]["image"] is None
    assert doc["projects"][0]["code"] > 0


def test_df_no_projects(vm):
    import shutil
    shutil.rmtree(vm.projects)
    assert "(no projects)" in vm.ok("df").stdout


def test_df_large_report_and_control_characters(vm):
    """Docker's report can outgrow one argument (128 KiB); agents' folder
    names don't reach the terminal with control characters."""
    big = dict(SYSTEM_DF, BuildCache=[{"ID": "c%04d" % i, "Size": "1MB", "Shared": "false",
                                       "Description": "x" * 300} for i in range(800)])
    vm.set_docker(system_df=big)
    (vm.src / "alpha" / "evil\x1b]52;c;aGk=\x07").mkdir()
    (vm.src / "alpha" / "evil\x1b]52;c;aGk=\x07" / "f").write_bytes(b"x" * 400_000)
    doc = json.loads(vm.ok("df", "--json").stdout)
    assert doc["shared"]["build_cache"] == 800_000_000
    out = vm.ok("df", "alpha").stdout
    assert "\x1b" not in out and "\x07" not in out and "]52;c;aGk=" in out
