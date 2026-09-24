# FRAUDLOOP console design

The overnight brief calls for a dense, readable fraud analyst console, dark by default, with a light theme and no emojis. These implementation choices follow that brief.

- Base: ink blue `#101720`; surfaces `#17212d`; borders `#2e3b4b`.
- Type: pale slate `#e9eef4`; secondary `#aab8c8`. Light theme uses paper `#f3f6f9`, white surfaces, and ink text.
- Focus and active controls: clear blue `#83b7fa`. Fraud red, cleared green, and uncertain amber carry meaning consistently.
- Typography: IBM Plex Sans for readable investigation prose; IBM Plex Mono for IDs and numeric values. Fonts are bundled locally.
- Layout: a compact navigation rail, persistent service status, and a full-width working surface. Queue rows carry the primary evidence summary; a case opens into facts, chronology, and recommendations.
- Keep explanations close to their evidence. Display routes, simulated replies, replay provenance, and graph-write status explicitly. A filing recommendation is never presented as an executed filing.
- Controls have visible focus, stable loading states, reduced-motion support, and readable contrast. Graphs supplement a keyboard-accessible entity list.
