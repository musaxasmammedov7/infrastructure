#!/usr/bin/env python3
"""Newest snapshot + its change file -> draw.io diagrams.

One .drawio file per diagram in build/; the workflow exports each to diagrams/<name>.png:
  1-overview.drawio           only when there is more than one subscription
  2-infra-<subscription>.drawio  VNets -> subnets -> resources, services, Internet exposure
  3-monitoring.drawio         data collection and alert routing
  4-access.drawio             who has which role where
  5-changes.drawio            added / changed / removed since the previous snapshot
Status: green border = added, orange = changed, red dashed = removed, red solid = risk.
Optional 2nd argument: write an approximate SVG preview of every page into that folder.
"""
import collections
import hashlib
import json
import math
import pathlib
import re
import sys
from xml.sax.saxutils import escape

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "diagrams"   # committed: PNG + README
BUILD = ROOT / "build"    # not committed: .drawio sources for the PNG export

# ------------------------------------------------------------------ styles
CAT_COLORS = {
    "Network": ("#DAE8FC", "#6C8EBF"), "Compute": ("#D5E8D4", "#82B366"),
    "Data": ("#E1D5E7", "#9673A6"), "Security": ("#F8CECC", "#B85450"),
    "AI": ("#FFE6CC", "#D79B00"), "Monitoring": ("#EEEEEE", "#7A7A7A"),
    "Other": ("#FFFFFF", "#9E9E9E"), "External": ("#FFF2CC", "#D6B656"),
}
STATUS_STROKE = {"added": ("#1A7F37", 3, False), "changed": ("#BF8700", 3, False),
                 "removed": ("#CF222E", 2, True), "risk": ("#CF222E", 3, False)}
STATUS_TAG = {"added": "<font color='#1A7F37'><b>NEW</b></font>",
              "changed": "<font color='#BF8700'><b>CHANGED</b></font>",
              "removed": "<font color='#CF222E'><b>REMOVED</b></font>"}
CONT_COLORS = {"sub": ("#F7F7F7", "#5F5F5F", 15), "vnet": ("#EAF2FB", "#2E75B6", 13),
               "subnet": ("#FFFFFF", "#6C8EBF", 11), "group": ("#FCFCFC", "#9E9E9E", 12),
               "ext": ("#FFF8E1", "#C9A227", 12), "plain": ("none", "none", 12)}
E_WEB = "strokeColor=#C9A227;strokeWidth=2;endArrow=block;endFill=1;"
E_RISK = "strokeColor=#CF222E;strokeWidth=2;dashed=1;endArrow=block;endFill=1;"
E_PEER = "strokeColor=#2E75B6;strokeWidth=3;endArrow=none;startArrow=none;"
E_VPN = "strokeColor=#B85450;strokeWidth=2;dashed=1;dashPattern=8 4;endArrow=none;startArrow=none;"
E_LINK = "strokeColor=#9673A6;strokeWidth=1.5;dashed=1;endArrow=block;endFill=1;"
E_FLOW = "strokeColor=#5B8C4A;strokeWidth=1.5;endArrow=block;endFill=1;"

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
# Never drawn as their own card (merged into other cards or pure noise).
HIDE = ("microsoft.network/networkinterfaces", "microsoft.compute/disks",
        "microsoft.network/networkwatchers", "microsoft.compute/virtualmachines/extensions",
        "microsoft.network/privatednszones/virtualnetworklinks", "microsoft.compute/snapshots",
        "microsoft.compute/restorepointcollections", "microsoft.operationsmanagement/solutions",
        "microsoft.network/virtualnetworks")
# Monitoring types summarised in one card on the infra page (details on the monitoring page).
MON_SUMMARY = ("microsoft.insights/metricalerts", "microsoft.insights/activitylogalerts",
               "microsoft.insights/scheduledqueryrules", "microsoft.insights/actiongroups",
               "microsoft.insights/datacollectionrules", "microsoft.insights/datacollectionendpoints",
               "microsoft.alertsmanagement/", "microsoft.portal/dashboards", "microsoft.insights/webtests",
               "microsoft.alertsmanagement/prometheusrulegroups", "microsoft.insights/workbooks")
RISKY_ROLES = {"owner", "contributor", "user access administrator",
               "role based access control administrator"}
OPEN_SOURCES = {"*", "internet", "0.0.0.0/0", "any", "::/0"}
PAAS = ("microsoft.storage/storageaccounts", "microsoft.keyvault/vaults",
        "microsoft.dbforpostgresql/flexibleservers", "microsoft.dbformysql/flexibleservers",
        "microsoft.sql/servers", "microsoft.documentdb/databaseaccounts",
        "microsoft.containerregistry/registries", "microsoft.eventhub/namespaces",
        "microsoft.servicebus/namespaces", "microsoft.cognitiveservices/accounts", "microsoft.cache/redis")
SUBNET_RE = re.compile(r"(/subscriptions/[^/]+/resourcegroups/[^/]+/providers/microsoft\.network/"
                       r"virtualnetworks/[^/]+/subnets/[^/]+)", re.I)


def category(t):
    for cat, prefixes in CATEGORY.items():
        if any(t.startswith(p) for p in prefixes):
            return cat
    return "Other"


def short(t):
    return (t or "").split("/")[-1]


def h(s):
    return escape(str(s))


def P(r):
    return r.get("properties") or {}


# ------------------------------------------------------------------ layout engine
CARD_W, PAD, GAP = 210, 10, 10


def plain_lines(label):
    return [re.sub(r"<[^>]+>", "", p).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
            for p in re.split(r"<br\s*/?>", label)]


def text_h(label, w, fs):
    cw = fs * 0.60
    n = sum(max(1, math.ceil(len(ln) * cw / max(1, w))) for ln in plain_lines(label))
    return n * fs * 1.25 + 10


