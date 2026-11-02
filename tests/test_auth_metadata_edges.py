"""Credential discovery and log metadata must stay bounded and secret-free."""

import json

from relayfence import discovery
from relayfence.storage import safe_details


def test_credential_sources_ignore_comments_empty_values_and_invalid_names(tmp_path, monkeypatch):
    name = "RELAYFENCE_TEST_PROVIDER_KEY"
    monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text(f"# {name}=ignored\n\nexport {name}=''\nOTHER=value\n", encoding="utf-8")
    assert discovery._credential_source(name, env) is None
    env.write_text(f"# Comment\nexport {name}='private-value'\n", encoding="utf-8")
    assert discovery._credential_source(name, None, env) == "dotenv"
    assert discovery._credential_source("BAD-NAME", env) is None
    assert discovery._dotenv_has(env, "BAD-NAME") is False
    assert discovery._dotenv_has(tmp_path / "missing", name) is False
    monkeypatch.setenv(name, "private-environment")
    assert discovery._credential_source(name, env) == "environment"


def test_codex_auth_reads_sources_without_publishing_secret_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert discovery._codex_auth()["authenticated"] is False
    (tmp_path / "auth.json").write_text("{broken", encoding="utf-8")
    assert discovery._codex_auth()["authenticated"] is False
    (tmp_path / ".env").write_text("export OPENAI_API_KEY=private-dotenv", encoding="utf-8")
    metadata = discovery._codex_auth()
    assert metadata["authenticated"] and metadata["source"] == "dotenv"
    assert "private-dotenv" not in json.dumps(metadata)
    (tmp_path / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "private-account"}), encoding="utf-8"
    )
    metadata = discovery._codex_auth()
    assert metadata["source"] == "vendor" and "private-account" not in json.dumps(metadata)


def test_zcode_auth_handles_malformed_and_valid_vendor_files(tmp_path):
    path = tmp_path / "credentials.json"
    assert discovery._zcode_auth(tmp_path)["authenticated"] is False
    path.write_text("{broken", encoding="utf-8")
    assert discovery._zcode_auth(tmp_path)["authenticated"] is False
    path.write_text(json.dumps({"provider:access_token": "private-token"}), encoding="utf-8")
    metadata = discovery._zcode_auth(tmp_path)
    assert metadata["authenticated"] and "private-token" not in json.dumps(metadata)


def test_shared_log_details_redact_nested_keys_and_bound_untrusted_data():
    nested = {"lease_token": "private-lease", "api_key": "private-key", "message": "visible"}
    result = safe_details({"events": [nested], "object": object()})
    assert result["events"] == [{"message": "visible"}]
    assert result["object"] is None
    assert safe_details({"invalid": float("nan")}) == {}
    assert safe_details(["not-an-object"]) == {}
    deep = {"leaf": "text"}
    for _ in range(10):
        deep = {"level": deep}
    bounded = safe_details(deep)
    assert "text" not in json.dumps(bounded)
    assert safe_details({"huge": "a" * 20000})["truncated"]
