# DAG execution and shared preparation

A node represents one scientific state at one calculation stage. An adsorption
workflow normally runs `bulk_opt -> clean_slab_opt -> adsorbate_opt` branches;
molecular references are independent. Opt/SCF/DOS are distinct stages only when
the objective requires them. Each node retains its complete worker/validator loop.

## Shared inputs

The completed clean-slab node is the preparation owner. Its final structure,
modeling summary (including site registry), and parameter summary form the common
basis. Its workers choose the basis with the requested family members in context.
There is no second preparation agent or duplicated family database.

```json
{
  "node_id": "oh_opt",
  "inputs": {
    "structure": "clean_opt/final_structure",
    "modeling_reference": "clean_opt/modeling_summary",
    "parameter_reference": "clean_opt/parameter_summary"
  }
}
```

Input references imply graph edges. `depends_on` adds only ordering/evidence
edges. `DAGNode.dependencies` is the single derived view used by scheduling,
validation, restart traversal, reporting and the dashboard. `inputs.json` contains
resolved paths for workers; bound inputs are read from the source without copying
large artifacts. Workers write only their member's inputs and results. Final
structure/charge/wavefunction keys resolve to the authoritative recovery attempt.

A `parameter_reference` is a same-stage comparability constraint. Validation
compares actual INCAR/KPOINTS and pseudopotentials for common elements, including
species-indexed +U settings. It does not require an adsorbate and clean slab to
have identical element lists or submission resources. For cross-stage transfer,
use upstream context and the destination task template instead.

Changing the common scientific basis requires explicit restart of the source
and its descendants. Existing `.job_id` files and results remain protected by the
existing reconcile/restart workflow. No automatic migration of running DAGs is
performed.

## Scheduling

The parent scheduler owns graph transitions. Worker processes execute independent
nodes; the parent accepts node_result.json and wakes newly ready work immediately.
The pool has a fixed capacity (`workflow.max_node_parallel` in `config.yaml`); a slow member does not
hold a batch barrier. Review mode uses one synchronous worker. Supervisor planning
runs at quiescence, so LLM planning never delays already runnable nodes.