class Box:
    """kind: card | cont | text. layout for containers: v | h | grid."""

    def __init__(self, key, label, kind="card", style=None, layout="v", cols=1, w=None, fs=11):
        self.key, self.label, self.kind, self.style = key, label, kind, style or {}
        self.layout, self.cols, self.fixed_w, self.fs = layout, cols, w, fs
        self.children = []

    def add(self, child):
        self.children.append(child)
        return child

    def measure(self):
        if self.kind in ("card", "text"):
            self.w = self.fixed_w or CARD_W
            self.h = max(40, text_h(self.label, self.w - 12, self.fs))
            return
        for c in self.children:
            c.measure()
        kids = self.children
        if self.layout == "grid" and kids:
            cols = max(1, min(self.cols, len(kids)))
            rows = [kids[i:i + cols] for i in range(0, len(kids), cols)]
            colw = [max(r[i].w for r in rows if i < len(r)) for i in range(cols)]
            y = 0
            for row in rows:
                x = 0
                for i, c in enumerate(row):
                    c.rx, c.ry = x, y
                    x += colw[i] + GAP
                y += max(c.h for c in row) + GAP
            iw, ih = sum(colw) + GAP * (cols - 1), y - GAP
        elif self.layout == "h" and kids:
            x = 0
            for c in kids:
                c.rx, c.ry = x, 0
                x += c.w + GAP
            iw, ih = x - GAP, max(c.h for c in kids)
        elif kids:
            y = 0
            for c in kids:
                c.rx, c.ry = 0, y
                y += c.h + GAP
            iw, ih = max(c.w for c in kids), y - GAP
        else:
            iw, ih = 0, 0
        pad = 0 if self.style.get("plain") else PAD
        self.w = max(iw + 2 * pad, self.fixed_w or 0, min(420, len(plain_lines(self.label)[0]) * self.fs * 0.62 + 24))
        title = text_h(self.label, self.w - 16, self.fs) if self.label else 0
        for c in kids:
            c.rx += pad
            c.ry += title + (pad if not self.label else 0)
        self.h = title + ih + pad + (pad if not self.label else 0) if kids else max(title + 4, 30)

    def place(self, x, y):
        self.x, self.y = x, y
        for c in self.children:
            c.place(x + c.rx, y + c.ry)

    def walk(self, parent=None):
        yield self, parent
        for c in self.children:
            yield from c.walk(self)


def cid(key):
    return "c" + hashlib.md5(key.encode()).hexdigest()[:12]


def card_style(cat, status=None, fs=11):
    fill, stroke = CAT_COLORS.get(cat, CAT_COLORS["Other"])
    sw, dashed = 1, False
    if status in STATUS_STROKE:
        stroke, sw, dashed = STATUS_STROKE[status]
    return (f"rounded=1;arcSize=8;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};strokeWidth={sw};"
            f"fontSize={fs};align=center;verticalAlign=middle;spacing=4;" + ("dashed=1;" if dashed else ""))


def cont_style(kind, status=None):
    fill, stroke, fs = CONT_COLORS[kind]
    if kind == "plain":
        return "text;html=1;fillColor=none;strokeColor=none;container=1;collapsible=0;", fs
    sw, dashed = (2 if kind in ("sub", "vnet") else 1), kind == "subnet"
    if status in STATUS_STROKE:
        stroke, sw, d = STATUS_STROKE[status]
        dashed = dashed or d
    return (f"rounded=1;arcSize=2;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};strokeWidth={sw};"
            f"fontSize={fs};fontStyle=0;align=left;verticalAlign=top;spacingLeft=8;spacingTop=2;container=1;collapsible=0;"
            + ("dashed=1;" if dashed else "")), fs


def card(key, label, cat, status=None, w=None):
    return Box(key, label, "card", {"style": card_style(cat, status)}, w=w)


def cont(key, label, kind="group", layout="v", cols=1, status=None, w=None):
    st, fs = cont_style(kind, status)
    return Box(key, label, "cont", {"style": st, "plain": kind == "plain"}, layout, cols, w=w, fs=fs)


def text(key, label, w=900, fs=12):
    return Box(key, label, "text",
               {"style": f"text;html=1;whiteSpace=wrap;align=left;verticalAlign=top;fontSize={fs};spacing=2;"}, w=w, fs=fs)


