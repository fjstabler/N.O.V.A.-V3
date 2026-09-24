# Shipped fonts

Both are served from the core alongside the bundle rather than fetched from a
CDN. A wall panel on a home network should not need the internet to render its
own clock, and the Echo Show's WebView has neither font installed.

| File | Family | Licence |
|---|---|---|
| `inter.woff2` | Inter, variable 100–900, latin | SIL Open Font License 1.1 |
| `orbitron.woff2` | Orbitron, variable 400–900, latin | SIL Open Font License 1.1 |

`base.css` names Inter first in the body stack and always has. Until these were
added, nothing on a panel could load it: LineageOS has no Inter, no SF Pro and
no Segoe, so every fallback missed and the whole interface rendered in Roboto —
which is why the clock looked like the stock Android one.

Orbitron is used for the clock's digits on a panel only. It is wide and
geometric, which reads at a distance and belongs with the orbital rings; it is
also nearly illegible at small sizes, so nothing else uses it.
