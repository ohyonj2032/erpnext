
# ERPNext 订单与库存并发控制实现指南

## 概述

本实现为 ERPNext 系统添加了完善的并发控制机制，解决了订单与库存对同一业务单据状态的竞态、重复提交和回滚问题。

## 实现的关键功能

1. **服务层并发控制** (OrderService)
   - 基于缓存的分布式锁机制
   - 幂等性处理，防止重复操作
   - 状态机验证，确保状态转换的正确性
   - 事务边界管理

2. **库存原子操作** (InventoryService)
   - 乐观锁机制，使用版本号检测并发修改
   - 原子化的库存扣减，确保一致性
   - 批量操作支持
   - 事务日志记录

3. **模型层扩展**
   - Sales Order: 添加 version、last_status、status_changed_at 字段
   - Bin: 添加 version 字段用于乐观锁
   - 新建 Idempotent Operation Log 和 Inventory Transaction Log 文档类型

4. **完整测试套件**
   - 并发测试
   - 幂等性测试
   - 状态转换验证测试
   - 库存扣减测试

## 文件结构

```
erpnext/
├── erpnext/
│   ├── selling/
│   │   ├── services/
│   │   │   └── order_service.py          # 订单服务
│   │   └── doctype/
│   │       └── sales_order/
│   │           └── sales_order_extension.py  # 订单模型扩展
│   ├── stock/
│   │   ├── services/
│   │   │   └── inventory_service.py      # 库存服务
│   │   └── doctype/
│   │       └── bin/
│   │           └── bin.py (扩展)
│   └── tests/
│       ├── test_order_inventory_race.py  # 并发测试
│       └── fixtures/
│           └── initial_data.json         # 测试数据
└── IMPLEMENTATION_GUIDE.md               # 本文档
```

## 快速开始

### 1. 数据库迁移

首先运行迁移脚本来添加新的字段和文档类型：

```python
# 迁移 Sales Order
bench execute erpnext.selling.doctype.sales_order.sales_order_extension.migrate_sales_order

# 迁移 Bin
bench execute erpnext.stock.services.inventory_service.migrate_bin
```

### 2. 运行测试

```bash
# 运行并发测试
bench run-tests --test erpnext.tests.test_order_inventory_race
```

### 3. 使用示例

#### 订单状态变更

```python
from erpnext.selling.services.order_service import OrderService

# 处理订单状态变更
result = OrderService.process_order_status_change(
    order_id="SO-2024-00001",
    new_status="To Deliver and Bill",
    idempotency_key="unique-key-123"
)

print(result)
```

#### 库存扣减

```python
from erpnext.stock.services.inventory_service import InventoryService

# 原子化库存扣减
result = InventoryService.atomic_deduct_inventory(
    item_code="ITEM-001",
    warehouse="Warehouse-001",
    qty=10.0,
    transaction_id="txn-456"
)

print(result)
```

#### 批量库存操作

```python
from erpnext.stock.services.inventory_service import InventoryService

# 批量扣减
result = InventoryService.batch_atomic_deduct(
    items=[
        {"item_code": "ITEM-001", "warehouse": "WH-001", "qty": 5},
        {"item_code": "ITEM-002", "warehouse": "WH-001", "qty": 3}
    ],
    transaction_id="batch-txn-789"
)

print(result)
```

## 关键实现细节

### 1. 锁机制 (OrderService)

- **分布式锁**：使用 Frappe 缓存实现
- **锁超时**：默认 30 秒
- **重试间隔**：500ms
- **锁前缀**：`order_lock_`

### 2. 幂等性处理

- 使用 SHA-256 哈希生成唯一签名
- 基于操作参数和时间戳生成幂等键
- 自动检测并返回已执行操作的结果

### 3. 状态机验证

支持的状态转换：

```
Draft → [To Deliver and Bill, To Bill, To Deliver, Completed, Cancelled]
To Deliver and Bill → [Completed, Cancelled]
Completed → [Closed, Cancelled]
...
```

### 4. 乐观锁 (InventoryService)

- 使用 `version` 字段检测并发修改
- 每次保存自动递增版本号
- 提供版本检查方法

## 监控指标

新增的可监控指标：

| 指标名称 | 说明 | 采集方式 |
|---------|------|---------|
| `order_lock_acquisitions` | 锁获取次数 | 通过缓存统计 |
| `order_lock_timeouts` | 锁超时次数 | 日志分析 |
| `idempotent_requests` | 幂等请求数 | 操作日志 |
| `concurrency_conflicts` | 并发冲突数 | 库存事务日志 |
| `inventory_transactions` | 库存事务数 | 事务日志 |

## 故障排查

### 常见问题

1. **锁超时**
   - 检查是否有长时间运行的事务
   - 考虑增加锁超时时间

2. **并发冲突**
   - 实现适当的重试逻辑
   - 使用乐观锁版本检查

3. **幂等性失效**
   - 确保幂等键的唯一性
   - 检查事务日志是否正常写入

## 回滚步骤

如需回滚此实现：

1. 删除新增的文档类型
2. 移除新增的字段
3. 删除服务文件
4. 恢复原始代码

## 性能影响

- **读操作**：无显著影响
- **写操作**：轻微开销（版本检查、锁获取）
- **并发场景**：显著改善，避免竞态条件

## 注意事项

1. 在生产环境部署前，请先在测试环境充分验证
2. 监控新增的指标，及时发现异常
3. 定期清理事务日志，避免数据膨胀
4. 确保缓存服务正常运行，锁机制依赖缓存

## 贡献指南

提交 Issue 或 PR 前请确保：
- 所有测试通过
- 代码符合项目规范
- 更新相关文档

## 许可证

GNU General Public License v3.0

