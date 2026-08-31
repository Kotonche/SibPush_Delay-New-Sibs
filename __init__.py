"""Progressive Siblings add-on entrypoint."""

from __future__ import annotations

from .sibpush.hooks import register_hooks


register_hooks()
