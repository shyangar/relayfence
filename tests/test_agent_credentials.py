import json

import pytest

from relayfence.agent_credentials import AgentCredentials, mask_secret
from relayfence.agent_inventory import build_inventory


def _provider(auth):
    return {
        "id": "isoft",
        "label": "iSoft",
        "models": [{"id": "deepseek-flash", "label": "DeepSeek Flash"}],
        "auth": auth,
    }


def test_credentials_are_encrypted_masked_and_available_only_for_execution(ledger, tmp_path):
    vault = AgentCredentials(ledger.store, tmp_path / "private")
    secret = "sk-test-secret-123456"
    hint = vault.save("dsh", "isoft", "ISOFT_API_KEY", secret)

    assert hint == "••••••••3456"
    assert secret not in hint
    assert vault.hint("dsh", "isoft", "ISOFT_API_KEY") == hint
    with ledger.store.connection() as db:
        row = db.execute(
            "SELECT ciphertext,hint FROM agent_credentials "
            "WHERE software='dsh' AND provider='isoft' AND name='ISOFT_API_KEY'"
        ).fetchone()
    assert secret not in row["ciphertext"]
    assert secret not in row["hint"]
    assert vault.key_path.is_file()
    assert secret not in vault.key_path.read_text()
    assert vault.environment("dsh", "isoft") == {"ISOFT_API_KEY": secret}
    # Injection restricted to the catalogue-current env name.
    assert vault.environment("dsh", "isoft", {"ISOFT_API_KEY"}) == {"ISOFT_API_KEY": secret}
    assert vault.environment("dsh", "isoft", {"DEEPSEEK_API_KEY"}) == {}
    assert vault.environment("dsh", "isoft", set()) == {}
    assert vault.delete("dsh", "isoft", "ISOFT_API_KEY") is True
    assert vault.hint("dsh", "isoft", "ISOFT_API_KEY") is None
    assert vault.environment("dsh", "isoft") == {}


def test_stored_key_authenticates_provider_without_exposing_plaintext(ledger, tmp_path):
    vault = AgentCredentials(ledger.store, tmp_path / "private")
    vault.save("dsh", "isoft", "ISOFT_API_KEY", "sk-test-secret-123456")
    catalogue = [
        {
            "id": "dsh",
            "label": "DSH",
            "installed": True,
            "providers": [
                _provider(
                    {
                        "kind": "api_key",
                        "authenticated": False,
                        "credential_env": "ISOFT_API_KEY",
                        "source": None,
                        "message": "缺少 ISOFT_API_KEY，请配置密钥",
                    }
                )
            ],
        }
    ]

    vault.annotate(catalogue)
    auth = catalogue[0]["providers"][0]["auth"]
    assert auth["authenticated"] is True
    assert auth["source"] == "database"
    assert auth["stored"] is True
    assert auth["masked"] == "••••••••3456"
    assert "sk-test-secret" not in json.dumps(catalogue)

    inventory = build_inventory(catalogue)
    assert inventory["agents"][0]["auth"]["state"] == "ready"
    assert inventory["agents"][0]["providers"][0]["auth"]["state"] == "authenticated"


def test_removing_stored_key_clears_database_auth_on_reannotation(ledger, tmp_path):
    vault = AgentCredentials(ledger.store, tmp_path / "private")
    catalogue = [
        {
            "id": "dsh",
            "label": "DSH",
            "installed": True,
            "providers": [
                _provider(
                    {
                        "kind": "api_key",
                        "authenticated": False,
                        "credential_env": "ISOFT_API_KEY",
                        "source": None,
                        "message": "缺少 ISOFT_API_KEY，请配置密钥",
                    }
                )
            ],
        }
    ]
    vault.save("dsh", "isoft", "ISOFT_API_KEY", "sk-test-secret-123456")
    vault.annotate(catalogue)
    assert catalogue[0]["providers"][0]["auth"]["authenticated"] is True
    assert catalogue[0]["providers"][0]["auth"]["source"] == "database"

    vault.delete("dsh", "isoft", "ISOFT_API_KEY")
    vault.annotate(catalogue)
    auth = catalogue[0]["providers"][0]["auth"]
    assert auth["authenticated"] is False
    assert auth["source"] is None
    assert auth["stored"] is False
    assert auth["masked"] is None
    assert auth["message"] == "缺少 ISOFT_API_KEY，请配置密钥"


def test_inventory_explains_missing_keys_and_signed_out_accounts():
    catalogue = [
        {
            "id": "mixed",
            "label": "Mixed",
            "installed": True,
            "providers": [
                _provider(
                    {
                        "kind": "api_key",
                        "authenticated": False,
                        "credential_env": "ISOFT_API_KEY",
                        "source": None,
                        "message": "缺少 ISOFT_API_KEY，请配置密钥",
                    }
                ),
                {
                    "id": "account",
                    "label": "Account",
                    "models": [{"id": "model"}],
                    "auth": {
                        "kind": "account",
                        "authenticated": False,
                        "credential_env": None,
                        "source": None,
                        "message": "请先登录",
                    },
                },
            ],
        },
        {"id": "absent", "label": "Absent", "installed": False, "providers": []},
    ]

    inventory = build_inventory(catalogue, discovering=True, scanned_at=123)
    mixed, absent = inventory["agents"]
    assert inventory["discovering"] is True
    assert inventory["scanned_at"] == 123
    assert mixed["auth"]["state"] == "missing_key"
    assert mixed["providers"][0]["auth"]["state"] == "missing_key"
    assert mixed["providers"][1]["auth"]["state"] == "signed_out"
    assert absent["auth"]["state"] == "not_installed"
    assert absent["installed"] is False


@pytest.mark.parametrize(
    "software,provider,name,value",
    [
        ("bad id", "isoft", "ISOFT_API_KEY", "secret"),
        ("dsh", "bad id", "ISOFT_API_KEY", "secret"),
        ("dsh", "isoft", "bad-name", "secret"),
        ("dsh", "isoft", "ISOFT_API_KEY", ""),
        ("dsh", "isoft", "ISOFT_API_KEY", "line\nbreak"),
        ("dsh", "isoft", "RELAYFENCE_TOKEN", "secret"),
    ],
)
def test_credentials_reject_invalid_input(ledger, tmp_path, software, provider, name, value):
    vault = AgentCredentials(ledger.store, tmp_path / "private")
    with pytest.raises(ValueError):
        vault.save(software, provider, name, value)


def test_lost_key_file_does_not_leave_a_provider_looking_authenticated(ledger, tmp_path):
    directory = tmp_path / "private"
    vault = AgentCredentials(ledger.store, directory)
    vault.save("dsh", "isoft", "ISOFT_API_KEY", "sk-test-secret-123456")
    catalogue = [
        {
            "id": "dsh",
            "label": "DSH",
            "installed": True,
            "providers": [
                _provider(
                    {
                        "kind": "api_key",
                        "authenticated": False,
                        "credential_env": "ISOFT_API_KEY",
                        "source": None,
                        "message": "缺少 ISOFT_API_KEY，请配置密钥",
                    }
                )
            ],
        }
    ]
    (directory / "agent-credentials.key").unlink()

    AgentCredentials(ledger.store, directory).annotate(catalogue)

    auth = catalogue[0]["providers"][0]["auth"]
    assert auth["stored"] is True
    assert auth["authenticated"] is False
    assert auth["source"] is None
    assert "无法解密" in auth["message"]


def test_mask_secret_never_returns_the_full_value():
    assert mask_secret("abc") == "••••"
    assert mask_secret("abcd") == "••••cd"
    assert mask_secret("abcdefghijkl") == "••••••••ijkl"
