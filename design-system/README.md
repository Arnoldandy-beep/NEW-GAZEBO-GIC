# GIC-MS — "Gazebo Premium" design system

Self-contained, dependency-free visual system and high-fidelity mockups for the GAZEBO Investment Club
management system. Nothing here is wired into the Flask app yet — open the files directly in a browser
(or `python3 -m http.server` from this folder) and lift the tokens/markup into the templates.

| File | Deliverable |
| --- | --- |
| `index.html` | Landing page linking all four deliverables |
| `admin-dashboard.html` | 1 · Admin / leadership command centre |
| `member-dashboard.html` | 2 · Member personal dashboard (mobile first) |
| `component-kit.html` | 3 · Component kit, every pattern in light **and** dark |
| `style-guide.html` | 4 · Brand board: tokens, type, spacing, elevation, icons, rollout |
| `css/gic-theme.css` | The whole system — tokens + components, ~1 file |
| `js/theme.js` | Theme toggle + `localStorage` persistence (`gic-theme`) |

## Principles

- **Navy is authority, gold is reward.** Gold is capped at roughly 10% of any screen: money-moving
  actions, the savings pool figure, dividends. Never body text.
- **Colour carries meaning.** Green/amber/red are reserved for state, not decoration.
- **Cooperative, not corporate.** Real member names, meeting notices, savings streaks, club sittings.
- **Dense for leadership, roomy for members.** Same components, two densities.
- **Dark mode is first-class.** Every component is authored against tokens, so no component needs a
  dark-specific override.

## Integration

1. Copy `css/gic-theme.css` and `js/theme.js` into `static/css/` and `static/js/`.
2. In `templates/base.html`: add `data-theme="light"` to `<html>`, link the stylesheet **after**
   Bootstrap, and load `theme.js` before `</body>`.
3. Rebuild each module's markup with the `.gic-*` classes. All classes are namespaced, so they can
   coexist with the current `static/css/style.css` during a gradual rollout.
4. Charts are inline SVG using token-aware classes — no charting library required.
5. Keep one Python `STATUS_CHIP` dict (see the style guide) so every module renders the same badge for
   the same state.

Suggested rollout order: shell/base → admin dashboard → member dashboard → savings/loans/fines →
members/minutes/notifications → statements/settings.
