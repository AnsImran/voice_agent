# Happy Hound Grooming Availability Integration Notes

## What this README is

This document explains the full investigation, the final business decision, the reasoning behind the implementation, the best Gingr endpoint to use, the exact payload fields to analyze, and how to integrate the final Python module into a larger agent.

This README is intentionally detailed so a Codex-style agent or future developer can understand both **what we decided** and **why we decided it**.

---

## 1. Original goal

The original goal was to make an AI agent that can answer service-availability questions for Happy Hound.

At the beginning, the idea was broadly:

- inspect Gingr data,
- determine whether a requested service has availability,
- return a clear yes/no answer,
- and ideally suggest the next available slot when the requested slot is not available.

Over time, after looking at the business email, the existing code, the public Gingr API documentation, exploratory scripts, raw reservation payloads, and validation runs, the final scope became much more precise.

---

## 2. Final business decision

### Daycare
Do **not** perform capacity math.

For the purposes of this agent, simply tell the caller there is availability.

### Boarding
Do **not** perform routine capacity math.

For the purposes of this agent, simply tell the caller there is availability.

### Grooming
Do perform **real availability checking** using Gingr data.

### Mixed cases
If the top-level request is not literally "Grooming" but the requested service is grooming-related, still use the grooming checker.

Examples:
- Boarding + Deluxe Bath
- Daycare + A la Carte
- Training + Last Day Bath
- Boarding + Mini Groom

Why this matters:
The raw Gingr payloads showed that grooming work is often attached to **boarding**, **daycare**, or **training** reservations as an additional service. Those still consume groomer time and must be counted.

---

## 3. What the business email told us

From the business email:

- Daycare has no stated max per day.
- Daycare has no stated max per playgroup.
- Enhanced Daycare has no stated cap.
- Boarding has no routine max in normal operation.
- Grooming is the only clearly constrained service.
- There is one groomer.
- Tuesday hours are 7am to 1pm.
- Wednesday through Saturday hours are 7am to 5pm.
- One appointment at a time.
- Baths are about 1 hour.
- Full grooms and shed-less baths are up to 2 hours.

That email was the first major clue that the agent should not spend time doing capacity math for daycare and boarding, but **should** do real slot logic for grooming.

---

## 4. What we first explored

We looked at several things during the investigation:

1. the existing Happy Hound Python agent file,
2. the public Gingr API documentation,
3. reference-data discovery scripts,
4. raw reservation payload dumps for multiple dates,
5. validation outputs from the new grooming checker.

We also generated several exploratory scripts to understand:

- what reservation types exist,
- what fields are returned by Gingr,
- whether grooming could be detected from reservation types alone,
- whether the parent reservation date range was enough,
- and whether the correct answer lived in the service rows instead.

The answer turned out to be: **the service rows are the truth**.

---

## 5. What did NOT work well enough

### 5.1 Relying on reservation type discovery alone

An exploratory script used reservation types and services-by-type discovery to try to identify grooming types.

That was not reliable enough for production.

Why:
- the discovery results were incomplete,
- service-by-type parsing did not give a stable grooming map,
- a grooming candidate ID file ended up effectively empty,
- but live reservations clearly contained real groomer-assigned work.

So the production logic should **not** depend on a hand-maintained list of grooming reservation type IDs.

### 5.2 Looking only at parent reservation dates

This also failed.

A reservation can span multiple days or represent boarding/training/daycare while containing a grooming-related service scheduled on a specific day and time.

Examples seen in real payloads:
- a boarding reservation containing `Grooming | Mini Groom`,
- a daycare reservation containing `Grooming | A la Carte`,
- a training reservation containing `Training Program | Last Day Bath - Business Only` assigned to `Groomer`.

So the parent reservation `start_date` and `end_date` are not enough.

### 5.3 Counting whole reservations instead of service intervals

This overcounted grooming occupancy.

A reservation may be returned by the date-range query even when its grooming service is scheduled on another date.

Therefore, grooming capacity must be counted from **same-day scheduled grooming service rows**, not from reservation counts.

---

## 6. The best endpoint to call

Use:

`POST /api/v1/reservations`

Why this is the best endpoint:

- It returns the raw reservation feed for a date range.
- It includes nested `services` rows.
- Those service rows include the exact scheduling information needed for grooming capacity.
- It works even when grooming is attached to boarding, daycare, or training reservations.
- It gives a consistent source of truth for both same-day checks and next-available-slot searches.

