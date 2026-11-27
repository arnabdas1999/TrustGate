---
name: TrustGate
description: The investigation as a living 3D stage, with a frosted heads-up display floating over it.
colors:
  scene: "#0e1a2d"
  scene-raised: "#15243a"
  panel: "rgba(13, 22, 38, 0.72)"
  panel-solid: "#111d31"
  panel-line: "rgba(214, 232, 255, 0.12)"
  panel-line-strong: "rgba(214, 232, 255, 0.22)"
  well: "rgba(2, 8, 20, 0.45)"
  ink: "#f2f6fc"
  ink-2: "#c3cfdf"
  ink-3: "#93a3b8"
  amber: "#fbbf24"
  sky: "#7dd3fc"
  green: "#4ade80"
  green-hover: "#6ee7a0"
  on-green: "#052e16"
  red: "#f87171"
  del-ink: "#fda4a4"
  ins-ink: "#86efac"
  plastic-white: "#f1f2f7"
  prop-white: "#e8eaf2"
  visor: "#0d1426"
  team-opspilot: "#f59e0b"
  team-vaultguard: "#14b8a6"
  team-gatekeeper: "#3b82f6"
  token-culprit: "#ef4444"
  token-fixed: "#22c55e"
  sealed-slate: "#64748b"
  zone-ring: "#3d7cc9"
  key-light: "#d6e8ff"
  rim-light: "#9cc9ff"
typography:
  display:
    fontFamily: "Geist Sans, Geist, ui-sans-serif, system-ui, sans-serif"
    fontSize: "40px"
    fontWeight: 600
    lineHeight: 1.08
    letterSpacing: "-0.035em"
  headline:
    fontFamily: "Geist Sans, Geist, ui-sans-serif, system-ui, sans-serif"
    fontSize: "24px"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "-0.025em"
  title:
    fontFamily: "Geist Sans, Geist, ui-sans-serif, system-ui, sans-serif"
    fontSize: "16px"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "-0.02em"
  body:
    fontFamily: "Geist Sans, Geist, ui-sans-serif, system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.55
  body-sm:
    fontFamily: "Geist Sans, Geist, ui-sans-serif, system-ui, sans-serif"
    fontSize: "13.5px"
    fontWeight: 400
    lineHeight: 1.55
  label:
    fontFamily: "Geist Sans, Geist, ui-sans-serif, system-ui, sans-serif"
    fontSize: "12.5px"
    fontWeight: 500
    lineHeight: 1.3
  mono:
    fontFamily: "Geist Mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "13px"
    fontWeight: 500
    lineHeight: 1.6
    fontFeature: "tnum"
rounded:
  xs: "4px"
  sm: "6px"
  md: "8px"
  pill: "999px"
spacing:
  xs: "6px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "28px"
  2xl: "32px"
components:
  button:
    backgroundColor: "rgba(214, 232, 255, 0.06)"
    textColor: "{colors.ink}"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    padding: "0 16px"
    height: "44px"
  button-hover:
    backgroundColor: "rgba(214, 232, 255, 0.12)"
  button-primary:
    backgroundColor: "{colors.green}"
    textColor: "{colors.on-green}"
    rounded: "{rounded.sm}"
    padding: "0 16px"
    height: "44px"
  button-primary-hover:
    backgroundColor: "{colors.green-hover}"
  button-reject:
    backgroundColor: "transparent"
    textColor: "{colors.red}"
    rounded: "{rounded.sm}"
    padding: "0 16px"
    height: "44px"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.ink-3}"
    rounded: "{rounded.sm}"
    padding: "0 8px"
    height: "32px"
  input:
    backgroundColor: "{colors.well}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "0 12px"
    height: "44px"
  hud-card:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "18px 20px"
  status-pill:
    rounded: "{rounded.pill}"
    typography: "{typography.label}"
    padding: "0 10px"
    height: "24px"
  scene-tag:
    backgroundColor: "rgba(10, 18, 32, 0.82)"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "6px 11px"
  diff-card:
    backgroundColor: "rgba(2, 8, 20, 0.35)"
    textColor: "{colors.ink}"
    typography: "{typography.mono}"
    rounded: "{rounded.sm}"
  evidence-drawer:
    backgroundColor: "{colors.panel-solid}"
    textColor: "{colors.ink}"
    width: "min(760px, 100vw)"
---

# Design System: TrustGate

## Overview

**Creative North Star: "The Twilight Studio"**

An investigation is staged, not tabulated. A deep navy studio floor under soft key and rim light holds a small cast of soft white plastic robots: the agent under test in its team colour, its replays sealed in glass sandbox pods that glow by outcome, a cobalt Gatekeeper before an amber energy-field gate, and a Prod robot on a platform behind it. The search, the culprit, the gate and the fix are objects on that stage, driven by real investigation data, and the person can drag to look around and click a pod to read its step.

