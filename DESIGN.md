# esRAGPro Frontend Design Contract

This document records the visual language already present in the Vue + Element Plus
operational UI. New auth surfaces use these tokens and primitives without changing
the existing business pages.

## 1. Atmosphere & Identity

The product is a quiet, information-dense knowledge-work console. White elevated
surfaces sit over a cool gray application canvas; blue is reserved for navigation,
primary actions, and links so status colors remain legible.

## 2. Color

| Role | Token | Value | Usage |
| --- | --- | --- | --- |
| App canvas | `--ui-bg` | `#f5f7fa` | Page background |
| Surface | `--ui-surface` | `#ffffff` | Header, forms, cards |
| Text primary | `--ui-text-primary` | `#303133` | Headings and values |
| Text secondary | `--ui-text-secondary` | `#606266` | Supporting copy |
| Text muted | `--ui-text-muted` | `#909399` | Metadata and hints |
| Border | `--ui-border` | `#ebeef5` | Dividers and outlines |
| Accent | `--ui-primary` | `#409eff` | Links, focus, primary actions |
| Success | `--ui-success` | `#67c23a` | Healthy or complete status |
| Warning | `--ui-warning` | `#e6a23c` | Caution states |
| Danger | `--ui-danger` | `#f56c6c` | Errors and destructive actions |

## 3. Typography

The existing system uses the platform sans stack: `-apple-system`, `BlinkMacSystemFont`,
`Segoe UI`, `Roboto`, `Helvetica Neue`, and `Arial`. Body text is 14-16px with a
1.5-1.6 line height; page headings use 24-36px and compact form headings use 22-24px.
Tracking remains normal (`0`) and CJK copy may wrap naturally.

## 4. Spacing & Layout

Spacing follows a 4px base. Common existing values are 4, 8, 12, 16, 20, and 24px.
The global header is 60px on desktop and 56px on mobile; page content uses 24px desktop
and 16px mobile padding. Auth content is a centered single-column form capped at 420px.

## 5. Components

### Element Plus Header
- **Structure**: logo, horizontal navigation, mobile menu control, user action area.
- **States**: signed out (`登录`), signed in (username menu), mobile drawer open/closed.
- **Accessibility**: native button/menu semantics, visible focus, keyboard reachability.

### Auth Form
- **Structure**: heading, supporting copy, alert, labeled username/password fields, primary submit.
- **States**: idle, loading, validation error, authentication error.
- **Spacing**: 16px field rhythm, 24px panel padding, 4px radius consistent with Element Plus.
- **Accessibility**: explicit labels, autocomplete hints, inline error text, full keyboard flow.

## 6. Motion & Interaction

Use Element Plus defaults and short 150-200ms ease-out transitions for menus and feedback.
No decorative motion is introduced. Respect `prefers-reduced-motion` through the existing
Element Plus behavior.

## 7. Depth & Surface

Use the existing mixed treatment: `1px solid #ebeef5` borders plus the restrained header/card
shadow `0 2px 8px rgba(0, 0, 0, 0.1)`. Auth surfaces are white on the app canvas and use
4px radii unless an Element Plus component supplies its own anatomy.

## 8. Accessibility Constraints & Accepted Debt

- Target WCAG 2.2 AA contrast for body text and controls.
- Every auth control is labeled, keyboard reachable, and exposes loading/error state.
- Do not persist passwords or place them in redirect URLs, logs, or session metadata.
- Accepted debt: existing business pages retain their current inline Element Plus tokens; this
  auth slice does not redesign them.
