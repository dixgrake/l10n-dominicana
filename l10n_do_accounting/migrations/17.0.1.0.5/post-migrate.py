# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging
from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """
    Post-installation fix for l10n_do_accounting
    
    This is NOT a migration script (no structural changes between v15 and v17).
    This script fixes missing l10n_latam_document_type_id on invoices migrated
    from older versions, which causes the NCF field to be invisible.
    
    The issue occurs because:
    1. Old invoices may have l10n_do_fiscal_number but no document_type_id
    2. The field visibility depends on l10n_latam_document_type_id
    3. Without it, users cannot see or edit the NCF field
    """
    _logger.info("=" * 80)
    _logger.info("Running l10n_do_accounting post-installation fix")
    _logger.info("Assigning document types to invoices with missing document_type_id")
    _logger.info("=" * 80)
    
    env = api.Environment(cr, SUPERUSER_ID, {})
    
    # Validate views were recreated correctly
    _validate_views(cr)
    
    # Validate tax group XML IDs
    _validate_tax_groups(cr)
    
    # Check if fix is needed
    if not _needs_fix(cr):
        _logger.info("✓ All invoices have document_type_id - no fix needed")
        return
    
    # Get document types for mapping
    document_types = _get_document_type_mapping(env)
    
    if not document_types:
        _logger.error("❌ No Dominican document types found - cannot proceed")
        return
    
    # Assign document types to invoices
    _assign_document_types(cr, document_types)
    
    # Validate and report
    _validate_and_report(cr)
    
    _logger.info("=" * 80)
    _logger.info("Post-installation fix completed")
    _logger.info("=" * 80)


def _needs_fix(cr):
    """Check if there are invoices without document_type_id"""
    cr.execute("""
        SELECT EXISTS (
            SELECT 1
            FROM account_move am
            JOIN res_company rc ON am.company_id = rc.id
            JOIN res_partner rp ON rc.partner_id = rp.id
            WHERE rp.country_id = (SELECT id FROM res_country WHERE code = 'DO')
            AND am.move_type IN ('out_invoice', 'out_refund', 'in_invoice', 'in_refund')
            AND am.l10n_latam_document_type_id IS NULL
            AND am.state != 'cancel'
            LIMIT 1
        )
    """)
    
    return cr.fetchone()[0]


def _get_document_type_mapping(env):
    """
    Get Dominican document types and create mapping
    
    Returns a dict with keys:
    - By code: 'B01', 'B02', etc.
    - By prefix for NCF detection: 'E31', 'E32', etc.
    """
    _logger.info("Loading Dominican document types...")
    
    # Get all Dominican document types
    doc_types = env['l10n_latam.document.type'].search([
        ('country_id.code', '=', 'DO')
    ])
    
    if not doc_types:
        return {}
    
    mapping = {}
    
    # Create mappings by code
    for doc_type in doc_types:
        # Map by code (B01, B02, etc.)
        if doc_type.code:
            mapping[doc_type.code] = doc_type.id
            _logger.info(f"  Mapped: {doc_type.code} → {doc_type.name} (ID: {doc_type.id})")
        
        # Map by NCF prefix for e-CF (E31, E32, etc.)
        if doc_type.doc_code_prefix:
            mapping[doc_type.doc_code_prefix] = doc_type.id
    
    _logger.info(f"✓ Loaded {len(doc_types)} document types")
    return mapping


def _assign_document_types(cr, document_types):
    """
    Assign document types to invoices based on available information
    
    Strategy:
    1. Try to infer from l10n_do_fiscal_number (NCF prefix)
    2. Use default types by move_type
    3. Consider journal configuration
    """
    _logger.info("Assigning document types to invoices...")
    
    # Get invoices without document_type_id
    cr.execute("""
        SELECT 
            am.id,
            am.move_type,
            am.l10n_do_fiscal_number,
            am.name,
            aj.l10n_latam_use_documents,
            rp.country_id
        FROM account_move am
        JOIN account_journal aj ON am.journal_id = aj.id
        JOIN res_company rc ON am.company_id = rc.id
        JOIN res_partner rp ON rc.partner_id = rp.id
        WHERE rp.country_id = (SELECT id FROM res_country WHERE code = 'DO')
        AND am.move_type IN ('out_invoice', 'out_refund', 'in_invoice', 'in_refund')
        AND am.l10n_latam_document_type_id IS NULL
        AND am.state != 'cancel'
        ORDER BY am.date
    """)
    
    invoices = cr.fetchall()
    total = len(invoices)
    
    if not invoices:
        _logger.info("No invoices need document_type_id assignment")
        return
    
    _logger.info(f"Processing {total} invoices...")
    
    assigned_count = 0
    unassigned_count = 0
    
    for inv_id, move_type, fiscal_number, name, use_documents, country_id in invoices:
        
        doc_type_id = _infer_document_type(
            document_types, 
            move_type, 
            fiscal_number, 
            name
        )
        
        if doc_type_id:
            cr.execute("""
                UPDATE account_move 
                SET l10n_latam_document_type_id = %s
                WHERE id = %s
            """, (doc_type_id, inv_id))
            assigned_count += 1
        else:
            unassigned_count += 1
            _logger.warning(f"  Could not assign document type to invoice ID {inv_id} ({move_type})")
    
    _logger.info(f"✓ Assigned document types: {assigned_count}/{total}")
    
    if unassigned_count > 0:
        _logger.warning(f"⚠ {unassigned_count} invoices could not be assigned automatically")


