def group(prefix, name):
    return {
        "id": prefix,
        "name": name,
        "software": "codex",
        "members": [
            {
                "id": prefix + "-lead",
                "name": "组长",
                "position": "lead",
                "avatar": 1,
                "gender": "female",
                "relationship": "leader",
                "agent_id": None,
                "provider": "openai",
                "model": "model-a",
                "parameters": {},
            }
        ],
    }


def test_team_groups_list_aggregates_projects_and_filters(ledger):
    ledger.ok(
        "office_put",
        project="project-one",
        groups=[group("alpha", "Alpha 小组")],
        expected_revision=0,
    )
    ledger.ok(
        "office_put",
        project="project-two",
        groups=[group("beta", "Beta 小组")],
        expected_revision=0,
    )

    data = ledger.ok("team_groups_list", project="project-one")
    assert data["total"] == 2
    assert data["current_project"] == "project-one"
    assert {item["project"] for item in data["groups"]} == {
        "project-one",
        "project-two",
    }
    current = next(item for item in data["groups"] if item["project"] == "project-one")
    other = next(item for item in data["groups"] if item["project"] == "project-two")
    assert current["in_office"] is True
    assert current["member_count"] == 1
    assert current["group"]["name"] == "Alpha 小组"
    assert other["in_office"] is False
    assert other["revision"] == 1
    assert {item["project"] for item in data["projects"]} == {
        "project-one",
        "project-two",
    }

    filtered = ledger.ok("team_groups_list", project="project-one", project_filter="project-two")
    assert filtered["total"] == 1
    assert filtered["groups"][0]["group"]["name"] == "Beta 小组"

    searched = ledger.ok("team_groups_list", project="project-one", query="beta")
    assert searched["total"] == 1
    assert searched["groups"][0]["project"] == "project-two"

    by_member = ledger.ok("team_groups_list", project="project-one", query="组长")
    assert by_member["total"] == 2


def test_team_groups_list_is_empty_without_office_configs(ledger):
    data = ledger.ok("team_groups_list", project="empty")
    assert data == {
        "groups": [],
        "projects": [],
        "total": 0,
        "truncated": False,
        "current_project": "empty",
        "server_time": ledger.clock.now,
    }
