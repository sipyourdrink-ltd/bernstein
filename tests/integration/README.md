# Integration Tests

This directory contains end-to-end integration tests for Bernstein.

## Integration Test Harness

The tests use a specialized harness defined in `tests/conftest.py`:

- **Test Server:** A FastAPI application running in-process via `starlette.testclient.TestClient`.
- **IntegrationMockAdapter:** A flexible mock CLI adapter that can execute Python code provided in the task description. This allows scripting agent behavior without needing real LLM providers.
- **Respx Mocking:** Uses `respx` to intercept HTTP calls from the `Orchestrator` and route them to the in-process `TestClient`.

## Writing Integration Tests

To write a new integration test:

1. Use the `test_client` fixture to interact with the task server API.
2. Use the `orchestrator_factory` fixture to create an `Orchestrator` instance.
3. Define tasks with `IntegrationMockAdapter` scripted behavior using triple-backticked python blocks containing `# INTEGRATION-MOCK`.
4. Run the orchestrator loop manually using `orch.tick()` and verify state transitions.

Example:
```python
@pytest.mark.asyncio
async def test_my_feature(test_client, orchestrator_factory, integration_sdd):
    # 1. Setup task
    test_client.post("/tasks", json={"title": "My Task", ...})
    
    # 2. Run orchestrator
    orch = orchestrator_factory()
    orch.tick()
    
    # 3. Verify
    resp = test_client.get("/tasks")
    assert resp.json()[0]["status"] == "done"
```
