import frappe
from erpnext.selling.doctype.customer.customer import check_credit_limit

class SalesOrderOrchestrator:
    def __init__(self, sales_order):
        self.sales_order = sales_order

    def execute_on_submit(self):
        """
        Orchestrates the submission logic for Sales Order.
        Handles distributed operations like credit check, stock reservation, etc.
        """
        # 1. Update previous doc status (Domain Logic of Sales)
        self.sales_order.update_prevdoc_status("submit")

        # 2. Check Credit Limit (Accounts / Selling Domain)
        try:
            self._check_credit_limit()
        except Exception as e:
            # Handle failure appropriately
            raise e

        # 3. Update Reserved Qty (Stock Domain)
        try:
            self.sales_order.update_reserved_qty()
        except Exception as e:
            # If stock reservation fails, no compensation needed because DB transaction rolls back
            # However, if it was an external service, we'd trigger a rollback for credit check here
            self._compensate_credit_limit()
            raise e

        # 4. Update Projects & Blanket Orders
        self.sales_order.update_project()
        self.sales_order.update_blanket_order()

    def _check_credit_limit(self):
        from frappe.utils import cint
        if not cint(
            frappe.db.get_value(
                "Customer Credit Limit",
                {"parent": self.sales_order.customer, "parenttype": "Customer", "company": self.sales_order.company},
                "bypass_credit_limit_check",
            )
        ):
            check_credit_limit(self.sales_order.customer, self.sales_order.company)

    def _compensate_credit_limit(self):
        # Placeholder for compensation logic if we used external APIs
        # e.g., frappe.logger().error("Rolling back credit hold...")
        pass

def on_submit_orchestrator(doc, method=None):
    """
    Hook entry point if used via doc_events
    """
    orchestrator = SalesOrderOrchestrator(doc)
    orchestrator.execute_on_submit()
