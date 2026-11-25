#!/usr/bin/env python3
"""Build Mermaid diagrams from the newest snapshot + its change file.

Output (GitHub renders Mermaid inside .md automatically):
  diagrams/README.md        index + change summary
  diagrams/1-overview.md    subscriptions and what is in them
  diagrams/2-network.md     VNets, subnets, NSG open ports, public exposure
  diagrams/3-environment.md resources per resource group and their links
  diagrams/4-monitoring.md  DCR -> LAW/AMW, alerts -> action groups
  diagrams/5-access.md      who has which role where
  diagrams/6-changes.md     added / changed / removed this run
Colours: green = added, yellow = changed, red dashed = removed, red = risk.
"""
import collections
import hashlib
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "diagrams"

CATEGORY = {
    "Network": ["microsoft.network/"],
    "Compute": ["microsoft.containerservice/", "microsoft.compute/", "microsoft.web/",
                "microsoft.app/", "microsoft.containerinstance/", "microsoft.containerregistry/"],
    "Data": ["microsoft.storage/", "microsoft.dbfor", "microsoft.sql/", "microsoft.documentdb/",
             "microsoft.cache/", "microsoft.eventhub/", "microsoft.servicebus/",
             "microsoft.recoveryservices/", "microsoft.dataprotection/"],
    "Security": ["microsoft.keyvault/", "microsoft.managedidentity/", "microsoft.security/"],
    "Monitoring": ["microsoft.insights/", "microsoft.operationalinsights/", "microsoft.monitor/",
                   "microsoft.alertsmanagement/", "microsoft.dashboard/", "microsoft.logic/",
                   "microsoft.operationsmanagement/", "microsoft.portal/"],
    "AI": ["microsoft.cognitiveservices/", "microsoft.machinelearningservices/", "microsoft.search/"],
}
# Low-level resources hidden on the environment diagram (shown elsewhere or pure noise).
ENV_HIDE = ("microsoft.network/networkinterfaces", "microsoft.compute/disks",
            "microsoft.network/networkwatchers", "microsoft.compute/virtualmachines/extensions",
            "microsoft.network/publicipaddresses", "microsoft.network/networksecuritygroups",
            "microsoft.compute/snapshots", "microsoft.compute/restorepointcollections")
RISKY_ROLES = {"owner", "contributor", "user access administrator",
               "role based access control administrator"}
OPEN_SOURCES = {"*", "internet", "0.0.0.0/0", "any", "::/0"}

CLASSDEFS = [
    "classDef added fill:#dafbe1,stroke:#1a7f37,stroke-width:3px,color:#000",
    "classDef changed fill:#fff8c5,stroke:#9a6700,stroke-width:3px,color:#000",
    "classDef removed fill:#ffebe9,stroke:#cf222e,stroke-width:2px,stroke-dasharray:6 4,color:#000",
    "classDef risk fill:#ffebe9,stroke:#cf222e,stroke-width:3px,color:#000",
    "classDef ext fill:#f0f0f0,stroke:#888,color:#000",
]
TAG = {"added": "NEW · ", "changed": "CHANGED · ", "removed": "REMOVED · "}


def nid(key):
    return "n" + hashlib.md5(key.encode()).hexdigest()[:10]


