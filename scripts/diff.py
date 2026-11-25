#!/usr/bin/env python3
"""Compare the two newest snapshots: what was added, changed, removed.

Writes changes/<new-date>.json. Items are matched by Azure resource ID.
"Who" comes from Resource Graph change history (last 8 days).
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Fields that change on their own and would only create noise.
NOISE = {
    "etag", "provisioningstate", "systemdata", "changedtime", "createdtime",
    "lastmodifiedtime", "lastmodifiedat", "lastmodifiedby", "lastmodifiedbytype",
    "createdat", "createdby", "createdbytype", "timecreated", "resourceguid",
}
MAX_FIELDS = 6


def items(snap):
    """All comparable things: resources, resource groups/subscriptions, role assignments."""
    out = {}
    for r in snap.get("resources", []):
        out[r["id"]] = {"kind": "resource", **r}
    for c in snap.get("containers", []):
        out[c["id"]] = {"kind": "container", **c}
    for a in snap.get("roles", []):
        out[a["id"]] = {"kind": "role", "name": f'{a.get("roleName")} -> {a.get("principalType")} {a.get("principalId", "")[:8]}',
                        "type": "role assignment", **a}
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
    return {k: it.get(k) for k in ("id", "kind", "name", "type", "resourceGroup", "subscriptionId")}


def report(out):
    """Human-readable version of the change file."""
    if not out["from"]:
        return f"# Changes {out['to']}\n\nFirst snapshot — baseline, nothing to compare yet.\n"
    lines = [f"# Changes {out['from']} → {out['to']}", ""]
    if not (out["added"] or out["changed"] or out["removed"]):
        lines.append("No changes.")
    for kind, title in (("added", "Added"), ("changed", "Changed"), ("removed", "Removed")):
        if not out[kind]:
            continue
        lines += [f"## {title} ({len(out[kind])})", ""]
        for x in out[kind]:
            where = f" · RG `{x['resourceGroup']}`" if x.get("resourceGroup") else ""
            lines.append(f"- **{x.get('name')}** ({(x.get('type') or '').split('/')[-1]}){where}"
                         f" · by {x.get('by') or 'unknown'}")
            for f in x.get("fields", []):
                lines.append(f"  - `{f}`")
            if x.get("moreFields"):
                lines.append(f"  - … +{x['moreFields']} more")
        lines.append("")
    return "\n".join(lines) + "\n"


def main():
    snaps = sorted((ROOT / "snapshots").glob("*.json"))
    if not snaps:
        raise SystemExit("no snapshots")
    new_p = snaps[-1]
    new = json.loads(new_p.read_text())
    out = {"to": new_p.stem, "from": None, "added": [], "changed": [], "removed": []}

    if len(snaps) > 1:
        old_p = snaps[-2]
        old = json.loads(old_p.read_text())
        out["from"] = old_p.stem
        a, b = items(old), items(new)
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

    dst = ROOT / "changes" / f"{new_p.stem}.json"
    dst.parent.mkdir(exist_ok=True)
    dst.write_text(json.dumps(out, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    dst.with_suffix(".md").write_text(report(out))
    print(f'added {len(out["added"])}, changed {len(out["changed"])}, removed {len(out["removed"])}'
          f' (since {out["from"] or "-"})')


if __name__ == "__main__":
    main()
