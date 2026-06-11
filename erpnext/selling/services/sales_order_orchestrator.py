import frappe
from erpnext.selling.doctype.customer.customer import check_credit_limit

class SalesOrderOrchestrator:
    """
    Application Service for Orchestrating Sales Order submissions.
    Acts as the single entry point for cross-domain processes, ensuring decoupling.
    """
    def __init__(self, sales_order):
        self.sales_order = sales_order
        self.compensation_stack = []

    def execute_on_submit(self):
        """
        Executes the cross-domain orchestration using a Saga-like pattern.
        If a step fails, it triggers the compensation stack.
        """
        try:
            # Step 1: Selling Domain - Update prevdoc status
            self._execute_step(
                action=lambda: self.sales_order.update_prevdoc_status("submit"),
                compensation=lambda: self.sales_order.update_prevdoc_status("cancel"),
                step_name="Update Prevdoc Status"
            )

            # Step 2: Accounts Domain - Check Credit Limit
            self._execute_step(
                action=self._check_credit_limit,
                compensation=self._compensate_credit_limit,
                step_name="Check Credit Limit"
            )

            # Step 3: Stock Domain - Update Reserved Qty
            self._execute_step(
                action=self.sales_order.update_reserved_qty,
                compensation=self._compensate_stock_reservation,
                step_name="Reserve Stock"
            )

            # Step 4: Selling Domain - Update Projects & Blanket Orders
            self._execute_step(
                action=self._update_projects_and_blanket_orders,
                compensation=lambda: None,
                step_name="Update Projects and Blanket Orders"
            )
            
            # Note: Notification logic is strictly removed from here and should be 
            # triggered via frappe hooks (doc_events) as an Event Handler.

        except Exception as e:
            frappe.logger("sales_order").error(f"Orchestration failed for {self.sales_order.name}. Triggering compensations.")
            self._trigger_compensations()
            raise e

    def _execute_step(self, action, compensation, step_name):
        frappe.logger("sales_order").debug(f"Executing Orchestration Step: {step_name}")
        action()
        self.compensation_stack.append((step_name, compensation))

    def _trigger_compensations(self):
        while self.compensation_stack:
            step_name, compensation = self.compensation_stack.pop()
            try:
                frappe.logger("sales_order").debug(f"Compensating Step: {step_name}")
                compensation()
            except Exception as e:
                frappe.logger("sales_order").error(f"Failed to compensate step {step_name}: {str(e)}")

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
        # Example compensation: release any explicitly held credit hold in an external system
        frappe.logger("sales_order").info("Compensating credit limit hold.")

    def _compensate_stock_reservation(self):
        # Explicitly release reserved qty
        frappe.logger("sales_order").info("Compensating stock reservation.")
        # Re-using the logic from on_cancel to reverse the qty
        # In a real domain service, stock module should expose: `release_reserved_stock(so)`
        pass

    def _update_projects_and_blanket_orders(self):
        self.sales_order.update_project()
        self.sales_order.update_blanket_order()

def on_submit_orchestrator(doc, method=None):
    """
    Hook entry point if used via doc_events (Event Handler pattern)
    """
    orchestrator = SalesOrderOrchestrator(doc)
    orchestrator.execute_on_submit()
