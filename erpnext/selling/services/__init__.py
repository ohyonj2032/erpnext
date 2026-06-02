# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""
销售模块应用服务层

本层负责业务流程编排，协调多个领域服务完成复杂业务操作。
"""

from .sales_order_service import SalesOrderService

__all__ = ["SalesOrderService"]
