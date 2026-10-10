# RelayFence

**让不同 Agent 软件共享状态、领取任务，并在提交结果前检查输入是否过期。**

[English](README.md) · [角色与模型配置](docs/roles-and-models.md) · [三端接入](docs/clients.md) · [接口协议](docs/protocol.md)

实际协作中，问题往往出在交接处：Codex 更新了接口约定，另一个 Agent 仍按旧约定写测试；一个任务同时被两个客户端领取；掉线的 Agent 恢复后覆盖了接手者的结果；请求超时后重试，导致重复发布。

RelayFence 把这些交接变成可检查的操作。不同软件连接同一个协调服务，各自保留模型、工具和对话，只共享任务、输入版本、产物与事件记录。

当前是 **v0.2 alpha**：适合一台可信机器上的协作工作流。包含 MCP/HTTP 服务、SQLite 持久化、Python 客户端、stdio 桥接、角色与模型配置面板、显式本地命令适配器和故障演示。核心服务与演示不调用模型，不需要模型 API Key。

![实际演示数据的查看面板](docs/assets/console.png)

## 已实现什么

| 能力 | 实际行为 |
| --- | --- |
| 共享状态版本控制 | 更新时携带预期版本，拒绝覆盖已经变化的数据。 |
| 原子领取任务 | 多个进程竞争同一个任务，只允许一个持有有效租约。 |
| 租约与 fencing | 每次接管递增版本号；旧租约不能续租或提交。 |
| 输入快照 | 领取时同时记录输入内容及版本，提交时重新校验。 |
| 产物依赖 | 上游状态变化后，下游产物查询结果显示过期，支持传递依赖。 |
| 幂等重试 | 同一请求 ID、同一内容返回原始成功回执，不重复写入。 |
| 故障恢复 | 已提交数据保留，未提交事务回滚，事件和回执随数据库恢复。 |
| 角色与模型选择 | 每个角色独立选择 Agent 实例、模型及其支持的参数，并设置工作说明和交付要求。 |
| 实例身份 | 每个实例使用独立凭证；冒用实例名、跨项目访问、越权修改配置会被拒绝。 |
| 执行配置快照 | 领取时固定角色与模型配置；执行中的任务不受之后的角色修改影响。 |
| 本地命令执行 | 显式 worker 命令把模型传给本地适配器，自动续租，通过原有接口提交结果。 |
| 控制台 | 配置角色、Agent 与模型，分派任务，查看负责人、租约、产物及执行记录。 |

这些约束作用于 RelayFence 的提交接口。并行改代码时，仍应让各个 Agent 使用独立 Git worktree，再以提交 SHA 或测试报告交接。

## 为每个角色选择 Agent 和模型

启动服务并连接控制台后：

1. 展开 **Agent instances & model catalogues**，添加具体实例、软件名称、接入模式和它支持的模型；可以为每个模型填写推理强度等参数的可选值。
2. 保存实例，点击 **Issue / rotate credential** 下载它的独立凭证，交给对应客户端使用。
3. 在 **Role bindings** 自定义角色，依次选择 **Agent 实例 → 模型 → 参数**，填写工作说明、能力要求和交付要求。
4. 在 **Assign work** 创建任务并选择角色。已有可领取任务可通过 **Reassign** 改派；运行中的任务需要先释放租约。

同一软件可以注册多个实例，分别使用不同模型。模型目录由你根据实际账号和适配器能力配置，本版不自动查询供应商。外部 MCP 会话需要在对应软件里设置模型；本地命令模式由 `relayfence worker` 将选择传给你配置的适配器。当前没有内置 Codex/ZCode/DSH 自动启动器。

执行记录分别保存请求的模型和执行者上报的模型；未知模型显示未知，上报不等于供应商认证。角色交付要求是协作说明，不是自动判断代码正确性的规则。完整配置与恢复步骤见[角色与模型指南](docs/roles-and-models.md)。

先运行一个真实 HTTP + 子进程的无 Key 演示：

~~~bash
uv run python examples/role_models.py
~~~

该演示用两个明确标注的 demo 模型标签验证选择传递与任务交接，不会调用真实模型。

![实际控制台的角色、Agent 和模型选择](docs/assets/roles-models.png)

## 先运行一次真实演示

