"""Shared billing helpers reused by every payment webhook (ADR-055).

Leaf package: the billing webhooks (``billing_adapty``, ``billing_cloudpayments``, and any future
payment contour) depend on ``billing_common``; ``billing_common`` depends on none of them. Currently
exposes the two-step user resolution (``resolve_user`` in :mod:`app.billing_common.resolve`) — the
single source of truth that maps an incoming ``customer_user_id`` / ``AccountId`` (a deviceId OR our
userId) to our internal ``userId`` via ``users`` then ``auth_devices``. Centralising it here is the
fix for the recurring ``user_not_found`` incident: the resolve logic lived in one webhook copy and
never propagated to the other (ADR-053 -> ADR-055).
"""
