"""Unit tests for context injection token budget enforcement."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock
from pathlib import Path
from bernstein.core.context_compression import PromptCompressor, DEFAULT_CATEGORY_BUDGETS
from bernstein.core.knowledge_base import TaskContextBuilder
from bernstein.core.models import Task

def test_prompt_compressor_logs_category_overflow(caplog):
    caplog.set_level(logging.INFO)
    sections = [
        ("role", "role prompt"),
        ("files context", "a" * (DEFAULT_CATEGORY_BUDGETS["files"] * 4 + 100)),
        ("lessons", "lesson content")
    ]
    
    compressor = PromptCompressor(token_budget=100_000)
    compressor.compress_sections(sections)
    
    assert "Section 'files context' exceeds category budget" in caplog.text

def test_prompt_compressor_drops_low_priority_sections(caplog):
    caplog.set_level(logging.INFO)
    # Total tokens will be ~2500
    sections = [
        ("role", "r" * 400),        # 100 tokens, priority 10
        ("tasks", "t" * 400),       # 100 tokens, priority 10
        ("lessons", "l" * 4000),    # 1000 tokens, priority 4
        ("team", "m" * 4000),       # 1000 tokens, priority 3
        ("instructions", "i" * 400) # 100 tokens, priority 10
    ]
    
    # Budget of 1500 tokens. Should drop 'team' (priority 3).
    compressor = PromptCompressor(token_budget=1500)
    compressed, original, final, dropped = compressor.compress_sections(sections)
    
    assert "team" in dropped
    assert "lessons" not in dropped
    assert final < original
    assert "Prompt budget exceeded; dropped sections: team" in caplog.text

# Minimal patch helper since we don't have mock.patch readily available in a clean way without imports
class patch_module:
    @staticmethod
    def patch(target, new_obj):
        import importlib
        module_path, attr_name = target.rsplit(".", 1)
        module = importlib.import_module(module_path)
        
        class ContextManager:
            def __enter__(self):
                self.original = getattr(module, attr_name)
                setattr(module, attr_name, new_obj)
                return new_obj
            def __exit__(self, exc_type, exc_val, exc_tb):
                setattr(module, attr_name, self.original)
        
        return ContextManager()

def test_task_context_builder_enforces_budgets(caplog, tmp_path):
    caplog.set_level(logging.INFO)
    builder = TaskContextBuilder(tmp_path)
    
    # Mock result from ContextCompressor
    mock_result = MagicMock()
    mock_result.selected_files = ["file1.py", "file2.py", "file3.py"]
    
    # Mock file_context to return large strings
    # Each will be ~6000 tokens (DEFAULT_CATEGORY_BUDGETS['files'] is 15000)
    builder.file_context = MagicMock(return_value="f" * 24000)
    
    # Mock ContextCompressor
    mock_compressor_cls = MagicMock()
    mock_compressor_cls.return_value.compress.return_value = mock_result
    
    # Mock CodebaseIndexer
    mock_indexer_cls = MagicMock()
    mock_indexer_cls.return_value.search.return_value = []

    tasks = [Task(id="T1", title="Task 1", role="backend", description="Fix budget enforcement")]

    with patch_module.patch("bernstein.core.context_compression.ContextCompressor", mock_compressor_cls):
        with patch_module.patch("bernstein.core.rag.CodebaseIndexer", mock_indexer_cls):
            context = builder.build_context(tasks)
            
    # Should have included 2 files (12000 tokens) but not the 3rd (18000 tokens > 15000)
    assert builder.file_context.call_count == 3
    assert "Truncating file context: reached budget of 15000 tokens" in caplog.text
