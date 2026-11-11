from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar

from openhands.sdk import Action, Observation, TextContent, ToolDefinition
from openhands.sdk.tool import Tool, ToolExecutor, register_tool
from pydantic import Field


def resolve_paths(workspace_dir: str, input_poscar: str, output_poscar: str) -> tuple[Path, Path, str]:
    """Resolve input/output POSCAR paths, warn if relative paths are used.

    Returns (in_path, out_path, warning_prefix).
    Absolute paths pass through unchanged; relative paths are resolved against
    workspace_dir but generate a warning since the workspace is the *parent*
    directory, not the ModelingWorker subdirectory.
    """
    warning = ""
    in_path = Path(input_poscar)
    out_path = Path(output_poscar)

    if not in_path.is_absolute():
        in_path = Path(workspace_dir) / input_poscar
        warning += (
            f'[Warning] input_poscar uses a relative path "{input_poscar}"; '
            f'resolved to "{in_path}". This may point to the parent directory rather than your subdirectory. '
            f"Please confirm the tool input and use an absolute path.\n"
        )
    if not out_path.is_absolute():
        out_path = Path(workspace_dir) / output_poscar
        warning += (
            f'[Warning] output_poscar uses a relative path "{output_poscar}"; '
            f'resolved to "{out_path}". Output may be written to the parent directory rather than your subdirectory. '
            f"Please use an absolute path.\n"
        )
    if warning:
        warning += "\n"
    return in_path, out_path, warning


def infer_text_status(message: str) -> str:
    if message.startswith("Note:"):
        return "needs_user_action"
    if message.startswith("[Error]") or message.startswith("Error:") or "failed" in message[:24].lower():
        return "error"
    return "success"


def should_skip_validation(result_msg: Any, output_path: Path) -> bool:
    """Skip validation when the plugin reported a non-success outcome or produced no file."""
    if not output_path.exists():
        return True
    if isinstance(result_msg, dict):
        return result_msg.get("status") in {"error", "needs_user_action"}
    return result_msg.startswith("Error:") or result_msg.startswith("Note:")


def normalize_plugin_result(result: Any) -> tuple[str, str, dict[str, Any]]:
    if isinstance(result, dict):
        payload = dict(result)
        status = str(payload.pop("status", "success"))
        message = str(payload.pop("message", ""))
        return status, message, payload
    message = str(result)
    return infer_text_status(message), message, {}


class VaspToolObservation(Observation):
    """Generic return object for VASP tools."""

    message: str = Field(default="")
    status: str = Field(default="success")
    operation: str | None = Field(default=None)
    output_path: str | None = Field(default=None)
    produced_file: bool | None = Field(default=None)
    validation_passed: bool | None = Field(default=None)
    validation_errors: list[str] = Field(default_factory=list)
    validation_warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        return [TextContent(text=self.message)]


class AutoPlaceAdsorbateAction(Action):
    input_poscar: str = Field(description="Input slab POSCAR path.")
    adsorbate_input: str = Field(
        description="Adsorbate molecule input, supporting common inorganic formulas (e.g., CO, OH) or SMILES for complex organics."
    )
    site_type: str = Field(default="ontop", description="Adsorption site type: 'ontop', 'bridge', or 'hollow'.")
    site_index: int = Field(
        default=0,
        description="Site index under the same site_type (0-based). Useful for placing multiple adsorbates at different sites, e.g. dissociative adsorption. Available sites and indices are reported by the tool.",
    )
    height: float = Field(default=1.5, description="Initial adsorbate height above the surface (A).")
    anchor_element: str | None = Field(
        default=None, description="Anchor atom element symbol for complex SMILES molecules (e.g., 'O', 'N')."
    )
    target_element: str | None = Field(
        default=None,
        description="Filter ontop sites to those above surface atoms of this element (e.g., 'Mn', 'Ti'). Only effective for site_type='ontop'.",
    )
    target_cn: int | None = Field(
        default=None,
        description="Further filter ontop sites by slab coordination number (e.g., 5 for undercoordinated metal). Only effective for site_type='ontop'.",
    )
    substrate_poscar: str | None = Field(
        default=None,
        description="Clean substrate reference in the same cell/frame. Required when input already contains adsorbates; site search and CN use only substrate atoms. Indices in site evidence refer to this file (1-based).",
    )
    output_poscar: str = Field(default="POSCAR_adsorbed", description="Output path.")


