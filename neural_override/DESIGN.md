---
name: NEURAL OVERRIDE
colors:
  surface: '#131313'
  surface-dim: '#131313'
  surface-bright: '#393939'
  surface-container-lowest: '#0e0e0e'
  surface-container-low: '#1c1b1b'
  surface-container: '#201f1f'
  surface-container-high: '#2a2a2a'
  surface-container-highest: '#353534'
  on-surface: '#e5e2e1'
  on-surface-variant: '#b9cacb'
  inverse-surface: '#e5e2e1'
  inverse-on-surface: '#313030'
  outline: '#849495'
  outline-variant: '#3a494b'
  surface-tint: '#00dce6'
  primary: '#e3fdff'
  on-primary: '#00373a'
  primary-container: '#00f3ff'
  on-primary-container: '#006b71'
  inverse-primary: '#00696f'
  secondary: '#ffabf3'
  on-secondary: '#5b005b'
  secondary-container: '#fe00fe'
  on-secondary-container: '#500050'
  tertiary: '#e8ffda'
  on-tertiary: '#053900'
  tertiary-container: '#36fd0f'
  on-tertiary-container: '#107000'
  error: '#ffb4ab'
  on-error: '#690005'
  error-container: '#93000a'
  on-error-container: '#ffdad6'
  primary-fixed: '#6ff6ff'
  primary-fixed-dim: '#00dce6'
  on-primary-fixed: '#002022'
  on-primary-fixed-variant: '#004f53'
  secondary-fixed: '#ffd7f5'
  secondary-fixed-dim: '#ffabf3'
  on-secondary-fixed: '#380038'
  on-secondary-fixed-variant: '#810081'
  tertiary-fixed: '#79ff5b'
  tertiary-fixed-dim: '#2ae500'
  on-tertiary-fixed: '#022100'
  on-tertiary-fixed-variant: '#095300'
  background: '#131313'
  on-background: '#e5e2e1'
  surface-variant: '#353534'
typography:
  display-lg:
    fontFamily: Space Mono
    fontSize: 48px
    fontWeight: '700'
    lineHeight: '1.1'
    letterSpacing: -0.02em
  display-md:
    fontFamily: Space Mono
    fontSize: 32px
    fontWeight: '700'
    lineHeight: '1.2'
    letterSpacing: 0.05em
  headline-sm:
    fontFamily: Space Mono
    fontSize: 20px
    fontWeight: '700'
    lineHeight: '1.4'
    letterSpacing: 0.1em
  body-lg:
    fontFamily: JetBrains Mono
    fontSize: 16px
    fontWeight: '400'
    lineHeight: '1.6'
    letterSpacing: 0em
  body-md:
    fontFamily: JetBrains Mono
    fontSize: 14px
    fontWeight: '400'
    lineHeight: '1.6'
    letterSpacing: 0em
  code-sm:
    fontFamily: JetBrains Mono
    fontSize: 12px
    fontWeight: '500'
    lineHeight: '1.4'
    letterSpacing: 0.02em
  label-caps:
    fontFamily: Space Mono
    fontSize: 10px
    fontWeight: '700'
    lineHeight: '1.2'
    letterSpacing: 0.2em
spacing:
  unit: 4px
  gutter: 16px
  margin-mobile: 16px
  margin-desktop: 32px
  container-max: 1440px
---

## Brand & Style
The design system embodies a high-fidelity "ICE-breaker" terminal aesthetic, positioning the user as an elite netrunner within a high-stakes cyber-warfare environment. The brand personality is aggressive, technical, and immersive, evoking the feeling of navigating a forbidden corporate mainframe.

The design style is a fusion of **Digital Brutalism** and **Glassmorphism**. It utilizes high-contrast neon accents against an abyssal black backdrop to simulate a self-illuminated hardware interface. The UI should feel like a physical console: utilitarian yet cinematic. Visual interest is driven by "glitch" artifacts, scanline overlays, and localized chromatic aberration on critical alerts.

## Colors
The palette is built on a "Pure Dark" foundation to maximize the luminosity of the neon accents.