class Page:
    def __init__(self, name, title):
        self.name = name
        self.root = cont("root:" + name, "", "plain", "v")
        self.root.add(text("title:" + name, title, w=1100, fs=13))
        self.edges = []

    def edge(self, src, tgt, style, label=""):
        self.edges.append((src, tgt, style, label))

    def build(self):
        self.root.measure()
        self.root.place(20, 20)
        cells, keys = [], {}
        for b, parent in self.root.walk():
            keys[b.key] = b
        for b, parent in self.root.walk():
            if b is self.root:
                continue
            pid = cid(parent.key) if parent is not None and parent is not self.root else "1"
            rx, ry = (b.x - parent.x, b.y - parent.y) if pid != "1" else (b.x, b.y)
            cells.append(f'<mxCell id="{cid(b.key)}" value="{escape(b.label, {chr(34): "&quot;"})}" '
                         f'style="{b.style["style"]}" vertex="1" parent="{pid}">'
                         f'<mxGeometry x="{rx:.0f}" y="{ry:.0f}" width="{b.w:.0f}" height="{b.h:.0f}" as="geometry"/></mxCell>')
        seen = set()
        for i, (s, t, st, label) in enumerate(self.edges):
            k = (frozenset((s, t)), label) if st in (E_PEER, E_VPN) else (s, t, label)
            if s not in keys or t not in keys or s == t or k in seen:
                continue
            seen.add(k)
            cells.append(f'<mxCell id="e{i}" value="{escape(label, {chr(34): "&quot;"})}" '
                         f'style="{st}edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;fontSize=10;labelBackgroundColor=#FFFFFF;" '
                         f'edge="1" parent="1" source="{cid(s)}" target="{cid(t)}">'
                         f'<mxGeometry relative="1" as="geometry"/></mxCell>')
        W, H = self.root.w + 40, self.root.h + 40
        self.size, self.keys = (W, H), keys
        return (f'<mxfile host="app.diagrams.net"><diagram id="{cid(self.name)}" name="{escape(self.name)}">'
                f'<mxGraphModel dx="1600" dy="900" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" '
                f'fold="1" page="1" pageScale="1" pageWidth="{W:.0f}" pageHeight="{H:.0f}" math="0" shadow="0"><root>'
                f'<mxCell id="0"/><mxCell id="1" parent="0"/>' + "".join(cells) + "</root></mxGraphModel></diagram></mxfile>\n")

    def preview_svg(self):
        """Rough SVG for a local look (straight edges); the real export is done by draw.io."""
        W, H = self.size
        col = lambda st, k, d: (re.search(k + r"=([^;]+)", st) or [None, d])[1]
        out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" font-family="Helvetica, Arial">'
               f'<rect width="{W:.0f}" height="{H:.0f}" fill="white"/>']
        for b, _ in self.root.walk():
            st = b.style["style"]
            if b.kind != "text" and col(st, "fillColor", "none") != "none":
                out.append(f'<rect x="{b.x:.0f}" y="{b.y:.0f}" width="{b.w:.0f}" height="{b.h:.0f}" rx="5" '
                           f'fill="{col(st, "fillColor", "#fff")}" stroke="{col(st, "strokeColor", "#000")}" '
                           f'stroke-width="{col(st, "strokeWidth", "1")}"' + (' stroke-dasharray="6 4"' if "dashed=1" in st else "") + "/>")
            lines = []
            for ln in plain_lines(b.label):
                maxc = max(1, int((b.w - 12) / (b.fs * 0.6)))
                while len(ln) > maxc:
                    lines.append(ln[:maxc]); ln = ln[maxc:]
                lines.append(ln)
            centered = b.kind == "card"
            lh = b.fs * 1.25
            y0 = b.y + (b.h - lh * len(lines)) / 2 + b.fs if centered else b.y + b.fs + 4
            for j, ln in enumerate(lines if b.label else []):
                x = b.x + b.w / 2 if centered else b.x + 8
                out.append(f'<text x="{x:.0f}" y="{y0 + j * lh:.0f}" font-size="{b.fs}" '
                           f'text-anchor="{"middle" if centered else "start"}">{escape(ln)}</text>')
        for s, t, st, label in self.edges:
            if s in self.keys and t in self.keys and s != t:
                a, b = self.keys[s], self.keys[t]
                out.append(f'<line x1="{a.x + a.w / 2:.0f}" y1="{a.y + a.h / 2:.0f}" x2="{b.x + b.w / 2:.0f}" '
                           f'y2="{b.y + b.h / 2:.0f}" stroke="{col(st, "strokeColor", "#000")}" stroke-width="1.5" opacity="0.6"/>')
        out.append("</svg>")
        return "\n".join(out)


# ------------------------------------------------------------------ model
class Model:
    def __init__(self, snap, changes):
        self.snap, self.changes = snap, changes
        self.status = {x["id"]: k for k in ("added", "changed", "removed") for x in changes.get(k, [])}
        self.res = {r["id"]: r for r in snap.get("resources", [])}
        self.roles = list(snap.get("roles", []))
        for x in changes.get("removed", []):  # removed things are drawn once, red dashed
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

    def owner(self, some_id):
        s = (some_id or "").lower()
        while s.count("/") > 4:
            if s in self.res:
                return s
            s = s.rsplit("/", 2)[0] if s.count("/") > 8 else s.rsplit("/", 1)[0]
        return None

    def strings(self, rid):
        found = []

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if isinstance(k, str) and k.lower().startswith("/subscriptions/"):
                        found.append(k.lower())
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
            elif isinstance(o, str) and o.lower().startswith("/subscriptions/"):
                found.append(o.lower())
        r = self.res[rid]
        walk(r.get("properties"))
        walk(r.get("identity"))
        return found

    def refs(self, rid):
        if rid not in self._refs:
            self._refs[rid] = {t for t in map(self.owner, self.strings(rid)) if t and t != rid}
        return self._refs[rid]

    def of_type(self, *types):
        return sorted((r for r in self.res.values() if r["type"] in types), key=lambda r: r["name"])

    def label(self, r, *extra):
        lines = []
        if self.st(r["id"]):
            lines.append(STATUS_TAG[self.st(r["id"])])
        lines += [f"<b>{h(r['name'])}</b>", f"<font color='#555555'>{h(short(r['type']))}</font>"]
        lines += [x for x in extra if x]
        return "<br>".join(lines)


def nsg_open_rules(nsg):
    out = []
    for rule in P(nsg).get("securityRules", []):
        p = rule.get("properties", rule)
        if p.get("direction") != "Inbound" or p.get("access") != "Allow":
            continue
        srcs = [s for s in [p.get("sourceAddressPrefix")] + list(p.get("sourceAddressPrefixes") or []) if s]
        if not any(s.lower() in OPEN_SOURCES for s in srcs):
            continue
        ports = ",".join(x for x in [p.get("destinationPortRange")] + list(p.get("destinationPortRanges") or []) if x) or "*"
        out.append(f"{ports} from {srcs[0]}")
    return out


