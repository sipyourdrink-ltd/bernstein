## ``bernstein compliance coverage`` reports per-control evidence status

Added ``bernstein compliance coverage`` — assesses each registered policy control against
the chain events present in the install and reports one of three statuses per control:
**evidenced** (all required inputs satisfied), **partially evidenced** (some inputs
satisfied; missing inputs are named), or **not evidenceable** (the install does not
produce the required artefact kind, with the reason why). (#4978)
