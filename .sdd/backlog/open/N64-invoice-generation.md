# N64 — Invoice Generation

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
Internal chargeback requires formatted invoices, but teams must manually compile usage data into spreadsheets — a tedious, error-prone process repeated every billing cycle.

## Solution
- Implement `bernstein invoice --month 2026-03 --output invoice.pdf`
- Generate a PDF invoice using `reportlab` or `weasyprint`
- Invoice shows line items: tasks executed, models used, tokens consumed, and cost per line item
- Include summary totals, date range, and workspace/team metadata
- Support `--format pdf|html|csv` for flexibility

## Acceptance
- [ ] `bernstein invoice --month YYYY-MM` generates an invoice for the specified month
- [ ] PDF output includes task, model, token, and cost line items
- [ ] Invoice includes summary totals and date range header
- [ ] `--output` flag specifies the output file path
- [ ] `reportlab` or `weasyprint` is used for PDF generation
- [ ] Workspace and team tags appear on the invoice when available
