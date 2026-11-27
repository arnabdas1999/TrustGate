---
version: 1
slug: "web-src-app"
primary_target: "web/src/app"
related_targets: ["web/src/components"]
---

# TrustGate web app (investigations inbox and investigation page)

Mode: Operate. Audience: hackathon judges watching the demo video; the working user it portrays is
an engineer approving one exact Flagsmith change. Task: understand what broke, why TrustGate is
sure, what will change, then approve or reject. Constraints: Next.js 16, React 19, plain CSS,
self-hosted fonts, mock and real data, recorded on video Saturday.

## Direction contract

THESIS: An incident read as one short, honest decision note that ends in a single signature,
refusing the category default of a dashboard (sidebar, KPI tiles, card grids, tables up front).

OWN-WORLD: Light, quiet paper-white ground, near-black ink, neutral greys, hairline borders. Colour
only does jobs: amber needs you, blue running, green verified, red failed, and diff red/green on the
change. Geist for text, Geist Mono for flags, values and calls. Black primary button, Linear/Stripe
craft.

STORY: Visitor reads the headline (which flag broke which agent), sees the exact change as a diff,
reads four plain checks that prove it, optionally opens the detail (other repairs, search, exact call,
gate activity), then approves or rejects from the bar.

FIRST VIEWPORT: Narrow top bar (TrustGate, Inbox, demo-data chip). Centred 720px column: meta line
(agent, status, segment, time), a two-line sentence headline at ~34px, the change diff card, the
"Why we're sure" checklist. A sticky bar at the column's foot holds expiry, sign-in or signer, Reject
set apart, and Approve.

FORM: Pinned by the user, no roll position. seed: none (pinned): "Clean product, no metaphor" and "I
don't want this to look like another dashboard"; layout chosen by the agent after the user gave no
preference between the decision page and split review.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the
verdict, DESIGN.md, and every shipping raster carrying its provenance

## Signature interaction

The approve moment: the sticky bar is the only place an action lives; Approve presses with a 0.97
scale, morphs to "Applying…" then the bar turns to the verified read-back result in place.

## Unresolved

None.