This separates dependency policy from worker execution, as in Linux
[workqueues](https://docs.kernel.org/core-api/workqueue.html). Reusing one prepared
source across branches is analogous to shared-prefix reuse; it does not require
turning the scientific DAG into an SGLang radix tree. Cycle validation uses
Python's TopologicalSorter rather than a custom traversal. Monitor phase groups
and unused single-node bootstrap fallback have been removed.

The pool capacity still covers the entire Coordinator lifetime, including Slurm
monitoring. This change does not introduce a second phase scheduler or suspend
live SDK conversations during an external job. Slurm resource admission remains
controlled by configured submission profiles and the cluster scheduler.

## Node-local failure repair

Coordinator owns one bounded runtime repair. CalculationRunner only submits,
monitors and extracts. Analyzer is called with ANALYSIS_MODE=diagnose after the
first attempt: it either writes the final node_result.json, or returns an
evidence-backed repair diagnosis without a terminal result. Facts and uncertain
hypotheses are separate; an unexplained crash is not permission for parameter tuning.

PrepareNodeRepair reserves the budget in runner_result.json:recovery_attempts,
checks the source jobs are terminal in Slurm, snapshots the small handoffs under
.repairs/attempt_1, and copies inputs into new *_recovery1 calculation paths.
The original job IDs, inputs and outputs remain untouched. Empty CONTCAR does not
prevent a justified repair from starting with the original POSCAR.

Coordinator routes to ModelingWorker or ParamWorker, then obtains fresh modeling
and parameter validation with repair_attempt=1. Parameter-only repairs retain the
copied geometry. Workers may reject an unsupported repair. Enabled human-review
gates apply again before submission. Only mapped new calculations are submitted;
unaffected calculations remain unchanged. authoritative_calculations preserves
the original logical identities for result extraction and downstream artifacts.
Analyzer then finalizes; there is no second runtime repair.

Repeated preparation returns the same persisted plan. Resume continues that plan,
including partially submitted attempts, without refreshing its budget. The default
shared node retry limit is one; CATPAW_NODE_RETRY_LIMIT=0 disables repair and a prior
Supervisor full retry consumes it. Raising the setting does not enable multiple
runtime repairs. Existing finalized failed nodes are not automatically reopened.

SubmitVaspJob serializes submissions and always reuses existing .job_id markers.
An interrupted submission with no confirmed job ID leaves .submission_pending;
further submission is blocked until its Slurm outcome is manually reconciled.
This avoids blindly retrying an ambiguously accepted job. These tool guards do
not replace the diagnosis, validation or human approval responsibilities.

## Dashboard

`python -m src.web.dashboard --host 127.0.0.1 --port 8123` serves the DAG, selected
node inputs/documents/visuals, and nested SDK conversations. Trace events are read
on demand in pages of 100, without copying SDK state into a second event log.
Only observable messages, tool actions/results and errors are projected; model
configuration and hidden reasoning are excluded. UI status follows DAG evidence,
not presence of a report file or successful Slurm wrapper exit.

The Web launcher uses the same prepared workspace and pipeline entry point as the
CLI. Interactive human review remains a CLI terminal operation.

The node inspector shows modeling and parameter validation, structure provenance,
and calculation snapshots (job ID, energy, force and convergence). Monitor snapshots
are merged with Runner evidence by last-check time; a scheduler terminal state does
not imply scientific convergence. Recovery calculations remain visible alongside
their replaced attempts. Evidence files are available in a separate tab.

The selected calculation has a read-only progress panel refreshed every ten seconds
while the page and overview tab are visible. It reads bounded OSZICAR/Slurm tails,
separates electronic iteration energy from ionic E0, and shows the latest force,
output timestamps and a collapsed log tail. Charts contain at most 120 recent
points, not the complete trajectory. Scheduler/convergence status remains a labeled
Runner snapshot; this endpoint never polls Slurm, cancels jobs or changes results.

The clickable conversation timeline uses the first and last persisted SDK events.
Its duration includes waiting and is not a measurement of LLM latency or CPU time.
Weave is a suitable optional observability backend for instrumented agent/tool calls;
it is not enabled or required by this dashboard. A future integration should propagate
trace parent context across Coordinator worker processes and use workspace/node IDs
as attributes, keeping DAG dependencies in CatPaw.

Configure each role under `agents.<role>` in `config.yaml` with `url`, `api_key`,
and `model`, following `config.example.yaml`. An optional `api` selects
`chat_completions` or `responses`; when omitted, it uses `llm.api` in `config.yaml` (default:
`chat_completions`). All calls are non-streaming. Subagent factories accept the
SDK's parent-LLM argument but construct the child's independently configured LLM.
The old fast/strong model tiers and global model/credential overrides are removed.

## Weave observation boundary

Set `weave.enabled: true`, `weave.project: entity/project`, and `weave.api_key`
in the ignored local config. An optional `weave.endpoint` overrides the default
`https://trace.wandb.ai/otel/v1/traces` for a dedicated deployment. The integration
uses OpenHands' native Laminar/OpenTelemetry instrumentation and Weave's OTLP
endpoint; no additional Weave SDK patching or event database is maintained.

Each invocation has a workflow span; Coordinator processes receive its explicit
parent context. Agent steps carry role names; SDK conversation, tool and LLM spans
provide execution details. Model/tool spans can contain prompts, arguments and
outputs. Slurm monitoring duration is tool waiting time, not model latency.
Resume creates a new invocation trace linked by the same workspace attribute.
`--reconcile-only` does not initialize tracing or export data.

The CatPaw dashboard owns scientific DAG dependencies, modeling/parameter checks,
job snapshots, convergence evidence and reports. Weave owns call timing and call
trees. The dashboard keeps local event inspection and links to the recorded Weave
project; it does not query Weave for scientific state. Its local event intervals
remain only first/last-event ranges. Runtime environment variables use the
`CATPAW_*` prefix; the benchmark project-root override is `CATPAW`.

API retries retain the SDK's retryable-error classification and exponential
backoff, with explicit coverage for LiteLLM's HTTP 502 `BadGatewayError`. `llm.num_retries: 0` (default) means unlimited attempts; a positive value
limits total attempts. Waiting starts at `llm.retry_min_wait: 60` seconds and caps
at `llm.retry_max_wait: 1800` seconds, without jitter. Authentication and invalid
request errors still fail immediately. Interrupting the process stops retries.
