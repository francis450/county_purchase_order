import calendar
from datetime import date

import frappe
from frappe import _
from frappe.utils import fmt_money, flt


def execute(filters=None):
	filters = filters or {}
	_set_defaults(filters)

	report_type = filters.get("report_type", "Summary")

	if report_type == "Detail":
		columns = _detail_columns()
		data = _get_detail_data(filters)
		message = _build_kpi_html(filters)
	else:
		columns = _summary_columns(filters)
		data = _get_summary_data(filters)
		message = _build_kpi_html(filters)

	return columns, data, message


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def _set_defaults(filters):
	if not filters.get("from_date"):
		filters["from_date"] = frappe.utils.get_first_day(frappe.utils.nowdate())
	if not filters.get("to_date"):
		filters["to_date"] = frappe.utils.nowdate()
	if not filters.get("company"):
		filters["company"] = frappe.defaults.get_user_default("Company") or "County Cables"


# ---------------------------------------------------------------------------
# WHERE clause builder
# ---------------------------------------------------------------------------

def _base_conditions(filters):
	conds = [
		"si.docstatus = 1",
		"si.posting_date BETWEEN %(from_date)s AND %(to_date)s",
		"si.company = %(company)s",
	]
	if filters.get("customer"):
		conds.append("si.customer = %(customer)s")
	if filters.get("warehouse"):
		conds.append("sii.warehouse = %(warehouse)s")
	if filters.get("item_code"):
		conds.append("sii.item_code = %(item_code)s")
	if filters.get("item_group"):
		conds.append("i.item_group = %(item_group)s")
	return " AND ".join(conds)


def _item_join(filters):
	"""Only JOIN tabItem when item_group filter is active."""
	if filters.get("item_group"):
		return "JOIN `tabItem` i ON i.name = sii.item_code"
	return "LEFT JOIN `tabItem` i ON i.name = sii.item_code"


# ---------------------------------------------------------------------------
# Summary mode — period grouping
# ---------------------------------------------------------------------------

def _period_expr(filters):
	group_by = filters.get("group_by", "Month")
	if group_by == "Day":
		return "DATE(si.posting_date)", "DATE(si.posting_date)"
	if group_by == "Week":
		return "YEARWEEK(si.posting_date, 3)", "MIN(DATE(si.posting_date))"
	# Month (default)
	return "DATE_FORMAT(si.posting_date, '%%Y-%%m')", "DATE_FORMAT(si.posting_date, '%%Y-%%m')"


def _summary_columns(filters):
	group_by = filters.get("group_by", "Month")
	period_label = {"Day": "Date", "Week": "Week", "Month": "Month"}.get(group_by, "Month")
	return [
		{"label": _(period_label), "fieldname": "period", "fieldtype": "Data", "width": 110},
		{"label": _("Invoices"), "fieldname": "invoices", "fieldtype": "Int", "width": 90},
		{"label": _("Qty Sold"), "fieldname": "total_qty", "fieldtype": "Float", "width": 100},
		{"label": _("Total Sales (KES)"), "fieldname": "revenue", "fieldtype": "Currency", "width": 150},
		{"label": _("Total Cost"), "fieldname": "cogs", "fieldtype": "Currency", "width": 150},
		{"label": _("Gross Profit (KES)"), "fieldname": "gross_profit", "fieldtype": "Currency", "width": 160},
		{"label": _("GP %"), "fieldname": "gp_pct", "fieldtype": "Percent", "width": 90},
	]


def _period_dates(period, group_by):
	"""Return (from_date, to_date) strings for a period row."""
	period = str(period)
	if group_by == "Day":
		return period, period
	if group_by == "Week":
		year, week = int(period[:4]), int(period[4:])
		first = date.fromisocalendar(year, week, 1)
		last = date.fromisocalendar(year, week, 7)
		return str(first), str(last)
	# Month: period = "YYYY-MM"
	year, month = int(period[:4]), int(period[5:7])
	first = date(year, month, 1)
	last = date(year, month, calendar.monthrange(year, month)[1])
	return str(first), str(last)