Do **not** build production grooming logic on reservation-type metadata alone.

Do **not** build production grooming logic on parent reservation dates alone.

---

## 7. The exact Gingr data that matters

Inside each reservation, inspect the `services` array.

For each service row, the important fields are:

- `name`
- `scheduled_at`
- `scheduled_until`
- `assigned_to`

These are the fields that matter for capacity.

### Why these fields matter

- `scheduled_at` = when the service begins
- `scheduled_until` = when the service ends
- `assigned_to` = who performs the service
- `name` = useful fallback signal when a row is clearly bath/groom-related

### What counts as groomer occupancy

A service row counts against grooming capacity when either:

1. `assigned_to` looks like `Groomer`, or
2. `name` looks grooming-related, such as:
   - Grooming | Basic Bath
   - Grooming | Deluxe Bath
   - Grooming | Deluxe Bath Plus
   - Grooming | Full Groom
   - Grooming | Mini Groom
   - Grooming | A la Carte
   - Grooming | Shed-less Bath
   - Training Program | Last Day Bath - Business Only

### What should be ignored

Ignore:
- unscheduled service rows,
- service rows with null `scheduled_at` or `scheduled_until`,
- cancelled reservations,
- non-grooming scheduled services like trainer sessions or Bark Ranger activities.

---

## 8. Key payload lessons from raw data

The raw reservation payloads gave several decisive lessons.

### 8.1 Grooming work appears under many reservation types

Real groomer-occupying service rows were found under:
- Grooming reservations,
- Boarding reservations,
- Daycare reservations,
- Training reservations.

So the checker must focus on **service rows**, not reservation type.

### 8.2 Tuesday is not literally "baths only" in the data

The business email described Tuesday as baths only, but the live data for Tuesday also showed short `A la Carte` grooming services.

So the production code should enforce **staffing windows**, not a fragile service-name whitelist for Tuesdays.

### 8.3 Service durations vary

The real payloads showed that not every service fits neatly into 60 or 120 minutes.

Examples seen during validation:
- `A la Carte` = 15 minutes
- `Basic Bath` = 60 minutes
- `Deluxe Bath` = 60 minutes
- `Mini Groom` = 120 minutes
- `Shed-less Bath` = 120 minutes
- `Full Groom` = 120 to 180 minutes in real data
- `Deluxe Bath Plus` varied by case

So the code uses:
- actual scheduled intervals for existing bookings,
- a duration map for new requested bookings,
- and allows explicit duration override when needed.

### 8.4 Closed-day behavior is real

Sunday and Monday behaved correctly as closed grooming days under the staffing model.

---

## 9. Final algorithm

This is the final design.

### Step 1: Decide whether the request needs grooming logic

If the request is ordinary daycare or boarding with no grooming-like service, do not call Gingr for capacity.

Return availability immediately.

If the category is grooming or the requested service looks grooming-related, continue.

### Step 2: Fetch reservations for the requested date

Call:

`POST /api/v1/reservations`

with:
- `key`
- `checked_in=false`
- `start_date=YYYY-MM-DD`
- `end_date=YYYY-MM-DD`
- `location_id`

### Step 3: Flatten all service rows

Walk through every reservation and every service row inside it.

### Step 4: Keep only same-day groomer-occupying rows

A service row is kept when:
- it has `scheduled_at` and `scheduled_until`,
- it overlaps the requested local day,
- it is assigned to a groomer or clearly grooming-related,
- the parent reservation is not cancelled.

### Step 5: Convert these rows into occupied intervals

Each kept row becomes one occupied grooming interval.

### Step 6: Compare the requested slot against staffing windows

Use local staffing windows such as:
- Tuesday 07:00-13:00 => 1 worker
- Wednesday-Saturday 07:00-17:00 => 1 worker
- Sunday/Monday => 0 workers

### Step 7: Split the requested interval into smaller segments

Segment boundaries include:
- requested slot start/end,
- occupied interval start/end,
- staffing window boundaries.

### Step 8: Check every segment

For each segment:
- count active workers,
- count overlapping occupied bookings,
- ensure overlapping bookings < workers.

If every segment passes, the slot is available.

If any segment has zero workers, the reason is `outside_staffing_hours`.

If any segment has workers but no remaining capacity, the reason is `groomer_capacity_full`.

