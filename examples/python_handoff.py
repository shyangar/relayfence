"""Run after 'relayfence init' and 'relayfence serve'; no model API needed."""

import json
import uuid
from pathlib import Path

from relayfence.cli import read_token
from relayfence.client import RelayFenceClient
from relayfence.errors import CoordinationError


def main():
    project = "example-" + uuid.uuid4().hex[:10]
    token = read_token(Path(".relayfence/access.token"))
    with RelayFenceClient("http://127.0.0.1:7433", token) as client:

        def mutate(operation, actor="planner", **fields):
            return client.call(
                operation, project=project, actor=actor, request_id=str(uuid.uuid4()), **fields
            )["data"]

        mutate("state_put", key="api/login", expected_version=0, value={"auth": "passkey"})
        mutate(
            "task_create",
            task_id="login-review",
            title="Review login contract",
            inputs=["api/login"],
        )
        claim = mutate("task_claim", actor="reviewer", task_id="login-review")

        # Retain this complete payload and ID for any exact network retry.
        completion = {
            "project": project,
            "actor": "reviewer",
            "request_id": str(uuid.uuid4()),
            "task_id": "login-review",
            "fence": claim["task"]["fence"],
            "lease_token": claim["lease_token"],
            "outputs": [
                {
                    "key": "review/login",
                    "expected_version": 0,
                    "value": {
                        "input_auth": claim["inputs"][0]["value"]["auth"],
                        "note": "Example metadata only; no code was generated or tested.",
                    },
                }
            ],
        }
        result = client.call("task_complete", **completion)
        retry = client.call("task_complete", **completion)
        assert retry["replayed"] and result["data"] == retry["data"]

        mutate(
            "state_put",
            key="api/login",
            expected_version=1,
            value={"auth": "passkey-with-recovery"},
        )
        artifact = client.call("item_get", project=project, key="review/login")["data"]
        assert not artifact["fresh"]
        print(json.dumps({"project": project, "artifact": artifact}, indent=2))

        try:
            mutate(
                "task_create", task_id="downstream", title="Consume review", inputs=["review/login"]
            )
            mutate("task_claim", actor="consumer", task_id="downstream")
        except CoordinationError as exc:
            assert exc.code == "stale_input"
            print("Downstream claim correctly blocked:", exc.code)


if __name__ == "__main__":
    main()
