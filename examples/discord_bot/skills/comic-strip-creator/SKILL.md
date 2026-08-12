---
name: comic-strip-creator
description: Create simple comic-strip visuals as editable SVG files and export them to JPG for Discord attachment. Use for visual explainers, comic panels, and revisions to existing comics.
---

# comic-strip-creator

Create simple comic-strip visuals as editable `SVG` files and export them to `JPG` for Discord attachment.

## When to use
Use this skill when the user asks for:
- a comic strip
- a visual explainer
- a simple illustrated infographic in comic panels
- revisions to an existing comic (colors, characters, captions, layout)

## Outputs
Always save user-facing files under `attachments/`.
Preferred outputs:
- source: `attachments/<name>.svg`
- export: `attachments/<name>.jpg`

In the final answer, always include:
- `[[attach:attachments/<name>.jpg]]`

Optionally also attach the SVG if the user may want edits.

## Workflow
1. Clarify the requested style, number of panels, and topic if missing.
2. Draft a short panel plan:
   - title
   - panel captions
   - simple visual elements per panel
3. Create an `SVG` comic strip:
   - use a clean 2x2 or horizontal layout
   - include readable fonts, large text, and high contrast
   - prefer simple vector shapes: panels, speech bubbles, robots, books, people, arrows, icons
4. Export the SVG to raster output.
   - First try `rsvg-convert` to produce PNG
   - Then convert PNG to JPG using `sips` on macOS
5. Return the JPG attachment marker.

## Visual guidelines
- Keep designs polished but simple.
- Use colorful human characters when appropriate.
- Maintain generous margins so text is not clipped.
- Keep text concise enough to fit at Discord-preview size.
- Prefer soft background colors and clear panel borders.
- If a warning or caveat exists, use a small icon panel element instead of too much text.

## Suggested file naming
- `attachments/<topic>_comic_strip.svg`
- `attachments/<topic>_comic_strip.jpg`

## Example export commands
Check available tools:
```sh
command -v rsvg-convert
```

Render and convert:
```sh
rsvg-convert attachments/example.svg -o attachments/example.png
sips -s format jpeg attachments/example.png --out attachments/example.jpg
```

## Notes
- If the user asks for revisions, edit the SVG source and regenerate the JPG.
- Never claim an image was generated unless the files were actually written.
- If JPG conversion is unavailable, provide the SVG and explain the limitation.
