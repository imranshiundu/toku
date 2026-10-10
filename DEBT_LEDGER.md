# Debt Ledger

Known problems, ordered by severity. Burn items down proactively; never walk past a defect.

- [Medium] Remote writers (Supabase REST, PostgreSQL) are unit-tested only at the payload/serialization level — never integration-tested against a live server. Action: point toku at a real Supabase project and verify one full push cycle.
- [Medium] opencode v2 storage schema is unverified — toku reads the v1 database (verified on opencode 1.18.30). Action: verify the v2 database schema and the `opencode db path` fallback before v2 becomes the default install.
- [Low] `total_tokens` includes `cache_write`; opencode's own `total` field matched input+output+reasoning+cache_read on all inspected samples (cache_write was always 0, so inclusion is unverified against opencode's math).
- [Low] `requests` counts `step-finish` parts (one per model call), which is a different grain than opencode's `stats` "messages" (it counted 28,540 vs 27,040 step-finishes on the same data). Documented, not reconciled.
- [Low] Labels for the `global` project (sessions started outside a git repo) show as "global"; no directory-level breakdown for them yet.
