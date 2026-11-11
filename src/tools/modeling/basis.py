import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

from openhands.sdk import Action, Observation, TextContent, ToolDefinition
from openhands.sdk.tool import Tool, ToolExecutor, register_tool
from pydantic import Field

from src.utils.config import database_dir, load_config, model_library_dir, project_root


class LocateStructureAction(Action):
    formula: str = Field(
        description="Chemical formula to query. Unordered or unreduced forms are accepted (e.g., 'Na2KPO4', 'KNa2PO4', 'O8P2K2Na4')."
    )


class LocateStructureObservation(Observation):
    message: str = Field(default="")

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        return [TextContent(text=self.message)]


class ModelingBasisToolObservation(Observation):
    message: str = Field(default="")

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        return [TextContent(text=self.message)]


class LocateStructureExecutor(ToolExecutor[LocateStructureAction, LocateStructureObservation]):
    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir

    def get_database_dir(self) -> str:
        return str(database_dir())

    def resolve_model_path(self, db_dir: str, raw_path: str | None) -> str:
        if not raw_path:
            return "Unknown"

        db_path = Path(db_dir).resolve()
        root = project_root()
        path = Path(raw_path).expanduser()

        if path.is_absolute():
            if path.exists():
                return str(path)
            local_by_name = model_library_dir() / path.name
            if path.name and local_by_name.exists():
                return str(local_by_name)
            return str(path)

        candidates = [
            root / path,
            db_path / path,
            model_library_dir() / path.name,
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate.resolve())
        return str((root / path).resolve())

    def __call__(self, action: LocateStructureAction, conversation=None) -> LocateStructureObservation:
        from pymatgen.core import Composition

        try:
            comp = Composition(action.formula)
            reduced_formula = comp.reduced_formula
        except Exception as e:
            return LocateStructureObservation(
                message=f"❌ Chemical-formula parsing failed: {str(e)}. Please verify formula validity."
            )

        local_msg = self.search_local(reduced_formula)
        if local_msg is not None:
            mp_hint = self.mp_brief(reduced_formula)
            if mp_hint:
                local_msg = local_msg + "\n\n" + mp_hint
            return LocateStructureObservation(message=local_msg)

        return self.search_materials_project(reduced_formula)

    def search_local(self, formula: str) -> str | None:
        db_dir = self.get_database_dir()
        json_path = Path(db_dir) / "available_models.json"

        if not json_path.exists():
            return None

        try:
            with json_path.open("r", encoding="utf-8") as f:
                local_db = json.load(f)
        except Exception:
            return None

        if formula not in local_db:
            return None

        polymorphs = local_db[formula]
        msg = f"✅ Local high-precision, human-validated database contains {len(polymorphs)} polymorph/structure entry(ies) for reduced formula {formula}:\n\n"

        for idx, poly in enumerate(polymorphs):
            msg += f"[{idx + 1}] Phase: {poly.get('phase', 'Unknown')}\n"
            msg += f"    - Space group: {poly.get('spacegroup', 'Unknown')}\n"
            model_path = self.resolve_model_path(db_dir, poly.get("model_path"))
            msg += f"    - Local path: {model_path}\n"
            if "magnetic_ordering" in poly:
                msg += f"    - Magnetic ordering metadata: {poly.get('magnetic_ordering', 'Unknown')}\n"
            msg += f"    - Expert notes: {poly.get('description', '')}\n\n"

        msg += (
            "[Next-step recommendations]:\n"
            "1. Select the most suitable polymorph for the research objective.\n"
            "2. Use the selected local path directly in the relevant modeler-tool call.\n"
            "3. Record only source provenance and retrieved structural facts in `modeling_summary.json`."
        )
        return msg

    def mp_brief(self, formula: str) -> str | None:
        """Return a short MP summary when local DB already has hits."""
        mp_api_key = load_config().get("materials_project", {}).get("api_key")
        if not mp_api_key:
            return None
        try:
            from mp_api.client import MPRester

            with MPRester(mp_api_key) as mpr:
                docs = mpr.materials.summary.search(
                    formula=formula,
                    theoretical=False,
                    fields=["material_id", "symmetry", "energy_above_hull"],
                )
                if not docs:
                    return None
                docs = sorted(docs, key=lambda d: getattr(d, "energy_above_hull", 999.0))
                lines = [
                    f"ℹ️ Materials Project provides {len(docs)} additional {formula} structure(s) for comparison (local database remains preferred due to human validation):"
                ]
                for d in docs[:3]:
                    sg = d.symmetry.symbol if hasattr(d, "symmetry") else "N/A"
                    lines.append(f"  - {d.material_id}  Space group {sg}")
                if len(docs) > 3:
                    lines.append(f"  (Total {len(docs)}; only the three most stable are listed)")
                lines.append("To use a cloud structure, call `DownloadMPStructure`.")
                return "\n".join(lines)
        except Exception:
            return None

    def search_materials_project(self, formula: str) -> LocateStructureObservation:
        mp_api_key = load_config().get("materials_project", {}).get("api_key")
        if not mp_api_key:
            return LocateStructureObservation(
                message=f"{formula} is not available in the local database, and materials_project.api_key in config.yaml is not configured, so cloud lookup is unavailable. Please contact the administrator."
            )

        try:
            from mp_api.client import MPRester

            with MPRester(mp_api_key) as mpr:
                search_fields = [
                    "symmetry",
                    "material_id",
                    "theoretical",
                    "energy_above_hull",
                    "band_gap",
                    "density",
                    "ordering",
                    "total_magnetization",
                ]

                docs = mpr.materials.summary.search(formula=formula, theoretical=False, fields=search_fields)
                if not docs:
                    docs = mpr.materials.summary.search(formula=formula, is_stable=True, fields=search_fields)

                if not docs:
                    return LocateStructureObservation(
                        message=f"No stable structure for formula {formula} was found in either the local database or Materials Project."
                    )

                def get_hull_energy(doc):
                    val = getattr(doc, "energy_above_hull", None)
                    return val if val is not None else 999.0

                docs = sorted(docs, key=get_hull_energy)

                result = (
                    f"{formula} is not in the local database. {len(docs)} stable structure(s) were retrieved from Materials Project "
                    f"(ranked by energy):\n\n"
                )
                for d in docs[:3]:
                    struct_type = (
                        "theoretical prediction" if getattr(d, "theoretical", False) else "experimentally validated"
                    )
                    energy_val = getattr(d, "energy_above_hull", "N/A")
                    energy_str = f"{energy_val:.4f}" if isinstance(energy_val, (int, float)) else energy_val
                    mag_ordering = getattr(d, "ordering", "Unknown")
                    total_mag = getattr(d, "total_magnetization", "N/A")
                    if isinstance(total_mag, (int, float)):
                        total_mag = f"{total_mag:.3f} µB/cell"

                    result += (
                        f"  MP ID: {d.material_id} [{struct_type}]\n"
                        f"   - Energy above hull: {energy_str} eV/atom\n"
                        f"   - Crystal system: {d.symmetry.crystal_system if hasattr(d, 'symmetry') else 'N/A'}\n"
                        f"   - Space group: {d.symmetry.symbol if hasattr(d, 'symmetry') else 'N/A'} ({d.symmetry.number if hasattr(d, 'symmetry') else 'N/A'})\n"
                        f"   - Theoretical band gap: {getattr(d, 'band_gap', 'N/A')} eV\n"
                        f"   - Magnetic ordering: {mag_ordering}\n"
                        f"   - Total cell magnetization: {total_mag}\n\n"
                    )

                if len(docs) > 3:
                    result += "(Only the three most stable structures are shown.)\n"

                result += (
                    "⚠️ Cloud-structure note: MP parameters (especially magnetic and band-structure related values) may deviate. "
                    "In `modeling_summary.json`, record only that the structure source is cloud-derived plus any retrieved structural facts."
                )

                return LocateStructureObservation(message=result)
        except Exception as e:
            return LocateStructureObservation(
                message=f"{formula} is not in the local database, and Materials Project query failed: {str(e)}"
            )


