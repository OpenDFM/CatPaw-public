"""Export OpenHands' native spans to Weave through OTLP."""

import base64
import os
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from src.utils.config import load_config
from src.utils.results import write_json


def enabled() -> bool:
    return load_config().get("weave", {}).get("enabled", False)


def initialize() -> None:
    from lmnr import Laminar, LaminarLiteLLMCallback

    if Laminar.is_initialized():
        return
    config = load_config()["weave"]
    token = base64.b64encode(f"api:{config['api_key']}".encode()).decode()
    os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = config.get("endpoint", "https://trace.wandb.ai/otel/v1/traces")
    os.environ["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] = (
        f"Authorization={quote('Basic ' + token)},project_id={quote(config['project'], safe='')}"
    )
    os.environ["OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"] = "http/protobuf"
    Laminar.initialize(instruments=[], force_http=True)
    import litellm

    litellm.callbacks.append(LaminarLiteLLMCallback())


def parent_context() -> str | None:
    if not enabled():
        return None
    from lmnr import Laminar

    return Laminar.serialize_span_context()


@contextmanager
def span(name: str, workspace: str, *, parent: str | None = None):
    if not enabled():
        yield
        return
    initialize()
    from lmnr import Laminar

    context = Laminar.deserialize_span_context(parent) if parent else None
    with Laminar.start_as_current_span(
        name,
        parent_span_context=context,
        session_id=workspace,
        attributes={"catpaw.workspace": workspace},
    ):
        yield


@contextmanager
def workflow(workspace: str):
    if not enabled():
        yield
        return
    from lmnr import Laminar

    config = load_config()["weave"]
    try:
        with span("catpaw.workflow", workspace):
            write_json(
                Path(workspace) / ".weave.json",
                {
                    "project": config["project"],
                    "url": f"https://wandb.ai/{config['project']}/weave",
                },
            )
            yield
    finally:
        Laminar.flush()
