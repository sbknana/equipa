from pathlib import Path


def test_anti_compaction_in_common_prompt():
    common = Path(__file__).parent.parent / "prompts" / "_common.md"
    assert common.exists(), "prompts/_common.md not found"
    content = common.read_text()
    assert ".forge-state.json" in content, "Anti-compaction instructions missing"
    assert "State Persistence" in content, "State Persistence section missing"
