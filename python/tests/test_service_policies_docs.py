"""Documentation contract checks for Service Policies."""

from pathlib import Path


DOC = Path(__file__).parents[2] / "docs" / "reference" / "service-policies.md"


def test_service_policy_reference_covers_beta_contract() -> None:
    content = DOC.read_text()

    for required in (
        "sensitive_data",
        "unsafe_content",
        "jailbreak",
        "hallucination",
        "llm_judge",
        "event VARIANT",
        "ON CALL",
        "ON RESULT",
        "ABAC",
        "dry-run",
        "--profile <name>",
    ):
        assert required in content


def test_service_policy_reference_points_to_native_capability_record() -> None:
    content = DOC.read_text()

    assert "service-policies-native-capabilities.md" in content
    assert "create-service-policy" in content
