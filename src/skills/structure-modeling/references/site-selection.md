# Site Selection

## Adsorbate Identifiers

For `AutoPlaceAdsorbate.adsorbate_input`, use this order:

1. Built-in keys: `H`, `O`, `OH`, `CO`, `OOH`, `N2`.
2. SMILES for other molecules, such as `[OH2]` for water, `OC` for methanol,
   `CCO` for ethanol, and `OC=O` for formic acid.
3. English names only as a last resort because PubChem lookup may fail on HPC
   systems.

`CO` is carbon monoxide. Use `OC` for methanol.

For `BuildMoleculeInBox`, built-in formulas include `H2`, `O2`, `N2`, `CO`,
`NO`, `OH`, `H2O`, `CO2`, and `NH3`.

## Adsorption Sites

When the intended site is not the topmost atom, derive `target_element` and
`target_cn` from `CleaveSurface` "Surface detail" output and pass them to
`AutoPlaceAdsorbate`.

Examples:

- OH on undercoordinated Mn of MnO2(110): use `target_element="Mn"` and the
  undercoordinated Mn slab CN from `CleaveSurface`.
- CO on Pt(111) top site: omit `target_element` and `target_cn`.
- H on MoS2 basal plane: set `target_element="S"` if the task intends S sites.

## Sequential And Dissociative Adsorption

For each fragment, keep `input_poscar` as the current full structure and pass
`substrate_poscar` as the original clean slab in the same cell/frame. The tool
finds sites and coordination on substrate atoms only, preserving prior fragments.
Do not remove an element/CN filter or switch to an unrelated bridge site merely
to obtain a successful call. An empty site list is a modeling problem to resolve.
Record the selected substrate atom index (1-based, in the reference file), its
CN/neighbors, and the adsorbate anchor in provenance. Output atom indices may
change when species are grouped; match by species and periodic position.

A dissociated state must preserve the intended fragment connectivity and put
fragments on the intended surface side. Validate the resulting geometry after
each addition; a saved fallback or ML relaxation is not proof of chemical identity.

## Site Evidence And Repairs

Identify coordination in the substrate **before** adding the adsorbate. Use the
tool's selected-site evidence or compute periodic neighbors on that reference
(e.g. `CrystalNN.get_nn_info`). A coordinate such as fractional y=0.75, a height,
or a free/fixed flag does not identify a chemical site. For a proposed repair,
provide the current and proposed substrate atom indices, neighbor species,
periodic images and distances; do not override calculated CN with a site name.

Determine the exposed side from the substrate geometry, excluding adsorbates.
On the tool's +z adsorption face, the upper substrate surface is near its maximum
z; the lower surface is near its minimum z. A short adsorbate–substrate distance
alone does not establish the correct face or an outward bond orientation.

## Vacancy And Doping Sites

When the task names a chemical environment, translate it into the
`coordination_number` argument for `ModifySurfaceDefects`. Prefer the actual
slab CN from `CleaveSurface` over static textbook values.

Examples:

- Rutile TiO2(110) bridging O vacancy: `target_element="O"`,
  `coordination_number=2`, `surface_side="top"`.
- N doping at a bridging O site: `defect_type="doping"`,
  `target_element="O"`, `dopant_element="N"`, and the bridging-O slab CN.
- Generic "surface O vacancy": leave `coordination_number=None` and use top
  z-layer selection.

For bulk substitution tasks, do not cleave a slab. Prepare a bulk supercell and
use `ModifySurfaceDefects` only as the substitution engine. Record that the
selected top-layer site is a tool convention for a bulk supercell.