class LocateStructureTool(ToolDefinition[LocateStructureAction, LocateStructureObservation]):
    """Locate crystal structures: local high-precision database first (with MP overview), then automatic Materials Project fallback."""

    name: ClassVar[str] = "LocateStructure"
    DESCRIPTION: ClassVar[str] = (
        "Locate a crystal structure: local database first (with MP overview), then Materials Project fallback."
    )

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        executor = LocateStructureExecutor(workspace_dir=conv_state.workspace.working_dir)
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=LocateStructureAction,
                observation_type=LocateStructureObservation,
                executor=executor,
            )
        ]


class DownloadMPStructureAction(Action):
    mp_id: str = Field(description="Materials Project material ID (e.g., 'mp-149').")
    output_poscar: str = Field(default="POSCAR_bulk", description="POSCAR output path for downloaded structure.")


class DownloadMPStructureExecutor(ToolExecutor[DownloadMPStructureAction, ModelingBasisToolObservation]):
    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir

    def __call__(self, action: DownloadMPStructureAction, conversation=None) -> ModelingBasisToolObservation:
        from mp_api.client import MPRester
        from pymatgen.io.vasp import Poscar
        from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

        out_path = Path(action.output_poscar)
        if not out_path.is_absolute():
            out_path = Path(self.workspace_dir) / out_path

        api_key = load_config().get("materials_project", {}).get("api_key")
        if not api_key:
            return ModelingBasisToolObservation(
                message="Download failed: materials_project.api_key is not configured in config.yaml."
            )

        try:
            with MPRester(api_key) as mpr:
                primitive_structure = mpr.get_structure_by_material_id(action.mp_id)
                sga = SpacegroupAnalyzer(primitive_structure)
                conventional_structure = sga.get_conventional_standard_structure()
                Poscar(conventional_structure).write_file(str(out_path))
                crystal_system = sga.get_crystal_system()

            return ModelingBasisToolObservation(
                message=(
                    f"✅ Successfully downloaded structure {action.mp_id} from cloud source; "
                    f"already converted to a conventional {crystal_system} cell and saved to {out_path}. "
                    f"This file is READY TO USE — rename or copy it to POSCAR directly. "
                    f"Do NOT recreate, rewrite, or 'clean up' this file."
                )
            )
        except Exception as e:
            return ModelingBasisToolObservation(
                message=f"Failed to download structure from Materials Project: {str(e)}"
            )


