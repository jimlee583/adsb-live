# adsb-live overview slide

## Audience
Someone who needs a one-slide explanation of what `adsb-live` is: a colleague, a demo host, or a project recap. They may know ADS-B exists, but not this desktop tool.

## Objective
In one 16:9 slide, explain that this is a local desktop RF waterfall for a 1090 MHz RTL-SDR, then show a real app screenshot so the audience can see the spectrum and waterfall immediately.

## Narrative arc
Single slide. Lead with the product name and a plain-language job-to-be-done. Support with three capabilities (see the RF, decode aircraft, record/replay). Anchor the claim with a live demo screenshot rather than a diagram.

## Slide list
1. **adsb-live** — What the app does, with a demo screenshot of the 1090 MHz PSD and scrolling waterfall.

## Source plan
- Project README and architecture notes in this repo.
- Live demo capture of the actual Qt window (`adsb-live --demo`), not a mock UI.

## Visual system
- Dark night-ops atmosphere behind a calm left copy column: deep navy, viridis green, cyan, and warm gold, matching the waterfall UI.
- Titles in Poppins, body/captions in Lato.
- Real screenshot sits in a rounded frame on the right. Generated art is texture/atmosphere only; all words are editable PowerPoint text.

## Imagegen plan
One text-free 16:9 plate. Abstract radio-wave atmosphere, faint constellation / aircraft-trail motifs, generous calm negative space on the left for copy. No labels, no fake UI chrome with readable text, no logos.

## Asset needs
- Generated art plate `slide-01.png` as full-bleed background with `fit: cover`.
- Real app screenshot of demo mode, placed as a framed image on the right.

## Editability plan
Title, kicker, body, three capability labels/bodies, screenshot caption, and footer are editable text boxes. Screenshot and art plate are images. No charts on this slide.
