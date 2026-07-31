# Lessons from running a video pipeline on a rate-limited LLM API

**Status:** written 2026-07-31 after an AI Studio dashboard showed ~150 `429 TooManyRequests`
and ~150 `500 InternalServerError` over two days against only ~25 analysis calls per day.
**Audience:** anyone — human or agent — touching `nanny_analyze.py`, or building anything else
that calls a metered LLM API on a schedule. The specifics are Gemini's; the failure shapes are
not.

Two of the three causes were **our own code turning one failure into a hundred**. That is the
part worth generalising.

---

## 1. Find out which limit actually binds before optimising anything

We spent a whole round of work driving **input tokens** down (1 fps → 0.25 fps, ~237k → ~59k
tokens per camera-hour, a real 4× win) and then hit a wall of errors anyway — because on the
free tier the binding limit was **requests per day**, which that work barely moved.

The tell was sitting in the dashboard the whole time:

| Console panel | Value |
|---|---|
| Requests **per model** (i.e. `generate_content`) | ~25/day |
| **Total** API requests | 500–1000/day |

A 20–40× gap between "calls I wrote" and "calls I made" is not noise. It is the shape of the
problem.

**Generalise:** a metered API usually has several independent ceilings (requests/min,
requests/day, tokens/min, concurrent jobs). Optimising the one you happen to be thinking about
is not the same as optimising the one that is failing. Read the provider's per-tier table for
the *exact model you are on* and compare it against measured usage before writing code.

## 2. The "main" call is often a minority of your requests

Each piece of video cost: **1 upload + N processing polls + 1 delete + 1 generate**. With the
old poll schedule (start 3 s, ×1.5, cap 20 s) N was ~8, so the analysis call was **under 10%**
of our API traffic. Our rate limiter — `Pacer` — wrapped only `generate_content`, so ~90% of
requests were entirely unthrottled.

Fixes, in descending order of effect:

1. **Do fewer, larger units of work.** `NANNY_PIECE_MINUTES` 30 → 60 halves uploads, polls,
   deletes *and* generates simultaneously. One config change, four request classes.
2. **Poll lazily.** Start at 10 s and double to 30 s. A small file is ready in seconds; a tight
   poll loop buys latency you do not need at a cost you cannot afford.
3. Only then, throttle.

**Generalise:** count *every* HTTP call in a unit of work, not just the interesting one. Upload,
poll, delete, and status endpoints are where request budgets quietly go. If a rate limiter wraps
one function, ask what fraction of traffic that function represents.

## 3. Retry logic multiplies deterministic failures

**If an error count looks like `known_workload × retry_limit`, suspect your own retry loop.**
Here: 25 generations × 5 retries ≈ 125, against ~150 observed `InternalServerError`s. That
arithmetic was the whole diagnosis.

Two specific traps, both of which we had:

**(a) A fallback keyed on one status code misses the same failure arriving as another.**
We handled "this model rejects `video_metadata`" as a **400**, and fell back to default
sampling. But some model/SDK combinations answer **500** for exactly the same rejection. A 500
is classified retryable, so instead of falling back we re-sent the identical rejected request
five times, per piece, every run. The fix is to treat *repeated* server errors on a request
carrying an optional optimisation as evidence the optimisation is the problem — drop it and pay
the higher cost rather than loop.

**(b) Not all 429s mean "slow down".** A per-minute quota recovers in seconds; a **per-day**
quota does not recover today at all. Retrying it five times per segment across a whole backlog
converts one exhausted quota into hundreds of dashboard errors and spends tomorrow's budget on
nothing. Parse the quota violation (`daily_quota_exhausted()`) and end the run.

**Generalise:** before adding a retry, ask *what has to change for the next attempt to succeed*.
If the answer is "nothing", or "a day must pass", retrying is not resilience — it is
amplification. Retries belong on transient failures only, and "transient" is a claim about the
cause, not about the status code.

## 4. Model choice is a rate-limit decision, not only a cost/quality one

Free-tier **Flash: 10 RPM / 250 RPD**. Free-tier **Flash-Lite: 15 RPM / 1000 RPD**. Same 250k
TPM. We were on Flash for a task that is *description, not reasoning* — paying 4× less daily
request budget for capability we did not use.

Related: thinking models spend the **output** budget on thinking. Ours showed 150–200k output
tokens against a `GEMINI_MAX_OUTPUT_TOKENS` of 32768, which truncates the JSON, which raises
`TruncatedResponse`, which retries, which spends more requests. Cap it
(`GEMINI_THINKING_LEVEL=minimal`) for extraction-shaped tasks.

## 5. Verify the console shows the model you think you are running

The dashboard said **Gemini 3.5 Flash**. The code default was `gemini-2.5-flash-lite`. The
difference lived in one line of `/etc/nursery-tracker/nanny.env` — invisible from the source,
and the source is what everyone reads first. Model IDs also get retired, so a default that was
correct when written can silently become a 404 later.

**Generalise:** for anything configured by environment, the provider's own console is ground
truth about what is running. Check it before reasoning from the code.

## 6. Free tiers are a data-handling decision

Google's API terms: unpaid-tier content is used "to provide, improve, and develop Google
products and services", and human reviewers may read API input and output. The paid tier says
the opposite explicitly. For this project the inputs are video of a named employee and an
infant. That is a product/legal decision, not a cost one, and it should be made deliberately
rather than as a side effect of chasing a bill. See `nanny-report-review.md` §4.

---

## Checklist for the next metered-API integration

- [ ] Which ceiling binds — requests/min, requests/day, or tokens/min? For the exact model, at
      the exact tier. Write the numbers down.
- [ ] Count every HTTP call per unit of work, not just the inference call.
- [ ] Can the unit of work be bigger? It usually reduces several request classes at once.
- [ ] For each retry: what changes before the next attempt? If nothing, do not retry.
- [ ] Does any fallback key on a single status code that could arrive as another?
- [ ] Is the cheapest sufficient model in use, and is thinking capped for extraction tasks?
- [ ] Does the provider console agree with what the code says is configured?
- [ ] What does the tier's data-usage policy say about the inputs being sent?
