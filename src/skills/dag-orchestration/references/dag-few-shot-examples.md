# DAG Few-Shot Examples

Use these examples only as decomposition patterns. Reuse the node grouping logic, not the chemistry details, node names, or wording.

Template-backed workflows such as `ADSORPTION_ENERGY`, `OER_AEM`, and
`VACANCY_FORMATION` live in `../assets/`; read the matched asset instead of
duplicating those examples here.

## Example 1: Simple Bulk Optimization

User goal:
Optimize cubic bulk Al and report the final energy and lattice constant.

Good DAG:

- `al_bulk_opt`

Good reason:

- Structure lookup, initial POSCAR preparation, VASP input generation,
  submission, monitoring, and result extraction are node-internal work for the
  single `bulk_opt` calculation node.
- Do not create an `al_bulk_struct`, `structure_prep`, retrieval, or setup node
  before `al_bulk_opt`.

## Example 2: Substitutional Doping

Optimize bulk, then clean_slab_opt, then doped_slab_opt. The doped node binds
structure, modeling_reference, and parameter_reference from the clean node.
Multiple requested dopants are independent branches, not sibling directories.

## Example 3: When Splitting Is Correct

User goal:
Optimize a bulk structure, then run SCF, then compute DOS.

Good DAG:

- `bulk_opt`
- `scf`
- `dos_calc`

Good reason:

- The `task_type` changes at each stage, so this is a true multi-step pipeline rather than one shared modeling family; do not merge `bulk_opt`, `scf`, and `dos_calc` into one node.

## Example 4: Surface d-Band Center

User goal:
Calculate the d-band center of the Ag(111) surface from surface Ag d-orbital
projected DOS.

Good DAG:

- `ag111_slab_opt`
- `ag111_scf`, depends on `ag111_slab_opt`
- `ag111_dos_calc`, depends on `ag111_scf`

Good reason:

- d-band center is a DOS-derived quantity. The optimized slab, converged static
  charge density, and high-density DOS/PDOS calculation are distinct
  calculation stages with different canonical task types.
- Do not collapse the DOS extraction into the `scf` node, and do not create a
  standalone analysis/report node. Analyzer extracts the d-band result inside
  the `dos_calc` node.
- The slab build itself is node-internal work for `ag111_slab_opt`; do not add
  a separate structure-preparation node.

## Example 5: Bulk Bader Charge

User goal:
Perform Bader charge analysis on bulk CaF2 and report the Ca charge.

Good DAG:

- `caf2_bulk_opt`
- `caf2_scf_charge`, depends on `caf2_bulk_opt`

Good reason:

- Bader charge is extracted from a static charge-density calculation, but the
  static calculation should use the optimized bulk geometry. Do not initialize a
  single generic `calculation` node or a standalone `scf` node for this request.
- Do not add a separate `bader`, `charge`, or `analysis` node. Analyzer runs
  Bader extraction inside the dependent `scf` node when the user objective asks
  for charge evidence.

Rule summary:

- Same `task_type` across different states: separate nodes with shared input references.
- Different `task_type` across stages: split into separate nodes.
