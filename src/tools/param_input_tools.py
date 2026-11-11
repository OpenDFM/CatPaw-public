import json
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

from openhands.sdk import Action, Observation, TextContent, Tool, ToolDefinition
from openhands.sdk.tool import ToolExecutor, register_tool
from pydantic import Field


class ParamInputToolObservation(Observation):
    message: str = Field(default="")
    success: bool | None = Field(default=None)
    output_path: str | None = Field(default=None)
    final_params: dict[str, str] = Field(default_factory=dict)

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        return [TextContent(text=self.message)]


def parse_incar_params(path: Path) -> dict[str, str]:
    params: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        clean = line.split("#", 1)[0].strip()
        if not clean or "=" not in clean:
            continue
        key, value = clean.split("=", 1)
        params[key.strip().upper()] = value.strip()
    return params


class CreateIncarAction(Action):
    working_dir: str = Field(
        description="Absolute path of the current subdirectory; all file operations are executed here."
    )
    poscar_path: str = Field(default="POSCAR", description="Input POSCAR path (relative to working_dir or absolute).")
    task_type: str = Field(
        default="slab_opt", description="Task type: 'bulk_opt', 'slab_opt', 'mol_opt', 'scf', 'dos_calc'."
    )
    is_charged: bool = Field(default=False, description="Whether the system is charged.")
    custom_params: dict = Field(
        default_factory=dict,
        description="Advanced parameter override dictionary, e.g. {'ENCUT': 520, 'LDAU': '.TRUE.', 'NCORE': 8}.",
    )
    output_path: str = Field(default="INCAR", description="Output INCAR path (relative to working_dir or absolute).")


class CreateIncarExecutor(ToolExecutor[CreateIncarAction, ParamInputToolObservation]):
    def __call__(self, action: CreateIncarAction, conversation=None) -> ParamInputToolObservation:
        try:
            effective_dir = Path(action.working_dir)
            poscar_path = Path(action.poscar_path)
            output_path = Path(action.output_path)
            abs_poscar = poscar_path if poscar_path.is_absolute() else effective_dir / poscar_path
            abs_out = output_path if output_path.is_absolute() else effective_dir / output_path

            from src.tools.param_input_plugins.create_INCAR import generate_smart_incar

            result_msg = generate_smart_incar(
                poscar_path=str(abs_poscar),
                task_type=action.task_type,
                is_charged=action.is_charged,
                custom_params=action.custom_params,
                output_path=str(abs_out),
            )
            success = abs_out.is_file() and "failed" not in result_msg.lower()
            final_params = parse_incar_params(abs_out) if success else {}
            if success:
                result_msg = (
                    f"{result_msg}\n"
                    f"Output path: {abs_out}\n"
                    f"Final INCAR parameters from generated file: "
                    f"{json.dumps(final_params, sort_keys=True)}"
                )
            return ParamInputToolObservation(
                message=result_msg,
                success=success,
                output_path=str(abs_out),
                final_params=final_params,
            )
        except Exception as e:
            return ParamInputToolObservation(
                message=f"INCAR generation failed: {str(e)}",
                success=False,
                output_path=str(abs_out) if "abs_out" in locals() else None,
                final_params={},
            )


class CreateIncarTool(ToolDefinition[CreateIncarAction, ParamInputToolObservation]):
    """Generate INCAR from task type, with support for ParamWorker advanced overrides via custom_params."""

    name: ClassVar[str] = "CreateIncar"

    DESCRIPTION: ClassVar[str] = "Generate an INCAR file from task settings."

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=CreateIncarAction,
                observation_type=ParamInputToolObservation,
                executor=CreateIncarExecutor(),
            )
        ]


class CreateKpointsAction(Action):
    working_dir: str = Field(
        description="Absolute path of the current subdirectory; all file operations are executed here."
    )
    poscar_path: str = Field(default="POSCAR", description="Input POSCAR path (relative to working_dir or absolute).")
    task_type: str | None = Field(
        default=None,
        description="Task type, e.g. 'dos_calc'. For DOS runs, the tool increases k-point density automatically.",
    )
    custom_grid: list[int] | None = Field(
        default=None,
        description="Force a specific k-point grid, e.g. [5, 5, 1]. If omitted, the grid is inferred from lattice constants.",
    )
    output_path: str = Field(
        default="KPOINTS", description="Output KPOINTS path (relative to working_dir or absolute)."
    )


