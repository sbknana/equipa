from pathlib import Path


def test_language_prompt_files_exist():
    lang_dir = Path(__file__).parent.parent / "prompts" / "languages"
    assert lang_dir.exists()
    expected = ["python.md", "typescript.md", "go.md", "csharp.md"]
    for f in expected:
        assert (lang_dir / f).exists(), f"Missing language prompt: {f}"
        content = (lang_dir / f).read_text()
        assert len(content) > 100, f"Language prompt {f} is too short"