The interface is a heads-up display floating over the scene: frosted navy panels with hairline edges, Geist for words and Geist Mono for anything a machine produced (flags, values, ids, times, calls). Density is calm: one story panel, one approval panel, one top bar; everything else lives in the evidence drawer. Colour does jobs only, and the same four status colours carry meaning on the HUD and in the scene, so a red pod, a red pill and a red diff line all say the same thing.

Motion is strong ease-out and spent once: panels rise and fade on load, pods scale in staggered, characters idle softly, and the single dramatic moment is approval, when the gate opens and the culprit token turns green and flies to Prod. Under reduced motion every element holds a still, legible pose.

**Key Characteristics:**
- Full-viewport React Three Fiber stage; procedural characters from primitives, no downloaded assets.
- Frosted HUD panels over the scene; a solid panel only where it covers the scene entirely (drawer).
- Four status colours with fixed jobs shared by HUD and scene; team colours live on characters.
- Geist Sans and Geist Mono, self-hosted; tabular numerals for every value.
- Sharp-ish radii (6px and 8px) for a developer tool; pills only for status.
- Dark only (`color-scheme: dark`); no violet anywhere.

## Colors

A twilight navy field with soft white plastic, lit by four saturated status colours that each mean one thing.

### Primary
- **Verified Green** (green): the one action colour and the "verified" state. Fills the Approve button (text in Deep Forest on-green), the brand mark, done timeline nodes, passing pods, the chosen repair row tint, evidence callouts, inserted diff lines (ins-ink), the opened gate ring and the celebration sparkles at Prod.

### Secondary
- **Needs-You Amber** (amber): waiting on a human. The proposed status pill, the waiting approval panel's edge glow and countdown, the locked gate's energy field, inconclusive pods, the dashed demo-data chip.
- **Running Sky** (sky): work in progress and focus. Live status pills, the run-budget meter, links, caret, selection, focus rings, the robots' idle and searching antenna and eye light, the gate while the change is being written.
- **Failed Red** (red): broken or refused. Failed pods, culprit flags in code chips, deleted diff lines (del-ink), refused log actions, read-back failures, the "Prod not yet fixed" label dot.

### Tertiary (the cast)
- **Soft Plastic White** (plastic-white, prop-white): robot bodies, pod pedestals, gate pillars and the Prod platform, as clearcoated physical material (roughness 0.35 to 0.4).
- **Night Visor** (visor): each robot's glossy face plate, behind glowing round eyes.
- **OpsPilot Amber** (team-opspilot), **VaultGuard Teal** (team-vaultguard), **Gatekeeper Cobalt** (team-gatekeeper): team accents on robot ear pods, belly plate and boots, and on the small team marks in the HUD (the agent square in the top bar, the row mark in the inbox).
- **Culprit Red** (token-culprit) and **Fixed Green** (token-fixed): the octahedral culprit token, deeper than the HUD red and green so the emissive glow stays saturated in the lit scene. It is red while held, green once it leaves the hand.

### Neutral
- **Twilight Navy** (scene): page and canvas background, fog colour, theme colour.
- **Raised Navy** (scene-raised): the studio floor disc.
- **Frost Panel** (panel) and **Solid Panel** (panel-solid): HUD panels over the scene; the drawer.
- **Hairline** (panel-line) and **Strong Hairline** (panel-line-strong): panel edges, dividers, button outlines, timeline connectors.
- **Well** (well): recessed fields under text input and code (inputs, the Flagsmith call block).
- **Ink** (ink), **Ink 2** (ink-2), **Ink 3** (ink-3): primary text, secondary text, muted metadata (Ink 3 holds 6.4:1 on the panel).
- **Sealed Slate** (sealed-slate): the gate ring and dot when no change is pending.
- **Zone Blue** (zone-ring): the faint ring marking the sandbox zone on the floor.
- **Key Light** (key-light) and **Rim Light** (rim-light): the studio's cool key, hemisphere and rim light, plus the ambient dust sparkles; warm (#ffd8a8) and cyan (#a5f3fc) lightformers complete the reflections.

### Named Rules
**The Colour Has a Job Rule.** Amber needs you, sky is running, green is verified, red failed. A status colour never decorates, and the same job keeps the same colour in the HUD and on the stage.

**The Team Colour Stays on the Cast Rule.** Team colours appear on robot plastic and on the small square team marks that name an agent. They never colour text, buttons, status or panels.

**The No Violet Rule.** The palette has no violet or purple at any step.

## Typography

**Display Font:** Geist Sans (with ui-sans-serif, system-ui)
**Label/Mono Font:** Geist Mono (with ui-monospace, SFMono-Regular, Menlo)

**Character:** One neutral grotesque for every human sentence, its mono sibling for every machine value. Tight negative tracking on headings, plain sentence case everywhere, weights 400 to 600 only.

