# Migración de la localización desde Odoo 11/12

El servicio oficial de Odoo (upgrade.odoo.com) migra los módulos de Odoo S.A.,
pero no los de esta localización. Después de recibir la base de datos en 17.0,
la localización se migra así:

| v11/v12                | v17                                  |
|------------------------|--------------------------------------|
| `ncf_manager`          | `l10n_do_accounting` (renombrado)    |
| `ncf_invoice_template` | fusionado en `l10n_do_accounting`    |
| `dgii_reports`         | `dgii_reports` (repo l10n-do-addons) |

## Pasos

Hacer un respaldo de la base de datos antes de empezar.

1. Renombrar los módulos v12 (idempotente):

   ```bash
   psql -d <db> -v ON_ERROR_STOP=1 -f scripts/migrate_from_v12/00_rename_v12_modules.sql
   ```

   Con docker: `docker exec -i <contenedor_db> psql -U odoo -d <db> -v ON_ERROR_STOP=1 < scripts/migrate_from_v12/00_rename_v12_modules.sql`

2. Actualizar la localización (instala `l10n_latam_invoice_document` y
   actualiza también `dgii_reports` si está en el addons path):

   ```bash
   odoo -d <db> -u l10n_do_accounting --stop-after-init
   ```

El renombrado hace que Odoo trate `l10n_do_accounting` como una actualización
desde 12.0.x, por lo que se ejecutan
`l10n_do_accounting/migrations/17.0.1.0.11/` y se conservan los grupos de
seguridad con sus usuarios. Sin este paso, `l10n_do_accounting` se instalaría
desde cero y los datos v12 no se migrarían.

## Qué se migra

- `account_invoice.reference` (NCF) → `account.move.l10n_do_fiscal_number`, y
  el tipo de documento (`l10n_latam_document_type_id`) según el prefijo del NCF.
- `expense_type`, `income_type`, `anulation_type`, `origin_out`,
  `ncf_expiration_date` → campos `l10n_do_*` de `account.move`.
- `res.partner.sale_fiscal_type` → `l10n_do_dgii_tax_payer_type`,
  `expense_type` → `l10n_do_expense_type`.
- `account.journal.payment_form` → `l10n_do_payment_form`; los diarios con NCF
  pasan a usar documentos (`l10n_latam_use_documents`) y se les crean sus tipos
  de documento con la fecha de vencimiento v12 si sigue vigente.
- xmlids de grupos de impuestos del plan v12 (`<cid>_group_itbis`, ...) → los
  nombres de la plantilla v17 (`<cid>_tax_group_itbis`, ...).
- Se eliminan las vistas, el cron y los parámetros de `ncf_manager`.

La numeración continúa desde el último NCF migrado de cada tipo.

## Después de migrar

- Revisar en los diarios las fechas de vencimiento de los tipos de documento:
  las fechas v12 vencidas no se copian (se deja el valor por defecto y se
  registra un warning en el log).
- La tabla `account_invoice` se conserva como referencia; se puede eliminar
  cuando la migración esté validada.
