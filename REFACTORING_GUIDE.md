# Sales Order 提交流程重构指南

## 📋 概述

本次重构旨在解决 ERPNext 中 Sales Order 提交时的高耦合问题，通过引入**编排层+领域服务+事件驱动**的三层架构来提高代码的可维护性和可扩展性。

---

## 🏗️ 重构架构

### 三层架构设计

```
┌─────────────────────────────────────────────────────────┐
│                   编排层 (Orchestrator)                  │
│              SalesOrderOrchestrator                      │
│         唯一的提交流程入口，协调各领域服务                │
└─────────────────────────────────────────────────────────┘
                          │
        ┌─────────────────┼─────────────────┐
        │                 │                 │
        ▼                 ▼                 ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  库存领域    │  │  会计领域    │  │  通知服务    │
│  服务        │  │  服务        │  │  (事件驱动)  │
└──────────────┘  └──────────────┘  └──────────────┘
```

### 关键组件

1. **编排层 (`erpnext/selling/services/`)**
   - `SalesOrderOrchestrator`: 主编排器
   - `TransactionManager`: 事务管理器（Sagas 模式）

2. **领域服务 (`erpnext/stock/services/`, `erpnext/accounts/services/`)**
   - `StockReservationService`: 库存预留服务
   - （待扩展）会计服务等

3. **事件处理器 (`erpnext/selling/events/`)**
   - 解耦通知等异步操作

---

## 🚀 迁移步骤

### 阶段 1: 基础设施准备（当前已完成）

✅ 创建服务目录结构
✅ 实现编排器和领域服务
✅ 实现事务管理器和回滚机制
✅ 创建事件处理器

### 阶段 2: 逐步集成（下一步）

#### 步骤 1: 备份当前代码
```bash
# 备份原始的 sales_order.py
cp erpnext/selling/doctype/sales_order/sales_order.py \
   erpnext/selling/doctype/sales_order/sales_order.py.backup
```

#### 步骤 2: 修改 SalesOrder.on_submit 方法

编辑 `erpnext/selling/doctype/sales_order/sales_order.py`:

```python
def on_submit(self):
    # 新的方式：使用编排器
    from erpnext.selling.services import SalesOrderOrchestrator
    
    orchestrator = SalesOrderOrchestrator(self)
    orchestrator.submit()

    # 【可选】保留原有逻辑作为降级方案（初期建议保留）
    # self._on_submit_legacy()

def _on_submit_legacy(self):
    # 将原有的 on_submit 代码移到这里
    super().update_prevdoc_status()
    self.check_credit_limit()
    # ... 其他原有代码
```

#### 步骤 3: 添加特性开关（推荐）

在 `Selling Settings` 中添加开关：

```python
# 在 SalesOrder.on_submit 中
use_new_orchestrator = frappe.get_cached_value(
    "Selling Settings", 
    None, 
    "use_new_sales_order_orchestrator"
)

if use_new_orchestrator:
    from erpnext.selling.services import SalesOrderOrchestrator
    SalesOrderOrchestrator(self).submit()
else:
    self._on_submit_legacy()
```

#### 步骤 4: 逐步迁移 on_cancel

类似地，修改 `on_cancel` 方法：

```python
def on_cancel(self):
    from erpnext.selling.events import on_sales_order_cancel
    on_sales_order_cancel(self)
```

### 阶段 3: 优化和完善

1. **添加单元测试**
2. **性能监控**
3. **日志增强**
4. **完善错误处理**

---

## 🔧 关键设计决策

### 1. 为什么选择编排器作为唯一入口？

**问题**：原代码中 `SalesOrder` 类直接调用多个模块：
- `stock` 模块的库存预留
- `accounts` 模块的会计处理
- `notifications` 模块的通知

**解决方案**：
- ✅ 单一职责原则：`SalesOrder` 只负责文档本身的业务逻辑
- ✅ 降低耦合：各模块通过编排器协调，不直接相互依赖
- ✅ 便于测试：可以独立测试编排逻辑

### 2. 领域服务 vs 应用服务 vs 事件处理器

| 组件类型 | 位置 | 职责 | 示例 |
|---------|------|------|------|
| **领域服务** | `erpnext/{module}/services/` | 封装纯领域逻辑 | `StockReservationService` |
| **应用服务** | `erpnext/selling/services/` | 协调用例流程 | `SalesOrderOrchestrator` |
| **事件处理器** | `erpnext/selling/events/` | 解耦异步操作 | 通知发送、审计日志 |

### 3. 补偿/回滚机制设计

采用 **Sagas 模式**：
- 每个事务步骤都有对应的回滚操作
- 失败时按相反顺序回滚已成功的步骤
- 回滚失败时记录日志但不中断其他回滚

```python
# 伪代码示例
transaction = TransactionManager()
transaction.add_step(
    name="StockReservation",
    execute=reserve_stock,
    rollback=cancel_reservation
)
transaction.add_step(
    name="Accounting",
    execute=post_to_gl,
    rollback=reverse_gl_entry
)
transaction.execute()
```

---

## 📁 新增文件清单

```
erpnext/
├── selling/
│   ├── services/
│   │   ├── __init__.py
│   │   ├── sales_order_orchestrator.py  # 编排器
│   │   └── transaction_manager.py        # 事务管理器
│   └── events/
│       ├── __init__.py
│       └── sales_order_events.py         # 事件处理器
└── stock/
    └── services/
        ├── __init__.py
        └── stock_reservation_service.py  # 库存预留服务
```

---

## 🎯 重构收益

1. **可维护性提升**
   - 职责清晰，易于理解
   - 修改某一模块不影响其他模块

2. **可测试性提升**
   - 可以独立测试各领域服务
   - 编排逻辑可以 mock 依赖

3. **可扩展性提升**
   - 新增业务步骤只需添加新的领域服务
   - 事件驱动便于添加新的通知类型

4. **数据一致性**
   - Sagas 模式保证失败时的正确回滚
   - 显式的事务边界

---

## ⚠️ 注意事项

### 迁移风险缓解

1. **灰度发布**
   - 先在测试环境验证
   - 再在生产环境小范围试用
   - 最后全量切换

2. **降级方案**
   - 保留原有代码作为降级方案
   - 通过特性开关快速切换

3. **监控和日志**
   - 增强日志记录
   - 监控关键指标（成功率、耗时等）

### 向后兼容

- ✅ 保持公共 API 不变
- ✅ 提供迁移工具
- ✅ 支持逐步迁移

---

## 📊 后续扩展建议

1. **会计领域服务**
   - 将会计相关逻辑从 `SalesOrder` 中剥离
   - 创建 `AccountingService`

2. **异步处理**
   - 使用 `frappe.enqueue` 处理耗时操作
   - 提高响应速度

3. **更多文档类型**
   - 将相同模式应用到 `Sales Invoice`、`Purchase Order` 等
   - 统一架构风格

4. **测试覆盖**
   - 单元测试：测试各领域服务
   - 集成测试：测试编排逻辑
   - E2E 测试：完整流程验证

---

## 🆘 常见问题

### Q: 如何回滚到原版本？
A: 使用特性开关关闭新编排器，或通过 git 恢复原代码。

### Q: 对性能有影响吗？
A: 理论上影响很小（增加了一层间接调用），实际性能需要测试验证。如发现问题可以优化。

### Q: 其他文档类型是否也需要重构？
A: 建议采用相同模式，但可以分阶段进行，优先处理高频使用的文档类型。

---

## 📞 联系方式

如有问题或建议，请联系开发团队。