### Hierarchy
- **Display** (600, 40px, 1.08, -0.035em; 30px under 900px): the inbox hangar headline only.
- **Headline** (600, 24px, 1.2, -0.025em): the story panel's one-sentence verdict; an inline culprit flag sits in it as a red code chip at 0.8em.
- **Title** (600, 16px; drawer heading 17px; section heads 14px to 15px): panel and drawer headings.
- **Body** (400, 15px, 1.55): base text; 13.5px for story facts, step cards and evidence lines; 14px for the fix sentence and buttons. Hangar body copy caps at 46ch.
- **Label** (500, 12.5px): status pills, meta lines, form labels, scene tags; table headers at 11.5px in Ink 3.
- **Mono** (500, 13px, tabular numerals): flags, diff lines, ids, run counts, timestamps (11px to 12px), the Flagsmith call (12px, 1.6).

### Named Rules
**The Mono Is for Machines Rule.** Anything a system produced (a flag key, a value, an id, a time, a call, a count) is set in Geist Mono with tabular numerals; anything a person reads as a sentence is Geist Sans.

**The Sentence Case Rule.** No uppercase labels and no tracked-out small caps; hierarchy comes from size, weight and ink level.

## Layout

The investigation page is a fixed full-viewport stage with HUD panels pinned to its corners at a 16px inset: the top bar spans the width (back link, agent mark and id, status pill, demo-data chip, run budget meter, Evidence button); the story panel sits bottom-left (min(440px, 100vw - 32px)); the approval panel bottom-right (min(400px, 100vw - 32px)) holds the primary action; a 300px step card appears top-left under the bar when a pod is selected. The scene camera keeps the whole cast in frame and pulls back as the aspect ratio narrows.

The inbox is a hangar: a 3D stage band (min(64vh, 620px), at least 440px) with the brand bar on top and the headline bottom-left, then a centred list (max 1100px, padding 28px 32px 64px) of investigation rows on an 8px gap.

Under 900px the investigation stops being fixed: the stage becomes a 56vh band at the top and the story and approval panels stack below it in flow with 12px margins; run budget, hint and id hide. Under 640px the top bar wraps. Spacing steps in 6, 8, 12, 16, 28 and 32px; panel padding runs 14px to 20px. Hover styles apply only on hovering, fine-pointer devices; every control is at least 44px tall (36px for compact HUD controls).

## Elevation & Depth

Depth is real: the 3D scene supplies it with key, rim and hemisphere light, ACES tone mapping, contact shadows, fog that fades the floor into the navy, and emissive glow on status objects. The HUD reads as frosted glass held above that scene: translucent panel, 14px backdrop blur with 140% saturation, a hairline edge, a faint inner top highlight and one deep soft shadow.

### Shadow Vocabulary
- **HUD lift** (`box-shadow: 0 1px 0 rgba(255,255,255,0.04) inset, 0 18px 50px rgba(2,8,20,0.45)`): every floating HUD panel.
- **Waiting glow** (`border-color: rgba(251,191,36,0.45); box-shadow: 0 0 0 1px rgba(251,191,36,0.18), 0 18px 60px rgba(251,191,36,0.08), HUD lift`): the approval panel while it waits for a human.
- **Drawer cast** (`box-shadow: -24px 0 60px rgba(2,8,20,0.5)`): the evidence drawer over a 50% navy scrim.
- **Status glow** (`box-shadow: 0 0 8px currentColor`): status dots in pills and scene tags; 6px on done timeline nodes and the culprit matrix dot; 10px to 18px on team marks.
- **Focus ring** (`box-shadow: 0 0 0 3px rgba(125,211,252,0.22)` with a sky border on inputs; `outline: 2px solid sky, offset 2px` elsewhere).

### Named Rules
**The Glow Means Status Rule.** Light is emitted only by things that carry state: status dots, pod rings and glass, mood lights on the robots, the token, the gate field. Nothing glows for decoration.

**The Frost Over, Solid Across Rule.** A panel floating over the scene is frosted; a surface that covers the scene entirely (the drawer) is solid.

## Shapes

Sharp-ish and consistent: 8px corners on panels, rows and the top bar; 6px on buttons, inputs, diff cards, tables, notices and scene tags; 4px on inline code and the focus outline; full pills only for status and the demo-data chip; small rounded squares (3px to 9px) for team and brand marks. Borders are 1px hairlines, never thick. In the scene, everything is rounded and soft: rounded boxes, capsule glass pods, cylinder pedestals, a disc floor, thin floor rings, and the only sharp form on stage is the octahedral token, because it is the thing being judged.

## Components

