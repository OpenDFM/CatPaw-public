---
name: convergence-recovery
description: >
  CatPaw ParamWorker skill for evidence-backed parameter repair within
  a Coordinator-prepared, bounded calculation attempt.
---

# Convergence Recovery

## Scope

Coordinator owns the repair loop and budget. Analyzer supplies observed facts,
uncertain hypotheses and acceptance criteria. ParamWorker selects and generates
the smallest justified input correction; ParamValidator checks it before submission.
CalculationRunner only submits, monitors and extracts results.

## Workflow

1. Read the persisted recovery_attempts plan and diagnosis in runner_result.json.
2. Treat .repairs/attempt_1 summaries and the original calculation inputs as
   read-only baselines. Write VASP inputs only in the mapped new directories.
3. Check whether the evidence supports a parameter correction. A crash or
   missing CONTCAR does not itself identify a parameter problem. Report a blocker
   for an unsupported diagnosis or an environment/library issue.
4. For a justified electronic or ionic convergence correction, read
   references/parameter-guide.md and choose the smallest relevant change.
5. Preserve the copied POSCAR for a parameter-only repair. If the geometry needs
   correction or a different starting structure, return that need to Coordinator
   for ModelingWorker and fresh ModelingValidator review.
6. Preserve scientific intent and comparable ENCUT, KPOINTS, POTCAR, DFT+U, vdW,
   spin treatment and final convergence targets. Do not loosen acceptance merely
   to report success.
7. Generate inputs with the normal parameter tools and update parameter_summary.json
   to the new physical paths, keeping unaffected calculations unchanged. Record
   actual changes and their evidence in the existing summary notes.
8. Return to Coordinator for ParamValidator and any enabled human-review gate.

Do not create attempts, edit job markers, submit jobs, or change the repair ledger.
Do not introduce a second pre-relaxation/recovery submission inside this attempt.