def exposure(r):
    p = P(r)
    pna = str(p.get("publicNetworkAccess") or "").lower()
    if pna == "disabled" or str((p.get("network") or {}).get("publicNetworkAccess", "")).lower() == "disabled":
        return None
    acls = p.get("networkAcls") or p.get("networkRuleSet") or {}
    if str(acls.get("defaultAction", "")).lower() == "deny":
        return "filtered"
    if pna == "enabled" or acls:
        return "open"
    return None


def receivers(ag):
    p, parts = P(ag), []
    for key, name in (("emailReceivers", "e-mail"), ("smsReceivers", "SMS"), ("webhookReceivers", "webhook"),
                      ("logicAppReceivers", "Logic App"), ("azureFunctionReceivers", "Function"),
                      ("armRoleReceivers", "ARM role"), ("eventHubReceivers", "Event Hub"),
                      ("automationRunbookReceivers", "runbook"), ("voiceReceivers", "voice")):
        if p.get(key):
            parts.append(f"{name} ×{len(p[key])}")
    return ", ".join(parts) or "no receivers"


# ------------------------------------------------------------------ infra page (per subscription)
def infra_page(m, sub_id, sub_name, date, summary):
    page = Page(f"Infra {sub_name}",
                f"<b style='font-size:20px'>Azure infrastructure — {h(sub_name)}</b><br>"
                f"Snapshot {date} · changes: {h(summary)}<br>"
                "<font color='#1A7F37'>■ added</font>  <font color='#BF8700'>■ changed</font>  "
                "<font color='#CF222E'>┅ removed · ■ risk (open to Internet / broad role)</font>  "
                "<font color='#C9A227'>→ Internet traffic</font>  <font color='#9673A6'>┅ private link</font>  "
                "<font color='#2E75B6'>━ peering</font>")
    res = {rid: r for rid, r in m.res.items() if r["subscriptionId"] == sub_id}
    top = page.root.add(cont(f"top:{sub_id}", "", "plain", "h"))
    top.add(card("internet", "<b>Internet</b>", "External", w=160))
    body = page.root.add(cont(f"body:{sub_id}", "", "plain", "h"))
    vcol = body.add(cont(f"vcol:{sub_id}", "", "plain", "v"))
    scol = body.add(cont(f"scol:{sub_id}", "", "plain", "grid", cols=2))

    # who sits in which subnet; public IPs; NSGs; NAT; route tables
    place, pip_of, extra = {}, collections.defaultdict(list), collections.defaultdict(list)
    merged = set()
    for nic in m.of_type("microsoft.network/networkinterfaces"):
        np_ = P(nic)
        if np_.get("privateEndpoint"):
            continue
        holder = m.owner((np_.get("virtualMachine") or {}).get("id")) or nic["id"]
        for ipc in np_.get("ipConfigurations", []):
            ip = ipc.get("properties", {})
            sid = (ip.get("subnet") or {}).get("id", "").lower()
            if sid:
                place.setdefault(holder, sid)
            pid = (ip.get("publicIPAddress") or {}).get("id", "").lower()
            if pid:
                pip_of[holder].append(pid)
                merged.add(pid)
            if ip.get("privateIPAddress"):
                extra[holder].append(f"private {h(ip['privateIPAddress'])}")
        nsg = m.owner((np_.get("networkSecurityGroup") or {}).get("id"))
        if nsg:
            rules = nsg_open_rules(m.res[nsg])
            extra[holder].append(f"NSG {h(m.res[nsg]['name'])}" + "".join(f"<br><font color='#CF222E'>⚠ open {h(x)}</font>" for x in rules))
            merged.add(nsg)
            if rules:
                extra[holder].append("__risk__")
    for pip in m.of_type("microsoft.network/publicipaddresses"):
        o = m.owner((P(pip).get("ipConfiguration") or {}).get("id"))
        if o and o != pip["id"] and m.res[o]["type"] != "microsoft.network/networkinterfaces":
            pip_of[o].append(pip["id"])
            merged.add(pip["id"])
    for ng in m.of_type("microsoft.network/natgateways"):
        for p_ in P(ng).get("publicIpAddresses", []):
            pid = m.owner(p_.get("id"))
            if pid:
                pip_of[ng["id"]].append(pid)
                merged.add(pid)
    aks_node = {(P(a).get("nodeResourceGroup") or "").lower(): a["id"]
                for a in m.of_type("microsoft.containerservice/managedclusters")}
    node_count = collections.Counter()
    for rid, r in res.items():
        owner_aks = aks_node.get(r["resourceGroup"])
        if owner_aks:
            merged.add(rid)
            node_count[owner_aks] += 1
            if r["type"] == "microsoft.network/publicipaddresses" and rid not in pip_of[owner_aks]:
                pip_of[owner_aks].append(rid)
    for a, n in node_count.items():
        extra[a].append(f"node RG: {n} AKS-managed resources")
    for rid, r in res.items():
        if rid in place or rid in merged or r["type"].startswith(HIDE) or r["type"] == "microsoft.network/networkinterfaces":
            continue
        for s in m.strings(rid):
            mt = SUBNET_RE.match(s)
            if mt and r["type"] not in ("microsoft.network/networksecuritygroups", "microsoft.network/natgateways",
                                        "microsoft.network/routetables"):
                place[rid] = mt.group(1).lower()
                break

    pe_in = collections.Counter()
    for rid in [x for x in place if m.res[x]["type"] == "microsoft.network/privateendpoints"]:
        pe_in[place.pop(rid)] += 1
        merged.add(rid)
        p_ = P(m.res[rid])
        for c in p_.get("privateLinkServiceConnections", []) + p_.get("manualPrivateLinkServiceConnections", []):
            tgt = m.owner(c.get("properties", {}).get("privateLinkServiceId"))
            if tgt:
                extra[tgt].append("private endpoint ✓")

    subnet_title = {}
    for v in m.of_type("microsoft.network/virtualnetworks"):
        for s in P(v).get("subnets", []):
            sp, lines = s.get("properties", {}), []
            risk = False
            nsg = m.owner((sp.get("networkSecurityGroup") or {}).get("id"))
            if nsg:
                rules = nsg_open_rules(m.res[nsg])
                lines.append(f"NSG {h(m.res[nsg]['name'])}" + (f" {STATUS_TAG[m.st(nsg)]}" if m.st(nsg) else ""))
                lines += [f"<font color='#CF222E'>⚠ open {h(x)}</font>" for x in rules]
                risk = bool(rules)
                merged.add(nsg)
            for key, word in (("natGateway", "NAT"), ("routeTable", "routes")):
                o = m.owner((sp.get(key) or {}).get("id"))
                if o:
                    ips = [P(m.res[x]).get("ipAddress", "") for x in pip_of.get(o, []) if x in m.res]
                    lines.append(f"{word} {h(m.res[o]['name'])}" + (f" (out {h(', '.join(ips))})" if ips else ""))
                    merged.add(o)
            if pe_in[s["id"].lower()]:
                lines.append(f"{pe_in[s['id'].lower()]} private endpoints")
            subnet_title[s["id"].lower()] = (lines, risk)

    # VNets -> subnets -> resources
    for v in m.of_type("microsoft.network/virtualnetworks"):
        if v["subscriptionId"] != sub_id:
            continue
        space = ", ".join(P(v).get("addressSpace", {}).get("addressPrefixes", []))
        tag = f" {STATUS_TAG[m.st(v['id'])]}" if m.st(v["id"]) else ""
        vbox = vcol.add(cont(v["id"], f"<b>VNet {h(v['name'])}</b> · {h(space)} · RG {h(v['resourceGroup'])}{tag}",
                             "vnet", "grid", cols=2, status=m.st(v["id"])))
        for s in P(v).get("subnets", []):
            sid = s["id"].lower()
            sp = s.get("properties", {})
            prefix = sp.get("addressPrefix") or ", ".join(sp.get("addressPrefixes", []))
            lines, risk = subnet_title.get(sid, ([], False))
            label = "<br>".join([f"<b>{h(s['name'])}</b> · {h(prefix)}"] + lines)
            members = sorted((rid for rid, sn in place.items() if sn == sid and rid in res), key=lambda x: res[x]["name"])
            sbox = vbox.add(cont(sid, label, "subnet", "grid", cols=2 if len(members) > 3 else 1,
                                 status="risk" if risk else None, w=CARD_W + 2 * PAD))
            if risk:
                page.edge("internet", sid, E_RISK, "inbound allowed")
            add_cards(m, page, sbox, members, pip_of, extra)
        for pe in P(v).get("virtualNetworkPeerings", []):
            remote = ((pe.get("properties") or {}).get("remoteVirtualNetwork") or {}).get("id", "").lower()
            if remote:
                page.edge(v["id"], remote, E_PEER, f"peering {(pe.get('properties') or {}).get('peeringState', '')}")

    # Services that are not inside a VNet, grouped by category
    groups = collections.defaultdict(list)
    mon_summary = collections.Counter()
    mon_status = set()
    for rid, r in sorted(res.items(), key=lambda x: x[1]["name"]):
        t = r["type"]
        if rid in place or rid in merged or t.startswith(HIDE):
            continue
        if t.startswith(MON_SUMMARY):
            mon_summary[short(t)] += 1
            if m.st(rid):
                mon_status.add(m.st(rid))
            continue
        groups[category(t)].append(rid)
    order = ["Data", "Security", "AI", "Compute", "Network", "Monitoring", "Other"]
    for cat in order:
        if not groups.get(cat) and not (cat == "Monitoring" and mon_summary):
            continue
        n = len(groups.get(cat, []))
        g = scol.add(cont(f"grp:{sub_id}:{cat}", f"<b>{cat}</b>", "group", "grid", cols=2 if n > 5 else 1))
        add_cards(m, page, g, groups.get(cat, []), pip_of, extra)
        if cat == "Monitoring" and mon_summary:
            st = "changed" if mon_status else None
            g.add(card(f"monsum:{sub_id}", (STATUS_TAG["changed"] + "<br>" if st else "") +
                       "<b>Alerts &amp; collection</b><br>" + "<br>".join(f"{n} {h(k)}" for k, n in sorted(mon_summary.items())) +
                       "<br><i>details: monitoring diagram</i>", "Monitoring", st))

    # anything an edge points to that is not on this page -> stub in "Outside this subscription"
    present = {b.key for b, _ in page.root.walk()}
    stubs = [t for s, t, _, _ in page.edges if t not in present] + [s for s, t, _, _ in page.edges if s not in present]
    if stubs:
        ext = top.add(cont(f"ext:{sub_id}", "<b>Outside this subscription</b>", "ext", "h"))
        for k in dict.fromkeys(stubs):
            r = m.res.get(k)
            name = r["name"] if r else k.rstrip("/").rsplit("/", 1)[-1]
            sub = m.subs.get(r["subscriptionId"], "") if r else ""
            ext.add(card(k, f"<b>{h(name)}</b><br>{h(short(r['type']) if r else 'resource')}" + (f"<br>{h(sub)}" if sub else ""),
                         "External"))
    return page


