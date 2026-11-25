#!/usr/bin/env python3
"""Take a READ-ONLY snapshot of Azure via Resource Graph.

Writes snapshots/<YYYY-MM-DD>.json. Only POST queries to Resource Graph
(a read API) are made; nothing in Azure is created or changed.
Sensitive values (webhook URLs, e-mails, keys, Logic App definitions)
are replaced with a short hash so changes are still detected.
"""
import datetime
import hashlib
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ARG_URL = ("https://management.azure.com/providers/"
           "Microsoft.ResourceGraph/resources?api-version=2022-10-01")

QUERIES = {
    "resources": """
Resources
| project id=tolower(id), name, type=tolower(type), kind, location,
          resourceGroup=tolower(resourceGroup), subscriptionId,
          sku, identity, tags, properties
""",
    "containers": """
ResourceContainers
| project id=tolower(id), name, type=tolower(type), subscriptionId,
          resourceGroup=tolower(resourceGroup), location
""",
    "roles": """
AuthorizationResources
| where type =~ 'microsoft.authorization/roleassignments'
| extend rdGuid = tolower(extract('([^/]+)$', 1, tostring(properties.roleDefinitionId)))
| join kind=leftouter (
    AuthorizationResources
    | where type =~ 'microsoft.authorization/roledefinitions'
    | extend rdGuid = tolower(extract('([^/]+)$', 1, tostring(id)))
    | summarize roleName = any(tostring(properties.roleName)) by rdGuid
  ) on rdGuid
| project id=tolower(id),
          principalId=tostring(properties.principalId),
          principalType=tostring(properties.principalType),
          roleName=iff(isempty(roleName), rdGuid, roleName),
          scope=tolower(tostring(properties.scope))
""",
    "dcrAssociations": """
InsightsResources
| where type =~ 'microsoft.insights/datacollectionruleassociations'
| project id=tolower(id),
          rule=tolower(tostring(properties.dataCollectionRuleId)),
          endpoint=tolower(tostring(properties.dataCollectionEndpointId))
""",
    "changeLog": """
ResourceChanges
| extend t = todatetime(properties.changeAttributes.timestamp)
| where t > ago(8d)
| project t, targetId=tolower(tostring(properties.targetResourceId)),
          changeType=tostring(properties.changeType),
          changedBy=tostring(properties.changeAttributes.changedBy),
          clientType=tostring(properties.changeAttributes.clientType),
          operation=tostring(properties.changeAttributes.operation)
| order by t desc
""",
}

SENSITIVE_KEY = re.compile(
    r"(serviceuri|webhook|email|phone|password|secret|token|connectionstring|"
    r"accesskey|primarykey|secondarykey|sharedkey|callback|^uri$|^url$)", re.I)
REDACT_WHOLE = {  # type -> property keys replaced entirely by a hash
    "microsoft.logic/workflows": {"definition", "parameters"},
}


def h(value):
    raw = json.dumps(value, sort_keys=True, default=str).encode()
    return "<redacted:%s>" % hashlib.sha256(raw).hexdigest()[:8]


def sanitize(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if SENSITIVE_KEY.search(k) and isinstance(v, (str, int, float)) and v != "":
                out[k] = h(v)
            else:
                out[k] = sanitize(v)
        return out
    if isinstance(obj, list):
        return [sanitize(x) for x in obj]
    return obj


def arg(query):
    rows, skip = [], None
    while True:
        body = {"query": query, "options": {"resultFormat": "objectArray", "$top": 1000}}
        if skip:
            body["options"]["$skipToken"] = skip
        p = subprocess.run(
            ["az", "rest", "--method", "post", "--url", ARG_URL,
             "--body", json.dumps(body), "--output", "json"],
            capture_output=True, text=True)
        if p.returncode:
            raise RuntimeError(p.stderr.strip()[-2000:])
        res = json.loads(p.stdout)
        rows += res.get("data", [])
        skip = res.get("$skipToken")
        if not skip:
            return rows


def main():
    snap = {"taken": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    for name, q in QUERIES.items():
        try:
            snap[name] = arg(q)
        except RuntimeError as e:
            # A missing table (e.g. no DCRs) must not break the whole run.
            print(f"WARNING: query '{name}' failed: {e}", file=sys.stderr)
            snap[name] = []
        print(f"{name}: {len(snap[name])}")

    for r in snap["resources"]:
        whole = REDACT_WHOLE.get(r["type"], set())
        props = r.get("properties") or {}
        for k in list(props):
            if k in whole:
                props[k] = h(props[k])
    snap = sanitize(snap)

    for key in ("resources", "containers", "roles", "dcrAssociations"):
        snap[key].sort(key=lambda x: x["id"])

    day = snap["taken"][:10]
    out = ROOT / "snapshots" / f"{day}.json"
    out.parent.mkdir(exist_ok=True)
    for old in out.parent.glob(f"{day}*.json"):  # one snapshot per day: a re-run replaces today's
        old.unlink()
    out.write_text(json.dumps(snap, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"saved {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
