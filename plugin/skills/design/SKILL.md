---
name: design
description: Use before writing the HTML and CSS of an artifact page. Mobile-first layout, what the page frame provides, light and dark theming through tokens, typography, inline SVG diagrams, and what to avoid.
---

# Live artifact page design (live-artifacts)

Load the `capabilities` skill first if the page stores or submits anything. The "What the page frame provides"
section is the authority on platform behaviour; general web advice applies where it says nothing.

## What the page frame provides

- **Wrapping.** You pass `html` to `live_artifact_create` or `live_artifact_update`. A body fragment is wrapped in a doctype, a
  charset, and a viewport meta with `viewport-fit=cover`, followed by a reset: `color-scheme: light`, `:root` padded by
  the safe-area insets, zero body margin, a 14px system font, `img { max-width: 100% }`, and
  `[hidden] { display: none !important }`. Toggle visibility with `el.hidden`, not `style.display`. A full document
  keeps its own head.
- **Safe areas.** Keep the `:root` padding. A bar fixed to the top or bottom adds `env(safe-area-inset-top, 0px)` or
  `env(safe-area-inset-bottom, 0px)` to its own padding. A sticky header uses `top: env(safe-area-inset-top, 0px)`.
- **Title.** Put `<title>` at the top of the file. It names the page in the browser tab. Use a two-to-four-word noun
  phrase specific to the subject, and keep it the same across updates.
- **Theme.** Only `prefers-color-scheme` reaches the page; there is no theme toggle. Define every colour as a token on
  bare `:root` in light values. Redefine only the tokens inside `@media (prefers-color-scheme: dark)`, with
  `color-scheme: dark` there so form controls follow. Give `body` an explicit background from a token. Never put a
  literal colour in a component rule that only reads correctly in one theme.
- **There are no files beside the page.** The server serves one page per artifact and no static assets. Each page
  must be a single self-contained document. Put CSS in a `<style>` block and JavaScript in an inline `<script>`.
  Images, fonts and data must be inline (`data:` URIs or inline `<svg>`) or come from an allowed CDN. There is no
  asset upload here, so do not write `<img src="logo.png">`, `<link href="style.css">` or `<script src="app.js">`.
- **Scripts and styles (CSP-enforced).** Inline `<script>` and `<style>` are allowed. External scripts load from
  `https://cdnjs.cloudflare.com` (preferred), `https://cdn.jsdelivr.net/npm/`, `https://unpkg.com`,
  `https://cdn.tailwindcss.com`, `https://code.jquery.com` and `https://cdn.plot.ly`. Stylesheets load from
  `https://fonts.googleapis.com`, and font files from `https://fonts.gstatic.com`. Everything else is blocked.
  Pin each library to an exact version. Place a UMD build before the inline script that uses it. Inline any library
  stylesheet.
- **Network.** `connect-src` is `'self'`. A page cannot fetch from any other host. Its only data path is
  `claude.use("db")` and, when declared, `claude.use("mcp")`. Images are allowed only from `data:` or `blob:` URLs.
  External image URLs are blocked.
- **The phone needs internet for CDN scripts and fonts.** Inline small assets, and use a CDN only when a library does
  real work. Give every font face a real fallback stack.

## Read the request first

Most artifacts here are tools the user works through once, often on a phone: a review list, a form, a decision sheet,
a dashboard. Those want a utilitarian treatment: clear hierarchy, considered spacing, a restrained palette, no hero.
A page the user will keep or share can have more character. A well-composed page is always acceptable.

Name the subject before you design: one concrete subject, one job for the page. Use real content (the actual items,
their units and terms). Never lorem ipsum, and never invent records that belong to the user.

## Mobile first

The link is usually opened on a phone, often from a chat app. Design the 400px column first, then widen it.

- One column with a side gutter of at least 16px at every width. Set vertical padding with `padding-block`, not a
  `padding` shorthand that zeroes the sides.
- Body text at least 16px. Smaller inputs make iOS Safari zoom on focus. Tap targets at least 40px high, with space
  between neighbours. Put the primary action at the end of the list or in a sticky bottom bar with
  `padding-bottom: env(safe-area-inset-bottom, 0px)`.
- Use flex or grid with `gap`, not per-element margins. Let rows wrap or stack. Give any flex or grid child that holds
  running text `min-width: 0`.
- The page body never scrolls sideways. Only tables, code and diagrams may be wider, each inside its own
  `overflow-x: auto` container. Images and `aspect-ratio` boxes get `max-width: 100%`.
- Prefer native controls (`checkbox`, `select`, `textarea`, `input type="date"`). They bring the phone's own pickers.
  Give every control a stable `id` and a visible `label`, and keep keyboard focus visible.
- Review lists: one card or row per item. Show state in form as well as colour (a checked box, a pill, a stripe).
  Put long notes behind a tap. Show progress near the submit control ("7 of 12 reviewed").
- Respect `prefers-reduced-motion`. Skip animation unless it carries meaning.

## Typography, colour and layout

- **Type.** Pair a display face with a body face from Google Fonts, or use system fonts and carry the design with
  weight, size and spacing. Keep to one small type scale. Use `font-variant-numeric: tabular-nums` where digits line
  up. Give headings `text-wrap: balance`. Keep running text near 65 characters wide. Give uppercase labels a little
  letter-spacing.
- **Colour.** Four to six named tokens. One accent. Semantic colours (good, warning, critical) are separate from the
  accent and used only for state. Neutrals should lean slightly toward the accent hue; pure grey looks unconsidered.
  Check the accent on both backgrounds. Do not simply invert the dark theme.
- **Cards.** Border, fill, radius and shadow each mark an element as a separate object. Use them for the one thing that
  needs setting off, not on every block.
