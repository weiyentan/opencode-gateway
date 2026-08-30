# Project AFK run status transactionally from AWX executions

Status: accepted

An AFK Run is one logical lifecycle whose AWX Execution Bindings are attempts to realize that lifecycle. Its execution status is a transactional projection of those bindings: no bindings is `pending`; any `running` binding makes the run `running`; once all bindings are terminal, any `completed` binding makes the run `completed`, otherwise any `failed` binding makes it `failed`, otherwise it is `cancelled`. This success-aware rule preserves failed-then-successful retry history without treating an earlier failed attempt as failure of the logical lifecycle.

Failed and cancelled runs may accept a new binding with a new AWX job identity and return to `running`; completed runs reject new bindings because additional work requires a new AFK Run. Every binding mutation that can affect this projection, including direct terminal creation and identical replay, locks the parent AFK Run first and converges the parent in the same transaction. This avoids a terminal parent racing with insertion of a running child.

The projection changes only `afk_runs.status` and never consults PR/MR state or EngineeringOutcome. Historical backfill, `finished_at` derivation, AWX status observation, polling, and cancellation reconciliation are separate concerns.
