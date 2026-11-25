# Domain rename: `aquarea` -> `aquarea_smart_cloud` (issue #188)

Status: implemented on this branch (legacy.py, config flow menu, tests/ha/test_legacy.py); release 3.0.0 pending. Branch `rename-domain-aquarea-smart-cloud`.

## Why
heishamon-homeassistant also uses domain/folder `aquarea`; the second one installed overwrites the first.

## Approach
1. Rename folder, `DOMAIN`, manifest, translations, tests, docs, workflows, README (breaking: 3.0.0, bundled release, ask before publishing).
2. One-time migration in the config flow: when the user adds "Aquarea Smart Cloud" and leftover config entries of the old domain `aquarea` exist (storage keeps them even without the integration) whose data has our shape (username + refresh token), import them instead of asking for credentials:
   - create the new entry with the old data and unique id;
   - `EntityRegistry.async_update_entity_platform(entity_id, DOMAIN, new_config_entry_id=..., new_unique_id=...)` keeps entity ids, history, customisations (available in HA 2026.9.4; check the 2026.3 minimum);
   - device registry: swap identifiers `("aquarea", id)` -> `(DOMAIN, id)` and attach the new entry;
   - move `.storage/aquarea.lifetime.<old entry id>` to `aquarea_smart_cloud.lifetime.<new entry id>`;
   - remove the old entry, clear old-domain issues.
3. Tests in `tests/ha/` (test_migrate pattern): full migration, nothing to migrate, interrupted run, foreign (heishamon) entries untouched.

## Open questions
- What HACS does with the old `custom_components/aquarea` folder on update (if it stays, the conflict stays). Verify; the README must say to delete it.
- Entry point when the user has no new entry yet: the add-integration flow (HA does not set up an integration without entries).
- Upstream cjaliaga installs: same path, domain unchanged there.
