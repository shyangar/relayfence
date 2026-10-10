# RelayFence

**让不同 Agent 软件共享状态、领取任务，并在提交结果前检查输入是否过期。**

[English](README.md) · [三端接入](docs/clients.md) · [接口协议](docs/protocol.md) · [设计与边界](docs/architecture.md)

实际协作中，问题往往出在交接处：Codex 更新了接口约定，另一个 Agent 仍按旧约定写测试；一个任务同时被两个客户端领取；掉线的 Agent 恢复后覆盖了接手者的结果；请求超时后重试，导致重复发布。

RelayFence 把这些交接变成可检查的操作。不同软件连接同一个协调服务，各自保留模型、工具和对话，只共享任务、输入版本、产物与事件记录。

当前是 **v0.1 alpha**：适合一台可信机器上的协作工作流。包含可运行的 MCP/HTTP 服务、SQLite 持久化、Python 客户端、stdio 桥接、查看面板和故障演示。核心服务与演示不调用模型，不需要模型 API Key。

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
| 查看面板 | 查看任务负责人、租约、产物版本、过期原因与事件记录。 |

这些约束作用于 RelayFence 的提交接口。并行改代码时，仍应让各个 Agent 使用独立 Git worktree，再以提交 SHA 或测试报告交接。

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

如果偏好直接 HTTP 连接，请使用[三端接入说明](docs/clients.md)中的方法。不同客户端必须约定同一个项目名，以及各自不同的 actor 名称。

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

服务令牌可以访问整个实例，项目名是数据命名空间，不能当权限隔离；actor 名是调用者填写的记录标签。v0.1 只监听本机回环地址。数据库、备份和令牌应放在当前用户控制的目录中。

RelayFence 不自动启动其他软件、不自动判断代码是否正确、不推断未声明的依赖，也不能阻止绕过服务直接写文件。事件采用游标轮询。跨机器高可用、自动调度、权限分级和外部操作防重复不在本版范围内。

本项目与 FlowTrial 完全独立：单独仓库、包、依赖、测试和版本，不引用 FlowTrial 内部代码。
