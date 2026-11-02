import pytest
from starlette.testclient import TestClient

from relayfence.local_session import BrowserSessions
from relayfence.server import create_app

TOKEN = "t" * 40
ORIGIN = "http://127.0.0.1"


def test_local_office_works_without_cookie_or_token_and_rejects_foreign_requests(ledger, tmp_path):
    app = create_app(ledger.store, TOKEN, workspace_manager=Manager(), local_ui=True)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 1234)) as client:
        marker = {"X-RelayFence-Local": "1", "Sec-Fetch-Site": "same-origin"}
        assert client.get("/local/status", headers=marker).status_code == 200
        assert not client.cookies
        write = {**marker, "Origin": ORIGIN}
        assert (
            client.post("/local/browse", json={"path": str(tmp_path)}, headers=write).status_code
            == 200
        )
        assert (
            client.post("/v1/office_get", json={"project": "default"}, headers=write).status_code
            == 200
        )
        assert client.post("/local/browse", json={}, headers=marker).status_code == 403
        assert (
            client.post(
                "/local/browse", json={}, headers={**marker, "Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/local/status", headers={**marker, "Sec-Fetch-Site": "cross-site"}
            ).status_code
            == 401
        )
        assert client.get("/local/status").status_code == 401
        assert client.get("/local/status", headers={"X-RelayFence-Local": "1"}).status_code == 401
    with TestClient(
        create_app(ledger.store, TOKEN, workspace_manager=Manager(), local_ui=True),
        base_url=ORIGIN,
        client=("192.0.2.1", 1234),
    ) as remote:
        assert remote.get("/local/status", headers=marker).status_code == 401


def test_local_ledger_busy_answers_503_and_interrupted_body_is_swallowed(ledger, tmp_path):
    import sqlite3

    class BusyManager(Manager):
        def status(self):
            raise sqlite3.OperationalError("database is locked")

    with TestClient(
        create_app(ledger.store, TOKEN, workspace_manager=BusyManager(), local_ui=True),
        base_url=ORIGIN,
        client=("127.0.0.1", 1234),
    ) as client:
        result = client.get(
            "/local/status", headers={"X-RelayFence-Local": "1", "Sec-Fetch-Site": "same-origin"}
        )
        assert result.status_code == 503
        assert "database is locked" not in result.text


@pytest.mark.parametrize("parent_name", ["workspace", "private"])
def test_browser_directory_listing_is_authenticated_and_excludes_files(
    ledger, tmp_path, parent_name
):
    tmp_path = tmp_path / parent_name
    tmp_path.mkdir()
    private_content = "fixture-file-content-that-must-not-be-listed"
    (tmp_path / "项目").mkdir()
    (tmp_path / "secret.token").write_text(private_content)
    with TestClient(
        create_app(ledger.store, TOKEN, workspace_manager=Manager()), base_url=ORIGIN
    ) as client:
        assert client.post("/local/browse", json={"path": str(tmp_path)}).status_code == 401
        headers = {"Authorization": "Bearer " + TOKEN}
        result = client.post("/local/browse", json={"path": str(tmp_path)}, headers=headers)
        assert result.status_code == 200
        assert result.json()["directories"] == [{"name": "项目", "path": str(tmp_path / "项目")}]
        assert private_content not in result.text
        assert "secret.token" not in result.text
        assert (
            client.post("/local/browse", json={"path": "relative"}, headers=headers).status_code
            == 400
        )
        assert (
            client.post(
                "/local/browse", json={"path": str(tmp_path / "secret.token")}, headers=headers
            ).status_code
            == 400
        )


class Manager:
    def __init__(self):
        self.started = False
        self.closed = False
        self.path = None

    def start_discovery(self):
        self.started = True

    def close(self):
        self.closed = True

    def status(self):
        return {"project": None, "workspace": self.path, "catalogue": []}

    def connect(self, path):
        if not isinstance(path, str):
            raise ValueError("Select a directory")
        self.path = path
        return self.status()

    def refresh(self):
        return self.status()


def test_pairing_is_single_use_expiring_and_keeps_admin_token_out_of_page(ledger):
    now = [100.0]
    sessions = BrowserSessions(clock=lambda: now[0])
    manager = Manager()
    with TestClient(
        create_app(ledger.store, TOKEN, sessions=sessions, workspace_manager=manager),
        base_url=ORIGIN,
    ) as client:
        assert manager.started
        assert TOKEN not in client.get("/").text
        assert sessions.code not in client.get("/").text
        assert client.get("/local/status").status_code == 401
        code = sessions.code
        # Possessing the pairing code still cannot authorize cross-origin writes.
        assert client.post("/local/session", json={"code": code}).status_code == 403
        assert (
            client.post(
                "/local/session", json={"code": code}, headers={"Origin": "https://example.com"}
            ).status_code
            == 403
        )
        paired = client.post("/local/session", json={"code": code}, headers={"Origin": ORIGIN})
        assert paired.status_code == 200
        assert "HttpOnly" in paired.headers["set-cookie"]
        assert "SameSite=strict" in paired.headers["set-cookie"]
        assert TOKEN not in paired.text + paired.headers["set-cookie"]
        assert (
            client.post(
                "/local/session", json={"code": code}, headers={"Origin": ORIGIN}
            ).status_code
            == 401
        )
        assert client.get("/local/status").status_code == 200
        assert client.post("/local/connect", json={"path": "work"}).status_code == 403
        assert (
            client.post("/local/connect", json={"path": "work"}, headers={"Origin": ORIGIN}).json()[
                "workspace"
            ]
            == "work"
        )
        now[0] += 8 * 3600
        assert client.get("/local/status").status_code == 401
    assert manager.closed


def test_pairing_code_expiry_and_invalid_values():
    now = [0]
    sessions = BrowserSessions(clock=lambda: now[0])
    assert sessions.pair(None) is None
    assert sessions.pair("wrong") is None
    assert sessions.pair("错误") is None
    now[0] = 600
    assert sessions.pair(sessions.code) is None
    assert not sessions.valid("bad cookie")


def test_local_routes_bounded_and_validate_requests(ledger):
    manager = Manager()
    with TestClient(
        create_app(ledger.store, TOKEN, workspace_manager=manager),
        base_url=ORIGIN,
        headers={"Authorization": "Bearer " + TOKEN},
    ) as client:
        assert client.post("/local/connect", json=[]).status_code == 400
        assert client.post("/local/connect", content="{").status_code == 400
        assert client.post("/local/connect", json={}).status_code == 400
        assert client.post("/local/connect", content="x" * 8193).status_code == 413
        assert client.post("/local/refresh", json={}).status_code == 200
        assert client.get("/local/connect").status_code == 404


def test_cookie_auth_also_supports_existing_operations(ledger):
    sessions = BrowserSessions()
    with TestClient(create_app(ledger.store, TOKEN, sessions=sessions), base_url=ORIGIN) as client:
        client.post("/local/session", json={"code": sessions.code}, headers={"Origin": ORIGIN})
        response = client.post(
            "/v1/office_get", json={"project": "default"}, headers={"Origin": ORIGIN}
        )
        assert response.status_code == 200
        assert response.json()["ok"]


def test_directory_picker_handles_unicode_cancel_and_unavailable_tk(monkeypatch):
    import subprocess
    from types import SimpleNamespace

    import pytest

    from relayfence.local_session import pick_directory

    captured = []

    def run(command, **kwargs):
        captured.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="D:/项目\n")

    monkeypatch.setattr(subprocess, "run", run)
    assert pick_directory() == "D:/项目"
    assert captured[0][1]["timeout"] == 120
    assert captured[0][1]["env"]["PYTHONIOENCODING"] == "utf-8"
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="\n")
    )
    assert pick_directory() == ""
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1, stdout="")
    )
    with pytest.raises(ValueError, match="绝对路径"):
        pick_directory()

    def timeout(*a, **kw):
        raise subprocess.TimeoutExpired("picker", 120)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ValueError, match="超时"):
        pick_directory()


