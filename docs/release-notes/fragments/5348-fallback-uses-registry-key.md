## _infer_adapter_name_for_provider fallback uses the registry key and warns for unregistered adapters

When `adapter_name_for_provider` returned no match for a provider/model pair, the
fallback path in `_infer_adapter_name_for_provider` returned `self._adapter.name()`
verbatim -- the adapter's display name such as `Qwen CLI` -- which downstream contract
hashing treated as a registry key and looked up `<name>.yaml`, producing a spurious
`no_contract` refusal under `BERNSTEIN_ADMISSION_POLICY=enforce`. The fallback now
returns `registry_name_for(self._adapter)`, the registry key the contract file is
indexed by, so a registered adapter whose provider/model does not match still spawns.
Adapters with no registry entry keep the previous "admit anyway" behaviour but now
log `WARNING _infer_adapter_name_for_provider: ... adapter X is not registered` so
operators can distinguish "no match for this model" from "this adapter is not in the
registry at all" (#5348).
