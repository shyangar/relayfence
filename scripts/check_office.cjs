/* Isolated browser check: mocked local discovery/ledger; never starts model software.
 * RELAYFENCE_PLAYWRIGHT points to the playwright module, RELAYFENCE_BROWSER to Chromium.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { chromium } = require(process.env.RELAYFENCE_PLAYWRIGHT || "playwright");
const positions = {
  lead: { label: "组长", instructions: "协调交付", deliverables: ["验收"] },
  backend: { label: "后端", instructions: "实现服务", deliverables: ["代码"] },
};
const catalogue = [
  {
    id: "codex",
    label: "Codex",
    installed: true,
    status: "ready",
    message: "从本地模型缓存检测",
    providers: [
      {
        id: "openai",
        label: "OpenAI 账户",
        agent_id: "local-codex",
        authenticated: true,
        auth: {
          kind: "account",
          authenticated: true,
          credential_env: "OPENAI_API_KEY",
          source: "vendor",
          stored: false,
          masked: null,
          message: "已使用本机 Codex 登录",
          state: "authenticated",
          label: "已登录",
        },
        models: [
          {
            id: "detected-one",
            reasoning_efforts: ["low", "high"],
            parameters: { reasoning_effort: ["low", "high"] },
          },
          {
            id: "detected-two",
            reasoning_efforts: ["medium"],
            parameters: { reasoning_effort: ["medium"] },
          },
        ],
      },
    ],
  },
  {
    id: "zcode",
    label: "ZCode",
    installed: false,
    status: "not_installed",
    message: "未找到软件",
    providers: [],
  },
  {
    id: "dsh",
    label: "DSH",
    installed: true,
    status: "ready",
    providers: [
      {
        id: "local-provider",
        label: "iSoft",
        agent_id: "local-dsh",
        authenticated: true,
        auth: {
          kind: "api_key",
          authenticated: true,
          credential_env: "ISOFT_API_KEY",
          source: "environment",
          stored: false,
          masked: null,
          message: "已从环境变量读取 ISOFT_API_KEY",
          state: "authenticated",
          label: "已配置",
        },
        models: [
          {
            id: "detected-dsh",
            reasoning_efforts: [],
            parameters: {},
          },
        ],
      },
      {
        id: "second-provider",
        label: "Local",
        agent_id: "local-dsh-second",
        authenticated: true,
        auth: {
          kind: "none",
          authenticated: true,
          credential_env: null,
          source: "runtime",
          stored: false,
          masked: null,
          message: "本地供应商无需密钥",
          state: "authenticated",
          label: "可用",
        },
        models: [
          {
            id: "second-provider-model",
            reasoning_efforts: ["high"],
            parameters: { reasoning_effort: ["high"] },
          },
        ],
      },
    ],
  },
  {
    id: "opencode",
    label: "OpenCode",
    installed: true,
    status: "ready",
    providers: [
      {
        id: "isoft",
        label: "iSoft",
        agent_id: "local-opencode",
        authenticated: false,
        auth: {
          kind: "api_key",
          authenticated: false,
          credential_env: "ISOFT_API_KEY",
          source: null,
          stored: false,
          masked: null,
          message: "缺少 ISOFT_API_KEY，请配置密钥",
          state: "missing_key",
          label: "缺少密钥",
        },
        models: [
          {
            id: "opencode-isoft-model",
            reasoning_efforts: [],
            parameters: {},
          },
        ],
      },
    ],
  },
];
const agentSummaries = {
  codex: {
    state: "ready",
    label: "可用",
    message: "1 / 1 个供应商已就绪",
    authenticated: true,
    ready_providers: 1,
    provider_count: 1,
  },
  zcode: {
    state: "not_installed",
    label: "未安装",
    message: "未找到软件",
    authenticated: false,
    ready_providers: 0,
    provider_count: 0,
  },
  dsh: {
    state: "ready",
    label: "可用",
    message: "2 / 2 个供应商已就绪",
    authenticated: true,
    ready_providers: 2,
    provider_count: 2,
  },
  opencode: {
    state: "missing_key",
    label: "缺少密钥",
    message: "请为需要 API Key 的供应商配置密钥",
    authenticated: false,
    ready_providers: 0,
    provider_count: 1,
  },
};
let project = "",
  workspace = "",
  authorized = false,
  config = { revision: 0, groups: [], positions };
const existingMember = (
  id,
  name,
  position,
  relationship,
  software,
  provider,
  model,
  agent_id,
) => ({
  id,
  name,
  position,
  avatar: 2,
  gender: "female",
  relationship,
  agent_id,
  provider,
  model,
  parameters: software === "codex" ? { reasoning_effort: "low" } : { provider },
  session_id: null,
  fallback_models: [],
  skills: [],
});
const mockExistingGroups = [
  {
    project: "other-codex",
    project_name: "已有 Codex 项目",
    project_path: "D:\\projects\\other-codex",
    revision: 3,
    updated_at: 1760000300,
    member_count: 2,
    in_office: false,
    group: {
      id: "existing-codex",
      name: "Codex · 项目团队",
      software: "codex",
      members: [
        existingMember(
          "existing-codex-lead",
          "项目组长",
          "lead",
          "leader",
          "codex",
          "openai",
          "detected-one",
          "local-codex",
        ),
        existingMember(
          "existing-codex-backend",
          "开发成员",
          "backend",
          "independent_session",
          "codex",
          "openai",
          "detected-one",
          "local-codex",
        ),
      ],
    },
  },
  {
    project: "other-dsh",
    project_name: "已有 DSH 项目",
    project_path: "D:\\projects\\other-dsh",
    revision: 2,
    updated_at: 1760000200,
    member_count: 2,
    in_office: false,
    group: {
      id: "existing-dsh",
      name: "DSH · 项目团队",
      software: "dsh",
      members: [
        existingMember(
          "existing-dsh-lead",
          "项目组长",
          "lead",
          "leader",
          "dsh",
          "local-provider",
          "detected-dsh",
          "local-dsh",
        ),
        existingMember(
          "existing-dsh-backend",
          "开发成员",
          "backend",
          "independent_session",
          "dsh",
          "local-provider",
          "detected-dsh",
          "local-dsh",
        ),
      ],
    },
  },
];
let tasks = [],
  observations = [],
  dispatched,
  saved,
  lastMutation;
let guidance = [],
  groupChatRequests = 0,
  mockDecisions = [],
  projectSessions = [],
  mockMeetings = [],
  mockAssignments = [];
const groupLogs = [
  {
    id: 2,
    project: "local-project-check",
    group_id: "codex-team",
    member_id: "member-one",
    task_id: "browser-task/1",
    kind: "task.failed",
    level: "error",
    message: "任务执行失败",
    details: { code: "adapter_failed" },
    created_at: 1760000200,
  },
  {
    id: 1,
    project: "local-project-check",
    group_id: "codex-team",
    member_id: "member-one",
    task_id: "browser-task/1",
    kind: "task.claimed",
    level: "info",
    message: "成员开始执行",
    details: { fence: 1 },
    created_at: 1760000100,
  },
];
const savedProjects = new Map();
let runtimeStatus = "healthy", overviewOverride = null, ledgerUnavailable = false, officeReads = 0;
let statusRequests = 0;
const localStatus = () => ({
  project,
  workspace,
  catalogue: ++statusRequests === 1 ? [] : catalogue,
  discovering: statusRequests === 1,
});
const agentInventory = () => ({
  agents: catalogue.map((entry) => ({
    ...entry,
    auth: agentSummaries[entry.id],
  })),
  discovering: false,
  scanned_at: 1760000000,
});
const providerOf = (software, provider) =>
  catalogue
    .find((entry) => entry.id === software)
    ?.providers.find((item) => item.id === provider);
const server = http.createServer(async (req, res) => {
  let raw = "";
  for await (const part of req) raw += part;
  const body = raw ? JSON.parse(raw) : {};
  res.setHeader("Content-Type", "application/json");
  const cookie = (req.headers.cookie || "")
    .split(/;\s*/)
    .find((part) => part.startsWith("relayfence_local="));
  const hasSession = cookie?.split("=")[1] === "browser-check";
  const localBrowser = req.headers["x-relayfence-local"] === "1" &&
    req.headers["sec-fetch-site"] === "same-origin" &&
    (req.method === "GET" || req.headers.origin === `http://${req.headers.host}`);
  if (req.url.startsWith("/local/")) {
    if (req.url === "/local/session") {
      authorized = body.code === "browser-check";
      if (!authorized) {
        res.statusCode = 401;
        res.end(JSON.stringify({ error: { message: "配对链接已失效" } }));
        return;
      }
      res.setHeader(
        "Set-Cookie",
        "relayfence_local=browser-check; Path=/; HttpOnly; SameSite=Strict",
      );
      res.end(JSON.stringify({ ok: true }));
      return;
    }
    if (!hasSession && !localBrowser) {
      res.statusCode = 401;
      res.end(JSON.stringify({ error: { message: "Unauthorized" } }));
      return;
    }
    if (req.url === "/local/project-sessions") {
      res.end(JSON.stringify({ project, sessions: projectSessions }));
      return;
    }
    if (req.url === "/local/runtime") {
      res.end(JSON.stringify({project, health: {status: runtimeStatus}, capacity: {
        limit: 2, active_total: 1, active: [],
        waiting: [{task_id: "queued-task", reason: "capacity_full"}], workspace_limit: 1,
      }}));
      return;
    }
    if (req.url === "/local/project-session-reset") {
      const saved = projectSessions.find((v) => v.runtime === body.runtime);
      assert.equal(saved.session_id, body.session_id);
      assert.equal(saved.updated_at, body.updated_at);
      projectSessions = projectSessions.filter(
        (v) => v.runtime !== body.runtime,
      );
      res.end(JSON.stringify({ project, sessions: projectSessions }));
      return;
    }
    if (req.url === "/local/decision-respond") {
      const decision = mockDecisions.find(
        (d) => d.decision_id === body.decision_id,
      );
      assert.equal(body.expected_revision, decision.revision);
      decision.status = "answered";
      decision.revision++;
      decision.answer = body.answer;
      res.end(
        JSON.stringify({
          status: "answered",
          decision_id: decision.decision_id,
        }),
      );
      return;
    }
    if (req.url === "/local/meeting-control") {
      mockMeetings[0].paused = body.action === "pause";
      tasks[0].status = mockMeetings[0].paused ? "paused" : "ready";
      tasks[0].revision++;
      res.end(JSON.stringify({ meeting: mockMeetings[0], stopping: false }));
      return;
    }
    if (req.url === "/local/task-control") {
      const task = tasks.find((t) => t.task_id === body.task_id);
      assert.equal(task.revision, body.expected_revision);
      task.status = {
        pause: "paused",
        resume: "ready",
        cancel: "cancelled",
        retry: "ready",
      }[body.action];
      task.revision++;
      res.end(JSON.stringify({ task, stopping: false }));
      return;
    }
    if (req.url === "/local/member-chat") {
      const limit = Math.min(100, body.limit || 100);
      const before = body.before || 0;
      const messages = guidance
        .filter((message) => !before || message.created_at < before)
        .slice(-limit);
      res.end(
        JSON.stringify({
          tasks,
          messages,
          has_more: messages.length === limit && guidance.length > limit,
          next_before: messages[0]?.created_at ?? null,
        }),
      );
      return;
    }
    if (req.url === "/local/group-chat") {
      groupChatRequests++;
      const groupId = body.group_id || config.groups[0]?.id || "";
      const rows = [
        ...groupLogs.map((entry) => ({
          id: "log-" + entry.id,
          log_id: entry.id,
          role: entry.kind === "task.failed" ? "system" : "system",
          kind: entry.kind,
          content: entry.message,
          level: entry.level,
          member_id: entry.member_id,
          member_name: "成员一",
          task_id: entry.task_id,
          task_title: "浏览器测试任务",
          created_at: entry.created_at,
        })),
        ...guidance.map((message, index) => ({
          id: "guidance-" + index,
          log_id: 100 + index,
          role: "user",
          kind: "guidance",
          content: message.content,
          status: message.status,
          member_id: config.groups[0]?.members[0]?.id || "",
          member_name: config.groups[0]?.members[0]?.name || "成员",
          task_id: tasks[0]?.task_id || "",
          task_title: tasks[0]?.title || "",
          created_at: message.created_at,
        })),
      ].sort((a, b) => a.log_id - b.log_id);
      const limit = Math.min(100, body.limit || 100);
      let page = rows;
      if (body.after) page = page.filter((entry) => entry.log_id > body.after);
      if (body.before)
        page = page.filter((entry) => entry.log_id < body.before);
      const hasMore = !body.after && page.length > limit;
      page = body.after ? page.slice(0, limit) : page.slice(-limit);
      res.end(
        JSON.stringify({
          project: body.project,
          group: {
            id: groupId,
            name: config.groups[0]?.name || groupId,
            software: config.groups[0]?.software || "codex",
            member_count: config.groups[0]?.members.length || 0,
          },
          messages: page,
          has_more: hasMore,
          next_before: hasMore ? page[0]?.log_id : null,
          next_after: page.at(-1)?.log_id || body.after || 0,
        }),
      );
      return;
    }
    if (req.url === "/local/member-guide") {
      guidance.push({
        id: "m" + (guidance.length + 1),
        role: "user",
        content: body.message,
        status: "applied",
        detail: "",
        created_at: 1760000000 + guidance.length,
      });
      res.end(JSON.stringify({ message: guidance.at(-1), replayed: false }));
      return;
    }
    if (req.url === "/local/agents") {
      res.end(JSON.stringify(agentInventory()));
      return;
    }
    if (req.url === "/local/agent-credential") {
      const provider = providerOf(body.software, body.provider);
      assert.ok(provider, "credential target exists");
      const masked = "••••••••" + String(body.value).slice(-4);
      provider.authenticated = true;
      provider.auth = {
        ...provider.auth,
        authenticated: true,
        source: "database",
        stored: true,
        masked,
        message: "已使用 RelayFence 加密保存的密钥",
        state: "authenticated",
        label: "已配置",
      };
      const agent = catalogue.find((entry) => entry.id === body.software);
      agentSummaries[body.software] = {
        state: "ready",
        label: "可用",
        message: "1 / " + agent.providers.length + " 个供应商已就绪",
        authenticated: true,
        ready_providers: 1,
        provider_count: agent.providers.length,
      };
      res.end(JSON.stringify({ saved: true, masked }));
      return;
    }
    if (req.url === "/local/agent-credential-delete") {
      const provider = providerOf(body.software, body.provider);
      assert.ok(provider, "credential target exists");
      provider.authenticated = false;
      provider.auth = {
        ...provider.auth,
        authenticated: false,
        source: null,
        stored: false,
        masked: null,
        message: "缺少 " + provider.auth.credential_env + "，请配置密钥",
        state: "missing_key",
        label: "缺少密钥",
      };
      agentSummaries[body.software] = {
        state: "missing_key",
        label: "缺少密钥",
        message: "请为需要 API Key 的供应商配置密钥",
        authenticated: false,
        ready_providers: 0,
        provider_count: catalogue.find((entry) => entry.id === body.software)
          .providers.length,
      };
      res.end(JSON.stringify({ deleted: true }));
      return;
    }
    if (req.url === "/local/refresh") {
      res.end(JSON.stringify(localStatus()));
      return;
    }
    if (req.url === "/local/projects") {
      if (req.method === "POST")
        savedProjects.set(body.path, {
          id: body.path,
          name: body.path.split(/[\\/]/).pop(),
          path: body.path,
          groups: 0,
          tasks: 0,
          exists: true,
        });
      res.end(JSON.stringify({ projects: [...savedProjects.values()] }));
      return;
    }
    if (req.url === "/local/browse") {
      res.end(
        JSON.stringify({
          path: "D:\\projects\\existing-project",
          parent: "D:\\projects",
          directories: [],
          truncated: false,
        }),
      );
      return;
    }
    if (req.url === "/local/connect") {
      workspace = body.path;
      project = "local-project-check";
    }
    res.end(JSON.stringify(localStatus()));
    return;
  }
  if (req.url.startsWith("/v1/")) {
    if (req.url === "/v1/office_get") {
      officeReads++;
      if (ledgerUnavailable) {
        res.statusCode = 503;
        res.end(JSON.stringify({ok: false, error: {code: "busy", message: "Test ledger unavailable"}}));
        return;
      }
    }
    if (!hasSession && !localBrowser) {
      res.statusCode = 401;
      res.end(JSON.stringify({ error: { message: "Unauthorized" } }));
      return;
    }
    if (req.url === "/v1/capabilities") {
      // Truthful surface report for the office capability probe.
      res.end(
        JSON.stringify({
          ok: true,
          data: {
            operations: [
              "office_get",
              "project_overview",
              "office_put",
              "office_activity",
              "mcp_list",
              "skill_list",
              "office_snapshot_list",
              "tasks_list",
              "task_inspect",
              "task_review",
              "agents_list",
              "quota_list",
              "events_list",
              "group_logs_list",
              "runners_list",
              "meetings_list",
              "decisions_list",
              "decision_respond",
              "meeting_revision",
              "office_dispatch",
              "team_groups_list",
              "task_delete",
            ],
          },
        }),
      );
      return;
    }
    let data;
    switch (req.url.slice(4)) {
      case "project_overview": {
        const eligible = tasks.filter(t => t.status !== "cancelled");
        const completed = tasks.filter(t => (t.display_status || t.status) === "done" && t.result_freshness?.fresh !== false).length;
        const counts = {};
        for (const task of tasks) counts[task.display_status || task.status] = (counts[task.display_status || task.status] || 0) + 1;
        data = overviewOverride || {counts, progress: {eligible: eligible.length, completed, total: tasks.length,
          cancelled: tasks.length - eligible.length, stale: 0,
          percent: eligible.length ? Math.round(completed * 100 / eligible.length) : 0}, attention: [], attention_total: 0};
        break;
      }
      case "office_activity":
        data = {
          members: config.groups.flatMap((g) =>
            g.members.map((m) => {
              const task = tasks.find(
                (t) =>
                  t.role_id === "office/" + m.id && t.status !== "cancelled",
              );
              return {
                member_id: m.id,
                group_id: g.id,
                task: task || null,
                messages: task
                  ? [
                      ...guidance.map((x) => ({
                        role: x.role,
                        text: x.content,
                        status: x.status,
                      })),
                      ...(task.status === "done"
                        ? [
                            {
                              role: "assistant",
                              text: "已完成本轮任务，保留前轮成果，等待下一轮修改。",
                            },
                          ]
                        : []),
                    ]
                  : [],
              };
            }),
          ),
        };
        break;
      case "decisions_list":
        data = { decisions: mockDecisions };
        break;
      case "meetings_list":
        data = { meetings: mockMeetings, assignments: mockAssignments };
        break;
      case "office_get":
        data = config;
        break;
      case "team_groups_list": {
        const current = config.groups.map((group) => ({
          project,
          project_name: "当前项目",
          project_path: workspace,
          revision: config.revision,
          updated_at: 1760000400,
          group,
          member_count: group.members.length,
          in_office: true,
        }));
        const all = [...mockExistingGroups, ...current];
        const groups = all.filter(
          (entry) =>
            (!body.project_filter || entry.project === body.project_filter) &&
            (!body.query ||
              [entry.project_name, entry.group.name, entry.group.software]
                .join(" ")
                .toLowerCase()
                .includes(body.query.toLowerCase())),
        );
        const projects = [...new Set(all.map((entry) => entry.project))].map(
          (id) => {
            const entry = all.find((item) => item.project === id);
            return {
              project: id,
              project_name: entry.project_name,
              project_path: entry.project_path,
              revision: entry.revision,
              group_count: all.filter((item) => item.project === id).length,
              updated_at: entry.updated_at,
            };
          },
        );
        data = {
          groups,
          projects,
          total: groups.length,
          truncated: false,
          current_project: project,
          server_time: 1760000500,
        };
        break;
      }
      case "task_inspect": {
        const task = tasks.find((t) => t.task_id === body.task_id);
        data = {
          task: { inputs: [], outputs: [], ...task },
          attempts: [
            {
              sequence: 1,
              fence: 1,
              owner: "mock-builder",
              started_at: 1760000000,
              ended_at: 1760000100,
              status: "completed",
              execution: { requested: { model: "detected-two" } },
              outcome: {},
            },
          ],
          reviews: task.review
            ? [{ fence: 1, status: task.review.status, note: "", evidence: [] }]
            : [],
          handoffs: [],
          next_before: null,
        };
        break;
      }
      case "task_review": {
        const task = tasks.find((t) => t.task_id === body.task_id);
        assert.equal(task.revision, body.expected_revision);
        assert.ok(body.note.trim());
        task.review = {
          status: body.action === "approve" ? "approved" : "rejected",
          note: body.note,
          evidence: body.evidence,
        };
        task.status = body.action === "approve" ? "done" : "paused";
        task.display_status = task.status;
        task.revision++;
        data = task;
        break;
      }
      case "tasks_list":
        data = { tasks };
        break;
      case "agents_list":
        data = { agents: [] };
        break;
      case "quota_list":
        data = { observations };
        break;
      case "events_list":
        data = { events: [], next_cursor: 0 };
        break;
      case "group_logs_list": {
        const groupId = config.groups[0]?.id || "codex-team";
        const filtered = groupLogs
          .map((entry) => ({ ...entry, group_id: groupId }))
          .filter(
            (entry) =>
              (!body.group_id || entry.group_id === body.group_id) &&
              (!body.level || entry.level === body.level) &&
              (!body.query ||
                entry.message.toLowerCase().includes(body.query.toLowerCase())),
          );
        const start = body.before
          ? filtered.findIndex((entry) => entry.id < body.before)
          : 0;
        const page = filtered.slice(
          Math.max(0, start),
          Math.max(0, start) + (body.limit || 50),
        );
        data = {
          logs: page,
          next_cursor: page.at(-1)?.id ?? null,
          has_more: Math.max(0, start) + page.length < filtered.length,
          analysis: {
            total_logs: filtered.length,
            by_level: {
              debug: 0,
              info: filtered.filter((entry) => entry.level === "info").length,
              warning: 0,
              error: filtered.filter((entry) => entry.level === "error").length,
            },
            by_kind: {},
            by_member: [],
            by_group: [...new Set(filtered.map((entry) => entry.group_id))].map(
              (group_id) => ({
                group_id,
                count: filtered.filter((entry) => entry.group_id === group_id)
                  .length,
              }),
            ),
            tasks: {
              total: 1,
              status: { running: 0, done: 0, failed: 1 },
              avg_done_seconds: null,
            },
            recent_alerts: [],
          },
        };
        break;
      }
      case "skill_list":
        data = {capabilities_version:1, workspace, skills:[{software:"codex",label:"Codex",skills:[{name:"test-skill",description:"Test capability long description",path:"C:/skills/test",origin:"user",files:1}]}], mcp:[{software:"codex",label:"Codex",selection_supported:true,servers:[{id:"docs",name:"docs",origin:"user",enabled:true,selectable:true,transport:"stdio",connection:"not_probed"}]},{software:"dsh",label:"DeepSeek Harness",selection_supported:false,servers:[]}]};
        break;
      case "runners_list":
        data = { runners: [{ agent_id: "local-codex", online: true }] };
        break;
      case "office_put":
        saved = body;
        config = {
          revision: config.revision + 1,
          groups: body.groups,
          positions,
        };
        data = config;
        break;
      case "office_snapshot_list":
        data = { snapshots: [] };
        break;
      case "task_delete":
        data = { deleted: true };
        break;
      case "meeting_revision": {
        const parent = mockMeetings.find((m) => m.id === body.meeting_id);
        const meeting = {
          ...parent,
          id: body.briefing_id,
          root_id: parent.root_id || parent.id,
          parent_id: parent.id,
          iteration: (parent.iteration || 1) + 1,
          changes: body.changes,
          stage: "planning",
        };
        mockMeetings.unshift(meeting);
        data = { meeting, tasks: [] };
        break;
      }
      case "office_dispatch":
        dispatched = body;
        lastMutation = body.request_id;
        tasks = [
          {
            task_id: body.briefing_id + "/1",
            title: body.title,
            status: "ready",
            revision: 1,
            description: "A real task description",
            updated_at: 1,
            role_id: "office/" + body.member_ids[0],
          },
        ];
        mockMeetings = [
          {
            id: body.briefing_id,
            title: body.title,
            goal: body.description,
            mode: body.mode,
            leader: body.leader_id,
            stage: "planning",
            round: 1,
          },
        ];
        mockAssignments = [
          {
            task_id: tasks[0].task_id,
            member_id: body.member_ids[0],
            group_id: config.groups[0].id,
            meeting_id: body.briefing_id,
            stage: "主导者拆解",
          },
        ];
        data = { tasks };
        break;
      default:
        res.statusCode = 400;
        res.end(
          JSON.stringify({
            ok: false,
            error: { message: "Unexpected operation" },
          }),
        );
        return;
    }
    res.end(JSON.stringify({ ok: true, data }));
    return;
  }
  const filename = {
    "/": "office.html",
    "/office.js": "office.js",
    "/office.css": "office.css",
  }[req.url];
  if (!filename) {
    res.statusCode = 404;
    res.end("missing");
    return;
  }
  res.setHeader(
    "Content-Type",
    filename.endsWith("js")
      ? "text/javascript"
      : filename.endsWith("css")
        ? "text/css"
        : "text/html",
  );
  res.end(fs.readFileSync(path.join(__dirname, "../src/relayfence", filename)));
});
(async () => {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const browser = await chromium.launch({
    executablePath: process.env.RELAYFENCE_BROWSER,
    headless: true,
  });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 1150 },
    });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const url = `http://127.0.0.1:${server.address().port}/`;
    await page.goto(url);
    await page.locator("#add-project-button").click();
    await page.waitForFunction(
      () => document.querySelector("#connect-dialog").open,
    );
    assert.equal(
      new URL(page.url()).hash,
      "",
      "direct local URL needs no pairing nonce",
    );
    assert.equal(
      await page.locator('[name="token"], [name="agent_id"]').count(),
      0,
    );
    await page
      .locator('#connect-form [name="path"]')
      .fill("D:\\typing-in-progress");
    await page.waitForFunction(
      () =>
        document.querySelectorAll("#software-status .software-status-row")
          .length === 4,
    );
    assert.equal(
      await page.locator('#connect-form [name="path"]').inputValue(),
      "D:\\typing-in-progress",
      "discovery poll preserves typed directory",
    );
    await page.locator("#browse-project").click();
    await page
      .getByRole("button", { name: "✓ 选择此文件夹", exact: true })
      .click();
    await page.waitForFunction(() =>
      document
        .querySelector('[name="path"]')
        .value.includes("existing-project"),
    );
    await page.locator("#connect-form button.primary").click();
    await page.waitForFunction(
      () => !document.querySelector("#connect-dialog").open,
    );
    assert.equal(workspace, "D:\\projects\\existing-project");
    await page.locator('.tabs [data-view="skills"]').click();
    await page.locator(".capability-table tbody tr").first().waitFor();
    assert.match(await page.locator(".capability-table").textContent(), /test-skill/);
    await page.locator("#capability-type").selectOption("mcp");
    assert.match(await page.locator(".capability-table").textContent(), /docs/);
    assert.equal(await page.locator(".capability-table").textContent().then(t=>t.includes("test-skill")), false);
    await page.locator("#capability-query").fill("not-found");
    assert.match(await page.locator(".capability-table").textContent(), /没有匹配/);
    await page.locator("#capability-query").fill("");
    await page.locator('.tabs [data-view="office"]').click();
    await page.locator("#existing-groups-button").click();
    await page.waitForFunction(
      () => document.querySelectorAll(".existing-group-row").length === 2,
    );
    await page.locator(".existing-group-row input").nth(0).check();
    await page.locator(".existing-group-row input").nth(1).check();
    await page.locator('#existing-groups-form button[type="submit"]').click();
    await page.waitForFunction(
      () => !document.querySelector("#existing-groups-dialog").open,
    );
    await page.waitForFunction(
      () => document.querySelectorAll(".desk").length === 4,
    );
    assert.equal(config.groups.length, 2, "both existing groups were imported");
    assert.equal(config.groups[0].members[0].agent_id, "local-codex");
    assert.equal(await page.locator("#workbench-teams .wb-team").count(), 2);
    assert.equal(await page.locator('.rail [data-view="tasks"] span').isVisible(), true);
    assert.equal(await page.locator('.rail [data-view="tasks"]').count(), 1);
    assert.equal(await page.locator("#team-view-button").getAttribute("aria-pressed"), "true");
    assert.equal(await page.locator("#office-scene-layout").isVisible(), false);
    await page.locator("#office-view-button").click();

    await page
      .locator(".group")
      .first()
      .getByRole("button", { name: "重命名", exact: true })
      .click();
    await page.locator('#rename-group-form [name="name"]').fill("产品研发组");
    await page.locator('#rename-group-form button[type="submit"]').click();
    await page.waitForFunction(
      () => !document.querySelector("#rename-group-dialog").open,
    );
    assert.equal(config.groups[0].name, "产品研发组");
    await page.locator('.tabs [data-view="teams"]').click();
    await page.waitForFunction(
      () => document.querySelectorAll(".team-card").length === 4,
    );
    const teamsText = await page.locator("#team-groups").textContent();
    assert.match(teamsText, /产品研发组/);
    assert.match(teamsText, /已有 Codex 项目/);
    assert.equal(
      await page.locator(".team-card .team-badge.current").count(),
      2,
    );
    await page.locator('.tabs [data-view="office"]').click();
    assert.equal(await page.locator(".member-live-card").count(), 4);
    await page.locator("#office-view-button").click();
    await page.locator(".desk").first().click();
    assert.deepEqual(
      await page
        .locator("#model-select option")
        .evaluateAll((opts) => opts.map((o) => o.value)),
      ["detected-one", "detected-two"],
    );
    await page.locator("#member-mcp-mode").selectOption("selected");
    await page.locator("#member-mcp-list input").first().check();
    await page.locator("#model-select").selectOption("detected-two");
    assert.deepEqual(
      await page
        .locator("#effort-select option")
        .evaluateAll((opts) => opts.map((o) => o.value)),
      ["medium"],
    );
    await page.locator("#effort-select").selectOption("medium");
    await page.locator('#member-form [name="name"]').fill("自动绑定成员");
    await page.locator("#position-select").selectOption("custom");
    await page.locator('[name="custom_position"]').fill("可靠性工程师");
    await page.locator('[name="prompt"]').fill("检查真实故障并保留复现步骤");
    await page.getByRole("button", { name: "样貌 24", exact: true }).click();
    await page.locator('#member-form button[type="submit"]').click();
    await page.waitForFunction(
      () => !document.querySelector("#member-dialog").open,
    );
    assert.equal(saved.groups[0].members[0].agent_id, "local-codex");
    assert.equal(saved.groups[0].members[0].model, "detected-two");
    assert.deepEqual(saved.groups[0].members[0].mcp_servers, ["docs"]);
    assert.equal(saved.groups[0].members[0].custom_position, "可靠性工程师");
    assert.equal(
      saved.groups[0].members[0].prompt,
      "检查真实故障并保留复现步骤",
    );
    assert.equal(
      saved.groups[0].members[0].parameters.reasoning_effort,
      "medium",
    );
    await page.locator(".group").first().locator(".desk").nth(1).click();
    await page.locator('[name="relationship"]').selectOption("native_subagent");
    await page.locator('#member-form button[type="submit"]').click();
    await page.waitForFunction(
      () => !document.querySelector("#member-dialog").open,
    );
    assert.equal(saved.groups[0].members[1].relationship, "native_subagent");
    assert.equal(saved.groups[0].members[1].agent_id, null);
    await page.locator(".group").nth(1).locator(".desk").first().click();
    assert.equal(await page.locator("#effort-select").isDisabled(), true);
    await page.locator("#provider-select").selectOption("second-provider");
    assert.equal(
      await page.locator("#model-select").inputValue(),
      "second-provider-model",
    );
    await page.locator('#member-form button[type="submit"]').click();
    await page.waitForFunction(
      () => !document.querySelector("#member-dialog").open,
    );
    assert.equal(saved.groups[1].members[0].agent_id, "local-dsh-second");
    await page.locator('.tabs [data-view="meeting"]').click();
    await page.locator("#meeting-roster input:not(:disabled)").first().check();
    await page.locator('#briefing-form [name="title"]').fill("浏览器测试任务");
    await page
      .locator('#briefing-form [name="description"]')
      .fill("仅保存到模拟账本");
    await page.locator("#meeting-capabilities > summary").click();
    await page.locator("#meeting-capabilities-load").click();
    await page.locator(".meeting-capability-member > label input").first().check();
    await page.locator(".meeting-capability-member select").first().selectOption("inherit");
    await page.locator("#briefing-form button.primary").click();
    await page.waitForFunction(
      () => document.querySelectorAll(".task-card").length === 1,
    );
    assert.ok(dispatched.member_ids.length === 1 && lastMutation);
    assert.equal(dispatched.leader_id, config.groups[0].members[0].id);
    assert.equal(dispatched.mode, "discussion");
    assert.equal(dispatched.member_overrides[dispatched.leader_id].mcp_servers, null);
    assert.deepEqual(config.groups[0].members[0].mcp_servers, ["docs"]);
    assert.equal(await page.locator("#tasks-view").isVisible(), true);
    await page.waitForFunction(
      () =>
        document.querySelectorAll("#group-chat-messages .task-chat-event")
          .length >= 1,
    );
    assert.ok(groupChatRequests > 0);
    assert.equal(
      await page.locator("#group-chat-form textarea").isVisible(),
      true,
    );
    await page
      .locator("#execution-board")
      .getByRole("button", { name: "暂停", exact: true })
      .click();
    await page.waitForFunction(() =>
      document.querySelector("#execution-board").textContent.includes("已暂停"),
    );
    await page
      .locator("#execution-board")
      .getByRole("button", { name: "继续", exact: true })
      .click();
    await page.waitForFunction(() =>
      document.querySelector("#execution-board").textContent.includes("待领取"),
    );
    assert.equal(tasks[0].revision, 3);
    await page.locator('.tabs [data-view="office"]').click();
    await page.getByRole("button", {name: "查看任务流程 →", exact: true}).click();
    assert.equal(await page.locator("#task-focus").isVisible(), true);
    assert.ok(await page.locator("#task-focus .wb-step").count() > 0);
    await page.locator("#task-focus").getByRole("button", {name: "成员对话", exact: true}).first().click();
    assert.equal(await page.locator("#member-chat").isVisible(), true);
    assert.equal(await page.locator("#task-focus").isVisible(), true);
    await page.locator("#close-member-chat").click();

    tasks[0].status = "failed";
    tasks[0].failure = {
      code: "provider_auth_failed",
      message: "模型供应商认证失败（401），请重新连接或交接。",
    };
    mockMeetings[0].waiting = [tasks[0].task_id];
    await page.evaluate(() => refresh());
    assert.match(
      await page.locator(".meeting-blocked").textContent(),
      /认证失败/,
    );
    assert.match(
      await page.locator(".delivery-lane.attention").textContent(),
      /浏览器测试任务/,
    );
    tasks[0].status = "ready";
    delete tasks[0].failure;
    delete mockMeetings[0].waiting;
    await page.evaluate(() => refresh());

    assert.equal(await page.locator(".delivery-lane").count(), 5);
    await page.locator(".delivery-ticket").first().click();
    await page.locator("#task-inspect-content .inspect-attempt").waitFor();
    assert.match(
      await page.locator("#task-inspect-content").textContent(),
      /mock-builder/,
    );
    await page.locator('[data-close="task-inspect-dialog"]').click();

    await page
      .getByRole("button", { name: "暂停整个任务", exact: true })
      .click();
    await page
      .getByRole("button", { name: "继续整个任务", exact: true })
      .waitFor();
    assert.equal(mockMeetings[0].paused, true);
    await page
      .getByRole("button", { name: "继续整个任务", exact: true })
      .click();
    await page
      .getByRole("button", { name: "暂停整个任务", exact: true })
      .waitFor();
    assert.equal(mockMeetings[0].paused, false);
    const savedHistoryTasks = tasks;
    tasks = [
      { ...tasks[0], task_id: "history-old", title: "Old task", created_at: 1, updated_at: 999 },
      { ...tasks[0], task_id: "history-new", title: "Newest task", created_at: 3, updated_at: 3 },
      { ...tasks[0], task_id: "history-middle", title: "Middle task", created_at: 2, updated_at: 2 },
    ];
    await page.evaluate(() => refresh());
    assert.equal(await page.locator("#execution-board .execution-task").count(), 2);
    assert.deepEqual(await page.locator("#execution-board .execution-task").evaluateAll(items => items.map(item => item.dataset.taskId)), ["history-new", "history-middle"]);
    await page.locator("#execution-more").click();
    assert.equal(await page.locator("#execution-board .execution-task").count(), 3);
    await page.evaluate(() => refresh());
    assert.equal(await page.locator("#execution-board .execution-task").count(), 3);
    assert.equal(await page.locator("#execution-more").getAttribute("aria-expanded"), "true");
    await page.locator("#execution-more").click();
    assert.equal(await page.locator("#execution-board .execution-task").count(), 2);
    tasks = savedHistoryTasks;
    await page.evaluate(() => refresh());
    assert.equal(await page.locator("#execution-more").isVisible(), false);
    await page.locator("#execution-board summary").first().click();
    await page.waitForTimeout(2400);
    assert.equal(
      await page
        .locator("#execution-board details")
        .first()
        .getAttribute("open"),
      "",
    );
    if (process.env.RELAYFENCE_SCREENSHOT_DIR)
      await page.screenshot({
        path: path.join(
          process.env.RELAYFENCE_SCREENSHOT_DIR,
          "task-command-center.png",
        ),
        fullPage: true,
      });
    assert.match(await page.locator("#task-board").textContent(), /待领取/);
    await page.locator('.tabs [data-view="office"]').click();
    await page.locator("#office-view-button").click();
    await page.locator(".desk").first().click();
    await page.locator("#member-chat").waitFor({ state: "visible" });
    assert.equal(await page.locator("#member-chat").getAttribute("aria-modal"), "false");
    assert.ok(await page.locator("#chat-model").textContent());
    assert.equal(await page.locator("body.member-chat-open").count(), 1);
    await page.getByRole("tab", {name: "执行记录", exact:true}).click();
    assert.equal(await page.locator("#member-activity-tab").getAttribute("aria-selected"), "true");
    await page.getByRole("tab", {name: "对话", exact:true}).click();

    await page.locator("#member-chat-form textarea").fill("先添加故障复现");
    await page.locator('#member-chat-form button[type="submit"]').click();
    await page.waitForFunction(() =>
      document
        .querySelector("#chat-messages")
        .textContent.includes("先添加故障复现"),
    );
    assert.equal(guidance.length, 1);
    assert.equal(await page.locator(".chat-message").count(), 1);
    assert.equal(await page.locator("#member-chat .chat-day").count(), 1);
    if (process.env.RELAYFENCE_SCREENSHOT_DIR)
      await page.screenshot({
        path: path.join(
          process.env.RELAYFENCE_SCREENSHOT_DIR,
          "member-chat.png",
        ),
      });
    await page.locator("#close-member-chat").click();
    mockDecisions = [
      {
        decision_id: "decision-browser",
        meeting_id: mockMeetings[0].id,
        task_id: tasks[0].task_id,
        kind: "task_question",
        status: "pending",
        revision: 1,
        question: {
          question: "是否兼容旧接口？",
          context: "兼容会增加工作量。",
          options: ["需要兼容", "无需兼容"],
        },
      },
    ];
    await page.locator('.rail [data-view="tasks"]').click();
    await page.getByRole("button", { name: "需要兼容", exact: true }).click();
    await page
      .locator("#decision-list textarea")
      .fill("需要兼容，并增加测试。");
    await page.waitForTimeout(2300);
    assert.equal(
      await page.locator("#decision-list textarea").inputValue(),
      "需要兼容，并增加测试。",
    );
    if (process.env.RELAYFENCE_SCREENSHOT_DIR)
      await page.screenshot({
        path: path.join(
          process.env.RELAYFENCE_SCREENSHOT_DIR,
          "decision-center.png",
        ),
        fullPage: true,
      });
    await page
      .getByRole("button", { name: "提交回复并继续", exact: true })
      .click();
    await page.waitForFunction(
      () => document.querySelector("#decision-count").textContent === "0",
    );
    assert.equal(mockDecisions[0].answer, "需要兼容，并增加测试。");

    await page.locator('.rail [data-view="logs"]').click();
    await page.waitForFunction(
      () => document.querySelectorAll(".log-row").length === 2,
    );
    assert.match(await page.locator("#log-list").textContent(), /任务执行失败/);
    assert.match(await page.locator("#log-summary").textContent(), /1错误/);
    await page.locator("#log-level").selectOption("error");
    await page.locator('#log-filter-form button[type="submit"]').click();
    await page.waitForFunction(
      () => document.querySelectorAll(".log-row").length === 1,
    );
    assert.equal(await page.locator(".log-row .badge").textContent(), "错误");
    if (process.env.RELAYFENCE_SCREENSHOT_DIR)
      await page.screenshot({
        path: path.join(
          process.env.RELAYFENCE_SCREENSHOT_DIR,
          "office-logs.png",
        ),
        fullPage: true,
      });
    await page.locator('.tabs [data-view="office"]').click();
    mockMeetings[0].stage = "done";
    tasks[0].status = "done";
    assert.equal(await page.locator('.rail [data-view="tasks"]').count(), 1);
    await page.locator('.rail [data-view="tasks"]').click();
    await page
      .getByRole("button", { name: "新增修改轮次", exact: true })
      .click();
    await page
      .locator("#revision-form textarea")
      .fill("增加日期筛选，保留现有行为");
    await page.locator('#revision-form button[type="submit"]').click();
    await page.waitForFunction(
      () => !document.querySelector("#revision-dialog").open,
    );
    assert.equal(mockMeetings[0].iteration, 2);
    assert.equal(await page.locator("#task-meeting-filter option").count(), 2);
    await page.waitForFunction(
      () =>
        document.querySelectorAll("#task-iteration-filter option").length === 3,
    );
    assert.equal(
      await page.locator("#task-iteration-filter option").count(),
      3,
    );
    assert.equal(await page.locator(".meeting-flow").count(), 1);
    assert.match(await page.locator(".meeting-flow").textContent(), /第 2 轮/);
    await page.locator("#task-iteration-filter").selectOption("");
    assert.equal(await page.locator(".meeting-agenda-item").count(), 2);
    await page.locator(".meeting-agenda-item").nth(1).click();
    assert.match(await page.locator(".meeting-flow").textContent(), /第 1 轮/);
    assert.equal(await page.locator(".meeting-flow").getByRole("button", { name: "新增修改轮次", exact: true }).count(), 0);
    assert.equal(await page.locator(".meeting-flow").getByRole("button", { name: "暂停整个任务", exact: true }).count(), 0);
    await page.locator(".meeting-flow .meeting-goal summary").click();
    await page.evaluate(() => refresh());
    assert.match(await page.locator(".meeting-flow").textContent(), /第 1 轮/);
    assert.equal(await page.locator(".meeting-flow .meeting-goal").getAttribute("open"), "");
    assert.ok(await page.locator(".meeting-timeline-step.complete").count() > 0);
    await page.locator(".meeting-agenda-item").first().click();
    assert.match(await page.locator(".meeting-flow").textContent(), /第 2 轮/);
    if (process.env.RELAYFENCE_SCREENSHOT_DIR)
      await page.locator(".meeting-workspace").screenshot({
        path: path.join(process.env.RELAYFENCE_SCREENSHOT_DIR, "meeting-progress.png"),
      });
    tasks[0].status = "done";
    tasks[0].display_status = "awaiting_review";
    tasks[0].review_required = true;
    tasks[0].review = { status: "pending" };
    await page.locator("#task-meeting-filter").selectOption("");
    await page.locator("#task-iteration-filter").selectOption("");
    tasks[0].fence = 1;
    await page.evaluate(() => refresh());
    await page.evaluate(() => window.scrollTo(0, 0));
    if (process.env.RELAYFENCE_SCREENSHOT_DIR)
      await page.locator(".delivery-overview").screenshot({
        path: path.join(
          process.env.RELAYFENCE_SCREENSHOT_DIR,
          "delivery-lanes.png",
        ),
      });

    await page
      .locator("#execution-board")
      .getByRole("button", { name: "验收通过", exact: true })
      .click();
    await page
      .locator('#task-review-form [name="note"]')
      .fill("已核对交付内容与验收要求");
    await page
      .locator('#task-review-form [name="evidence"]')
      .fill("browser regression passed");
    await page.locator('#task-review-form button[type="submit"]').click();
    await page.waitForFunction(
      () => !document.querySelector("#task-review-dialog").open,
    );
    assert.equal(tasks[0].review.status, "approved");
    await page.evaluate(() => window.scrollTo(0, 0));
    if (process.env.RELAYFENCE_SCREENSHOT_DIR)
      await page.screenshot({
        path: path.join(
          process.env.RELAYFENCE_SCREENSHOT_DIR,
          "delivery-board.png",
        ),
        fullPage: true,
      });
    await page.locator('.tabs [data-view="office"]').click();
    if (process.env.RELAYFENCE_SCREENSHOT_DIR)
      await page.screenshot({
        path: path.join(
          process.env.RELAYFENCE_SCREENSHOT_DIR,
          "office-desktop.png",
        ),
        fullPage: true,
      });
    projectSessions = [
      {
        runtime: "codex",
        session_id: "native-fixed-project",
        status: "ready",
        turns: 2,
        updated_at: 1760000800,
      },
    ];
    await page.evaluate(() => refresh());
    assert.match(
      await page.locator("#project-session-list").textContent(),
      /native-fixed-project/,
    );
    await page.waitForTimeout(2300);
    assert.equal(
      await page
        .locator("#project-session-list")
        .getByText("native-fixed-project", { exact: true })
        .count(),
      1,
    );
    if (process.env.RELAYFENCE_SCREENSHOT_DIR)
      await page.locator(".project-session-panel").screenshot({
        path: path.join(
          process.env.RELAYFENCE_SCREENSHOT_DIR,
          "project-session.png",
        ),
      });
    page.once("dialog", (dialog) => dialog.accept());
    await page
      .locator("#project-session-list")
      .getByRole("button", { name: "重建绑定" })
      .click();
    await page.waitForFunction(
      () =>
        !document
          .querySelector("#project-session-list")
          .textContent.includes("native-fixed-project"),
    );
    assert.equal(projectSessions.length, 0);
    page.once("dialog", (dialog) => dialog.accept());
    await page
      .locator(".group")
      .nth(1)
      .getByRole("button", { name: "删除", exact: true })
      .click();
    await page.waitForFunction(
      () => document.querySelectorAll(".group").length === 1,
    );
    assert.equal(config.groups.length, 1);
    await page.setViewportSize({ width: 390, height: 844 });
    if (process.env.RELAYFENCE_SCREENSHOT_DIR)
      await page.screenshot({
        path: path.join(
          process.env.RELAYFENCE_SCREENSHOT_DIR,
          "office-mobile.png",
        ),
        fullPage: true,
      });
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      true,
      "mobile page overflows",
    );
    await page.locator('.tabs [data-view="tasks"]').click();
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      true,
      "mobile delivery board overflows",
    );
    await page.locator('.tabs [data-view="office"]').click();
    assert.equal(await page.evaluate(() => localStorage.length), 0);
    assert.deepEqual(errors, []);
    // Operational view must expose queue pressure and stop claiming healthy on stale reads.
    await page.locator('.tabs [data-view="office"]').click();
    await page.evaluate(() => refresh());
    assert.match(await page.locator("#runtime-badge").innerText(), /运行正常/);
    const readsBeforeOverlap = officeReads;
    await page.evaluate(() => Promise.all([refresh(), refresh()]));
    assert.ok(officeReads >= readsBeforeOverlap + 2, "an overlapping explicit refresh is not discarded");
    assert.match(await page.locator("#runtime-queue").innerText(), /等待本服务执行名额/);
    runtimeStatus = "stopped";
    overviewOverride = {counts: {done: 1, lease_expired: 1, cancelled: 2},
      progress: {total: 4, eligible: 2, completed: 1, cancelled: 2, stale: 0, percent: 50},
      attention_total: 1, attention: [{task_id: tasks[0].task_id, title: "中断后待恢复的任务", reason: "lease_expired"}]};
    await page.evaluate(() => refresh());
    assert.equal(await page.locator("#hero-progress").innerText(), "50%");
    assert.equal(await page.locator("#hero-running").innerText(), "0 个执行中");
    assert.ok(await page.locator("#runtime-reconnect").isVisible());
    await page.locator("#runtime-attention-title").click();
    assert.match(await page.locator("#runtime-attention-list").innerText(), /执行失联/);
    const recoveryShots = process.env.RELAYFENCE_SCREENSHOT_DIR;
    if (recoveryShots) await page.screenshot({path: path.join(recoveryShots, "runtime-recovery.png"), fullPage: true});
    ledgerUnavailable = true;
    await page.evaluate(async () => { try { await refresh(); } catch {} });
    assert.match(await page.locator("#runtime-badge").innerText(), /同步中断/);
    assert.match(await page.locator("#screen-live").innerText(), /同步中断/);
    ledgerUnavailable = false;
    runtimeStatus = "healthy";
    overviewOverride = null;
    await page.evaluate(() => refresh());
    assert.match(await page.locator("#runtime-badge").innerText(), /运行正常/);
    assert.deepEqual(errors, []);
    await page.reload();
    await page.waitForFunction(() =>
      document.querySelector("#connection-state").classList.contains("online"),
    );
    assert.deepEqual(errors, []);
    await page.locator("#projects-button").click();
    for (const name of ["project-alpha", "project-beta"]) {
      await page.locator("#add-project-button").click();
      await page
        .locator('#connect-form [name="path"]')
        .fill("D:/projects/" + name);
      await page.locator("#save-project-button").click();
      await page.waitForFunction(
        () => !document.querySelector("#connect-dialog").open,
      );
    }
    await page.waitForFunction(() => document.querySelectorAll(".project-card").length === 2);
    assert.equal(await page.locator(".project-card").count(), 2);
    await page.reload();
    await page.locator("#projects-button").click();
    await page.waitForFunction(
      () => document.querySelectorAll(".project-card").length === 2,
    );
    await page
      .locator(".project-card")
      .first()
      .getByRole("button", { name: "进入项目 →" })
      .click();
    await page.waitForFunction(
      () => !document.querySelector("#office-view").hidden,
    );
    assert.equal(workspace, "D:/projects/project-alpha");
    await page.locator('.tabs [data-view="agents"]').click();
    await page.waitForFunction(
      () => document.querySelectorAll(".agent-card").length === 4,
    );
    assert.equal(await page.locator(".agent-card").count(), 4);
    await page.locator(".agent-card").filter({ hasText: "OpenCode" }).click();
    await page.waitForFunction(
      () => !document.querySelector("#agent-detail").hidden,
    );
    assert.match(
      await page.locator("#agent-detail").innerText(),
      /ISOFT_API_KEY/,
    );
    assert.match(await page.locator("#agent-detail").innerText(), /缺少密钥/);
    await page
      .locator("#agent-detail")
      .getByRole("button", { name: "配置密钥" })
      .click();
    await page.locator("#agent-key-value").fill("sk-browser-secret-3456");
    await page.locator('#agent-key-form button[type="submit"]').click();
    await page.waitForFunction(
      () => !document.querySelector("#agent-key-dialog").open,
    );
    await page.waitForFunction(() =>
      document.querySelector("#agent-detail").innerText.includes("已加密保存"),
    );
    assert.ok(
      !(await page.locator("body").innerText()).includes(
        "sk-browser-secret-3456",
      ),
      "plaintext key is never rendered",
    );
    assert.match(
      await page.locator("#agent-detail").innerText(),
      /••••••••3456/,
    );
    page.once("dialog", (dialog) => dialog.accept());
    await page
      .locator("#agent-detail")
      .getByRole("button", { name: "删除" })
      .click();
    await page.waitForFunction(() =>
      document
        .querySelector("#agent-detail")
        .innerText.includes("缺少 ISOFT_API_KEY"),
    );
    const longRoleGroup = await page.evaluate(() => {
      const savedConfig = state.config;
      const savedTasks = state.tasks;
      const savedAssignments = state.assignments;
      const memberId = "a".repeat(128);
      const role = "office/" + "b".repeat(32);
      state.config = {
        ...savedConfig,
        groups: [
          {
            id: "long-role-group",
            name: "Long role",
            members: [{ id: memberId, name: "Long member" }],
          },
        ],
        member_roles: { [memberId]: role },
      };
      state.tasks = [
        { task_id: "long-role-task", title: "Long task", role_id: role },
      ];
      state.assignments = [];
      const found = assignedMember(state.tasks[0]);
      state.config = savedConfig;
      state.tasks = savedTasks;
      state.assignments = savedAssignments;
      return found?.g.id || "";
    });
    assert.equal(longRoleGroup, "long-role-group");
    const guidanceStatuses = await page.evaluate(() => {
      const base = {
        kind: "guidance",
        status: "pending",
        status_label: "等待执行器确认",
        task_id: "task",
        member_id: "member",
      };
      const exact = {
        messages: new Map([
          ["log-1", { ...base, id: "log-1", log_id: 1, guidance_id: 7 }],
        ]),
      };
      mergeGroupChatMessages(exact, [
        {
          id: "log-2",
          log_id: 2,
          event_kind: "chat.applied",
          guidance_id: 7,
          task_id: "task",
          member_id: "member",
        },
      ]);
      const legacy = {
        messages: new Map([
          ["log-3", { ...base, id: "log-3", log_id: 3, guidance_id: null }],
        ]),
      };
      mergeGroupChatMessages(legacy, [
        {
          id: "log-4",
          log_id: 4,
          event_kind: "chat.failed",
          guidance_id: null,
          task_id: "task",
          member_id: "member",
        },
      ]);
      return [
        exact.messages.get("log-1").status,
        exact.messages.get("log-1").status_label,
        legacy.messages.get("log-3").status,
        legacy.messages.get("log-3").status_label,
      ];
    });
    assert.deepEqual(guidanceStatuses, [
      "applied",
      "已应用",
      "failed",
      "未能应用",
    ]);
    assert.deepEqual(errors, []);
    console.log(
      JSON.stringify({
        passed: true,
        checks: [
          "capability table, filters and MCP metadata",
          "member MCP selection and per-meeting override",
          "single text navigation and project workbench",
          "task timeline and member drawer navigation",
          "team and office view switch",
          "nonmodal member drawer, model context and record tabs",
          "direct local access",
          "custom role and editable prompt",
          "member chat guidance",
          "task dashboard pause and resume",
          "latest two task controls and expandable history",
          "meeting leader and mode",
          "local directory picker",
          "detected catalogues only",
          "model effort cascade",
          "provider runner auto-binding",
          "mock dispatch",
          "team library and existing-group import",
          "agent inventory and encrypted key masking",
          "long member role mapping",
          "guidance outcome merge",
          "group rename",
          "objective iteration filters",
          "meeting agenda selection, timeline and refresh retention",
          "member live wall",
          "session reload",
          "mobile width",
          "delivery board and attempt archive",
          "delivery approval with evidence",
          "mobile delivery board width",
          "fixed project sessions and explicit reset",
          "no JS errors",
          "runtime capacity and recovery attention",
          "cancelled and expired progress semantics",
          "stale connection state and recovery",
        ],
      }),
    );
  } finally {
    await browser.close();
  }
})()
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  })
  .finally(() => server.close());
