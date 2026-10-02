# 生成啤酒与白酒组合经营快照基础服务

本项目提供酒类生产、品牌和渠道团队共享的后台基础能力，负责经营主体、生产经营站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域项目可以在这些稳定边界上增加独立的业务状态、规则与接口。

`src/beverage_ops_foundation/snapshot/` 在此基础上实现**集团半年复盘组合经营快照服务**：啤酒高端化与白酒调整两类口径不同的业务共用同一套版本化快照，指标定义、组织范围、币种汇率与期间均有版本，经营事实携带来源与批次号，发布前完成冻结复算、勾稽、异常说明与角色会签，发布后迟到渠道数据只能产生新的重述版本，已发布快照永不被静默修改。

## 目录

- `src/beverage_ops_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/beverage_ops_foundation/snapshot/`：指标目录与复算引擎、快照状态机、快照 HTTP 路由和端到端验收；
- `tests/`：基础规则、事务边界、接口路由、快照生命周期与端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

基础模块：

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.acceptance
```

组合经营快照（勾稽拦截、修订重算、重启续审、会签发布、晚到数据重述、权限下钻）：

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.snapshot.acceptance
```

成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.api --database beverage_ops.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态、审计链、未完成会签与重述继续保留。

### 快照接口（路径前缀 `/snapshot`）

| 方法与路径 | 作用 |
| --- | --- |
| `POST /snapshot/definitions` | 登记指标定义新版本（高端占比、同比、单位成本、EBITDA 等口径、勾稽规则、会签角色） |
| `GET  /snapshot/definitions?definition_id=&version_no=` | 查询定义版本 |
| `POST /snapshot/scopes` | 登记组织范围与币种汇率新版本（树形节点，含品牌/板块/地区标记） |
| `GET  /snapshot/scopes?scope_id=&version_no=` | 查询范围版本 |
| `POST /snapshot/grants` | 给操作者授权可见节点（含其整棵子树） |
| `POST /snapshot/facts` | 接收带来源 `source` 与 `batch_id` 的经营事实；同键修正写修订留痕 |
| `POST /snapshot/versions` | 冻结输入并复算生成草稿；`kind=restatement` 生成重述版本 |
| `POST /snapshot/versions/{id}/rebuild` | 草稿吸收晚到批次后重算（已发布版本拒绝） |
| `GET  /snapshot/versions/{id}/findings` | 查看勾稽异常与说明状态 |
| `POST /snapshot/versions/{id}/acknowledge` | 对警告性异常逐条说明（阻断性异常必须修订数据消除） |
| `POST /snapshot/versions/{id}/submit` | 异常清零且说明齐备后提交会签 |
| `POST /snapshot/versions/{id}/signoff` | 定义要求的角色批准/拒绝会签 |
| `POST /snapshot/versions/{id}/publish` | 管理员授权发布；发布前再次冻结复算校验 |
| `POST /snapshot/versions/{id}/cancel` | 撤销草稿或被拒绝版本 |
| `GET  /snapshot/versions/{id}` | 版本详情（状态、指纹、批次、晚到批次、会签、重述差异） |
| `GET  /snapshot/versions/{id}/recompute` | 完全从冻结输入复算并比对结果摘要 |
| `GET  /snapshot/versions/{id}/data?brand=&segment=&region=&node_id=&drill=` | 下钻查询；无权限节点整段隐藏 |
| `GET  /snapshot/versions?scope_id=&period_code=&batch_id=&status=` | 版本清单（可按批次追溯影响版本） |

## 关键规则

- **冻结复算**：高端产品占比、同比、单位成本、EBITDA 及利润率全部由指标引擎从冻结的定义、范围树、汇率与事实确定性计算（定点 Decimal），发布时再次复算并比对哈希；`/recompute` 可随时离线复核。
- **勾稽与说明**：缺汇率、超范围事实、必需度量缺失、`高端营业额 <= 营业额` 等口径关系、直报与下级汇总偏差按定义中的规则检查；阻断项必须通过修订事实消除，警告项必须逐条书面说明。
- **会签与授权**：提交会签前异常必须清零；定义中声明的角色（如 admin、reviewer）全部批准后，管理员才能授权发布，拒绝会签使版本进入 rejected。
- **迟到数据只触发重述**：已发布版本拒绝重建与修改；晚到批次在版本详情中以 `late_batches` 提示，新数据通过带 `change_reason` 的 restatement 版本发布，旧版自动置为 superseded 但结果哈希永不变；差异报告列出新增/修订事实、批次与逐节点影响范围。
- **权限下钻**：非管理员只能看到被授权节点及其子树；查询无权限节点返回 403，过滤结果中无权限明细整段隐藏并仅给出隐藏节点计数，`partial=true` 提示合计不完整。
- **重启续办**：草稿、待会签、重述、授权与修订记录全部在 SQLite 中，进程重启后可继续推进。
