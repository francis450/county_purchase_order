// Copyright (c) 2026, ERP Kenya and contributors
// For license information, please see license.txt

frappe.query_reports["Reorder Recommendations"] = {
	filters: [
		{
			fieldname: "view",
			label: __("View"),
			fieldtype: "Select",
			options: ["Action", "Dormant Stock", "Dead SKU", "All"],
			default: "Action",
			reqd: 1,
		},
		{
			fieldname: "status",
			label: __("Status (overrides view)"),
			fieldtype: "Select",
			options: ["", "Critical", "Reorder Now", "Watch", "OK", "Dormant Stock", "Dead SKU", "Insufficient History"],
		},
		{
			fieldname: "item_group",
			label: __("Item Group"),
			fieldtype: "Link",
			options: "Item Group",
		},
		{
			fieldname: "supplier",
			label: __("Supplier"),
			fieldtype: "Link",
			options: "Supplier",
		},
		{
			fieldname: "show_diagnostics",
			label: __("Show Diagnostics"),
			fieldtype: "Check",
			default: 0,
		},
	],
	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (!data || !data.item_code) {
			// Supplier subtotal / grand total rows.
			return `<b>${value || ""}</b>`;
		}
		if (column.fieldname === "status") {
			const colors = {
				Critical: "red",
				"Reorder Now": "orange",
				Watch: "yellow",
				OK: "green",
				"Dormant Stock": "blue",
				"Dead SKU": "grey",
				"Insufficient History": "grey",
			};
			const color = colors[data.status] || "grey";
			value = `<span class="indicator-pill ${color}">${data.status}</span>`;
		}
		return value;
	},
};
