# Parameter Input Contract

## Execution Mode

ParamWorker directly generates static VASP inputs with native tools, then leaves submission and monitoring to CalculationRunner.

## Required Summary Contents

Each calculation entry in `parameter_summary.json` should include:

1. `subdir`;
2. absolute `working_dir`;
3. `task_type`;
6. `input_files` with POSCAR, INCAR, KPOINTS, and POTCAR absolute paths;
7. `incar_params`;
8. `potcar_mapping`;
9. `kpoints_custom_grid`, only when ParamWorker intentionally forces a grid;
10. required file inheritance, such as SCF `CONTCAR`/`CHGCAR`;
11. configured `resource_profile` name and submission resource parameters: `job_name`, `num_dcus`, `total_cpus`, `omp_threads`;
12. `analysis_contract` for Analyzer analysis.

Do not paste VASP input-generation shell commands into `parameter_summary.json`. ParamWorker generates inputs by calling `CreateIncar`, `CreateKpoints`, and `CreatePotcar`.

## Role Boundary

ParamWorker owns:

- INCAR `custom_params`;
- `task_type`;
- KPOINTS `custom_grid`, if any;
- POTCAR mapping;
- submission resource parameters for CalculationRunner;
- required inherited files before submission;
- static file generation for INCAR, KPOINTS, and POTCAR.

CalculationRunner owns:

- generating `vasp.sbatch` through SubmitVaspJob;
- running `sbatch`;
- writing `.job_id`;
- queue/status monitoring;
- writing `runner_result.json`;
- running `ExtractCalcResults`.

ParamWorker must not:

- open or inspect the configured pseudopotential directory or search the filesystem for POTCAR files;
- run `which vasp`, `vasp_std`, `module load`, or similar environment checks;
- call `sbatch`;
- hand-write INCAR/KPOINTS/POTCAR content.

## Summary Template

Use `src/skills/parameter-planning/assets/parameter_summary_template.json` as the single structural template for `parameter_summary.json`.

## Analyzer Handoff

Each calculation in `parameter_summary.json` must include an `analysis_contract` object. This is the handoff from ParamWorker to Analyzer: it tells Analyzer what the calculation represents, which outputs to expect, and which supported analyses to run after CalculationRunner writes `runner_result.json`.

Required fields:

- `calc_purpose`
- `scientific_object`
- `state_label`
- `reaction_family`
- `reaction_group`
- `state_order`
- `comparable`
- `is_final_deliverable`
- `expected_outputs`
- `suggested_analyses`
- `param_worker_notes_for_analyzer`

Allowed `suggested_analyses`: `summary`, `diagnosis`, `charge`, `dos`. Do not use custom names for energy, geometry, forces, or magnetization; those belong under `summary` or `diagnosis`.

Use optional analyses narrowly:

- `charge` is for explicit Bader/charge-density objectives. Do not request it
  just because static calculations can write `CHGCAR`, `AECCAR0`, or `AECCAR2`.
- `dos` is for DOS/PDOS, d-band center, and DOS-derived band-gap objectives.
  In an `opt -> scf -> dos_calc` chain, the `dos_calc` node is normally the one
  that requests `dos`; the prerequisite `scf` node provides charge-density
  evidence and usually keeps `suggested_analyses` to `summary` and `diagnosis`.

## Repair

If ParamValidator fails because of recoverable ParamWorker choices or generated-file defects, rerun ParamWorker with the validator's `next_worker_input` from `parameter_validation.json`. If the failure is a terminal blocker such as missing pseudopotentials, missing upstream artifacts, or an infrastructure constraint that ParamWorker cannot fix, stop and report it to Coordinator.
