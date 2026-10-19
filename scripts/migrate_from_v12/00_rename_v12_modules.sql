-- Rename the Dominican localization modules of Odoo 11/12 to their Odoo 17 names.
--
-- Run it ONCE on the database returned by the official Odoo upgrade service
-- (upgrade.odoo.com), BEFORE updating the localization:
--
--   psql -d <db> -v ON_ERROR_STOP=1 -f 00_rename_v12_modules.sql
--   odoo -d <db> -u l10n_do_accounting --stop-after-init
--
-- Odoo upgrade only migrates Odoo S.A. modules: ncf_manager keeps its 11.0/12.0
-- version and l10n_do_accounting does not exist in the database. Installing
-- l10n_do_accounting would be a fresh install, which skips its migration
-- scripts. Renaming ncf_manager to l10n_do_accounting turns it into an upgrade
-- from 12.0.x, so l10n_do_accounting/migrations/17.0.1.0.11 runs and the
-- existing xmlids (security groups and their users) are kept.
--
-- ncf_invoice_template is merged into l10n_do_accounting (it provides the
-- invoice report). Its obsolete records are removed by Odoo at the end of the
-- update, as any xmlid of l10n_do_accounting that is no longer loaded.
--
-- The script is idempotent: it does nothing if ncf_manager is not installed.

DO $$
DECLARE
    src_id integer;
    old_id integer;
    old_state varchar;
    merged varchar;
BEGIN
    SELECT id INTO src_id
      FROM ir_module_module
     WHERE name = 'ncf_manager'
       AND state IN ('installed', 'to upgrade', 'to remove');

    IF src_id IS NULL THEN
        RAISE NOTICE 'ncf_manager is not installed: nothing to rename';
        RETURN;
    END IF;

    -- A module list update may already have created l10n_do_accounting as
    -- 'uninstalled': drop that record so ncf_manager can take its name.
    SELECT id, state INTO old_id, old_state
      FROM ir_module_module
     WHERE name = 'l10n_do_accounting';

    IF old_id IS NOT NULL THEN
        IF old_state NOT IN ('uninstalled', 'uninstallable') THEN
            RAISE EXCEPTION 'l10n_do_accounting is already % in this database, '
                            'the v12 data cannot be migrated by renaming ncf_manager',
                            old_state;
        END IF;
        DELETE FROM ir_module_module_dependency WHERE module_id = old_id;
        DELETE FROM ir_module_module_exclusion WHERE module_id = old_id;
        DELETE FROM ir_model_data WHERE model = 'ir.module.module' AND res_id = old_id;
        DELETE FROM ir_module_module WHERE id = old_id;
    END IF;

    -- 'installed' (not 'to upgrade') so that "-u l10n_do_accounting" goes
    -- through button_upgrade, which installs the new dependencies
    -- (l10n_latam_invoice_document) before migrating the module.
    UPDATE ir_module_module
       SET name = 'l10n_do_accounting', state = 'installed'
     WHERE id = src_id;

    UPDATE ir_model_data
       SET name = 'module_l10n_do_accounting'
     WHERE module = 'base' AND name = 'module_ncf_manager';

    UPDATE ir_module_module_dependency
       SET name = 'l10n_do_accounting'
     WHERE name = 'ncf_manager';

    -- Move the xmlids of ncf_manager and of the merged modules to
    -- l10n_do_accounting, dropping the ones whose name already exists there.
    FOREACH merged IN ARRAY ARRAY['ncf_manager', 'ncf_invoice_template']
    LOOP
        DELETE FROM ir_model_data src
         USING ir_model_data dst
         WHERE src.module = merged
           AND dst.module = 'l10n_do_accounting'
           AND dst.name = src.name;

        UPDATE ir_model_data
           SET module = 'l10n_do_accounting'
         WHERE module = merged;
    END LOOP;

    UPDATE ir_module_module
       SET state = 'uninstalled', latest_version = NULL
     WHERE name = 'ncf_invoice_template'
       AND state IN ('installed', 'to upgrade', 'to remove');

    UPDATE ir_module_module_dependency
       SET name = 'l10n_do_accounting'
     WHERE name = 'ncf_invoice_template';

    RAISE NOTICE 'ncf_manager renamed to l10n_do_accounting (module id %)', src_id;
END
$$;
