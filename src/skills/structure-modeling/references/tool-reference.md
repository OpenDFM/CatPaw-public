# Modeling Tool Reference

## Basis Tools

- `LocateStructure(formula)`: normalize a formula, search the local model
  library first, then query Materials Project when local data is unavailable.
  Prefer local human-verified structures.
- `DownloadMPStructure(mp_id, output_poscar="POSCAR_bulk")`: download a bulk
  structure and write POSCAR.
- `CleaveSurface(input_bulk_poscar, miller_index, num_layers=4,
  vacuum_size=15.0, termination_element=None, max_repeat_units=6,
  fail_repeat_units=8, output_poscar="POSCAR_slab")`: build a slab.
  `num_layers` is a repeat-count target. The output includes termination
  ranking and "Surface detail" lines with slab coordination numbers. If every
  candidate exceeds `fail_repeat_units`, no slab is written; reduce `num_layers`.
  `vacuum_size` is the minimum total vacuum gap between neighboring periodic
  slabs, not padding on each side. For a centered slab, the bottom and top
  cell-boundary gaps add up to this gap. The tool reports actual slab thickness,
  cell height along the surface normal, and actual periodic vacuum gap; the
  actual gap may exceed the requested minimum due to crystallographic repeats.
- `AnalyzeSurfaceLayout(input_slab_poscar, adsorbates=[],
  surface_modifications=[], min_defect_image_distance=8.0)`: recommend surface
  supercell and vacuum for adsorption, vacancy, and doping families. It does not
  place adsorbates or create defects.
- `CreateSupercell(input_poscar, scaling_matrix, output_poscar)`: expand a
  structure.
- `FixLayersByElement(input_poscar, fix_ratio=0.5, tolerance=0.1,
  output_poscar="POSCAR")`: add Selective Dynamics tags to fix lower layers.

## Modeler Tools

- `AutoPlaceAdsorbate(input_poscar, adsorbate_input, site_type="ontop",
  site_index=0, height=2.0, anchor_element=None, output_poscar="POSCAR",
  target_element=None, target_cn=None, substrate_poscar=None)`: place an adsorbate on a surface.
  Use the clean `substrate_poscar` for sequential placement; see `site-selection.md`.
  Resolve reported collisions from geometry before changing the placement height.
- `ModifySurfaceDefects(input_poscar, defect_type, target_element,
  dopant_element=None, surface_side="top", coordination_number=None,
  output_poscar="POSCAR")`: create a vacancy or substitutional dopant.
  `defect_type` is `vacancy` or `doping`; `dopant_element` is required for
  doping.
- `BuildMoleculeInBox(molecule_input, box_size=15.0,
  output_poscar="POSCAR")`: build an isolated molecule POSCAR.

## Execution Rules

- Final output in every calculation subdirectory must be named `POSCAR`.
- Use absolute input and output paths.
- Pre-copy the basis `POSCAR` into adsorption, vacancy, and doping
  subdirectories before calling the structure-changing tool.
- POSCAR species/count lines define atom identity; trailing element comments do
  not. Never consolidate species headers without reordering whole sites, including
  coordinates and Selective Dynamics. Adsorption outputs are already grouped.
- Do not handwrite structure edits for adsorption, vacancy, doping, or molecule
  construction.
- If `AnalyzeSurfaceLayout` recommends larger vacuum, re-cleave before fixing
  layers.
- Report tool warnings in the final ModelingWorker response.
