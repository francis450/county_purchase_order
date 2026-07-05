# Copyright (c) 2026, ERP Kenya and contributors
# For license information, please see license.txt

import json
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import flt

from county_cables_po.tasks import get_summary_numbers

VIEW_STATUS_FILTERS = {
	"Action": ["Critical", "Reorder Now", "Watch"],
	"Dormant Stock": ["Dormant Stock"],
	"Dead SKU": ["Dead SKU"],
	"All": None,
}


def execute(filters=None):
	filters = frappe._dict(filters or {})
	view = filters.get("view") or "Action"
	show_diagnostics = filters.get("show_diagnostics")

	rows = get_data(filters, view)
	month_labels = get_month_labels(rows)
	columns = get_columns(month_labels, show_diagnostics)
	data = build_rows(rows, month_labels, view)
	report_summary = get_report_summary()

	return columns, data, None, None, report_summary


def get_month_labels(rows):
	for row in rows:
		if row.monthly_demand_breakdown:
			try:
				breakdown = json.loads(row.monthly_demand_breakdown)
			except ValueError:
				continue
			return [b["month"] for b in breakdown]
	return []


def get_columns(month_labels, show_diagnostics):
	# Ordered so the numbers the client actually acts on - demand, current
	# stock, how long it lasts, and what to order - come first. Everything
	# else (money, trend, diagnostics) is secondary and pushed later.
	columns = [
		{"fieldname": "item_code", "label": _("Item"), "fieldtype": "Link", "options": "Item", "width": 160},
		{"fieldname": "item_name", "label": _("Item Name"), "fieldtype": "Data", "width": 220},
		{"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 140},
	]
	for ym in month_labels:
		columns.append({"fieldname": _bucket_fieldname(ym), "label": ym, "fieldtype": "Data", "width": 100})
	columns += [
		{"fieldname": "median_monthly_demand", "label": _("Median Monthly Demand"), "fieldtype": "Float", "width": 190},
		{"fieldname": "actual_qty", "label": _("Actual Qty"), "fieldtype": "Float", "width": 120},
		{"fieldname": "projected_qty", "label": _("Projected Qty"), "fieldtype": "Float", "width": 140},
		{"fieldname": "months_of_stock_remaining", "label": _("Months of Stock Remaining"), "fieldtype": "Data", "width": 210},
		{"fieldname": "projected_stockout_date", "label": _("Projected Stockout Date"), "fieldtype": "Date", "width": 190},
		{"fieldname": "order_by_date", "label": _("Order By Date"), "fieldtype": "Date", "width": 150},
		{"fieldname": "suggested_order_qty", "label": _("Suggested Order Qty"), "fieldtype": "Float", "width": 180},
		{"fieldname": "suggested_order_value", "label": _("Suggested Order Value"), "fieldtype": "Currency", "width": 190},
		# --- secondary: not what the client scans first, but still useful ---
		{"fieldname": "default_supplier", "label": _("Supplier"), "fieldtype": "Link", "options": "Supplier", "width": 150},
		{"fieldname": "trend", "label": _("Trend"), "fieldtype": "Data", "width": 100},
		{"fieldname": "reorder_level", "label": _("Reorder Level"), "fieldtype": "Float", "width": 150},
		{"fieldname": "anomaly_flag", "label": _("Anomaly"), "fieldtype": "Check", "width": 110},
		{"fieldname": "large_order_flag", "label": _("Large Order"), "fieldtype": "Check", "width": 130},
		{"fieldname": "rate_missing_flag", "label": _("Rate Missing"), "fieldtype": "Check", "width": 130},
		{"fieldname": "monthly_revenue_at_risk", "label": _("Monthly Revenue at Risk"), "fieldtype": "Currency", "width": 190},
		{"fieldname": "locked_capital", "label": _("Locked Capital"), "fieldtype": "Currency", "width": 160},
		{"fieldname": "last_sale_date", "label": _("Last Sale Date"), "fieldtype": "Date", "width": 150},
		{"fieldname": "last_receipt_date", "label": _("Last Receipt Date"), "fieldtype": "Date", "width": 160},
	]
	if show_diagnostics:
		columns += [
			{"fieldname": "corroboration_flag", "label": _("Corroboration Flag"), "fieldtype": "Check", "width": 160},
			{"fieldname": "stale_open_po_flag", "label": _("Stale Open PO"), "fieldtype": "Check", "width": 150},
			{"fieldname": "lead_time_months_used", "label": _("Lead Time (Months)"), "fieldtype": "Float", "width": 170},
			{"fieldname": "lead_time_source", "label": _("Lead Time Source"), "fieldtype": "Data", "width": 160},
			{"fieldname": "item_group", "label": _("Item Group"), "fieldtype": "Link", "options": "Item Group", "width": 160},
			{"fieldname": "last_calculated_display", "label": _("Last Calculated"), "fieldtype": "Data", "width": 180},
		]
	return columns


def _bucket_fieldname(ym):
	return "bucket_" + ym.replace("-", "_")


def get_data(filters, view):
	conditions = []
	values = {}

	statuses = VIEW_STATUS_FILTERS.get(view)
	if filters.get("status"):
		statuses = [filters.status]
	if statuses:
		conditions.append("rr.status in %(statuses)s")
		values["statuses"] = statuses

	if filters.get("item_group"):
		conditions.append("rr.item_group = %(item_group)s")
		values["item_group"] = filters.item_group

	if filters.get("supplier"):
		conditions.append("rr.default_supplier = %(supplier)s")
		values["supplier"] = filters.supplier

	where = ("where " + " and ".join(conditions)) if conditions else ""

	order_by = "rr.order_by_date asc"
	if view == "Dormant Stock":
		order_by = "rr.locked_capital desc"
	elif view == "Dead SKU":
		order_by = "rr.actual_qty desc"

	return frappe.db.sql(
		f"""
		select rr.*
		from `tabReorder Recommendation` rr
		{where}
		order by {order_by}
		""",
		values,
		as_dict=True,
	)


def build_rows(rows, month_labels, view):
	supplier_groups = defaultdict(list)
	ordered_records = []
	for row in rows:
		record = _row_to_record(row, month_labels)
		ordered_records.append(record)
		supplier_groups[row.default_supplier or "No Supplier"].append(record)

	if view == "Action" and ordered_records:
		return _add_supplier_subtotals(supplier_groups)
	return ordered_records


def _format_months_remaining(months):
	"""'1.333' months means nothing to a buyer - show it as months/days instead.
	30-day months, consistent with how projected_stockout_date is computed."""
	if months is None:
		return ""
	total_days = round(flt(months) * 30)
	if total_days <= 0:
		return "0 days"
	months_part, days_part = divmod(total_days, 30)
	parts = []
	if months_part:
		parts.append(f"{months_part} month" + ("s" if months_part != 1 else ""))
	if days_part or not months_part:
		parts.append(f"{days_part} day" + ("s" if days_part != 1 else ""))
	return " ".join(parts)


def _row_to_record(row, month_labels):
	breakdown_by_month = {}
	if row.monthly_demand_breakdown:
		try:
			breakdown_by_month = {b["month"]: b for b in json.loads(row.monthly_demand_breakdown)}
		except ValueError:
			breakdown_by_month = {}

	record = dict(row)
	record["last_calculated_display"] = (
		row.last_calculated.strftime("%Y-%m-%d %H:%M") if row.last_calculated else ""
	)
	record["months_of_stock_remaining"] = (
		_format_months_remaining(row.months_of_stock_remaining) if row.median_monthly_demand else ""
	)

	for ym in month_labels:
		bucket = breakdown_by_month.get(ym)
		key = _bucket_fieldname(ym)
		if not bucket:
			record[key] = ""
			continue
		qty = flt(bucket["qty"])
		qty_str = f"{qty:g}"
		if bucket.get("excluded"):
			qty_str = f"[{qty_str}]"
		elif bucket.get("clipped"):
			qty_str = f"{qty_str}*"
		record[key] = qty_str
	return record


def _add_supplier_subtotals(supplier_groups):
	"""Group-subtotal suggested_order_value by default supplier, plus a grand
	total row - CBM/weight aggregation deferred until item weights exist."""
	ordered = []
	grand_total = 0.0
	grand_count = 0
	for supplier, records in supplier_groups.items():
		ordered.extend(records)
		subtotal = sum(flt(r.get("suggested_order_value")) for r in records)
		grand_total += subtotal
		grand_count += len(records)
		ordered.append(
			{
				"item_code": "",
				"item_name": f"Subtotal - {supplier}",
				"suggested_order_value": subtotal,
			}
		)
	ordered.append(
		{
			"item_code": "",
			"item_name": f"Total suggested this cycle: KES {grand_total:,.0f} across {grand_count} item(s).",
			"suggested_order_value": grand_total,
		}
	)
	return ordered


def get_report_summary():
	s = get_summary_numbers()
	return [
		{"value": s["critical_count"], "label": _("Critical"), "datatype": "Int", "indicator": "Red"},
		{"value": s["reorder_now_count"], "label": _("Reorder Now"), "datatype": "Int", "indicator": "Orange"},
	]
