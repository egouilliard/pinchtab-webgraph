"""Optional offline web UI over the per-host interaction-graph cache.

STRUCTURALLY fastapi-free base install: this subpackage is imported by NOTHING in
the base package (`__init__.py` / `cli.py` never touch it) — it is reached ONLY via
its own console script `pinchtab-webgraph-ui`, so `pip install pinchtab-webgraph`
(no extras) never needs `fastapi`. The web binding lives entirely behind the `ui`
extra; the same isolation discipline `mcp_server.py` keeps for `mcp`.
"""