class DownloadMPStructureTool(ToolDefinition[DownloadMPStructureAction, ModelingBasisToolObservation]):
    """Download a bulk structure from Materials Project by MP-ID and save as POSCAR."""

    name: ClassVar[str] = "DownloadMPStructure"
    DESCRIPTION: ClassVar[str] = "Download a bulk structure from Materials Project as POSCAR."

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=DownloadMPStructureAction,
                observation_type=ModelingBasisToolObservation,
                executor=DownloadMPStructureExecutor(workspace_dir=conv_state.workspace.working_dir),
            )
        ]


class CleaveSurfaceAction(Action):
    input_bulk_poscar: str = Field(description="Input bulk POSCAR path.")
    miller_index: list[int] = Field(description="Miller index, e.g. [1, 1, 1].")
    num_layers: int = Field(
        default=4,
        description="Number of crystallographic repeats (multiples of d_hkl), not direct atomic-layer count. After cleavage, use reported atomic-layer count and total atom count to decide whether adjustment is needed.",
    )
    vacuum_size: float = Field(
        default=15.0,
        description=(
            "Minimum total vacuum gap between neighboring periodic slabs (A), not padding on each side. "
            "For a centered slab, the bottom and top cell-boundary gaps add up to this gap. "
            "Actual gap may exceed the requested minimum due to crystallographic repeats."
        ),
    )
    termination_element: str | None = Field(
        default=None, description="Desired exposed element symbol at the outermost surface."
    )
    max_repeat_units: int = Field(
        default=6, description="Prefer candidate slabs at or below this inferred repeat-unit count."
    )
    fail_repeat_units: int = Field(
        default=8,
        description="Reject cleavage without writing a slab if all candidates exceed this repeat-unit limit. Raise only for explicitly justified thickness convergence.",
    )
    output_poscar: str = Field(default="POSCAR_slab", description="Output slab POSCAR path.")