class CreateKpointsExecutor(ToolExecutor[CreateKpointsAction, ParamInputToolObservation]):
    def __call__(self, action: CreateKpointsAction, conversation=None) -> ParamInputToolObservation:
        try:
            effective_dir = Path(action.working_dir)
            poscar_path = Path(action.poscar_path)
            output_path = Path(action.output_path)
            abs_poscar = poscar_path if poscar_path.is_absolute() else effective_dir / poscar_path
            abs_out = output_path if output_path.is_absolute() else effective_dir / output_path

            from src.tools.param_input_plugins.create_KPOINTS import (
                generate_smart_kpoints,
            )

            result_msg = generate_smart_kpoints(
                poscar_path=str(abs_poscar),
                task_type=action.task_type,
                custom_grid=action.custom_grid,
                output_path=str(abs_out),
            )
            return ParamInputToolObservation(message=result_msg)
        except Exception as e:
            return ParamInputToolObservation(message=f"KPOINTS generation failed: {str(e)}")


class CreateKpointsTool(ToolDefinition[CreateKpointsAction, ParamInputToolObservation]):
    """Generate KPOINTS from lattice constants and vacuum geometry, with optional ParamWorker-enforced custom_grid override."""

    name: ClassVar[str] = "CreateKpoints"

    DESCRIPTION: ClassVar[str] = "Generate a KPOINTS file for the current structure."

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=CreateKpointsAction,
                observation_type=ParamInputToolObservation,
                executor=CreateKpointsExecutor(),
            )
        ]


class CreatePotcarAction(Action):
    working_dir: str = Field(
        description="Absolute path of the current subdirectory; all file operations are executed here."
    )
    poscar_path: str = Field(default="POSCAR", description="Input POSCAR path (relative to working_dir or absolute).")
    potcar_mapping: dict = Field(
        default_factory=dict, description="Custom pseudopotential mapping, e.g. {'Fe': 'Fe_pv', 'Ti': 'Ti_sv'}."
    )
    output_path: str = Field(default="POTCAR", description="Output POTCAR path (relative to working_dir or absolute).")


class CreatePotcarExecutor(ToolExecutor[CreatePotcarAction, ParamInputToolObservation]):
    def __call__(self, action: CreatePotcarAction, conversation=None) -> ParamInputToolObservation:
        try:
            effective_dir = Path(action.working_dir)
            poscar_path = Path(action.poscar_path)
            output_path = Path(action.output_path)
            abs_poscar = poscar_path if poscar_path.is_absolute() else effective_dir / poscar_path
            abs_out = output_path if output_path.is_absolute() else effective_dir / output_path

            from src.tools.param_input_plugins.create_POTCAR import (
                generate_smart_potcar,
            )

            result_msg = generate_smart_potcar(
                poscar_path=str(abs_poscar),
                potcar_mapping=action.potcar_mapping,
                output_path=str(abs_out),
            )
            return ParamInputToolObservation(message=result_msg)
        except Exception as e:
            return ParamInputToolObservation(message=f"POTCAR generation failed: {str(e)}")


class CreatePotcarTool(ToolDefinition[CreatePotcarAction, ParamInputToolObservation]):
    """Assemble POTCAR strictly following the POSCAR element order, with optional ParamWorker mapping overrides."""

    name: ClassVar[str] = "CreatePotcar"

    DESCRIPTION: ClassVar[str] = "Assemble a POTCAR file using the selected pseudopotentials."

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=CreatePotcarAction,
                observation_type=ParamInputToolObservation,
                executor=CreatePotcarExecutor(),
            )
        ]


register_tool("CreateIncar", CreateIncarTool)
register_tool("CreateKpoints", CreateKpointsTool)
register_tool("CreatePotcar", CreatePotcarTool)


def get_param_input_tools_config() -> list[Tool]:
    """Native tools for generating VASP input files."""
    return [
        Tool(name="CreateIncar"),
        Tool(name="CreateKpoints"),
        Tool(name="CreatePotcar"),
    ]
