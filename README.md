# 电梯与自动扶梯巡检和事件响应

这是一个只使用Python标准库和SQLite的模块化原型项目，默认端口为`8336`。领域对象包括设备、检验、维保、困人报警、救援任务、整改证据和恢复许可。`app.py`只负责参数解析、依赖组装和服务生命周期，业务状态机与约束集中在`src/rules.py`。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、领域异常、身份解析和实体数据结构。
- `src/rules.py`：状态机、角色权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁、审计和幂等键。
- `src/service.py`：用例编排、离线记录合并、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、JSON解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8336
```

服务启动时自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。初始化服务不需要单独命令，首次启动即可访问：

```bash
curl http://127.0.0.1:8336/health
```

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。## 核心流程

创建设备后安排检验、维保和困人报警；报警派发救援任务，完成后才能解决。整改证据通过复核后关闭，恢复运行许可必须基于有效的检验和已关闭整改。

## 派单容量与排队

困人报警集中爆发时，救援任务按人力容量自动派单或排队：

- 新增`rescuer`（救援人员）对象，`name`唯一，状态为`on_duty`/`off_duty`，可通过`deactivate`/`go_off_shift`停用或换班。
- 每人同时只带一个未结束任务（`dispatched`/`on_site`）。创建`rescue_job`时自动派给有空的在岗人员；都没空则进入排队（`queued`）。
- 报警可带`level`（1-5，越大越急）。排队按`(优先级高, 创建早)`排序：高等级插到队首，被挤的同等级任务保持原有先后（稳定FIFO）。
- 人员停用或换班时，其未结束任务改派给其他有空的人；都没空则退回排队。`arrive`登记的`arrived_at`到场时间在改派中保留。
- 设备状态更新（`suspend`/`out_of_service`/`return_to_service`）后立即重算该设备未开始的派单（`queued`/`dispatched`），在场（`on_site`）任务继续。
- 任务完成或中止后自动释放人力，队首任务补位。
- 两名调度员为同一报警提交派单时，后到的会看到当前归属并返回409冲突（乐观锁 + 报警级活跃任务唯一校验）。

## 规则重点

- 同一设备编号不能重复创建；同一设备和故障代码不能同时存在多个未关闭报警。
- 组件更换维保必须填写`part_serial`。
- 恢复许可受设备状态、通过检验和未关闭整改共同限制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。