def _infer_document_type(document_types, move_type, fiscal_number, invoice_name):
    """
    Infer document type from available data
    
    NCF/e-CF codes in Dominican Republic:
    Paper NCF:
    - B01: Tax Credit (Fiscal invoice)
    - B02: Final Consumer
    - B04: Credit Note
    - B14: Income Tax Withholding
    - B15: Special Regime
    - B16: Governmental
    
    Electronic e-CF:
    - E31: Tax Credit Electronic
    - E32: Final Consumer Electronic
    - E33: Debit Note Electronic
    - E34: Credit Note Electronic
    - E41: Purchases Electronic
    - E43: Minor Expenses Electronic
    - E44: Special Regime Electronic
    - E45: Governmental Electronic
    """
    
    # Try to extract NCF/e-CF prefix from fiscal_number
    ncf_code = None
    
    if fiscal_number:
        # Format: E310000000001 or B0100000001
        if len(fiscal_number) >= 3:
            # First 3 characters are usually the prefix
            prefix = fiscal_number[0:3]
            if prefix in document_types:
                return document_types[prefix]
            
            # For paper NCF, try first 3 chars (B01, B02, etc.)
            if prefix[0] in ('B', 'b'):
                ncf_code = prefix.upper()
    
    # Try to extract from invoice name if not found in fiscal_number
    if not ncf_code and invoice_name:
        import re
        # Look for patterns like B01, B02, E31, E32, etc.
        match = re.search(r'[BE]\d{2}', invoice_name.upper())
        if match:
            ncf_code = match.group(0)
    
    # If we found a code, try to map it
    if ncf_code and ncf_code in document_types:
        return document_types[ncf_code]
    
    # Fallback: Use default document types by move_type
    defaults = {
        'out_invoice': 'B01',   # Fiscal invoice (most common)
        'out_refund': 'B04',    # Credit note
        'in_invoice': 'B01',    # Vendor bill
        'in_refund': 'B04',     # Vendor credit note
    }
    
    default_code = defaults.get(move_type)
    
    if default_code and default_code in document_types:
        return document_types[default_code]
    
    # Last resort: get the first document type for invoices or credit notes
    # based on internal_type
    if move_type in ('out_invoice', 'in_invoice'):
        # Look for any invoice type
        for code in ['B01', 'E31', 'B02', 'E32']:
            if code in document_types:
                return document_types[code]
    else:
        # Look for any credit note type
        for code in ['B04', 'E34']:
            if code in document_types:
                return document_types[code]
    
    return None


def _validate_and_report(cr):
    """Validate the fix and generate report"""
    _logger.info("\nValidation Results:")
    _logger.info("-" * 80)
    
    # Count by move_type
    cr.execute("""
        SELECT 
            am.move_type,
            COUNT(*) as total,
            COUNT(am.l10n_latam_document_type_id) as with_doc_type,
            COUNT(*) - COUNT(am.l10n_latam_document_type_id) as without_doc_type
        FROM account_move am
        JOIN res_company rc ON am.company_id = rc.id
        JOIN res_partner rp ON rc.partner_id = rp.id
        WHERE rp.country_id = (SELECT id FROM res_country WHERE code = 'DO')
        AND am.move_type IN ('out_invoice', 'out_refund', 'in_invoice', 'in_refund')
        AND am.state != 'cancel'
        GROUP BY am.move_type
        ORDER BY am.move_type
    """)
    
    results = cr.fetchall()
    
    total_invoices = 0
    total_with_type = 0
    total_without_type = 0
    
    for move_type, total, with_type, without_type in results:
        pct = (with_type / total * 100) if total > 0 else 0
        _logger.info(
            f"{move_type:15} | Total: {total:5} | "
            f"With type: {with_type:5} ({pct:5.1f}%) | "
            f"Missing: {without_type:3}"
        )
        total_invoices += total
        total_with_type += with_type
        total_without_type += without_type
    
    _logger.info("-" * 80)
    
    overall_pct = (total_with_type / total_invoices * 100) if total_invoices > 0 else 0
    _logger.info(
        f"{'TOTAL':15} | Total: {total_invoices:5} | "
        f"With type: {total_with_type:5} ({overall_pct:5.1f}%) | "
        f"Missing: {total_without_type:3}"
    )
    
    if total_without_type > 0:
        _logger.warning(f"\n⚠ {total_without_type} invoices still without document_type_id")
        _logger.warning("These invoices may need manual assignment through the UI")
        
        # Show some examples
        cr.execute("""
            SELECT 
                am.id,
                am.name,
                am.move_type,
                am.l10n_do_fiscal_number,
                am.date
            FROM account_move am
            JOIN res_company rc ON am.company_id = rc.id
            JOIN res_partner rp ON rc.partner_id = rp.id
            WHERE rp.country_id = (SELECT id FROM res_country WHERE code = 'DO')
            AND am.l10n_latam_document_type_id IS NULL
            AND am.state != 'cancel'
            LIMIT 5
        """)
        
        _logger.warning("\nExamples of invoices without document_type_id:")
        for inv_id, name, move_type, ncf, date in cr.fetchall():
            _logger.warning(f"  ID {inv_id}: {name} | {move_type} | NCF: {ncf or 'N/A'} | Date: {date}")
    else:
        _logger.info("\n✓ All invoices now have document_type_id assigned")
        _logger.info("✓ NCF fields should now be visible in invoice forms")

