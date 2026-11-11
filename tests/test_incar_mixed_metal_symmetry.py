from __future__ import annotations

from pymatgen.core import Lattice, Structure

from src.tools.param_input_plugins.create_INCAR import generate_smart_incar


def write_slab(tmp_path, species: list[str], name: str):
    structure = Structure(
        Lattice.from_parameters(5.0, 5.0, 20.0, 90.0, 90.0, 120.0),
        species,
        [[0.0, 0.0, 0.4], [0.5, 0.5, 0.4]][: len(species)],
    )
    path = tmp_path / name
    structure.to(fmt="poscar", filename=path)
    return path


def test_mixed_metal_slab_forces_isym_zero(tmp_path) -> None:
    poscar = write_slab(tmp_path, ["Ni", "Pd"], "POSCAR.mixed")
    incar = tmp_path / "INCAR.mixed"

    generate_smart_incar(
        str(poscar),
        task_type="slab_opt",
        custom_params={"ISYM": 2},
        output_path=str(incar),
    )

    incar_text = incar.read_text()
    assert "ISYM       = 0" in incar_text
    magmom_line = next(line for line in incar_text.splitlines() if line.startswith("MAGMOM"))
    assert magmom_line.split("=", 1)[1].strip() == "2.0 0.6"
