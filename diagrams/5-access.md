# Access — 2026-11-25

Role assignments: who has which role on which scope. ⚠ = Owner/Contributor/User Access Admin.

Legend: green = added · yellow = changed · red dashed = removed (shown for one run) · red solid = risk · grey = outside this diagram

```mermaid
flowchart LR
    subgraph nf8b42a8074["Who"]
        n0e1b440657("ServicePrincipal 5a0e9ca9…")
        n08446bb84f("User d10c1d23…")
    end
    subgraph n1e9ac935c6["Where"]
        n85cb07cd1f["Subscription Azure subscription 1"]
    end
    n08446bb84f -->|"⚠ Owner"| n85cb07cd1f
    n0e1b440657 -->|"Reader"| n85cb07cd1f
    classDef added fill:#dafbe1,stroke:#1a7f37,stroke-width:3px,color:#000
    classDef changed fill:#fff8c5,stroke:#9a6700,stroke-width:3px,color:#000
    classDef removed fill:#ffebe9,stroke:#cf222e,stroke-width:2px,stroke-dasharray:6 4,color:#000
    classDef risk fill:#ffebe9,stroke:#cf222e,stroke-width:3px,color:#000
    classDef ext fill:#f0f0f0,stroke:#888,color:#000
    class n08446bb84f risk
    class n85cb07cd1f ext
```
