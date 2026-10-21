# shellcheck shell=bash
# boxen df: what boxen keeps on disk (sourced by bin/boxen). Read-only.
#
#   boxen df [--json]          every project, what they share, leftovers, free space
#   boxen df <name> [--json]   one project, with its biggest folders
#
# Per project: code (its folder under ~/src, build output included), image
# (its build image beyond the shared base), changes (written inside its
# containers outside the folder and volumes: lost when they're recreated),
# volumes (its download cache and sidecar data) and claude (its Claude
# history). Docker's sizes come from `docker system df -v`, which prints them
# rounded (decimal units); they're turned back into bytes, so they're close,
# not exact.
#
# --json (for boxen ui and scripts):
#   {"version": 1, "docker": bool,
#    "projects": [{"name", "path", "total", "code", "claude", "claude_path",
#                  "image": {"name", "size"} | null,
#                  "containers": [{"name", "size"}], "volumes": [{"name", "size"}],
#                  "top": [{"name", "size"}]   (with <name> only)}],
#    "shared": {"base": {"names": [...], "size"} | null,
#               "sidecar_images": [{"name", "size", "projects": [...]}],
#               "build_cache": bytes | null, "old_images": {"count", "size"}},
#    "other": {"images", "containers", "volumes": {"count", "size"}},
#    "leftovers": [{"kind": "image"|"container"|"volume", "name", "size"}],
#    "total": bytes (everything boxen's: projects, shared, leftovers),
#    "filesystems": [{"mount", "size", "free", "for": ["projects"|"docker"|"claude", ...]}]}
# Sizes are bytes, or null where they couldn't be measured.

# claude_hist NAME: Claude Code's folder for sessions started in the project
# folder: that path with every character other than a letter or digit as -.
claude_hist() { printf '%s/projects/%s' "$CLAUDE_DIR" "$(src_dir "$1" | sed 's/[^A-Za-z0-9]/-/g')"; }

# du_b PATH: bytes on disk under PATH (that filesystem only), or null.
du_b() {
  local o=""
  [[ -e "$1" ]] || { echo null; return; }
  o=$(du -sx --block-size=1 -- "$1" 2>/dev/null | cut -f1) || true
  if [[ "$o" =~ ^[0-9]+$ ]]; then echo "$o"; else echo null; fi
}

# du_top DIR: its biggest folders as JSON lines {"name", "size"}.
du_top() {
  local dir=$1
  [[ -d "$dir" ]] || return 0
  { du -x -d1 --block-size=1 -- "$dir" 2>/dev/null || true; } \
    | awk -F'\t' -v d="$dir" '$2 != d' | sort -rn | head -n 8 \
    | jq -Rc --arg d "$dir/" 'split("\t") | {name: (.[1] | ltrimstr($d)), size: (.[0] | tonumber)}'
}

