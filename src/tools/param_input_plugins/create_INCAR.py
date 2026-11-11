import json

from pymatgen.core import Structure

from src.utils.config import database_dir
from src.utils.task_types import CANONICAL_TASK_TYPES

DEFAULT_MAGNETIC_ELEMENTS = {"V", "Cr", "Mn", "Fe", "Co", "Ni"}


def load_hubbard_u_info() -> dict | None:
    db_path = database_dir() / "hubbard_u_info.json"
    if not db_path.is_file():
        return None
    with db_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def compress_magmom(magmom_str: str) -> str:
    """Compress a MAGMOM string to VASP n*value format.

    Example: "2.0 2.0 2.0 0.6 0.6" -> "3*2.0 2*0.6"
    Supports mixed compressed input (e.g., "3*2.0 0.6 0.6").
    """
    # First expand all n*value tokens into flat entries.
    tokens = []
    for part in magmom_str.split():
        if "*" in part:
            n_str, val = part.split("*", 1)
            tokens.extend([val] * int(n_str))
        else:
            tokens.append(part)

    if not tokens:
        return magmom_str

    # Recompress consecutive identical values.
    parts = []
    count = 1
    curr = tokens[0]
    for tok in tokens[1:]:
        if tok == curr:
            count += 1
        else:
            parts.append(f"{count}*{curr}" if count > 1 else curr)
            curr = tok
            count = 1
    parts.append(f"{count}*{curr}" if count > 1 else curr)
    return " ".join(parts)


def count_magmom_atoms(magmom_str: str) -> int:
    """Count total atoms represented by MAGMOM string (supports n*value format)."""
    total = 0
    for part in magmom_str.split():
        if "*" in part:
            n_str, _ = part.split("*", 1)
            total += int(n_str)
        else:
            total += 1
    return total