def esc(s):
    return (str(s).replace("&", "&amp;").replace('"', "#quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def short(t):
    return t.split("/")[-1]


def category(t):
    for cat, prefixes in CATEGORY.items():
        if any(t.startswith(p) for p in prefixes):
            return cat
    return "Other"


class Graph:
    SHAPES = {"rect": ('["', '"]'), "round": ('("', '")'), "db": ('[("', '")]'),
              "hex": ('{{"', '"}}'), "stadium": ('(["', '"])')}

    def __init__(self, direction="LR"):
        self.body = [f"flowchart {direction}"]
        self.edges, self.seen_edges, self.nodes = [], set(), set()
        self.classes = collections.defaultdict(list)
        self.depth = 1

    def node(self, key, label, shape="rect", cls=None):
        i = nid(key)
        if i not in self.nodes:
            self.nodes.add(i)
            o, c = self.SHAPES[shape]
            self.body.append("    " * self.depth + f"{i}{o}{label}{c}")
            if cls:
                self.classes[cls].append(i)
        return i

    def sub(self, key, title, cls=None):
        i = nid(key)
        self.body.append("    " * self.depth + f'subgraph {i}["{title}"]')
        self.depth += 1
        if cls:
            self.classes[cls].append(i)
        return i

    def end(self):
        self.depth -= 1
        self.body.append("    " * self.depth + "end")

    def edge(self, a, b, label=None, dotted=False, both=False):
        key = (min(a, b), max(a, b), label) if both else (a, b, label)
        if a == b or key in self.seen_edges:
            return
        self.seen_edges.add(key)
        arrow = "<-.->" if (dotted and both) else "-.->" if dotted else "<-->" if both else "-->"
        lab = f'|"{label}"|' if label else ""
        self.edges.append(f"    {a} {arrow}{lab} {b}")

    def mermaid(self):
        lines = self.body + self.edges + ["    " + c for c in CLASSDEFS]
        lines += [f"    class {','.join(v)} {k}" for k, v in self.classes.items()]
        return "```mermaid\n" + "\n".join(lines) + "\n```\n"


class Model:
    def __init__(self, snap, changes):
        self.snap = snap
        self.changes = changes
        self.status = {}
        for kind in ("added", "changed", "removed"):
            for x in changes.get(kind, []):
                self.status[x["id"]] = kind
        self.res = {r["id"]: r for r in snap.get("resources", [])}
        self.roles = list(snap.get("roles", []))
        for x in changes.get("removed", []):
            it = dict(x["item"])
            kind = it.pop("kind", "")
            if kind == "resource":
                self.res[it["id"]] = it
            elif kind == "role":
                self.roles.append(it)
        self.subs = {c["subscriptionId"]: c["name"] for c in snap.get("containers", [])
                     if c["type"] == "microsoft.resources/subscriptions"}
        for r in self.res.values():
            self.subs.setdefault(r["subscriptionId"], r["subscriptionId"][:8])
        self._refs = {}

    def st(self, rid):
        return self.status.get(rid)

    def label(self, r, extra=""):
        tag = TAG.get(self.st(r["id"]), "")
        s = f"{tag}<b>{esc(r['name'])}</b><br>{esc(short(r['type']))}"
        return s + (f"<br>{extra}" if extra else "")

    def owner(self, some_id):
        """Map any ID (child like .../subnets/x) to the top-level resource we know."""
        s = (some_id or "").lower()
        while s.count("/") > 4:
            if s in self.res:
                return s
            s = s.rsplit("/", 2)[0] if s.count("/") > 8 else s.rsplit("/", 1)[0]
        return None

    def refs(self, rid):
        """Resource IDs referenced anywhere in a resource (properties, identity)."""
        if rid in self._refs:
            return self._refs[rid]
        found = set()

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if isinstance(k, str) and k.lower().startswith("/subscriptions/"):
                        walk(k)
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
            elif isinstance(o, str) and o.lower().startswith("/subscriptions/"):
                t = self.owner(o)
                if t and t != rid:
                    found.add(t)
        r = self.res[rid]
        walk(r.get("properties"))
        walk(r.get("identity"))
        self._refs[rid] = found
        return found

    def of_type(self, *types):
        return [r for r in self.res.values() if r["type"] in types]


def P(r):
    return r.get("properties") or {}


# ---------------------------------------------------------------- 1 overview
def overview(m):
    g = Graph("LR")
    inet = g.node("internet", "Internet", "stadium", "ext")
    cat_node, cat_members = {}, collections.defaultdict(list)
    for r in m.res.values():
        cat_members[(r["subscriptionId"], category(r["type"]))].append(r)
    for sub_id, sub_name in sorted(m.subs.items(), key=lambda x: x[1]):
        g.sub("sub:" + sub_id, f"Subscription: {esc(sub_name)}")
        for cat in list(CATEGORY) + ["Other"]:
            members = cat_members.get((sub_id, cat))
            if not members:
                continue
            names = sorted({r["name"] for r in members})
            shown = ", ".join(esc(n) for n in names[:4]) + (f" … +{len(names) - 4}" if len(names) > 4 else "")
            sts = {m.st(r["id"]) for r in members} - {None}
            cls = "changed" if sts else None
            note = (" · " + ", ".join(sorted(sts))) if sts else ""
            cat_node[(sub_id, cat)] = g.node(f"cat:{sub_id}:{cat}",
                                             f"<b>{cat}</b> ({len(members)}){esc(note)}<br>{shown}", "round", cls)
        g.end()
    # Internet -> subscriptions that own public IPs
    pips = collections.Counter(r["subscriptionId"] for r in m.of_type("microsoft.network/publicipaddresses"))
    for sub_id, n in pips.items():
        if (sub_id, "Network") in cat_node:
            g.edge(inet, cat_node[(sub_id, "Network")], f"public IP ×{n}")
    # category -> category links derived from resource references
    for rid, r in m.res.items():
        a = (r["subscriptionId"], category(r["type"]))
        for t in m.refs(rid):
            tr = m.res[t]
            b = (tr["subscriptionId"], category(tr["type"]))
            if a != b and a in cat_node and b in cat_node:
                g.edge(cat_node[a], cat_node[b], "peering" if a[1] == b[1] == "Network" else None)
    return g


# ---------------------------------------------------------------- 2 network
def nsg_open_rules(nsg):
    out = []
    for rule in P(nsg).get("securityRules", []):
        p = rule.get("properties", rule)
        if p.get("direction") != "Inbound" or p.get("access") != "Allow":
            continue
        srcs = [p.get("sourceAddressPrefix")] + list(p.get("sourceAddressPrefixes") or [])
        srcs = [s for s in srcs if s]
        if not any(s.lower() in OPEN_SOURCES for s in srcs):
            continue
        ports = [p.get("destinationPortRange")] + list(p.get("destinationPortRanges") or [])
        ports = ",".join(x for x in ports if x) or "*"
        out.append(f"{ports}/{p.get('protocol', '*')} from {srcs[0]}")
    return out


def exposure(r):
    """Public endpoint of a PaaS resource: None, 'filtered' or 'open'."""
    p = P(r)
    pna = str(p.get("publicNetworkAccess") or "").lower()
    if pna == "disabled":
        return None
    acls = p.get("networkAcls") or p.get("networkRuleSet") or {}
    if str(acls.get("defaultAction", "")).lower() == "deny":
        return "filtered"
    if p.get("network", {}).get("publicNetworkAccess", "").lower() == "disabled":
        return None
    if pna == "enabled" or acls:
        return "open"
    return None


PAAS_EXPOSURE = ("microsoft.storage/storageaccounts", "microsoft.keyvault/vaults",
                 "microsoft.dbforpostgresql/flexibleservers", "microsoft.dbformysql/flexibleservers",
                 "microsoft.sql/servers", "microsoft.documentdb/databaseaccounts",
                 "microsoft.containerregistry/registries", "microsoft.eventhub/namespaces",
                 "microsoft.servicebus/namespaces", "microsoft.cognitiveservices/accounts",
                 "microsoft.cache/redis")


def network(m):
    g = Graph("LR")
    inet = g.node("internet", "Internet", "stadium", "ext")
    subnet_node = {}

    def rnode(rid, extra="", shape="rect", cls=None):
        r = m.res[rid]
        return g.node(rid, m.label(r, extra), shape, m.st(rid) or cls)

    for v in sorted(m.of_type("microsoft.network/virtualnetworks"), key=lambda r: r["name"]):
        space = ", ".join(P(v).get("addressSpace", {}).get("addressPrefixes", []))
        tag = TAG.get(m.st(v["id"]), "")
        g.sub(v["id"], f"{tag}VNet {esc(v['name'])} · {esc(space)}", m.st(v["id"]))
        for s in P(v).get("subnets", []):
            sp = s.get("properties", {})
            prefix = sp.get("addressPrefix") or ", ".join(sp.get("addressPrefixes", []))
            subnet_node[s["id"].lower()] = g.node(s["id"].lower(), f"{esc(s['name'])}<br>{esc(prefix)}")
        g.end()

    def to_subnet(any_id):
        return subnet_node.get((any_id or "").lower())

    # peerings (subgraph to subgraph)
    for v in m.of_type("microsoft.network/virtualnetworks"):
        for pe in P(v).get("virtualNetworkPeerings", []):
            pp = pe.get("properties", {})
            remote = (pp.get("remoteVirtualNetwork") or {}).get("id", "").lower()
            if not remote:
                continue
            b = nid(remote) if remote in m.res else g.node(remote, f"remote VNet<br>{esc(remote.rsplit('/', 1)[-1])}", "rect", "ext")
            g.edge(nid(v["id"]), b, f"peering {pp.get('peeringState', '')}", both=True)

    # NSGs
    for n in m.of_type("microsoft.network/networksecuritygroups"):
        rules = nsg_open_rules(n)
        extra = "<br>".join("⚠ open " + esc(x) for x in rules)
        a = rnode(n["id"], extra, "hex", "risk" if rules else None)
        for s in P(n).get("subnets", []):
            if to_subnet(s.get("id")):
                g.edge(a, to_subnet(s["id"]), "filters", dotted=True)
        if rules:
            g.edge(inet, a, "inbound allowed", dotted=True)

    # NICs -> VM / subnet / NSG / public IP
    pip_owner = {}
    for nic in m.of_type("microsoft.network/networkinterfaces"):
        np = P(nic)
        holder = m.owner((np.get("virtualMachine") or {}).get("id")) or nic["id"]
        h = rnode(holder)
        for ipc in np.get("ipConfigurations", []):
            ip = ipc.get("properties", {})
            if to_subnet((ip.get("subnet") or {}).get("id")):
                g.edge(h, to_subnet(ip["subnet"]["id"]), esc(ip.get("privateIPAddress", "")))
            pid = (ip.get("publicIPAddress") or {}).get("id", "").lower()
            if pid:
                pip_owner[pid] = holder
        nsg = m.owner((np.get("networkSecurityGroup") or {}).get("id"))
        if nsg:
            g.edge(nid(nsg), h, "filters", dotted=True)

    # Public IPs: Internet -> IP -> owner
    for pip in m.of_type("microsoft.network/publicipaddresses"):
        pp = P(pip)
        a = rnode(pip["id"], esc(pp.get("ipAddress", "not allocated")), "round")
        g.edge(inet, a)
        owner = pip_owner.get(pip["id"]) or m.owner((pp.get("ipConfiguration") or {}).get("id"))
        if owner and owner != pip["id"]:
            g.edge(a, rnode(owner))

    # NAT gateways, gateways, AKS, app gateways, private endpoints
    for r in m.res.values():
        t, p = r["type"], P(r)
        if t == "microsoft.network/natgateways":
            for s in p.get("subnets", []):
                if to_subnet(s.get("id")):
                    g.edge(to_subnet(s["id"]), rnode(r["id"]), "outbound")
        elif t in ("microsoft.network/virtualnetworkgateways", "microsoft.network/applicationgateways",
                   "microsoft.network/bastionhosts", "microsoft.network/azurefirewalls"):
            for ipc in p.get("ipConfigurations", []) + p.get("gatewayIPConfigurations", []):
                sid = (ipc.get("properties", {}).get("subnet") or {}).get("id")
                if to_subnet(sid):
                    g.edge(rnode(r["id"]), to_subnet(sid))
        elif t == "microsoft.network/connections":
            gw1 = m.owner((p.get("virtualNetworkGateway1") or {}).get("id"))
            other = m.owner((p.get("localNetworkGateway2") or {}).get("id")) or \
                m.owner((p.get("virtualNetworkGateway2") or {}).get("id"))
            if gw1 and other:
                g.edge(rnode(gw1), rnode(other, esc(P(m.res[other]).get("gatewayIpAddress", ""))),
                       f"{p.get('connectionType', '')} {p.get('connectionStatus', '')}".strip(), both=True)
        elif t == "microsoft.containerservice/managedclusters":
            for pool in p.get("agentPoolProfiles", []):
                if to_subnet(pool.get("vnetSubnetID")):
                    g.edge(rnode(r["id"], esc(p.get("powerState", {}).get("code", ""))),
                           to_subnet(pool["vnetSubnetID"]), esc(pool.get("name", "")))
            api = p.get("apiServerAccessProfile") or {}
            if not api.get("enablePrivateCluster"):
                ips = api.get("authorizedIPRanges") or []
                g.edge(inet, rnode(r["id"]),
                       "API server: " + (f"{len(ips)} allowed ranges" if ips else "⚠ open to all"), dotted=True)
        elif t == "microsoft.network/privateendpoints":
            a = rnode(r["id"])
            if to_subnet((p.get("subnet") or {}).get("id")):
                g.edge(a, to_subnet(p["subnet"]["id"]))
            for c in p.get("privateLinkServiceConnections", []) + p.get("manualPrivateLinkServiceConnections", []):
                tgt = m.owner(c.get("properties", {}).get("privateLinkServiceId"))
                if tgt:
                    g.edge(a, rnode(tgt), "private link")
        elif t in ("microsoft.dbforpostgresql/flexibleservers", "microsoft.dbformysql/flexibleservers"):
            sid = (p.get("network") or {}).get("delegatedSubnetResourceId")
            if to_subnet(sid):
                g.edge(rnode(r["id"], shape="db"), to_subnet(sid), "VNet integrated")
        if t in PAAS_EXPOSURE:
            e = exposure(r)
            if e == "open":
                g.edge(inet, rnode(r["id"], "⚠ public endpoint, no IP filter", cls="risk"), "public", dotted=True)
            elif e == "filtered":
                g.edge(inet, rnode(r["id"], "public endpoint, IP filter"), "filtered", dotted=True)
    return g


# ---------------------------------------------------------------- 3 environment
def environment(m):
    g = Graph("LR")
    shown = {rid for rid, r in m.res.items()
             if not r["type"].startswith(ENV_HIDE) and category(r["type"]) != "Monitoring"}
    by_rg = collections.defaultdict(list)
    for rid in shown:
        r = m.res[rid]
        by_rg[(r["subscriptionId"], r["resourceGroup"])].append(r)
    for sub_id, sub_name in sorted(m.subs.items(), key=lambda x: x[1]):
        rgs = sorted(rg for (s, rg) in by_rg if s == sub_id)
        if not rgs:
            continue
        g.sub("env:" + sub_id, f"Subscription: {esc(sub_name)}")
        for rg in rgs:
            g.sub(f"env:{sub_id}:{rg}", f"RG {esc(rg)}")
            for r in sorted(by_rg[(sub_id, rg)], key=lambda r: (category(r["type"]), r["name"])):
                shape = "db" if category(r["type"]) == "Data" else "rect"
                g.node(r["id"], m.label(r), shape, m.st(r["id"]))
            g.end()
        g.end()
    # links: direct references, or one hop through a hidden resource (VM -> NIC -> VNet)
    for rid in shown:
        targets = set()
        for t in m.refs(rid):
            if t in shown:
                targets.add(t)
            else:
                targets |= {x for x in m.refs(t) if x in shown}
        for t in targets - {rid}:
            label = "identity" if m.res[t]["type"] == "microsoft.managedidentity/userassignedidentities" else None
            g.edge(nid(rid), nid(t), label)
    return g


# ---------------------------------------------------------------- 4 monitoring
def receivers(ag):
    p = P(ag)
    parts = []
    for key, name in (("emailReceivers", "e-mail"), ("smsReceivers", "SMS"), ("webhookReceivers", "webhook"),
                      ("logicAppReceivers", "Logic App"), ("azureFunctionReceivers", "Function"),
                      ("armRoleReceivers", "ARM role"), ("eventHubReceivers", "Event Hub"),
                      ("automationRunbookReceivers", "runbook"), ("voiceReceivers", "voice")):
        n = len(p.get(key) or [])
        if n:
            parts.append(f"{name} ×{n}")
    return ", ".join(parts) or "no receivers"


def monitoring(m):
    g = Graph("LR")
    mon = {rid for rid, r in m.res.items() if category(r["type"]) == "Monitoring"}

    def node(rid):
        r = m.res[rid]
        p, t, extra, shape = P(r), r["type"], "", "rect"
        if t == "microsoft.operationalinsights/workspaces":
            cap = (p.get("workspaceCapping") or {}).get("dailyQuotaGb", -1)
            extra = f"{p.get('retentionInDays', '?')} days" + (f" · cap {cap} GB/day" if cap not in (-1, None) else "")
            shape = "db"
        elif t == "microsoft.monitor/accounts":
            shape = "db"
        elif t == "microsoft.insights/actiongroups":
            extra = receivers(r)
            shape = "hex"
        elif t in ("microsoft.insights/metricalerts", "microsoft.insights/scheduledqueryrules"):
            extra = f"sev {p.get('severity', '?')}" + ("" if p.get("enabled", True) else " · DISABLED")
        elif t == "microsoft.insights/activitylogalerts":
            extra = "" if p.get("enabled", True) else "DISABLED"
        elif t == "microsoft.insights/datacollectionrules":
            streams = sorted({s for f in p.get("dataFlows", []) for s in f.get("streams", [])})
            extra = esc(", ".join(x.replace("Microsoft-", "") for x in streams)[:120])
        if rid not in mon:
            return g.node(rid, m.label(r), "rect", m.st(rid) or "ext")
        return g.node(rid, m.label(r, extra), shape, m.st(rid))

    for sub_id, sub_name in sorted(m.subs.items(), key=lambda x: x[1]):
        ids = sorted((rid for rid in mon if m.res[rid]["subscriptionId"] == sub_id), key=lambda x: (m.res[x]["type"], x))
        if not ids:
            continue
        g.sub("mon:" + sub_id, f"Subscription: {esc(sub_name)}")
        for rid in ids:
            node(rid)
        g.end()

    for rid in mon:
        t = m.res[rid]["type"]
        for tgt in m.refs(rid):
            tt = m.res[tgt]["type"]
            if t.endswith("alerts") or t.endswith("queryrules"):
                label = "notifies" if tt == "microsoft.insights/actiongroups" else "watches"
            elif t == "microsoft.insights/datacollectionrules":
                label = "sends to"
            elif t == "microsoft.insights/actiongroups":
                label = "calls"
            else:
                label = None
            if label == "watches":
                g.edge(node(tgt), node(rid), label, dotted=True)
            else:
                g.edge(node(rid), node(tgt), label)

    # DCR associations: which resource sends its data through which rule
    for a in m.snap.get("dcrAssociations", []):
        src = m.owner(a["id"].split("/providers/microsoft.insights/")[0])
        rule = m.owner(a.get("rule"))
        if src and rule:
            g.edge(node(src), node(rule), "agent data")
    return g


# ---------------------------------------------------------------- 5 access
def access(m):
    g = Graph("LR")
    principal_name = {}
    for r in m.res.values():
        pid = (r.get("identity") or {}).get("principalId")
        if pid:
            principal_name[pid] = f"{r['name']} (system identity)"
        if r["type"] == "microsoft.managedidentity/userassignedidentities":
            principal_name[P(r).get("principalId")] = f"{r['name']} (managed identity)"

    def scope_label(scope):
        s = scope.lower()
        if s.startswith("/providers/microsoft.management/managementgroups/"):
            return "Mgmt group " + s.rsplit("/", 1)[-1]
        parts = s.split("/")
        if len(parts) == 3:
            return "Subscription " + m.subs.get(parts[2], parts[2][:8])
        if len(parts) == 5:
            return "RG " + parts[4]
        o = m.owner(s)
        return (m.res[o]["name"] + " (" + short(m.res[o]["type"]) + ")") if o else s.rsplit("/", 1)[-1]

    g.sub("principals", "Who")
    by_p = collections.defaultdict(list)
    for a in m.roles:
        by_p[a["principalId"]].append(a)
    pnode = {}
    for pid, assigns in sorted(by_p.items(), key=lambda x: principal_name.get(x[0], x[1][0]["principalType"] + x[0])):
        sts = {m.st(a["id"]) for a in assigns} - {None}
        risky = any(a["roleName"].lower() in RISKY_ROLES for a in assigns)
        cls = sorted(sts)[0] if sts else ("risk" if risky else None)
        name = principal_name.get(pid) or f"{assigns[0]['principalType']} {pid[:8]}…"
        pnode[pid] = g.node("p:" + pid, esc(name), "round", cls)
    g.end()
    g.sub("scopes", "Where")
    snode = {}
    for a in m.roles:
        if a["scope"] not in snode:
            snode[a["scope"]] = g.node("s:" + a["scope"], esc(scope_label(a["scope"])), "rect", "ext")
    g.end()
    for a in m.roles:
        role = a["roleName"]
        warn = "⚠ " if role.lower() in RISKY_ROLES else ""
        tag = TAG.get(m.st(a["id"]), "")
        g.edge(pnode[a["principalId"]], snode[a["scope"]], esc(f"{tag}{warn}{role}"),
               dotted=m.st(a["id"]) == "removed")
    return g


# ---------------------------------------------------------------- 6 changes
def changes_graph(m):
    ch = m.changes
    g = Graph("TB")
    if not ch.get("from"):
        g.node("baseline", f"First snapshot {esc(ch.get('to'))} — baseline, nothing to compare yet", "round", "ext")
        return g
    if not (ch["added"] or ch["changed"] or ch["removed"]):
        g.node("none", f"No changes between {ch['from']} and {ch['to']}", "round", "ext")
        return g
    for kind, title in (("added", "Added"), ("changed", "Changed"), ("removed", "Removed")):
        if not ch[kind]:
            continue
        g.sub("chg:" + kind, f"{title} ({len(ch[kind])})")
        for x in ch[kind]:
            lines = [f"<b>{esc(x.get('name'))}</b>", esc(short(x.get("type") or ""))]
            if x.get("resourceGroup"):
                lines.append("RG " + esc(x["resourceGroup"]))
            for f in x.get("fields", []):
                lines.append("• " + esc(f[:70]))
            if x.get("moreFields"):
                lines.append(f"• … +{x['moreFields']} more")
            lines.append("by " + esc(x.get("by") or "unknown (older than history)"))
            g.node(x["id"], "<br>".join(lines), "rect", kind)
        g.end()
    return g


DIAGRAMS = [
    ("1-overview", "Overview", "Subscriptions and what is inside them. Arrows = one resource references another.", overview),
    ("2-network", "Network", "VNets, subnets, NSGs with rules open to the Internet, public IPs, public PaaS endpoints.", network),
    ("3-environment", "Environment", "Resources per resource group and the links between them (monitoring hidden, see 4).", environment),
    ("4-monitoring", "Monitoring & alerts", "Data collection rules, workspaces, alert rules and where alerts go. "
                                            "Diagnostic settings are not visible in Resource Graph.", monitoring),
    ("5-access", "Access", "Role assignments: who has which role on which scope. ⚠ = Owner/Contributor/User Access Admin.", access),
    ("6-changes", "Changes", "What was added, changed or removed since the previous snapshot.", changes_graph),
]
LEGEND = ("Legend: green = added · yellow = changed · red dashed = removed (shown for one run) · "
          "red solid = risk · grey = outside this diagram\n")


def main():
    snaps = sorted((ROOT / "snapshots").glob("*.json"))
    snap = json.loads(snaps[-1].read_text())
    date = snaps[-1].stem
    ch_p = ROOT / "changes" / f"{date}.json"
    ch = json.loads(ch_p.read_text()) if ch_p.exists() else {"to": date, "from": None,
                                                             "added": [], "changed": [], "removed": []}
    m = Model(snap, ch)
    OUT.mkdir(exist_ok=True)
    summary = (f"{len(ch['added'])} added, {len(ch['changed'])} changed, {len(ch['removed'])} removed"
               f" since {ch['from']}" if ch.get("from") else "first snapshot (baseline)")
    index = [f"# Azure diagrams — {date}\n", f"Changes: {summary}\n"]
    for fname, title, desc, fn in DIAGRAMS:
        text = fn(m).mermaid()
        if len(text) > 48000:
            print(f"WARNING: {fname} is {len(text)} chars; GitHub may refuse to render (> 50 000)")
        (OUT / f"{fname}.md").write_text(f"# {title} — {date}\n\n{desc}\n\n{LEGEND}\n{text}")
        index.append(f"- [{title}]({fname}.md) — {desc}")
    (OUT / "README.md").write_text("\n".join(index) + "\n")
    print(f"## Azure diagrams {date}\n\n{summary}\n")
    for kind in ("added", "changed", "removed"):
        for x in ch.get(kind, []):
            print(f"- {kind}: {x.get('name')} ({short(x.get('type') or '')}) by {x.get('by') or 'unknown'}")


if __name__ == "__main__":
    main()
