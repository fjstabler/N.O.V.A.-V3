"""The mobile web client's static files (served by transport.server).

No Python logic lives here — this package exists only so `static/` ships
alongside the rest of `nova` under any install method (editable or built),
found at runtime via a path relative to this file rather than a hardcoded
repo layout.
"""
