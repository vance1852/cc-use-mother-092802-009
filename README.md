# 生成啤酒与白酒组合经营快照基础服务

本项目提供酒类生产、品牌和渠道团队共享的后台基础能力，负责经营主体、生产经营站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域项目可以在这些稳定边界上增加独立的业务状态、规则与接口。

`beverage_snapshot` 包在此基础上实现**集团半年复盘的组合经营快照服务**：为指标定义、组织范围、币种与期间保存版本；接收带来源与批次号的经营事实；发布前完成勾稽、异常说明与复核人/管理员双角色会签；高端占比、同比、单位成本与 EBITDA 全部可从冻结输入复算；晚到渠道数据只触发新的重述版本，绝不静默修改已发布快照，并明确展示变化原因与影响范围；支持按板块/品牌/地区下钻并隐藏无权限明细；服务重启后可继续尚未完成的审核与重述。

## 目录

- `src/beverage_ops_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/beverage_snapshot/`：指标定义版本、批次事实、快照版本状态机、勾稽、会签、重述与权限下钻；
- `tests/`：基础规则、事务边界、接口路由、复算引擎、快照工作流和端到端验收测试。

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

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.acceptance
PYTHONPATH=src python3 -m beverage_snapshot.acceptance
```

验收命令会在临时 SQLite 数据库中登记经营主体、操作者、站点和参考资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

快照验收覆盖：定义版本冻结、批次事实上报、勾稽与异常说明、双角色会签发布、从冻结事实复算核对、晚到数据触发重述且旧版数字不变、影响范围（板块/品牌/地区）、按权限隐藏明细，以及关闭并重新打开数据库后继续待会签的版本。

## 组合经营快照的关键规则

- **定义版本**：指标目录、组织范围、报告币种与汇率随 `definition_version` 冻结，事实只能上报到已冻结版本；派生指标（高端占比、同比、单位成本、EBITDA、EBITDA 率）不能手工上报，只能从事实复算。
- **口径隔离**：啤酒销量单位千升（kL）、白酒销量单位吨（tonne），单位不匹配会被拒绝；跨板块不汇总销量，因此总体单位成本与高端销量占比显式置空；金额按冻结汇率换算后汇总。
- **批次与晚到数据**：事实带 `source` 与 `batch_id`，同批次同维度内容不同会冲突；晚到批次（`is_late=true`）是新行，从不覆盖旧批次。
- **快照不可变**：快照版本通过绑定表钉住所用事实并记录输入指纹；发布后数字不可变，晚到数据只能用 `/snapshots/restate` 生成新版本，新版本发布时旧版本转为 `superseded`，其 `impact` 给出原因、总体/板块/分组的新旧值与 delta、受影响板块/品牌/地区。
- **勾稽与会签**：阻塞项（如高端销量超过总销量、缺核心输入）必须补数据后重算，不能用说明绕过；预警项（同比异常、晚到批次）需书面说明；提交后须复核人与管理员两个角色批准才发布，驳回退回草稿。
- **下钻与权限**：`/snapshot-view?segment=&brand=&region=` 下钻；操作者只能看到被授予的板块/品牌/地区范围，其余明细计入 `hidden_fact_count`，受限视图以 `scope_limited=true` 标识。
- **重启续审**：全部状态在 SQLite 中，重新打开数据库后 `submitted` 版本可继续会签，未完成重述可继续勾稽。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.api --database beverage_ops.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m beverage_snapshot.api --database beverage_ops.sqlite3 --host 127.0.0.1 --port 8081
```

两个服务共用同一 SQLite 数据库与审计链：组织/操作者/站点经基础服务（8080）建档，快照服务（8081）提供定义版本、事实、快照与下钻接口。健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。

快照服务主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/definition-versions` | 创建指标定义/范围/币种/期间版本（管理员） |
| POST | `/definition-versions/freeze` | 冻结定义版本 |
| POST | `/data-scopes` | 授予板块/品牌/地区数据范围（管理员） |
| POST | `/facts` | 上报带来源与批次号的经营事实 |
| GET | `/facts` | 按批次/板块/品牌/地区/期间查询事实（隐藏无权限明细） |
| POST | `/snapshots` | 基于最晚批次事实创建期间 v1 草稿 |
| POST | `/snapshots/restate` | 基于已发布版本创建重述新版本 |
| POST | `/snapshots/reconcile` | 刷新最晚批次、复算并输出勾稽问题 |
| POST | `/issues/explain` | 对预警异常书面说明 |
| POST | `/snapshots/submit` | 勾稽与说明齐备后提交会签 |
| POST | `/snapshots/approve` | 复核人/管理员批准或驳回 |
| GET | `/snapshot-versions` | 列出版本与重述影响 |
| GET | `/snapshot-version?id=` | 版本元数据、勾稽问题与会签记录 |
| GET | `/snapshot-view?id=&segment=&brand=&region=` | 下钻复算结果（按权限过滤） |
| GET | `/snapshot-verify?id=` | 用冻结事实复算并核对指纹与结果 |
