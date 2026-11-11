from pathlib import Path

from pymatgen.io.vasp import Poscar

from src.utils.config import potcar_dir


def generate_smart_potcar(poscar_path: str, potcar_mapping: dict = None, output_path: str = "POTCAR") -> str:
    """
    Robustly assemble POTCAR without lossy transformations.
    1. Strictly follows element order parsed from POSCAR.
    2. Uses binary concatenation (rb/wb) to avoid encoding or line-ending corruption.
    """
    base_pot_dir = potcar_dir()

    if potcar_mapping is None:
        potcar_mapping = {}

    try:
        if not base_pot_dir.exists():
            return (
                f"❌ Fatal error: POTCAR root directory does not exist. Resolved path: {base_pot_dir}. "
                "Please check paths.potcar_dir in config.yaml."
            )

        # Parse POSCAR using VASP-specific parser to preserve official element order.
        poscar = Poscar.from_file(poscar_path)
        elements_in_poscar = poscar.site_symbols

        used_potentials = []

        # Open output in binary mode to preserve original POTCAR format.
        with Path(output_path).open("wb") as fout:
            for el in elements_in_poscar:
                pot_name = potcar_mapping.get(el, el)
                pot_file_path = base_pot_dir / pot_name / "POTCAR"

                if not pot_file_path.exists():
                    return f"❌ Fatal error: pseudopotential file for element {el} was not found at {pot_file_path}."

                # Raw binary read/write.
                with pot_file_path.open("rb") as fin:
                    fout.write(fin.read())

                used_potentials.append(pot_name)

        return (
            f"✅ POTCAR generated successfully. POSCAR element order strictly applied: {elements_in_poscar}. "
            f"Applied pseudopotentials: {used_potentials}. Source: config.yaml paths.potcar_dir={base_pot_dir}."
        )

    except Exception as e:
        return f"❌ POTCAR generation failed: {str(e)}"