- **Repeated elements** share edges, baselines and inner padding. Let content set heights. Text that can outgrow its
  track wraps or scrolls inside its own container. Clipped text is a bug.
- **Complete at rest.** Everything meant to be read is visible on load. Nothing waits at `opacity: 0` for a scroll
  observer. A tool opens in a working state. When records come from `db`, the first frame is a designed empty state
  that says what will appear and how to add the first entry, then the store fills it in.
- **Dashboards and tools** are scanned, not read. Put the summary first. Show state in form as well as numbers. Show
  big-number tiles only when the figures are the point.
- **CSS hygiene.** Watch selector specificity so one rule does not silently cancel another's spacing. Close every
  element and double-quote attributes.

## Copy

Write from the user's side of the screen. Name things by what people recognise, not how the system works. A button
says what happens ("Send answers"), and after a tap the page says it happened. Errors say what went wrong and what to
do next. Use short, plain sentences.

## Diagrams

Use an inline `<svg>` for a diagram of a mechanism, a flow or a comparison. A diagram earns its place when a reader
would otherwise have to rebuild the mechanism from prose. When a sentence makes the point as quickly, write the
sentence.

- **No mermaid, no libraries.** The page frame does not render mermaid. Do not write `mermaid` fences or
  `<pre class="mermaid">`. Draw with `rect`, `circle`, `line`, `polyline`, `path` and `text`, and do not load a
  diagram library from a CDN.
- **Size by `viewBox`.** `viewBox="0 0 W H"`, with CSS `max-width: 100%; height: auto`. Keep a wide flow to about 640
  units, or stack it top to bottom. On a phone the figure is about 360px wide, so keep the smallest label at 11px or
  more after scaling.
- **Colour through `currentColor`.** Strokes, text and arrowheads inherit the foreground, which works in both themes.
  Use `var(--accent)` only on the element that carries the point, and check it on both backgrounds. Give every shape
  an explicit `fill`, `fill="none"` or a token, never the default black.
- **Show the mechanism, not the name.** A box labelled "cache" says less than the route a request takes through it.
  Draw the parts the argument depends on. For two options, draw the one edge each adds or removes. Separate boxes with
  no connection to the system are a list, not a comparison.
- **Label the arrows.** "writes" or "polls every 30s" says more than a bare arrow. Add a legend only when a visual code
  (dashed, coloured) repeats.
- **Arrowheads** come from a `<defs><marker>` with `marker-end="url(#arrow)"`, or a small `<polygon>` at the line end.
  Use unique ids if the page has several SVGs.
- **Text.** About 11 to 13px at drawn scale, `text-anchor` for alignment, short labels. Explanation goes in the caption.
- **Structure.** Wrap the SVG in `<figure>` with a `<figcaption>` that states the claim. Give the `<svg>`
  `role="img"` and an `aria-label` with the same claim. Do not use `<script>`, `<style>` or `<foreignObject>` inside
  the SVG. Reference gradients and `<use>` ids within the same fragment.

    <figure>
      <svg viewBox="0 0 560 140" role="img" aria-label="Writes go to the queue, not the database"
           style="max-width:100%;height:auto;color:var(--fg)">
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7"
                  orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="currentColor"/></marker>
        </defs>
        <rect x="10"  y="50" width="120" height="40" rx="6" fill="none" stroke="currentColor"/>
        <rect x="220" y="50" width="120" height="40" rx="6" fill="none" stroke="var(--accent)"/>
        <rect x="430" y="50" width="120" height="40" rx="6" fill="none" stroke="currentColor"/>
        <text x="70"  y="75" text-anchor="middle" font-size="13" fill="currentColor">writer</text>
        <text x="280" y="75" text-anchor="middle" font-size="13" fill="currentColor">queue</text>
        <text x="490" y="75" text-anchor="middle" font-size="13" fill="currentColor">reader</text>
        <line x1="130" y1="70" x2="218" y2="70" stroke="currentColor" marker-end="url(#arrow)"/>
        <line x1="340" y1="70" x2="428" y2="70" stroke="currentColor" marker-end="url(#arrow)"/>
        <text x="174" y="62" text-anchor="middle" font-size="11" fill="var(--muted)">enqueue</text>
        <text x="384" y="62" text-anchor="middle" font-size="11" fill="var(--muted)">poll 30s</text>
      </svg>
      <figcaption>Writers never touch the reader's store; the queue is the only shared edge.</figcaption>
    </figure>

## Avoid the defaults

Models drift toward the same few looks: cream backgrounds with a serif display and a terracotta accent; near-black
with one neon accent; purple-to-blue gradients; emoji as section markers; everything centred; the same large radius
on every element; an accent bar on every card. If the user names a direction, follow it exactly, including when it
is one of these. Otherwise, choose something specific to the subject.

Numbering, eyebrows and dividers should carry information. Number a list only when the order matters.

## Updating a page that has data

`live_artifact_update` replaces the HTML and keeps the database. Keep the token block, the structure and the field names,
so documents saved by the previous version still render. Treat every field as possibly absent. Give every form control
a stable `id`, because the page matches stored values to controls by id. An open page does not reload itself; the
user sees the new version after a reload, and autosaved entries come back with it.

## Before you create the page

Before `live_artifact_create`, check the source for the phone-width risks: fixed widths wider than 400px, `100vw`,
`min-width` on flex children with text, literal colours outside the tokens, and a dark block that misses a token.

If the agent has a browser tool available (for example a Hermes browser or Playwright skill), take one look at the
page at phone width and desktop width, in both themes, before creating it, and fix what it shows. Otherwise rely on
the source check. Either way, create the page once, and fix what the user reports rather than polishing unprompted.
