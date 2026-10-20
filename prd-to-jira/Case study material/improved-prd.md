# PRD: Saved Searches for the Portfolio Case-Study Widget

*Draft v0.3 — improved from v0.1 via the `prd-quality-reviewer` /
`improve-prd` workflow, including a second review pass on the fixes
themselves.*

## Problem

Visitors chatting with the Octopus Tariff Advisor widget on the portfolio
site often ask a similar handful of questions each session (e.g. "solar
plus EV, overnight charging, region C"). Right now every visit starts from
a blank input with no memory of a useful household profile from a previous
visit, so returning visitors re-type the same details every time.

## Goals

Let a visitor save the household profile they used for a recommendation
and reload it on a later visit, without requiring an account or login.

**Success metric:** At least 20% of returning-visitor sessions that have a
saved profile in `localStorage` result in that profile being loaded,
measured over the first 30 days post-launch (profile-load events ÷
sessions where a saved profile exists on page load).

## Scope

**In scope:**
- A "Save this profile" button shown alongside a recommendation result.
- Saved profiles are stored in the visitor's browser (localStorage), named
  by the visitor at save time.
- A "Load a saved profile" control that lists the visitor's saved profiles
  and re-populates the chat input.
- Visitors can delete a saved profile.

**Out of scope:**
- Cross-device sync (no accounts in this project).
- Saving anything server-side.
- Sharing a saved profile with another visitor.
- Renaming a saved profile (no dedicated rename control — achieved via
  delete + re-save under a new name).

## Features

### 1. Save a profile

After a recommendation is shown, the visitor can click "Save this
profile" and give it a short name. The profile (the household inputs used
for that recommendation) is stored in `localStorage` under that name.

**Acceptance criteria:**
- The Save button appears only after a recommendation has been returned,
  not before.
- If the visitor tries to save with a name that's already in use, they're
  asked to confirm overwriting it or pick a different name.
- A saved profile survives a page reload and a new browser tab on the same
  device, outside private-browsing modes that clear storage on session end
  (see Risks).
- A visitor can save up to 10 profiles. Overwriting an existing name
  replaces that profile in place and does not count against the cap; once
  at 10, only overwriting an existing name is allowed — the Save button is
  disabled for a new name until an existing profile is deleted.
- If a save fails because `localStorage` is full, the visitor sees an
  error (e.g. "Couldn't save — storage is full") and the profile is not
  saved; no existing profile is deleted automatically.
- Profile names must be non-empty after trimming whitespace, and are
  capped at 40 characters. Names are rendered as plain text in the list
  (never as HTML), so no character is unsafe to include.

### 2. Load a saved profile

The visitor can open a list of their saved profiles and pick one to
re-populate the chat input, ready to send.

**Acceptance criteria:**
- If the visitor has no saved profiles, the "Load a saved profile" control
  is shown in a disabled/empty state (e.g. "No saved profiles yet") rather
  than opening an empty list.
- Selecting a profile populates the chat input with that profile's
  household inputs but does **not** auto-submit — the visitor must click
  send themselves.
- If the chat input already contains text (typed or previously loaded)
  when the visitor selects a profile to load, they're asked to confirm
  before it's replaced.
- The list shows each saved profile's name; selecting one closes the list
  and returns focus to the chat input.
- If a stored profile's data is unreadable or malformed (e.g. edited
  outside the app), it's shown in the list as an unreadable entry that can
  only be deleted, rather than causing the Load list or the rest of the
  app to error.
- The saved-profiles list is not live-synced across tabs; it reflects
  current state when opened. Selecting an entry that was deleted in
  another tab is handled the same as a missing/unreadable entry (see
  above).

### 3. Delete a saved profile

The visitor can remove a saved profile they no longer want from the list.

**Acceptance criteria:**
- Deleting asks for confirmation before removing.
- The list updates immediately after deletion, with no page reload
  required.

## Non-functional requirements

- Must continue to work with the existing per-IP and global daily token
  budgets on the backend — saved profiles are a pure frontend feature and
  make no additional backend calls beyond the existing `/chat` endpoint.
- No personally identifying information should ever be part of a saved
  profile (household inputs like region letter and usage figures are not
  PII on their own).
- Saved profiles store a last-used timestamp, updated whenever they're
  saved or loaded. On each page load, any saved profile whose last-used
  timestamp is more than 6 months old is pruned before the list renders.
- The Save, Load, and Delete controls (including the profiles list and
  delete-confirmation dialog) must be keyboard-operable and have
  accessible labels, consistent with the rest of the widget.
- Depends on the existing chat widget's input API to populate the chat
  input, and on the existing `/chat` endpoint and its per-IP/global token
  budgets (no new backend surface is introduced).

## Rollout / rollback plan

Ships in a single PR with no feature flag. If an issue is found
post-launch, rollback is a straight revert of that PR — saved profiles are
pure client-side `localStorage` state, so no data migration or backend
coordination is needed either way.

## Risks

- `localStorage` has no cross-browser or cross-device sync — if a visitor
  switches devices, their saved profiles won't follow them. Flagged for
  visibility, not solved in this PRD.
- Very old private-browsing modes may clear `localStorage` between
  sessions, which also affects the "survives a new browser tab" guarantee
  in Feature 1's acceptance criteria; treat this as expected/acceptable
  rather than a bug to fix.
- Two tabs saving or deleting profiles at nearly the same time can race,
  since `localStorage` writes aren't coordinated across tabs — the last
  write wins and an earlier change in another tab may be silently lost.
  Accepted as a low-probability edge case given saved profiles are a
  convenience feature, not solved in this PRD.

## Open questions

*(none currently — all previously open items above have an accepted
resolution)*
