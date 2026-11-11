# Modeling Validation Guide

## Summary Contract

`modeling_summary.json` must be a JSON object with:

- `schema_version`
- `workspace`
- `structures`
- `shared_modeling_context`

Each structure entry must include `subdirectory`, `poscar_path`,
`poscar_source`, `content`, `structure_kind`, `calculation_intent`, optional
`task_type_hint`, `atom_count`, and `provenance`.

Reject summaries that contain downstream-worker notes, validation conclusions,
VASP parameter advice, INCAR/KPOINTS/POTCAR settings, or a top-level validation
field. `task_type_hint` is advisory only; ParamWorker owns the final canonical
`task_type`.

For molecule or cluster entries, periodic isolation should be described with
`periodic_image_distance_angstrom`. Reserve `vacuum_angstrom` for slab vacuum
thickness, where it means the empty gap normal to the slab.

## Tool Use

`ValidateStructureRelations` does not read `modeling_summary.json`. The
validator must supply explicit entries:

- `structures`: labels plus POSCAR paths.
- `relations`: `same_slab`, `adsorption`, `vacancy`, or `doping` with
  `reference` and `target`.

Use default `max_repeat_units=5` and `fail_repeat_units=8` unless the task
explicitly requests slab-thickness convergence or unusual high-precision
surface-energy modeling. Treat warnings above the default repeat/layer limit as
repair-required for directly compared surface families unless the task
explicitly allows a thicker slab.

Use `ValidateStructure` for:

- single POSCAR format checks (slabs still require the single-entry size check);
- molecule periodic-image spacing;
- adsorption atom-count and height checks against a clean slab;
- targeted evidence after a relation warning.

## Surface-Energy Checks

For `SURFACE_ENERGY` or explicit surface-energy tasks:

- Fail bottom-fixed slabs. Treat fixed bottom or lower layers in a standard
  surface-energy slab as a common modeling error, because the standard `2A`
  formula assumes two equivalent relaxed surfaces.
- Fail clearly too-thin slabs when deterministic evidence shows fewer than
  three atomic layers or slab repeats.
- Treat around six layers or repeats as the ordinary target, but do not fail
  solely for being below six if the slab has at least three layers and the
  summary justifies the choice.
- Require a repair if the task uses the standard `2A` formula but the slab is
  asymmetric and the summary does not state an adjusted convention.

For non-surface-energy slabs, use `ValidateStructureRelations` repeat-count
errors and warnings instead of maintaining a separate thickness rule.

## Semantic Checks

- Adsorption must add the requested adsorbate without changing substrate atoms.
- Vacancy must remove the requested species/site.
- Doping must be a balanced substitution with the intended target and dopant
  elements.
- Molecules must match the requested species and use a reasonable isolated box.
- Surface-family calculations should preserve lattice, shared slab geometry,
  and Selective Dynamics policy across comparable variants.
- Directly compared surface-family states are stricter than generic slab
  checks: sibling slab structures must share the same in-plane cell, slab
  layer/repeat count, fixed-layer convention, vacuum convention, and active-site
  registry. A smaller clean slab may not be used as the direct energy reference
  for a larger adsorbed/defective slab unless the task explicitly requests an
  integer-supercell scaling convention and the analysis contract records that
  convention.
- Do not silently accept routine directly compared slabs above 5 repeats/layers
  or very large in-plane cells. If `ValidateStructureRelations` warns about slab
  thickness or large atoms-per-layer, fail validation and ask ModelingWorker to
  rebuild a smaller shared slab family unless the user/task explicitly justifies
  the larger model.

## Adsorption Evidence

Read atom identities through a POSCAR parser, not trailing coordinate comments.
Use `ValidateStructure` adsorbate environments to check the requested fragment
connectivity, anchor–substrate contact, surface side and outward orientation.
Neighbor distances are evidence, not an automatic bond assignment: check the
expected molecular graph, including which heavy atom carries each hydrogen.
Atom totals, one heavy-atom bond length, or spatial clustering are insufficient.
Use periodic distances; x=0 and x=1 are equivalent, not a reason to relocate atoms.

Apply the before-adsorption coordination and repair evidence rules in
`site-selection.md`. A previous Validator's coordinate prescription is a claim
to verify, not ground truth. After repair, repeat the full semantic check, not
only the previously failed distance. Record atom indices and measured distances
in findings. If the chemical site or connectivity is unverified, do not claim PASS.

## Output

Return and write exactly:

```json
{
  "status": "PASS|FAIL",
  "findings": ["files, paths, commands, and tool evidence checked"],
  "required_fixes": ["worker-side fixes required"],
  "next_worker_input": "concise retry brief when failing",
  "terminal_blocker": false
}
```

For PASS, `required_fixes` and `next_worker_input` must be empty. For repairable
FAIL, `required_fixes` must be non-empty and `next_worker_input` must be directly
usable by ModelingWorker.
