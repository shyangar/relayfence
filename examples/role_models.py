"""Run two role/model selections through real HTTP and local subprocesses, without model calls."""

import json
import os
import sys
import uuid
from pathlib import Path

from relayfence.cli import init_directory
from relayfence.client import RelayFenceClient
from relayfence.runner import LocalServer
from relayfence.worker import run_task, save_private


def main():
    directory = Path(".relayfence") / ("roles-demo-" + uuid.uuid4().hex[:10])
    directory = init_directory(directory)
    token = (directory / "access.token").read_text().strip()
    checks = []
    with LocalServer(directory, token) as server, RelayFenceClient(server.url, token) as admin:

        def mutate(operation, **payload):
            return admin.call(
                operation,
                project="roles-demo",
                actor="operator",
                request_id=uuid.uuid4().hex,
                **payload,
            )["data"]

        for role_id, model in [("implement", "demo-fast"), ("review", "demo-deep")]:
            agent_id = "fixture-" + role_id
            mutate(
                "agent_put",
                agent_id=agent_id,
                expected_revision=0,
                label=agent_id,
                runtime="demo-command",
                mode="command",
                capabilities=["demo"],
                models=[
                    {"id": "demo-fast"},
                    {"id": "demo-deep", "parameters": {"effort": ["high"]}},
                ],
            )
            credential = mutate("agent_token_rotate", agent_id=agent_id, expected_revision=1)[
                "token"
            ]
            fd = os.open(
                directory / (agent_id + ".token"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(credential + "\n")
            selection = {
                "model": model,
                "parameters": {"effort": "high"} if role_id == "review" else {},
            }
            mutate(
                "role_put",
                role_id=role_id,
                expected_revision=0,
                label=role_id.title(),
                agent_id=agent_id,
                selection=selection,
                instructions="Demonstrate model delivery; no model API is called.",
                required_capabilities=["demo"],
                deliverables=["Adapter receipt"],
            )
            mutate(
                "task_create",
                task_id=role_id,
                title=role_id.title() + " model selection",
                role_id=role_id,
                inputs=["result/implement"] if role_id == "review" else [],
            )
            config_path = directory / (agent_id + ".json")
            save_private(
                config_path,
                {
                    "project": "roles-demo",
                    "agent_id": agent_id,
                    "command": [
                        sys.executable,
                        str(Path(__file__).with_name("role_command.py").resolve()),
                        "{model}",
                        "{parameters_json}",
                    ],
                },
            )
            with RelayFenceClient(server.url, credential) as client:
                result = run_task(client, config_path, role_id, directory / ("run-" + role_id))
            execution = result["data"]["task"]["execution"]
            assert execution["requested"] == selection and execution["reported_model"] == model
            checks.append(
                {
                    "role": role_id,
                    "agent": agent_id,
                    "requested_model": model,
                    "reported_model": execution["reported_model"],
                    "evidence": execution["model_evidence"],
                    "status": "PASS",
                }
            )
    report = {
        "checks": checks,
        "model_api_calls": 0,
        "vendor_clients_tested": [],
        "directory": str(directory),
    }
    save_private(directory / "roles-report.json", report)
    print(json.dumps(report, indent=2))
    print(f'Inspect: relayfence serve --directory "{directory}" (project: roles-demo)')


if __name__ == "__main__":
    main()