def generate_smart_incar(
    poscar_path: str,
    task_type: str = "slab_opt",
    is_charged: bool = False,
    custom_params: dict = None,
    output_path: str = "INCAR",
) -> str:
    """Generate an INCAR for a canonical task type and detected magnetic state."""
    try:
        struct = Structure.from_file(poscar_path)
        element_order = list(dict.fromkeys(site.specie.symbol for site in struct))
        metal_elements = {site.specie.symbol for site in struct if site.specie.is_metal}

        # Normalize task_type (case-tolerant only)
        raw_task_type = task_type
        if task_type not in CANONICAL_TASK_TYPES:
            task_type = task_type.lower().strip()
        if task_type not in CANONICAL_TASK_TYPES:
            # Keep original value if unrecognized; downstream logic preserves the input.
            task_type = raw_task_type

        # Automatic magnetic detection (Pymatgen)
        has_magnetic_element = False
        magmom_list = []
        for site in struct:
            # Enable spin only for common magnetic elements by default.
            # Other transition metals can still be spin-polarized via custom_params.
            if site.specie.symbol in DEFAULT_MAGNETIC_ELEMENTS or site.specie.is_rare_earth or site.specie.is_actinoid:
                has_magnetic_element = True
                magmom_list.append("2.0")
            else:
                magmom_list.append("0.6")  # Small perturbation for nominally nonmagnetic elements.

        magmom_str = compress_magmom(" ".join(magmom_list))

        # Base flat parameter dictionary (formatted into blocks later)
        incar_flat = {
            "SYSTEM": f"Auto_Generated_{task_type}",
            # read_in
            "ISTART": 0,
            "ICHARG": 2,
            # accuracy
            "PREC": "Accurate",
            "LREAL": "Auto",
            "ENCUT": 520,
            "EDIFFG": -0.03,
            "EDIFF": "1E-5",
            "ISMEAR": 0,
            "SIGMA": 0.05,
            "IVDW": 12,
            # optimization
            "IBRION": 2,
            "ISIF": 2,
            "NSW": 200,
            "NELM": 400,
            "ALGO": "Fast",
            "ISYM": 2,
            # calculation_resource (conservative defaults; ParamWorker may override via custom_params)
            "NCORE": 1,
            "KPAR": 1,
            # output
            "LVTOT": ".F.",
            "LAECHG": ".F.",
            "LELF": ".F.",
            "LCHARG": ".F.",
            "LWAVE": ".F.",
        }

        # If magnetic elements are present, enable spin polarization.
        if has_magnetic_element:
            incar_flat["ISPIN"] = 2
            incar_flat["MAGMOM"] = magmom_str
        else:
            incar_flat["ISPIN"] = 1

        # Automatic DFT+U fallback detection
        # Detect co-presence of strongly correlated elements (d/f electrons) and anions.
        strong_corr_elements = set()
        has_anion = False
        anion_symbols = {"O", "S", "Se", "Te", "N", "F", "Cl", "Br", "I"}

        for site in struct:
            sym = site.specie.symbol
            if site.specie.is_transition_metal or site.specie.is_rare_earth or site.specie.is_actinoid:
                strong_corr_elements.add(sym)
            if sym in anion_symbols:
                has_anion = True

        if strong_corr_elements and has_anion:
            # Try reading U parameters from hubbard_u_info.json.
            try:
                u_db = load_hubbard_u_info()

                if u_db:
                    # Build LDAU arrays in POSCAR element order.
                    # Detect system anions for context matching.
                    anions_in_system = [el for el in element_order if el in anion_symbols]
                    context_anion = anions_in_system[0] if anions_in_system else None

                    l_list, u_list, j_list = [], [], []
                    needs_u = False
                    has_f_electron = False

                    for el in element_order:
                        if el in u_db:
                            info = u_db[el]
                            l_val = info.get("L", -1)
                            if l_val == 3:
                                has_f_electron = True
                            # Prefer context-matched U/J; fallback to defaults.
                            contexts = info.get("contexts", {})
                            if context_anion and context_anion in contexts:
                                u_val = contexts[context_anion].get("U", 0.0)
                                j_val = contexts[context_anion].get("J", 0.0)
                            else:
                                defaults = info.get("default", info)
                                u_val = defaults.get("U", 0.0)
                                j_val = defaults.get("J", 0.0)
                            if u_val > 0:
                                needs_u = True
                        else:
                            l_val, u_val, j_val = -1, 0.0, 0.0
                        l_list.append(str(l_val))
                        u_list.append(str(u_val))
                        j_list.append(str(j_val))

                    if needs_u:
                        incar_flat["LDAU"] = ".TRUE."
                        incar_flat["LDAUTYPE"] = 2
                        incar_flat["LDAUL"] = " ".join(l_list)
                        incar_flat["LDAUU"] = " ".join(u_list)
                        incar_flat["LDAUJ"] = " ".join(j_list)
                        incar_flat["LMAXMIX"] = 6 if has_f_electron else 4
            except Exception:
                pass  # Fallback failure must not block INCAR generation.

        # Task-type specialization
        if task_type == "bulk_opt":
            incar_flat.update({"ISIF": 3, "EDIFFG": -0.03, "NSW": 300})
        elif task_type == "slab_opt":
            incar_flat.update({"ISIF": 2})
        elif task_type == "mol_opt":
            incar_flat.update({"ISYM": 0})
        elif task_type == "scf":
            incar_flat.update(
                {
                    "IBRION": -1,
                    "NSW": 0,
                    "NELM": 200,
                    "LORBIT": 11,
                    "ISMEAR": 0,
                    "SIGMA": 0.05,
                    "LCHARG": ".TRUE.",
                    "LWAVE": ".TRUE.",
                    "LAECHG": ".TRUE.",
                    "EDIFF": "1E-6",
                    "ALGO": "Normal",
                }
            )
        elif task_type == "dos_calc":
            # Non-SCF DOS: read existing CHGCAR and skip SCF iterations.
            incar_flat.update(
                {
                    "ICHARG": 11,
                    "IBRION": -1,
                    "NSW": 0,
                    "NELM": 200,
                    "LORBIT": 11,
                    "NEDOS": 3000,
                    "ISMEAR": 0,
                    "SIGMA": 0.05,  # Gaussian smearing.
                    "LCHARG": ".F.",
                    "LWAVE": ".F.",
                    "EDIFF": "1E-6",
                    "ALGO": "Normal",
                }
            )

        # Remove ionic-relaxation-only parameters for non-ionic tasks.
        if task_type in ("scf", "dos_calc"):
            for incar_key in ("EDIFFG", "ISIF"):
                incar_flat.pop(incar_key, None)

        if is_charged:
            incar_flat.update({"LDIPOL": ".TRUE.", "IDIPOL": 4})

        # ParamWorker overrides (e.g., +U, specific ENCUT, NCORE/KPAR).
        # Normalize keys to uppercase to avoid duplicate logical parameters.
        if custom_params and isinstance(custom_params, dict):
            custom_params = {k.upper(): v for k, v in custom_params.items()}
            incar_flat.update(custom_params)

        # Mixed-metal slabs are chemically low-symmetry even when their lattice is ideal.
        # Disable symmetry only for this class; single-metal slabs keep the standard default.
        if task_type == "slab_opt" and len(metal_elements) > 1:
            incar_flat["ISYM"] = 0

        # Post-processing: enforce parameter consistency
        # 3.5a Validate LDAU array lengths against number of POSCAR species.
        if incar_flat.get("LDAU") == ".TRUE.":
            n_species = len(element_order)
            ldau_keys = ["LDAUL", "LDAUU", "LDAUJ"]
            needs_rebuild = False
            for key in ldau_keys:
                val = incar_flat.get(key, "")
                if isinstance(val, str) and len(val.split()) != n_species:
                    needs_rebuild = True
                    break

            if needs_rebuild:
                # Rebuild LDAU arrays using complete POSCAR element order.
                try:
                    u_db_fallback = load_hubbard_u_info()

                    if u_db_fallback:
                        fb_anions = [el for el in element_order if el in anion_symbols]
                        fb_context = fb_anions[0] if fb_anions else None
                        fb_l, fb_u, fb_j = [], [], []
                        fb_has_f = False
                        for el in element_order:
                            if el in u_db_fallback:
                                info = u_db_fallback[el]
                                l_v = info.get("L", -1)
                                if l_v == 3:
                                    fb_has_f = True
                                contexts = info.get("contexts", {})
                                if fb_context and fb_context in contexts:
                                    u_v = contexts[fb_context].get("U", 0.0)
                                    j_v = contexts[fb_context].get("J", 0.0)
                                else:
                                    defaults = info.get("default", info)
                                    u_v = defaults.get("U", 0.0)
                                    j_v = defaults.get("J", 0.0)
                            else:
                                l_v, u_v, j_v = -1, 0.0, 0.0
                            fb_l.append(str(l_v))
                            fb_u.append(str(u_v))
                            fb_j.append(str(j_v))
                        incar_flat["LDAUL"] = " ".join(fb_l)
                        incar_flat["LDAUU"] = " ".join(fb_u)
                        incar_flat["LDAUJ"] = " ".join(fb_j)
                        incar_flat["LMAXMIX"] = 6 if fb_has_f else 4
                except Exception:
                    pass  # Keep original values if rebuild fails.

        # 3.5b Validate MAGMOM length and normalize compression format.
        natoms = len(struct)
        if "MAGMOM" in incar_flat and isinstance(incar_flat["MAGMOM"], str):
            magmom_count = count_magmom_atoms(incar_flat["MAGMOM"])
            if magmom_count != natoms:
                # Rebuild from auto-detected defaults when atom count mismatches.
                incar_flat["MAGMOM"] = magmom_str
            else:
                # Ensure compressed format.
                incar_flat["MAGMOM"] = compress_magmom(incar_flat["MAGMOM"])

        # 3.5c Nonmagnetic fallback: only when ParamWorker did not explicitly set ISPIN.
        explicit_ispin = custom_params is not None and isinstance(custom_params, dict) and "ISPIN" in custom_params
        if not has_magnetic_element and not explicit_ispin:
            incar_flat["ISPIN"] = 1

        # 3.5d Remove MAGMOM when final ISPIN != 2 to avoid inconsistency.
        if incar_flat.get("ISPIN", 2) != 2 and "MAGMOM" in incar_flat:
            del incar_flat["MAGMOM"]

        # Write INCAR in logical blocks
        block_keys = {
            "read_in": ["ISTART", "ICHARG", "INIWAV"],
            "accuracy": [
                "PREC",
                "LREAL",
                "ENCUT",
                "EDIFFG",
                "EDIFF",
                "POTIM",
                "ISPIN",
                "MAGMOM",
                "ISMEAR",
                "SIGMA",
                "IVDW",
                "LDIPOL",
                "IDIPOL",
                "GGA",
                "NGXF",
                "NGYF",
                "NGZF",
            ],
            "dft_plus_u": ["LDAU", "LDAUTYPE", "LDAUL", "LDAUU", "LDAUJ", "LMAXMIX", "LDAUPRINT"],
            "optimization": ["IBRION", "ISIF", "NSW", "NELM", "ALGO", "IALGO", "ISYM"],
            "calculation_resource": ["NCORE", "KPAR", "NPAR"],
            "output": ["LVTOT", "LAECHG", "LELF", "LCHARG", "LWAVE", "LORBIT"],
        }

        with open(output_path, "w", encoding="utf-8") as f:
            # Header
            f.write(f"SYSTEM = {incar_flat.pop('SYSTEM', 'Auto_VASP')}\n\n")

            # Predefined blocks
            for block_name, keys in block_keys.items():
                block_dict = {k: incar_flat.pop(k) for k in keys if k in incar_flat}
                if block_dict:
                    f.write(f"#{block_name}\n")
                    for k, v in block_dict.items():
                        f.write(f"{k: <10} = {v}\n")
                    f.write("\n")

            # Remaining uncategorized parameters
            if incar_flat:
                f.write("#other_params\n")
                for k, v in incar_flat.items():
                    f.write(f"{k: <10} = {v}\n")
                f.write("\n")

        final_ispin = (
            custom_params.get("ISPIN", 2 if has_magnetic_element else 1)
            if custom_params
            else (2 if has_magnetic_element else 1)
        )
        final_ncore = custom_params.get("NCORE", 1) if custom_params else 1
        final_kpar = custom_params.get("KPAR", 1) if custom_params else 1
        task_info = f"{task_type}" + (f" (raw input: {raw_task_type})" if raw_task_type != task_type else "")

        return f"INCAR generated successfully ({task_info}). Spin-polarization detection: {'enabled (ISPIN=2)' if final_ispin == 2 else 'disabled (ISPIN=1)'}. Parallel settings: NCORE={final_ncore}, KPAR={final_kpar}."

    except Exception as e:
        return f"INCAR generation failed: {str(e)}"