def _validate_views(cr):
    """Validate that all l10n_do_accounting views were recreated correctly."""
    _logger.info("\n" + "=" * 60)
    _logger.info("VALIDATING VIEWS")
    _logger.info("=" * 60)
    
    # Check if views exist and are active
    cr.execute("""
        SELECT COUNT(*) as total,
               COUNT(*) FILTER (WHERE v.active = true) as active,
               COUNT(*) FILTER (WHERE v.active = false) as inactive
        FROM ir_ui_view v
        INNER JOIN ir_model_data d ON d.model = 'ir.ui.view' AND d.res_id = v.id
        WHERE d.module = 'l10n_do_accounting'
    """)
    
    result = cr.fetchone()
    total, active, inactive = result if result else (0, 0, 0)
    
    _logger.info(f"Found {total} views for l10n_do_accounting:")
    _logger.info(f"  - Active: {active}")
    _logger.info(f"  - Inactive: {inactive}")
    
    if inactive > 0:
        _logger.warning(f"⚠️  {inactive} inactive views found!")
        cr.execute("""
            SELECT v.id, v.name, d.name as xml_id
            FROM ir_ui_view v
            INNER JOIN ir_model_data d ON d.model = 'ir.ui.view' AND d.res_id = v.id
            WHERE d.module = 'l10n_do_accounting'
              AND v.active = false
            ORDER BY v.id
        """)
        _logger.warning("Inactive views:")
        for view_id, view_name, xml_id in cr.fetchall():
            _logger.warning(f"  - {xml_id} (ID={view_id}): {view_name}")
    
    # Check critical views exist
    critical_views = [
        'document_tax_totals',
        'report_invoice_document_inherited',
        'custom_header',
        'informations',
    ]
    
    cr.execute("""
        SELECT d.name
        FROM ir_model_data d
        WHERE d.module = 'l10n_do_accounting'
          AND d.model = 'ir.ui.view'
          AND d.name IN %s
    """, (tuple(critical_views),))
    
    found_views = {row[0] for row in cr.fetchall()}
    missing_views = set(critical_views) - found_views
    
    if missing_views:
        _logger.error(f"❌ Critical views missing: {', '.join(missing_views)}")
    else:
        _logger.info("✓ All critical views present")


def _validate_tax_groups(cr):
    """Validate that tax group XML IDs are in the correct format."""
    _logger.info("\n" + "=" * 60)
    _logger.info("VALIDATING TAX GROUP XML IDs")
    _logger.info("=" * 60)
    
    # Check for Dominican companies
    cr.execute("""
        SELECT c.id, c.name
        FROM res_company c
        INNER JOIN res_partner p ON p.id = c.partner_id
        WHERE p.country_id = (SELECT id FROM res_country WHERE code = 'DO')
        ORDER BY c.id
    """)
    
    companies = cr.fetchall()
    
    if not companies:
        _logger.warning("⚠️  No Dominican companies found")
        return
    
    _logger.info(f"Found {len(companies)} Dominican company(ies)")
    
    all_valid = True
    
    for company_id, company_name in companies:
        _logger.info(f"\nValidating company: {company_name} (ID={company_id})")
        
        # Check for required tax groups
        for group_suffix in ['itbis', 'isr']:
            expected_xmlid = f"{company_id}_tax_group_{group_suffix}"
            
            cr.execute("""
                SELECT d.res_id, tg.name
                FROM ir_model_data d
                INNER JOIN account_tax_group tg ON tg.id = d.res_id
                WHERE d.module = 'account'
                  AND d.model = 'account.tax.group'
                  AND d.name = %s
            """, (expected_xmlid,))
            
            result = cr.fetchone()
            
            if result:
                res_id, tg_name = result
                _logger.info(
                    f"  ✓ account.{expected_xmlid} → "
                    f"{tg_name.get('en_US') or tg_name.get('es')} (ID={res_id})"
                )
            else:
                _logger.error(f"  ❌ Missing: account.{expected_xmlid}")
                all_valid = False
    
    if all_valid:
        _logger.info("\n✓ All tax group XML IDs are valid")
    else:
        _logger.error("\n❌ Some tax group XML IDs are missing or invalid")
        _logger.error("Invoice printing may fail!")