class CleaveSurfaceExecutor(ToolExecutor[CleaveSurfaceAction, ModelingBasisToolObservation]):
    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir

    def __call__(self, action: CleaveSurfaceAction, conversation=None) -> ModelingBasisToolObservation:
        workspace_dir = Path(self.workspace_dir)
        in_path = Path(action.input_bulk_poscar)
        out_path = Path(action.output_poscar)
        if not in_path.is_absolute():
            in_path = workspace_dir / in_path
        if not out_path.is_absolute():
            out_path = workspace_dir / out_path
        try:
            from src.tools.modeler_plugins.basic_operations import cleave_surface

            result_msg = cleave_surface(
                input_bulk_poscar=str(in_path),
                miller_index=action.miller_index,
                num_layers=action.num_layers,
                vacuum_size=action.vacuum_size,
                termination_element=action.termination_element,
                output_poscar=str(out_path),
                max_repeat_units=action.max_repeat_units,
                fail_repeat_units=action.fail_repeat_units,
            )
            return ModelingBasisToolObservation(message=result_msg)
        except Exception as e:
            return ModelingBasisToolObservation(message=f"Surface cleavage failed: {str(e)}")


class CleaveSurfaceTool(ToolDefinition[CleaveSurfaceAction, ModelingBasisToolObservation]):
    """Cleave a slab from bulk using specified Miller index, with automatic centering and vacuum addition."""

    name: ClassVar[str] = "CleaveSurface"
    DESCRIPTION: ClassVar[str] = "Cleave a surface slab from a bulk structure."

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=CleaveSurfaceAction,
                observation_type=ModelingBasisToolObservation,
                executor=CleaveSurfaceExecutor(workspace_dir=conv_state.workspace.working_dir),
            )
        ]


class CreateSupercellAction(Action):
    input_poscar: str = Field(description="Input POSCAR path.")
    scaling_matrix: list[int] = Field(description="Supercell scaling matrix, e.g. [2, 2, 1].")
    output_poscar: str = Field(default="POSCAR_supercell", description="Output path.")


class CreateSupercellExecutor(ToolExecutor[CreateSupercellAction, ModelingBasisToolObservation]):
    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir

    def __call__(self, action: CreateSupercellAction, conversation=None) -> ModelingBasisToolObservation:
        workspace_dir = Path(self.workspace_dir)
        in_path = Path(action.input_poscar)
        out_path = Path(action.output_poscar)
        if not in_path.is_absolute():
            in_path = workspace_dir / in_path
        if not out_path.is_absolute():
            out_path = workspace_dir / out_path
        try:
            from src.tools.modeler_plugins.basic_operations import create_supercell

            result_msg = create_supercell(str(in_path), action.scaling_matrix, str(out_path))
            return ModelingBasisToolObservation(message=result_msg)
        except Exception as e:
            return ModelingBasisToolObservation(message=f"Supercell expansion failed: {str(e)}")


class CreateSupercellTool(ToolDefinition[CreateSupercellAction, ModelingBasisToolObservation]):
    """Expand unit cell into a larger supercell for constructing sufficiently large unified surface substrates."""

    name: ClassVar[str] = "CreateSupercell"
    DESCRIPTION: ClassVar[str] = "Expand a structure into a larger supercell."

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=CreateSupercellAction,
                observation_type=ModelingBasisToolObservation,
                executor=CreateSupercellExecutor(workspace_dir=conv_state.workspace.working_dir),
            )
        ]


