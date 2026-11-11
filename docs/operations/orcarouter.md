# OrcaRouter (optional OpenAI-compatible endpoint)

Operators can use [OrcaRouter's OpenAI-compatible API](https://docs.orcarouter.ai/compatibility/openai-sdk)
through Bernstein's existing `openai_agents` adapter. Its base URL is
`https://api.orcarouter.ai/v1`; model IDs are provider-prefixed, such as
`openai/gpt-4o-mini`. This is an opt-in configuration example, not a claim
that a live OrcaRouter endpoint or model has been certified by Bernstein.

## Configure the adapter

Install the optional SDK dependency with `pip install 'bernstein[openai]'`
(see the [adapter guide](../adapters/openai-agents.md)). Supply your
OrcaRouter key as `ORCAROUTER_API_KEY` in the host environment using your
secret manager. Keep the value out of YAML, command arguments and receipts.

`ORCAROUTER_API_KEY` is outside Bernstein's built-in credential-name
allowlist. Explicitly allow that name on the host:

```bash
export BERNSTEIN_ALLOWED_API_KEY_ENVS="${BERNSTEIN_ALLOWED_API_KEY_ENVS:+${BERNSTEIN_ALLOWED_API_KEY_ENVS},}ORCAROUTER_API_KEY"
```

This preserves any credential names already allowed by the operator.
If credential scoping is enabled, also permit `ORCAROUTER_API_KEY` for
the intended role in `.sdd/config/credential_scopes.yaml`, following the
[adapter credential guide](../adapters/openai-agents.md#credentials).

Copy [the example bernstein.yaml](../../examples/orcarouter/bernstein.yaml)
into your project or merge its endpoint and `backend` role entries into
your existing configuration. Despite its name, `local_endpoints` accepts
an OpenAI-compatible hosted URL. The role inherits the profile's
`base_url`, `model` and `api_key_env`; do not repeat those fields alongside
`endpoint:`. The example supplies the environment variable's **name**, never
the secret. Other roles keep their existing adapter selection.

Choose an explicit chat model from the [OrcaRouter catalog](https://www.orcarouter.ai/models).
Keep the same model ID in the profile, certification command and fallback
chain. Image/video generation APIs are outside this adapter example.

## Qualify before dispatch

Run certification from the project root, where the configuration will be
loaded. This contacts the provider and may incur usage charges:

```bash
bernstein endpoints certify \
  --base-url https://api.orcarouter.ai/v1 \
  --model openai/gpt-4o-mini \
  --engine orcarouter \
  --api-key-env ORCAROUTER_API_KEY \
  --role backend
```

The equivalent doctor command uses these option names:

```bash
bernstein doctor \
  --endpoint https://api.orcarouter.ai/v1 \
  --endpoint-model openai/gpt-4o-mini \
  --endpoint-engine orcarouter \
  --endpoint-api-key-env ORCAROUTER_API_KEY \
  --role backend
```

Both commands run the existing reachability, chat completion, tool calling,
patch fidelity, timeout and context-floor probes. Require exit code 0 and
a passing verdict for every intended role before dispatch, and before adding
an entry to a `provider_availability` chain. This example does not configure
such a chain, so no binary availability probe is used for its adapter.
Repeat `--role` for additional roles; omitting it evaluates only the default
low-stakes tier.

The signed receipt is stored under
`.sdd/endpoints/certifications/<fingerprint>.json`, bound to the exact
`(base_url, model)` pair, anchored in the certification lineage spine and
mirrored in the audit chain. Verify the stored receipt offline:

```bash
bernstein endpoints verify \
  --base-url https://api.orcarouter.ai/v1 \
  --model openai/gpt-4o-mini
```

The example's `backend` role is gated: loading the configuration without
a valid receipt certifying that role fails closed. Low-stakes roles do not
have the same automatic gate; operators must still qualify them before
adding their entries to a chain. Re-certify when changing URL/model or when
provider behavior changes. See [endpoint profiles and certification](../reference/local-endpoints.md)
for the role tiers and receipt contract.

## Routing boundaries

This example selects `openai_agents` for `backend` through `role_model_policy`
and does not declare a Bernstein `provider_availability` chain. The adapter
uses the role's endpoint profile for its URL and key. If you add a chain,
qualify each model first; Bernstein then selects from the declared order and
recorded probe outcomes, with routing receipts, replay and offline verification
using the existing implementation.

OrcaRouter's own routing and provider failover happen behind the endpoint;
they are not Bernstein routing decisions or evidence of a fixed upstream.
Endpoint certification records observed behavior at qualification time.
Provider-side budgets and usage tracking remain managed by the operator
in OrcaRouter; this configuration adds no billing or partner-program integration.