class AutoPlaceAdsorbateExecutor(ToolExecutor[AutoPlaceAdsorbateAction, VaspToolObservation]):
    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir

    def __call__(self, action: AutoPlaceAdsorbateAction, conversation=None) -> VaspToolObservation:
        from src.tools.modeler_plugins.adsorption import auto_place_adsorbate
        from src.tools.modeling.validation import (
            format_validation_report,
            validate_structure,
        )

        in_path, out_path, warning = resolve_paths(self.workspace_dir, action.input_poscar, action.output_poscar)
        if not in_path.exists():
            return VaspToolObservation(
                message=f"[Error] Input file does not exist: {in_path}\n"
                f"Please verify the absolute path specified in the tool input.",
                status="error",
                operation="adsorption",
                output_path=str(out_path),
                produced_file=False,
            )
        try:
            from pymatgen.io.vasp import Poscar

            substrate_path = None
            if action.substrate_poscar:
                substrate_path = Path(action.substrate_poscar)
                if not substrate_path.is_absolute():
                    substrate_path = Path(self.workspace_dir) / substrate_path
            # Snapshot before writing: input and output may be the same POSCAR.
            substrate = Poscar.from_file(substrate_path or in_path, check_for_potcar=False).structure
            pre_atom_count = len(substrate)
            result_payload = auto_place_adsorbate(
                str(in_path),
                action.adsorbate_input,
                action.site_type,
                action.site_index,
                action.height,
                action.anchor_element,
                str(out_path),
                target_element=action.target_element,
                target_cn=action.target_cn,
                substrate_poscar=str(substrate_path) if substrate_path is not None else None,
            )
            status, result_msg, metadata = normalize_plugin_result(result_payload)
            if should_skip_validation(result_payload, out_path):
                return VaspToolObservation(
                    message=f"{warning}{result_msg}",
                    status=status,
                    operation="adsorption",
                    output_path=str(out_path),
                    produced_file=out_path.exists(),
                    metadata=metadata,
                )

            val = validate_structure(
                str(out_path),
                operation_type="adsorption",
                pre_atom_count=pre_atom_count,
                slab_structure=substrate,
            )
            selected_site = metadata.get("selected_site")
            if selected_site:
                import json

                result_msg += "\nSelected substrate site evidence (before adsorption): " + json.dumps(selected_site)
            val_report = format_validation_report(val, out_path.name)
            final_status = status
            if not val["passed"] and status in {"success", "guarded_fallback"}:
                final_status = "validation_failed"
            return VaspToolObservation(
                message=f"{warning}{result_msg}\n\n{val_report}",
                status=final_status,
                operation="adsorption",
                output_path=str(out_path),
                produced_file=out_path.exists(),
                validation_passed=val["passed"],
                validation_errors=val["errors"],
                validation_warnings=val["warnings"],
                metadata=metadata,
            )
        except Exception as e:
            return VaspToolObservation(
                message=f"Molecular adsorption failed: {str(e)}",
                status="error",
                operation="adsorption",
                output_path=str(out_path),
                produced_file=out_path.exists(),
            )


class AutoPlaceAdsorbateTool(ToolDefinition[AutoPlaceAdsorbateAction, VaspToolObservation]):
    """Automatically place a molecule/group at a selected slab site, including lightweight CHGNet pre-relaxation."""

    name: ClassVar[str] = "AutoPlaceAdsorbate"
    DESCRIPTION: ClassVar[str] = "Place an adsorbate on a slab at a selected site."

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=AutoPlaceAdsorbateAction,
                observation_type=VaspToolObservation,
                executor=AutoPlaceAdsorbateExecutor(workspace_dir=conv_state.workspace.working_dir),
            )
        ]