class AnalyzeSurfaceLayoutAction(Action):
    input_slab_poscar: str = Field(description="Input slab POSCAR path.")
    adsorbates: list[str] = Field(
        default_factory=list,
        description=(
            "Adsorbate list to evaluate on the surface, e.g. ['H'] or ['OH', 'OOH']. "
            "Leave empty for vacancy/doping-only tasks."
        ),
    )
    surface_modifications: list[str] = Field(
        default_factory=list,
        description=(
            "Surface modification types to account for, e.g. ['adsorption'], ['vacancy'], ['doping'], or combinations."
        ),
    )
    min_defect_image_distance: float = Field(
        default=8.0,
        description=(
            "Minimum lateral periodic-image distance in Angstrom for vacancy/doping "
            "planning. Default is 8.0; increase for high-precision tasks if needed."
        ),
    )


class AnalyzeSurfaceLayoutExecutor(ToolExecutor[AnalyzeSurfaceLayoutAction, ModelingBasisToolObservation]):
    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir

    def __call__(self, action: AnalyzeSurfaceLayoutAction, conversation=None) -> ModelingBasisToolObservation:
        slab_path = Path(action.input_slab_poscar)
        if not slab_path.is_absolute():
            slab_path = Path(self.workspace_dir) / slab_path

        adsorbates = [ads.strip() for ads in action.adsorbates if ads.strip()]
        modifications = {mod.strip().lower() for mod in action.surface_modifications if mod.strip()}
        if adsorbates:
            modifications.add("adsorption")

        defect_like_modifications = {
            "defect",
            "vacancy",
            "doping",
            "dopant",
            "substitution",
            "substitutional_doping",
            "atom_replacement",
        }
        needs_defect_spacing = bool(modifications & defect_like_modifications)
        if not adsorbates and not needs_defect_spacing:
            return ModelingBasisToolObservation(
                message=(
                    "Surface-layout analysis failed: provide at least one adsorbate "
                    "or a vacancy/doping-like surface_modifications entry."
                )
            )

        try:
            from pymatgen.core import Structure

            slab = Structure.from_file(str(slab_path))
            a_len = float(slab.lattice.abc[0])
            b_len = float(slab.lattice.abc[1])

            analyses: list[dict] = []
            max_required_lateral = 0.0
            max_required_vacuum = 15.0

            if adsorbates:
                from src.tools.modeler_plugins.adsorption import (
                    analyze_adsorbate_footprint,
                )

                for adsorbate in adsorbates:
                    analysis = analyze_adsorbate_footprint(adsorbate)
                    if "error" in analysis:
                        return ModelingBasisToolObservation(
                            message=f"Surface-layout analysis failed: {analysis['error']}"
                        )
                    analyses.append(analysis)
                    max_required_lateral = max(
                        max_required_lateral,
                        float(analysis["required_lateral_size_A"]),
                    )
                    max_required_vacuum = max(
                        max_required_vacuum,
                        float(analysis["recommended_vacuum_size_A"]),
                    )

            if needs_defect_spacing:
                max_required_lateral = max(
                    max_required_lateral,
                    float(action.min_defect_image_distance),
                )

            scale_a = max(1, math.ceil(max_required_lateral / a_len))
            scale_b = max(1, math.ceil(max_required_lateral / b_len))
            scaling_matrix = [scale_a, scale_b, 1]

            lines = [
                "Surface-layout analysis completed.",
                f"Current in-plane slab lattice lengths: a = {a_len:.2f} A, b = {b_len:.2f} A",
            ]

            if analyses:
                lines.append("Adsorbate size assessment:")
                for item in analyses:
                    lines.append(
                        f"- {item['adsorbate_input']}: maximum diameter {item['max_diameter_A']:.2f} A, "
                        f"recommended minimum lateral size {item['required_lateral_size_A']:.2f} A, "
                        f"recommended vacuum thickness {item['recommended_vacuum_size_A']:.2f} A"
                    )

            if needs_defect_spacing:
                lines.append(
                    "Defect/doping periodic-image assessment: "
                    f"target lateral image distance >= {action.min_defect_image_distance:.2f} A."
                )

            lines.extend(
                [
                    (
                        f"Integrated recommendation: expand a/b to at least {max_required_lateral:.2f} A; "
                        f"recommended scaling_matrix={scaling_matrix}."
                    ),
                    (
                        f"Integrated vacuum recommendation: vacuum_size={max_required_vacuum:.2f} A. "
                        "If this exceeds the current setting, rerun CleaveSurface before bottom-layer fixing. "
                        "For vacancy/doping-only tasks, keep the slab vacuum unless task requirements demand a change. "
                        "If scaling_matrix=[1, 1, 1], no extra supercell expansion is needed."
                    ),
                ]
            )

            return ModelingBasisToolObservation(message="\n".join(lines))
        except Exception as e:
            return ModelingBasisToolObservation(message=f"Surface-layout analysis failed: {str(e)}")


