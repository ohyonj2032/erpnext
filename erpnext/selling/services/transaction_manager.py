# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from typing import List, Callable, Optional
from dataclasses import dataclass, field


@dataclass
class TransactionStep:
    """事务步骤"""
    name: str
    execute: Callable
    rollback: Optional[Callable] = None
    executed: bool = False
    success: bool = False


@dataclass
class TransactionContext:
    """事务上下文"""
    steps: List[TransactionStep] = field(default_factory=list)
    current_step: int = 0
    error: Optional[Exception] = None


class TransactionManager:
    """
    事务管理器 - 提供 Sagas 模式的实现
    用于处理分布式事务的补偿/回滚
    """

    def __init__(self, name: str = "SalesOrderTransaction"):
        self.name = name
        self.context = TransactionContext()

    def add_step(
        self,
        name: str,
        execute_func: Callable,
        rollback_func: Optional[Callable] = None
    ) -> 'TransactionManager':
        """
        添加事务步骤
        
        Args:
            name: 步骤名称
            execute_func: 执行函数
            rollback_func: 回滚函数（可选）
        """
        step = TransactionStep(
            name=name,
            execute=execute_func,
            rollback=rollback_func
        )
        self.context.steps.append(step)
        return self

    def execute(self) -> bool:
        """
        执行所有事务步骤
        失败时自动执行已完成步骤的回滚
        """
        frappe.log_error(f"Starting transaction: {self.name}")

        for i, step in enumerate(self.context.steps):
            self.context.current_step = i

            try:
                frappe.log_error(f"Executing step: {step.name}")
                step.execute()
                step.executed = True
                step.success = True
                frappe.log_error(f"Step completed: {step.name}")

            except Exception as e:
                step.success = False
                self.context.error = e
                frappe.log_error(f"Step failed: {step.name}, Error: {str(e)}")

                # 执行回滚
                self._rollback()
                raise

        frappe.log_error(f"Transaction completed successfully: {self.name}")
        return True

    def _rollback(self):
        """
        执行回滚操作 - 按相反顺序回滚已成功执行的步骤
        """
        frappe.log_error(f"Starting rollback for transaction: {self.name}")

        # 按相反顺序回滚已成功执行的步骤
        for step in reversed(self.context.steps[:self.context.current_step + 1]):
            if step.executed and step.success and step.rollback:
                try:
                    frappe.log_error(f"Rolling back step: {step.name}")
                    step.rollback()
                    frappe.log_error(f"Step rolled back: {step.name}")
                except Exception as e:
                    # 回滚失败时记录日志但继续尝试其他回滚
                    frappe.log_error(
                        f"Failed to rollback step: {step.name}, Error: {str(e)}"
                    )

        frappe.log_error(f"Rollback completed for transaction: {self.name}")

    def get_status(self) -> dict:
        """获取事务状态"""
        return {
            "transaction_name": self.name,
            "total_steps": len(self.context.steps),
            "current_step": self.context.current_step,
            "steps": [
                {
                    "name": step.name,
                    "executed": step.executed,
                    "success": step.success
                }
                for step in self.context.steps
            ],
            "error": str(self.context.error) if self.context.error else None
        }


def create_sales_order_transaction(sales_order) -> TransactionManager:
    """
    为 Sales Order 创建事务管理器
    """
    from erpnext.stock.services import StockReservationService

    transaction = TransactionManager("SalesOrderSubmit")

    reservation_service = StockReservationService()

    # 步骤 1: 库存预留
    transaction.add_step(
        name="StockReservation",
        execute_func=lambda: reservation_service.reserve_for_sales_order(sales_order) 
            if sales_order.get("reserve_stock") and not sales_order.get("is_subcontracted") 
            else None,
        rollback_func=lambda: reservation_service.cancel_reservation_for_sales_order(sales_order)
    )

    # 步骤 2: 优惠券更新
    if sales_order.coupon_code:
        from erpnext.accounts.doctype.pricing_rule.utils import update_coupon_code_count
        
        transaction.add_step(
            name="CouponUpdate",
            execute_func=lambda: update_coupon_code_count(sales_order.coupon_code, "used"),
            rollback_func=lambda: update_coupon_code_count(sales_order.coupon_code, "cancelled")
        )

    # 可以继续添加更多步骤...

    return transaction
