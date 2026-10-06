# Skills

Optional prompt shortcuts. Follow a shortcut only when the user's message uses that command. Apply it to the active document. If the user did not use a shortcut, ignore this file and answer normally. A shortcut updates the document only when that shortcut says to.

## /critique

Critique the whole active document. Be as full as possible. List every negative the text will support. Do not stop at one or two. Do not flatter the user and do not apologise. Quote or paraphrase the passage each point refers to. Do not treat a character's views as the author's. Do not change the document. Leave updated_content null.

Lenses, in this order:

- male: what assumes a male reader, and what shuts that reader out, without stereotyping
- female: the same, for a female reader
- feminist: power, agency, and gender on the page
- lgbtq+: sexuality and gender identity on the page
- logical: motivation, cause and effect, timeline, and contradictions
- literally: what the words actually say, including unclear pronouns, missing events, and a line that cannot mean what the scene seems to intend

If `/critique` is followed by a comma-separated list, use only those lenses. Match the names loosely (`male`, `female`, `feminist`, `lgbtq`, `lgbtq+`, `logical`, `literally`). With no list, use every lens above.

Number every point in one sequence for the whole reply, starting at 1. Do not restart the numbers for a new lens or for the fixes. The user will refer back to these numbers, such as "point 4" or "points 2 and 7".

If several lenses would make the same point, write it once under the nearest lens, then say which other critiques would agree. Do not repeat the point.

After all the critique points, add Suggested Fixes, ordered by priority, most worth doing first. Fixes are optional: if there are none, say so. Each fix continues the same number sequence and names the point numbers it addresses.

## /grammar

Correct the active document in en_GB and update it automatically. Use British spelling, including -ise and -our. Put the complete corrected document in updated_content whenever you change anything. Leave updated_content null only when the document needs no change.

In the reply, list the real changes, grouped as spelling, punctuation, grammar, and sense. For each one, quote the original snippet and the correction. If nothing changed, say so in one line.

Sense means: does the file read clearly? Correct missing words, broken sentences, unclear pronouns, tense shifts, and contradictions when the intended reading is clear. If a sentence could mean two different things, describe it in the reply and leave that sentence as it is.

Do not treat these as errors: names, invented words, dialect, or a fragment or comma splice that fits the narrative voice. Do not impose single or double quotes; only make quote marks consistent with the convention already used. Change nothing else.

## /continuity

`/consistency` is the same command. Check the active document only. Do not invent facts from other files. Do not change the document. Leave updated_content null.

Number every break in one sequence for the whole reply, starting at 1. Quote or paraphrase the passages that disagree. Look for:

- timeline: order of events, durations, and time of day that cannot all be true
- names: a person, place, or thing spelled or identified in more than one way
- facts: a detail that contradicts an earlier line in this document
- knowledge: a character using information they have not been shown

If nothing conflicts, say so in one line.

## /dialogue

`/dialog` is the same command. Read the speech in the active document. Do not change the document. Leave updated_content null.

Number every point in one sequence, starting at 1. Quote the lines you mean. Look for:

- two speakers who sound the same
- narration that is doing the talking for them
- a line where it is unclear who is speaking
- attribution that attaches a line to the wrong person

If the dialogue is clear, say so in one line. Do not offer rewrites.

## /synopsis

`/summary` is the same command. In the reply, give a short account of what this file actually does. Do not change the document. Leave updated_content null.

Cover what happens, who changes, and what a reader still does not know at the end. Keep it to a short paragraph. If the file has more than one thread, add a numbered list with one line per thread. Do not retell every sentence.
