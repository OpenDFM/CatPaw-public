---
name: job-submission
description: >
  CatPaw CalculationRunner skill for submitting prepared VASP jobs,
  monitoring SLURM status, and writing runner_result.json.
---

# Job Submission

## Scope

This skill is only for CalculationRunner. It starts from a ParamValidator-approved node directory and ends by writing `runner_result.json`.

CalculationRunner owns:

- submitting prepared calculation subdirectories through `SubmitVaspJob`;
- monitoring submitted jobs through `MonitorSubmittedJobs`;
- writing `runner_result.json` through `ExtractCalcResults`;
- reporting submission IDs, terminal status, and blockers to Coordinator.

CalculationRunner does not own modeling, initial parameter planning, static input generation, or post-calculation physics analysis.

## Workflow

1. Read `parameter_summary.json` for calculation subdirectories and `submit_params`.
2. Submit each ready calculation with `SubmitVaspJob`, passing resource values from `submit_params`.
3. Scientific prerequisites are resolved by the DAG before this node starts. Recovery is another physical attempt of this same state, not a new scientific phase.
4. Monitor submitted jobs with `MonitorSubmittedJobs` until a terminal or blocked state is known.
5. Call `ExtractCalcResults` once monitoring has enough evidence to write `runner_result.json`.
6. Read `runner_result.json:completion_assessment`. If `objective_satisfied=true`, return the terminal evidence to Coordinator and do not recover optional failed competitors.
7. Return failures to Coordinator for Analyzer diagnosis. Never independently modify inputs or create recovery attempts.
8. For a Coordinator-prepared repair, submit only the plan's new physical directories and monitor all authoritative paths via explicit subdirs. Preserve recovery_attempts and authoritative_calculations.

runner_result.json is the only Runner handoff. Do not maintain separate status or report files.

## Completion Profiles

- `strict` preserves the existing requirement that all logical calculations converge.
- `demonstration` applies partial-candidate acceptance only to node IDs matching the persisted policy patterns. `ExtractCalcResults` records usable and excluded calculations without changing `job_status.all_converged`.
- A demonstration candidate still needs finite energy and force evidence, no reached-`NELM` signal, the configured force limit, and basic structure-health evidence. Analyzer remains responsible for rejecting abnormal geometry or magnetization.
- Never reinterpret a running calculation as optional completion evidence. The assessment is actionable only after the node's submitted jobs are terminal.

## Boundaries

- Do not handwrite `vasp.sbatch` or `.job_id`; `SubmitVaspJob` owns script rendering, `sbatch`, Job ID parsing, and duplicate-submission handling.
- Do not edit `POSCAR`, `INCAR`, `KPOINTS`, `POTCAR`, `parameter_summary.json`, or validation files.
- Do not override recorded `submit_params` with tool defaults unless the field is genuinely absent.
- Treat failures, cancellations and non-convergence as recorded outcomes for Coordinator; do not choose repairs or change the retry budget.
- Use narrow scheduler or file-inspection commands only when a tool failure needs evidence.

## Reference

- Read `src/skills/job-submission/references/tool-reference.md` for `SubmitVaspJob` parameters and ownership.
- Read `src/skills/job-submission/references/monitoring-guide.md` when continuing long-running jobs or interpreting monitor output.