def test_directory_picker_is_single_flight(monkeypatch):
    import threading

    from relayfence import local_session

    entered = threading.Event()
    release = threading.Event()

    def slow():
        entered.set()
        release.wait(5)
        return "D:/project"

    monkeypatch.setattr(local_session, "_pick_directory", slow)
    result = []
    thread = threading.Thread(target=lambda: result.append(local_session.pick_directory()))
    thread.start()
    assert entered.wait(2)
    try:
        import pytest

        with pytest.raises(ValueError, match="已在运行"):
            local_session.pick_directory()
    finally:
        release.set()
        thread.join(5)
    assert result == ["D:/project"]


def test_picker_endpoint_does_not_start_without_authentication(ledger, monkeypatch):
    from relayfence import local_session

    calls = []
    monkeypatch.setattr(local_session, "pick_directory", lambda: calls.append(True) or "D:/project")
    with TestClient(
        create_app(ledger.store, TOKEN, workspace_manager=Manager()), base_url=ORIGIN
    ) as client:
        assert client.post("/local/pick", json={}).status_code == 401
        assert not calls
        response = client.post("/local/pick", json={}, headers={"Authorization": "Bearer " + TOKEN})
        assert response.json() == {"path": "D:/project"}
        assert len(calls) == 1


def test_local_ledger_busy_answers_503(ledger):
    import sqlite3

    class BusyManager(Manager):
        def status(self):
            raise sqlite3.OperationalError("database is locked")

    with TestClient(
        create_app(ledger.store, TOKEN, workspace_manager=BusyManager(), local_ui=True),
        base_url=ORIGIN,
        client=("127.0.0.1", 1234),
    ) as client:
        result = client.get(
            "/local/status", headers={"X-RelayFence-Local": "1", "Sec-Fetch-Site": "same-origin"}
        )
        assert result.status_code == 503
        assert "database is locked" not in result.text


def test_local_browser_survives_expired_cookie_and_service_restart(ledger):
    now = [0.0]
    sessions = BrowserSessions(clock=lambda: now[0])
    app = create_app(
        ledger.store, TOKEN, sessions=sessions, local_ui=True, workspace_manager=Manager()
    )
    marker = {"X-RelayFence-Local": "1", "Sec-Fetch-Site": "same-origin"}
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 12)) as client:
        client.cookies.set("relayfence_local", "expired-before-restart")
        now[0] = 36000
        assert client.get("/local/status", headers=marker).status_code == 200
        assert (
            client.post(
                "/v1/office_get", json={"project": "default"}, headers={**marker, "Origin": ORIGIN}
            ).status_code
            == 200
        )
        assert (
            client.post("/v1/office_get", json={"project": "default"}, headers=marker).status_code
            == 403
        )
        for site in ("cross-site", "same-site", "none"):
            assert (
                client.get("/local/status", headers={**marker, "Sec-Fetch-Site": site}).status_code
                == 401
            )
        assert client.post("/mcp", json={}, headers=marker).status_code == 401
