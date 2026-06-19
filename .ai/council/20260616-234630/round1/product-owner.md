# Council Opinion: Product Owner

## Summary

The OpenCode Gateway solves a genuine, well-defined orchestration gap that emerges the moment you try to run OpenCode headlessly in automation pipelines, and the existing implementation demonstrates disciplined scope control and a clear understanding of who benefits — but I have concerns about over-abstracted interfaces that signal partial MVP scope creep, a missing Paperclip integration story that weakens the value proposition for the primary customer, and a lack of quantifiable success metrics against which to validate the investment.

## Assessment

**Problem validity — high confidence.** The problem is real. The brief asks "could existing tools (AWX + OpenCode directly) achieve the same outcomes with less complexity?" — and the answer is no, not without rebuilding this exact state machine. Raw AWX playbooks calling OpenCode would require each integration to reinvent runner health checking, pre-flight policy evaluation, port allocation coordination, diff retrieval routing, job state tracking, workspace cleanup scheduling, and approval gating. Those are not one-liners; they are systems in their own right. The Gateway consolidates them into a single API surface with consistent semantics. That is the right call.

**User segmentation is well-executed.** The PRD identifies six distinct user types with 29 user stories. This is thorough without being gratuitous. The three primary beneficiaries are platform engineers (who get a stable API instead of writing fragile scripts), Paperclip/agent orchestrators (who get a clean delegation boundary), and Gateway operators (who get observability and control). The AWX admin and security auditor stories demonstrate awareness that this system doesn't exist in isolation.

**MVP scope — mostly disciplined, with two yellow flags.**

The out-of-scope list in the PRD is strong: no multi-tenancy, no Kubernetes pod orchestration, no untrusted command execution without gates, no replacing Paperclip. These boundaries show restraint.

However, I flag two items:

1. **"Future surface" on the executor interface.** The ExecutorPlugin ABC defines six methods, but only four are actively invoked. The remaining two (`restart_opencode`, `collect_state`) are annotated as "intentional future surface." In an MVP, abstract methods that nobody calls are not future-proofing — they are speculative generality. A concrete interface that grows as call sites emerge would have been more honest to the MVP ethos. The ADR 0002 refinement acknowledges this but keeps them as required ABC methods, which forces every executor implementation to provide stubs for methods that may never be called. This is over-engineering.

2. **Approval gates (#11) as MVP.** The approval gate feature introduces two new job statuses (`needs_approval`, `rejected`) and two API endpoints. For a headless automation backend, approval is a critical safety mechanism — I agree it belongs. But it also implies the Gateway is being positioned as a trust boundary, which raises the question: is the MVP a reliable execution backend, or a governance-aware orchestration layer? These are different products. If the true MVP customer is Paperclip (which handles its own governance), the approval gate may duplicate a concern that belongs above the Gateway.

**The Paperclip gap is the biggest product risk.** Issue #13 (Paperclip integration adapter) is the only planned item not yet implemented. But the PRD's user stories 16–19 explicitly describe Paperclip workflows: submitting jobs from agent workflows, receiving completion callbacks, querying for associated jobs. Without this adapter, the primary integration story is incomplete. The Gateway is a general-purpose API, but its *raison d'être* as part of the larger OpenCode ecosystem is enabling Paperclip. Leaving this adapter for "later" means the system cannot be validated end-to-end with its intended primary consumer. I would argue this should have been an earlier priority, not a trailing item.

**No success metrics defined anywhere.** The PRD, README, and ADRs describe *what* the system does but never *how we know it works well enough.* Key missing measures:
- Target job completion rate (e.g., >95% of submitted jobs complete without operator intervention)
- Acceptable latency for the end-to-end flow (submission → diff retrieval)
- Maximum concurrent jobs the MVP should handle
- Maximum acceptable observation data loss or staleness
- Target time to detect and respond to runner degradation

Without these, "MVP complete" is a feature checklist, not a product decision. The Council cannot validate whether scope was appropriate without success criteria.

**The port range (10000–10999, 1000 ports) deserves a brief challenge.** At MVP scale this is fine. But what happens when a single Runner VM hosts 1000 workspaces? Each OpenCode Serve instance consumes memory, CPU, and disk. The real constraint is not port availability — it's resource exhaustion on the VM. If the port range implies the system is designed to allow 1000 concurrent sessions, the Gateway should have a concurrent-job policy check that prevents overloading the VM before it hits the port ceiling. ADR 0003 acknowledges the range is "sufficient" but doesn't articulate the resource-boundary relationship.

## Key Concerns

- **Over-abstracted executor interface for MVP.** Two of six abstract methods are "future surface" that no one calls. This is speculative generality that adds implementation burden for every new executor type without delivering user value today.
- **Paperclip integration is the missing keystone.** Issue #13 is deferred, but without it the primary customer story (agent orchestration delegating to Gateway) cannot be validated end-to-end. This should have been prioritized earlier or reframed as the actual MVP scope.
- **No measurable success criteria.** The project has 29 user stories, 650+ tests, 4 ADRs — and zero quantifiable targets for reliability, throughput, latency, or operational health. Without metrics, "MVP complete" is arbitrary.
- **Approval gates may be a layer violation.** If Paperclip handles governance (as the PRD suggests), the Gateway duplicating approval logic introduces ambiguity about which layer owns trust decisions. This needs clear resolution before it creates integration friction.

## Recommendations

1. **Define and publish success metrics before declaring production readiness.** At minimum: target job completion rate (e.g., 95%), acceptable P95 end-to-end latency, maximum concurrent jobs, and maximum observation staleness. Use these to gate any "production-ready" milestone.

2. **Schedule Paperclip integration (#13) as the next priority, not a trailing item.** The Gateway's value is clearest when demonstrated end-to-end with its primary consumer. Deliver a working integration that covers stories 16–19 before investing in additional features.

3. **Remove the "future surface" abstract methods from the ExecutorPlugin ABC.** Move `restart_opencode` and `collect_state` to a subclass or an optional protocol. Every executor type implementing stubs for uncalled methods is a smell. Add them back when a call site exists.

4. **Clarify the approval gate boundary in writing.** Document whether the Gateway's approval feature is meant to be used independently (as a standalone trust boundary) or exclusively in coordination with Paperclip's governance. If the latter, consider deferring approval gates to Paperclip and keeping the Gateway stateless with respect to authorization decisions.

5. **Add a concurrent-job capacity check to the pre-flight policy.** The port range (1000) implies capacity, but the real bottleneck is VM resources. The policy engine should check active workspace count per runner and reject jobs when the runner is near saturation — regardless of port availability.

## Questions That Need Answers

1. What is the target job completion rate for the MVP? What P95 latency is acceptable from submission to diff retrieval?
2. Without the Paperclip adapter, how does a Paperclip agent submit a job to the Gateway today? Is it through raw HTTP calls to the `/jobs` endpoint, and if so, is that documented and tested?
3. Did the team consider making the executor interface a concrete class with optional method overrides rather than an ABC with abstract methods? If so, why was ABC chosen despite the "future surface" problem?
4. At what point does the 1000-port range become a problem — and is it a port problem or a resource-exhaustion problem? Does the Gateway have any defense against over-subscribing a single Runner VM?
5. If the approval gate feature were removed from MVP scope, would the Gateway still satisfy the core Paperclip integration stories? Or is the approval gate a prerequisite for Paperclip's trust model?