def add_cards(m, page, box, rids, pip_of, extra, limit=3):
    """Changed / risky / connected resources get their own card; >limit plain ones of a type share one list card."""
    by_type = collections.defaultdict(list)
    for rid in rids:
        by_type[m.res[rid]["type"]].append(rid)
    for t, ids in sorted(by_type.items()):
        plain = []
        for rid in ids:
            n_edges = len(page.edges)
            c = resource_card(m, page, rid, pip_of, extra)
            if c.flag or len(page.edges) != n_edges:
                box.add(c)
            else:
                plain.append((rid, c))
        if len(plain) <= limit:
            for _, c in plain:
                box.add(c)
            continue
        names = sorted(m.res[rid]["name"] for rid, _ in plain)
        notes = []
        pe = sum("private endpoint ✓" in extra.get(rid, []) for rid, _ in plain)
        if pe:
            notes.append(f"private endpoint: {pe} of {len(plain)}")
        if t in PAAS and all(exposure(m.res[rid]) is None for rid, _ in plain):
            notes.append("all: no public access")
        label = (f"<b>{h(short(t))} ×{len(plain)}</b><br>" + "<br>".join(f"<i>{h(n)}</i>" for n in notes) +
                 ("<br>" if notes else "") + "<br>".join(h(n) for n in names[:14]) +
                 (f"<br>… +{len(names) - 14} more" if len(names) > 14 else ""))
        box.add(card(f"list:{box.key}:{t}", label, category(t)))
    n = len(box.children)
    box.cols = 2 if n > 3 else 1


