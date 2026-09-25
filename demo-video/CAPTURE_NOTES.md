# Demo-video capture notes

Created on 2026-09-25 for a detailed hackathon walkthrough. The finished edit is
4 minutes 10 seconds, landscape 1920×1080 at 30 frames per second. It uses actual
browser captures with animated framing, explanatory captions, original quiet
transition sounds, selectable English subtitles, and chapter markers. It is an
edited screenshot walkthrough, not an uninterrupted screen recording. There is
no generated voiceover or third-party music.

## Source and scope

- Current React console captured through the Codex browser. Case and graph
  close-ups are crops of those unaltered screenshots.
- The capture backend used a separate temporary root, local graph mode and
  disabled LLM narration. It read copies/references of the supplied inputs;
  runtime evidence and outputs stayed in that temporary root.
- HHG-014 shows a saved benchmark case and a current local prepared-data graph
  neighborhood. HHG-002 demonstrates an explicitly simulated confirmation in
  a saved answer. Historical records do not prove present service connectivity.
- A fresh local historical-data stream selected transaction `3478782`, with
  start `2016-11-22T17:27:07Z`, speed `0.1` and demo threshold `0.25`. It opened
  `CASE-538683EDCE28`. The stream was paused after the alert.
- A demo analyst L1 denial resumed that case, changing its evidence heuristic
  from 0.702 (uncertain) to 0.960 (fraud). Final routes were
  `CREATE_CASE/auto` and `BLOCK_CARD/L1`. Graph write was false, narration was
  template, and there were zero LLM attempts. This was not customer contact.
- Retrieval previews used available local graph/text evidence. No claims of
  complete vector coverage or live Gemini success appear in the video.
- The approval and ingestion screens were shown without submitting an approval
  or committing an import. No card block or external report filing occurred.
- The original detector configuration, secrets, graph, embedding checkpoints,
  graded answers, replay timelines and original transaction store were not
  changed. The 20 saved answers were revalidated for this task.

The local stream's start window was prepared through the backend API after the
browser date control did not persist the intended date. The video shows the
resulting actual app state and does not claim to record that setup interaction.

## Editorial structure

1. Queue and validated benchmark outputs.
2. Connected evidence, independent families, and bank precedents.
3. L2/SAR routing and a verification-driven change of recommendation.
4. Historical-data stream, alert, human evidence, and reassessment.
5. GraphRAG settings, source contributions, and exact narration context.
6. Approval boundaries, memory provenance, and import controls.
7. Validation proof and invitation to explore the demo.

Edit `scenes.json` to change copy, timing or focus crops, then rerun the renderer
as documented in `README.md`. Keep captions and disclosures accurate when
replacing captures. The final video is local; nothing was uploaded or published.
