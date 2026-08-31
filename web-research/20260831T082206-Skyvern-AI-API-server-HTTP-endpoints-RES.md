# Web research

**Query:** Skyvern AI API server HTTP endpoints REST API documentation

**Sources:**
- https://www.skyvern.com/docs/api-reference/api-reference/agent/run-task/
- https://www.skyvern.com/docs/cloud/account-settings/api-versioning-and-deprecation

Based on Source 2, here's the Skyvern AI API REST API documentation:

## Base URL
`https://api.skyvern.com/v1/` - current and only supported version

## Primary Endpoint: Run Task
**POST** `/v1/run/tasks`
```bash
curl -X POST "https://api.skyvern.com/v1/run/tasks" \
  -H "x-api-key: [REDACTED:auth_header]" \
  -H "Content-Type: application/json" \
  -d '{ "url": "https://example.com", "prompt": "Extract the pricing table" }'
```

**Request Body:**
- `url` (string): Target URL to process
- `prompt` (string): Task description/instruction

## API Stability & Versioning

### What's Stable
- All `/v1/*` endpoints covered by OpenAPI spec
- OpenAPI spec: `https://www.skyvern.com/docs/api-reference/openapi.json`

### Backward-Compatible Changes (Safe)
- New optional request/response fields
- New enum values
- New HTTP methods on existing paths
- Field ordering changes

### Breaking Changes (Never in v1)
- Removing/renaming endpoints/fields
- Making optional params required
- Changing HTTP status codes
- Removing enum values

## Client Best Practices
1. **Pin version**: Use `/v1/` prefix
2. **Parse leniently**: Ignore unknown fields, handle unknown enums
3. **Monitor deprecation headers**: Log `Deprecation`/`Sunset` headers
4. **Track OpenAPI spec**: Diff in CI for generated clients

## Related Docs (from Source 1 links)
- SDK Reference: `/docs/sdk-reference/browser-automation/agent-run-task`
- Agents API: `/v1/agents` (with `search_key` param instead of deprecated `title`)

## Official Channels
- **Changelog**: `https://www.skyvern.com/docs/changelog`
- **OpenAPI Spec**: `https://www.skyvern.com/docs/api-reference/openapi.json`
- **Support**: support@skyvern.com

The complete OpenAPI specification contains the authoritative list of all endpoints, parameters, and data types.
