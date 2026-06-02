# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt
#
# hooks.py 更新示例 - 展示如何添加新的事件钩子
# 【注意】这是一个示例文件，不要直接替换原文件！
# 将 doc_events 部分的修改应用到实际的 hooks.py 中

# ... (保持 hooks.py 的其他部分不变)

doc_events = {
    "*": {
        "validate": [
            "erpnext.support.doctype.service_level_agreement.service_level_agreement.apply",
            "erpnext.setup.doctype.transaction_deletion_record.transaction_deletion_record.check_for_running_deletion_job",
        ],
    },
    tuple(period_closing_doctypes): {
        "validate": "erpnext.accounts.doctype.accounting_period.accounting_period.validate_accounting_period_on_doc_save",
    },
    tuple(pre_submit_validation_doctypes): {
        "validate": "erpnext.accounts.utils.pre_submit_validation",
    },
    
    # ========================================================================
    # 新增：Sales Order 事件钩子
    # ========================================================================
    "Sales Order": {
        "on_submit": [
            # 可选：如果使用事件钩子而不是直接在类中调用
            # "erpnext.selling.events.sales_order_events.on_sales_order_submit",
        ],
        "on_cancel": [
            # 可选：如果使用事件钩子而不是直接在类中调用
            # "erpnext.selling.events.sales_order_events.on_sales_order_cancel",
        ],
        "on_update_after_submit": [],
    },
    
    "Stock Entry": {
        "on_submit": "erpnext.stock.doctype.material_request.material_request.update_completed_and_requested_qty",
        "on_cancel": "erpnext.stock.doctype.material_request.material_request.update_completed_and_requested_qty",
    },
    
    # ... (保持 hooks.py 的其他部分不变)
}

# ... (保持 hooks.py 的其他部分不变)
