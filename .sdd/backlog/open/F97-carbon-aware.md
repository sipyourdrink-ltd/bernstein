# F97 — Carbon-Aware Scheduling

**Priority:** P5
**Scope:** small (15 min for skeleton/foundation)
**Wave:** 5 — Future-Proofing 2030-2035

## Problem
Compute-intensive agent tasks contribute to carbon emissions, but the scheduler has no awareness of grid carbon intensity, missing opportunities to reduce environmental impact.

## Solution
- Integrate with the Electricity Maps API to query real-time grid carbon intensity by region
- Add a `carbon_budget` field to `bernstein.yaml` allowing users to set carbon preferences (e.g., `prefer_low_carbon: true`, `max_carbon_intensity_gCO2_per_kWh: 200`)
- When latency budget allows, scheduler routes tasks to agents in lower-carbon regions
- If all regions exceed the configured threshold and task is not urgent, queue for later execution
- Track estimated CO2 per task based on compute duration and regional carbon intensity
- Display CO2 savings in the run summary: "Saved ~X gCO2 by routing to region Y"
- Add `bernstein carbon report` showing cumulative carbon metrics

## Acceptance
- [ ] Electricity Maps API integration returns real-time carbon intensity per region
- [ ] `bernstein.yaml` supports `carbon_budget` configuration section
- [ ] Scheduler prefers low-carbon regions when latency budget permits
- [ ] Tasks queued when all regions exceed carbon threshold and task is non-urgent
- [ ] CO2 estimate tracked per task based on compute time and regional intensity
- [ ] Run summary includes carbon savings message
- [ ] `bernstein carbon report` displays cumulative carbon metrics