### Buttons
Tactile and quiet: outlined glass by default, one solid green action.
- **Shape:** gently squared (6px), 44px tall, 16px side padding, 14px weight 500.
- **Default:** faint frost fill with a strong hairline; hover lifts fill and border; press scales to 0.97 over 160ms on the strong ease-out.
- **Primary (Approve):** solid green with Deep Forest text, weight 600; hover brightens to the lighter green.
- **Reject:** transparent with red text and a 35% red outline; hover fills with the red tint.
- **Ghost:** 32px, transparent, Ink 3 at 13px, for copy and minor actions.
- **Busy:** the label blurs 1.5px and fades to 65% while the request runs. Disabled drops to 50%.

### Status pill
- **Style:** 24px full pill, label type, tinted background in the status colour at about 13% with text in the full colour, and a 6px glowing dot.
- **State:** live statuses breathe the dot (1.8s); the demo-data chip is the outlined variant: a dashed amber border with no fill.

### HUD panels
- **Corner Style:** 8px.
- **Background:** frost panel with 14px backdrop blur and 140% saturation.
- **Shadow Strategy:** HUD lift (see Elevation); the approval panel adds the waiting glow while proposed.
- **Border:** one hairline.
- **Internal Padding:** 14px to 20px; the top bar 8px.

### Notice
One plain status sentence with a leading stroke icon, 6px corners, 9px 11px padding, Ink 2 text on the status tint; the icon takes the full status colour. No side stripe.

### Diff card
The exact Flagsmith change: a hairline card on a recessed navy well with a meta strip (flag key in mono) and mono lines at 13px / 1.8 with a centred +/- gutter; deletions on red tint in del-ink, insertions on green tint in ins-ink.

### Inputs
- **Style:** 44px, recessed well, strong hairline, 6px corners, 12px padding.
- **Focus:** border turns sky with a 3px soft sky ring.

### Navigation
The top HUD bar carries the back link (Ink 2, background on hover) and the Evidence button; the inbox rows are full-width 8px cards with a glowing team mark, title, sub line, status pill and mono time, lifting 2px on hover and pressing to 0.99.

### Evidence drawer
Solid panel from the right (min(760px, 100vw)), sliding on the iOS drawer curve in 340ms and out in 260ms over a navy scrim; holds the search matrix (sky dots, a glowing red culprit dot), repairs table (chosen row green-tinted), the Flagsmith call with copy, read-back, gate activity log and stats.

### Scene tags (label layer)
DOM labels projected onto 3D anchors each frame: 6px corners, 82% navy with 6px blur, a hairline, 12.5px weight 500, an optional glowing status dot; zone tags are lighter (60%), the token tag sets its flag in mono. They appear instantly at their anchor (no fade) and hide when the anchor leaves view.

### The cast (signature)
- **Robot:** rounded white plastic body, glossy night visor with round glowing eyes, team-coloured ear pods, belly plate and boots, and an antenna light in the mood colour (sky idle and searching, amber alert, green happy, red sad). Idles with a gentle bob, scans when searching, raises the token when alert, droops when sad, waves when happy, blinks every few seconds.
- **Sandbox pod:** a white pedestal with an emissive outcome ring and a transmissive glass capsule tinted by outcome, a small robot inside; hover or select lifts it 0.18 units and shows its step tag.
- **Gate:** two white pillars and sliding doors with an additive energy field (bright rim, fine scanlines, one sweeping band) in the gate colour: amber locked, sky opening, green open, red breached, slate sealed. Opening slides the doors apart and dissolves the field.
- **Token flight:** on approval the token turns green, follows a curve through the gate to Prod, and green sparkles burst there as the Prod robot celebrates.

## Do's and Don'ts

### Do:
- **Do** keep amber, sky, green and red on their jobs (needs you, running, verified, failed) in both the HUD and the scene.
- **Do** float HUD panels over the scene as frost (panel fill, blur(14px) saturate(140%), hairline, HUD lift) at a 16px inset.
- **Do** set flags, values, ids, times and calls in Geist Mono with tabular numerals.
- **Do** use the strong ease-out (cubic-bezier(0.23, 1, 0.32, 1)) for entrances and press feedback, the drawer curve (cubic-bezier(0.32, 0.72, 0, 1)) for the drawer, and play entrances once on load.
- **Do** build new characters and props from primitives in clearcoated soft plastic, with team colour only on accents.
- **Do** give every scene state a still, readable pose under reduced motion.
- **Do** label demo numbers with the dashed amber demo-data chip.

### Don't:
- **Don't** use violet or purple at any step.
- **Don't** colour text, buttons or panels with a team colour.
- **Don't** add glow to anything that does not carry state.
- **Don't** use side-stripe accents on cards or callouts; tint the background instead.
- **Don't** add eyebrow or kicker labels above headings, or uppercase tracked labels.
- **Don't** apply hover styles on touch devices; gate them behind (hover: hover) and (pointer: fine).
- **Don't** load downloaded 3D models, textures or icon packages; icons are inline 24px stroke SVGs.
