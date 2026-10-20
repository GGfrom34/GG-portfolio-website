---
name: improve-prd
description: Walks the user through fixing the gaps a PRD review found — tightening the problem statement, success metrics, scope, acceptance criteria, edge cases, non-functional requirements, risks, and rollout plan — one rubric shared with prd-quality-reviewer, applied interactively with the user's sign-off on each change. Use after prd-quality-reviewer has flagged gaps, or whenever the user asks to improve, tighten, flesh out, or fill in a PRD.
---

# Improving a PRD

## Why this is one skill, not a library split by gap type

It would be possible to split this into per-dimension skills (e.g. a skill
just for tightening success metrics, another just for scope). That was
considered and rejected: a PRD's gaps are usually correlated — vague scope
usually comes with vague acceptance criteria in the same sections — and
having one skill share the exact same rubric as `prd-quality-reviewer`
keeps the two pieces speaking the same vocabulary instead of drifting apart
over time. Don't split this skill into a library unless a specific
dimension turns out to need materially deeper, reusable treatment than the
general rubric below gives it.

## The rubric (same eight dimensions as `prd-quality-reviewer`)

1. Problem statement / context
2. Success metrics (specific, measurable, with a measurement plan)
3. Scope (explicit in *and* out)
4. Requirements with concrete, testable acceptance criteria
5. Edge cases and error states
6. Non-functional requirements
7. Dependencies, risks, open questions (named, not discovered later)
8. Rollout / rollback plan

## How to work through it

Don't rewrite the whole PRD at once. For each dimension the user wants to
address (typically the Blockers and Gaps `prd-quality-reviewer` flagged,
taken one at a time):

1. **Ask a targeted clarifying question** specific to that gap — not "tell
   me about your success metrics" but something answerable in one or two
   sentences, e.g. "What number would tell you this feature worked — and
   over what time window?"
2. **Propose concrete replacement or added text** based on the answer,
   written the way the rest of the PRD is written (match its tone and
   section structure — don't impose a different template).
3. **Show it as a before/after** — the current text (or "currently absent")
   next to the proposed text — so the user is approving a specific change,
   not a vague direction.
4. **Get explicit sign-off before writing it into the PRD file.** Move to
   the next gap only after this one is either accepted, edited, or
   explicitly deferred by the user.

If the user wants to defer a gap (e.g. "we'll figure out rollback later"),
that's a valid outcome — note it as an explicitly acknowledged open item
rather than pushing to resolve everything.

## Worked example

> Reviewer flagged: **Gap — Success metrics** — "the PRD says this feature
> will 'improve user engagement' with no target or measurement window."
>
> **Clarifying question:** "What number would tell you this worked, and
> over what time window — e.g. weekly active users, task completion rate,
> something else?"
>
> **User answer:** "If 40% of users who see the new onboarding flow
> complete it within their first session."
>
> **Proposed text** (replacing "improve user engagement"): *"Success
> metric: 40% of users who are shown the new onboarding flow complete it
> within their first session, measured over the first 30 days post-launch."*
>
> User approves → the PRD file is edited to replace the vague line with
> this one.

## Verification

Once the flagged gaps have been worked through, re-run
`prd-quality-reviewer` against the updated PRD rather than assuming the
changes landed correctly. This catches cases where a fix addressed the
letter of a gap but not really the substance (e.g. a success metric was
added but still isn't actually measurable), and confirms no new gap was
introduced by the edits.