def resource_card(m, page, rid, pip_of, extra):
    r = m.res[rid]
    p, t = P(r), r["type"]
    lines, status = [], m.st(rid)
    risk = "__risk__" in extra.get(rid, [])
    lines += [x for x in extra.get(rid, []) if x != "__risk__"]
    for pid in pip_of.get(rid, []):
        ip = P(m.res[pid]).get("ipAddress", "not allocated") if pid in m.res else "?"
        lines.append(f"<font color='#8A6D00'>public IP {h(ip)}</font>")
        if t != "microsoft.network/natgateways":
            page.edge("internet", rid, E_WEB, ip)
    if t == "microsoft.containerservice/managedclusters":
        lines.append(f"k8s {h(p.get('kubernetesVersion', ''))} · {h((r.get('sku') or {}).get('tier', ''))} · "
                     f"{h((p.get('powerState') or {}).get('code', ''))}")
        pools = p.get("agentPoolProfiles", [])
        lines.append(f"{len(pools)} node pools: " + ", ".join(f"{h(x.get('name'))}×{x.get('count', '?')}" for x in pools)[:110])
        api = p.get("apiServerAccessProfile") or {}
        if not api.get("enablePrivateCluster") and not api.get("authorizedIPRanges"):
            lines.append("<font color='#CF222E'>⚠ API server open to Internet</font>")
            risk = True
            page.edge("internet", rid, E_RISK, "API server")
    elif t == "microsoft.compute/virtualmachines":
        lines.append(h(((p.get("hardwareProfile") or {}).get("vmSize", ""))))
    elif t in ("microsoft.dbforpostgresql/flexibleservers", "microsoft.dbformysql/flexibleservers"):
        lines.append(f"{h(p.get('version', ''))} · {h((r.get('sku') or {}).get('name', ''))} · {h(p.get('state', ''))}")
    elif t == "microsoft.operationalinsights/workspaces":
        cap = (p.get("workspaceCapping") or {}).get("dailyQuotaGb", -1)
        lines.append(f"{p.get('retentionInDays', '?')} days" + (f" · cap {cap} GB/day" if cap not in (-1, None) else ""))
    elif t == "microsoft.network/privateendpoints":
        for c in p.get("privateLinkServiceConnections", []) + p.get("manualPrivateLinkServiceConnections", []):
            tgt = (c.get("properties", {}).get("privateLinkServiceId") or "").lower()
            if tgt:
                page.edge(rid, m.owner(tgt) or tgt, E_LINK, "private link")
    elif t == "microsoft.network/connections":
        lines.append(f"{h(p.get('connectionType', ''))} · {h(p.get('connectionStatus', ''))}")
        for k in ("virtualNetworkGateway1", "localNetworkGateway2", "virtualNetworkGateway2"):
            o = (p.get(k) or {}).get("id")
            if o:
                page.edge(rid, m.owner(o) or o.lower(), E_VPN)
    elif t == "microsoft.network/localnetworkgateways":
        lines.append(f"peer {h(p.get('gatewayIpAddress', ''))}")
        lines.append(h(", ".join((p.get("localNetworkAddressSpace") or {}).get("addressPrefixes", []))))
    elif t == "microsoft.network/networksecuritygroups":
        lines.append("<i>not attached</i>")
        lines += [f"<font color='#CF222E'>⚠ open {h(x)}</font>" for x in nsg_open_rules(r)]
    elif t == "microsoft.network/publicipaddresses":
        lines.append(f"{h(p.get('ipAddress', 'not allocated'))} · <i>not attached</i>")
    if t in PAAS:
        e = exposure(r)
        if e == "open":
            lines.append("<font color='#CF222E'>⚠ public endpoint, no IP filter</font>")
            risk = True
            page.edge("internet", rid, E_RISK, "public")
        elif e == "filtered":
            lines.append("public endpoint · IP filter")
        else:
            lines.append("no public access")
    c = card(rid, m.label(r, *lines), category(t), status or ("risk" if risk else None))
    c.flag = bool(status or risk)
    return c


