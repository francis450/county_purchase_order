"""
Server-side recalculation for China Import Purchase Orders.

Runs in before_validate so that ERPNext's own calculate_taxes_and_totals()
picks up the correct qty and rate when it runs during validate.
"""

from frappe.utils import flt, rounded


def before_validate(doc, method=None):
    """Recalculate every China-import costing field from the raw inputs."""
    factor      = flt(doc.custom_factor)
    freight_rate = flt(doc.custom_freight_rate)

    total_boxes       = 0
    total_pcs         = 0
    total_cost_rmb    = 0.0
    total_cost_kes    = 0.0
    total_cbm         = 0.0
    total_freight     = 0.0
    total_proj_profit = 0.0

    for item in doc.items:
        boxes     = flt(item.custom_no_of_boxes)
        pcs_box   = flt(item.custom_pcs_per_box)
        price_rmb = flt(item.custom_price_rmb)
        cbm_box   = flt(item.custom_cbm_per_box)
        proj_price = flt(item.custom_projected_price)

        # Skip rows with no input data
        if not boxes or not pcs_box:
            continue

        # ── Quantities ───────────────────────────────────────────────────────
        row_total_pcs = boxes * pcs_box

        # ── Cost ─────────────────────────────────────────────────────────────
        row_total_cost_rmb = row_total_pcs * price_rmb
        row_total_cost_kes = row_total_cost_rmb * factor
        row_cost_per_item  = row_total_cost_kes / row_total_pcs if row_total_pcs else 0.0

        # ── Freight ──────────────────────────────────────────────────────────
        row_total_cbm     = boxes * cbm_box
        row_total_freight = row_total_cbm * freight_rate
        row_freight_item  = row_total_freight / row_total_pcs if row_total_pcs else 0.0

        # ── Unit economics (round each component first — matches Excel cell-by-cell behaviour)
        row_cost_per_item_r  = round(row_cost_per_item, 0)
        row_freight_item_r   = round(row_freight_item, 0)
        row_unit_cost        = row_cost_per_item_r + row_freight_item_r   # integer sum
        row_proj_profit_unit = proj_price - row_unit_cost                  # integer diff
        row_total_proj_profit = row_total_pcs * row_proj_profit_unit       # integer product

        # ── Push to ERPNext native fields (used by calculate_taxes_and_totals)
        item.qty  = row_total_pcs
        item.rate = round(row_unit_cost, 2)

        # ── Push to custom calculated fields ─────────────────────────────────
        item.custom_total_pcs_row    = row_total_pcs
        item.custom_total_cost_rmb   = round(row_total_cost_rmb, 2)
        item.custom_total_cost_kes   = round(row_total_cost_kes, 0)
        item.custom_cost_per_item    = row_cost_per_item_r
        item.custom_total_cbm        = round(row_total_cbm, 3)
        item.custom_total_freight    = round(row_total_freight, 0)
        item.custom_freight_per_item = row_freight_item_r
        item.custom_unit_cost        = row_unit_cost
        item.custom_proj_profit_unit = row_proj_profit_unit
        item.custom_total_proj_profit = row_total_proj_profit

        # ── Accumulate PO-level totals ────────────────────────────────────────
        total_boxes       += boxes
        total_pcs         += row_total_pcs
        total_cost_rmb    += row_total_cost_rmb
        total_cost_kes    += row_total_cost_kes
        total_cbm         += row_total_cbm
        total_freight     += row_total_freight
        total_proj_profit += row_total_proj_profit

    # ── Set PO header totals ─────────────────────────────────────────────────
    doc.custom_total_boxes          = total_boxes
    doc.custom_total_pcs            = total_pcs
    doc.custom_total_cost_rmb_po    = round(total_cost_rmb, 2)
    doc.custom_total_cost_kes_po    = round(total_cost_kes, 0)
    doc.custom_total_cbm_po         = round(total_cbm, 3)
    doc.custom_total_freight_po     = round(total_freight, 0)
    doc.custom_total_proj_profit_po = round(total_proj_profit, 0)
