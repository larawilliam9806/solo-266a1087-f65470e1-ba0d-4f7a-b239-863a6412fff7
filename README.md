# 混凝土试件申报入仓

Flask + SQLite 的入仓网页，用于汇总混凝土试件报告（含补页）中申报的强度指标值。
支持 `docker compose up` 一键启动。

## 概念模型

| 层次 | 表 | 说明 |
| --- | --- | --- |
| 指标字典 | `indicators` | 仓库内的指标（如「抗压强度（实测）」「抗压强度（折算）」），单位 MPa。 |
| 报告模板 | `templates` / `template_columns` | 登记 CSV 列名 → 指标 的对照，以及试件号列名、报告修订号列名。**同一个列名「强度」在不同模板中可以指向不同指标。** |
| 批次档案 | `batches` / `declarations` | 每次上传的原始 CSV 字节原样保存；`declarations` 是按「当前模板」解释出的 (试件, 指标, 修订号, 值)。 |
| 底表 | `base_values` | 每个 (试件, 指标) 一行，取**修订号最高**的申报值；补页未带的指标列沿用旧申报。 |

> 补页只更正部分指标时，文件里只出现它带的指标列即可；模板中登记但文件里没有的列
> 不会产生申报，也不会清掉底表上的旧值。

## 业务规则

* **上传**：选模板 + CSV。表内需有模板登记的试件号列、报告修订号列和至少一个指标列。
  值为空表示本次不申报该指标。
* **取值**：底表按 (试件, 指标) 选修订号最高的申报；修订号相同则取最近一次上传。
  低修订号的申报仍留在档案中，但不影响底表。
* **改选模板**：批次档案记录原始模板与当前模板。改选时用新模板重新解释**原始字节**：
  旧申报被替换，新旧指标重新取值；失去所有申报来源的指标从底表撤下（若还有更低修订号的
  申报，则回落到该申报）。原件字节与 `original_template_id` 始终保留，改选历史可追溯。
* **冲突拒绝**：若上传或改选会使同一 (试件, 指标, 修订号) 出现**不同值**，整笔操作被拒绝
  （HTTP 409），页面列出冲突的取值与所有来源（批次/行号）。原档与底表保持原状 ——
  校验与写入在同一个事务中完成，冲突时整体回滚。
  同一修订号下的**相同值**来自多个批次是允许的，视为相互印证。
* **试件查询**：按试件号查看各指标现值、所用修订号、来源批次，以及所有给出该值的
  同修订批次（含行号、上传时间、当前/原始模板）。

## 本地运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
flask --app app seed-demo      # 可选：写入示例指标/模板/批次
flask --app app run --port 5000
# 或
python3 app.py                 # 监听 127.0.0.1:5000，HOST/PORT 可覆盖
```

数据库默认位于 `./data/warehouse.db`，可用环境变量 `DATABASE` 指定。
首次启动会自动建表（`AUTO_INIT_DB=1`）。

常用命令：

```bash
flask --app app init-db        # 只建表
flask --app app seed-metadata  # 写入示例指标与模板
flask --app app seed-demo      # 写入示例指标、模板和三个批次
```

## Docker Compose

```bash
docker compose up --build -d
# 打开 http://localhost:8000
```

数据保存在命名卷 `warehouse-data`（容器内 `/app/data`）。可选环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `APP_PORT` | `8000` | 宿主机映射端口 |
| `SECRET_KEY` | `dev-only-change-me` | 生产环境请务必覆盖 |

常用操作：

```bash
docker compose exec warehouse flask --app app seed-demo
docker compose exec warehouse flask --app app seed-metadata
docker compose down            # 停止（保留数据卷）
```

## 测试

```bash
python3 -m pytest -q
```

测试覆盖：修订号取值、补页沿用、跨批次同修订同值/异值、文件内冲突、改选模板后的
重新取值与撤下/回落、原始字节保留、试件查询输出等。

## 目录

```
app.py                    # 入口
warehouse/
  __init__.py             # 应用工厂
  db.py / schema.sql      # SQLite 连接与建表
  core.py                 # CSV 解析、模板映射、冲突检测、底表重算
  views.py                # 路由
  cli.py                  # init-db / seed-* 命令
  templates/ static/      # Jinja 页面与样式
tests/                    # pytest
Dockerfile, docker-compose.yml
```
