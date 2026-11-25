# Environment — 2026-11-25

Resources per resource group and the links between them (monitoring hidden, see 4).

Legend: green = added · yellow = changed · red dashed = removed (shown for one run) · red solid = risk · grey = outside this diagram

```mermaid
flowchart LR
    subgraph n6521d1d30a["Subscription: Azure subscription 1"]
        subgraph n011736e01c["RG tritinain"]
            nd24de09067[("<b>storage1musa</b><br>storageaccounts")]
        end
    end
    classDef added fill:#dafbe1,stroke:#1a7f37,stroke-width:3px,color:#000
    classDef changed fill:#fff8c5,stroke:#9a6700,stroke-width:3px,color:#000
    classDef removed fill:#ffebe9,stroke:#cf222e,stroke-width:2px,stroke-dasharray:6 4,color:#000
    classDef risk fill:#ffebe9,stroke:#cf222e,stroke-width:3px,color:#000
    classDef ext fill:#f0f0f0,stroke:#888,color:#000
```
