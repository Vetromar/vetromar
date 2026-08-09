# Vetromar launch site

A static single-page site: the desktop app's 3D knowledge graph as the
navigation surface. One clickable node — "Get in contact" — everything else is
decorative. No build step, no framework, no backend.

- `js/camera3d.js` is a verbatim copy of
  `desktop/frontend/src/lib/camera3d.js`; the scene math, physics config, and
  styling mirror `Graph.svelte` / `app.css`.
- `vendor/d3-force.bundle.min.js` is the only third-party code (d3-dispatch +
  d3-quadtree + d3-timer + d3-force UMD builds, committed — no CDN at runtime).
- The contact email lives in two places: `js/data.js` (`EMAIL`) and the
  `mailto:` link in `index.html`.

**Preview locally** (ES modules need http, not `file://`):

```sh
cd website
npm run dev
# open http://localhost:3000  (PORT=xxxx npm run dev to change)
```

`npm run dev` needs no `npm install` — `dev-server.mjs` is a zero-dependency
node static server. `python3 -m http.server 8000 -d website` works too.

**Deploy:** upload the `website/` folder as-is to any static host
(GitHub Pages, Netlify, Cloudflare Pages, an S3 bucket…).
