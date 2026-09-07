# How the docs should sound

The fleet's copy of its writing voice lives in crossband. Membro and
spendglass point here.

This is the fleet's writing voice, for every doc in every repo. It's
for whoever writes or rewrites a page, and for whoever reviews one.
The examples are the standard. The rules at the end are a backstop
that catches the mechanical habits of machine writing, and a page can
pass every one of them and still fail [the test](#the-test).

## The test

Read each sentence as if you're explaining the app to a smart friend
who's never seen it. If you wouldn't say it like that, rewrite it
until you would. Say what the thing does, in the words the reader
would use to tell someone else. Don't try to sound impressive.

## What it sounds like

Each example is real crossband material. When a doc covers the same
ground, it should read like this.

A setup step:

> You'll need one key for voice. Put your ElevenLabs key in `.env` as
> `ELEVENLABS_API_KEY` and restart the app. Each model then speaks in
> its own voice. Speech goes through the backend, so the key never
> reaches the browser. If you don't add a key, voice is off and
> everything else still works.

The first sentence says what you need, the second says what to do,
and the last says what happens if you don't.

A feature, for someone who's never seen it:

> Room mode is for when more than one person is talking to the models.
> When someone speaks, the app compares the voice with the voices it's
> learnt. That check happens on the computer the app runs on, in well
> under a second. If it knows
> the voice, it puts that person's name on the turn, so the models
> know who said what. If it doesn't, it asks who's joined. The small
> model it uses for this is downloaded once and then works offline.

Each sentence is one step of what happens, and you could explain it
to someone else after one read.

A limitation:

> The phrases the app listens for, like "this is Dave" or "solo mode",
> came from one household speaking English. They won't cover
> everything your household says. When a phrase is missed, the app
> still recognises a voice it already knows, still asks who a new
> voice is, and you can always switch room mode by hand in the voice
> settings.

The limitation is stated straight, in its own sentence, and the next
sentence says what still works.

An operations note:

> Before a deploy restarts an app, the watcher asks the app whether
> it's busy. Crossband says yes while a round is running, a voice
> capture is open, a guest visit is running, or a sync pass is in
> progress. The watcher waits up to fifteen minutes, checking every
> twenty seconds, and posts one line in chat while it waits. If the
> app's still busy after that, the deploy is skipped and the chat says
> why, so you can run it again later.

What happens, in the order it happens, with the numbers you'd want
and nothing you wouldn't.

A warning:

> Never open the port to the internet, and never use Tailscale Funnel.
> The app is built to be reached over your own tailnet and nowhere
> else. Anyone who can reach the port still needs the owner password
> or a passkey, but that's the second lock, not the first.

The instruction comes first and the reason second, and the reason is
a fact about the app, not a scare.

A troubleshooting entry:

> If memory search and voice sync both go quiet after you rotate the
> membro token, the two copies of `MEMORY_AUTH_TOKEN` no longer match.
> Crossband's log says so: "membro refused /search: MEMORY_AUTH_TOKEN
> in crossband's .env no longer opens membro". Copy membro's value
> into crossband's `.env` and restart crossband.

Symptom, cause, the exact line to look for, fix. Nothing between the
reader and the fix.

A changelog entry:

> Forgetting a voice now reaches memory too. Forget used to delete the
> audio only on the computer the app runs on, and membro kept its copy and could hand
> it back on the next sync. Now the forget goes to membro on the next
> sync pass, membro deletes its audio, and the facts it learned from
> that person go back to review. A forget that membro can't take yet
> waits for the next pass instead of being dropped.

What changed for the person, in their words, with the mechanism only
as far as it explains the change. The issue number goes at the end of
the entry.

What the seven share: short words, the first sentence says what the
paragraph is about, each sentence does one job, the app or the reader
is the subject, numbers where you'd want them, and no sentence is
there to sound good.

## The rules behind it

Rules marked (CI) are checked by each repo's `tests/test_doc_style.py`.
Everything else is for the reviewer.

Sentences:

- One claim per sentence. Keep the average under 18 words, fewer
  than one in ten over 35, and none over 55 (CI).
- Lead with the claim. The reason or the caveat gets its own
  sentence. Don't open a sentence with "So", "Because", "Since",
  "Given" or "Otherwise" (CI).
- Every sentence has a verb. A bold label at the start of a list item
  is a heading, not a sentence.
- A paragraph is one thought. Its first sentence says what the thought
  is, and the rest develops it. Join sentences that belong together, and
  keep a short sentence for landing a point.
- When you introduce something from outside the app, such as launchd,
  Tailscale or a protocol, link its public documentation at the first
  mention, so a reader who wants more has somewhere to go. Explain it
  in a sentence first. The link is for depth.

Punctuation:

- No dashes in prose: no em-dash, no en-dash, no hyphen with a space
  either side (CI). A hyphen only joins the halves of one word.
- No semicolons (CI). One colon per sentence, and only to introduce a
  list, a command or a quoted value (CI).
- Brackets hold a name, a value or a pointer, under eight words (CI),
  and no sentence starts with one (CI). If it has a verb, it's a
  sentence of its own.
- Bold only marks a term where it's defined or a label the reader
  sees on screen. Capitals are for acronyms (CI).

Words:

- Plain words, Australian English, contractions the way you'd speak
  them, and the reader is "you".
- A word the code uses ("seat", "bank", "vouch", "arm") comes after
  the sentence that says what it means.
- Don't use "actually", "genuinely", "really", "simply", "literally"
  or "truly" (CI). "Exactly" only goes before a number or a value (CI).
- "Largely", "mostly", "typically", "usually", "roughly" and "in
  practice" stand in for a missing fact. Give the number or the
  condition, or say nobody has measured it.
- Don't say "honest", "honestly", "deliberately", "on purpose", "by
  design" or "a conscious choice" (CI). Say what the app does and,
  when the reason matters, why.
- "Rather than", "instead of", "not just" and "X, not Y" only when the
  reader would otherwise assume Y. At most one per 50 sentences (CI,
  checked at 2 per 100).

The document:

- Say what's true today. No issue numbers, no "used to", "no longer",
  "previously" or "any more" in reference prose (CI). History lives in
  the changelog and the issue.
- The doc doesn't talk about itself: no "this page", "above", "below"
  or "the next section" (CI). A pointer is a link to a heading.
- Say a thing once, in one repo. Two copies of a rule is one copy that
  will go stale.
- A heading every 30 to 50 lines of prose (CI at 50). A table cell
  holds a value and one sentence, under 45 words (CI).
- Changelog entries use the same voice. They may name the issue, at
  the end.

## The pass

Every rewrite, and every new page, goes through these steps in this
order. The first five are judgement. The last one is the machine, and
it can't hear the voice, so the judgement steps are still yours.

1. Keep every fact. List the facts, commands, settings and numbers on
   the old page before you touch it, and check them off at the end.
2. The voice, one sentence at a time. Read each sentence as if to a
   smart friend who's never seen the app, and rewrite it until you'd say
   it like that.
3. The flow, one paragraph at a time. Each paragraph is one thought,
   its first sentence says what that thought is, and the instruction
   comes before the mechanism. Join what belongs together, and keep a
   short sentence for landing a point.
4. Explain, then link. A term from the code gets a sentence that says
   what it means before it's used. Something from outside the app gets
   that sentence and a link to its public documentation at the first
   mention.
5. A diagram only where there's a mechanism to show, following the
   Diagrams section.
6. Add the page to the converted list and run the checks. Fix the
   writing, never the check.

## Diagrams

A diagram earns its place when it shows something you'd otherwise have
to build up from prose: where data flows, which parts talk, what state
a thing moves through. If a sentence says it faster, write the
sentence. A box with the name of a thing in it says less than the
prose did.

Pick the shape that fits. A flow of parts and arrows for architecture.
A sequence diagram for what happens in one round or one request. A
state diagram for modes and what moves between them. A flow with
diamonds for a procedure with decisions.

Every diagram is a Mermaid block in the page, never an image file, so
it's reviewed, versioned and searched like text. The one exception is
a screenshot of the real app.

GitHub draws the diagram in whichever mode the reader has on, and it
swaps its own line and text colours to suit. We leave GitHub's theme
alone and set only the fills, with dark text on every fill, which reads
on white and on dark alike. This was checked on GitHub in both modes.

For a flow, paste these lines at the end of the block. Put every node
in the `node` class, and the one thing the diagram is about in `hero`.

```
classDef node fill:#d4d4d8,stroke:#757575,color:#18181b
classDef hero fill:#38bdf8,stroke:#0284c7,color:#18181b,stroke-width:2px
classDef bad fill:#fca5a5,stroke:#dc2626,color:#18181b
style <group id> fill:transparent,stroke:#757575,color:#757575
```

For a sequence diagram, start the block with this line.

```
%%{init: {"themeVariables": {"actorBkg": "#d4d4d8", "actorBorder": "#757575", "actorTextColor": "#18181b", "noteBkgColor": "#38bdf8", "noteTextColor": "#18181b", "noteBorderColor": "#0284c7", "activationBkgColor": "#38bdf8", "activationBorderColor": "#0284c7"}}}%%
```

The rules:

- Shapes mean things. A pill is a person. A rounded box is a model or
  a service. A square box is a thing inside the app. A cylinder is a
  store. A diamond is a decision.
- One accent. The sky blue goes on the one thing the diagram is about.
  Red only on a failure path. Everything else stays grey.
- Label the arrows with what moves: "types", "read, reply", "checks
  again in 20s". Leave one bare only when its label would repeat the
  one beside it.
- The doc's own words, in the same voice as the prose. A term from
  the code appears in a diagram only after the prose has said what it
  means.
- Twelve nodes at most, and left to right. If it needs more, it's two
  diagrams or a table. Group only for a real boundary such as a
  computer, a repo or a service, and don't set a direction inside a
  group, because GitHub ignores it and stacks the group into a column.
- Never a theme block that fixes every colour. In dark mode GitHub
  paints such a diagram on a white slab.
