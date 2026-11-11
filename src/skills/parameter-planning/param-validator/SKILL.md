---
name: param-validator
description: >
  CatPaw parameter-validation skill for ParamValidator. Use when independently
  validating parameter_summary.json, generated VASP input files, submission
  resource fields, and task-type template consistency before CalculationRunner
  submits jobs.
---

# VASP Parameter Validation

## Core Workflow

1. Read `task.md`, `modeling_summary.json`, `modeling_validation.json`, and `parameter_summary.json` from the active node directory.
2. Confirm `modeling_validation.json` is PASS, unless the Coordinator message explicitly supplies an equivalent latest ModelingValidator PASS JSON.
3. Read `src/skills/parameter-planning/references/parameter-input-contract.md` and `src/skills/parameter-planning/assets/parameter_summary_template.json` to check the summary contract.
4. Read `src/skills/parameter-planning/assets/calculation_templates/index.yaml`, then read the matching task-type YAML in that same directory for every planned calculation.
5. Inspect each planned calculation directory with narrow file checks for non-empty POSCAR, INCAR, KPOINTS, and POTCAR.
6. Compare `parameter_summary.json` behavior-relevant `incar_params` with the actual INCAR in each calculation directory.
7. Call `ValidateParameterPolicy` with `ACTIVE_NODE_DIR` and a jobs list extracted from the node documents and summary.
8. Write only `parameter_validation.json` and return the same strict JSON object to Coordinator.

Template assets are validation evidence. Use them to catch wrong or stale parameters; do not use them to regenerate inputs.

For a Coordinator-prepared runtime repair, assess recorded convergence-control
deviations against the diagnosis and recovery guidance, not the initial template
alone. Scientific intent, comparable precision and final acceptance targets remain
binding; a recovery label does not waive those checks.

## Required Checks

- Accept only canonical task types listed in `calculation_templates/index.yaml`: `bulk_opt`, `slab_opt`, `mol_opt`, `scf`, and `dos_calc`.
- Reject task-type aliases such as `static`, `mol_static`, `dos`, `band`, or any template that does not exist.
- Reject unsupported `analysis_contract.suggested_analyses`; allowed values are `summary`, `diagnosis`, `charge`, and `dos`.
- Confirm each calculation records `subdir`, `working_dir`, `task_type`, `input_files`, `incar_params`, `potcar_mapping`, `resource_profile`, `submit_params`, and `analysis_contract`.
- Confirm `resource_profile`, INCAR `KPAR`/`NCORE`, and the `submit_params` fields `job_name`, `total_cpus`, `num_dcus`, and `omp_threads` match the configured profile selected by `ValidateParameterPolicy`.
- Confirm every listed POSCAR, INCAR, KPOINTS, and POTCAR exists and is non-empty.
- Fail if `parameter_summary.json` would mislead downstream agents or reports about actual behavior-relevant INCAR settings.
- Treat missing required inherited files for SCF, DOS, charge, or chained calculations as FAIL unless the missing input is an external terminal blocker.
- When the persisted policy is demonstration, validate the slab template's
  explicit demonstration override instead of rejecting it against the strict
  mandatory baseline. Confirm the summary records the profile and matches the
  actual INCAR.
- For comparable reaction-path or family calculations, verify the jobs passed to `ValidateParameterPolicy` include `family_key` and `comparable` when that context is present.
- For directly compared slab-family calculations, do not reinterpret POSCAR
  geometry. Instead, verify `modeling_validation.json` has accepted the slab
  family and fail if `parameter_summary.json` ignores unresolved ModelingValidator
  findings about slab alignment, slab thickness, or unusually large in-plane
  models.
- For DFT+U, pass `dftu_allowed` or `explicit_dftu_request` only when justified by user intent, task context, or material chemistry.
- For charge or DOS tasks depending on CHGCAR, pass `requires_fft_inheritance` plus `upstream_fft_grid` or `upstream_fft_source` when available.

## Evidence Rules

- Use narrow shell evidence such as `test -s`, `ls -lh`, `head -n 8 POSCAR`, `sed -n '1,12p' INCAR`, `sed -n '1,5p' KPOINTS`, or `grep -m 1 'TITEL\|ENMAX\|PBE' POTCAR`.
- Do not full-open numeric or concatenated VASP files such as POSCAR coordinate blocks, POTCAR, CHGCAR, WAVECAR, DOSCAR, PROCAR, EIGENVAL, OUTCAR, or `vasprun.xml`.
- Read JSON and small YAML/Markdown contracts directly; do not invent a parser tool for handoff files.
- Include concrete files, paths, commands, tool findings, and repair hints in `findings`.

## Failure Handling

- Use `status: "FAIL"` when any required check fails.
- Use `terminal_blocker: false` for ParamWorker-repairable defects such as wrong task type, missing generated input, inconsistent summary, bad INCAR policy, wrong KPOINTS, missing analysis contract, or unsupported analysis names.
- Use `next_worker_input` as a concise retry brief that ParamWorker can act on directly.
- Use `terminal_blocker: true` only when ParamWorker cannot repair the issue, such as missing external pseudopotentials, missing upstream artifacts, infrastructure constraints, or unresolved task ambiguity.
- Do not modify `parameter_summary.json`; Coordinator reruns ParamWorker with `next_worker_input` when repair is possible.

## Boundaries

- Do not repair files.
- Do not call `CreateIncar`, `CreateKpoints`, or `CreatePotcar`.
- Do not modify POSCAR, INCAR, KPOINTS, POTCAR, `parameter_summary.json`, or modeler outputs.
- Do not generate `vasp.sbatch`, submit jobs, run VASP, monitor jobs, or call Runner tools.
- Do not write reports. The only ParamValidator artifact is `parameter_validation.json`.

## Shared Inputs

ValidateParameterPolicy reads inputs.json and checks parameter_reference against
actual input files for the same-stage family. A single member must still pass
this cross-node check. Do not waive a mismatch because the node has only one job.
