---
version: 1
slug: "web-src-app"
primary_target: "web/src/app"
related_targets: ["web/src/components"]
---

# TrustGate web app (inbox hangar and investigation arena)

Mode: Operate. Audience: hackathon judges watching the demo video; the working user it portrays is
an engineer approving one exact Flagsmith change. Task: see what broke, why TrustGate is sure, what
will change, then approve or reject. Constraints: Next.js 16, React 19, plain CSS, React Three
Fiber scene with procedural characters (no downloaded assets), self-hosted fonts, mock and real
data, recorded on video Saturday.

## Direction contract

THESIS: The investigation as a living 3D stage the judge can watch and touch, refusing both the
dashboard (tiles and tables) and a static page: the search, the gate and the fix are things on a
stage, driven by the real investigation data.

OWN-WORLD: Twilight studio: deep blue-navy stage, soft key and rim light, soft white plastic
characters with team accents (OpsPilot amber, VaultGuard teal, Gatekeeper cobalt), glass sandbox
pods glowing green/red/amber by outcome, an amber energy field on the locked gate. Frosted navy HUD
panels, Geist and Geist Mono, no violet.

STORY: The visitor sees the agent robot holding the red culprit token beside its sandbox pods,
reads the story panel (which flag broke it, the fix, plain proofs), sees the exact change as a diff,
signs in and approves; the gate opens, the token turns green and flies to the Prod robot, which
turns green; Verified.

FIRST VIEWPORT: Full-viewport canvas. Top HUD bar (Inbox, agent and id, status, demo-data chip,
run budget, Evidence). Stage: pods arc on the left with a Sandbox label, agent robot centre with
the culprit token, Gatekeeper before the gate, Prod platform behind. Story panel bottom-left;
approval panel bottom-right holds the primary action.

FORM: Not from the roll's list position: seed:c5c08b5bf235b1cb rolled this round (an assigned
direction, then a re-roll, then a bolder re-roll); the user rejected all three hands, then said
"Clean product, no metaphor" and "I don't want this to look like another dashboard", and finally
asked for "immersive interactive ... graphics heavy not a plain linear dashboard ... some characters
or something" (context, not a pin). The agent offered three concepts (3D sandbox arena, isometric
control world, illustrated story); the user chose option 1, "3D sandbox arena", and the world
(twilight studio, soft plastic, navy) was the agent's.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the
verdict, DESIGN.md, and every shipping raster carrying its provenance

## Signature interaction

Approval opens the gate: doors slide apart, the amber field dissolves, the culprit token turns green
and flies along a curve to the Prod robot, which celebrates; green sparkles mark the verified fix.
Drag anywhere to look around (snaps back); hover or click a pod for its step.

## Motion grammar

Strong ease-out, entrances once on load (panels rise 10px and fade; pods scale in staggered),
hover only on hovering devices, buttons press to 0.97, drawer on the iOS drawer curve, everything
still under reduced motion.

## Unresolved

None.