准备 Python 3.11+ 与 [uv](https://docs.astral.sh/uv/getting-started/installation/)：

~~~bash
git clone https://github.com/shyangar/relayfence.git
cd relayfence
uv sync --locked
uv run relayfence demo
~~~

演示会启动真实服务进程和三个 MCP 协议客户端，验证：

1. 并发抢任务只有一个成功。
2. 接口约定在执行中变化，旧结果被拒绝。
3. 重试已成功提交的请求，产物版本保持不变。
4. 上游再次变化，已有产物显示过期。
5. 租约超时后接管，旧 Agent 的迟到提交被 fencing 拒绝。
6. 强制结束服务进程再启动，回执、版本和事件仍然存在。

每次运行都会创建独立的 `.relayfence/demo-*` 目录并保存 `report.json`，不覆盖旧数据。末尾会打印查看该次演示的启动命令。

**这是真实协议联调，未冒充 Codex、ZCode 或 DSH 的真实模型会话。三款软件的实际端到端联调情况见接入文档。**

## 启动正式使用的本地服务

~~~bash
uv run relayfence init
uv run relayfence serve
~~~

保留这个终端运行。查看面板在 **http://127.0.0.1:7433/**，MCP 地址为 **http://127.0.0.1:7433/mcp**。

用编辑器打开本地 `.relayfence/access.token`，将值粘贴到面板的 Access token 输入框；演示数据库使用 `demo` 项目，普通数据库默认使用 `default`。令牌只保存在当前页面内存。

在另一个终端生成对应客户端配置：

~~~bash
uv run relayfence config codex
uv run relayfence config zcode
uv run relayfence config dsh
~~~

把输出合并到对应软件的现有配置，再重新连接 MCP。命令只打印配置，不改动已有设置。配置中的 stdio 桥接器会连接同一个服务，不会为每个软件各建一套独立状态。

如果偏好直接 HTTP 连接，请使用[三端接入说明](docs/clients.md)中的方法。分角色执行时，用 `config <client> --token-file <实例凭证文件>` 生成配置；该实例的 actor 必须与注册 ID 相同。环境变量 `RELAYFENCE_TOKEN` 会覆盖文件配置，注意不要继续传入管理员令牌。

## 交给 Agent 的协作约定

可参考 [agent-instructions.md](examples/agent-instructions.md)：

> 在项目 shop 内协作。先读取共享输入，创建任务时声明全部输入键，再领取任务。保存返回的 fence 和 lease_token，并及时续租。使用领取时的输入快照工作。完成时通过 task_complete 发布产物。遇到 stale_input，释放任务、重新领取并重做受影响部分；遇到 lease_lost 或 lease_expired，停止使用旧租约。超时重试时必须保留原请求 ID 和内容。

每个 MCP 工具都将参数放在 `request` 对象中。写操作额外需要 `actor` 和 `request_id`。具体例子和错误处理见[接口协议](docs/protocol.md)，也可以直接运行 [Python 示例](examples/python_handoff.py)。

## 维护与使用边界

仓库包含进程竞争、事务中断、响应丢失、传递依赖、HTTP 鉴权、两代 MCP 协议和 stdio 桥接测试，以及跨平台 CI。依赖通过 `uv.lock` 锁定。

~~~bash
uv run pytest --cov=relayfence --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv build
~~~

当前通过源码或 GitHub Release 安装，尚未发布到 PyPI。备份使用 `relayfence backup`，操作方法见[运维说明](docs/operations.md)。

管理员令牌可以访问整个服务。实例凭证限制到所属项目和实例 actor，并禁止修改角色、实例或任务分派；同项目仍是可信共享工作区。未注册的旧式 actor 标签保留兼容行为。服务只监听本机回环地址。数据库、备份和令牌应放在当前用户控制的目录中。

MCP/HTTP 服务不会自行启动其他软件；本地 worker 只执行操作员显式配置的一项任务。RelayFence 不自动判断代码是否正确、不推断未声明的依赖，也不能阻止绕过服务直接写文件。事件采用游标轮询。跨机器高可用、自动调度、多租户隔离和外部操作防重复不在本版范围内。

从 v0.1 升级时先做备份并停止旧进程；v0.2 首次打开数据库会事务化升级到 schema 2，保留旧任务、fence 和请求回执。升级后的数据库不能再交给 v0.1 运行。

本项目与 FlowTrial 完全独立：单独仓库、包、依赖、测试和版本，不引用 FlowTrial 内部代码。
