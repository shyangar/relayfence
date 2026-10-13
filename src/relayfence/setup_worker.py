"""One-time local Codex setup; credentials and commands never enter the shared registry."""

import os
import uuid
from pathlib import Path

from .models import ModelId, Name
from .worker import WorkerConfig, save_private


def setup_codex(client, directory, project, agent_id, model, cwd, executable, sandbox, effort):
    config = WorkerConfig(
        project=project,
        agent_id=agent_id,
        adapter="codex",
        cwd=str(Path(cwd).resolve()),
        executable=executable,
        sandbox=sandbox,
    )
    from pydantic import TypeAdapter

    TypeAdapter(ModelId).validate_python(model)
    TypeAdapter(Name).validate_python(project)
    if not Path(config.cwd).is_dir():
        raise ValueError("Workspace must be an existing directory")
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)

    def mutation(**fields):
        return {
            "project": project,
            "actor": "setup-operator",
            "request_id": uuid.uuid4().hex,
            **fields,
        }

    parameters = {"reasoning_effort": [effort]} if effort else {}

    # Persist mutation envelopes before sending them so an unknown network outcome can be replayed.
    def send(name, payload):
        save_private(directory / (name + "-request.json"), payload)
        response = client.call(name, **payload)
        save_private(directory / (name + "-response.json"), response)
        return response["data"]

    agent = send(
        "agent_put",
        mutation(
            agent_id=agent_id,
            expected_revision=0,
            label=agent_id,
            runtime="codex",
            mode="command",
            models=[{"id": model, "parameters": parameters}],
        ),
    )
    issued = send(
        "agent_token_rotate", mutation(agent_id=agent_id, expected_revision=agent["revision"])
    )
    fd = os.open(directory / "agent.token", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(issued["token"] + "\n")
    role = send(
        "role_put",
        mutation(
            role_id=agent_id,
            expected_revision=0,
            label=agent_id,
            agent_id=agent_id,
            selection={
                "model": model,
                "parameters": {"reasoning_effort": effort} if effort else {},
            },
            instructions="Complete the assigned task in the configured workspace and report actual results.",
            deliverables=["Summary, files changed, checks performed"],
        ),
    )
    save_private(directory / "worker.json", config.model_dump(mode="json", exclude_none=True))
    save_private(
        directory / "dispatcher.json",
        {"workers": [{"config": "worker.json", "token_file": "agent.token"}], "max_parallel": 1},
    )
    return {
        "agent_id": agent_id,
        "role_id": role["role_id"],
        "dispatcher_config": str(directory / "dispatcher.json"),
    }
