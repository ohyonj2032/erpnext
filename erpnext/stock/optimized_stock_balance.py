#!/usr/bin/env python3
# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""
优化的库存余额管理模块
添加了并发控制、乐观锁、幂等性等改进
"""

import frappe
from frappe.utils import flt, now
from frappe.query_builder.functions import Sum
from erpnext.controllers.stock_controller import create_repost_item_valuation_entry


class OptimizedStockManager:
    """优化的库存管理器 - 带并发控制"""
    
    @staticmethod
    def update_bin_qty_with_lock(item_code, warehouse, qty_dict=None):
        """
        带数据库锁的Bin数量更新
        
        使用SELECT ... FOR UPDATE来防止竞态条件
        """
        from erpnext.stock.utils import get_bin
        
        # 获取Bin并加锁
        bin = get_bin(item_code, warehouse)
        
        # 使用数据库行锁
        frappe.db.sql("""
            SELECT name FROM `tabBin` 
            WHERE name = %s FOR UPDATE
        """, bin.name)
        
        # 重新加载以确保最新
        bin.load_from_db()
        
        mismatch = False
        for field, value in qty_dict.items():
            if flt(bin.get(field)) != flt(value):
                bin.set(field, flt(value))
                mismatch = True
        
        bin.modified = now()
        if mismatch:
            bin.set_projected_qty()
            bin.db_update()
            bin.clear_cache()
        
        return bin
    
    @staticmethod
    def atomic_stock_update(item_code, warehouse, qty_change, operation_type):
        """
        原子化的库存更新
        
        使用数据库级别的原子操作代替先读后改
        """
        Bin = frappe.qb.DocType("Bin")
        
        # 使用原子UPDATE语句
        query = (
            frappe.qb.update(Bin)
            .set(Bin.actual_qty, Bin.actual_qty + qty_change)
            .set(Bin.modified, now())
            .where(
                (Bin.item_code == item_code) & 
                (Bin.warehouse == warehouse)
            )
        )
        
        # 如果不允许负库存，添加条件
        stock_settings = frappe.get_cached_doc("Stock Settings")
        if not stock_settings.allow_negative_stock:
            query = query.where(Bin.actual_qty + qty_change >= 0)
        
        result = query.run()
        
        # 检查是否更新成功
        if not result or frappe.db.sql("""
            SELECT ROW_COUNT()
        """)[0][0] == 0:
            frappe.throw(f"库存更新失败，可能是负库存限制或竞态条件")
        
        # 清除缓存
        frappe.clear_cache(doctype="Bin")
        
        return True
    
    @staticmethod
    def update_reserved_qty_idempotent(item_code, warehouse, qty_change, reservation_id):
        """
        幂等的预留库存更新
        
        使用reservation_id确保同一操作不会被重复应用
        """
        # 记录操作日志
        log_exists = frappe.db.exists(
            "Stock Reservation Log",
            {"reservation_id": reservation_id}
        )
        
        if log_exists:
            frappe.logger().info(f"操作 {reservation_id} 已执行，跳过")
            return False  # 幂等 - 已经执行过
        
        # 开始事务
        try:
            # 创建操作日志
            log = frappe.new_doc("Stock Reservation Log")
            log.reservation_id = reservation_id
            log.item_code = item_code
            log.warehouse = warehouse
            log.qty_change = qty_change
            log.timestamp = now()
            log.insert(ignore_permissions=True)
            
            # 更新库存
            OptimizedStockManager.update_bin_qty_with_lock(
                item_code,
                warehouse,
                {"reserved_qty": qty_change}
            )
            
            frappe.db.commit()
            return True
            
        except Exception as e:
            frappe.db.rollback()
            raise e


def update_bin_qty(item_code, warehouse, qty_dict=None):
    """
    优化的update_bin_qty替换函数
    
    与原函数保持API兼容，但内部使用更好的并发控制
    """
    return OptimizedStockManager.update_bin_qty_with_lock(item_code, warehouse, qty_dict)


def repost_stock_safe(item_code, warehouse, allow_zero_rate=False, 
                      only_actual=False, only_bin=False, allow_negative_stock=False):
    """
    安全的库存重过账 - 带乐观锁
    """
    # 创建锁记录
    lock_name = f"repost-{item_code}-{warehouse}"
    
    try:
        # 尝试获取锁
        lock = frappe.new_doc("Stock Repost Lock")
        lock.name = lock_name
        lock.item_code = item_code
        lock.warehouse = warehouse
        lock.locked_at = now()
        lock.insert(ignore_permissions=True)
        
        # 执行重过账
        from erpnext.stock.stock_balance import repost_stock as original_repost
        result = original_repost(
            item_code, warehouse, allow_zero_rate, 
            only_actual, only_bin, allow_negative_stock
        )
        
        return result
        
    except frappe.DuplicateEntryError:
        # 锁已存在，等待或重试
        frappe.throw(f"库存 {item_code} 在 {warehouse} 正在重过账中，请稍后再试")
        
    finally:
        # 释放锁
        if frappe.db.exists("Stock Repost Lock", lock_name):
            frappe.delete_doc("Stock Repost Lock", lock_name, ignore_permissions=True)


class OptimisticLockManager:
    """乐观锁管理器"""
    
    @staticmethod
    def acquire_lock(doc_type, doc_name, timeout=30):
        """获取乐观锁"""
        lock_name = f"{doc_type}-{doc_name}"
        expires_at = frappe.utils.add_to_date(None, seconds=timeout)
        
        try:
            lock = frappe.new_doc("Optimistic Lock")
            lock.name = lock_name
            lock.doc_type = doc_type
            lock.doc_name = doc_name
            lock.expires_at = expires_at
            lock.insert(ignore_permissions=True)
            return True
        except frappe.DuplicateEntryError:
            # 检查锁是否过期
            lock = frappe.get_doc("Optimistic Lock", lock_name)
            if lock.expires_at < now():
                # 过期了，删除并重新获取
                lock.delete()
                return OptimisticLockManager.acquire_lock(doc_type, doc_name, timeout)
            return False
    
    @staticmethod
    def release_lock(doc_type, doc_name):
        """释放锁"""
        lock_name = f"{doc_type}-{doc_name}"
        if frappe.db.exists("Optimistic Lock", lock_name):
            frappe.delete_doc("Optimistic Lock", lock_name, ignore_permissions=True)


def check_and_update_status(doc, expected_old_status, new_status):
    """
    带状态检查的状态更新 - 防止竞态条件
    
    使用CAS（Compare-And-Swap）模式
    """
    # 使用数据库原子操作检查并更新
    result = frappe.db.sql("""
        UPDATE `tab{doctype}`
        SET status = %s, modified = %s
        WHERE name = %s AND status = %s
    """.format(doctype=doc.doctype), (new_status, now(), doc.name, expected_old_status))
    
    affected_rows = frappe.db.sql("SELECT ROW_COUNT()")[0][0]
    
    if affected_rows == 0:
        frappe.throw(f"状态更新失败：当前状态不是 {expected_old_status}")
    
    doc.reload()
    return True


def validate_duplicate_submission(doc, identifier_field=None):
    """
    重复提交验证
    
    基于唯一标识符防止重复处理
    """
    if not identifier_field:
        # 使用文档名和时间戳作为标识符
        identifier = f"{doc.doctype}-{doc.name}-{frappe.utils.now()}"
    else:
        identifier = doc.get(identifier_field)
    
    # 检查是否已处理
    if frappe.db.exists("Submission Log", {"identifier": identifier}):
        frappe.throw("该操作已处理，请勿重复提交", frappe.DuplicateEntryError)
    
    # 记录处理
    log = frappe.new_doc("Submission Log")
    log.identifier = identifier
    log.doctype_name = doc.doctype
    log.docname = doc.name
    log.timestamp = now()
    log.insert(ignore_permissions=True)
    
    return identifier


# 辅助函数 - 金额计算精度控制
def precise_currency_calculation(amount, precision=None):
    """
    精确的货币计算
    
    使用Decimal代替float进行计算
    """
    from decimal import Decimal, ROUND_HALF_UP
    
    if precision is None:
        precision = frappe.db.get_single_value("System Settings", "currency_precision") or 2
    
    decimal_amount = Decimal(str(amount))
    quantize_str = f"1.{'0' * precision}"
    
    return float(decimal_amount.quantize(Decimal(quantize_str), rounding=ROUND_HALF_UP))


def calculate_with_precision(amount1, amount2, operation='+', precision=2):
    """带精度的数学运算"""
    from decimal import Decimal, ROUND_HALF_UP
    
    d1 = Decimal(str(amount1))
    d2 = Decimal(str(amount2))
    
    if operation == '+':
        result = d1 + d2
    elif operation == '-':
        result = d1 - d2
    elif operation == '*':
        result = d1 * d2
    elif operation == '/':
        result = d1 / d2 if d2 != 0 else Decimal('0')
    else:
        result = d1
    
    quantize_str = f"1.{'0' * precision}"
    return float(result.quantize(Decimal(quantize_str), rounding=ROUND_HALF_UP))


# 以下是建议创建的DocType定义（用于支持上述功能）
"""
建议创建的DocType:

1. Stock Reservation Log - 库存预留日志
   - reservation_id (Data, 唯一)
   - item_code (Link to Item)
   - warehouse (Link to Warehouse)
   - qty_change (Float)
   - timestamp (Datetime)

2. Stock Repost Lock - 库存重过账锁
   - name (Data, Primary Key)
   - item_code (Link to Item)
   - warehouse (Link to Warehouse)
   - locked_at (Datetime)

3. Optimistic Lock - 乐观锁
   - name (Data, Primary Key)
   - doc_type (Data)
   - doc_name (Data)
   - expires_at (Datetime)

4. Submission Log - 提交日志
   - identifier (Data, 唯一)
   - doctype_name (Data)
   - docname (Data)
   - timestamp (Datetime)
"""