class ModifySurfaceDefectsAction(Action):
    input_poscar: str = Field(description="Input slab POSCAR path.")
    defect_type: str = Field(description="Defect type, e.g. 'vacancy' or 'doping' (subject to backend script support).")
    target_element: str = Field(description="Target element for operation, e.g. 'O' to create oxygen vacancies.")
    dopant_element: str | None = Field(
        default=None, description="For doping operations, replacement element symbol, e.g. 'N'."
    )
    surface_side: str = Field(default="top", description="Surface side for operation: 'top' or 'bottom'.")
    coordination_number: int | None = Field(
        default=None,
        description="Filter target atoms by chemical environment (coordination number). Typical values: 2 = bridging O (e.g., rutile TiO2(110)), 3 = in-plane threefold O. If omitted, only z-height is used for top-layer selection, which may select unintended sites.",
    )
    output_poscar: str = Field(default="POSCAR_defect", description="Output path.")


class ModifySurfaceDefectsExecutor(ToolExecutor[ModifySurfaceDefectsAction, VaspToolObservation]):
    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir

    def __call__(self, action: ModifySurfaceDefectsAction, conversation=None) -> VaspToolObservation:
        from src.tools.modeler_plugins.defect_and_doping import modify_surface_defects
        from src.tools.modeling.validation import (
            format_validation_report,
            validate_structure,
        )

        in_path, out_path, warning = resolve_paths(self.workspace_dir, action.input_poscar, action.output_poscar)
        if not in_path.exists():
            return VaspToolObservation(
                message=f"[Error] Input file does not exist: {in_path}\n"
                f"Please verify the absolute path specified in the tool input.",
                status="error",
                operation=action.defect_type,
                output_path=str(out_path),
                produced_file=False,
            )
        try:
            from pymatgen.core import Structure

            try:
                pre_atom_count = len(Structure.from_file(str(in_path)))
            except Exception:
                pre_atom_count = None

            result_payload = modify_surface_defects(
                str(in_path),
                action.defect_type,
                action.target_element,
                action.dopant_element,
                action.surface_side,
                str(out_path),
                coordination_number=action.coordination_number,
            )
            status, result_msg, metadata = normalize_plugin_result(result_payload)
            if should_skip_validation(result_payload, out_path):
                return VaspToolObservation(
                    message=f"{warning}{result_msg}",
                    status=status,
                    operation=action.defect_type,
                    output_path=str(out_path),
                    produced_file=out_path.exists(),
                    metadata=metadata,
                )

            val = validate_structure(
                str(out_path),
                operation_type=action.defect_type,
                pre_atom_count=pre_atom_count,
            )
            val_report = format_validation_report(val, out_path.name)
            final_status = status
            if not val["passed"] and status == "success":
                final_status = "validation_failed"
            return VaspToolObservation(
                message=f"{warning}{result_msg}\n\n{val_report}",
                status=final_status,
                operation=action.defect_type,
                output_path=str(out_path),
                produced_file=out_path.exists(),
                validation_passed=val["passed"],
                validation_errors=val["errors"],
                validation_warnings=val["warnings"],
                metadata=metadata,
            )
        except Exception as e:
            return VaspToolObservation(
                message=f"Surface-defect construction failed: {str(e)}",
                status="error",
                operation=action.defect_type,
                output_path=str(out_path),
                produced_file=out_path.exists(),
            )


