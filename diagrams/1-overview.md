# Overview — 2026-11-25

Subscriptions and what is inside them. Arrows = one resource references another.

Legend: green = added · yellow = changed · red dashed = removed (shown for one run) · red solid = risk · grey = outside this diagram

```mermaid
flowchart LR
    nc358151686(["Internet"])
    subgraph ne6b0882471["Subscription: Azure subscription 1"]
        n48b5703e93("<b>Network</b> (1)<br>number1-network")
        n5a4573177f("<b>Data</b> (1)<br>storage1musa")
    end
    classDef added fill:#dafbe1,stroke:#1a7f37,stroke-width:3px,color:#000
    classDef changed fill:#fff8c5,stroke:#9a6700,stroke-width:3px,color:#000
    classDef removed fill:#ffebe9,stroke:#cf222e,stroke-width:2px,stroke-dasharray:6 4,color:#000
    classDef risk fill:#ffebe9,stroke:#cf222e,stroke-width:3px,color:#000
    classDef ext fill:#f0f0f0,stroke:#888,color:#000
    class nc358151686 ext
```
