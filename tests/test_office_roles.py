"""Contract tests for the Chinese office role catalogue.

The tests pin the eight positions, the presence and richness of every field and
the fact that the catalogue stays free of provider, credential or authentication
settings. They import the catalogue directly instead of standing up a full
ledger, because the catalogue is pure data.
"""

from relayfence.office_roles import POSITIONS

POSITION_KEYS = [
    "lead",
    "product",
    "architect",
    "frontend",
    "backend",
    "qa",
    "security",
    "reviewer",
]

# Field names that would smuggle provider, credential or authentication settings
# into a pure-data catalogue. Only mapping keys are inspected: the instruction
# text legitimately mentions credentials and authentication in order to forbid
# touching them.
FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "api_key",
        "auth",
        "authorization",
        "credential",
        "credentials",
        "model",
        "parameters",
        "password",
        "provider",
        "secret",
        "selection",
        "token",
    }
)


def has_chinese(text):
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def iter_mapping_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from iter_mapping_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_mapping_keys(item)


def test_positions_expose_the_eight_contract_keys():
    assert set(POSITIONS) == set(POSITION_KEYS)
    assert len(POSITIONS) == 8


def test_every_position_is_a_well_formed_mapping():
    for key in POSITION_KEYS:
        spec = POSITIONS[key]
        assert isinstance(spec, dict), key
        assert set(spec) == {"label", "instructions", "deliverables"}, key


def test_labels_are_nonempty_unique_chinese_strings():
    labels = [POSITIONS[key]["label"] for key in POSITION_KEYS]
    for key, label in zip(POSITION_KEYS, labels, strict=True):
        assert isinstance(label, str), key
        assert label.strip() == label and label, key
        assert has_chinese(label), key
    assert len(set(labels)) == len(labels)


def test_instructions_are_substantive_chinese_text():
    for key in POSITION_KEYS:
        instructions = POSITIONS[key]["instructions"]
        assert isinstance(instructions, str), key
        assert instructions.strip() == instructions and instructions, key
        assert has_chinese(instructions), key
        # A label alone cannot express responsibility and handoff boundaries.
        assert len(instructions) >= 80, key


def test_instructions_cover_handoff_and_truthful_reporting():
    for key in POSITION_KEYS:
        instructions = POSITIONS[key]["instructions"]
        assert "RelayFence" in instructions, key
        assert "交接" in instructions, key
        # Truthful reporting must be explicit, not merely implied by the title.
        assert "如实" in instructions, key
        # Every role must admit what it did not verify instead of hedging silently.
        assert any(
            marker in instructions
            for marker in ("未验证", "未执行", "未检查", "未审计", "未覆盖", "不得", "禁止")
        ), key


def test_instructions_are_role_specific():
    texts = [POSITIONS[key]["instructions"] for key in POSITION_KEYS]
    assert len(set(texts)) == len(texts)


def test_catalogue_exposes_no_provider_credential_or_auth_fields():
    keys = set(iter_mapping_keys(POSITIONS))
    assert not (keys & FORBIDDEN_FIELD_NAMES), sorted(keys & FORBIDDEN_FIELD_NAMES)


def test_instructions_forbid_unauthorized_secret_and_auth_changes():
    for key in POSITION_KEYS:
        instructions = POSITIONS[key]["instructions"]
        assert "认证" in instructions or "安全" in instructions, key
        assert "凭据" in instructions or "密钥" in instructions or "令牌" in instructions, key


def test_deliverables_are_nonempty_chinese_string_lists():
    for key in POSITION_KEYS:
        deliverables = POSITIONS[key]["deliverables"]
        assert isinstance(deliverables, list), key
        # One vague line cannot describe the work a position owes.
        assert len(deliverables) >= 3, key
        assert len(set(deliverables)) == len(deliverables), key
        for item in deliverables:
            assert isinstance(item, str), (key, item)
            assert item.strip() == item and item, (key, item)
            assert has_chinese(item), (key, item)
            # Concrete deliverables name an artefact, not a bare topic word.
            assert len(item) >= 8, (key, item)
