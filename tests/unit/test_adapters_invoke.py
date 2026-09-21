import pytest
from bernstein.adapters.base import CLIAdapter
from bernstein.adapters.generic import GenericAdapter


class TestCLIAdapterInvoke:
    def test_invoke_raises_not_implemented(self):
        class MinimalAdapter(CLIAdapter):
            def spawn(self, **kwargs):
                pass
            def name(self):
                return "minimal"

        adapter = MinimalAdapter()
        with pytest.raises(NotImplementedError):
            adapter.invoke(model="test", input_text="test")


class TestGenericAdapterInvoke:
    def test_invoke_raises_not_implemented_with_message(self):
        adapter = GenericAdapter(cli_command="echo")
        with pytest.raises(NotImplementedError) as exc_info:
            adapter.invoke(model="test", input_text="test")
        assert "does not support direct model invocation" in str(exc_info.value)