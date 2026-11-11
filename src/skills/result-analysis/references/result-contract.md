# Result Contract

Analyzer writes node_result.json only when the node is ready to finish, including final failures and blockers. A proposed repair is not a terminal outcome.

## WriteNodeResult

`WriteNodeResult(node_dir, node_status, error, artifacts, calculations, analysis)`

Writes `node_result.json`, normalizes the `analysis` section, and adds standard absolute artifact paths for `node_result`, `modeling_summary`, `modeling_validation`, `parameter_summary`, `parameter_validation`, and `runner_result`.

Use `node_status="success"` only when required calculations completed successfully or available evidence satisfies the node objective. Use `blocked` for missing required evidence or unresolved upstream blockers. Use `failed` for completed failures, non-convergence, invalid structures, or exhausted repair budgets.

## node_result.json

Analyzer writes one JSON object:

```json
{
  "node_status": "success",
  "error": null,
  "artifacts": {
    "node_result": "/abs/path/node_result.json",
    "modeling_summary": "/abs/path/modeling_summary.json",
    "modeling_validation": "/abs/path/modeling_validation.json",
    "parameter_summary": "/abs/path/parameter_summary.json",
    "parameter_validation": "/abs/path/parameter_validation.json",
    "runner_result": "/abs/path/runner_result.json"
  },
  "calculations": [],
  "analysis": {
    "summary": "",
    "key_findings": [],
    "limitations": [],
    "suggested_followups": []
  }
}
```

On failure:

- `node_status`: `failed` or `blocked`
- `error`: concise failure summary

Artifacts should use absolute paths and may include key VASP outputs such as `CONTCAR`, `CHGCAR`, or `OUTCAR` when they are needed by downstream nodes.

## Diagnosis before finalization

The default ANALYSIS_MODE is finalize: write the terminal node_result.json.
Coordinator may explicitly request diagnose after CalculationRunner returns.

In diagnose mode, propose repair only if the scientific objective remains unmet,
specific evidence supports a bounded worker-owned correction, and no
recovery_attempts entry exists. Success and optional failed competitors accepted
by the completion policy do not need repair. External cancellations, unknown
scheduler state, missing credentials, program/library failures without a supported
input correction, and speculative causes are blockers, not tuning opportunities.
If repair is unsupported or already spent, write the final result.

Return this JSON instead of calling WriteNodeResult when proposing repair:

```json
{
  "calculations": ["slab_opt"],
  "diagnosis": {
    "worker": "param_worker_agent",
    "evidence": ["path/to/slurm.out: observed electronic convergence warning"],
    "hypotheses": ["Possible electronic minimization instability; cause not proven"],
    "repair_goal": "Prepare an evidence-backed electronic convergence correction",
    "acceptance_criteria": ["Preserve the scientific model and comparable precision", "Converged electronic and ionic results"]
  }
}
```

Use modeling_worker_agent for supported geometry corrections, param_worker_agent
for input/convergence corrections. Cite file paths or tool evidence for facts.
Keep uncertain causes under hypotheses; temporal sequence is not causation.
Prefer tool-extracted numbers and preserve exponent/unit interpretation. A SIGABRT
or malloc error alone does not prove an ALGO or MAGMOM problem.
Do not prescribe speculative INCAR values or declare that a proposed fix will work.
Workers select the correction; validators independently check the prepared inputs.
Final acceptance still requires actual calculation evidence from the new attempt.
