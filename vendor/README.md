# Vendored assets

Every file here is served by `serve.py`'s `_serve_vendor_asset()` from
`/vendor/<name>` and referenced by `index.html` with a **root-relative** path.
No `<script src>` in the shipped page points anywhere but `/vendor/`.

This is not merely an availability preference. `index.html` renders the user's
own prompts, file paths and source code, so a third-party script executing in
that page is a privacy and supply-chain surface, not just a dependency —
CLAUDE.md constraint 2, *nothing leaves the machine*. Downloading a library
once, to vendor it, is fine; referencing one at runtime is not.

Neither bundle contains a network API. `tests/test_serve.py`'s
`VendoredAssetTest` re-checks that on every run, along with the SHA-256 of each
file against the digest recorded below, so a swapped or edited bundle fails CI
rather than shipping.

`vendor/` holds no build step, no bundler and no package manifest. Adding a
`package.json` would break the `stdlib-only` CI job by design.

## alpine.min.js — Alpine.js 3.15.12 (MIT)

| | |
|---|---|
| Version | 3.15.12 |
| Retrieved | 2026-08-05 |
| Origin | `https://registry.npmjs.org/alpinejs/-/alpinejs-3.15.12.tgz`, member `package/dist/cdn.min.js` |
| Size | 46,346 bytes |
| SHA-256 | `57b37d7cae9a27d965fdae4adcc844245dfdc407e655aee85dcfff3a08036a3f` |

Integrity, checked on retrieval (2026-08-05): the tarball's SHA-512 was compared
against the `dist.integrity` value the npm registry publishes for this release
(`sha512-nJvPAQVNPdZZ0NrExJ/kzQco3ijR8LwvCOadQecllESiqT4NyZ/57sN9V2XyvhlBGAbmlKYgeWZvYdKq99ij/Q==`)
and matched, as did `dist.shasum`
(`2ccd224961ba6236ca379c2abbcfa0bfeade07ee`). The file here was then extracted
from that verified tarball rather than downloaded separately, so it inherits
that check. The SHA-256 above is of the extracted file and is what the test
asserts.

The bundle mentions `https://alpinejs.dev/plugins/...` twice, both inside a
console warning about a missing plugin. It makes no request: `fetch(`,
`XMLHttpRequest`, `import(`, `importScripts`, `navigator.sendBeacon`,
`WebSocket` and `EventSource` do not appear in it at all.

## chart.umd.min.js — Chart.js 4.4.1 (MIT)

| | |
|---|---|
| Version | 4.4.1 |
| Retrieved | before this file existed — see below |
| Origin | jsDelivr, `/npm/chart.js@4.4.1/dist/chart.umd.js` |
| Size | 205,399 bytes |
| SHA-256 | `d2af8974e95271638772e9e9524db5b9a6f58d6ec2d5d781400447b4a31c681e` |

The version and origin are read from the bundle's own banner, which jsDelivr
wrote into it; CPB's history does not record when it was fetched, and this file
does not invent a date. The SHA-256 was computed from the committed file on
2026-08-05 and pins it from here on: it is a record of what is in the tree, not
an independent verification of what upstream published.
