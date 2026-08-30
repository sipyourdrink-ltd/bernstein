## Adapter auth basis and volunteer-mode status

Every adapter contract (`tests/contract/contracts/*.yaml`) now declares an explicit `auth.basis` (`api_key`, `local`, `subscription_oauth`, or `unknown`) on `ContractSpec` and `AdapterCapabilityProfile`, making the authentication mechanism readable from the contract rather than inferred from configuration (#3875).

Volunteer donor budgets (`VolunteerBudget.refuses_claim` and `filter_local_profiles`) gate adapter selection by auth basis and `local_models`: `local_only=True` budgets admit only `local_models=True` adapters (`opencode`, `pydantic_ai`); `subscription_oauth` adapters (`agy`, `copilot`) are refused under `local_only`; `api_key` adapters require `local_ok=True`.

The full adapter auth-basis and volunteer-mode table lives in [`docs/adapters/auth-basis-volunteer-mode.md`](auth-basis-volunteer-mode.md).