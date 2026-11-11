# Database resources

`paths.database_dir` in `config.yaml` selects the directory containing these JSON files.

## Local structures: `available_models.json`

When a formula has no local index entry, structure lookup queries Materials Project;
configure `materials_project.api_key` in `config.yaml` to use this fallback.

To add local structures, place the structure files in `database/model_library/`
(or configure `paths.model_library_dir`) and add matching entries to the index.
Only index files that actually exist: local lookup currently treats an index entry
as available without checking that its structure file is present.

The following complete Si record illustrates the existing index format. 

```json
{
  "Si": [
    {
      "category": "semi-conductor",
      "spacegroup": "Fd-3m (227)",
      "description": "Si",
      "model_path": "database/model_library/Si_227.vasp",
      "ispin": 1,
      "mp_reference": {
        "mp_id": "mp-149",
        "energy_above_hull": 0.0,
        "band_gap": 0.6105,
        "magnetic_ordering": "NM",
        "total_magnetization": 0.0
      }
    }
  ]
}
```

Each top-level key is a reduced chemical formula. Its value is a list of structure
records, allowing multiple polymorphs of the same composition.

- `category`: material category label.
- `spacegroup`: space-group symbol and number.
- `description`: structure label or notes.
- `model_path`: structure file path; the example is relative to the repository root.
  For a custom `paths.model_library_dir`, the resolver also checks that directory
  for the referenced filename.
- `ispin`: VASP spin setting (`1` for non-spin-polarized, `2` for spin-polarized).
- `mp_reference`: Materials Project reference metadata: `mp_id` identifies the
  reference material, `energy_above_hull` is in eV/atom, `band_gap` is in eV,
  `magnetic_ordering` describes the magnetic order (`NM` means nonmagnetic), and
  `total_magnetization` is in Bohr magnetons per reference cell. These fields
  describe the MP reference and do not establish equivalence to a local structure.

## Pseudopotential metadata: `potcar_info.json`

This file contains element-indexed lists of pseudopotential metadata, including
`tag`, `ZVAL`, `ENMAX`, `EAUG`, `RWIGS_ang`, `LEXCH`, `valence_config`, and `titel`.
It is used to recommend pseudopotential tags and cutoff energies; it does not
contain the actual POTCAR datasets.

Provide your own licensed VASP pseudopotential library and set `paths.potcar_dir`
in `config.yaml` (default: `database/potcars`). Each potential must be stored
as `<potcar_dir>/<tag>/POTCAR`, for example `<potcar_dir>/Si/POTCAR`.
The supplied metadata must match the potentials you use. Check the selected tags and ENMAX values
against your actual files before using the recommendations.

## Hubbard parameters: `hubbard_u_info.json`

This JSON contains the parameter data itself; no companion data files are needed.
Element entries define `L`, `tier`, `source`, default `U` and `J` values in eV,
and optional anion-specific `contexts`. The `_meta` section explains the format,
source labels, and selection policy. These defaults require system-specific
assessment; they are not universally applicable Hubbard parameters.
