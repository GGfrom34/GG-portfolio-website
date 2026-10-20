# PRD: Saved Searches for the Portfolio Case-Study Widget

## Problem

Visitors chatting with the Octopus Tariff Advisor widget on the portfolio
site often ask a similar handful of questions each session (e.g. "solar
plus EV, overnight charging, region C"). Right now every visit starts from
a blank input with no memory of a useful household profile from a previous
visit, so returning visitors re-type the same details every time.

## Goals

Let a visitor save the household profile they used for a recommendation
and reload it on a later visit, without requiring an account or login.

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
  device.

### 2. Load a saved profile

The visitor can open a list of their saved profiles and pick one to
re-populate the chat input, ready to send.

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

## Risks / open questions

- `localStorage` has no cross-browser or cross-device sync — if a visitor
  switches devices, their saved profiles won't follow them. Flagged for
  visibility, not solved in this PRD.
- Very old private-browsing modes may clear `localStorage` between
  sessions; treat this as expected/acceptable rather than a bug to fix.

---
