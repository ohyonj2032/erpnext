# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from typing import Dict, List, Optional
from dataclasses import dataclass


@dataclass
class SalesOrderSubmitContext:
    """上下文数据结构，在各个步骤间传递数据"""
    sales_order: object
    stock_reservation_result: Optional[Dict] = None
    accounting_result: Optional[Dict] = None
    notification_result: Optional[Dict] = None
    rollback_steps: List[callable] = None
    errors: List[str] = None

    def __post_init__(self):
        self.rollback_steps = self.rollback_steps or []
        self.errors = self.errors or []


class SalesOrderOrchestrator:
    """
    Sales Order 提交流程编排器
    作为唯一的编排入口，协调各个领域服务
    """

    def __init__(self, sales_order: object):
        self.sales_order = sales_order
        self.context = SalesOrderSubmitContext(sales_order=sales_order)

    def submit(self):
        """
        主编排方法，按顺序执行提交流程
        """
        try:
            # 步骤 1: 验证前置条件
            self._validate_prerequisites()

            # 步骤 2: 更新前置文档状态
            self._update_prevdoc_status()

            # 步骤 3: 检查信用额度
            self._check_credit_limit()

            # 步骤 4: 库存预留（添加回滚）
            self._reserve_stock()

            # 步骤 5: 更新库存预留数量
            self._update_reserved_qty()

            # 步骤 6: 审批控制
            self._validate_approving_authority()

            # 步骤 7: 更新项目
            self._update_project()

            # 步骤 8: 更新一揽子订单
            self._update_blanket_order()

            # 步骤 9: 会计处理（添加回滚）
            self._process_accounting()

            # 步骤 10: 更新关联文档
            self._update_linked_doc()

            # 步骤 11: 更新优惠券
            self._update_coupon_code()

            # 步骤 12: 发送通知
            self._send_notifications()

            return True

        except Exception as e:
            # 发生错误时执行回滚
            self._rollback()
            frappe.log_error(f"Sales Order submission failed: {str(e)}")
            raise

    def _validate_prerequisites(self):
        """验证前置条件"""
        from erpnext.selling.doctype.sales_order.sales_order import check_modified_date
        check_modified_date(self.sales_order)

    def _update_prevdoc_status(self):
        """更新前置文档状态"""
        self.sales_order.update_prevdoc_status()

    def _check_credit_limit(self):
        """检查信用额度"""
        self.sales_order.check_credit_limit()

    def _reserve_stock(self):
        """库存预留 - 使用领域服务"""
        from erpnext.stock.services.stock_reservation_service import StockReservationService

        if self.sales_order.get("reserve_stock") and not self.sales_order.get("is_subcontracted"):
            try:
                reservation_service = StockReservationService()
                result = reservation_service.reserve_for_sales_order(self.sales_order)
                self.context.stock_reservation_result = result

                # 注册回滚步骤
                self.context.rollback_steps.append(
                    lambda: reservation_service.cancel_reservation_for_sales_order(self.sales_order)
                )

            except Exception as e:
                self.context.errors.append(f"Stock reservation failed: {str(e)}")
                raise

    def _update_reserved_qty(self):
        """更新库存预留数量"""
        self.sales_order.update_reserved_qty()

    def _validate_approving_authority(self):
        """验证审批权限"""
        if hasattr(frappe, "get_cached_doc"):
            authorization_control = frappe.get_cached_doc("Authorization Control")
            if hasattr(authorization_control, "validate_approving_authority"):
                authorization_control.validate_approving_authority(
                    self.sales_order.doctype, 
                    self.sales_order.company, 
                    self.sales_order.base_grand_total, 
                    self.sales_order
                )

    def _update_project(self):
        """更新项目"""
        self.sales_order.update_project()

    def _update_blanket_order(self):
        """更新一揽子订单"""
        self.sales_order.update_blanket_order()

    def _process_accounting(self):
        """会计处理 - 这里可以扩展为完整的会计领域服务"""
        # 目前会计处理在 ERPNext 中更多是在 Sales Invoice 阶段
        # 这里可以预留位置，为未来的会计处理扩展
        pass

    def _update_linked_doc(self):
        """更新关联文档"""
        if hasattr(self.sales_order, 'update_linked_doc'):
            self.sales_order.update_linked_doc(
                self.sales_order.doctype, 
                self.sales_order.name, 
                self.sales_order.inter_company_order_reference
            )
        else:
            from erpnext.accounts.doctype.sales_invoice.sales_invoice import update_linked_doc
            update_linked_doc(
                self.sales_order.doctype, 
                self.sales_order.name, 
                self.sales_order.inter_company_order_reference
            )

    def _update_coupon_code(self):
        """更新优惠券"""
        if self.sales_order.coupon_code:
            try:
                from erpnext.accounts.doctype.pricing_rule.utils import update_coupon_code_count
                update_coupon_code_count(self.sales_order.coupon_code, "used")

                # 注册回滚步骤
                self.context.rollback_steps.append(
                    lambda: update_coupon_code_count(self.sales_order.coupon_code, "cancelled")
                )
            except Exception as e:
                self.context.errors.append(f"Coupon code update failed: {str(e)}")

    def _send_notifications(self):
        """发送通知 - 可以通过事件驱动实现"""
        # 这里可以触发事件，让通知系统异步处理
        frappe.publish_realtime(
            "sales_order_submitted",
            {
                "name": self.sales_order.name,
                "customer": self.sales_order.customer,
                "company": self.sales_order.company
            }
        )

    def _rollback(self):
        """执行回滚操作"""
        frappe.log_error(f"Rolling back Sales Order {self.sales_order.name} submission")

        # 按相反顺序执行回滚步骤
        for rollback_step in reversed(self.context.rollback_steps):
            try:
                rollback_step()
            except Exception as e:
                frappe.log_error(f"Rollback step failed: {str(e)}")

        # 记录所有错误
        if self.context.errors:
            frappe.log_error(f"Sales Order submission errors: {', '.join(self.context.errors)}")