def _get_summary_data(filters):
	group_expr, _period_select = _period_expr(filters)
	item_join = _item_join(filters)
	conds = _base_conditions(filters)

	rows = frappe.db.sql(
		f"""
		SELECT
			{group_expr} AS period,
			COUNT(DISTINCT si.name) AS invoices,
			SUM(sii.qty) AS total_qty,
			SUM(sii.net_amount) AS revenue,
			SUM(sii.qty * sii.incoming_rate) AS cogs,
			SUM(sii.net_amount) - SUM(sii.qty * sii.incoming_rate) AS gross_profit
		FROM `tabSales Invoice` si
		JOIN `tabSales Invoice Item` sii ON sii.parent = si.name
		{item_join}
		WHERE {conds}
		GROUP BY {group_expr}
		ORDER BY {group_expr}
		""",
		filters,
		as_dict=True,
	)

	group_by = filters.get("group_by", "Month")

	# Compute GP% and period date range for drill-down links
	for r in rows:
		r["gp_pct"] = flt(r["gross_profit"]) / flt(r["revenue"]) * 100 if r.get("revenue") else 0
		pf, pt = _period_dates(r["period"], group_by)
		r["period_from"] = pf
		r["period_to"] = pt

	# Totals row
	if rows:
		t_rev = sum(flt(r["revenue"]) for r in rows)
		t_cogs = sum(flt(r["cogs"]) for r in rows)
		t_gp = t_rev - t_cogs
		t_inv = sum(int(r.get("invoices") or 0) for r in rows)
		t_qty = sum(flt(r.get("total_qty")) for r in rows)
		rows.append({
			"period": _("TOTAL"),
			"invoices": t_inv,
			"total_qty": t_qty,
			"revenue": t_rev,
			"cogs": t_cogs,
			"gross_profit": t_gp,
			"gp_pct": t_gp / t_rev * 100 if t_rev else 0,
			"period_from": filters.get("from_date"),
			"period_to": filters.get("to_date"),
			"bold": 1,
		})

	return rows


# ---------------------------------------------------------------------------
# Detail mode — one row per item, aggregated over the period
# ---------------------------------------------------------------------------

def _detail_columns():
	return [
		{"label": _("Item Code"), "fieldname": "item_code", "fieldtype": "Link", "options": "Item", "width": 160},
		{"label": _("Item Name"), "fieldname": "item_name", "fieldtype": "Data", "width": 220},

		{"label": _("Transactions"), "fieldname": "txn_count", "fieldtype": "Int", "width": 110},
		{"label": _("Qty Sold"), "fieldname": "total_qty", "fieldtype": "Float", "width": 100},
		{"label": _("Avg Selling Price"), "fieldname": "avg_sell_rate", "fieldtype": "Currency", "width": 160},
		{"label": _("Total Sales (KES)"), "fieldname": "revenue", "fieldtype": "Currency", "width": 150},
		{"label": _("Avg Cost Rate"), "fieldname": "avg_cost_rate", "fieldtype": "Currency", "width": 130},
		{"label": _("Total Cost"), "fieldname": "cogs", "fieldtype": "Currency", "width": 150},
		{"label": _("Gross Profit (KES)"), "fieldname": "gross_profit", "fieldtype": "Currency", "width": 160},
		{"label": _("GP %"), "fieldname": "gp_pct", "fieldtype": "Percent", "width": 80},
	]


