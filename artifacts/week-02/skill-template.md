---
name: your-skill-name-here
description: One or two sentences describing what this Skill does and — just as importantly — when it should be used. This is the single most important field in the whole document, so read the notes below before you write it.
---

<!--
==================================================================================
  HOW TO USE THIS TEMPLATE
==================================================================================
  1. Copy this whole file and rename it to something like: my-skill-name.md
  2. Fill in every [BRACKETED PLACEHOLDER] below with your own content.
  3. Delete these instructional comments (the text inside <!-- --> blocks)
     once you're done — they're just guardrails, not part of the finished Skill.
  4. Read each section's "What goes here" and "Example" before you write it.
     You do not need any coding experience to fill this out.
==================================================================================
-->

# [Skill Name — plain-English title, e.g. "Weekly Sales Report Builder"]

<!--
====================================================================
  SECTION 1: FRONTMATTER  (the block between the --- --- at the top)
====================================================================

  WHAT IT IS:
  The frontmatter is a small block of structured information at the very
  top of the file, wrapped between two "---" lines. Think of it like the
  label on a folder in a filing cabinet — it's what lets a system (or a
  person) quickly identify what's inside without opening the whole thing.

  WHY IT MATTERS:
  This is the ONLY part of the Skill that gets scanned automatically to
  decide "should I use this Skill right now?" If the frontmatter is vague,
  the Skill will either never get used, or get used at the wrong times.
  Everything below the frontmatter (the instructions) is only read AFTER
  the Skill has already been selected — so the frontmatter carries all
  the weight of getting selected correctly in the first place.

  FIELDS TO FILL IN:

  name:
    - A short, unique, machine-friendly identifier for this Skill.
    - Use lowercase words separated by hyphens (this is called "kebab-case").
    - No spaces, no punctuation other than hyphens.
    - Good:  weekly-sales-report, customer-email-drafter, meeting-notes-cleanup
    - Bad:   Weekly Sales Report!, WeeklySalesReport, my_skill_v2_FINAL

  description:
    - This is the most important sentence you will write in this document.
    - It must answer TWO questions at once:
        (a) WHAT does this Skill do?
        (b) WHEN should it be used (and, if helpful, when should it NOT
            be used)?
    - Write it the way you'd explain the Skill to a new coworker in one
      breath — specific enough that they'd know exactly when to reach
      for it, not just what it's broadly "about."
    - Avoid vague marketing language ("helps with reports"). Be concrete
      ("turns a raw weekly sales CSV export into a formatted summary
      with totals, top products, and week-over-week change").
    - If there are trigger phrases a user might say, it can help to
      include a few, e.g. "Use when the user asks to 'summarize this
      week's sales' or 'build the weekly sales report.'"

  EXAMPLE (filled in):
    ---
    name: weekly-sales-report
    description: Turns a raw weekly sales CSV export into a clean,
      formatted summary showing total revenue, top 5 products, and
      week-over-week percent change. Use when the user asks to
      "summarize sales," "build the weekly report," or shares a sales
      CSV/export and asks what happened this week. Do not use for
      monthly or quarterly reports — see the monthly-sales-report Skill
      for those.
    ---
-->

---
name: [your-skill-name-here]
description: [One to three sentences: what it does + when to use it + (optional) when NOT to use it]
---

<!--
====================================================================
  SECTION 2: DETAILED DESCRIPTION
====================================================================

  WHAT IT IS:
  A longer, plain-English explanation that lives in the body of the
  document (not the frontmatter). While the frontmatter description has
  to be short, this section can be as thorough as it needs to be. This
  is where you explain the "why" and the "how it fits into the bigger
  picture" — not just the "what."

  WHAT GOES HERE — answer each of these in a sentence or two:

  1. Purpose
     What problem does this Skill solve? What was frustrating or slow
     before this Skill existed?

  2. Who it's for
     Is this for a specific role, team, or type of request? (e.g.
     "for anyone preparing the Monday leadership standup")

  3. When to use it
     What does a request look like when this Skill should kick in?
     Give a few example phrases a person might actually type or say.

  4. When NOT to use it
     Are there similar-looking requests this Skill should NOT handle?
     Naming the boundary prevents mix-ups with other Skills.

  5. Inputs it expects
     What does someone need to have ready or provide before this Skill
     can do its job? (a file, a date range, an account name, a URL...)

  6. Outputs it produces
     What will the person have in hand when the Skill is finished?
     (a document, an email draft, a table, a chart, a checklist...)

  EXAMPLE (filled in):

  ## Purpose
  Building the weekly sales report by hand takes about 30 minutes of
  copy-pasting numbers between spreadsheets and formatting them into a
  readable summary. This Skill automates that so it takes under a minute.

  ## Who it's for
  Sales operations staff and managers who send a Monday morning summary
  to the leadership team.

  ## When to use it
  Use this Skill when someone:
  - Shares a CSV export from the sales system and asks for a summary
  - Says "build this week's sales report"
  - Says "what were our top products this week?"

  ## When NOT to use it
  Do not use this for monthly, quarterly, or annual reports — those
  use a different format and a different Skill. Do not use it if the
  data provided is not a weekly export (e.g. a single day's data).

  ## Inputs it expects
  - A CSV file exported from the sales system, containing at minimum:
    date, product name, quantity sold, revenue
  - (Optional) the previous week's totals, for comparison

  ## Outputs it produces
  A formatted summary containing: total revenue for the week, the top
  5 products by revenue, and the percent change versus last week.
-->

## Detailed Description

**Purpose:**
[What problem does this Skill solve? What was slow, repetitive, or error-prone before it existed?]

**Who it's for:**
[Which person, role, or team will use this?]

**When to use it:**
[List a few example requests or phrases that should trigger this Skill]
-
-
-

**When NOT to use it:**
[Name any similar-looking requests this Skill should NOT handle, to avoid confusion with other Skills]
-
-

**Inputs it expects:**
[What must the person provide or have ready before this Skill can run?]
-
-

**Outputs it produces:**
[What will exist when the Skill is done — a document, a list, an email draft, a chart, etc.?]
-
-

---

<!--
====================================================================
  SECTION 3: INSTRUCTION BODY
====================================================================

  WHAT IT IS:
  This is the actual "how-to" — the step-by-step recipe that gets
  followed once this Skill has been chosen. Think of it as a very
  detailed set of instructions you'd hand to a capable new employee on
  their first day: they're smart and can use good judgment, but they
  have never done this specific task before and don't know your
  team's conventions.

  HOW TO WRITE GOOD INSTRUCTIONS:

  - Use numbered steps in the order they should happen. Numbered lists
    are easier to follow than paragraphs.
  - Be specific rather than general. Instead of "format it nicely,"
    say exactly what "nicely" means (e.g. "use a bulleted list with the
    product name in bold and revenue in parentheses").
  - Call out decision points. If step 3 could go two different ways
    depending on the situation, spell out both paths and how to choose
    between them ("If revenue is missing for a product, skip it and
    note it was excluded — do not guess a number").
  - Include a worked example. Showing one full example from start to
    finish (a sample input and the exact output it should produce) is
    often more useful than any amount of prose explanation.
  - List edge cases and how to handle them. What should happen if the
    input is incomplete, empty, unusual, or contradicts expectations?
  - State what "done" looks like. How does someone know the task was
    completed successfully?
  - Avoid vague words like "appropriately," "properly," or "as needed"
    without defining what they mean in this context.

  EXAMPLE (filled in):

  ## Steps

  1. Open the provided CSV file and confirm it has these columns:
     date, product name, quantity sold, revenue. If any column is
     missing, stop and ask the user to re-export the file.

  2. Calculate total revenue by summing the revenue column.

  3. Identify the top 5 products by revenue (highest to lowest). If
     two products tie, list them in alphabetical order.

  4. If the previous week's total was provided, calculate the percent
     change: ((this week - last week) / last week) x 100, rounded to
     one decimal place. If not provided, skip this and simply note
     "no prior week data provided."

  5. Format the summary using this exact structure:

     ---
     Weekly Sales Summary — [date range]
     Total Revenue: $[amount]
     Week-over-Week Change: [+/-X.X]% (or "No prior data")

     Top 5 Products:
     1. [Product Name] — $[revenue]
     2. [Product Name] — $[revenue]
     ...
     ---

  6. Double-check that the total revenue and the sum of the top 5
     products' revenue are consistent (top 5 should never exceed the
     total). If they don't reconcile, flag it rather than guessing.

  ## Edge Cases

  - Empty file: tell the user no data was found and ask them to check
    the export.
  - Duplicate rows for the same product/date: combine them before
    calculating totals.
  - Negative revenue (e.g. a refund): include it in the total but
    exclude it from the "top 5 products" ranking.

  ## Done When

  The person has a complete summary in the exact format above, with
  all numbers double-checked against the source file.
-->

## Instructions

### Steps

1. [First step — be specific about exactly what to do]
2. [Second step]
3. [Third step]
4. [Continue numbering as needed...]

### Decision points

[If any step could branch depending on the situation, explain each path clearly]

- If [condition], then [what to do]
- If [condition], then [what to do]

### Worked example

**Sample input:**
[Show a realistic example of what the input looks like]

**Expected output:**
[Show exactly what the finished result should look like, formatted the way it should actually appear]

### Edge cases to handle

- [Situation — e.g. missing information] → [what to do about it]
- [Situation — e.g. conflicting or unusual data] → [what to do about it]
- [Situation — e.g. empty input] → [what to do about it]

### How to know it's done

[Describe what "finished and correct" looks like, so anyone can check the result against this checklist]

---

<!--
====================================================================
  QUICK CHECKLIST BEFORE YOU SHIP THIS SKILL
====================================================================
  [ ] The "name" field is short, lowercase, and hyphenated
  [ ] The "description" field explains both WHAT and WHEN in 1-3 sentences
  [ ] The Detailed Description section is filled in completely
  [ ] The Instructions section has clear, numbered, specific steps
  [ ] At least one worked example is included
  [ ] Common edge cases are listed with a clear response for each
  [ ] All [BRACKETED PLACEHOLDERS] have been replaced with real content
  [ ] All instructional comment blocks (like this one) have been deleted
====================================================================
-->