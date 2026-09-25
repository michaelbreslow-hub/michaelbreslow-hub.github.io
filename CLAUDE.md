# CLAUDE.md

## Git commits

- Never add Claude as a co-author in commit messages. Do not include any `Co-Authored-By: Claude ...` trailer or other AI attribution lines.

## JavaScript and CSS libraries

- Don't use npm (or yarn, pnpm or any other JS package manager). Never add a `package.json`, `node_modules` or a JS build step.
- Load any JS or CSS library as a CDN link (`<script src>` or `<link rel="stylesheet">`) from a reputable CDN such as cdnjs or jsDelivr. Pin an exact version, and add an `integrity` (SRI) hash with `crossorigin="anonymous"` when the CDN provides one.
