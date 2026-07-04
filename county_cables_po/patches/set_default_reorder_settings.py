import frappe


def execute():
	"""Seed Reorder Automation Settings with sensible defaults, including the
	4 Kenya warehouses as included_warehouses (pending client confirmation of
	final warehouse scope - see project plan)."""
	settings = frappe.get_single("Reorder Automation Settings")
	if settings.included_warehouses:
		return

	default_warehouses = [
		"APPLE FARM - CC",
		"NYUMBANI - CC",
		"COUNTY CABLES - CC",
		"ABC CABLES - CC",
	]
	existing = set(frappe.get_all("Warehouse", pluck="name"))
	for warehouse in default_warehouses:
		if warehouse in existing:
			settings.append("included_warehouses", {"warehouse": warehouse})

	if not settings.notify_role and frappe.db.exists("Role", "Purchase Manager"):
		settings.notify_role = "Purchase Manager"

	settings.save(ignore_permissions=True)
