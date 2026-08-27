"""API-triggered AFK backfill: durable job queue, worker, and shared store.

The package has two halves:

* :mod:`app.backfill.jobs` — the durable ``afk_backfill_jobs`` store shared by
  the API (producer) and the worker (consumer) so the SQL never drifts.
* :mod:`app.backfill.worker` — the dedicated worker
  (``python -m app.backfill.worker``) that claims queued jobs, serializes per
  provider/repository via session advisory locks, executes the existing
  ``scripts.afk_backfill.run_backfill`` orchestration with bounded retries,
  and sweeps expired job records.

Neither module re-implements correlation or persistence semantics — those
remain owned by ``afk_outcomes`` and ``scripts/afk_backfill.py``.
"""
