# Network — 2026-11-25

VNets, subnets, NSGs with rules open to the Internet, public IPs, public PaaS endpoints.

Legend: green = added · yellow = changed · red dashed = removed (shown for one run) · red solid = risk · grey = outside this diagram

```mermaid
flowchart LR
    nc358151686(["Internet"])
    n83afec801f{{"<b>number1-network</b><br>networksecuritygroups"}}
    nd24de09067["<b>storage1musa</b><br>storageaccounts<br>⚠ public endpoint, no IP filter"]
    nc358151686 -.->|"public"| nd24de09067
    classDef added fill:#dafbe1,stroke:#1a7f37,stroke-width:3px,color:#000
    classDef changed fill:#fff8c5,stroke:#9a6700,stroke-width:3px,color:#000
    classDef removed fill:#ffebe9,stroke:#cf222e,stroke-width:2px,stroke-dasharray:6 4,color:#000
    classDef risk fill:#ffebe9,stroke:#cf222e,stroke-width:3px,color:#000
    classDef ext fill:#f0f0f0,stroke:#888,color:#000
    class nc358151686 ext
    class nd24de09067 risk
```