# fs_lines LABEL PATH...: the filesystem of each path (or its nearest
# existing parent) as JSON lines {"mount", "size", "free", "for"}.
fs_lines() {
  local label p
  while (( $# >= 2 )); do
    label=$1 p=$2
    shift 2
    [[ -n "$p" ]] || continue
    while [[ ! -e "$p" && "$p" != / ]]; do p=$(dirname "$p"); done
    { df -B1 --output=size,avail,target -- "$p" 2>/dev/null || true; } | tail -n +2 \
      | jq -Rc --arg l "$label" 'capture("^\\s*(?<s>[0-9]+)\\s+(?<f>[0-9]+)\\s+(?<m>.*)$")
                                  | {mount: .m, size: (.s | tonumber), free: (.f | tonumber), for: $l}'
  done
}

DF_JQ='
def bytes:
  if type != "string" then null
  else [capture("^(?<n>[0-9]+(\\.[0-9]+)?)\\s*(?<u>[kKMGTP]?B)$")] | .[0] as $m
    | if $m == null then null
      else ({"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "PB": 1e15}[$m.u]) as $f
        | if $f == null then null else ($m.n | tonumber) * $f | floor end
      end
  end;
def addn: map(select(. != null)) | add // 0;
def ref: sub("^docker\\.io/(library/)?"; "") | if test(":[^/]*$") then . else . + ":latest" end;
# boxen.<project>.<name>
def bparts: [capture("^boxen\\.(?<p>[^.]+)\\.(?<n>.+)$")] | .[0];

. as $df
| ($projs | map({key: .name, value: .}) | from_entries) as $P
| ($df // {}) as $d
| ($d.Images // [] | map(. + {ref: "\(.Repository):\(.Tag)", size: (.Size | bytes),
                              unique: (.UniqueSize | bytes)})) as $imgs
| ($d.Containers // [] | map(. + {name: (.Names // "" | split(",")[0]), size: (.Size | bytes)})) as $ctrs
| ($d.Volumes // [] | map(. + {name: .Name, size: (.Size | bytes)})) as $vols
# images by ID: their names (an image can have several tags)
| ($imgs | group_by(.ID) | map({id: .[0].ID, names: map(.ref), repos: map(.Repository),
                                 size: .[0].size, unique: .[0].unique})) as $byid
| ($imgs | map({key: .ref, value: .ID}) | from_entries) as $refid
# containers: a current project'"'"'s, left over, or not boxen'"'"'s
| ($ctrs | map(. as $c | ($c.name | bparts) as $b
      | . + {project: (if $b != null and $P[$b.p] != null then $b.p else null end),
             leftover: ($b != null and $P[$b.p] == null),
             image_id: ($refid[$c.Image | ref] // (if ($c.Image | startswith("sha256:")) then $c.Image else null end))})) as $cs
| ($vols | map(. as $v | ($v.name | bparts) as $b
      | . + {project: (if $b != null and $P[$b.p] != null then $b.p else null end),
             leftover: ($b != null and $P[$b.p] == null)})) as $vs
| ($cs | map(select(.project != null and (.name | endswith(".env") | not)) | {id: .image_id, p: .project})
       | map(select(.id != null))) as $sideuse
| ($byid | map(. as $i
      | ($i.repos | map(select(startswith("boxen-env-")) | ltrimstr("boxen-env-"))) as $envp
      | . + {kind: (if ($i.repos | index("boxen-env")) != null then "base"
                    elif ($envp | map(select($P[.] != null)) | length) > 0 then "project"
                    elif ($sideuse | map(select(.id == $i.id)) | length) > 0 then "sidecar"
                    elif ($i.repos | map(select(startswith("boxen"))) | length) > 0 then "leftover"
                    elif ($i.repos | all(. == "<none>")) then "old"
                    else "other" end),
             project: ($envp | map(select($P[.] != null)) | .[0])})) as $ik
| ($projs | map(. as $p
      | ($ik | map(select(.kind == "project" and .project == $p.name)) | .[0]) as $img
      | ($cs | map(select(.project == $p.name)) | map({name, size})) as $pc
      | ($vs | map(select(.project == $p.name)) | map({name, size})) as $pv
      | {name: $p.name, path: $p.path,
         code: $p.code, claude: $p.claude, claude_path: $p.claude_path,
         image: (if $img == null then null else {name: ($img.names | map(select(startswith("boxen-env-"))) | .[0]),
                                                  size: $img.unique} end),
         containers: $pc, volumes: $pv}
      + (if $p.top == null then {} else {top: $p.top} end)
      | .total = ([.code, .claude, .image.size] + (.containers | map(.size)) + (.volumes | map(.size)) | addn))) as $projects
| ($ik | map(select(.kind == "base"))) as $base
| {version: 1, docker: $docker,
   projects: $projects,
   shared: {
     base: (if ($base | length) == 0 then null
            else {names: ($base | map(.names[]) | sort),
                  size: (($base | max_by(.size // 0)) as $big
                         | [$big.size] + ($base | map(select(.id != $big.id) | .unique)) | addn)} end),
     sidecar_images: ($ik | map(select(.kind == "sidecar")) | map(. as $i
       | {name: ($i.names | map(select(. != "<none>:<none>")) | .[0] // $i.id[7:19]), size: $i.size,
          projects: ($sideuse | map(select(.id == $i.id) | .p) | unique)})),
     build_cache: (if $df == null then null
                   else ($d.BuildCache // [] | map(select(.Shared != "true" and .Shared != true) | .Size | bytes) | addn) end),
     old_images: ($ik | map(select(.kind == "old")) | {count: length, size: (map(.unique) | addn)})},
   other: {
     images: ($ik | map(select(.kind == "other")) | {count: length, size: (map(.unique) | addn)}),
     containers: ($cs | map(select(.project == null and (.leftover | not))) | {count: length, size: (map(.size) | addn)}),
     volumes: ($vs | map(select(.project == null and (.leftover | not))) | {count: length, size: (map(.size) | addn)})},
   leftovers: (
     ($ik | map(select(.kind == "leftover") | {kind: "image", name: (.names | map(select(. != "<none>:<none>")) | .[0] // .id[7:19]), size: .unique}))
     + ($cs | map(select(.leftover) | {kind: "container", name, size}))
     + ($vs | map(select(.leftover) | {kind: "volume", name, size}))),
   filesystems: ($fs | group_by(.mount) | map(.[0] + {for: (map(.for) | unique)}))}
| .total = ([.projects[].total, .shared.base.size, (.shared.sidecar_images[].size), .shared.build_cache,
             .shared.old_images.size, (.leftovers[].size)] | addn)
'

DF_TEXT='
def hb: if . == null then "?" else . as $n
  | [["T", 1099511627776], ["G", 1073741824], ["M", 1048576], ["K", 1024]] | map(select($n >= .[1]))
  | if length == 0 then "\($n)B"
    else .[0] as $u | ($n / $u[1]) as $v
      | if $v < 10 then ((($v * 10) | floor) / 10 | tostring | if test("\\.") then . else . + ".0" end) + $u[0]
        else "\($v | floor)\($u[0])" end
    end end;
def sp($n): [range(0; ([$n, 0] | max))] | map(" ") | join("");
def r($n): tostring | . + sp($n - length);
def l($n): tostring | sp($n - length) + .;
def sizes(f): map(f) | if length == 0 then 0 else (map(select(. != null)) | add // null) end;
. as $doc
| def dk: if $doc.docker then . else "?" end;
$doc
| if $one then
    (.projects[0] | [
      "\(.name): \(.total | hb)",
      "  code    \(.code | hb | l(7))   \(.path)",
      (.top // [] | .[:6][] | "          \(.size | hb | l(7))     \(.name)/"),
      (if .image == null then (if $doc.docker then "  image   \("-" | l(7))   not built yet"
                               else "  image   \("?" | l(7))   Docker unreachable" end)
       else "  image   \(.image.size | hb | l(7))   \(.image.name) (beyond the base image)" end),
      (.containers[] | "  changes \(.size | hb | l(7))   \(.name) (written inside it, outside the folder and volumes)"),
      (.volumes[] | "  volume  \(.size | hb | l(7))   \(.name)"),
      "  claude  \(.claude | hb | l(7))   \(.claude_path)"
    ] | .[])
  else
    ("PROJECT" | r(24)) + ("TOTAL" | l(8)) + ("CODE" | l(8)) + ("IMAGE" | l(8)) + ("CHANGES" | l(9))
      + ("VOLUMES" | l(9)) + ("CLAUDE" | l(8)),
    (.projects[] | (.name | r(24)) + (.total | hb | l(8)) + (.code | hb | l(8))
      + (if .image == null then ("-" | dk) else (.image.size | hb) end | l(8))
      + (.containers | sizes(.size) | hb | dk | l(9)) + (.volumes | sizes(.size) | hb | dk | l(9))
      + (.claude | hb | l(8))),
    (if (.projects | length) == 0 then "(no projects)" else empty end),
    "",
    (if .docker then
      "Shared by the projects",
      (if .shared.base then "  base image      \(.shared.base.size | hb | l(7))   \(.shared.base.names | join(", "))" else empty end),
      (.shared.sidecar_images[] | "  sidecar image   \(.size | hb | l(7))   \(.name) (\(.projects | join(", ")))"),
      "  build cache     \(.shared.build_cache | hb | l(7))   docker builder prune frees it",
      (if .shared.old_images.count > 0 then
        "  old images      \(.shared.old_images.size | hb | l(7))   \(.shared.old_images.count) left by rebuilds: docker image prune frees them"
       else empty end),
      (if (.leftovers | length) > 0 then
        "Left over from removed projects (remove with docker volume rm / rm -f / image rm NAME)",
        (.leftovers[] | "  \(.kind | r(14))  \(.size | hb | l(7))   \(.name)")
       else empty end),
      ("Not boxen'"'"'s (other things in your rootless Docker): "
        + ([(.other.images | select(.count > 0) | "\(.count) image(s) \(.size | hb)"),
            (.other.containers | select(.count > 0) | "\(.count) container(s) \(.size | hb)"),
            (.other.volumes | select(.count > 0) | "\(.count) volume(s) \(.size | hb)")]
           | if length == 0 then "nothing" else join(", ") end)),
      "boxen in all: \(.total | hb)"
     else "Docker: couldn'"'"'t reach it, so images, containers and volumes aren'"'"'t counted" end),
    "",
    (.filesystems[] | "Free: \(.free | hb) of \(.size | hb) on \(.mount) (\(.for | join(", ")))"),
    "",
    "CODE: the project folder, build output included. IMAGE: its build image beyond the",
    "base. CHANGES: written inside its containers outside the folder and volumes (lost",
    "when they are recreated). VOLUMES: its download cache and sidecar data. CLAUDE: its",
    "Claude history. boxen df <name> lists a project'"'"'s biggest folders."
  end
'

cmd_df() {
  local json=0 one="" n df="" root docker=true pj=() fs=() doc
  while (( $# )); do
    case $1 in
      --json) json=1 ;;
      -*) die "usage: boxen df [<name>] [--json]" ;;
      *) [[ -z "$one" ]] || die "usage: boxen df [<name>] [--json]"; one=$1 ;;
    esac
    shift
  done
  if [[ -n "$one" ]]; then require_project "$one"; fi
  if ! df=$(docker system df -v --format json 2>/dev/null) || ! jq -e 'type == "object"' >/dev/null 2>&1 <<<"$df"; then
    df=null docker=false
  fi
  root=""
  [[ "$docker" == false ]] || root=$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || true)
  for n in $(projects); do
    pj+=("$(jq -nc --arg name "$n" --arg path "$(src_dir "$n")" --arg cp "$(claude_hist "$n")" \
              --argjson code "$(du_b "$(src_dir "$n")")" --argjson claude "$(du_b "$(claude_hist "$n")")" \
              --argjson top "$(if [[ "$n" == "$one" ]]; then du_top "$(src_dir "$n")" | jq -sc .; else echo null; fi)" \
              '{name: $name, path: $path, code: $code, claude: $claude, claude_path: $cp, top: $top}')")
  done
  mapfile -t fs < <(fs_lines projects "$SRC_ROOT" docker "$root" claude "$CLAUDE_DIR")
  # Docker's report goes in on stdin: it can outgrow what one argument may
  # hold (128 KiB).
  doc=$(printf '%s\n' "$df" | jq --argjson docker "$docker" \
          --argjson projs "$(printf '%s\n' "${pj[@]}" | jq -sc 'map(select(. != null))')" \
          --argjson fs "$(printf '%s\n' "${fs[@]}" | jq -sc 'map(select(. != null))')" "$DF_JQ")
  if [[ -n "$one" ]]; then
    doc=$(jq --arg n "$one" '.projects |= map(select(.name == $n))' <<<"$doc")
  fi
  if (( json )); then
    printf '%s\n' "$doc"
  else
    # Folder and project names are the agents': control characters stripped.
    printf '%s\n' "$doc" | jq -r --argjson one "$([[ -n "$one" ]] && echo true || echo false)" "$DF_TEXT" \
      | LC_ALL=C tr -d '\000-\010\013\014\016-\037\177'
    [[ "$docker" == true ]] || warn "couldn't reach rootless Docker ($DOCKER_HOST)"
  fi
}
