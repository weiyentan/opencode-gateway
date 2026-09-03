# Change request owns AFK Run lifecycle

Status: accepted

An AFK Run is a change-request-owned lifecycle with one AWX Execution Binding containing many uniquely identified AWX jobs for development, review, fixes, and retries. Individual AWX job outcomes are historical child facts and never close or reopen the AFK Run; the AFK Run remains open while its canonical change request is open and becomes completed only when that change request is merged, with closure without merge represented separately. This supersedes ADR 0027's rule that a completed AWX binding closes the parent run.
