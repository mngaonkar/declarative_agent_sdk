# A2UI Contact Sample

A UI component for generating and visualizing A2UI (Agent-to-User Interface) contact responses.

## Prerequisites

- [Node.js](https://nodejs.org/en) (v18 or higher recommended)
- npm (comes with Node.js)
- **A2UI Repository** - Clone the A2UI repository at `../../../A2UI/` (relative to this directory)
  ```bash
  # From the google-adk-samples root directory
  cd ../..
  git clone https://github.com/google/A2UI.git
  ```
  
  The A2UI repository should contain these renderers:
  - `angular/`
  - `flutter/`
  - `lit/`
  - `markdown/`
  - `react/`
  - `web_core/`
  
  **Build Required Renderers** - This sample requires the Lit and Markdown renderers to be built:
  ```bash
  # Build Lit renderer
  cd A2UI/renderers/lit
  npm install
  npm run build
  
  # Build Markdown renderer
  cd ../markdown/markdown-it
  npm install
  npm run build
  
  # Return to the a2ui sample directory
  cd ../../../../google-adk-samples/declarative_agent_sdk/a2ui
  ```

## Installation

```bash
npm install
```

## Running the Development Server

```bash
npm run dev
```

The dev server will start and display the local URL (typically http://localhost:5173/, but may use a different port if 5173 is already in use).

## Dependencies

This sample uses the following key dependencies:

- **@a2ui/lit** - Lit-based renderer for A2UI components
- **@a2ui/markdown-it** - Markdown rendering support
- **@modelcontextprotocol/ext-apps** - MCP (Model Context Protocol) Apps integration

All dependencies are installed via npm and don't require manual building.

## Configuration

The project uses Vite for development and building. Configuration can be found in `vite.config.ts`.

### Using Local A2UI Development

If you have the A2UI repository cloned locally and want to use it for development, you can uncomment the aliases in `vite.config.ts`:

```typescript
alias: {
  "@a2ui/markdown-it": resolve(__dirname, "../../../../renderers/markdown/markdown-it/dist/src/markdown.js"),
  // ... other aliases
}
```

**Note:** The required renderers should already be built if you followed the Prerequisites section. If not, build them first:
```bash
# Build Lit renderer
cd ../../../A2UI/renderers/lit
npm install
npm run build

# Build Markdown renderer
cd ../markdown/markdown-it
npm install
npm run build
```

### Troubleshooting

If you encounter module resolution errors:

1. **Clear Vite cache:**
   ```bash
   rm -rf node_modules/.vite
   ```

2. **Reinstall dependencies:**
   ```bash
   rm -rf node_modules package-lock.json
   npm install
   ```

3. **Check for broken symlinks:**
   ```bash
   ls -la node_modules/@a2ui/
   ```

## Project Structure

- `main.ts` - Main chat interface component
- `ui/` - UI components and styles
- `middleware/` - Server middleware
- `vite.config.ts` - Vite configuration
- `index.html` - Entry HTML file

## Building for Production

```bash
npm run build
```

The built files will be output to the `dist/` directory.

## Available Scripts

- `npm run dev` - Start development server
- `npm run build` - Build for production
- `npm run serve` - Run the build via Wireit
- `npm run format` - Format code with Prettier
- `npm run format:check` - Check code formatting