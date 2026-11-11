# Monitoring Guide

`MonitorSubmittedJobs` is CalculationRunner's source of job status. It performs blocking polling internally and reports progress in the terminal.

Use it after the intended jobs have been submitted. If it returns `still_running=true`, call it again to continue monitoring. Focus on `all_terminal`, `all_converged`, `has_failure`, `still_running`, and calculation-level status lines.

## Convergence And Failure Signals

Runner uses monitor signals to decide whether to keep watching or preserve evidence:

- `still_running=true`: jobs are not terminal; call `MonitorSubmittedJobs` again later.
- `all_terminal=true` and `all_converged=true`: all submitted calculations completed successfully.
- `has_failure=true`, `all_converged=false`, or status `NOT_CONVERGED`, `STALLED`, `FAILED`, or `CANCELLED`: stop treating the node as progressing normally and preserve evidence.
- `OSCILLATING` or oscillation details: electronic instability evidence.
- `stale` or `stale_steps`: no useful energy progress for recent ionic steps.
- `Fmax` above target, high `ionic` count, or high `elec` count: slow or failed convergence evidence.

After terminal, failed, stalled, cancelled, or blocked states, call `ExtractCalcResults`. It writes `runner_result.json` with `job_status`, `calc_summary`, and calculation evidence such as `reached_nelm`, `reached_nsw`, and `errors` when available.

Do not build ad hoc `sacct` or `squeue` parsing loops unless the tool itself fails and scheduler evidence is needed for the final response.

For slow but progressing jobs, later monitor calls may use a longer `max_poll_seconds`. If jobs fail, stall, are cancelled, or do not converge, call `ExtractCalcResults` to preserve evidence in `runner_result.json`. Return this evidence to Coordinator for Analyzer diagnosis and the bounded worker/validator repair loop.