# ------------------------------------------------------------------ overview (only for >1 subscription)
def overview_page(m, date, summary):
    page = Page("Overview", f"<b style='font-size:20px'>Azure overview</b><br>Snapshot {date} · changes: {h(summary)}")
    grid = page.root.add(cont("ov:grid", "", "plain", "grid", cols=3))
    cats = collections.defaultdict(collections.Counter)
    changed = collections.defaultdict(set)
    for r in m.res.values():
        if r["type"].startswith(HIDE):
            continue
        cats[r["subscriptionId"]][category(r["type"])] += 1
        if m.st(r["id"]):
            changed[r["subscriptionId"]].add(m.st(r["id"]))
    for sub_id, name in sorted(m.subs.items(), key=lambda x: x[1]):
        if not cats[sub_id]:
            continue
        box = grid.add(cont("ov:" + sub_id, f"<b>{h(name)}</b>", "sub", "grid", cols=2,
                            status="changed" if changed[sub_id] else None))
        for cat, n in sorted(cats[sub_id].items()):
            box.add(card(f"ov:{sub_id}:{cat}", f"<b>{cat}</b><br>{n} resources", cat, w=130))
    for v in m.of_type("microsoft.network/virtualnetworks"):
        for pe in P(v).get("virtualNetworkPeerings", []):
            remote = m.owner(((pe.get("properties") or {}).get("remoteVirtualNetwork") or {}).get("id"))
            if remote and m.res[remote]["subscriptionId"] != v["subscriptionId"]:
                a, b = sorted([v["subscriptionId"], m.res[remote]["subscriptionId"]])
                page.edge("ov:" + a, "ov:" + b, E_PEER, "peering")
    return page


# ------------------------------------------------------------------ monitoring
def monitoring_page(m, date):
    page = Page("Monitoring", f"<b style='font-size:20px'>Monitoring &amp; alerts</b><br>Snapshot {date}. "
                              "Top: agent data → collection rule → store. Bottom: alert rule → action group → receiver. "
                              "Diagnostic settings are not visible in Resource Graph.")
    flow1 = page.root.add(cont("mon:flow1", "", "plain", "h"))
    flow2 = page.root.add(cont("mon:flow2", "", "plain", "h"))
    src = flow1.add(cont("mon:src", "<b>Sources (agents)</b>", "group"))
    dcr = flow1.add(cont("mon:dcr", "<b>Data collection rules</b>", "group"))
    store = flow1.add(cont("mon:store", "<b>Stores</b>", "group"))
    alerts = flow2.add(cont("mon:alerts", "<b>Alert rules</b>", "group", "grid", cols=2))
    ags = flow2.add(cont("mon:ags", "<b>Action groups</b>", "group"))
    recv = flow2.add(cont("mon:recv", "<b>Receivers (Logic Apps)</b>", "group"))

    def mcard(rid, *extra):
        return card(rid, m.label(m.res[rid], *extra), category(m.res[rid]["type"]), m.st(rid))

    for r in m.of_type("microsoft.operationalinsights/workspaces", "microsoft.monitor/accounts",
                       "microsoft.insights/components"):
        p = P(r)
        cap = (p.get("workspaceCapping") or {}).get("dailyQuotaGb", -1)
        info = f"{p.get('retentionInDays')} days" if p.get("retentionInDays") else ""
        info += f" · cap {cap} GB/day" if cap not in (-1, None) else ""
        store.add(mcard(r["id"], h(m.subs.get(r["subscriptionId"], "")), info))
    sources = set()
    for r in m.of_type("microsoft.insights/datacollectionrules"):
        streams = sorted({s.replace("Microsoft-", "") for f in P(r).get("dataFlows", []) for s in f.get("streams", [])})
        dcr.add(mcard(r["id"], h(", ".join(streams))[:150]))
        for t in m.refs(r["id"]):
            if m.res[t]["type"] in ("microsoft.operationalinsights/workspaces", "microsoft.monitor/accounts"):
                page.edge(r["id"], t, E_FLOW, "sends to")
    for a in m.snap.get("dcrAssociations", []):
        s, rule = m.owner(a["id"].split("/providers/microsoft.insights/")[0]), m.owner(a.get("rule"))
        if s and rule:
            if s not in sources:
                sources.add(s)
                src.add(mcard(s))
            page.edge(s, rule, E_FLOW)
    if not src.children:
        src.add(card("mon:nosrc", "<i>no agent associations</i>", "Other"))
    for r in m.of_type("microsoft.insights/metricalerts", "microsoft.insights/scheduledqueryrules",
                       "microsoft.insights/activitylogalerts", "microsoft.alertsmanagement/prometheusrulegroups"):
        p = P(r)
        targets = [m.res[t]["name"] for t in m.refs(r["id"]) if m.res[t]["type"] != "microsoft.insights/actiongroups"]
        info = (f"sev {p['severity']}" if "severity" in p else "") + ("" if p.get("enabled", True) else " · <b>DISABLED</b>")
        alerts.add(mcard(r["id"], info, ("on " + h(", ".join(targets[:2]))) if targets else ""))
        for t in m.refs(r["id"]):
            if m.res[t]["type"] == "microsoft.insights/actiongroups":
                page.edge(r["id"], t, E_FLOW)
    alerts.cols = 2 if len(alerts.children) > 6 else 1
    for r in m.of_type("microsoft.insights/actiongroups"):
        ags.add(mcard(r["id"], receivers(r)))
        for t in m.refs(r["id"]):
            if t not in {b.key for b in recv.children}:
                recv.add(mcard(t))
            page.edge(r["id"], t, E_FLOW, "calls")
    for b in (dcr, store, alerts, ags, recv):
        if not b.children:
            b.add(card(b.key + ":none", "<i>none</i>", "Other"))
    return page


