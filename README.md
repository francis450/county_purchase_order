## County Cables Po

Customizations on purchase orders for countycables.co.ke

### Reorder Automation

Nightly job (`county_cables_po.tasks.recalculate_reorder_recommendations` /
`check_low_stock_and_notify`) computes a median-demand-based reorder level and
suggested order quantity per item, stored in **Reorder Recommendation** and
surfaced in the **Reorder Recommendations** report. Configure trailing window,
lead time default, warehouse scope, and thresholds in **Reorder Automation
Settings**.

**Keep closing completed Purchase Orders.** This site receives stock via
Purchase Invoice / Stock Entry rather than Purchase Receipt, so the reorder
calculation relies on PO status (not receipt records) to know when ordered
stock has arrived. If POs are left open after their goods have actually
arrived, the system will keep counting that stock as "still incoming"
(inflating projected qty) and can silently suppress reorder alerts for that
item.

**Report-only for now.** This round does not create Material Requests or
Purchase Orders from the suggested quantities - that's a deliberate Phase 2
candidate, not an oversight. Suggested quantities shouldn't drive documents
until per-item lead times are populated (`Item.lead_time_days` is currently
unset for every item, so all calculations use the Settings-level default) and
the client has had a cycle to sanity-check the numbers against real buying
decisions.

**Workspace**: "Reorder Management" (visible to Purchase Manager / Purchase
User / System Manager) surfaces the number cards, status chart, and one-click
report shortcuts (Action View / Dormant Stock / Dead SKUs). It's a `public=1`
Workspace with a `roles` restriction, not a private one - in this Frappe
version `public=0` means "private to one user" (matched via `for_user`), not
"role-restricted"; role-based sharing requires `public=1` plus the `roles`
child table.

#### License

mit