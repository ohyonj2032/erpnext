# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""
会计模块领域服务层

本层包含会计相关的核心业务逻辑。
"""

from .sales_invoice_service import SalesInvoiceService

__all__ = ["SalesInvoiceService"]