# ------------------------------------------------------------------ access
def access_page(m, date):
    page = Page("Access", f"<b style='font-size:20px'>Access (role assignments)</b><br>Snapshot {date}. "
                          "<font color='#CF222E'>⚠ = Owner / Contributor / User Access Administrator</font>")
    names = {}
    for r in m.res.values():
        pid = (r.get("identity") or {}).get("principalId")
        if pid:
            names[pid] = (f"{r['name']}", "system identity")
        if r["type"] == "microsoft.managedidentity/userassignedidentities":
            names[P(r).get("principalId")] = (r["name"], "managed identity")

    def scope_label(s):
        if s.startswith("/providers/microsoft.management/managementgroups/"):
            return "mgmt group " + s.rsplit("/", 1)[-1]
        parts = s.split("/")
        if len(parts) == 3:
            return "subscription " + m.subs.get(parts[2], parts[2][:8])
        if len(parts) == 5:
            return "RG " + parts[4]
        o = m.owner(s)
        return m.res[o]["name"] if o else s.rsplit("/", 1)[-1]

    by_p = collections.defaultdict(list)
    for a in m.roles:
        by_p[a["principalId"]].append(a)
    groups = collections.defaultdict(list)
    for pid, assigns in by_p.items():
        nm, kind = names.get(pid, (f"{pid[:8]}…", assigns[0].get("principalType") or "Unknown"))
        groups[kind].append((nm, pid, assigns))
    grid = page.root.add(cont("acc:grid", "", "plain", "h"))
    for kind in sorted(groups):
        items = sorted(groups[kind])
        g = grid.add(cont("acc:" + kind, f"<b>{h(kind)}</b> ({len(items)})", "group", "grid",
                          cols=max(1, min(3, math.ceil(len(items) / 8)))))
        for nm, pid, assigns in items:
            lines, sts, risky = [f"<b>{h(nm)}</b>"], set(), False
            for a in sorted(assigns, key=lambda a: a["roleName"]):
                bad = a["roleName"].lower() in RISKY_ROLES
                risky |= bad
                tag = {"added": " (NEW)", "removed": " (REMOVED)", "changed": " (CHANGED)"}.get(m.st(a["id"]), "")
                if m.st(a["id"]):
                    sts.add(m.st(a["id"]))
                line = f"{'⚠ ' if bad else ''}{h(a['roleName'])} → {h(scope_label(a['scope']))}{tag}"
                lines.append(f"<font color='#CF222E'>{line}</font>" if bad else line)
            status = sorted(sts)[0] if sts else ("risk" if risky else None)
            g.add(card("acc:" + pid, "<br>".join(lines), "Security" if risky else "Other", status, w=280))
    return page


# ------------------------------------------------------------------ changes
def changes_page(m, date):
    ch = m.changes
    page = Page("Changes", f"<b style='font-size:20px'>Changes</b><br>" +
                (f"{ch['from']} → {ch['to']}" if ch.get("from") else f"First snapshot {date} — baseline"))
    row = page.root.add(cont("chg:row", "", "plain", "h"))
    if not ch.get("from") or not (ch["added"] or ch["changed"] or ch["removed"]):
        row.add(card("chg:none", "Nothing to compare yet" if not ch.get("from") else "No changes", "Other", w=300))
        return page
    for kind, title in (("added", "Added"), ("changed", "Changed"), ("removed", "Removed")):
        if not ch[kind]:
            continue
        col = row.add(cont("chg:" + kind, f"<b>{title}</b> ({len(ch[kind])})", "group", "grid",
                           cols=max(1, min(3, math.ceil(len(ch[kind]) / 10)))))
        for x in ch[kind]:
            lines = [STATUS_TAG[kind], f"<b>{h(x.get('name'))}</b>", f"<font color='#555555'>{h(short(x.get('type')))}</font>"]
            if x.get("resourceGroup"):
                lines.append("RG " + h(x["resourceGroup"]))
            lines += [h(f) for f in x.get("fields", [])]
            if x.get("moreFields"):
                lines.append(f"… +{x['moreFields']} more")
            lines.append("by " + h(x.get("by") or "unknown"))
            col.add(card("chg:" + x["id"], "<br>".join(lines), category(x.get("type") or ""), kind, w=280))
    return page


# ------------------------------------------------------------------ main
def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "sub"


def main():
    snaps = sorted((ROOT / "snapshots").glob("*.json"))
    snap = json.loads(snaps[-1].read_text())
    date = snaps[-1].stem
    ch_p = ROOT / "changes" / f"{date}.json"
    ch = json.loads(ch_p.read_text()) if ch_p.exists() else {"to": date, "from": None,
                                                             "added": [], "changed": [], "removed": []}
    m = Model(snap, ch)
    summary = (f"{len(ch['added'])} added, {len(ch['changed'])} changed, {len(ch['removed'])} removed since {ch['from']}"
               if ch.get("from") else "first snapshot (baseline)")

    pages = []
    subs_with_res = [s for s in sorted(m.subs, key=lambda s: m.subs[s]) if any(r["subscriptionId"] == s for r in m.res.values())]
    if len(subs_with_res) > 1:
        pages.append(("1-overview", overview_page(m, date, summary)))
    for s in subs_with_res:
        pages.append((f"2-infra-{slug(m.subs[s])}", infra_page(m, s, m.subs[s], date, summary)))
    pages.append(("3-monitoring", monitoring_page(m, date)))
    pages.append(("4-access", access_page(m, date)))
    pages.append(("5-changes", changes_page(m, date)))

    OUT.mkdir(exist_ok=True)
    BUILD.mkdir(exist_ok=True)
    for old in [p for d in (OUT, BUILD) for p in d.iterdir() if p.is_file()]:
        old.unlink()
    preview = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else None
    index = [f"# Azure diagrams — {date}", "", f"Changes: {summary} — details: [changes/{date}.md](../changes/{date}.md)", ""]
    for fname, page in pages:
        (BUILD / f"{fname}.drawio").write_text(page.build())
        if preview:
            preview.mkdir(parents=True, exist_ok=True)
            (preview / f"{fname}.svg").write_text(page.preview_svg())
        index += [f"## {page.name}", "", f"![{page.name}]({fname}.png)", ""]
    (OUT / "README.md").write_text("\n".join(index) + "\n")
    print(f"## Azure diagrams {date}\n\n{summary}\n")
    for kind in ("added", "changed", "removed"):
        for x in ch.get(kind, []):
            print(f"- {kind}: {x.get('name')} ({short(x.get('type'))}) by {x.get('by') or 'unknown'}")


if __name__ == "__main__":
    main()
