from __future__ import annotations

import numpy as np
from pymatgen.core import Lattice, Structure

import src.tools.modeler_plugins.adsorption as adsorption


def adsorption_structures() -> tuple[Structure, Structure]:
    lattice = Lattice.from_parameters(5.0, 5.0, 20.0, 90.0, 90.0, 120.0)
    slab = Structure(
        lattice,
        ["Ni"],
        [[0.0, 0.0, 0.4]],
        site_properties={"selective_dynamics": [[False, False, False]]},
    )
    merged = slab.copy()
    merged.append(
        "H",
        [0.0, 0.0, 0.55],
        properties={"selective_dynamics": [True, True, True]},
    )
    return slab, merged


def test_guarded_relaxation_disables_cell_relaxation(monkeypatch, tmp_path) -> None:
    from chgnet.model.dynamics import StructOptimizer
    from pymatgen.io.ase import AseAtomsAdaptor

    slab, merged = adsorption_structures()
    observed: dict[str, object] = {}

    def fake_relax(self, atoms, **kwargs):
        observed.update(kwargs)
        return {"final_structure": AseAtomsAdaptor.get_structure(atoms)}

    monkeypatch.setattr(StructOptimizer, "relax", fake_relax)
    output = tmp_path / "POSCAR"

    result = adsorption.run_guarded_relaxation(
        merged,
        slab,
        expected_ads_count=1,
        num_slab_atoms=1,
        output_poscar=str(output),
        steps=3,
    )

    assert result["status"] == "success"
    assert result["relaxation_accepted"] is True
    assert observed["relax_cell"] is False
    written = Structure.from_file(output)
    assert np.allclose(written.lattice.matrix, slab.lattice.matrix, atol=1e-12, rtol=0)


def test_guarded_relaxation_rejects_lattice_drift(monkeypatch, tmp_path) -> None:
    from chgnet.model.dynamics import StructOptimizer
    from pymatgen.io.ase import AseAtomsAdaptor

    slab, merged = adsorption_structures()

    def fake_relax(self, atoms, **kwargs):
        relaxed = AseAtomsAdaptor.get_structure(atoms)
        matrix = relaxed.lattice.matrix.copy()
        matrix[0, 1] += 1e-5
        return {
            "final_structure": Structure(
                matrix,
                relaxed.species,
                relaxed.frac_coords,
                site_properties=relaxed.site_properties,
            )
        }

    monkeypatch.setattr(StructOptimizer, "relax", fake_relax)
    output = tmp_path / "POSCAR"

    result = adsorption.run_guarded_relaxation(
        merged,
        slab,
        expected_ads_count=1,
        num_slab_atoms=1,
        output_poscar=str(output),
        steps=3,
    )

    assert result["status"] == "guarded_fallback"
    assert result["relaxation_accepted"] is False
    assert "changed lattice vectors" in result["fallback_reason"]
    written = Structure.from_file(output)
    assert np.allclose(written.lattice.matrix, slab.lattice.matrix, atol=1e-12, rtol=0)