class ModifySurfaceDefectsTool(ToolDefinition[ModifySurfaceDefectsAction, VaspToolObservation]):
    """Create vacancies or substitutional dopants for selected elements on slab surfaces or in bulk supercells."""

    name: ClassVar[str] = "ModifySurfaceDefects"
    DESCRIPTION: ClassVar[str] = (
        "Create a vacancy or substitutional dopant on a slab surface or within a bulk supercell."
    )

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=ModifySurfaceDefectsAction,
                observation_type=VaspToolObservation,
                executor=ModifySurfaceDefectsExecutor(workspace_dir=conv_state.workspace.working_dir),
            )
        ]


class BuildMoleculeInBoxAction(Action):
    molecule_input: str = Field(
        description="Molecule identifier: common abbreviation, SMILES string, or English common name."
    )
    box_size: float = Field(default=15.0, description="Cubic box edge length (A).")
    output_poscar: str = Field(default="POSCAR_molecule", description="Output path.")


class BuildMoleculeInBoxExecutor(ToolExecutor[BuildMoleculeInBoxAction, VaspToolObservation]):
    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir

    def __call__(self, action: BuildMoleculeInBoxAction, conversation=None) -> VaspToolObservation:
        from src.tools.modeler_plugins.basic_operations import build_molecule_in_box
        from src.tools.modeling.validation import (
            format_validation_report,
            validate_structure,
        )

        out_path = Path(action.output_poscar)
        warning = ""
        if not out_path.is_absolute():
            out_path = Path(self.workspace_dir) / out_path
            warning = (
                f'[Warning] output_poscar uses a relative path "{action.output_poscar}"; '
                f'resolved to "{out_path}". Output may be written to the parent directory rather than your subdirectory. '
                f"Please use an absolute path.\n\n"
            )
        try:
            result_payload = build_molecule_in_box(action.molecule_input, action.box_size, str(out_path))
            status, result_msg, metadata = normalize_plugin_result(result_payload)
            if should_skip_validation(result_payload, out_path):
                return VaspToolObservation(
                    message=f"{warning}{result_msg}",
                    status=status,
                    operation="molecule",
                    output_path=str(out_path),
                    produced_file=out_path.exists(),
                    metadata=metadata,
                )

            val = validate_structure(str(out_path), operation_type="molecule")
            val_report = format_validation_report(val, out_path.name)
            final_status = status
            if not val["passed"] and status == "success":
                final_status = "validation_failed"
            return VaspToolObservation(
                message=f"{warning}{result_msg}\n\n{val_report}",
                status=final_status,
                operation="molecule",
                output_path=str(out_path),
                produced_file=out_path.exists(),
                validation_passed=val["passed"],
                validation_errors=val["errors"],
                validation_warnings=val["warnings"],
                metadata=metadata,
            )
        except Exception as e:
            return VaspToolObservation(
                message=f"Molecular modeling failed: {str(e)}",
                status="error",
                operation="molecule",
                output_path=str(out_path),
                produced_file=out_path.exists(),
            )


class BuildMoleculeInBoxTool(ToolDefinition[BuildMoleculeInBoxAction, VaspToolObservation]):
    """Build a POSCAR model for an isolated molecule, used for gas-phase reference-energy calculations."""

    name: ClassVar[str] = "BuildMoleculeInBox"
    DESCRIPTION: ClassVar[str] = "Build an isolated molecule in a periodic box."

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=BuildMoleculeInBoxAction,
                observation_type=VaspToolObservation,
                executor=BuildMoleculeInBoxExecutor(workspace_dir=conv_state.workspace.working_dir),
            )
        ]


register_tool("AutoPlaceAdsorbate", AutoPlaceAdsorbateTool)
register_tool("ModifySurfaceDefects", ModifySurfaceDefectsTool)
register_tool("BuildMoleculeInBox", BuildMoleculeInBoxTool)


def get_modeler_tools_config() -> list[Tool]:
    """Unified entry called by modeling_worker.py during agent initialization."""
    return [
        Tool(name="AutoPlaceAdsorbate"),
        Tool(name="ModifySurfaceDefects"),
        Tool(name="BuildMoleculeInBox"),
    ]
