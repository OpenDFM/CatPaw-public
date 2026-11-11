# Tool Reference

## SubmitVaspJob

`SubmitVaspJob(working_dir, job_name=None, total_cpus=8, num_dcus=1, omp_threads=None, sbatch_filename="vasp.sbatch", render_sbatch_if_missing=True, force_render_sbatch=False)`

`working_dir` is a calculation subdirectory prepared by ParamWorker. Prefer an absolute path when available.

The Agent-filled resource fields are `job_name`, `total_cpus`, `num_dcus`, and `omp_threads`. Take them from `parameter_summary.json:submit_params`; built-in defaults are fallbacks for absent fields only.

`SubmitVaspJob` owns:

- checking non-empty `POSCAR`, `INCAR`, `KPOINTS`, and `POTCAR`;
- rendering or validating `vasp.sbatch`;
- running `sbatch vasp.sbatch`;
- parsing `Submitted batch job <id>`;
- writing `.job_id`;
- always reusing existing `.job_id`, including concurrent calls;
- refusing a retry when `.submission_pending` records an ambiguous submission outcome until Slurm reconciliation;
- checking fresh repair validation and source-job termination for prepared recovery attempts.

Fixed sbatch content, including partition, modules, environment setup, and `mpirun`, belongs to the config-backed tool template. Do not treat those lines as Agent decision fields.

Submission success requires a submitted or existing numeric Job ID and a `.job_id` file in `working_dir`. Monitoring is a separate CalculationRunner step.

## MonitorSubmittedJobs

`MonitorSubmittedJobs(workspace_path=None, subdirs=None, max_poll_seconds=7200)`

Use `workspace_path` for the active node directory. Use `subdirs` only when monitoring a subset of already submitted calculation subdirectories.

This tool owns polling and status aggregation. The important output fields are `all_terminal`, `all_converged`, `has_failure`, `still_running`, and per-calculation statuses. Reinvoke it when `still_running=true`.

## ExtractCalcResults

`ExtractCalcResults(workspace_path=None, subdirs=None)`

Call this after monitoring reaches a terminal, blocked, or failure state. It writes or updates `runner_result.json` with `job_status` and `calc_summary`.

Use `subdirs` only when extracting a subset of submitted calculations. Do not replace this handoff with separate status files or ad hoc result summaries.