class AnalyzeSurfaceLayoutTool(ToolDefinition[AnalyzeSurfaceLayoutAction, ModelingBasisToolObservation]):
    """Recommend surface supercell and vacuum settings for adsorbates, defects, and dopants."""

    name: ClassVar[str] = "AnalyzeSurfaceLayout"
    DESCRIPTION: ClassVar[str] = (
        "Analyze the current slab and recommend surface supercell/vacuum settings for "
        "adsorbates, vacancies, dopants, and shared surface-family models."
    )

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=AnalyzeSurfaceLayoutAction,
                observation_type=ModelingBasisToolObservation,
                executor=AnalyzeSurfaceLayoutExecutor(workspace_dir=conv_state.workspace.working_dir),
            )
        ]


class FixLayersByElementAction(Action):
    input_poscar: str = Field(description="Input slab POSCAR path.")
    fix_ratio: float = Field(default=0.5, description="Ratio of bottom layers to fix.")
    tolerance: float = Field(default=0.1, description="Z-axis tolerance for layer identification (A).")
    output_poscar: str = Field(default="POSCAR", description="Output path.")


class FixLayersByElementExecutor(ToolExecutor[FixLayersByElementAction, ModelingBasisToolObservation]):
    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir

    def __call__(self, action: FixLayersByElementAction, conversation=None) -> ModelingBasisToolObservation:
        workspace_dir = Path(self.workspace_dir)
        in_path = Path(action.input_poscar)
        out_path = Path(action.output_poscar)
        if not in_path.is_absolute():
            in_path = workspace_dir / in_path
        if not out_path.is_absolute():
            out_path = workspace_dir / out_path
        try:
            from src.tools.modeler_plugins.constraints import fix_layers_by_element

            result_msg = fix_layers_by_element(str(in_path), action.fix_ratio, action.tolerance, str(out_path))
            return ModelingBasisToolObservation(message=result_msg)
        except Exception as e:
            return ModelingBasisToolObservation(message=f"Bottom-layer fixing failed: {str(e)}")


class FixLayersByElementTool(ToolDefinition[FixLayersByElementAction, ModelingBasisToolObservation]):
    """Identify layers by element and fix bottom atoms to generate a unified constrained surface substrate."""

    name: ClassVar[str] = "FixLayersByElement"
    DESCRIPTION: ClassVar[str] = "Fix the bottom layers of a slab by element grouping."

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=FixLayersByElementAction,
                observation_type=ModelingBasisToolObservation,
                executor=FixLayersByElementExecutor(workspace_dir=conv_state.workspace.working_dir),
            )
        ]


register_tool("LocateStructure", LocateStructureTool)
register_tool("DownloadMPStructure", DownloadMPStructureTool)
register_tool("CleaveSurface", CleaveSurfaceTool)
register_tool("AnalyzeSurfaceLayout", AnalyzeSurfaceLayoutTool)
register_tool("CreateSupercell", CreateSupercellTool)
register_tool("FixLayersByElement", FixLayersByElementTool)


def get_modeling_basis_tools_config() -> list[Tool]:
    """
    Basis-preparation tools used by ModelingWorker.
    Returns Tool objects by name; OpenHands invokes their .create(conv_state) methods at runtime.
    """
    return [
        Tool(name="LocateStructure"),
        Tool(name="DownloadMPStructure"),
        Tool(name="CleaveSurface"),
        Tool(name="AnalyzeSurfaceLayout"),
        Tool(name="CreateSupercell"),
        Tool(name="FixLayersByElement"),
    ]
