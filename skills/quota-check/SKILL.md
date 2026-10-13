---
name: quota-check
description: "Before long tasks or 429s: check quota; ask, don't degrade."
version: 1.0.0
tags: [quota, providers, limits, costs]
---

# Quota check (quota plugin)

Real per-provider consumption and limits from the `quota` plugin (CLI +
desktop widget). Read them before committing to long work.

## When to Use

- Before STARTING a potentially long task: audits, batch delegation, long
  builds or test runs, serial generation, cron jobs, wide refactors.
- When diagnosing 429s, "rate limit", "insufficient credits", or generation
  that stopped midway.
- Before delegating to another agent or profile: check the EXECUTOR's quota
  (`hermes -p <profile> quota ...`), not only your own.

## How to check

1. Cache first (instant, zero network):

   ```
   hermes quota status --cached
   hermes quota status --json --cached   # for parsing; includes cache age
   ```

2. `hermes quota refresh` only when the cache is stale (>15 min) and a
   decision is imminent. A refresh calls provider APIs (up to ~20s) and
   consumes their request budgets.
3. Non-default profiles: `hermes -p <profile> quota status --cached`.
   The cache is per profile; provider credentials are global.

## How to read it

- Providers expose different windows — rolling 5-hour, weekly, billing
  cycle, or USD credit balances — whichever the vendor documents.
- `unavailable (no-credentials)` and similar states are honest: never treat
  them as zero, never invent a number.
- remaining% = the LOWEST remaining percentage among the windows relevant to
  the task's horizon (rolling window for sessions; billing cycle for
  credit-metered providers). For USD credits: `remaining / total * 100`.

## Alerts by remaining quota

| remaining | conduct |
|---|---|
| ≤50% | State the value when starting long tasks. Non-blocking. |
| ≤20% | Before ANY long task: WARN and ASK AUTHORIZATION (options below). |
| ≤10% | Applies to medium tasks too. Nothing extensive without approval. |
| ≤5%  | Only short, essential replies. Anything bigger needs the requester's decision. |

## Required conduct

- NEVER reduce quality, scope, or depth to save quota. No quota concern
  justifies a poor deliverable, a shallow answer, or "economical work".
  Quota is managed by warning and asking, never by worsening the result.
- NEVER rely on automatic fallback for quota exhaustion. Fallback chains are
  built for provider outages (5xx); a 429 from quota exhaustion may not
  switch models, and some Hermes installs configure no fallback providers at
  all.
- If the task fits the available quota with room to spare: just do the work.
- If it does NOT fit, or there is reasonable doubt: STOP BEFORE STARTING and
  present the requester (the user, or the delegating agent) exactly these
  options:

  1. SPLIT the task into a smaller step that fits the remaining quota
     (propose the concrete cut).
  2. USE A DIFFERENT MODEL (ask whether the requester wants to pick one;
     if the profile defines a fallback chain, offer it as a starting point).

  Wait for explicit authorization. Never unilaterally deliver "half the
  task" without approval.

## If quota runs out mid-task (a real 429)

- Stop at the nearest step boundary; do not improvise a conclusion.
- Report the real state: what is done, what is missing, how much quota was
  left.
- Offer the same two options (split / switch model) and wait for a decision.
