from pathlib import Path

import numpy as np
from pymatgen.core import Lattice, Structure
from pymatgen.io.vasp import Poscar

from src.tools.modeler_plugins import adsorption
from src.tools.modeling.advanced import AutoPlaceAdsorbateAction, AutoPlaceAdsorbateExecutor
from src.tools.modeling.validation import validate_structure, validate_structure_relations

FIXTURES = Path(__file__).parent / "fixtures" / "modeling"


def test_grouped_poscar_preserves_species_geometry_and_constraints(tmp_path):
    original = Structure(
        Lattice.cubic(20),
        ["Ce", "O", "C", "O", "H"],
        [[0, 0, 0.3], [0, 0, 0.4], [0, 0, 0.57], [0, 0, 0.5], [0.05, 0, 0.59]],
        site_properties={"selective_dynamics": [[False] * 3, [False] * 3, [True] * 3, [True] * 3, [True] * 3]},
    )
    out = tmp_path / "POSCAR"
    adsorption.write_adsorption_poscar(original, str(out))
    written = Poscar.from_file(out, check_for_potcar=False).structure
    assert out.read_text().splitlines()[5].split() == ["Ce", "O", "C", "H"]
    for site in original:
        matches = [s for s in written if s.specie == site.specie and s.distance(site) < 1e-10]
        assert len(matches) == 1
        assert np.array_equal(matches[0].properties["selective_dynamics"], site.properties["selective_dynamics"])


def test_ontop_evidence_distinguishes_periodic_ir_sites():
    slab = Poscar.from_file(FIXTURES / "IrO2_110_clean.vasp", check_for_potcar=False).structure
    finder = adsorption.AdsorbateSiteFinder(slab, height=1.5)
    labels = adsorption.label_ontop_sites(finder, finder.find_adsorption_sites(distance=0)["ontop"])
    ir = {label["substrate_atom_index_1based"]: label for label in labels if label["element"] == "Ir"}
    assert ir[5]["cn"] == 6
    assert ir[6]["cn"] == 5
    assert len(ir[6]["neighbors"]) == 5
    assert all(n["element"] == "O" and 1.8 < n["distance_A"] < 2.1 for n in ir[6]["neighbors"])


def test_sequential_adsorption_preserves_methoxy_and_targets_substrate(monkeypatch, tmp_path):
    from chgnet.model.dynamics import StructOptimizer
    from pymatgen.io.ase import AseAtomsAdaptor

    # Keep the ML optimizer inert here; test the full real selection, placement,
    # guarding, serialization and executor validation path deterministically.
    monkeypatch.setattr(
        StructOptimizer,
        "relax",
        lambda self, atoms, **kwargs: {"final_structure": AseAtomsAdaptor.get_structure(atoms)},
    )
    clean = FIXTURES / "CeO2_111_clean.vasp"
    (tmp_path / "methoxy.vasp").write_text(clean.read_text())
    worker = AutoPlaceAdsorbateExecutor(str(tmp_path))
    first = worker(
        AutoPlaceAdsorbateAction(
            input_poscar="methoxy.vasp",
            adsorbate_input="C[O]",
            anchor_element="O",
            target_element="Ce",
            height=2.4,
            output_poscar="methoxy.vasp",
        )
    )
    assert first.status == "success", first.message
    before = Poscar.from_file(tmp_path / "methoxy.vasp", check_for_potcar=False).structure
    second = worker(
        AutoPlaceAdsorbateAction(
            input_poscar="methoxy.vasp",
            substrate_poscar=str(clean.resolve()),
            adsorbate_input="H",
            target_element="O",
            target_cn=3,
            height=1.0,
            output_poscar="POSCAR",
        )
    )
    assert second.status == "success", second.message
    assert second.metadata["selected_site"]["element"] == "O"
    assert second.metadata["selected_site"]["substrate_atom_index_1based"] <= 120
    assert "Selected substrate site evidence" in second.message
    after = Poscar.from_file(tmp_path / "POSCAR", check_for_potcar=False).structure
    for site in before:
        assert any(s.specie == site.specie and s.distance(site) < 1e-8 for s in after)
    result = validate_structure(str(tmp_path / "POSCAR"), "adsorption", 120, str(clean))
    assert result["passed"], result
    env = result["info"]["adsorbate_environments"]
    hydrogen = [e for e in env if e["element"] == "H"]
    assert sorted(e["neighbors"][0]["element"] for e in hydrogen) == ["C", "C", "C", "O"]
    assert min(e["z_A"] for e in env) > result["info"]["slab_top_z_A"]
    # Reproduce the historic wrong-face repair: a short OH bond is not enough.
    detached_h = next(e for e in hydrogen if e["neighbors"][0]["element"] == "O")
    bottom_o = min((s for s in after if s.species_string == "O"), key=lambda s: s.z)
    h_index = detached_h["atom_index_1based"] - 1
    after.translate_sites([h_index], bottom_o.coords + [0, 0, 1.01] - after[h_index].coords, frac_coords=False)
    adsorption.write_adsorption_poscar(after, str(tmp_path / "wrong_face.vasp"))
    failed = validate_structure(str(tmp_path / "wrong_face.vasp"), "adsorption", 120, str(clean))
    assert not failed["passed"]
    assert any("embedded" in error for error in failed["errors"])


def test_clean_slab_single_entry_size_check():
    result = validate_structure_relations(str(FIXTURES), [{"label": "clean", "poscar_path": "CeO2_111_clean.vasp"}], [])
    assert not result["passed"]
    assert any("repeat count 10" in error for error in result["errors"])


def test_cleavage_rejects_excessive_thickness_before_writing(tmp_path):
    from src.tools.modeler_plugins.basic_operations import cleave_surface

    bulk = Structure.from_spacegroup("Fm-3m", Lattice.cubic(5.4), ["Ce", "O"], [[0, 0, 0], [0.25, 0.25, 0.25]])
    source = tmp_path / "bulk.vasp"
    bulk.to(filename=source, fmt="poscar")
    out = tmp_path / "slab.vasp"
    result = cleave_surface(str(source), [1, 1, 1], num_layers=9, max_repeat_units=20, output_poscar=str(out))
    assert result.startswith("Error:"), result
    assert "No structure was written" in result
    assert not out.exists()
    result = cleave_surface(str(source), [1, 1, 1], num_layers=4, output_poscar=str(out))
    assert result.startswith("Success:"), result
    assert out.exists()
