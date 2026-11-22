# A2UI web client — design spec

A browser client (Lit + Vite, TypeScript) that talks to an A2A agent and
renders A2UI responses: structured UI the agent sends as data parts
(`A2UIResponseFormatter`, [transport-a2a.md](transport-a2a.md)). It depends on
the A2UI renderers from the google/A2UI repository. User guide:
[../a2ui.md](../a2ui.md).

Code: `a2ui/`.

## `a2ui/index.html`

**Does:** the page ("Agent UI") that loads `main.ts`.

## `a2ui/main.ts`

**Does:** `A2UIMain`, the app element: the chat input, sending a message, and
rendering the agent's A2UI surfaces as they arrive.

## `a2ui/client.ts`

**Does:** `A2UIClient`, sends a message to the agent through `/a2a` and returns
the A2UI messages from the reply.

**Rules:**

- A2UI-1: The browser never calls the agent directly; it calls `/a2a`, which
  the Vite middleware forwards (A2UI-2).

## `a2ui/middleware/a2a.ts`

**Does:** a Vite dev-server plugin that proxies `/a2a` to the agent's A2A
endpoint.

**Rules:**

- A2UI-2: The agent's address is configured in the dev server, not in the
  browser, so the page works without CORS changes on the agent.

## `a2ui/middleware/index.ts`

**Does:** exports the middleware (`A2AMiddleware`).

## `a2ui/vite.config.ts`

**Does:** Vite config: installs the `/a2a` middleware and resolves the A2UI
renderer packages.

## `a2ui/events/events.ts`

**Does:** custom DOM events, such as `SnackbarActionEvent`.

## `a2ui/types/types.ts`

**Does:** shared types for snackbar messages and actions.

## `a2ui/theme/theme.ts`

**Does:** the A2UI theme (colours, typography, component styles) passed to the
renderer.

## `a2ui/ui/ui.ts`

**Does:** exports the UI elements (`Snackbar`).

## `a2ui/ui/snackbar.ts`

**Does:** `Snackbar`, transient notifications with optional actions.

## `a2ui/ui/shared-constants.ts`

**Does:** paths of the sandbox iframe used by MCP app components.

## `a2ui/ui/custom-components/register-components.ts`

**Does:** `registerContactComponents()` registers the custom components below
with the A2UI renderer, so an agent can name them in its UI.

## `a2ui/ui/custom-components/org-chart.ts`

**Does:** `OrgChart`, renders a tree of people (`OrgChartNode`).

## `a2ui/ui/custom-components/premium-text-field.ts`

**Does:** `PremiumTextField`, a styled replacement for the standard text
field, used to show overriding a built-in component.

## `a2ui/ui/custom-components/web-frame.ts`

**Does:** `WebFrame`, shows a web page inside the surface in an iframe.

## `a2ui/ui/custom-components/mcp-apps-component.ts`

**Does:** `McpApp`, hosts an MCP app in a sandboxed iframe.

## `a2ui/ui/custom-components/test/org-chart-test.html`

**Does:** a manual test page for `org-chart`.

## `a2ui/ui/custom-components/test/override-test.html`

**Does:** a manual test page for overriding a built-in component.

## `a2ui/ui/custom-components/test/override-test.ts`

**Does:** script for `override-test.html`.
