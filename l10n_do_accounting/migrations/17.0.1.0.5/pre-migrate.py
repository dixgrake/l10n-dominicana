"""Pre-migration script for l10n_do_accounting from v15 to v17.

This script handles:
1. Deletion of obsolete v15 views that will be recreated from XML
2. Correction of tax group XML IDs format (tax_group_* prefix)
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Execute pre-migration tasks."""
    _logger.info("Starting pre-migration for l10n_do_accounting v17")
    
    # Step 1: Delete obsolete v15 views
    _delete_obsolete_views(cr)
    
    # Step 2: Fix tax group XML IDs
    _fix_tax_group_xmlids(cr)
    
    _logger.info("Pre-migration for l10n_do_accounting v17 completed")


def _delete_obsolete_views(cr):
    """Delete all views from l10n_do_accounting to force recreation from v17 XML files.
    
    This ensures that any changes in view structure between v15 and v17 are properly applied.
    """
    _logger.info("Deleting obsolete l10n_do_accounting views...")
    
    # Get all view IDs from the module
    cr.execute("""
        SELECT v.id, v.name, d.name as xml_id
        FROM ir_ui_view v
        INNER JOIN ir_model_data d ON d.model = 'ir.ui.view' AND d.res_id = v.id
        WHERE d.module = 'l10n_do_accounting'
        ORDER BY v.id
    """)
    
    views = cr.fetchall()
    if views:
        view_ids = [v[0] for v in views]
        _logger.info(
            f"Found {len(views)} views to delete: {', '.join(v[2] for v in views)}"
        )
        
        # Find and delete all child views that inherit from these views (including from other modules)
        cr.execute("""
            SELECT v.id, v.name, d.module, d.name as xml_id
            FROM ir_ui_view v
            INNER JOIN ir_model_data d ON d.model = 'ir.ui.view' AND d.res_id = v.id
            WHERE v.inherit_id IN %s
        """, (tuple(view_ids),))
        
        child_views = cr.fetchall()
        if child_views:
            child_view_ids = [v[0] for v in child_views]
            _logger.info(
                f"Found {len(child_views)} child views to delete: "
                f"{', '.join(f'{v[2]}.{v[3]}' for v in child_views)}"
            )
            
            # Delete ir_model_data for child views
            cr.execute("""
                DELETE FROM ir_model_data 
                WHERE model = 'ir.ui.view' AND res_id IN %s
            """, (tuple(child_view_ids),))
            
            # Delete child views
            cr.execute("""
                DELETE FROM ir_ui_view 
                WHERE id IN %s
            """, (tuple(child_view_ids),))
            _logger.info(f"Deleted {len(child_views)} child views")
        
        # Delete ir_model_data entries for l10n_do_accounting views
        cr.execute("""
            DELETE FROM ir_model_data 
            WHERE module = 'l10n_do_accounting' AND model = 'ir.ui.view'
        """)
        deleted_data = cr.rowcount
        
        # Delete l10n_do_accounting views
        cr.execute("""
            DELETE FROM ir_ui_view 
            WHERE id IN %s
        """, (tuple(view_ids),))
        deleted_views = cr.rowcount
        
        _logger.info(
            f"Deleted {deleted_data} ir_model_data entries and {deleted_views} views"
        )
    else:
        _logger.info("No obsolete views found")


def _fix_tax_group_xmlids(cr):
    """Fix tax group XML IDs to match v17 format.
    
    In v15, tax groups might have been created without 'tax_' prefix.
    In v17, the format is: account.{company_id}_tax_group_{itbis|isr}
    
    This function ensures the correct XML IDs exist.
    """
    _logger.info("Fixing tax group XML IDs...")
    
    # Check if incorrect XML IDs exist (without tax_ prefix)
    cr.execute("""
        SELECT name, res_id 
        FROM ir_model_data 
        WHERE module = 'account' 
          AND model = 'account.tax.group'
          AND name ~ '^[0-9]+_group_(itbis|isr)$'
        ORDER BY name
    """)
    
    incorrect_xmlids = cr.fetchall()
    
    if incorrect_xmlids:
        _logger.info(f"Found {len(incorrect_xmlids)} incorrect tax group XML IDs")
        
        for xmlid_name, res_id in incorrect_xmlids:
            # Parse company_id and group name from old format
            # Format: {company_id}_group_{itbis|isr}
            parts = xmlid_name.split('_group_')
            if len(parts) == 2:
                company_id = parts[0]
                group_name = parts[1]
                
                # New format: {company_id}_tax_group_{itbis|isr}
                new_xmlid_name = f"{company_id}_tax_group_{group_name}"
                
                _logger.info(
                    f"Renaming XML ID: {xmlid_name} -> {new_xmlid_name} "
                    f"(res_id={res_id})"
                )
                
                # Update the XML ID name
                cr.execute("""
                    UPDATE ir_model_data 
                    SET name = %s
                    WHERE module = 'account' 
                      AND model = 'account.tax.group'
                      AND name = %s
                """, (new_xmlid_name, xmlid_name))
    else:
        _logger.info("No incorrect tax group XML IDs found")
    
    # Verify all required tax groups have correct XML IDs
    _verify_tax_group_xmlids(cr)


def _verify_tax_group_xmlids(cr):
    """Verify that all tax groups have the correct XML ID format."""
    _logger.info("Verifying tax group XML IDs...")
    
    # Get all tax groups for Dominican companies
    cr.execute("""
        SELECT 
            tg.id,
            tg.name->>'en_US' as name,
            c.id as company_id,
            c.name as company_name
        FROM account_tax_group tg
        CROSS JOIN res_company c
        INNER JOIN res_partner p ON p.id = c.partner_id
        WHERE p.country_id = (SELECT id FROM res_country WHERE code = 'DO')
          AND (tg.name->>'en_US' IN ('ITBIS', 'ISR') 
               OR tg.name->>'es' IN ('ITBIS', 'ISR'))
        ORDER BY c.id, tg.name
    """)
    
    tax_groups = cr.fetchall()
    
    for tg_id, tg_name, company_id, company_name in tax_groups:
        # Determine the expected XML ID suffix
        if 'ITBIS' in (tg_name or ''):
            suffix = 'itbis'
        elif 'ISR' in (tg_name or ''):
            suffix = 'isr'
        else:
            continue
        
        expected_xmlid = f"{company_id}_tax_group_{suffix}"
        
        # Check if XML ID exists
        cr.execute("""
            SELECT id 
            FROM ir_model_data 
            WHERE module = 'account' 
              AND model = 'account.tax.group'
              AND name = %s
              AND res_id = %s
        """, (expected_xmlid, tg_id))
        
        if not cr.fetchone():
            _logger.warning(
                f"Missing XML ID for tax group {tg_name} (ID={tg_id}) "
                f"in company {company_name} (ID={company_id}). Creating: {expected_xmlid}"
            )
            
            # Create the missing XML ID
            cr.execute("""
                INSERT INTO ir_model_data (name, module, model, res_id, noupdate)
                VALUES (%s, 'account', 'account.tax.group', %s, false)
                ON CONFLICT (module, name) DO NOTHING
            """, (expected_xmlid, tg_id))
            
            if cr.rowcount > 0:
                _logger.info(f"Created XML ID: account.{expected_xmlid}")
