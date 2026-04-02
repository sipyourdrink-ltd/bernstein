"""Tests for log viewer with syntax highlighting."""

from __future__ import annotations

from bernstein.tui.log_viewer import LogViewer, detect_code_blocks, has_code_blocks


class TestLogViewer:
    """Test log viewer syntax highlighting."""

    def test_detect_code_blocks_present(self) -> None:
        """Test detecting code blocks."""
        log_text = """
Some text
```python
def hello():
    print("world")
```
More text
"""
        blocks = detect_code_blocks(log_text)

        assert len(blocks) == 1
        assert blocks[0][2] == "python"

    def test_detect_code_blocks_multiple(self) -> None:
        """Test detecting multiple code blocks."""
        log_text = """
```python
code1
```
```javascript
code2
```
"""
        blocks = detect_code_blocks(log_text)

        assert len(blocks) == 2

    def test_detect_code_blocks_none(self) -> None:
        """Test no code blocks detected."""
        log_text = "Just plain text\nNo code here"

        blocks = detect_code_blocks(log_text)

        assert len(blocks) == 0

    def test_has_code_blocks_true(self) -> None:
        """Test has_code_blocks returns True."""
        log_text = "Text\n```python\ncode\n```"

        assert has_code_blocks(log_text) is True

    def test_has_code_blocks_false(self) -> None:
        """Test has_code_blocks returns False."""
        log_text = "Plain text only"

        assert has_code_blocks(log_text) is False

    def test_log_viewer_creation(self) -> None:
        """Test log viewer can be created."""
        log_text = "Test log"

        viewer = LogViewer(log_text)

        assert viewer._log_text == log_text

    def test_log_viewer_custom_theme(self) -> None:
        """Test log viewer with custom theme."""
        log_text = "Test log"

        viewer = LogViewer(log_text, theme="dracula", line_numbers=True)

        assert viewer._theme == "dracula"
        assert viewer._line_numbers is True
