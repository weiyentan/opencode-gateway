# Explicit Replay Corrections

Gateway keeps normal usage ingest first-write-wins and idempotent so duplicate collector deliveries cannot double-count usage. Historical collector replay may need to correct previously incomplete token categories, so corrections must use an explicit replay/correction path that updates stored records and session summaries safely instead of silently changing duplicate-ingest behavior.
