# UX Benchmarks — the critic's yardstick for mail-triage

Two parts, both sourced from first-party pages fetched 2026-10-01:
**Part 1** — how leading commercial email products (Gmail, Superhuman, Spark,
Shortwave, HEY, Apple Mail, Fastmail, Outlook) handle the patterns this app uses.
**Part 2** — cross-app craft and industry standards (dashboard stats, rule builders,
log viewers, forms, settings, mobile chrome & motion, WCAG 2.2, craft sources).

Critics: read this file IN FULL and cite sections by name/number in your verdicts.

---

## Part 1 — Email product benchmarks

**Purpose.** Critic's yardstick for `mail-triage` (phone-first PWA: dashboard, list with All / Awaiting LLM / Needs reply / Moved / Tagged filters, viewer, assistant chat, settings). **Method:** product claims come from first-party pages fetched 2026-10-01 (links inline); vendor claims marked; gaps called out, never filled from memory.

### 1. Message list anatomy & density

- **Gmail** — threads group by default; each swipe direction is configurable (archive, delete, mark read/unread, move to label, snooze, **None**); iOS defaults swipe to archive. [Android](https://support.google.com/mail/answer/6562?hl=en&co=GENIE.Platform%3DAndroid) · [iOS](https://support.google.com/mail/answer/6562?hl=en&co=GENIE.Platform%3DiOS)
- **Superhuman** — the list is partitioned into **Split Inboxes** ("separate important emails from newsletters" — vendor); offline cache: "up to 1250 emails from each Split". [Offline](https://help.superhuman.com/hc/en-us/articles/46005499629325-Offline-Access) · [Blog](https://blog.superhuman.com/ai-powered-email/)
- **Spark** — two list models: **Classic** and **Smart Inbox**; Smart sorts accounts into Personal / Notifications / Newsletters, new mail on top, read mail dropping into a **Seen** section at the bottom. [Personalization](https://support.readdle.com/spark/personalization)
- **Shortwave** — **Splits** (tabs for important emails, senders, labels, custom queries); **Bundles** collapse noisy senders. [shortwave.com](https://www.shortwave.com/)
- **HEY** — the **Imbox** holds only "important, immediate emails … from people or services you care about"; a dominating sender collapses to one row; transactional mail goes to the Paper Trail. [Features](https://www.hey.com/features/)

**Why it works.** The list is a triage surface: grouping cuts rows, splits give one queue per context, read items recede, bundling stops one sender from dominating.

**THE BAR:** row = sender + subject + snippet + time + unmistakable unread affordance; threads grouped by default; ≥2 scoping axes (category/sender/label/query) beyond "All"; read items recede; high-volume senders collapse to one row.

### 2. Triage mechanics

- **Gmail (mobile)** — optional confirmations before delete/archive/send; **auto-advance** after handling (older / newer / list); notification default action delete-or-archive. [Android](https://support.google.com/mail/answer/6562?hl=en&co=GENIE.Platform%3DAndroid)
- **Superhuman** — **Cmd/Ctrl+K = "Superhuman Command — your master control for any action"**, each action's shortcut listed beside it; learn by hover. Vendor: Auto Reminders resurface unanswered mail after 1–7 days. [Shortcuts](https://help.superhuman.com/hc/en-us/articles/46005701270541-Keyboard-Shortcuts-in-Superhuman-Mail) · [Blog](https://blog.superhuman.com/ai-powered-email/)
- **Spark** — one swipe to mark read, **pin**, archive, delete, **snooze** (Settings > General; "Swipe preferences don't sync across all your devices"); Gatekeeper screens/blocks senders; Mute auto-archives dead threads; Done Marker, Set Aside, Send Later, Reminders. [Personalization](https://support.readdle.com/spark/personalization) · [sparkmailapp.com](https://sparkmailapp.com/)
- **Shortwave** — snooze with AI-suggested times; plain-English **AI filters** label, star, archive automatically. [shortwave.com](https://www.shortwave.com/)
- **HEY** — **Reply Later** pile ("at the bottom of the screen"), **Set Aside**, **Bubble Up** (returns to top later), **Focus & Reply** (shows *only* Reply Later mail; reply "one after another"). [Features](https://www.hey.com/features/) · [Reply mode](https://www.hey.com/features/reply-mode/)
- **Fastmail** — single-key model: `j`/`k` focus, `x` / `Shift+x` select / range, `d` trash, `y` archive, `[`/`]` archive-and-next/previous, snooze presets `b`+1–7, `z` **undo up to the last 10 actions**. [Shortcuts](https://www.fastmail.help/hc/en-us/articles/360058753534-Keyboard-shortcuts)

**Why it works.** Undo turns mistakes into noise; presets beat menus; queues separate *deciding* from *doing*; a palette keeps actions discoverable.

**THE BAR:** per-direction swipe config incl. snooze + None; ≥5 single-key actions; range bulk select; global undo (toast or ~10-action stack); snooze presets (today/tomorrow/weekend/next week/custom); a linear "needs reply" queue; destructive actions confirmed or undoable.

### 3. Reading pane

- **Gmail** — auto-advance defines post-handling flow; Gemini adds an **Add to calendar** button above the email when an event is detected (confirm if multiple); smart replies are **hover-previewable in full** before selection. [Gemini](https://support.google.com/mail/answer/14355636?hl=en) · [AI page](https://workspace.google.com/products/gmail/ai/)
- **HEY** — batch reading: Focus & Reply removes each item as you reply ("knock out 5 of 12, or all 12, or none"); **sticky notes** on any email; **Reply to Everyone** sends one reply to many mails. [Reply mode](https://www.hey.com/features/reply-mode/) · [Features](https://www.hey.com/features/)
- **Fastmail** — the pane carries its own vocabulary: `j`/`k` next/prev, `r`/`a`/`f` reply / reply-all / forward, `[`/`]` archive-and-move-on; `n`/`p` focus a message so reply keys target *it*. [Shortcuts](https://www.fastmail.help/hc/en-us/articles/360058753534-Keyboard-shortcuts)
- **Shortwave** — assistant works on "any email on your screen" (dates, action items, summaries); inline **Improve draft** button; Tab-accepted autocomplete. [AI docs](https://www.shortwave.com/docs/guides/ai-assistant/)

**Why it works.** Reading must carry its own actions so triage never bounces to the list; batch reply exploits the mode switch; AI output is previewable before it goes out.

**THE BAR:** next/prev + archive-and-advance + reply/reply-all/forward; a queue mode that clears several replies in place; AI replies land as editable previews; primary mobile actions one-handed *(bar placement is undocumented in fetched sources — judge from screenshots).*

### 4. Compose / draft / send

- **Gmail** — "Message sent" + **Undo**/View; cancellation period 5/10/20/**30s**. [Send/unsend](https://support.google.com/mail/answer/2819488?hl=en&co=GENIE.Platform%3DDesktop)
- **Fastmail** — **20s undo held on Fastmail's servers**; the FAQ states the principle: a client-side delay means "you can think it's been sent when it hasn't"; a server hold sends even if you close the laptop. [Undo send](https://www.fastmail.com/features/undo-send/)
- **Apple Mail** — 10s to undo ("Undo Send" at the top of the inbox); configurable **Undo Send Delay**. [Apple](https://support.apple.com/guide/iphone/unsend-email-with-undo-send-iph0e7288015/ios)
- **Spark** — default **5s**, customizable Undo Send Timer. [Spark help](https://sparkmailapp.com/help/sending-emails/customize-undo-send-timer)
- **Superhuman** — scheduled mail sends at its scheduled time; mail sent offline goes out on reconnection; Write with AI drafts from short prompts in your voice (vendor). [Offline](https://help.superhuman.com/hc/en-us/articles/46005499629325-Offline-Access) · [Blog](https://blog.superhuman.com/ai-powered-email/)
- **Shortwave** — one-click contextual drafts via Ghostwriter (learns from sent mail), refined by follow-up instructions. [AI docs](https://www.shortwave.com/docs/guides/ai-assistant/)

**Why it works.** Send is the one irreversible action: buy back seconds, keep the state honest (a visible, undoable "sent" moment); server hold beats client delay.

**THE BAR:** undo window default-on and configurable (≥10s); server-side hold if the architecture allows; explicit sent/undo affordance right after send; drafts autosave visibly; AI drafting ends in an editable draft, never an auto-send.

### 5. AI assistant surfacing

- **Gmail Gemini** — **sidebar assistant** + inline entries: Summarize (thread → bullets), Draft/refine, Organize (delete/label), Search, Schedule; side-panel Q&A over the thread. Agentic actions are gated: mail is "never deleted, archived, labeled, or marked read or unread until you explicitly confirm", with **60s undo** after confirming; guardrails: labels must exist, no spam-marking, 10k-thread cap. "Help me write" drafts from a prompt. [Gemini help](https://support.google.com/mail/answer/14355636?hl=en) · [AI page](https://workspace.google.com/products/gmail/ai/)
- **Superhuman** — vendor claims: Write with AI matches recipient tone; AI Categorization learns priorities; Split Inbox; **Read Statuses** ("know exactly when someone opens your email"). [Blog](https://blog.superhuman.com/gmail-ai-assistant/) · [Blog](https://blog.superhuman.com/ai-powered-email/)
- **Shortwave** — conversational assistant: "organize my inbox" returns **recommended actions on groups of threads** (archive low-priority, label, delete junk, create todos), user "fully in control at each step"; saved one-click prompts; **AI memories** (behavior rules); natural-language AI filters. [AI docs](https://www.shortwave.com/docs/guides/ai-assistant/)
- **Spark / HEY / Apple / Fastmail** — no AI claims (no fetched source).

**Why it works.** Two surfaces for two modes — inline for the open message, chat for the mailbox; suggestions are reviewed; mutations need confirm + undo; learned voice/memories drive acceptance.

**THE BAR:** (a) inline summarize + draft on the open message; (b) chat that can search/act across the mailbox with visible tool actions; (c) every mutation gated by confirm + undo (60s floor); (d) never silently modify mail — log actions, keep undo reachable.

### 6. Sync / status / freshness + offline

- **Superhuman** — offline: "Triage your inbox, reply to emails, and work just as you normally would"; work "synchronizes" on return; cache scope stated (last 30 days of mail; ≤1250 per Split; attachments); offline-composed mail sends on reconnection. [Offline](https://help.superhuman.com/hc/en-us/articles/46005499629325-Offline-Access)
- **Gmail** — offline read/write/search/delete/label; on reconnect Gmail "automatically updates and sends messages in your outbox and downloads any new messages"; local window 7/30/90 days (default 30), attachments optional. [Offline](https://knowledge.workspace.google.com/admin/gmail/use-gmail-offline-with-google-workspace)
- **Gmail mobile** — per-account sync frequency; "Never" = manual pull-to-refresh. [Android](https://support.google.com/mail/answer/6562?hl=en&co=GENIE.Platform%3DAndroid)
- **Fastmail** — `u` = explicit refresh; server-side undo hold keeps shown state truthful. [Shortcuts](https://www.fastmail.help/hc/en-us/articles/360058753534-Keyboard-shortcuts) · [Undo](https://www.fastmail.com/features/undo-send/)
- **Spark** — cautionary: swipe preferences "don't sync across all your devices" (device-local state, at least documented). [Personalization](https://support.readdle.com/spark/personalization)

**Why it works.** A triage tool with invisible freshness can't be trusted; offline needs a contract (what's cached, how long) and an outbox that marks unsent items; reconnect sync must be deterministic.

**THE BAR:** visible freshness state (synced / syncing / offline) + manual refresh; offline states what is available; composed mail visibly **pending** until it actually leaves; no "sent" before the server has it; device-local vs synced behavior stated in the UI.

### 7. Settings organization

- **Gmail** — *General* holds list behaviors (swipe, sender image, conversation view, auto-advance, confirmations); per-account sync/server settings; undo send under full settings on desktop. [Android](https://support.google.com/mail/answer/6562?hl=en&co=GENIE.Platform%3DAndroid) · [Send](https://support.google.com/mail/answer/2819488?hl=en&co=GENIE.Platform%3DDesktop)
- **Superhuman** — Cmd+K → Shortcuts shows every shortcut. [Shortcuts](https://help.superhuman.com/hc/en-us/articles/46005701270541-Keyboard-Shortcuts-in-Superhuman-Mail)
- **Spark** — Settings > General carries swipe actions and the Undo Send Timer. [Personalization](https://support.readdle.com/spark/personalization) · [Timer](https://sparkmailapp.com/help/sending-emails/customize-undo-send-timer)
- **Fastmail** — shortcuts toggle under Settings → Display options → Accessibility. [Shortcuts](https://www.fastmail.help/hc/en-us/articles/360058753534-Keyboard-shortcuts)
- **Shortwave** — assistant settings hold saved prompts and AI memories. [AI docs](https://www.shortwave.com/docs/guides/ai-assistant/)
- **Apple Mail** — Undo Send Delay under Settings > Apps > Mail. [Apple](https://support.apple.com/guide/iphone/unsend-email-with-undo-send-iph0e7288015/ios)
- **HEY** — no settings page fetched; omitted from this section.

**Why it works.** Grouping by the task it changes, reachable from the surface it affects; the palette as universal entry; sync scope explicit.

**THE BAR:** task-named sections; each preference ≤2 hops from its surface where possible; per-device vs synced scope stated; a shortcut reference; every setting shows its current value.

### 8. Empty states / onboarding

- **Superhuman** — progressive onboarding: start with most-used actions, discover the rest via palette and hover tooltips. [Shortcuts](https://help.superhuman.com/hc/en-us/articles/46005701270541-Keyboard-Shortcuts-in-Superhuman-Mail)
- **HEY** — the first-run contract *is* the product: Imbox for important, immediate mail only; receipts → Paper Trail; newsletters/offers out of scope by design. [Features](https://www.hey.com/features/)
- **Spark** — the inbox model is an explicit choice (Classic vs Smart, stated sorting rules); Gatekeeper "screens" senders before they arrive. [Personalization](https://support.readdle.com/spark/personalization) · [sparkmailapp.com](https://sparkmailapp.com/)
- **Gmail** — trust defaults: optional confirmation before destructive actions; offline is a per-user opt-in with a data-handling choice (keep vs remove local data). [Android](https://support.google.com/mail/answer/6562?hl=en&co=GENIE.Platform%3DAndroid) · [Offline](https://knowledge.workspace.google.com/admin/gmail/use-gmail-offline-with-google-workspace)

**Why it works.** Empty states and first-run choices teach the mental model; a few consequential upfront choices prevent later cleanup; progressive disclosure avoids overwhelming.

**THE BAR:** every empty list (no messages, filter empty, LLM queue not run, offline) = title + reason + one action; first-run asks ≤2 consequential, reversible questions; agentic/destructive permissions opt-in and visible; "why is this here / where did it go" is one tap away.

---

*Never assert (no fetched source): time-format rules, row heights; Apple Mail / Fastmail list & triage mechanics; HEY settings; Spark/HEY/Fastmail/Apple AI; onboarding screens. Superhuman's 4h/week and 2–3× figures are vendor marketing, not measurements.*

---

## Part 2 — Cross-app craft & industry standards

Yardstick for critic passes over this app's screenshots: phone-first PWA, "square, black-and-white, Vercel-like, Geist, light theme"; surfaces = dashboard (hero stat, system status, recent mail), rules/classifiers/flows, log viewer, settings, account setup, and a 4-destination bottom tab bar (Dashboard / Messages / Assistant / More) with direction-aware transitions. Every factual claim cites a source fetched for this doc ([n] → Sources); **THE BAR** lines are pass/fail review targets.

### 1. Dashboard & stat surfaces

**What a dashboard is for.** A dashboard gives an at-a-glance snapshot so users don't have to check "10,000 screens"; design it around the questions users need answered, prioritizing warnings and actionable items; loading and empty states are commonly forgotten.[23]

**When cards work.** A card = content and actions about a single topic, easy to scan, clear hierarchy, entry point into detail; cards in a collection share resting elevation; filter/sort controls sit outside it.[26] A stat card earns its box only if it answers one question and leads somewhere (drill-down, filtered list); card-on-card nesting dilutes the scan.[26]

**Anti-patterns:** KPI tiles answering no user question [23]; status by color alone (3:1 non-text contrast plus a text equivalent is required) [3]; omitted loading/empty states [23]; untimestamped "recent" data.[16]

**Status surfaces.** Vercel's convention in its own product: 4xx = amber warning, 5xx = red error.[16] A system status card states the state and the next action, not just a color.

**B/W still has a contrast contract.** Geist is "a high contrast, accessible color system", with typefaces designed for developers and designers and the grid central to the Vercel aesthetic[22] — so the B/W look must clear WCAG text (4.5:1; 3:1 large) and non-text (3:1) contrast.[3]

**THE BAR:** The hero stat answers one question and links to the filtered list it summarizes; the status card names state + action; cards only for single-topic groupings, no card-in-card; timestamps on "recent" data; B/W clears 4.5:1 / 3:1 contrast.[23][26][22][3][16]

### 2. Automation / rule builders

**Condition → action, ordered.** Gmail builds filters from search criteria, lets you test the search before committing, and exports them as XML.[11] Outlook requires every rule to have a name, a condition, and an action (multiple allowed); rules apply in list order, reorder with up/down arrows, can run on existing messages, and toggle off without deletion.[13] Fastmail matches sender, subject, or anything else you search by, and any search result can become a rule.[12]

**Dry-run affordances.** Gmail: verify matches with Search first.[11] Outlook: "Run this new rule now on messages already in the current folder".[13] Zapier: each step has a Test tab (Data in / Data out, Skip test), testing is explicitly live ("may result in changes made in your app"), and Zap History logs every run.[15]

**Reorder UX.** Zapier: drag steps in the editor sidebar (hover → drag handle); placing a step after one whose data it maps raises a fix-your-mappings alert; triggers/path conditions can't be moved.[14]

**THE BAR:** The rules/classifiers/flows editor shows ordered, editable conditions and actions; every rule toggles off non-destructively; a dry-run (preview matches / run-on-existing) exists before commit; reordering is supported and guarded against broken references; the empty state teaches the search → rule path.[11][13][12][14][15]

### 3. Log / console viewers

**Filters are the product.** Vercel's runtime-log UI filters from a sidebar: timeline (hour / 3 days / custom), level (Warning/Error/Fatal), and route, resource, status, host, deployment fields, plus a search bar; rows are newest-first, times in UTC, and "Live mode" follows logs in real time.[16] Severity is encoded as 4xx amber warnings / 5xx red errors.[16]

**Readability.** Machine data gets the monospace face — Geist includes a Mono typeface designed for developers[22] — but log text is still text: 4.5:1 contrast, 3:1 for UI graphics/states, and color-coded severity needs a text equivalent; real-time auto-follow is auto-updating content under WCAG 2.2 and needs a pause/stop mechanism.[3]

**THE BAR:** Level filter + time window + search + live-tail toggle; newest-first, UTC; monospace IDs with copy affordances; severity in color and text (4xx-amber / 5xx-red); pauseable auto-follow; black panel passes contrast; readable at 320px.[16][22][3]

### 4. Forms & validation

**Labels and structure.** Don't get clever with labels — placeholders and floating labels both hurt; plain visible labels above inputs; inputs ≥44px tall; width hints expected content length; avoid multi-column layouts; sentence case; mark optional fields, not required ones; use browser autofill; disable autocorrect/autocapitalize on machine data; one question per page.[18]

**Errors: summary + inline.** GOV.UK: always show an error summary — even for one error — headed "There is a problem", linking each errored answer, keyboard focus moved to it, wording identical to inline messages.[9] Inline: red message after the question/hint, red border connecting message to field, hidden "Error:" prefix for screen readers; never clear fields on error.[10]

**When to validate.** Baymard: live inline validation (checked as the user completes a field) measurably improves error recovery, yet ~31% of sites have none; avoid premature validation; remove the message once corrected; add positive validation.[17] Adam Silver advises validating on submit, not as the user types.[18] Safe union: validate on blur and submit, never mid-typing.[17][18]

**THE BAR:** Visible label above every input, ≥44px tall, width signaling content; validation fires after the user finishes, never mid-entry; errors above the field in red + icon + text, never color alone; on submit a focused error summary links each error to its field with matching wording; failing answers are never cleared; optional fields are marked.[18][17][9][10]

### 5. Settings structure

HIG: put general, infrequently changed options in the app's own settings area — people must suspend their task to open Settings, so it shouldn't hold anything they need constantly; don't duplicate system/global options (it implies global settings may not apply); only the rarest options belong at system level; a few essential options can live at the bottom of the main view or under a More menu.[7] Applied here: credentials/servers, classifier thresholds, retention, notification channels = Settings; triage actions (archive, reply, run rule) = main flow; "More" is the right home for rarely used destinations.[7]

**THE BAR:** Settings holds only infrequent, general configuration; core triage actions stay in the main flow; no duplicate global/system controls; More carries long-tail destinations; saves/sync emit announced status messages.[7][3]

### 6. Mobile chrome: tab bar, safe areas, gestures, motion

**Bottom tab bar (M3).** Three to five destinations, at the bottom, each an icon plus a 1–2-word label; filled icon + active indicator mark the current destination; badges carry dynamic counts; icons hold ≥3:1 contrast; **don't swipe between destinations** — swipe is for related items and row actions like archiving; a tapped destination uses a top-level transition; pick a preserve-vs-reset state policy, scrolling to top on re-tap; don't hide the bar on scroll while a screen reader is active; nav bars are for compact/medium widths — wider gets rail/inline navigation.[1]

**Tab bar (HIG).** Navigates top-level sections, preserving each section's state; for navigation, not actions; badges for critical information; customizable tabs default to five or fewer.[4]

**Standalone PWA.** Standalone runs in an OS-standard window without browser navigation UI; on mobile the status bar stays visible; `theme-color` paints the status bar, `background_color` covers pre-CSS paint, and the HTML title is part of app UX.[20] Respect safe areas — regions not covered by hardware/system chrome — so the Dynamic Island/home indicator never obstruct content[5]; on the web that's `env(safe-area-inset-*)`, the safe distance from each edge on non-rectangular displays.[21]

**Gestures.** Edge-swipe back is an accelerator, not a replacement — keep a visible Back control; don't assign familiar gestures to unique actions or collide with system ones; gestures need immediate, predictive feedback.[8] Swipe-back within a stack is fine; swipe must not switch destinations.[1]

**Motion bar.** M3 tokens: standard easing `cubic-bezier(0.2, 0, 0, 1)`; emphasized decelerate `cubic-bezier(0.05, 0.7, 0.1, 1.0)`; durations — 50–200ms utility, 250–400ms medium-area, 450–600ms large.[2] Apple: make motion optional — never the only carrier of meaning; supplement with haptics/audio; feedback motion follows the gesture.[6] Emil: ease-out under ~300ms; transform/opacity only, off the main thread (CSS/WAAPI), interruptible; never animate keyboard-initiated or high-frequency actions.[19] Rauno: respond immediately and interruptibly, matching trigger timing to the weight of the action — lightweight overlays may fire mid-swipe; destructive actions wait for release.[24]

**THE BAR:** Four labeled destinations, ≥3:1 icons, no cross-destination swipe; direction-aware push/pop transitions ~250–300ms with emphasized easing, interruptible, off under reduced-motion; safe-area padding in standalone; a visible Back control alongside edge-swipe.[1][4][20][5][21][2][6][19][24]

### 7. Accessibility bar — WCAG 2.2 AA checklist

Pass/fail list for screenshots and DOM (AA unless noted):
- **Target size:** pointer targets ≥24×24 CSS px — 2.5.8 AA (spacing/exceptions apply); 24px WCAG floor, 44px practical touch bar for inputs.[3][18]
- **Focus:** visible focus mode (2.4.7 AA); not entirely hidden by sticky bars/overlays (2.4.11 AA); indicator ≥2px perimeter at ≥3:1 (2.4.13 AAA).[3]
- **Contrast:** text 4.5:1, large text 3:1 — 1.4.3 AA; UI components/meaningful graphics 3:1 — 1.4.11 AA.[3]
- **Reflow:** usable at 320 CSS px width without 2-D scrolling — 1.4.10 AA.[3]
- **Dragging:** swipe functions need a single-pointer alternative — 2.5.7 AA; swipe-to-archive needs a button path.[3]
- **Status messages:** save/sync messages are programmatically determinable without focus — 4.1.3 AA.[3]
- **Auto-updating content:** live logs/feeds can be paused/stopped/hidden — 2.2.2 A.[3]
- **Motion:** interaction-triggered motion can be disabled — 2.3.3 AAA; support `prefers-reduced-motion`.[3][6]
- **Error focus:** keyboard focus moves to the error summary on submit[9]; tab-bar icons ≥3:1 in all states.[1]

**THE BAR:** No AA failures: 24px minimum targets (44px for inputs), visible unobscured focus, 4.5:1/3:1 contrast, 320px reflow, no swipe-only actions, announced status messages, pauseable live feeds, reduced-motion support.[3][18][9][1][6]

### 8. The craft bar (rauno, emil)

Rauno's thesis: interaction quality comes from "hundreds of design decisions obsessing over the tiniest margins so that when they work, no one has to think about them"; reuse learned metaphors (each gesture teaches the next), model real-world physics (momentum, interruptibility), respond immediately, and match trigger timing to action weight.[24] His Craft index is the method: one interaction at a time, until the invisible details are deliberate.[25]

Emil's discipline: people choose tools by overall experience; keep animations natural yet snappy (ease-out, usually under 300ms); fast motion improves perceived performance; animate transform/opacity only and keep 60fps (offload to CSS/WAAPI when the main thread is busy); keep animations interruptible; never animate keyboard-initiated actions; pace animations instead of spraying them; let motion-sensitive people turn them off.[19]

At review time: states (hover/pressed/focus/disabled/loading/empty); tab-bar indicator and badges; monospace alignment; filter-chip states; severity colors with text equivalents.[19][24][16][3]

**THE BAR:** Every micro-detail is deliberate (states, timings, thresholds); motion explains and can be interrupted mid-flight; frequent/keyboard actions are never animated; a default the design system would not ship is a finding.[24][19][25]

### Sources

1. M3 — Navigation bar guidelines: https://m3.material.io/components/navigation-bar/guidelines
2. M3 — Easing and duration tokens: https://m3.material.io/styles/motion/easing-and-duration/tokens-specs
3. W3C — WCAG 2.2: https://www.w3.org/TR/WCAG22/
4. Apple HIG — Tab bars: https://developer.apple.com/design/human-interface-guidelines/tab-bars
5. Apple HIG — Layout: https://developer.apple.com/design/human-interface-guidelines/layout
6. Apple HIG — Motion: https://developer.apple.com/design/human-interface-guidelines/motion
7. Apple HIG — Settings: https://developer.apple.com/design/human-interface-guidelines/settings
8. Apple HIG — Gestures: https://developer.apple.com/design/human-interface-guidelines/gestures
9. GOV.UK Design System — Error summary: https://design-system.service.gov.uk/components/error-summary/
10. GOV.UK Design System — Error message: https://design-system.service.gov.uk/components/error-message/
11. Gmail Help — Create rules to filter your emails: https://support.google.com/mail/answer/6579
12. Fastmail — Rules Help You Spend Less Time on Email: https://www.fastmail.com/blog/fastmail-rules/
13. Microsoft Support — Rules in Outlook: https://support.microsoft.com/en-us/outlook/mail/manage-email-messages-by-using-rules-in-outlook
14. Zapier — Reorder or duplicate action steps and paths: https://help.zapier.com/hc/en-us/articles/9528974130957-Reorder-or-duplicate-action-steps-and-paths
15. Zapier — Set up your Zap action: https://help.zapier.com/hc/en-us/articles/8496257774221-Set-up-your-Zap-action
16. Vercel — Runtime Logs: https://vercel.com/docs/logs/runtime
17. Baymard — Inline Form Validation: https://baymard.com/research-articles/inline-form-validation
18. Adam Silver — Form design: from zero to hero: https://adamsilver.io/blog/form-design-from-zero-to-hero-all-in-one-blog-post/
19. Emil Kowalski — Great Animations: https://emilkowal.ski/ui/great-animations
20. web.dev — Learn PWA: App design: https://web.dev/learn/pwa/app-design
21. MDN — CSS env() (safe-area insets): https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Values/env
22. Vercel — Geist Design System: https://vercel.com/geist/introduction
23. Pencil & Paper — Dashboard Design UX Patterns: https://www.pencilandpaper.io/articles/ux-pattern-analysis-data-dashboards
24. Rauno Freiberg — Invisible Details of Interaction Design: https://rauno.me/craft/interaction-design
25. Rauno Freiberg — Craft index: https://rauno.me/craft
26. M3 — Cards guidelines: https://m3.material.io/components/cards/guidelines
