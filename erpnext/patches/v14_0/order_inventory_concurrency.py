# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import frappe

def execute():
    """
    Patch to set up order and inventory concurrency control.
    Creates necessary database tables and adds version field to Bin.
    """
    
    # Create Order Lock table
    if not frappe.db.exists("DocType", "Order Lock"):
        frappe.db.sql("""
            CREATE TABLE IF NOT EXISTS `tabOrder Lock` (
                `name` VARCHAR(140) NOT NULL,
                `lock_time` DATETIME,
                `locked_by` VARCHAR(140),
                `creation` DATETIME,
                `modified` DATETIME,
                `modified_by` VARCHAR(140),
                `owner` VARCHAR(140),
                `docstatus` INT DEFAULT 0,
                `idx` INT DEFAULT 0,
                PRIMARY KEY (`name`)
            ) ENGINE=InnoDB
        """)
    
    # Create Order Idempotency Record table
    if not frappe.db.exists("DocType", "Order Idempotency Record"):
        frappe.db.sql("""
            CREATE TABLE IF NOT EXISTS `tabOrder Idempotency Record` (
                `name` VARCHAR(140) NOT NULL,
                `order` VARCHAR(140),
                `idempotency_key` VARCHAR(255),
                `result` TEXT,
                `timestamp` DATETIME,
                `creation` DATETIME,
                `modified` DATETIME,
                `modified_by` VARCHAR(140),
                `owner` VARCHAR(140),
                `docstatus` INT DEFAULT 0,
                `idx` INT DEFAULT 0,
                PRIMARY KEY (`name`),
                UNIQUE KEY `idx_order_key` (`order`, `idempotency_key`)
            ) ENGINE=InnoDB
        """)
    
    # Create Order State Change table
    if not frappe.db.exists("DocType", "Order State Change"):
        frappe.db.sql("""
            CREATE TABLE IF NOT EXISTS `tabOrder State Change` (
                `name` VARCHAR(140) NOT NULL,
                `order` VARCHAR(140),
                `old_status` VARCHAR(140),
                `new_status` VARCHAR(140),
                `changed_by` VARCHAR(140),
                `timestamp` DATETIME,
                `creation` DATETIME,
                `modified` DATETIME,
                `modified_by` VARCHAR(140),
                `owner` VARCHAR(140),
                `docstatus` INT DEFAULT 0,
                `idx` INT DEFAULT 0,
                PRIMARY KEY (`name`),
                KEY `idx_order` (`order`)
            ) ENGINE=InnoDB
        """)
    
    # Add version column to Bin if not exists
    if not frappe.db.has_column("Bin", "version"):
        frappe.db.sql("ALTER TABLE `tabBin` ADD COLUMN `version` INT DEFAULT 0")
        # Initialize version for existing records
        frappe.db.sql("UPDATE `tabBin` SET `version` = 0 WHERE `version` IS NULL")
    
    frappe.db.commit()
