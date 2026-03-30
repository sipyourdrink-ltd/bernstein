# P77 — Free Tier Limits

**Priority:** P4
**Scope:** medium (15 min for skeleton/foundation)
**Wave:** 4 — Platform & Network Effects

## Problem
Without usage limits on the free tier, a single user could exhaust server resources, and there is no incentive to upgrade to paid plans.

## Solution
- Define free tier constraints: 100 tasks/month, 3 concurrent agents, community models only
- Add cloud middleware that checks usage counters before executing a task
- Store per-user counters in Redis with keys like `usage:{user_id}:tasks:{YYYY-MM}` and `usage:{user_id}:concurrent`
- Increment task counter on task start; decrement concurrent counter on task completion
- Reject requests exceeding limits with 429 status and a message indicating which limit was hit
- Reset monthly counters via Redis TTL set to end-of-month
- Block non-community model selections at the routing layer for free-tier users

## Acceptance
- [ ] Free-tier users limited to 100 tasks per calendar month
- [ ] Concurrent agent count capped at 3 for free-tier users
- [ ] Only community models are routable for free-tier users
- [ ] Counters stored in Redis and reset automatically each month
- [ ] Exceeding any limit returns HTTP 429 with descriptive error message
- [ ] Middleware is non-blocking for paid-tier users
