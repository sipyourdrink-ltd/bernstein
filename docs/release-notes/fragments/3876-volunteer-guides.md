## Volunteer worker documentation: threat model, donor guide, project guide

Someone donating compute to a volunteer project had no written answer to the
questions that decide whether they should: what the worker is allowed to run
on their machine, what a project can and cannot see about them, and who is
responsible for the work that comes out. The code enforcing those boundaries
existed; the page explaining them did not.

`docs/volunteer/threat-model.md` states the security surface and what each
boundary does and does not protect. `docs/volunteer/donor-guide.md` covers
running a worker, the budget a donor sets and what happens when it is spent.
`docs/volunteer/project-guide.md` covers the other side: declaring a manifest
and what a project is committing to. `DISCLAIMER.md` states that volunteers
act on their own behalf.