### Step 9: If unavailable, search for the next available slot

Search forward by 15-minute increments across the configured lookahead horizon.

---

## 10. Validation examples that proved the approach

The final checker was validated against real uploaded reservation dumps and test outputs.

### Example A: Saturday 2026-02-21 at 09:00, Mini Groom

Result: unavailable.

Why:
A real same-day service interval already occupied 09:00-11:00 for `Grooming | Mini Groom`.

### Example B: Saturday 2026-02-21 at 15:45, Deluxe Bath

Result: unavailable.

Why:
A groomer-occupying `A la Carte` service already overlapped 15:45-16:00.

### Example C: Sunday 2026-02-22 at 09:00, Deluxe Bath

Result: unavailable.

Reason: `outside_staffing_hours`.

### Example D: Monday 2026-02-23 at 09:00, Deluxe Bath

Result: unavailable.

Reason: `outside_staffing_hours`.

### Example E: Tuesday 2026-02-24 at 08:15, A la Carte

Result: unavailable.

Why:
Existing same-day `A la Carte` intervals occupied 08:15-08:30 and 08:30-08:45.

Next available result: 08:45.

These examples confirmed that the checker is using the correct unit of logic.

---

## 11. Why we do NOT just use reservation type = Grooming

Because real groomer time appeared under non-grooming reservation types.

Examples found during investigation included:
- boarding reservations with baths and mini grooms,
- daycare reservations with a-la-carte grooming,
- training reservations with last-day baths assigned to the groomer.

If we only looked at reservation type `Grooming`, we would miss real groomer occupancy and return false positives.

---

## 12. Why we do NOT just count reservations per day

That would be wrong for grooming.

Grooming is appointment-like and time-specific.

Two grooming services on the same day may be perfectly compatible if they occur at different times.

Likewise, a single 15-minute service can block a short slot while leaving the rest of the day open.

The correct model is interval overlap, not day-level counts.

---

## 13. What the final Python file contains

The final Python file is:

`happy_hound_grooming_availability.py`

It contains:

- environment-backed Gingr configuration,
- staffing rules,
- default duration map,
- service classification helpers,
- Gingr reservation parsing helpers,
- occupied-slot extraction,
- precise slot-check logic,
- next-available search,
- a high-level orchestration function for the agent,
- and a CLI for local testing.

The most important integration function is:

`determine_service_availability(...)`

That function enforces the final business policy:
- ordinary daycare/boarding => immediately available,
- grooming or grooming-like add-on => real Gingr check.

---

## 14. Integration guidance for the main agent

The simplest integration pattern is:

1. classify the user request into category + requested service,
2. call `determine_service_availability(...)`,
3. convert the structured result to natural language.

### Example integration logic

```python
from happy_hound_grooming_availability import determine_service_availability

result = determine_service_availability(
    category="Boarding",
    requested_date="2026-02-24",
    requested_start_hhmm="08:15",
    requested_service="A la Carte",
)

if result.available:
    print("Yes, that slot is available.")
else:
    if result.next_available_start:
        print(f"That slot is not available. The next available time is {result.next_available_start}.")
    else:
        print("That slot is not available.")
```

### Suggested natural-language policy

- If `reason == non_grooming_baked_in_available`:
  say that availability is open for that service.
- If `reason == available`:
  say the requested grooming slot is available.
- If `reason == groomer_capacity_full`:
  say the requested slot is unavailable because the grooming schedule is full.
- If `reason == outside_staffing_hours`:
  say the requested time is outside grooming hours.

---

## 15. Recommended request classification policy

Use grooming logic when either of these is true:

- the category is Grooming,
- or the requested service sounds like grooming.

Suggested grooming-like keywords:
- groom
- bath
- deluxe bath
- basic bath
- mini groom
- full groom
- a la carte
- shed-less
- de-skunk
- nail

This policy is important because a caller may say something like:
- “I need boarding and a bath”
- “Can I book daycare plus a mini groom?”
- “Can you do a last day bath with training?”

Those should trigger the grooming checker.

---

## 16. Command-line usage

### Live API usage

```bash
set GINGR_TENANT=happyhound
set GINGR_API_KEY=YOUR_API_KEY_HERE
set GINGR_API_BASE=https://happyhound.gingrapp.com/api/v1
set GINGR_LOCATION_ID=1
```

Then run:

```bash
uv run python happy_hound_grooming_availability.py --category Grooming --date 2026-02-21 --start 09:00 --service "Mini Groom"
```

### Mixed-category grooming example

```bash
uv run python happy_hound_grooming_availability.py --category Boarding --date 2026-02-24 --start 08:15 --service "A la Carte"
```

### Non-grooming example

```bash
uv run python happy_hound_grooming_availability.py --category Daycare --date 2026-02-24 --start 09:00
```

That non-grooming example should short-circuit to available without doing grooming math.

### Offline replay mode

You can replay saved reservation dumps instead of hitting the live API.

Example:

```bash
uv run python happy_hound_grooming_availability.py \
  --category Grooming \
  --date 2026-02-24 \
  --start 08:15 \
  --service "A la Carte" \
  --payload-file 2026-02-24=reservations_2026-02-24_to_2026-02-24.json
```

For next-available search across multiple days, you can pass multiple payload files:

```bash
uv run python happy_hound_grooming_availability.py \
  --category Grooming \
  --date 2026-02-21 \
  --start 15:45 \
  --service "Deluxe Bath" \
  --payload-file 2026-02-21=reservations_2026-02-21_to_2026-02-21.json \
  --payload-file 2026-02-22=reservations_2026-02-22_to_2026-02-22.json \
  --payload-file 2026-02-23=reservations_2026-02-23_to_2026-02-23.json \
  --payload-file 2026-02-24=reservations_2026-02-24_to_2026-02-24.json
```

---

## 17. Suggested staffing override file

If staffing changes over time, use a JSON staffing file.

Example:

```json
{
  "0": [],
  "1": [["07:00", "13:00", 1]],
  "2": [["07:00", "12:00", 1], ["12:00", "17:00", 2]],
  "3": [["07:00", "17:00", 1]],
  "4": [["07:00", "17:00", 1]],
  "5": [["07:00", "17:00", 1]],
  "6": []
}
```

Then pass:

```bash
uv run python happy_hound_grooming_availability.py --category Grooming --date 2026-02-21 --start 12:30 --service "Shed-less Bath" --staffing-file staffing.json
```

---

## 18. Important caveats

### 18.1 Daycare and boarding are simplified by business decision

The current business rule for this agent is to simply say those services have availability.

That is a deliberate product choice, not a claim that they are mathematically unlimited in every possible real-world situation.

### 18.2 Duration defaults are still a model

The code uses a duration map for new requests.

If the business wants size-specific or coat-specific durations, pass explicit duration or expand the duration resolution layer.

### 18.3 Next available time depends on loaded data

If running live, the next available search is only as accurate as the Gingr data and staffing rules.

If running offline, the next available search is only as accurate as the payload files supplied.

### 18.4 The code is intentionally conservative about what counts as groomer work

It counts both:
- services assigned to a groomer,
- and services whose names clearly look grooming-related.

That is the safest choice based on the payloads investigated.

---

## 19. What changed from the old approach

The earlier code in the project focused on daycare/boarding occupancy and did not actually perform grooming availability checks.

The final design changes that.

### Old mental model
- count reservations per day,
- compare with rough capacity,
- do not attempt grooming scheduling.

### New mental model
- daycare/boarding: baked-in availability response,
- grooming and grooming-like add-ons: real slot math,
- use same-day service intervals from Gingr,
- use staffing windows,
- use overlap checking,
- optionally suggest the next available slot.

---

## 20. Recommended next integration step

The recommended next step is to wire `determine_service_availability(...)` into the main agent and replace any older logic that:

- assumes grooming is not API-checkable,
- counts grooming by reservation totals,
- or ignores grooming add-ons attached to non-grooming reservations.

That integration is the natural final step after this investigation.

---

## 21. Final summary

This project started as a general availability-checking problem.

After reviewing the business email, Gingr documentation, exploratory scripts, raw reservation payloads, and live validation outputs, the final conclusion is:

- Daycare and boarding should be treated as available by product policy.
- Grooming should be checked with real Gingr data.
- Groomer capacity lives in **service-level scheduled intervals**, not in reservation counts.
- Groomer work can appear under grooming, boarding, daycare, and training reservations.
- The correct production endpoint is `POST /api/v1/reservations`.
- The correct production logic is overlap checking across same-day groomer-occupying service rows and staffing windows.

That is the implementation contained in `happy_hound_grooming_availability.py`.
