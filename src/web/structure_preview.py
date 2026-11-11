"""Parse structure snapshots only when requested by the dashboard."""

from pathlib import Path


def structure_snapshot(source: Path) -> dict:
    from pymatgen.io.cif import CifWriter
    from pymatgen.io.vasp.inputs import Poscar

    structure = Poscar.from_file(source, check_for_potcar=False).structure
    return {
        "formula": structure.composition.reduced_formula,
        "atoms": len(structure),
        "filename": source.name,
        "cif": str(CifWriter(structure)),
    }
