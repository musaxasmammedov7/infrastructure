#!/usr/bin/env python3
"""Compare the two newest snapshots: what was added, changed, removed.

Writes changes/<new-date>.json. Items are matched by Azure resource ID.
"Who" comes from Resource Graph change history (last 8 days).
"""
import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Fields that change on their own and would only create noise.
NOISE = {
    "etag", "provisioningstate", "systemdata", "changedtime", "createdtime",
    "lastmodifiedtime", "lastmodifiedat", "lastmodifiedby", "lastmodifiedbytype",
    "createdat", "createdby", "createdbytype", "timecreated", "resourceguid",
}
MAX_FIELDS = 6
RISKY_ROLES = {"owner", "contributor", "user access administrator", "role based access control administrator"}


def items(snap):
    """All comparable things: resources, resource groups/subscriptions, role assignments."""
    out = {}
    for r in snap.get("resources", []):
        out[r["id"]] = {"kind": "resource", **r}
    for c in snap.get("containers", []):
        out[c["id"]] = {"kind": "container", **c}
    for a in snap.get("roles", []):
        scope = a.get("scope", "").rstrip("/").rsplit("/", 1)[-1]
        out[a["id"]] = {"kind": "role", "type": "role assignment", **a,
                        "name": f'{a.get("roleName")}: {a.get("principalType")} {a.get("principalId", "")[:8]} → {scope}'}
    return out


def flat(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in NOISE:
                continue
            yield from flat(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from flat(v, f"{path}[{i}]")
    else:
        yield path, obj


def changed_fields(old, new):
    a, b = dict(flat(old)), dict(flat(new))
    diff = sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))
    nice = []
    for k in diff:
        path = k.removeprefix("properties.")
        if "[" in path:  # inside a list: report the list once
            line = path.split("[")[0] + " (list changed)"
        else:            # plain value: show old -> new
            line = f"{path}: {str(a.get(k, '∅'))[:30]} → {str(b.get(k, '∅'))[:30]}"
        if line not in nice:
            nice.append(line)
    return nice


def who(change_log, rid):
    for c in change_log:  # newest first
        t = c.get("targetId", "")
        if t == rid or t.startswith(rid + "/"):
            return c.get("changedBy") or c.get("clientType") or ""
    return ""


def brief(it):
    return {k: it.get(k) for k in ("id", "kind", "name", "type", "resourceGroup", "subscriptionId", "env")}


def report_section(out):
    """One run, human-readable."""
    if not out["from"]:
        return f"## Run {out['to']}\n\nFirst snapshot — baseline, nothing to compare yet.\n"
    lines = [f"## Run {out['to']} (compared with {out['from']})", ""]
    if not (out["added"] or out["changed"] or out["removed"]):
        lines.append("No changes.")
    for kind, title in (("added", "Added"), ("changed", "Changed"), ("removed", "Removed")):
        if not out[kind]:
            continue
        lines += [f"### {title} ({len(out[kind])})", ""]
        for x in sorted(out[kind], key=lambda x: (x.get("env") or "", x.get("name") or "")):
            where = f" · RG `{x['resourceGroup']}`" if x.get("resourceGroup") else ""
            where = f" · **{x['env']}**{where}" if x.get("env") else where
            warn = "⚠ " if x.get("kind") == "role" and str(x.get("name", "")).split(":")[0].lower() in RISKY_ROLES else ""
            lines.append(f"- {warn}**{x.get('name')}** ({(x.get('type') or '').split('/')[-1]}){where}"
                         f" · by {x.get('by') or 'unknown'}")
            for f in x.get("fields", []):
                lines.append(f"  - `{f}`")
            if x.get("moreFields"):
                lines.append(f"  - … +{x['moreFields']} more")
        lines.append("")
    return "\n".join(lines) + "\n"


def when(snap):
    return snap["taken"][:16].replace("T", " ") + " UTC"


def previous_snapshot(new_name):
    """The snapshot of the PREVIOUS run = the newest one in the last commit (any date, even today)."""
    try:
        names = subprocess.run(["git", "ls-tree", "--name-only", "HEAD", "snapshots/"], cwd=ROOT,
                               capture_output=True, text=True, check=True).stdout.split()
        names = sorted(n for n in names if n.endswith(".json"))
        if names:
            raw = subprocess.run(["git", "show", f"HEAD:{names[-1]}"], cwd=ROOT,
                                 capture_output=True, text=True, check=True).stdout
            return json.loads(raw)
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        pass
    files = sorted(p for p in (ROOT / "snapshots").glob("*.json") if p.name != new_name)  # outside git
    return json.loads(files[-1].read_text()) if files else None


def main():
    snaps = sorted((ROOT / "snapshots").glob("*.json"))
    if not snaps:
        raise SystemExit("no snapshots")
    new_p = snaps[-1]
    new = json.loads(new_p.read_text())
    old = previous_snapshot(new_p.name)
    out = {"to": when(new), "from": when(old) if old else None, "added": [], "changed": [], "removed": []}

    if old:
        a, b = items(old), items(new)
        names = {c["subscriptionId"]: c["name"] for snap in (old, new) for c in snap.get("containers", [])
                 if c.get("type") == "microsoft.resources/subscriptions"}
        for it in list(a.values()) + list(b.values()):
            sid = it.get("subscriptionId") or (it.get("scope", "").split("/")[2] if it.get("scope", "").startswith("/subscriptions/") else "")
            it["env"] = names.get(sid, "")
        log = new.get("changeLog", [])
        for rid in sorted(b.keys() - a.keys()):
            out["added"].append({**brief(b[rid]), "by": who(log, rid)})
        for rid in sorted(a.keys() - b.keys()):
            out["removed"].append({**brief(a[rid]), "by": who(log, rid), "item": a[rid]})
        for rid in sorted(a.keys() & b.keys()):
            fields = changed_fields(a[rid], b[rid])
            if fields:
                out["changed"].append({**brief(b[rid]), "by": who(log, rid),
                                       "fields": fields[:MAX_FIELDS], "moreFields": max(0, len(fields) - MAX_FIELDS)})

    day = new_p.stem
    folder = ROOT / "changes"
    folder.mkdir(exist_ok=True)
    for f in list(folder.glob(f"{day}T*")) + list(folder.glob("*.json")):  # old leftovers; only .md is kept
        f.unlink()
    # .md keeps every run of the day, newest first
    md = folder / f"{day}.md"
    earlier = md.read_text().split("\n", 1)[1].lstrip("\n") if md.exists() else ""
    md.write_text(f"# Changes {day}\n\n" + report_section(out) + ("\n" + earlier if earlier else ""))
    print(f'added {len(out["added"])}, changed {len(out["changed"])}, removed {len(out["removed"])}'
          f' (since {out["from"] or "-"})')


if __name__ == "__main__":
    main()