- **Primary (Electric Cyan):** Used for "Player" or "Friendly" actions, active navigation, and successful connection states.
- **Secondary (Neon Magenta):** Reserved for "Hostile" presence, opponent actions, and high-threat warnings.
- **Tertiary (Terminal Green):** Used for system logs, diagnostic data, and "Safe" execution status.
- **Surface Neutrals:** Deep greys are used to create structural hierarchy without breaking the immersion of the black void.

All neon colors should be implemented with a `0 0 8px` outer glow (drop-shadow) using the same hex code at 50% opacity to simulate light bleed on a CRT monitor.

## Typography
Typography is strictly monospaced to maintain the "Terminal" aesthetic. 

**Headlines** must always be uppercase. For large display text, use a slight horizontal "glitch" offset (1px shift of the red/blue channels) to reinforce the digital transmission theme. 

**Body Text** uses JetBrains Mono for its exceptional legibility in dense data environments. Use "Terminal Green" for blocks of technical readout and "Electric Cyan" for interactive prompts. 

**Labels** are used for micro-copy and data headers, utilizing wide letter-spacing to ensure a technical, blueprint-like feel.

## Layout & Spacing
The layout follows a **Rigid Grid** philosophy. All elements must align to a 4px baseline grid. 

The screen is divided into a 12-column system for desktop and a 4-column system for mobile. Visual containers should use "terminal window" logic: fixed-width sidebars for system stats (RAM, CPU, Connection Strength) and a fluid central area for the primary task/hacking simulation.

Margins and gutters are kept tight (16px) to maximize screen real estate for data. Use subtle 1px grid-lines in the background (at 5% opacity) to provide a spatial anchor for the UI elements.

## Elevation & Depth
Elevation is achieved through **Glassmorphism** and **Luminous Outlines** rather than traditional shadows.

1.  **Level 0 (Background):** Pure #050505 with a subtle scanline overlay (repeating linear gradient).
2.  **Level 1 (Panels):** #121212 with a 40% opacity. Backdrop-filter: blur(12px). 1px solid border in Dark Gray.
3.  **Level 2 (Active/Selected):** Same as Level 1, but the 1px border takes the Primary (Cyan) or Secondary (Magenta) color with a subtle outer glow.
4.  **Level 3 (Modals/Overlays):** High transparency (20%), heavy blur (24px), with a thick (2px) corner-bracket accent in the Primary color.

Avoid drop shadows. Use "inner-glow" on containers to simulate the light of the screen reflecting off the frame.

## Shapes
The shape language is **Strictly Geometric and Brutalist**. 

- **Corners:** 0px radius (Sharp). Softening is permitted only for specific circular readouts (e.g., System Integrity meters).
- **Accents:** Use 45-degree "clipped" corners on buttons and card headers to evoke military-grade hardware. 
- **Dividers:** Use dashed lines or sequences of small blocks (e.g., `[ ■ ■ ■ □ □ ]`) to indicate progress or separation.

## Components
- **Buttons:** Sharp-edged. Default state has a 1px Cyan border. Hover state fills the button with Cyan, changing text to Black. Include a "bracket" detail `[` `]` at the ends of the button text.
- **Cards (Data Nodes):** Styled as microchips. Title bar should have a background color (Cyan or Magenta) with Black text. The body of the card should be semi-transparent with a 1px border.
- **Progress Bars (System Integrity):** Represented as a series of vertical blocks or a circular "loading" ring with segmented ticks. Terminal Green for 100-50%, Yellow for 49-20%, and Neon Magenta for <20%.
- **Input Fields:** Styled as a command prompt. Starts with a `>` character and includes a blinking underscore cursor `_`. 
- **Chips/Badges:** Small, rectangular tags. Use monospaced labels. Hostile entities get Magenta badges; System tasks get Green badges.
- **Terminal Logs:** A scrolling list component. New entries should "flicker" into existence. Use different colors for log levels: `INFO` (Green), `WARN` (Yellow), `THREAT` (Magenta).