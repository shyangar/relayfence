"""Deterministic adapter fixture, not an LLM or vendor integration.

The same stdin/stdout contract can wrap a locally authenticated agent CLI.
Only claim/report model identities supported by the real adapter's evidence.
"""

import hashlib
import json
import sys


def main():
    request = json.load(sys.stdin)
    model = sys.argv[1]
    parameters = json.loads(sys.argv[2])
    assert request["selection"] == {"model": model, "parameters": parameters}
    value = {
        "adapter": "deterministic-protocol-fixture",
        "received_model": model,
        "received_parameters": parameters,
        "input_digest": hashlib.sha256(
            json.dumps(request["inputs"], sort_keys=True).encode()
        ).hexdigest(),
    }
    print(
        json.dumps(
            {
                "reported_model": model,
                "outputs": [
                    {
                        "key": "result/" + request["task"]["task_id"],
                        "expected_version": 0,
                        "value": value,
                    }
                ],
            }
        )
    )


if __name__ == "__main__":
    main()