def _get_detail_data(filters):
	item_join = _item_join(filters)
	conds = _base_conditions(filters)

	rows = frappe.db.sql(
		f"""
		SELECT
			sii.item_code,
			sii.item_name,
			COALESCE(i.item_group, sii.item_group) AS item_group,
			COUNT(sii.name) AS txn_count,
			SUM(sii.qty) AS total_qty,
			AVG(sii.net_rate) AS avg_sell_rate,
			SUM(sii.net_amount) AS revenue,
			AVG(sii.incoming_rate) AS avg_cost_rate,
			SUM(sii.qty * sii.incoming_rate) AS cogs,
			SUM(sii.net_amount) - SUM(sii.qty * sii.incoming_rate) AS gross_profit
		FROM `tabSales Invoice` si
		JOIN `tabSales Invoice Item` sii ON sii.parent = si.name
		{item_join}
		WHERE {conds}
		GROUP BY sii.item_code, sii.item_name, COALESCE(i.item_group, sii.item_group)
		ORDER BY revenue DESC
		""",
		filters,
		as_dict=True,
	)

	for r in rows:
		r["gp_pct"] = flt(r["gross_profit"]) / flt(r["revenue"]) * 100 if r.get("revenue") else 0

	# Totals row
	if rows:
		t_rev = sum(flt(r["revenue"]) for r in rows)
		t_cogs = sum(flt(r["cogs"]) for r in rows)
		t_gp = t_rev - t_cogs
		rows.append({
			"item_code": _("TOTAL"),
			"item_name": "",
			"item_group": "",
			"txn_count": sum(int(r.get("txn_count") or 0) for r in rows),
			"total_qty": sum(flt(r.get("total_qty")) for r in rows),
			"avg_sell_rate": None,
			"revenue": t_rev,
			"avg_cost_rate": None,
			"cogs": t_cogs,
			"gross_profit": t_gp,
			"gp_pct": t_gp / t_rev * 100 if t_rev else 0,
			"bold": 1,
		})

	return rows


# ---------------------------------------------------------------------------
# KPI card HTML (shown as report message)
# ---------------------------------------------------------------------------

def _get_financials(filters):
	item_join = _item_join(filters)
	conds = _base_conditions(filters)

	result = frappe.db.sql(
		f"""
		SELECT
			COUNT(DISTINCT si.name) AS invoices,
			SUM(sii.net_amount) AS revenue,
			SUM(sii.qty * sii.incoming_rate) AS cogs
		FROM `tabSales Invoice` si
		JOIN `tabSales Invoice Item` sii ON sii.parent = si.name
		{item_join}
		WHERE {conds}
		""",
		filters,
		as_dict=True,
	)
	row = result[0] if result else {}
	revenue = flt(row.get("revenue"))
	cogs = flt(row.get("cogs"))
	return {
		"invoices": row.get("invoices") or 0,
		"revenue": revenue,
		"cogs": cogs,
		"gross_profit": revenue - cogs,
		"gp_pct": (revenue - cogs) / revenue * 100 if revenue else 0,
	}


def _build_kpi_html(filters):
	fin = _get_financials(filters)

	currency = (
		frappe.get_cached_value("Company", filters.get("company"), "default_currency") or "KES"
	)

	def money(val):
		return fmt_money(val or 0, currency=currency)

	def pct(val):
		return f"{val:.1f}%"

	gp_color = "#27ae60" if fin["gp_pct"] >= 15 else "#e67e22"

	kpis = [
		("Invoices", str(int(fin["invoices"])), "#2563eb"),
		("Total Sales", money(fin["revenue"]), "#2563eb"),
		("Total Cost", money(fin["cogs"]), "#e74c3c"),
		("Gross Profit", money(fin["gross_profit"]), gp_color),
		("GP %", pct(fin["gp_pct"]), gp_color),
	]

	cards_html = ""
	for label, value, color in kpis:
		cards_html += f"""
		<div style="
			background: var(--card-bg, #fff);
			border-radius: 6px;
			box-shadow: var(--card-shadow, 0 1px 4px rgba(0,0,0,0.08));
			padding: 14px 18px;
			min-width: 140px;
			flex: 1;
		">
			<div style="font-size: 10px; color: var(--text-muted, #6c757d); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px;">{_(label)}</div>
			<div style="font-size: 12px; font-weight: 600; color: {color}; letter-spacing: 0.3px;">{value}</div>
		</div>"""

	return f"""
	<div style="padding: 12px 0 4px;">
		<div style="font-size: 12px; color: var(--text-muted); margin-bottom: 10px;">
			{filters.get("from_date")} &mdash; {filters.get("to_date")} &nbsp;|&nbsp; {filters.get("company")}
		</div>
		<div style="display: flex; flex-wrap: wrap; gap: 12px;">
			{cards_html}
		</div>
	</div>"""
