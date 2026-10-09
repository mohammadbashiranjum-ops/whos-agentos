"""On-demand fan-out to a gate-verified WHOS Hatchet runnable and LibreFang agents."""

from __future__ import annotations

import json
from typing import Any

from agno.workflow.step import Step, StepInput, StepOutput
from agno.workflow.workflow import Workflow
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.integrations.parallel_execution import (
    IntegrationConfigurationError,
    run_parallel_execution,
    validate_whos_handoff,
)
from db import get_postgres_db


class ParallelExecutionInput(BaseModel):
    """Inputs are narrow: an exact WHOS handoff plus a caller-controlled agent prompt."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(
        min_length=1,
        max_length=12_000,
        description=(
            "Forwarded unchanged to the configured LibreFang agents. Their message endpoint runs a full agent turn; "
            "AgentOS does not enforce read-only behavior or restrict target-agent tools."
        ),
    )
    hatchet_payload: dict[str, Any]

    @field_validator("hatchet_payload")
    @classmethod
    def validate_handoff(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_whos_handoff(value)


async def parallel_execution_step(step_input: StepInput) -> StepOutput:
    """Run both configured provider branches once; errors never auto-retry paid calls."""
    try:
        request = ParallelExecutionInput.model_validate(step_input.input)
        result = await run_parallel_execution(
            message=request.message,
            hatchet_payload=request.hatchet_payload,
        )
    except IntegrationConfigurationError as exc:
        result = {
            "status": "blocked",
            "dispatch_attempted": False,
            "reason": exc.reason,
            "missing_configuration": list(exc.missing),
        }
    except ValidationError as exc:
        fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors(include_input=False)})
        result = {
            "status": "rejected",
            "dispatch_attempted": False,
            "reason": "Input does not match the WHOS parallel-execution contract.",
            "invalid_fields": fields,
        }
    except ValueError as exc:
        # The handoff validator reports only field names and shape problems, never values.
        result = {"status": "rejected", "dispatch_attempted": False, "reason": str(exc)}
    except Exception as exc:  # never include provider exception text or request contents
        result = {
            "status": "failed",
            "dispatch_attempted": False,
            "reason": f"Unexpected integration failure ({type(exc).__name__}).",
        }

    succeeded = result.get("status") == "completed"
    return StepOutput(content=json.dumps(result, ensure_ascii=False, sort_keys=True), success=succeeded)


parallel_execution = Workflow(
    id="parallel-execution",
    name="Parallel Execution",
    description=(
        "On demand, dispatch one authorized WHOS handoff to a configured Hatchet runnable "
        "and forward the caller-supplied message to allowlisted LibreFang agents. Agent tool permissions "
        "are controlled in LibreFang; this workflow does not enforce read-only behavior."
    ),
    db=get_postgres_db(),
    input_schema=ParallelExecutionInput,
    # Paid/side-effecting provider calls are never retried automatically.
    steps=[Step(name="parallel-execution", executor=parallel_execution_step, max_retries=0)],
)
