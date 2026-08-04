"""Pure domain model.

Nothing in this package performs I/O. No Playwright, no HTTP, no model calls. That
keeps the snapshot representation, ref resolution, staleness rules and safety
classification deterministically testable without a browser or a model, and it is
where the rules that must not drift live.
"""
