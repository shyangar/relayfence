/* Office rendering uses DOM text nodes only: provider/task content is untrusted. */
"use strict";
const $ = (id) => document.getElementById(id);
const state = {
  token: "",
  project: "",
  workbenchSignature: null,
  focusedMeeting: null,
  stageMeeting: null,
  stageSignature: null,
  authenticated: false,
  workspace: "",
  catalogue: [],
  discovering: false,
  refreshPending: false,
  config: { revision: 0, groups: [], positions: {} },
  tasks: [],
  meetings: [],
  decisions: [],
  decisionDrafts: new Map(),
  decisionRequests: new Map(),
  decisionSignature: null,
  sessionFilter: "all",
  liveMembers: [],
  assignments: [],
  taskDetails: new Set(),
  taskHistory: { scope: null, expanded: false },
  taskResults: new Map(),
  agents: [],
  agentManager: {
    items: [],
    selected: "",
    discovering: false,
    scannedAt: null,
    loading: false,
    loaded: false,
    error: "",
    edit: null,
    retry: null,
  },
  observations: [],
  snapshots: [],
  projectSessions: [],
  projectSessionsError: false,
  runtime: null,
  overview: null,
  syncFailed: false,
  runtimeAttentionSignature: "",
  operations: null,
  events: [],
  runners: [],
  busy: false,
  refreshing: false,
  refreshPromise: null,
  refreshRequested: false,
  generation: 0,
  order: [],
  edit: null,
  avatar: 0,
  route: null,
  pending: null,
  eventCursor: 0,
  chat: null,
  taskChat: {
    group: "",
    messages: new Map(),
    after: 0,
    before: null,
    hasMore: false,
    loading: false,
    loaded: false,
    signature: null,
    pending: null,
    sending: false,
    error: "",
    lastError: "",
    targetSignature: "",
    focusTask: "",
    scrollOlder: false,
    scrollSignature: null,
  },
  teams: {
    groups: [],
    projects: [],
    query: "",
    project: "",
    loading: false,
    loaded: false,
    error: "",
  },
  logs: {
    group: "",
    level: "",
    query: "",
    before: 0,
    entries: [],
    summary: null,
    hasMore: false,
    loading: false,
    loaded: false,
    error: "",
  },
  skills: { skills: [], workspace: "" },
  skillLoading: false,
  memberSkills: [],
  memberMcp: null,
  meetingCapabilities: {},
  meetingCapabilitySignature: null,
  meetingCapabilitiesLoaded: false,
};
const labels = {
  ready: "待领取",
  waiting_user: "等待你确认",
  awaiting_review: "待验收",
  lease_expired: "执行失联",
  running: "执行中",
  done: "已完成",
  failed: "失败",
  paused: "已暂停",
  cancelled: "已取消",
  blocked: "等待依赖",
  unknown: "未知",
  available: "可用",
  exhausted: "已耗尽",
  rate_limited: "暂时限速",
};
const taskStatusRank = {
  running: 0,
  ready: 1,
  paused: 2,
  blocked: 3,
  failed: 4,
  done: 5,
  cancelled: 6,
};
function taskStatusOrder(status) {
  return taskStatusRank[status] ?? 7;
}
function softwareEntry(software) {
  return state.catalogue.find((entry) => entry.software === software);
}
function availableProviders(entry) {
  return (entry?.providers || []).filter(
    (provider) => provider.authenticated !== false,
  );
}
function effortValues(model) {
  return (model?.reasoning_efforts || [])
    .map((value) =>
      typeof value === "string" ? value : value.id || value.reasoning_effort,
    )
    .filter(Boolean);
}
function defaultEffort(model) {
  const values = effortValues(model);
  return (
    [model?.default_reasoning_effort, "medium", "high", values[0]].find(
      (value) => values.includes(value),
    ) || ""
  );
}
function finitePrice(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : null;
}
function money(value) {
  if (value === 0) return "$0";
  if (value == null) return "?";
  return (
    "$" +
    (Number.isInteger(value)
      ? String(value)
      : value.toFixed(4).replace(/0+$/, "").replace(/\.$/, ""))
  );
}
function modelPriceText(model) {
  if (!model) return "";
  if (model.free)
    return "免费模型：本机目录标记输入与输出价格为 $0 / 百万 token；实际可用额度仍以供应商为准。";
  const pricing = model.pricing;
  if (!pricing)
    return "本机 OpenCode 模型目录未提供价格信息，请以供应商账单为准。";
  const parts = [];
  const input = finitePrice(pricing.input);
  const output = finitePrice(pricing.output);
  const read = finitePrice(pricing.cache_read);
  const write = finitePrice(pricing.cache_write);
  if (input != null) parts.push("输入 " + money(input) + "/M");
  if (output != null) parts.push("输出 " + money(output) + "/M");
  if (read) parts.push("缓存读 " + money(read) + "/M");
  if (write) parts.push("缓存写 " + money(write) + "/M");
  return parts.length
    ? "基础价格（USD / 百万 token）：" +
        parts.join(" · ") +
        "。实际计费以供应商账单为准。"
    : "本机 OpenCode 模型目录未提供价格信息，请以供应商账单为准。";
}
function updateModelPrice() {
  const group = state.config.groups.find((g) => g.id === state.edit?.groupId);
  if (group?.software !== "opencode") {
    $("model-price-help").textContent = "";
    return;
  }
  const provider = selectedProvider();
  const model = provider?.models?.find((m) => m.id === $("model-select").value);
  $("model-price-help").textContent = modelPriceText(model);
}
function modelDefaults(software) {
  const entry = softwareEntry(software);
  const choices = availableProviders(entry).flatMap((provider) =>
    (provider.models || []).map((model) => ({ provider, model })),
  );
  // Prefer a zero-price model when the local catalogue identifies one.
  const choice = choices.find(({ model }) => model.free) || choices[0];
  const provider = choice?.provider;
  const model = choice?.model;
  if (!provider || !model)
    throw Error("该软件尚未检测到模型。请先连接项目并重新检测。");
  const parameters = {};
  if (software !== "codex") parameters.provider = provider.id;
  if (defaultEffort(model)) parameters.reasoning_effort = defaultEffort(model);
  return {
    provider: provider.id,
    model: model.id,
    parameters,
    agent_id: provider.agent_id || null,
  };
}
async function localRequest(path, body) {
  const response = await fetch("/local/" + path, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      "X-RelayFence-Local": "1",
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
    cache: "no-store",
    signal: AbortSignal.timeout(path === "pick" ? 300000 : 60000),
  });
  const result = await response.json().catch(() => null);
  if (!response.ok || result?.error) {
    if (response.status === 401) {
      state.authenticated = false;
      throw Error("无法使用本地连接，请用本机浏览器打开当前服务地址。");
    }
    const error = Error(
      result?.error?.message || "本地请求失败（" + response.status + "）",
    );
    error.status = response.status;
    throw error;
  }
  if (!result) throw Error("本地服务返回了无效响应，请检查服务是否仍在运行。");
  return result;
}
function applyLocalStatus(status) {
  state.authenticated = true;
  state.catalogue = (status.catalogue || []).map((entry) => ({
    ...entry,
    software: entry.software || entry.id,
  }));
  state.discovering = Boolean(status.discovering);
  if (state.discovering) state.agentManager.discovering = true;
  state.refreshPending = Boolean(status.refresh_pending);
  const changed =
    state.project !== (status.project || "") ||
    state.workspace !== (status.workspace || "");
  if (changed) {
    state.chat = null;
    // A pending write belongs to the old project. Replaying it here would
    // dispatch into the wrong ledger, so clear it with the old state.
    state.pending = null;
    $("retry-request").hidden = true;
    $("rename-group-dialog").close();
    $("revision-dialog").close();
    if (!$("member-chat").hidden) closeMemberChat();
    state.generation++;
    state.project = status.project || "";
    state.meetingCapabilities = {};
    state.meetingCapabilitySignature = null;
    state.meetingCapabilitiesLoaded = false;
    state.focusedMeeting = null;
    state.stageMeeting = null;
    state.stageSignature = null;
    state.workbenchSignature = null;
    state.workspace = status.workspace || "";
    state.config = { revision: 0, groups: [], positions: {} };
    state.projectSessions = [];
    state.runtime = state.overview = null;
    state.syncFailed = false;
    state.projectSessionsError = false;
    state.tasks = [];
    state.meetings = [];
    state.decisions = [];
    state.decisionDrafts.clear();
    state.decisionRequests.clear();
    state.decisionSignature = null;
    state.liveMembers = [];
    state.assignments = [];
    state.taskDetails.clear();
    state.taskResults.clear();
    resetTaskChat(true);
    state.agents = [];
    state.observations = [];
    state.events = [];
    state.runners = [];
    state.order = [];
    state.eventCursor = 0;
  }
  $("project-label").textContent = state.workspace?.split(/[\\/]/).filter(Boolean).pop() || "选择本地项目目录";
  $("sidebar-project").textContent = $("project-label").textContent;
  $("project-label").title = state.workspace;
  if (changed && state.workspace)
    $("connect-form").elements.path.value = state.workspace;
  $("local-session-help").textContent =
    "已安全连接本地服务，选择目录即可开始。";
  renderSoftwareStatus();
  $("connect-form").querySelector("button.primary").disabled =
    state.discovering;
  if (status.refresh_pending && status.refresh_message)
    $("software-status").append(node("p", "muted", status.refresh_message));
  if (status.discovery_error || status.runner_error) {
    $("software-status").append(
      node("p", "error", status.discovery_error || status.runner_error),
    );
  }
  return changed;
}
function renderSoftwareStatus() {
  const target = $("software-status");
  target.replaceChildren();
  const statusLabels = {
    ready: "可用",
    available: "可用",
    not_installed: "未安装",
    missing: "未安装",
    unavailable: "暂不可用",
    unknown: "待检测",
    error: "检测失败",
  };
  for (const entry of state.catalogue) {
    const row = node("div", "software-status-row");
    row.append(
      node("strong", "", entry.label || entry.software),
      node(
        "span",
        "badge",
        statusLabels[entry.status] || entry.status || "待检测",
      ),
      node("p", "muted", entry.message || ""),
    );
    const providers = availableProviders(entry);
    const count = providers.reduce((n, p) => n + (p.models?.length || 0), 0);
    const free = providers.reduce(
      (n, p) => n + (p.models || []).filter((m) => m.free).length,
      0,
    );
    if (entry.auth?.label)
      row.append(
        node(
          "small",
          "",
          "登录状态：" +
            entry.auth.label +
            (entry.auth.message ? " · " + entry.auth.message : ""),
        ),
      );
    row.append(
      node(
        "small",
        "",
        "已发现 " +
          count +
          " 个可用模型" +
          (free ? " · 其中 " + free + " 个免费模型" : ""),
      ),
    );
    target.append(row);
  }
  if (state.discovering)
    target.append(
      node("p", "muted", "正在读取本机软件与模型目录，检测完成后会自动更新…"),
    );
  else if (!state.catalogue.length)
    target.append(
      node("p", "muted", "暂未发现模型，请检查软件安装和登录状态后重新检测。"),
    );
  const select = $("group-form").elements.software;
  const previous = select.value;
  select.replaceChildren(
    ...state.catalogue.map((entry) => {
      const providers = availableProviders(entry);
      const free = providers.reduce(
        (n, p) => n + (p.models || []).filter((m) => m.free).length,
        0,
      );
      const label = entry.label || entry.software;
      const option = new Option(
        free ? label + " · " + free + " 个免费模型" : label,
        entry.software,
      );
      option.disabled = !providers.some((p) => p.models?.length);
      return option;
    }),
  );
  if ([...select.options].some((o) => o.value === previous && !o.disabled))
    select.value = previous;
  else select.value = [...select.options].find((o) => !o.disabled)?.value || "";
  $("group-form").querySelector(
    'button[type="submit"], button.primary',
  ).disabled = !select.value;
}
function agentById(agentId) {
  return state.agentManager.items.find((item) => item.id === agentId);
}
function agentProviderById(agent, providerId) {
  return (agent?.providers || []).find((item) => item.id === providerId);
}
function agentStatusClass(status) {
  return "badge agent-" + (status || "unknown");
}
function agentModelCount(agent) {
  return (agent?.providers || []).reduce(
    (total, provider) => total + (provider.models?.length || 0),
    0,
  );
}
function agentSourceText(auth) {
  if (!auth) return "待检测";
  if (auth.source === "database")
    return "已加密保存" + (auth.masked ? " · " + auth.masked : "");
  if (auth.source === "environment") return "已从环境变量读取";
  if (auth.source === "dotenv") return "已从 .env 读取";
  if (auth.source === "vendor") return "已使用本机登录";
  if (auth.source === "free") return "免费模型无需登录";
  if (auth.source === "api" || auth.source === "configured")
    return "已配置登录或密钥";
  return "未配置";
}
function agentModelParams(model) {
  const params = model?.parameters || {};
  const values = [];
  for (const [key, allowed] of Object.entries(params)) {
    if (Array.isArray(allowed) && allowed.length)
      values.push(key + "：" + allowed.join(" / "));
    else if (typeof allowed === "string") values.push(key + "：" + allowed);
  }
  return values;
}
function agentSummaryCard(value, label) {
  const card = node("article", "agent-summary-card");
  card.append(node("strong", "", String(value)), node("small", "", label));
  return card;
}
async function loadAgents(force = false) {
  if (state.agentManager.loading) return;
  if (state.agentManager.loaded && !force) return;
  state.agentManager.loading = true;
  state.agentManager.error = "";
  renderAgents();
  try {
    const data = await localRequest("agents");
    state.agentManager.items = Array.isArray(data.agents) ? data.agents : [];
    state.agentManager.discovering = Boolean(data.discovering);
    state.agentManager.scannedAt = data.scanned_at || null;
    state.agentManager.loaded = true;
    if (state.agentManager.selected && !agentById(state.agentManager.selected))
      state.agentManager.selected = "";
    if (!state.agentManager.selected) {
      const installed = state.agentManager.items.filter(
        (item) => item.installed,
      );
      const preferred =
        installed.find((item) => item.auth?.state === "ready") || installed[0];
      state.agentManager.selected = preferred?.id || "";
    }
  } catch (error) {
    state.agentManager.error = error.message;
  } finally {
    state.agentManager.loading = false;
    renderAgents();
    if (state.agentManager.discovering) {
      clearTimeout(state.agentManager.retry);
      state.agentManager.retry = setTimeout(() => loadAgents(true), 1500);
    }
  }
}
function renderAgentCard(agent) {
  const selected = state.agentManager.selected === agent.id;
  const card = node(
    "article",
    "agent-card" +
      (selected ? " selected" : "") +
      (agent.installed ? "" : " not-installed"),
  );
  card.tabIndex = 0;
  card.setAttribute("role", "button");
  card.setAttribute(
    "aria-label",
    "查看 " + (agent.label || agent.id) + " 的模型与登录状态",
  );
  const choose = () => {
    state.agentManager.selected = agent.id;
    renderAgents();
    $("agent-detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
  };
  card.onclick = choose;
  card.onkeydown = (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      choose();
    }
  };
  const head = node("div", "agent-card-head");
  head.append(
    node("h3", "", agent.label || agent.id),
    node(
      "span",
      agentStatusClass(agent.auth?.state),
      agent.auth?.label || (agent.installed ? "待检测" : "未安装"),
    ),
  );
  card.append(head, node("p", "", agent.message || "等待扫描结果"));
  const meta = node("div", "agent-card-meta");
  meta.append(
    node("span", "", (agent.providers?.length || 0) + " 个供应商"),
    node("span", "", agentModelCount(agent) + " 个模型"),
    node("span", "", agent.installed ? "本机已安装" : "未检测到安装"),
  );
  card.append(meta);
  return card;
}
function renderAgentProvider(agent, provider) {
  const auth = provider.auth || {};
  const section = node("section", "agent-provider");
  const head = node("div", "agent-provider-head");
  head.append(
    node("strong", "", provider.label || provider.id),
    node("code", "", provider.id),
    node("span", agentStatusClass(auth.state), auth.label || "待检测"),
  );
  section.append(head);
  if (auth.message)
    section.append(node("p", "agent-auth-message", auth.message));
  if (auth.credential_env) {
    const row = node("div", "agent-key-row");
    row.append(node("code", "", auth.credential_env));
    row.append(
      node(
        "span",
        "agent-key-status" + (auth.stored ? " saved" : ""),
        agentSourceText(auth),
      ),
    );
    const actions = node("div", "agent-key-actions");
    const set = node(
      "button",
      "secondary",
      auth.stored ? "更换密钥" : "配置密钥",
    );
    set.type = "button";
    set.onclick = () => openAgentKey(agent, provider);
    actions.append(set);
    if (auth.stored) {
      const remove = node("button", "danger", "删除");
      remove.type = "button";
      remove.onclick = () => deleteAgentKey(agent, provider);
      actions.append(remove);
    }
    row.append(actions);
    section.append(row);
  }
  if (auth.kind === "account" && !auth.authenticated && !auth.message) {
    section.append(
      node(
        "p",
        "agent-hint",
        auth.credential_env
          ? "请先在 " +
              (agent.label || agent.id) +
              " 客户端登录，或配置 " +
              auth.credential_env +
              " 作为 API Key。"
          : "请先在 " +
              (agent.label || agent.id) +
              " 客户端登录账号，然后点击“重新扫描”。",
      ),
    );
  }
  const models = provider.models || [];
  if (models.length) {
    const details = node("details", "agent-models");
    details.append(
      node("summary", "", "模型配置 · " + models.length + " 个模型"),
    );
    const list = node("div", "agent-model-list");
    for (const model of models) {
      const item = node("div", "agent-model");
      const title = node("div", "");
      title.append(
        node("strong", "", model.label || model.id),
        node("code", "", model.id),
      );
      item.append(title);
      const params = agentModelParams(model);
      if (model.free) params.push("免费模型");
      if (params.length) {
        const wrap = node("div", "agent-model-params");
        for (const text of params)
          wrap.append(node("span", "agent-param", text));
        item.append(wrap);
      }
      list.append(item);
    }
    details.append(list);
    section.append(details);
  } else {
    section.append(
      node("p", "agent-auth-message", "未发现该供应商的模型列表。"),
    );
  }
  return section;
}
function renderAgentDetail() {
  const detail = $("agent-detail");
  const agent = agentById(state.agentManager.selected);
  if (!agent) {
    detail.hidden = true;
    detail.replaceChildren();
    return;
  }
  detail.hidden = false;
  detail.replaceChildren();
  const head = node("div", "agent-detail-head");
  const title = node("div", "");
  const heading = node("h3", "", agent.label || agent.id);
  heading.append(
    node(
      "span",
      agentStatusClass(agent.auth?.state),
      agent.auth?.label || "待检测",
    ),
  );
  title.append(
    heading,
    node("p", "muted", agent.auth?.message || agent.message || ""),
  );
  const close = node("button", "text-button", "收起详情");
  close.type = "button";
  close.onclick = () => {
    state.agentManager.selected = "";
    renderAgents();
  };
  head.append(title, close);
  detail.append(head);
  if (!agent.installed) {
    detail.append(
      node(
        "p",
        "agent-empty",
        "未检测到该智能体的本机安装。安装并登录后点击“重新扫描”。",
      ),
    );
    return;
  }
  if (!agent.providers?.length) {
    detail.append(
      node(
        "p",
        "agent-empty",
        "已检测到安装，但还没有可展示的模型供应商。请检查登录状态或供应商配置后重新扫描。",
      ),
    );
    return;
  }
  for (const provider of agent.providers)
    detail.append(renderAgentProvider(agent, provider));
}
function renderAgents() {
  const summary = $("agent-summary");
  if (!summary) return;
  const items = state.agentManager.items;
  const installed = items.filter((item) => item.installed);
  const ready = installed.filter((item) => item.auth?.state === "ready");
  const attention = installed.filter((item) =>
    ["missing_key", "signed_out"].includes(item.auth?.state),
  );
  summary.replaceChildren(
    agentSummaryCard(installed.length, "已安装智能体"),
    agentSummaryCard(ready.length, "已就绪"),
    agentSummaryCard(attention.length, "需要配置"),
    agentSummaryCard(
      installed.reduce((total, item) => total + agentModelCount(item), 0),
      "已发现模型",
    ),
  );
  const status = $("agent-status");
  if (state.agentManager.loading && !items.length)
    status.textContent = "正在扫描本机智能体…";
  else if (state.agentManager.error)
    status.textContent = state.agentManager.error;
  else if (state.agentManager.discovering || state.discovering)
    status.textContent = "正在扫描本机智能体与模型配置，完成后自动更新…";
  else if (state.agentManager.scannedAt)
    status.textContent =
      "最近扫描：" +
      new Date(state.agentManager.scannedAt * 1000).toLocaleTimeString();
  else status.textContent = "等待扫描结果。";
  const grid = $("agent-grid");
  grid.replaceChildren();
  if (state.agentManager.error)
    grid.append(node("p", "error", state.agentManager.error));
  else if (!items.length && state.agentManager.loading)
    grid.append(node("p", "agent-empty", "正在读取本机安装信息…"));
  else if (!items.length)
    grid.append(
      node(
        "p",
        "agent-empty",
        "暂未发现智能体。请确认本机已安装 Codex、OpenCode、ZCode 或 DSH。",
      ),
    );
  else for (const agent of items) grid.append(renderAgentCard(agent));
  renderAgentDetail();
}
function openAgentKey(agent, provider) {
  const auth = provider.auth || {};
  if (!auth.credential_env) return;
  state.agentManager.edit = {
    software: agent.id,
    provider: provider.id,
    name: auth.credential_env,
  };
  $("agent-key-context").textContent =
    (agent.label || agent.id) +
    " / " +
    (provider.label || provider.id) +
    " · " +
    auth.credential_env;
  $("agent-key-help").textContent = auth.stored
    ? "当前已保存 " +
      (auth.masked || "密钥") +
      "。输入新值会覆盖旧密钥，保存后仍只显示掩码。"
    : "密钥会加密写入本机 SQLite；页面和接口只返回掩码，不返回明文。";
  const input = $("agent-key-value");
  const errorBox = $("agent-key-error");
  errorBox.textContent = "";
  errorBox.hidden = true;
  input.value = "";
  $("agent-key-dialog").showModal();
  setTimeout(() => input.focus(), 0);
}
async function refreshAgentViews() {
  // A save or delete just changed server-side auth state; pull the catalogue
  // as well so group/member pickers see the provider immediately instead of
  // waiting for the next background poll.
  state.agentManager.loaded = false;
  await loadAgents(true);
  applyLocalStatus(await localRequest("status"));
}
async function deleteAgentKey(agent, provider) {
  const auth = provider.auth || {};
  if (!auth.credential_env) return;
  if (
    !window.confirm(
      "删除 " +
        (agent.label || agent.id) +
        " / " +
        (provider.label || provider.id) +
        " 的已保存密钥？",
    )
  )
    return;
  await action(async () => {
    const result = await localRequest("agent-credential-delete", {
      software: agent.id,
      provider: provider.id,
      name: auth.credential_env,
    });
    await refreshAgentViews();
    notice(
      result.deleted
        ? "密钥已从本地加密存储中删除。"
        : "该供应商已没有本地保存的密钥。",
    );
  });
}
$("agent-key-form").onsubmit = async (event) => {
  event.preventDefault();
  const edit = state.agentManager.edit;
  const input = $("agent-key-value");
  const errorBox = $("agent-key-error");
  const submit = event.target.querySelector('button[type="submit"]');
  if (!edit || state.busy) return;
  const value = input.value;
  if (!value.trim()) {
    errorBox.textContent = "请输入密钥";
    errorBox.hidden = false;
    return;
  }
  state.busy = true;
  if (submit) submit.disabled = true;
  errorBox.hidden = true;
  try {
    await localRequest("agent-credential", { ...edit, value });
    input.value = "";
    $("agent-key-dialog").close();
    await refreshAgentViews();
    notice("密钥已加密保存到本机数据库，后续执行会自动注入。");
  } catch (error) {
    // The global notice sits behind the open dialog; show the failure here.
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    state.busy = false;
    if (submit) submit.disabled = false;
  }
};
$("agents-refresh").onclick = () =>
  action(async () => {
    applyLocalStatus(await localRequest("refresh", {}));
    state.agentManager.loaded = false;
    await loadAgents(true);
    notice(
      state.discovering
        ? "正在重新扫描本机智能体，结果会自动更新。"
        : "本机智能体与模型配置已刷新。",
    );
  });
async function bootstrap() {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const code = fragment.get("pair");
  let pairingError = null;
  if (code) {
    // The one-time code is in the fragment so it never reaches server logs.
    // Exchange it for an HttpOnly session cookie before any local API call.
    history.replaceState(null, "", location.pathname + location.search);
    try {
      const response = await fetch("/local/session", {
        method: "POST",
        headers: {
          "X-RelayFence-Local": "1",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ code }),
        credentials: "same-origin",
        cache: "no-store",
        signal: AbortSignal.timeout(15000),
      });
      const result = await response.json().catch(() => null);
      if (!response.ok) {
        const message =
          result?.error?.message ||
          "配对链接已失效，请从启动服务的终端重新打开配对链接。";
        pairingError = message;
      }
    } catch {
      const message = "无法连接本地服务，请确认 relayfence serve 仍在运行。";
      pairingError = message;
    }
  }
  try {
    await loadCapabilities();
    applyLocalStatus(await localRequest("status"));
    loadAgents(true);
    if (state.project) {
      await refresh();
      notice(
        "本地项目已连接。软件与模型来自本机配置，可直接配置团队并派发任务。",
      );
    } else {
      notice("本地服务已连接，请选择已有项目目录。");
      await openProjects();
    }
  } catch (error) {
    const message = pairingError || error.message;
    $("local-session-help").textContent = message;
    notice(message, true);
  }
}
function node(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
function resetTaskChat(clearGroup = false) {
  const input = $("group-chat-form")?.elements?.message;
  if (input) input.value = "";
  state.taskChat = {
    group: clearGroup ? "" : state.taskChat.group,
    messages: new Map(),
    after: 0,
    before: null,
    hasMore: false,
    loading: false,
    loaded: false,
    signature: null,
    pending: null,
    sending: false,
    error: "",
    lastError: "",
    targetSignature: "",
    focusTask: "",
    scrollOlder: false,
    scrollSignature: null,
  };
}
function notice(text, error = false) {
  $("notice").textContent = text;
  $("notice").className = error ? "error" : "";
}
function id(prefix) {
  return prefix + "-" + crypto.randomUUID().slice(0, 8);
}
function people() {
  return state.config.groups.flatMap((g) => g.members.map((m) => ({ g, m })));
}
function routes() {
  return people().flatMap(({ g, m }) => [
    { g, m },
    ...(m.fallback_models || []).map((f, i) => ({
      g,
      m: {
        ...m,
        ...f,
        id: m.id + "/fallback/" + (i + 1),
        name: m.name + " · 备用 " + (i + 1),
      },
    })),
  ]);
}
function avatar(m) {
  const p = node("span", `person v${m.avatar} ${m.gender}`);
  p.setAttribute("aria-hidden", "true");
  for (const c of ["head", "body", "legs"]) p.append(node("i", c));
  return p;
}
// Assets are read from disk on every page load while Python modules are loaded once
// at process start, so the browser can be newer than the server. Ask what this
// process supports and skip anything it does not, instead of failing the whole view.
function supports(operation) {
  return !state.operations || state.operations.has(operation);
}
async function loadCapabilities() {
  // The newest surface this build needs. If the server lacks it, say so plainly.
  const required = ["office_snapshot_list", "task_delete", "decisions_list", "mcp_list"];
  try {
    const response = await fetch("/v1/capabilities", {
      method: "POST",
      headers: {
        "X-RelayFence-Local": "1",
        "Content-Type": "application/json",
      },
      body: "{}",
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
    const result = await response.json();
    if (response.status === 401 || response.status === 403) {
      // Authentication failure says nothing about the installed API version.
      state.operations = null;
      $("stale-server").hidden = true;
      return;
    }
    // Accept both the enveloped shape and a bare {operations:[...]} payload, so a
    // shape change can never silently disable this check again.
    const list = result?.data?.operations ?? result?.operations;
    if (Array.isArray(list)) {
      state.operations = new Set(list);
      $("stale-server").hidden = required.every((op) =>
        state.operations.has(op),
      );
      return;
    }
  } catch {
    // Older servers may not answer this endpoint at all.
  }
  // Unknown surface: assume the process predates these operations.
  state.operations = null;
  $("stale-server").hidden = false;
}
async function api(operation, extra = {}) {
  if (!state.project || (!state.authenticated && !state.token))
    throw Error("请先选择并连接本地项目目录。");
  if (!supports(operation))
    throw Error("当前服务版本不支持该功能，请重启 relayfence serve 后重试。");
  const body = JSON.stringify({ project: state.project, ...extra });
  if (extra.request_id) {
    if (
      state.pending &&
      (state.pending.operation !== operation || state.pending.body !== body)
    )
      throw Error("上一个写入请求结果未知，请先重试原请求，不要重复派发任务。");
    state.pending = { operation, extra, body };
    $("retry-request").hidden = false;
  }
  const response = await fetch("/v1/" + operation, {
    method: "POST",
    headers: {
      "X-RelayFence-Local": "1",
      "Content-Type": "application/json",
      ...(state.token ? { Authorization: "Bearer " + state.token } : {}),
    },
    body,
    cache: "no-store",
    signal: AbortSignal.timeout(15000),
  });
  const result = await response.json().catch(() => null);
  if (!result)
    throw Error("服务返回了无效响应，写入结果尚未确认，请重试原请求。");
  if (
    extra.request_id &&
    (result.ok || (result.error && response.status < 500))
  ) {
    state.pending = null;
    $("retry-request").hidden = true;
  }
  if (!response.ok || !result.ok)
    throw Error(
      (result.error?.code || response.status) +
        "：" +
        (result.error?.message || "请求失败"),
    );
  return result.data;
}
function mutation(extra) {
  return {
    actor: "office-operator",
    request_id: crypto.randomUUID(),
    ...extra,
  };
}
async function action(fn) {
  if (state.busy) return;
  state.busy = true;
  try {
    await fn();
  } catch (e) {
    notice(e.message, true);
  } finally {
    state.busy = false;
  }
}
async function pages(operation, key, extra = {}) {
  let out = [],
    after = "";
  for (let i = 0; i < 10; i++) {
    const data = await api(operation, { ...extra, after, limit: 200 });
    out.push(...data[key]);
    if (!data.next_cursor) return out;
    after = data.next_cursor;
  }
  throw Error("数据超过办公室视图容量，请在高级控制台查看。");
}
async function refresh() {
  // Coalesce overlapping requests, but never discard a refresh after a mutation
  // or project switch. Every caller awaits the final requested snapshot.
  state.refreshRequested = true;
  if (state.refreshPromise) return state.refreshPromise;
  state.refreshPromise = (async () => {
    state.refreshing = true;
    try {
      do {
        state.refreshRequested = false;
        const generation = state.generation;
        try {
          await refreshNow();
        } catch (error) {
          if (generation !== state.generation) continue;
          state.syncFailed = true;
          renderRuntime();
          $("connection-state").className = "connection";
          $("screen-live").textContent = "● 同步中断";
          throw error;
        }
      } while (state.refreshRequested);
    } finally {
      state.refreshing = false;
      state.refreshPromise = null;
    }
  })();
  return state.refreshPromise;
}
async function refreshNow() {
  const generation = state.generation;
  const result = await Promise.all([
    api("office_get"),
    pages("tasks_list", "tasks"),
    pages("agents_list", "agents"),
    api("quota_list"),
    api("events_list", { after: state.eventCursor, limit: 200 }),
    api("runners_list"),
    api("meetings_list"),
    api("office_activity"),
    supports("office_snapshot_list")
      ? api("office_snapshot_list")
      : Promise.resolve({ snapshots: [] }),
    supports("decisions_list")
      ? api("decisions_list")
      : Promise.resolve({ decisions: [] }),
    localRequest("project-sessions", { project: state.project }).catch(() => ({
      sessions: state.projectSessions,
      unavailable: true,
    })),
    supports("project_overview") ? api("project_overview") : Promise.resolve(null),
    localRequest("runtime", { project: state.project }).catch(() => null),
  ]);
  if (generation !== state.generation) return;
  [state.config, state.tasks, state.agents] = result;
  state.observations = result[3].observations;
  state.events = [...state.events, ...result[4].events].slice(-200);
  state.eventCursor = result[4].next_cursor;
  state.runners = result[5].runners;
  state.meetings = result[6].meetings;
  state.assignments = result[6].assignments;
  state.liveMembers = result[7].members;
  state.snapshots = result[8].snapshots;
  state.decisions = result[9].decisions;
  state.projectSessions = result[10].sessions || [];
  state.projectSessionsError = Boolean(result[10].unavailable);
  state.overview = result[11];
  state.runtime = result[12];
  state.syncFailed = false;
  renderRuntime();
  renderProjectSessions();
  render();
  $("last-sync").textContent = "同步于 " + new Date().toLocaleTimeString();
  $("connection-state").textContent = state.workspace
    ? state.workspace.split(/[\\/]/).filter(Boolean).pop() || state.workspace
    : "本地项目";
  $("connection-state").title = state.workspace;
  $("connection-state").className = "connection online";
  $("screen-live").textContent = "● CONNECTED";
}
async function save(groups) {
  const config = await api(
    "office_put",
    mutation({ expected_revision: state.config.revision, groups }),
  );
  state.config = config;
  state.teams.loaded = false;
  render();
  notice("办公室配置已保存。执行环境按所选软件和模型自动连接。");
  if (!$("teams-view").hidden) loadTeams(true);
}
function renderSnapshotCount() {
  const badge = $("snapshot-count");
  if (badge) badge.textContent = String(state.snapshots.length);
}
function snapshotSummary(s) {
  const when = new Date(s.created_at * 1000).toLocaleString();
  return `${s.label} · ${s.group_count} 组 / ${s.member_count} 人 · ${when}`;
}
async function saveSnapshot() {
  const groups = state.config.groups || [];
  if (!groups.length) throw Error("当前没有任何小组配置可保存。");
  const label = prompt(
    "给当前配置起个名字，方便以后恢复：",
    "配置 " + new Date().toLocaleDateString(),
  );
  if (label === null) return;
  if (!label.trim()) throw Error("名字不能为空。");
  await api(
    "office_snapshot_save",
    mutation({
      label: label.trim(),
      expected_revision: state.config.revision,
    }),
  );
  await refresh();
  notice("已保存快照“" + label.trim() + "”。");
}
async function restoreSnapshot(snapshotId) {
  const target = state.snapshots.find((s) => s.snapshot_id === snapshotId);
  if (!target) return;
  if (
    !confirm(
      "用快照“" +
        target.label +
        "”覆盖当前配置？\n\n当前的小组和成员设置会被替换。此操作会立即保存。",
    )
  )
    return;
  await api(
    "office_snapshot_restore",
    mutation({
      snapshot_id: snapshotId,
      expected_revision: state.config.revision,
    }),
  );
  await refresh();
  notice("已恢复到快照“" + target.label + "”。");
}
async function deleteSnapshot(snapshotId) {
  const target = state.snapshots.find((s) => s.snapshot_id === snapshotId);
  if (!target) return;
  if (!confirm("删除快照“" + target.label + "”？当前配置不受影响。")) return;
  await api("office_snapshot_delete", mutation({ snapshot_id: snapshotId }));
  await refresh();
  notice("快照已删除。");
}
function openSnapshotDialog() {
  const dialog = $("snapshot-dialog");
  const list = $("snapshot-list");
  list.replaceChildren();
  if (!state.snapshots.length) {
    list.append(
      node("p", "muted", "还没有保存过快照。点「保存当前配置」创建一个。"),
    );
  }
  for (const snapshot of state.snapshots) {
    const row = node("div", "snapshot-row");
    row.append(node("strong", "", snapshot.label));
    row.append(
      node(
        "small",
        "muted",
        `${snapshot.group_count} 组 / ${snapshot.member_count} 人 · ` +
          new Date(snapshot.created_at * 1000).toLocaleString(),
      ),
    );
    const tools = node("div", "snapshot-tools");
    const restore = node("button", "text-button", "恢复");
    restore.onclick = () =>
      action(async () => {
        dialog.close();
        await restoreSnapshot(snapshot.snapshot_id);
      });
    const remove = node("button", "text-button danger", "删除");
    remove.onclick = () =>
      action(async () => {
        await deleteSnapshot(snapshot.snapshot_id);
        openSnapshotDialog();
      });
    tools.append(restore, remove);
    row.append(tools);
    list.append(row);
  }
  dialog.showModal();
}
$("snapshot-save").onclick = () =>
  action(async () => {
    await saveSnapshot();
  });
$("snapshot-open").onclick = () => openSnapshotDialog();
function show(view) {
  for (const name of [
    "agents",
    "projects",
    "office",
    "teams",
    "meeting",
    "quota",
    "tasks",
    "skills",
    "logs",
  ])
    $(name + "-view").hidden = name !== view;
  document.querySelectorAll("[data-view]").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === view);
    b.classList.toggle("selected", b.dataset.view === view);
  });
  if (view === "agents")
    loadAgents(state.discovering || state.agentManager.discovering);
  if (view === "teams") loadTeams(false);
  if (view === "skills") loadSkills();
  if (view === "logs") loadLogs(true);
  if (view === "tasks") {
    ensureTaskChatGroup();
    renderTaskChat();
    loadGroupChat(!state.taskChat.loaded);
  }
}
document
  .querySelectorAll("[data-view]")
  .forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));
document
  .querySelectorAll("[data-close]")
  .forEach((b) =>
    b.addEventListener("click", () => $(b.dataset.close).close()),
  );
$("connect-button").onclick = () => $("connect-dialog").showModal();
async function openProjects() {
  show("projects");
  $("project-cards").replaceChildren(node("p", "muted", "正在读取项目…"));
  const data = await localRequest("projects");
  $("project-cards").replaceChildren();
  for (const project of data.projects) {
    const card = node("article", "card project-card");
    card.append(
      node("h3", "", project.name),
      node("p", "muted project-path", project.path),
      node(
        "p",
        "",
        `${project.groups} 个小组 · ${project.tasks} 个任务 · ${project.running_tasks || 0} 个执行中${project.active ? " · 当前项目" : ""}`,
      ),
    );
    const enter = node(
      "button",
      "primary",
      project.exists ? "进入项目 →" : "目录不存在",
    );
    enter.disabled = !project.exists;
    enter.onclick = () =>
      action(async () => {
        if (state.pending) throw Error("请先确认当前项目的待定写入请求。");
        applyLocalStatus(await localRequest("connect", { path: project.path }));
        await refresh();
        show("office");
        notice("已进入项目：" + project.name);
      });
    card.append(enter);
    $("project-cards").append(card);
  }
  if (!data.projects.length)
    $("project-cards").append(
      node(
        "p",
        "muted",
        "还没有项目。添加本地项目目录后，它会一直保存在这里。",
      ),
    );
}
$("projects-button").onclick = () => action(openProjects);
$("add-project-button").onclick = () => {
  $("connect-form").elements.path.value = "";
  $("connect-dialog").showModal();
};
$("save-project-button").onclick = () =>
  action(async () => {
    await localRequest("projects", {
      path: $("connect-form").elements.path.value.trim(),
    });
    $("connect-dialog").close();
    await openProjects();
    notice("项目已添加，可随时进入配置团队。");
  });
$("retry-request").onclick = () =>
  action(async () => {
    if (!state.pending) return;
    const { operation, extra } = state.pending;
    await api(operation, extra);
    await refresh();
    notice("原请求已确认，使用相同请求 ID，没有重复派发。");
  });
$("connect-form").onsubmit = (e) => {
  e.preventDefault();
  const path = e.currentTarget.elements.path.value.trim();
  action(async () => {
    if (state.pending) throw Error("请先确认上一个项目的待定写入请求。");
    const status = await localRequest("connect", { path });
    applyLocalStatus(status);
    await refresh();
    $("connect-dialog").close();
    show("office");
    notice("已连接本地目录。软件、模型和执行环境已自动检测。");
  });
};
let directoryGeneration = 0;
async function browseDirectory(path = "") {
  const generation = ++directoryGeneration;
  $("directory-browser").hidden = false;
  $("directory-message").textContent = "正在读取文件夹…";
  $("directory-list").replaceChildren();
  try {
    const result = await localRequest("browse", { path });
    if (generation !== directoryGeneration) return;
    $("directory-location").textContent = result.path || "本机磁盘";
    const button = (label, callback) => {
      const element = node("button", "secondary", label);
      element.type = "button";
      element.onclick = callback;
      $("directory-list").append(element);
    };
    if (result.path) {
      button("✓ 选择此文件夹", () => {
        $("connect-form").elements.path.value = result.path;
        $("directory-browser").hidden = true;
      });
      button("↑ 上一级", () => browseDirectory(result.parent || ""));
    }
    for (const entry of result.directories)
      button("📁 " + entry.name, () => browseDirectory(entry.path));
    $("directory-message").textContent = result.truncated
      ? "目录较多，仅显示前 500 项；也可直接填写项目路径。"
      : result.directories.length
        ? "点击文件夹进入，再选择此文件夹。"
        : "没有子文件夹，可以选择当前目录。";
  } catch (error) {
    if (generation !== directoryGeneration) return;
    $("directory-message").textContent = error.message;
    const retry = node("button", "secondary", "返回本机磁盘");
    retry.type = "button";
    retry.onclick = () => browseDirectory("");
    $("directory-list").replaceChildren(retry);
  }
}
$("browse-project").onclick = () =>
  browseDirectory($("connect-form").elements.path.value.trim());
$("close-directory-browser").onclick = () => {
  directoryGeneration++;
  $("directory-browser").hidden = true;
};
$("discover-button").onclick = () =>
  action(async () => {
    applyLocalStatus(await localRequest("refresh", {}));
    if (state.project) await refresh();
    notice(
      state.discovering
        ? "正在重新检测本机软件与模型，结果会自动更新。"
        : "本机软件与模型目录已刷新。",
    );
  });
$("refresh-button").onclick = () =>
  action(async () => {
    applyLocalStatus(await localRequest("refresh", {}));
    if (state.project) await refresh();
  });
$("skills-refresh").onclick = () =>
  action(async () => {
    if (!state.project) {
      notice("请先连接本地项目目录。", true);
      return;
    }
    state.skills = { skills: [], workspace: "" };
    await loadSkills();
    notice("已重新扫描本机技能。");
  });
function taskFor(m) {
  return state.tasks
    .filter((t) => assignedMember(t)?.m.id === m.id)
    .sort(
      (a, b) =>
        taskStatusOrder(a.status) - taskStatusOrder(b.status) ||
        b.updated_at - a.updated_at,
    )[0];
}
function chatRoleLabel(message) {
  if (message.kind === "decision")
    return message.role === "user" ? "你的确认回复" : "成员提出问题";
  if (message.role === "user") return "你的引导";
  if (message.role === "assistant") return "成员任务结果";
  return "执行记录";
}
function appendChatText(target, text) {
  const source = String(text ?? "");
  const fence = /```([^\n`]*)\n?([\s\S]*?)```/g;
  let cursor = 0;
  for (const match of source.matchAll(fence)) {
    if (match.index > cursor)
      target.append(
        node("div", "chat-text", source.slice(cursor, match.index)),
      );
    const pre = node("pre", "chat-code");
    pre.append(node("code", "", match[2].replace(/\n$/, "")));
    target.append(pre);
    cursor = match.index + match[0].length;
  }
  if (cursor < source.length)
    target.append(node("div", "chat-text", source.slice(cursor)));
}
// Activity is what the member is doing right now: a command it ran, a file it read,
// or a line of reasoning. Rendering it as a timeline step is what makes a run
// legible while it is still in progress.
const activityLabels = {
  command: "执行",
  output: "输出",
  note: "思考",
  result: "交付",
  error: "错误",
};
function renderActivityMessage(message, date) {
  const kind = message.entry_kind || "note";
  const wrap = node(
    "article",
    "chat-activity kind-" +
      kind +
      (message.status === "failed" ? " failed" : ""),
  );
  const head = node("div", "chat-activity-head");
  head.append(
    node("span", "chat-activity-badge", activityLabels[kind] || kind),
    node("span", "chat-activity-who", message.member_name || ""),
  );
  if (message.source_agent)
    head.append(
      node(
        "span",
        "session-chip",
        (message.parent_agent ? "子会话 · " : "会话 · ") + message.source_agent,
      ),
    );
  if (message.parent_agent) wrap.classList.add("child-session");
  if (message.status === "failed")
    head.append(node("span", "chat-activity-state", "失败"));
  head.append(node("time", "", date.toLocaleTimeString()));
  wrap.append(head);
  const content = String(message.content || "");
  if (content) {
    const body = node("div", "chat-activity-body");
    appendChatText(body, content);
    wrap.append(body);
  }
  const context = message.task_title || message.task_id;
  if (context)
    wrap.append(node("small", "chat-activity-task", "关于：" + context));
  return wrap;
}
function appendChatContent(card, message) {
  const content = String(message.content || message.text || "");
  const body = node("div", "chat-body");
  if (content.length > 2400) {
    card.append(
      node("p", "chat-preview", content.slice(0, 600).trimEnd() + " …"),
    );
    const details = node("details", "chat-long");
    details.append(
      node(
        "summary",
        "",
        `展开完整内容 · ${content.length.toLocaleString()} 字`,
      ),
    );
    appendChatText(body, content);
    details.append(body);
    card.append(details);
  } else {
    appendChatText(body, content);
    if (content) card.append(body);
  }
}
function renderChatMessages(chat) {
  const box = $("chat-messages");
  const previousHeight = box.scrollHeight;
  const previousTop = box.scrollTop;
  const nearBottom =
    previousHeight === 0 ||
    previousHeight - previousTop - box.clientHeight < 80;
  const messages = [...chat.messages.values()].filter(message => chat.view === "activity"
    ? message.kind === "activity" || message.role === "system"
    : message.kind !== "activity" && message.role !== "system").sort(
    (a, b) =>
      a.created_at - b.created_at || String(a.id).localeCompare(String(b.id)),
  );
  const signature = JSON.stringify(
    messages.map((m) => [
      m.id,
      m.status || "",
      m.detail || "",
      String(m.content || m.text || "").length,
    ]),
  );
  if (signature !== chat.signature) {
    const fragment = document.createDocumentFragment();
    let previousDay = "";
    for (const message of messages) {
      const date = new Date(message.created_at * 1000);
      const day = date.toLocaleDateString();
      if (day !== previousDay) {
        fragment.append(node("div", "chat-day", day));
        previousDay = day;
      }
      if (message.kind === "activity") {
        fragment.append(renderActivityMessage(message, date));
        continue;
      }
      const card = node("article", "chat-message " + message.role);
      const head = node("header", "chat-message-head");
      head.append(
        node("strong", "", chatRoleLabel(message)),
        node("time", "", date.toLocaleTimeString()),
      );
      card.append(head);
      appendChatContent(card, message);
      const statuses = {
        pending: "等待执行器停止",
        applied: "已加入续跑指令",
        failed: "未能续跑",
        received: "已保存",
      };
      if (message.status)
        card.append(
          node(
            "span",
            "chat-status-pill " + message.status,
            statuses[message.status] || message.status,
          ),
        );
      if (message.detail) card.append(node("p", "muted", message.detail));
      fragment.append(card);
    }
    if (!messages.length)
      fragment.append(
        node("p", "muted", chat.view === "activity" ? "尚无已记录的执行事件，可加载更早记录。" : "这里显示引导记录和实际完成结果，不生成模拟对话。"),
      );
    box.replaceChildren(fragment);
    chat.signature = signature;
    if (chat.scrollOlder && signature !== chat.scrollSignature) {
      box.scrollTop = previousTop + (box.scrollHeight - previousHeight);
    } else if (nearBottom) {
      box.scrollTop = box.scrollHeight;
    } else {
      box.scrollTop = previousTop;
    }
    chat.scrollOlder = false;
    chat.scrollSignature = signature;
  }
  $("chat-load-older").hidden = !chat.hasMore;
  $("chat-load-older").textContent = "加载更早记录";
  $("chat-sync").textContent = "更新于 " + new Date().toLocaleTimeString();
}
async function openMemberChat(groupId, memberId) {
  const member = state.config.groups
    .find((g) => g.id === groupId)
    ?.members.find((m) => m.id === memberId);
  if (!member) return;
  const current = state.chat;
  const panelOpen = !$("member-chat").hidden;
  if (
    current &&
    panelOpen &&
    current.groupId === groupId &&
    current.memberId === memberId
  ) {
    $("member-chat-form").elements.message.focus();
    return;
  }
  if (current && panelOpen) {
    const draft = $("member-chat-form").elements.message.value.trim();
    if (current.pending || current.sending || draft) {
      const reason =
        current.pending || current.sending
          ? "上一条引导的发送结果尚未确认；切换成员会丢失重试状态和草稿。"
          : "当前引导尚未发送；切换成员会清空草稿。";
      if (!window.confirm(reason + "仍要切换？")) return;
    }
  }
  state.chat = {
    groupId,
    memberId,
    view: "conversation",
    project: state.project,
    taskId: taskFor(member)?.task_id || null,
    pending: null,
    messages: new Map(),
    before: null,
    hasMore: false,
    loaded: false,
    signature: null,
    scrollOlder: false,
    scrollSignature: null,
  };
  $("member-conversation-tab").setAttribute("aria-selected", "true");
  $("member-activity-tab").setAttribute("aria-selected", "false");
  $("chat-messages").setAttribute("aria-labelledby", "member-conversation-tab");
  $("chat-title").textContent = member.name + " · 任务对话";
  $("chat-member-meta").textContent = member.custom_position || state.config.positions[member.position]?.label || member.position;
  $("chat-model").textContent = [member.provider, member.model].filter(Boolean).join(" / ");
  state.chatReturnFocus = document.activeElement;
  $("member-chat").hidden = false;
  document.body.classList.add("member-chat-open");
  $("close-member-chat").focus();
  $("member-chat-form").reset();
  $("member-chat-form").hidden = false;
  $("chat-status").textContent = "";
  $("chat-messages").replaceChildren();
  $("chat-load-older").hidden = true;
  await refreshMemberChat();
}
function closeMemberChat(force = false) {
  const chat = state.chat;
  const draft = $("member-chat").hidden
    ? ""
    : $("member-chat-form").elements.message.value.trim();
  if (!force && chat && (chat.pending || chat.sending || draft)) {
    const reason =
      chat.pending || chat.sending
        ? "上一条引导的发送结果尚未确认；关闭窗口会丢失重试状态和草稿。"
        : "当前引导尚未发送；关闭窗口会清空草稿。";
    if (!window.confirm(reason + "仍要关闭？")) return;
  }
  state.chat = null;
  $("member-chat").hidden = true;
  document.body.classList.remove("member-chat-open");
  const target = state.chatReturnFocus;
  state.chatReturnFocus = null;
  if (target && typeof target.focus === "function") target.focus();
}
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("member-chat").hidden) {
    event.preventDefault();
    closeMemberChat();
  }
});
async function refreshMemberChat(older = false) {
  const chat = state.chat;
  if (!chat || chat.loading || $("member-chat").hidden) return;
  if (older && !chat.hasMore) return;
  chat.loading = true;
  if (older) chat.scrollOlder = true;
  try {
    const payload = {
      project: chat.project,
      member_id: chat.memberId,
      limit: 100,
    };
    if (older) payload.before = chat.before;
    const data = await localRequest("member-chat", payload);
    if (state.chat !== chat) return;
    for (const message of data.messages || [])
      chat.messages.set(message.id, message);
    if (older) {
      chat.hasMore = Boolean(data.has_more);
      chat.before = data.next_before;
      if (!(data.messages || []).length) chat.scrollOlder = false;
    } else if (!chat.loaded) {
      chat.hasMore = Boolean(data.has_more);
      chat.before = data.next_before;
    }
    chat.loaded = true;
    const tasks = data.tasks || [];
    const pendingTask = chat.pending
      ? tasks.find((t) => t.task_id === chat.pending.task_id)
      : null;
    let closedPending = false;
    if (
      chat.pending &&
      !chat.sending &&
      (!pendingTask ||
        !["running", "ready", "paused"].includes(pendingTask.status))
    ) {
      // The unresolved send can no longer be replayed against its task.
      chat.pending = null;
      chat.lastError = null;
      closedPending = true;
      $("chat-status").textContent =
        "原引导的目标任务已结束；未确认的发送已关闭。";
    }
    const currentTask = tasks.find((t) => t.task_id === chat.taskId);
    const activeTasks = tasks
      .filter((t) => ["running", "ready", "paused"].includes(t.status))
      .sort(
        (a, b) =>
          taskStatusOrder(a.status) - taskStatusOrder(b.status) ||
          b.updated_at - a.updated_at,
      );
    const draft = $("member-chat-form").elements.message.value.trim();
    // Stay on the task the operator opened when a draft exists; switching
    // automatically would retarget the text. With no draft, move to the best
    // active task once the old one finishes.
    let task = currentTask;
    if (chat.pending && pendingTask) task = pendingTask;
    else if (
      (!currentTask ||
        !["running", "ready", "paused"].includes(currentTask.status)) &&
      !draft
    )
      task = activeTasks[0] || (currentTask ? currentTask : tasks[0]);
    chat.taskId = task ? task.task_id : null;
    $("chat-task").textContent = task
      ? `${task.title} · ${labels[task.display_status] || labels[task.status] || task.status}`
      : activeTasks.length
        ? "草稿保留；重新打开成员对话可切换任务"
        : tasks.length
          ? "草稿保留；当前没有可切换的进行中任务"
          : "该成员还没有任务";
    renderChatMessages(chat);
    const enabled =
      task && ["running", "ready", "paused"].includes(task.status);
    const submit = $("member-chat-form").querySelector('button[type="submit"]');
    $("member-chat-form").hidden = !enabled && !draft && !chat.pending;
    submit.disabled = !enabled || chat.sending;
    submit.textContent = chat.pending ? "重试发送" : "发送引导";
    if (!chat.pending && !chat.lastError && !closedPending) {
      const normal = enabled
        ? "可输入指令调整任务；运行中的任务会先停止再继续。"
        : activeTasks.length
          ? "当前任务已结束；清空草稿或重新打开成员对话可切换到进行中任务。"
          : "当前没有可调整的进行中任务。";
      if ($("chat-status").textContent !== normal)
        $("chat-status").textContent = normal;
    }
  } catch (error) {
    chat.scrollOlder = false;
    if (state.chat === chat && $("chat-status").textContent !== error.message)
      $("chat-status").textContent = error.message;
  } finally {
    chat.loading = false;
  }
}
$("chat-load-older").onclick = () => refreshMemberChat(true);
$("close-member-chat").onclick = () => closeMemberChat();
$("chat-edit-member").onclick = () => {
  if (state.chat) editMember(state.chat.groupId, state.chat.memberId);
};
$("member-chat-form").onsubmit = async (event) => {
  event.preventDefault();
  const chat = state.chat;
  if (!chat || chat.sending || !chat.taskId) return;
  const message = event.currentTarget.elements.message.value.trim();
  if (!message) {
    $("chat-status").textContent = "请输入引导内容。";
    return;
  }
  if (chat.pending && chat.pending.message !== message) {
    $("chat-status").textContent =
      "上一条引导的发送结果尚未确认；请保持原内容重试。";
    return;
  }
  chat.pending ||= {
    project: chat.project,
    member_id: chat.memberId,
    task_id: chat.taskId,
    message,
    request_id: crypto.randomUUID(),
  };
  chat.sending = true;
  chat.lastError = null;
  const button = $("member-chat-form").querySelector('button[type="submit"]');
  button.disabled = true;
  $("chat-status").textContent = "正在提交引导…";
  try {
    await localRequest("member-guide", chat.pending);
    chat.pending = null;
    if (state.chat === chat) $("member-chat-form").reset();
  } catch (error) {
    if (error.status >= 400 && error.status < 500) chat.pending = null;
    chat.lastError = error.message;
    if (state.chat === chat)
      $("chat-status").textContent =
        error.message +
        (chat.pending ? "。再次点击会使用相同请求重试，不重复添加。" : "");
  } finally {
    chat.sending = false;
    if (state.chat === chat) {
      button.disabled = false;
      await refreshMemberChat();
    }
  }
};
setInterval(refreshMemberChat, 2000);
function renderGroups() {
  const target = $("groups");
  target.replaceChildren();
  if (!state.config.groups.length) {
    const empty = node("div", "empty-office");
    empty.append(
      node("h3", "", "你的下一支团队，从这里开始。"),
      node(
        "p",
        "",
        "新建小组或选择已有小组。配置好多少位成员，办公室就摆放多少张工位。",
      ),
    );
    target.append(empty);
    return;
  }
  for (const g of state.config.groups) {
    const group = node("section", "group " + g.software),
      header = node("div", "group-header"),
      title = node("h3");
    title.append(
      node("span", "software-icon", g.software[0].toUpperCase()),
      document.createTextNode(g.name),
    );
    header.append(
      title,
      node("small", "", g.members.length + " 位 · " + g.software),
    );
    const rename = node("button", "text-button", "重命名");
    rename.onclick = () => {
      renameGroupId = g.id;
      renameGroupProject = state.project;
      $("rename-group-form").elements.name.value = g.name;
      $("rename-group-dialog").showModal();
    };
    const remove = node("button", "text-button danger-text", "删除");
    remove.onclick = () => deleteProjectGroup(state.project, g);
    header.append(rename, remove);
    const desks = node("div", "desks");
    for (const m of g.members) {
      const task = taskFor(m),
        desk = node("button", "desk " + (task?.status || ""));
      desk.type = "button";
      desk.setAttribute(
        "aria-label",
        m.name +
          "，" +
          (m.custom_position ||
            state.config.positions[m.position]?.label ||
            m.position) +
          "，配置成员",
      );
      desk.onclick = () =>
        task ? openMemberChat(g.id, m.id) : editMember(g.id, m.id);
      if (task && task.status !== "cancelled") {
        desk.append(
          node(
            "span",
            "speech",
            (labels[task.display_status] ||
              labels[task.status] ||
              task.status) +
              "：" +
              task.title,
          ),
        );
      }
      const top = node("span", "desk-top");
      top.style.display = "block";
      top.append(node("i", "monitor"), node("i", "keyboard"), node("i", "mug"));
      desk.append(
        top,
        avatar(m),
        node(
          "span",
          "desk-name",
          m.name + (m.relationship === "leader" ? " · 组长" : ""),
        ),
        node(
          "span",
          "desk-job",
          m.custom_position ||
            state.config.positions[m.position]?.label ||
            m.position,
        ),
        node(
          "span",
          "desk-status",
          m.relationship === "native_subagent"
            ? "原生子 Agent · 待适配"
            : !m.agent_id
              ? "本地软件暂不可用"
              : state.runners.some((r) => r.agent_id === m.agent_id && r.online)
                ? "本地执行环境在线"
                : "等待本地执行环境",
        ),
      );
      desks.append(desk);
    }
    const add = node("button", "add-desk");
    add.type = "button";
    add.append(node("b", "", "＋"), document.createTextNode("添加成员"));
    add.onclick = () => editMember(g.id);
    desks.append(add);
    group.append(header, desks);
    target.append(group);
  }
}
function renderStats() {
  for (const [key, status] of [
    ["running", "running"],
    ["ready", "ready"],
    ["done", "done"],
  ])
    $("stat-" + key).textContent = (state.overview ? (key === "done" ? state.overview.progress.completed : state.overview.counts[status] || 0) : null) ?? state.tasks.filter(
      (t) => (t.display_status || t.status) === status && (key !== "done" || t.result_freshness?.fresh !== false),
    ).length;
  $("stat-team").textContent = people().length;
  const active =
    state.tasks.find((t) => (t.display_status || t.status) === "running") ||
    state.tasks.filter((t) => (t.display_status || t.status) === "ready").at(-1);
  $("mission-title").textContent = active
    ? active.title
    : state.tasks.length
      ? "当前任务已同步 · 前往会议室查看"
      : "等待第一场任务简报";
}
function renderActivity() {
  const target = $("activity");
  target.replaceChildren();
  for (const event of state.events.slice(-6).reverse()) {
    const row = node("div", "activity-row");
    row.append(
      node("time", "", new Date(event.created_at * 1000).toLocaleTimeString()),
      node("p", "", event.kind + " · " + event.subject),
    );
    target.append(row);
  }
  if (!state.events.length)
    target.append(
      node("p", "muted", "还没有项目动态。完成配置后，可召开第一场任务简报。"),
    );
}
function quotaFor(m) {
  return state.observations.find((o) => o.route_id === m.id);
}
function quotaLabel(o) {
  return !o || o.stale
    ? "未知" + (o?.stale ? " · 已过期" : "")
    : (labels[o.status] || o.status) +
        (o.source === "operator" ? " · 人工观察" : "");
}
function renderQuotas() {
  const mini = $("quota-mini"),
    cards = $("quota-cards");
  mini.replaceChildren();
  cards.replaceChildren();
  for (const { g, m } of routes()) {
    const o = quotaFor(m),
      row = node("div", "quota-row");
    row.append(
      node("span", "", m.name + " · " + m.model),
      node(
        "span",
        "badge " + (o?.stale ? "unknown" : o?.status || "unknown"),
        quotaLabel(o),
      ),
    );
    mini.append(row);
    const card = node("section", "card quota-card");
    card.append(
      node("h3", "", m.name + " · " + g.name),
      node("small", "", m.provider + " / " + m.model),
      node(
        "div",
        "quota-number",
        o && !o.stale && o.remaining !== null ? String(o.remaining) : "—",
      ),
      node("span", "badge " + (o?.status || "unknown"), quotaLabel(o)),
      node(
        "small",
        "",
        o
          ? "来源：" + o.source + " · " + (o.unit || "无余额数值")
          : "尚无额度数据",
      ),
      node(
        "small",
        "",
        o
          ? "观察时间：" + new Date(o.observed_at * 1000).toLocaleString()
          : "等待适配器或人工报告",
      ),
    );
    if (o?.detail) card.append(node("p", "muted", o.detail));
    const button = node("button", "text-button", "记录人工观察 →");
    button.onclick = () => {
      state.route = m.id;
      $("observation-form").reset();
      $("observation-dialog").showModal();
    };
    card.append(button);
    cards.append(card);
  }
  if (!people().length) {
    mini.append(node("p", "muted", "配置成员后，模型额度会显示在这里。"));
    cards.append(node("p", "muted", "办公室尚未配置模型。"));
  }
  for (const selectId of ["route-member", "handoff-member"]) {
    const select = $(selectId),
      old = select.value;
    select.replaceChildren(
      ...routes().map(
        ({ g, m }) =>
          new Option(g.name + " / " + m.name + " / " + m.model, m.id),
      ),
    );
    if ([...select.options].some((o) => o.value === old)) select.value = old;
  }
}
function renderMeeting() {
  renderMeetingCapabilities();
  const roster = $("meeting-roster"),
    seats = $("meeting-people"),
    board = $("task-board");
  const leaderSelect = $("meeting-leader"),
    chosenLeader = leaderSelect.value;
  leaderSelect.replaceChildren(
    ...people()
      .filter(({ m }) => m.agent_id && m.relationship !== "native_subagent")
      .map(({ g, m }) => new Option(g.name + " · " + m.name, m.id)),
  );
  if ([...leaderSelect.options].some((o) => o.value === chosenLeader))
    leaderSelect.value = chosenLeader;
  roster.replaceChildren();
  seats.replaceChildren();
  board.replaceChildren();
  state.order = state.order.filter((mid) =>
    people().some(({ m }) => m.id === mid),
  );
  for (const { g, m } of people()) {
    const label = node("label", "checkbox"),
      check = node("input");
    check.type = "checkbox";
    check.value = m.id;
    check.checked = state.order.includes(m.id);
    check.disabled = !m.agent_id || m.relationship === "native_subagent";
    check.onchange = () => {
      state.order = state.order.filter((x) => x !== m.id);
      if (check.checked) state.order.push(m.id);
      renderMeeting();
    };
    label.append(
      check,
      document.createTextNode(
        g.name +
          " · " +
          m.name +
          " / " +
          (m.custom_position ||
            state.config.positions[m.position]?.label ||
            m.position) +
          (check.disabled ? "（待接入）" : ""),
      ),
    );
    roster.append(label);
    if (check.checked) {
      const seat = node("div", "meeting-seat");
      seat.append(avatar(m), node("small", "", m.name));
      seats.append(seat);
    }
  }
  $("meeting-order").replaceChildren(
    ...state.order.map((mid) =>
      node("li", "", people().find(({ m }) => m.id === mid).m.name),
    ),
  );
  for (const task of state.tasks.slice().reverse().slice(0, 60)) {
    // A board entry is a way into the task's live conversation, not a dead label:
    // clicking it must show what that task is actually doing.
    const card = node("article", "task-card");
    const live = state.taskChat.messages;
    let lastActivity = null;
    for (const message of live.values()) {
      if (message.task_id !== task.task_id || message.kind !== "activity")
        continue;
      if (!lastActivity || (message.log_id || 0) > (lastActivity.log_id || 0))
        lastActivity = message;
    }
    card.append(
      node(
        "span",
        "badge " + task.status,
        labels[task.display_status] || labels[task.status] || task.status,
      ),
      node("strong", "", task.title),
      node("small", "", task.task_id),
    );
    if (lastActivity) {
      const strip = node("div", "task-card-live");
      strip.append(
        node("span", "task-card-live-dot"),
        node(
          "span",
          "task-card-live-copy",
          (lastActivity.entry_kind === "command" ? "执行：" : "") +
            String(lastActivity.content || "").slice(0, 90),
        ),
      );
      card.append(strip);
    }
    if (task.failure) card.append(node("p", "muted", task.failure.message));
    card.tabIndex = 0;
    card.setAttribute("role", "button");
    const open = () => {
      const target = assignedMember(task);
      if (!target) {
        show("tasks");
        notice("该任务未绑定办公室小组，无法打开小组对话。", true);
        return;
      }
      if (state.taskChat.group !== target.g.id) {
        if (!taskChatCanLeaveGroup(target.g.id)) return;
        state.taskChat.group = target.g.id;
        resetTaskChat(false);
      }
      state.taskChat.focusTask = task.task_id;
      show("tasks");
      // Keep the composer aimed at the card the operator opened; otherwise a
      // task card would show one execution while the next send went elsewhere.
      chooseTaskChatTarget(task.task_id);
    };
    card.onclick = open;
    card.onkeydown = (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    };
    board.append(card);
  }
  if (!state.tasks.length)
    board.append(node("p", "muted", "会议派发后，真实任务会出现在这里。"));
}
function renderSkills() {
  const target = $("skill-groups");
  if (state.skillLoading) {target.replaceChildren(node("p", "muted", "正在扫描本机能力…")); return;}
  const software = $("capability-software").value;
  const type = $("capability-type").value;
  const query = $("capability-query").value.trim().toLowerCase();
  const origins = {user:"软件配置", project:"项目内", plugin:"插件声明", environment:"环境配置"};
  const rows = [];
  for (const group of state.skills.skills || []) for (const skill of group.skills || []) {
    rows.push({name:skill.name, type:"skill", software:group.software, label:group.label,
      origin:skill.origin, status:skill.disabled_by_config ? "默认禁用" : "已安装", description:skill.description,
      detail:skill.path, enabled:people().filter(({g,m}) => g.software === group.software && (m.skills || []).includes(skill.name)).map(({m}) => m.name)});
  }
  for (const group of state.skills.mcp || []) for (const server of group.servers || []) {
    rows.push({name:server.name, type:"mcp", software:group.software, label:group.label, origin:server.origin,
      status:server.origin === "plugin" ? "插件声明 · 未核实启用" : server.enabled ? "已配置 · 未检测连接" : "配置已禁用",
      description:server.selectable ? "可在成员或本次任务中选择。只控制本地配置的 MCP；插件和组织策略由软件管理。" : "由软件管理加载；当前仅支持沿用软件配置。",
      detail:"传输：" + server.transport + "。扫描不会连接服务或执行命令。", enabled:[]});
  }
  const filtered = rows.filter(row => (!software || row.software === software) && (!type || row.type === type) &&
    (!query || [row.name,row.description,row.label].join(" ").toLowerCase().includes(query)));
  $("skill-summary").textContent = `Skill ${rows.filter(r => r.type === "skill").length} 项 · MCP ${rows.filter(r => r.type === "mcp").length} 项 · 当前显示 ${filtered.length} 项 · 只读扫描` +
    (state.skills.capabilities_version ? "" : " · MCP 目录需要重启本地服务后读取");
  const wrapper = node("div", "capability-table-wrap"); const table = node("table", "capability-table");
  table.append(node("caption", "", "本地 Skill 与 MCP 能力目录；配置状态不等同于连接状态。"));
  const head = node("thead"), tr = node("tr");
  for (const label of ["名称", "类型", "智能体", "来源", "状态", "说明 / 详情"]) {
    const cell = node("th", "", label); cell.scope = "col"; tr.append(cell);
  }
  head.append(tr); table.append(head); const body = node("tbody");
  for (const row of filtered) {
    const line = node("tr");
    for (const value of [row.name, row.type === "skill" ? "Skill" : "MCP", row.label, origins[row.origin] || row.origin, row.status]) line.append(node("td", "", value));
    const detail = node("td"), details = node("details");
    details.append(node("summary", "", "展开说明"), node("p", "", row.description), node("p", "", row.detail));
    if (row.enabled.length) details.append(node("p", "", "已选成员：" + row.enabled.join("、")));
    detail.append(details); line.append(detail); body.append(line);
  }
  if (!filtered.length) {const line = node("tr"), cell = node("td", "muted", query || type || software ? "没有匹配的能力，可调整筛选条件。" : "没有发现本地能力配置。可先在对应智能体软件中安装或配置，再重新扫描。"); cell.colSpan = 6;line.append(cell);body.append(line);}
  table.append(body);wrapper.append(table);target.replaceChildren(wrapper);
  for (const entry of state.skills.mcp || []) if (entry.warnings?.length) target.append(node("p", "error", entry.label + " 的部分配置无法完整读取，请检查该软件配置后重试。"));
}
async function loadSkills() {
  if (state.skillLoading) return;
  state.skillLoading = true;
  renderSkills();
  try {
    state.skills = await api("skill_list");
    renderMeetingCapabilities();
  } catch (error) {
    notice(error.message, true);
    state.skills = { skills: [], workspace: "" };
  } finally {
    state.skillLoading = false;
    renderSkills();
  }
}
function render() {
  renderWorkbench();
  renderRuntime();
  renderProjectSessions();
  renderGroups();
  renderStats();
  renderActivity();
  renderQuotas();
  renderMeeting();
  renderTaskDashboard();
  renderMemberWall();
  renderDecisions();
  renderSkills();
  renderLogGroups();
  renderAgents();
  renderSnapshotCount();
}
function makeMember(
  software,
  name,
  position,
  relationship = "independent_session",
  index = 0,
) {
  return {
    id: id("person"),
    name,
    position,
    relationship,
    avatar: index % 24,
    gender: ["female", "male", "neutral"][index % 3],
    agent_id: null,
    ...modelDefaults(software),
    session_id: null,
    fallback_models: [],
    skills: [],
  };
}
function teamKey(entry) {
  return entry.project + "\u0000" + entry.group.id;
}
function uniqueGroupName(name, groups = state.config.groups) {
  const base = (name || "导入小组").trim();
  let candidate = base;
  let index = 2;
  while (groups.some((group) => group.name === candidate))
    candidate = base + "（导入 " + index++ + "）";
  return candidate;
}
function cloneGroupForOffice(entry, groups = state.config.groups) {
  const clone = structuredClone(entry.group);
  clone.id = id("team");
  clone.name = uniqueGroupName(clone.name, groups);
  const members = Array.isArray(clone.members) ? clone.members : [];
  clone.members = members.map((member) => {
    const copy = structuredClone(member);
    copy.id = id("person");
    copy.session_id = null;
    copy.fallback_models = [];
    copy.agent_id = null;
    copy.parameters = { ...(copy.parameters || {}) };
    const software = softwareEntry(clone.software);
    const provider = availableProviders(software).find(
      (item) => item.id === copy.provider,
    );
    const model = provider?.models?.find((item) => item.id === copy.model);
    if (provider && model) {
      copy.agent_id = provider.agent_id || null;
      if (clone.software !== "codex") copy.parameters.provider = provider.id;
      const values = effortValues(model);
      if (
        copy.parameters.reasoning_effort &&
        !values.includes(copy.parameters.reasoning_effort)
      )
        copy.parameters.reasoning_effort = defaultEffort(model);
    }
    return copy;
  });
  if (
    clone.members.length &&
    !clone.members.some((member) => member.relationship === "leader")
  )
    clone.members[0].relationship = "leader";
  return clone;
}
function checkGroupCapacity(count, extraPeople = 0) {
  if (state.config.groups.length + count > 8)
    throw Error("办公室最多保存 8 个小组，请先删除不需要的小组。");
  if (people().length + extraPeople > 48)
    throw Error("办公室最多容纳 48 位成员，请先删除部分小组成员。");
}
function existingGroupCandidates() {
  return state.teams.groups.filter(
    (entry) => !entry.in_office && entry.group?.id,
  );
}
function updateExistingSelection() {
  const count = $("existing-groups-list").querySelectorAll(
    'input[name="existing-group"]:checked',
  ).length;
  $("existing-groups-status").textContent = count
    ? "已选择 " + count + " 个小组。"
    : "请选择至少一个小组。";
  const submit = $("existing-groups-form").querySelector(
    'button[type="submit"]',
  );
  if (submit) submit.disabled = count === 0;
}
function renderExistingGroups() {
  const target = $("existing-groups-list");
  target.replaceChildren();
  if (state.teams.loading) {
    target.append(node("p", "muted", "正在读取已有小组…"));
    updateExistingSelection();
    return;
  }
  if (state.teams.error) {
    target.append(node("p", "error", state.teams.error));
    updateExistingSelection();
    return;
  }
  const candidates = existingGroupCandidates();
  if (!candidates.length) {
    target.append(
      node(
        "p",
        "muted",
        "暂无其他项目的小组。请先在对应项目中新建小组，或使用“新建小组”。",
      ),
    );
    updateExistingSelection();
    return;
  }
  for (const entry of candidates) {
    const label = node("label", "existing-group-row");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = "existing-group";
    input.value = teamKey(entry);
    const body = node("span", "existing-group-body");
    const heading = node("span", "existing-group-title");
    heading.append(
      node("strong", "", entry.group.name),
      node("span", "team-software", entry.group.software),
    );
    const members = Array.isArray(entry.group.members)
      ? entry.group.members.map((member) => member.name).filter(Boolean)
      : [];
    body.append(
      heading,
      node(
        "small",
        "",
        (entry.project_name || entry.project) +
          " · " +
          entry.member_count +
          " 位成员",
      ),
      node(
        "span",
        "existing-group-members",
        members.length ? members.join(" · ") : "暂无成员",
      ),
    );
    label.append(input, body);
    target.append(label);
  }
  target.onchange = updateExistingSelection;
  updateExistingSelection();
}
async function loadTeams(force = false) {
  if (!state.project || state.teams.loading || (state.teams.loaded && !force))
    return;
  state.teams.loading = true;
  state.teams.error = "";
  renderTeams();
  try {
    const extra = { query: state.teams.query };
    if (state.teams.project) extra.project_filter = state.teams.project;
    const data = await api("team_groups_list", extra);
    state.teams.groups = Array.isArray(data.groups) ? data.groups : [];
    state.teams.projects = Array.isArray(data.projects) ? data.projects : [];
    state.teams.loaded = true;
  } catch (error) {
    state.teams.error = error.message;
  } finally {
    state.teams.loading = false;
    renderTeams();
  }
}
function renderTeams() {
  const filter = $("team-project-filter"),
    previous = state.teams.project;
  filter.replaceChildren(
    new Option("全部项目", ""),
    ...state.teams.projects.map(
      (project) => new Option(project.project_name, project.project),
    ),
  );
  if (
    previous &&
    !state.teams.projects.some((project) => project.project === previous)
  )
    state.teams.project = "";
  filter.value = state.teams.project;
  $("team-query").value = state.teams.query;

  const summary = $("team-summary");
  const groups = state.teams.groups;
  summary.replaceChildren(
    ...[
      ["小组", groups.length],
      ["项目", new Set(groups.map((entry) => entry.project)).size],
      ["当前办公室", groups.filter((entry) => entry.in_office).length],
    ].map(([label, value]) => {
      const card = node("div", "team-stat");
      card.append(node("strong", "", String(value)), node("span", "", label));
      return card;
    }),
  );
  const status = $("team-status");
  if (state.teams.loading) status.textContent = "正在读取各项目的团队配置…";
  else if (state.teams.error) status.textContent = state.teams.error;
  else if (state.teams.loaded)
    status.textContent =
      "共 " +
      groups.length +
      " 个小组。删除小组只移除办公室配置，不会取消已派发任务。";
  else status.textContent = "";
  status.className = state.teams.error ? "error" : "muted";

  const target = $("team-groups");
  target.replaceChildren();
  if (state.teams.loading && !groups.length) {
    target.append(node("p", "muted", "正在读取团队…"));
    return;
  }
  if (state.teams.error && !groups.length) {
    target.append(node("p", "error", state.teams.error));
    return;
  }
  if (!groups.length) {
    const empty = node("div", "empty-office");
    empty.append(
      node("h3", "", "还没有保存的小组。"),
      node("p", "", "新建小组后会出现在这里；也可以从其他项目导入已有小组。"),
    );
    target.append(empty);
    return;
  }
  for (const entry of groups) {
    const group = entry.group || {},
      members = Array.isArray(group.members) ? group.members : [],
      card = node("article", "team-card"),
      heading = node("div", "team-card-head"),
      title = node("div", "team-card-title"),
      actions = node("div", "team-actions");
    title.append(
      node("h3", "", group.name || "未命名小组"),
      node(
        "small",
        "",
        (entry.project_name || entry.project) + " · " + (group.software || ""),
      ),
    );
    heading.append(
      title,
      node(
        "span",
        "team-badge" + (entry.in_office ? " current" : ""),
        entry.in_office ? "当前办公室" : "其他项目",
      ),
    );
    const meta = node("div", "team-meta");
    meta.append(
      node("span", "", entry.member_count + " 位成员"),
      node("span", "", "配置修订 " + entry.revision),
    );
    const path = node("p", "team-path", entry.project_path || entry.project);
    path.title = entry.project_path || entry.project;
    const names = node(
      "p",
      "team-members",
      members.length
        ? members
            .map((member) => member.name)
            .filter(Boolean)
            .join(" · ")
        : "暂无成员",
    );
    if (!entry.in_office) {
      const importButton = node("button", "primary", "导入到办公室");
      importButton.onclick = () => importTeamGroup(entry);
      actions.append(importButton);
    }
    const rename = node("button", "secondary", "重命名");
    rename.onclick = () => renameTeamGroup(entry);
    const remove = node("button", "danger", "删除");
    remove.onclick = () => deleteProjectGroup(entry.project, group);
    actions.append(rename, remove);
    const load = node("div", "team-workload");
    for (const member of members) {
      const workload = (entry.workload || []).find(
        (w) => w.member_id === member.id,
      );
      const line = node("div", "workload-member");
      line.append(node("strong", "", member.name));
      if (workload) {
        const descriptions = [
          ["running", "执行"],
          ["queued", "排队"],
          ["blocked", "阻塞"],
          ["awaiting_review", "待验收"],
          ["waiting_user", "待确认"],
          ["paused", "暂停"],
          ["failed", "失败"],
          ["lease_expired", "失联"],
        ]
          .filter(([k]) => workload[k])
          .map(([k, label]) => label + " " + workload[k]);
        line.append(node("span", "", descriptions.join(" · ") || "待命"));
      } else line.append(node("span", "muted", "负载信息待同步"));
      load.append(line);
    }
    card.append(heading, meta, path, names, load, actions);
    target.append(card);
  }
}
function importTeamGroup(entry) {
  action(async () => {
    checkGroupCapacity(1, entry.member_count);
    const clone = cloneGroupForOffice(entry);
    await save([...state.config.groups, clone]);
    notice(
      "已导入小组“" +
        clone.name +
        "”。执行环境会按当前项目的软件和模型重新连接。",
    );
  });
}
function deleteProjectGroup(project, group) {
  if (!confirm("删除小组“" + group.name + "”？已派发任务不会被取消。")) return;
  action(async () => {
    const config = await api("office_get", { project });
    const groups = (config.groups || []).filter((item) => item.id !== group.id);
    if (groups.length === (config.groups || []).length)
      throw Error("小组已不存在，请刷新后重试。");
    const next = await api(
      "office_put",
      mutation({
        project,
        expected_revision: config.revision,
        groups,
      }),
      { project },
    );
    if (project === state.project) {
      state.config = next;
      render();
    }
    state.teams.loaded = false;
    if (!$("teams-view").hidden) await loadTeams(true);
    notice(
      "已删除小组“" +
        group.name +
        "”。已派发任务保持原状，可在任务指挥台继续处理。",
    );
  });
}
function renameTeamGroup(entry) {
  renameGroupId = entry.group.id;
  renameGroupProject = entry.project;
  $("rename-group-form").elements.name.value = entry.group.name;
  $("rename-group-dialog").showModal();
}
$("existing-groups-button").onclick = () => {
  if (!state.project) {
    $("connect-dialog").showModal();
    return;
  }
  action(async () => {
    if (!state.teams.loaded) await loadTeams();
    renderExistingGroups();
    $("existing-groups-dialog").showModal();
  });
};
$("existing-groups-form").onsubmit = (event) => {
  event.preventDefault();
  const form = event.currentTarget,
    selected = new Set(
      [...form.querySelectorAll('input[name="existing-group"]:checked')].map(
        (input) => input.value,
      ),
    ),
    candidates = existingGroupCandidates().filter((entry) =>
      selected.has(teamKey(entry)),
    );
  if (!candidates.length) {
    notice("请选择至少一个小组。", true);
    return;
  }
  action(async () => {
    checkGroupCapacity(
      candidates.length,
      candidates.reduce((count, entry) => count + entry.member_count, 0),
    );
    const clones = [];
    for (const entry of candidates)
      clones.push(
        cloneGroupForOffice(entry, [...state.config.groups, ...clones]),
      );
    await save([...state.config.groups, ...clones]);
    form.reset();
    $("existing-groups-dialog").close();
    notice(
      "已导入 " +
        clones.length +
        " 个小组。执行环境会按当前项目的软件和模型重新连接。",
    );
  });
};
$("team-filter-form").onsubmit = (event) => {
  event.preventDefault();
  state.teams.project = $("team-project-filter").value;
  state.teams.query = $("team-query").value.trim();
  loadTeams(true);
};
$("teams-refresh").onclick = () => loadTeams(true);
$("team-new-group").onclick = () => {
  if (!state.project) {
    $("connect-dialog").showModal();
    return;
  }
  $("group-dialog").showModal();
};
$("new-group").onclick = () => {
  if (!state.project) {
    $("connect-dialog").showModal();
    return;
  }
  $("group-dialog").showModal();
};
$("group-form").onsubmit = (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  action(async () => {
    const g = {
      id: id("team"),
      name: f.elements.name.value.trim(),
      software: f.elements.software.value,
      members: [
        makeMember(
          f.elements.software.value,
          "新组长",
          "lead",
          "leader",
          people().length,
        ),
      ],
    };
    await save([...state.config.groups, g]);
    $("group-dialog").close();
    f.reset();
  });
};
function editMember(groupId, memberId) {
  const g = state.config.groups.find((g) => g.id === groupId),
    m =
      g.members.find((m) => m.id === memberId) ||
      makeMember(
        g.software,
        "新成员",
        "backend",
        "independent_session",
        people().length,
      );
  state.edit = { groupId, memberId, value: m };
  state.avatar = m.avatar;
  const f = $("member-form");
  f.elements.fallback_models.value = JSON.stringify(
    m.fallback_models || [],
    null,
    2,
  );
  for (const k of ["name", "position", "gender", "relationship"])
    if (k !== "position") f.elements[k].value = m[k];
  $("position-select").replaceChildren(
    ...Object.entries(state.config.positions).map(
      ([k, v]) => new Option(v.label, k),
    ),
    new Option("＋ 自定义岗位", "custom"),
  );
  f.elements.position.value = m.position;
  f.elements.custom_position.value = m.custom_position || "";
  f.elements.prompt.value =
    m.prompt ?? state.config.positions[m.position]?.instructions ?? "";
  populateProviders(
    g.software,
    m.provider,
    m.model,
    m.parameters?.reasoning_effort || "",
  );
  $("remove-member").disabled = !memberId;
  state.memberSkills = [...(m.skills || [])];
  state.memberMcp = m.mcp_servers == null ? null : [...m.mcp_servers];
  renderMemberMcp(g.software);
  renderMemberSkills(g.software);
  renderAvatars();
  positionHelp();
  relationshipHelp();
  $("member-dialog").showModal();
  // The picker needs the installed catalogue; fetch it once if the tab was never
  // opened, then redraw only the list so the dialog stays usable meanwhile.
  if (!state.skills.skills.length && !state.skillLoading)
    loadSkills().then(() => {renderMemberSkills(g.software); renderMemberMcp(g.software);});
}
function softwareSkills(software) {
  return (
    (state.skills.skills || []).find((entry) => entry.software === software)
      ?.skills || []
  );
}
function renderMemberSkills(software) {
  const target = $("member-skills-list");
  const help = $("member-skills-help");
  if (!target) return;
  target.replaceChildren();
  const installed = softwareSkills(software);
  if (state.skillLoading) {
    target.append(node("p", "muted", "正在扫描本机技能…"));
    return;
  }
  if (!installed.length) {
    help.textContent =
      "该软件下未发现 SKILL.md。把技能放进它的技能目录后，可在「技能中心」重新扫描。";
    target.append(node("p", "muted", "暂无可选技能。"));
    return;
  }
  help.textContent =
    software === "codex" ? "本次注入所选技能，并显式禁用其他已检测到的技能。" : "控制本次注入的技能；软件全局技能是否加载仍由该软件决定。";
  // A stored selection that is no longer installed stays visible so saving cannot
  // silently drop it without the user noticing.
  const missing = state.memberSkills.filter(
    (name) => !installed.some((s) => s.name === name),
  );
  for (const name of missing) {
    const row = node("label", "skill-option missing");
    const box = node("input");
    box.type = "checkbox";
    box.checked = true;
    box.disabled = true;
    box.dataset.skill = name;
    row.append(box, node("span", "skill-option-name", name));
    row.append(node("span", "badge warn", "本机未安装"));
    target.append(row);
  }
  for (const skill of installed) {
    const row = node("label", "skill-option");
    const box = node("input");
    box.type = "checkbox";
    box.dataset.skill = skill.name;
    box.checked = state.memberSkills.includes(skill.name);
    box.onchange = () => {
      const name = skill.name;
      state.memberSkills = box.checked
        ? [...state.memberSkills.filter((s) => s !== name), name]
        : state.memberSkills.filter((s) => s !== name);
      updateMemberSkillCount();
    };
    const label = node("span", "skill-option-body");
    label.append(
      node("span", "skill-option-name", skill.name),
      node("span", "skill-option-desc", skill.description),
    );
    row.append(box, label);
    if (skill.disabled_by_config)
      row.append(node("span", "badge warn", "本机配置已禁用"));
    if (skill.origin === "project")
      row.append(node("span", "badge alt", "项目内"));
    target.append(row);
  }
  updateMemberSkillCount();
}
function updateMemberSkillCount() {
  const box = $("member-skills");
  if (!box) return;
  const legend = box.querySelector("legend");
  if (legend)
    legend.textContent = `技能（可多选${state.memberSkills.length ? " · 已选 " + state.memberSkills.length : ""}）`;
}
function renderAvatars() {
  const target = $("avatar-picker");
  target.replaceChildren();
  for (let i = 0; i < 24; i++) {
    const b = node("button", state.avatar === i ? "selected" : "");
    b.type = "button";
    b.setAttribute("aria-label", "样貌 " + (i + 1));
    b.setAttribute("aria-pressed", String(state.avatar === i));
    b.append(
      avatar({ avatar: i, gender: $("member-form").elements.gender.value }),
    );
    b.onclick = () => {
      state.avatar = i;
      renderAvatars();
    };
    target.append(b);
  }
}
function positionHelp() {
  const spec = state.config.positions[$("position-select").value];
  $("position-description").replaceChildren();
  if (spec)
    $("position-description").append(
      node("strong", "", spec.label),
      node("p", "", "交付：" + spec.deliverables.join("；")),
    );
  const custom = $("position-select").value === "custom";
  $("custom-position-field").hidden = !custom;
  $("member-form").elements.custom_position.required = custom;
}
$("position-select").onchange = positionHelp;
$("reset-member-prompt").onclick = () => {
  $("member-form").elements.prompt.value =
    state.config.positions[$("position-select").value]?.instructions || "";
};
function relationshipHelp() {
  $("relationship-help").textContent =
    $("member-form").elements.relationship.value === "native_subagent"
      ? "可保存为原生子 Agent 配置；当前运行接口尚不支持直接创建和控制它。需要立即执行任务时请选择协作成员。"
      : "协作成员使用可独立配置模型的会话。每个小组必须保留一位组长。";
}
$("member-form").elements.relationship.onchange = relationshipHelp;
$("member-form").elements.gender.onchange = renderAvatars;
function selectedProvider() {
  const g = state.config.groups.find((g) => g.id === state.edit?.groupId);
  return availableProviders(softwareEntry(g?.software)).find(
    (p) => p.id === $("provider-select").value,
  );
}
function populateProviders(software, providerId, modelId, effort) {
  const providers = availableProviders(softwareEntry(software));
  const select = $("provider-select");
  select.replaceChildren(
    ...providers
      .filter((p) => p.models?.length)
      .map((p) => {
        const free = (p.models || []).filter((m) => m.free).length;
        const label = p.label || p.id;
        return new Option(
          free ? label + " · " + free + " 个免费" : label,
          p.id,
        );
      }),
  );
  if ([...select.options].some((o) => o.value === providerId))
    select.value = providerId;
  select.disabled = !select.options.length;
  if (!select.options.length) select.append(new Option("尚未检测到供应商", ""));
  populateModels(modelId, effort);
}
function populateModels(modelId, effort) {
  const provider = selectedProvider();
  const select = $("model-select");
  select.replaceChildren(
    ...(provider?.models || []).map((m) => {
      const label = m.label || m.id;
      return new Option(m.free ? label + " · 免费" : label, m.id);
    }),
  );
  if ([...select.options].some((o) => o.value === modelId))
    select.value = modelId;
  select.disabled = !select.options.length;
  if (!select.options.length) select.append(new Option("尚未检测到模型", ""));
  populateEfforts(effort);
  updateModelPrice();
}
function populateEfforts(effort) {
  const provider = selectedProvider();
  const model = provider?.models?.find((m) => m.id === $("model-select").value);
  const select = $("effort-select");
  const effortLabels = {
    none: "无",
    minimal: "最低",
    low: "低",
    medium: "中等",
    high: "高",
    xhigh: "极高",
    max: "最高",
    ultra: "超高",
  };
  const values = effortValues(model);
  select.replaceChildren(
    ...(values.length
      ? values.map((value) => new Option(effortLabels[value] || value, value))
      : [new Option("软件默认", "")]),
  );
  select.value = values.includes(effort) ? effort : defaultEffort(model);
  select.disabled = values.length === 0;
  $("binding-help").textContent = !model
    ? "当前软件未提供可验证的模型目录，请重新检测。"
    : provider.agent_id
      ? "已自动连接本地执行环境。派发任务后系统将启动对应软件。"
      : "已发现模型，执行环境尚不可用。请查看连接项目中的软件状态。";
  $("member-form").querySelector('button[type="submit"]').disabled = !model;
}
$("provider-select").onchange = () => populateModels();
$("model-select").onchange = () => {
  populateEfforts();
  updateModelPrice();
};
$("member-form").onsubmit = (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  action(async () => {
    const groups = structuredClone(state.config.groups),
      g = groups.find((g) => g.id === state.edit.groupId),
      m = {
        ...state.edit.value,
        name: f.elements.name.value.trim(),
        position: f.elements.position.value,
        custom_position:
          f.elements.position.value === "custom"
            ? f.elements.custom_position.value.trim()
            : null,
        prompt: f.elements.prompt.value.trim() ? f.elements.prompt.value : null,
        gender: f.elements.gender.value,
        relationship: f.elements.relationship.value,
        provider: f.elements.provider.value.trim(),
        model: f.elements.model.value.trim(),
        avatar: state.avatar,
        fallback_models: JSON.parse(f.elements.fallback_models.value || "[]"),
        agent_id:
          f.elements.relationship.value === "native_subagent"
            ? null
            : selectedProvider()?.agent_id || null,
        parameters: {},
        skills: [...state.memberSkills],
        ...(state.skills.capabilities_version && state.memberMcp !== null ? {mcp_servers:[...state.memberMcp]} : {}),
      };
    // A skill from another runtime would be rejected by the server; say so first.
    const foreign = m.skills.filter(
      (name) => !softwareSkills(g.software).some((s) => s.name === name),
    );
    if (foreign.length)
      throw Error(
        `这些技能不是「${g.software}」本机安装的技能：${foreign.join("、")}。请在技能中心确认后重选。`,
      );
    if (g.software !== "codex") m.parameters.provider = m.provider;
    if (f.elements.effort.value)
      m.parameters.reasoning_effort = f.elements.effort.value;
    const index = g.members.findIndex((x) => x.id === m.id);
    if (index >= 0) g.members[index] = m;
    else g.members.push(m);
    if (
      g.members.filter((member) => member.relationship === "leader").length !==
      1
    )
      throw Error(
        "每个小组需要恰好一位组长。请先保留或设置组长，再调整其他成员的组织关系。",
      );
    await save(groups);
    $("member-dialog").close();
  });
};
$("remove-member").onclick = () =>
  action(async () => {
    const groups = structuredClone(state.config.groups),
      g = groups.find((g) => g.id === state.edit.groupId);
    g.members = g.members.filter((m) => m.id !== state.edit.memberId);
    await save(groups.filter((g) => g.members.length));
    $("member-dialog").close();
  });
$("briefing-form").elements.title.oninput = (e) =>
  ($("briefing-preview").textContent =
    e.target.value || "这次，我们一起做什么？");
$("briefing-form").onsubmit = (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  action(async () => {
    if (!state.order.length)
      throw Error("请先选择至少一位已连接本地软件的成员。");
    const data = await api(
      "office_dispatch",
      mutation({
        expected_revision: state.config.revision,
        briefing_id: id("brief"),
        title: f.elements.title.value.trim(),
        description: f.elements.description.value.trim(),
        member_ids: state.order,
        leader_id: f.elements.leader_id.value,
        mode: f.elements.mode.value,
        review_required: f.elements.review_required.checked,
        ...(Object.keys(meetingCapabilityOverrides()).length ? {member_overrides:meetingCapabilityOverrides()} : {}),
      }),
    );
    await refresh();
    show("tasks");
    notice("会议已创建，主导者开始拆解目标。可在任务指挥台查看真实进展。");
  });
};
$("observation-form").onsubmit = (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  action(async () => {
    const now = Date.now() / 1000,
      remaining = f.elements.remaining.value;
    await api(
      "quota_observe",
      mutation({
        route_id: state.route,
        status: f.elements.status.value,
        remaining: remaining === "" ? null : Number(remaining),
        unit: remaining === "" ? null : f.elements.unit.value,
        observed_at: now,
        expires_at: now + Number(f.elements.minutes.value) * 60,
        source: "operator",
        detail: f.elements.detail.value,
      }),
    );
    $("observation-dialog").close();
    await refresh();
    notice("人工额度观察已保存；来源会明确标注。");
  });
};
$("route-form").onsubmit = (e) => {
  e.preventDefault();
  action(async () => {
    const result = await api("office_route_preview", {
      current_route_id: $("route-member").value,
    });
    const m = routes().find(({ m }) => m.id === result.route_id)?.m;
    $("route-result").textContent =
      (m ? "候选：" + m.name + " / " + m.model : "暂无可验证的可用路线") +
      "。" +
      result.reason +
      (result.requires_probe ? "；需要重新检查额度。" : "");
  });
};
$("handoff-form").onsubmit = (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  action(async () => {
    const task = await api("task_get", {
      task_id: f.elements.task_id.value.trim(),
    });
    await api(
      "office_handoff",
      mutation({
        task_id: task.task_id,
        expected_revision: task.revision,
        current_route_id: $("handoff-member").value,
        checkpoint_key: f.elements.checkpoint_key.value.trim(),
        stopped_confirmed: true,
      }),
    );
    await refresh();
    notice("任务已交接，新的执行器将读取检查点后继续。");
  });
};
setInterval(() => {
  if (
    state.authenticated &&
    (state.discovering || state.refreshPending) &&
    !state.busy
  )
    action(async () => {
      applyLocalStatus(await localRequest("status"));
    });
}, 2000);
setInterval(() => {
  if (
    !$("agents-view").hidden &&
    (state.discovering || state.agentManager.discovering) &&
    !state.agentManager.loading
  )
    loadAgents(true);
}, 2000);
setInterval(() => {
  if (
    state.project &&
    (state.authenticated || state.token) &&
    !state.busy &&
    !state.refreshing &&
    !document.hidden &&
    !document.querySelector("dialog[open]")
  )
    action(refresh);
}, 5000);
setInterval(() => {
  if (
    !$("tasks-view").hidden &&
    state.project &&
    (state.authenticated || state.token) &&
    state.taskChat.group &&
    !document.hidden &&
    !state.taskChat.loading &&
    !document.querySelector("dialog[open]")
  )
    loadGroupChat(false);
}, 2000);
render();
bootstrap();

function assignedMember(task) {
  const assigned = state.assignments.find((a) => a.task_id === task.task_id);
  if (assigned) return people().find(({ m }) => m.id === assigned.member_id);
  const roles = state.config.member_roles || {};
  return people().find(({ m }) => {
    const role = roles[m.id];
    return (
      task.role_id ===
      (typeof role === "string" && role ? role : "office/" + m.id)
    );
  });
}
const meetingStageLabels = {
  planning: "主导者拆解",
  reviewing: "组长可行性评估",
  ready_for_execution: "方案一致 · 等待执行确认",
  delegating: "组长分配成员",
  executing: "成员执行",
  done: "已完成",
  discussion_done: "方案已保存 · 讨论结束",
  cancelled: "已取消",
  needs_attention: "需要调整方案",
};
function meetingBlockers(meeting) {
  // Finished meetings retain history, but those records are not active blockers.
  if (["done", "discussion_done", "cancelled"].includes(meeting.stage)) return [];
  return (meeting.waiting || [])
    .map((id) => state.tasks.find((task) => task.task_id === id))
    .filter((task) => task && ["failed", "cancelled"].includes(task.status));
}
function meetingPresentation(meeting) {
  if (meeting.stage === "cancelled") return { label: "已取消", tone: "neutral" };
  if (["done", "discussion_done"].includes(meeting.stage))
    return { label: meeting.stage === "done" ? "已完成" : "讨论结束", tone: "success" };
  if (meeting.paused) return { label: "已暂停", tone: "neutral" };
  if (meeting.error || meetingBlockers(meeting).length || meeting.stage === "needs_attention")
    return { label: "需要处理", tone: "warning" };
  if (meeting.stage === "ready_for_execution") return { label: "待你确认", tone: "pending" };
  return { label: meetingStageLabels[meeting.stage] || meeting.stage, tone: "active" };
}
function renderMeetingStages(meetings) {
  const flows = $("meeting-flows");
  const selected = meetings.find((m) => state.stageMeeting === `${state.project}/${m.id}`) || meetings[0];
  state.stageMeeting = selected ? `${state.project}/${selected.id}` : null;
  const signature = JSON.stringify([
    state.project, state.stageMeeting, meetings,
    meetings.flatMap((m) => meetingBlockers(m)),
    state.meetings.map((m) => [m.id, m.root_id, m.iteration]),
    people().map(({ m }) => [m.id, m.name]),
  ]);
  if (signature === state.stageSignature) return;
  state.stageSignature = signature;
  flows.replaceChildren();
  $("meeting-flow-count").textContent = `${meetings.length} 场会议`;
  if (!selected) {
    const empty = node("div", "meeting-empty");
    empty.append(node("strong", "", "还没有会议记录"), node("p", "", "在会议室发起任务后，拆解、讨论与执行的阶段会显示在这里。"));
    flows.append(empty);
    return;
  }
  const list = node("nav", "meeting-agenda");
  list.setAttribute("aria-label", "选择会议记录");
  for (const meeting of meetings) {
    const presentation = meetingPresentation(meeting);
    const button = node("button", "meeting-agenda-item" + (meeting.id === selected.id ? " selected" : ""));
    button.type = "button";
    button.dataset.meetingId = meeting.id;
    button.setAttribute("aria-pressed", String(meeting.id === selected.id));
    const meta = node("span", "meeting-agenda-meta");
    meta.append(node("span", "", `第 ${meeting.iteration || 1} 轮 · ${meeting.mode === "discussion" ? "讨论" : "执行"}`), node("span", `meeting-status ${presentation.tone}`, presentation.label));
    button.append(node("strong", "", meeting.title), meta);
    button.onclick = () => {
      state.stageMeeting = `${state.project}/${meeting.id}`;
      renderMeetingStages(meetings);
      [...flows.querySelectorAll(".meeting-agenda-item")].find((item) => item.dataset.meetingId === meeting.id)?.focus({ preventScroll: true });
    };
    list.append(button);
  }
  const meeting = selected, presentation = meetingPresentation(meeting);
  const card = node("article", "meeting-flow");
  card.setAttribute("aria-label", "选中会议的阶段详情");
  const header = node("header", "meeting-detail-head");
  const title = node("div");
  title.append(node("span", "meeting-eyebrow", "会议进展"), node("h3", "", meeting.title));
  header.append(title, node("span", `meeting-status ${presentation.tone}`, presentation.label));
  const lead = people().find(({ m }) => m.id === meeting.leader);
  const meta = node("p", "meeting-detail-meta", `${meeting.mode === "discussion" ? "讨论模式" : "执行模式"} · 第 ${meeting.iteration || 1} 轮 · 第 ${meeting.round || 1} 次协商 · 主导者 ${lead?.m.name || "原成员已移除"}`);
  card.append(header, meta);
  const discussion = meeting.mode === "discussion";
  const stages = discussion
    ? ["planning", "reviewing", ...(meeting.stage === "discussion_done" ? ["discussion_done"] : ["ready_for_execution"]), ...(["delegating", "executing", "done"].includes(meeting.stage) ? ["delegating", "executing", "done"] : [])]
    : ["planning", "delegating", "executing", "done"];
  const shortLabels = { planning: "目标拆解", reviewing: "可行性评估", ready_for_execution: "确认方案", delegating: "成员分工", executing: "任务执行", done: "交付完成", discussion_done: "保存方案" };
  const current = stages.indexOf(meeting.stage);
  const steps = node("ol", "meeting-timeline");
  steps.setAttribute("aria-label", "会议阶段进度");
  stages.forEach((stage, index) => {
    const complete = current >= 0 && (index < current || ["done", "discussion_done"].includes(meeting.stage));
    const active = index === current && !complete;
    const step = node("li", "meeting-timeline-step" + (complete ? " complete" : active ? " current" : ""));
    if (active) step.setAttribute("aria-current", "step");
    step.append(node("span", "meeting-step-dot", complete ? "✓" : String(index + 1)), node("strong", "", shortLabels[stage]), node("span", "meeting-step-caption", complete ? "已完成" : active ? presentation.label : meeting.stage === "cancelled" ? "已终止" : "待推进"));
    steps.append(step);
  });
  card.append(steps);
  const blockers = meetingBlockers(meeting);
  const status = node("div", `meeting-next ${presentation.tone}`);
  const copy = node("div");
  const nextCopy = {
    planning: ["正在拆解目标", "主导者明确交付内容，并分配给各个小组。"],
    reviewing: ["正在评估可行性", "各组长评估任务和成员能力，有异议时继续协商。"],
    ready_for_execution: ["等待你确认执行", "方案已达成一致。确认后，组长会继续分配成员任务。"],
    delegating: ["正在安排成员", "组长根据分工与职责，把任务派发给组内成员。"],
    executing: ["成员正在推进任务", "执行记录与对话可在下方的小组频道查看。"],
    done: ["本轮已完成", "可以查看交付记录，或基于成果发起下一轮修改。"],
    discussion_done: ["方案已保存", "本次讨论已经结束，需要调整时可新增修改轮次。"],
    cancelled: ["会议已取消", "历史阶段与任务记录仍然保留。"],
    needs_attention: ["需要调整方案", "请查看阶段说明，处理分工或可行性问题。"],
  };
  const terminal = ["done", "discussion_done", "cancelled"].includes(meeting.stage);
  const [headline, description] = !terminal && meeting.paused
    ? ["会议已暂停", "继续后恢复整体推进；单独暂停的步骤仍需单独恢复。"]
    : !terminal && (blockers.length || meeting.error)
      ? ["当前阶段需要处理", "存在失败或取消的步骤，请查看原因后再继续推进。"]
      : nextCopy[meeting.stage] || ["等待阶段更新", "请查看任务记录了解当前进展。"];
  copy.append(node("strong", "", headline), node("p", "", description));
  status.append(node("span", "meeting-next-icon", presentation.tone === "success" ? "✓" : presentation.tone === "warning" ? "!" : "·"), copy);
  card.append(status);
  if (blockers.length || meeting.error) {
    const details = node("details", "meeting-issues");
    details.append(node("summary", "", `查看阻塞原因${blockers.length ? ` · ${blockers.length} 个步骤` : ""}`));
    for (const task of blockers) {
      const blocked = node("div", "meeting-blocked");
      blocked.append(node("strong", "", "当前阶段已阻塞 · " + (assignedMember(task)?.m.name || task.title)), node("p", "", task.failure?.message || "等待步骤已取消，需要重新安排。"));
      const inspect = node("button", "text-button", "查看失败步骤");
      inspect.onclick = () => openTaskInspect(task);
      blocked.append(inspect);
      details.append(blocked);
    }
    if (meeting.error) details.append(node("p", "error", meeting.error));
    card.append(details);
  }
  if (meeting.changes) card.append(node("p", "iteration-changes", "本轮修改：" + meeting.changes));
  card.append(meetingGoal(meeting.goal));
  const actions = node("footer", "meeting-detail-actions");
  if (!terminal) {
    const pause = node("button", "secondary", meeting.paused ? "继续整个任务" : "暂停整个任务");
    pause.onclick = () => action(async () => {
      const result = await localRequest("meeting-control", { project: state.project, ...mutation({ meeting_id: meeting.id, action: meeting.paused ? "resume" : "pause" }) });
      await refresh();
      notice(result.stopping ? "任务已暂停，进程仍在退出。" : "整个任务的运行状态已更新。");
    });
    actions.append(pause);
  }
  if (meeting.stage === "ready_for_execution" || (meeting.error && ["planning", "reviewing", "delegating"].includes(meeting.stage))) {
    const button = node("button", "primary", meeting.stage === "ready_for_execution" ? "方案已确认，进入执行" : "重新生成本阶段结果");
    button.onclick = () => action(async () => {
      await api(meeting.stage === "ready_for_execution" ? "meeting_execute" : "meeting_retry", mutation({ meeting_id: meeting.id }));
      await refresh();
    });
    actions.append(button);
  }
  if (["done", "discussion_done"].includes(meeting.stage) && !state.meetings.some((m) => (m.root_id || m.id) === (meeting.root_id || meeting.id) && (m.iteration || 1) > (meeting.iteration || 1))) {
    const revise = node("button", "primary", "新增修改轮次");
    revise.onclick = () => {
      revisionParent = meeting;
      $("revision-form").reset();
      $("revision-parent").textContent = `${meeting.title} · 基于第 ${meeting.iteration || 1} 轮成果`;
      $("revision-dialog").showModal();
    };
    actions.append(revise);
  }
  if (actions.childElementCount) card.append(actions);
  flows.append(list, card);
}
function renderTaskDashboard() {
  const select = $("task-meeting-filter"),
    selected = select.value;
  const roots = new Map();
  for (const meeting of state.meetings)
    roots.set(meeting.root_id || meeting.id, meeting.title);
  select.replaceChildren(
    new Option("全部目标与独立任务", ""),
    ...[...roots].map(([id, title]) => new Option(title, id)),
  );
  if ([...select.options].some((o) => o.value === selected))
    select.value = selected;
  const iterationSelect = $("task-iteration-filter"),
    oldIteration = iterationSelect.value;
  const family = state.meetings.filter(
    (m) => !select.value || (m.root_id || m.id) === select.value,
  );
  iterationSelect.replaceChildren(
    new Option("全部轮次", ""),
    ...family.map(
      (m) => new Option(`${m.title} · 第 ${m.iteration || 1} 轮`, m.id),
    ),
  );
  if ([...iterationSelect.options].some((o) => o.value === oldIteration))
    iterationSelect.value = oldIteration;
  const visibleMeetings = family.filter(
    (m) => !iterationSelect.value || m.id === iterationSelect.value,
  );
  const visibleIds = new Set(visibleMeetings.map((m) => m.id));
  const matches = (task) =>
    (!select.value && !iterationSelect.value) ||
    state.assignments.some(
      (a) => a.task_id === task.task_id && visibleIds.has(a.meeting_id),
    );
  const tasks = state.tasks.filter(matches);
  renderDeliveryLanes(tasks);
  renderMeetingStages(visibleMeetings);
  ensureTaskChatGroup();
  const progress = $("group-progress");
  progress.replaceChildren();
  if (!state.config.groups.length)
    progress.append(
      node("p", "muted", "还没有小组。先到团队管理创建小组并配置成员。"),
    );
  for (const group of state.config.groups) {
    const groupTasks = tasks.filter(
      (t) => assignedMember(t)?.g.id === group.id,
    );
    const completed = groupTasks.filter(
      (t) => (t.display_status || t.status) === "done",
    ).length;
    const running = groupTasks.filter((t) => t.status === "running").length;
    const failed = groupTasks.filter((t) => t.status === "failed").length;
    const active = state.taskChat.group === group.id;
    const channel = node(
      "button",
      "group-channel" + (active ? " active" : "") + (running ? " live" : ""),
    );
    channel.type = "button";
    channel.dataset.groupId = group.id;
    channel.setAttribute("aria-pressed", active ? "true" : "false");
    const top = node("span", "group-channel-top");
    top.append(
      node("strong", "", group.name),
      node(
        "span",
        "group-channel-badge",
        active ? "当前" : running ? "LIVE" : group.software,
      ),
    );
    channel.append(
      top,
      node(
        "span",
        "group-channel-stats",
        groupTasks.length
          ? `${running} 执行中 · ${completed}/${groupTasks.length} 完成${failed ? ` · ${failed} 失败` : ""}`
          : "暂无任务 · 进入会议室派发",
      ),
      node(
        "span",
        "group-channel-hint",
        running ? "成员正在执行，对话实时同步" : "查看小组协作记录",
      ),
    );
    channel.onclick = () => selectTaskChatGroup(group.id);
    progress.append(channel);
  }
  const board = $("execution-board");
  board.replaceChildren();
  const historyScope = JSON.stringify([state.project, select.value, iterationSelect.value]);
  if (state.taskHistory.scope !== historyScope)
    state.taskHistory = { scope: historyScope, expanded: false };
  const recentTasks = [...tasks].sort((a, b) =>
    (b.created_at || 0) - (a.created_at || 0) || b.task_id.localeCompare(a.task_id),
  );
  const shownTasks = state.taskHistory.expanded ? recentTasks : recentTasks.slice(0, 2);
  $("execution-summary").textContent = tasks.length
    ? `共 ${tasks.length} 条 · ${state.taskHistory.expanded ? "已展开历史" : "默认显示最新两条"}`
    : "暂停、继续、交接";
  const more = $("execution-more");
  more.hidden = recentTasks.length <= 2;
  more.textContent = state.taskHistory.expanded ? "收起历史" : `更多 · 另外 ${recentTasks.length - 2} 条`;
  more.setAttribute("aria-expanded", String(state.taskHistory.expanded));
  for (const task of shownTasks) {
    const member = assignedMember(task);
    const assignment = state.assignments.find(
      (a) => a.task_id === task.task_id,
    );
    const card = node("article", "card execution-task");
    card.dataset.taskId = task.task_id;
    card.append(
      node(
        "span",
        "badge " + task.status,
        labels[task.display_status] || labels[task.status] || task.status,
      ),
      node("h3", "", task.title),
      node(
        "p",
        "",
        `${member?.g.name || "未绑定小组"} → ${member?.m.name || task.role_id || "未分配"}${assignment ? " · " + assignment.stage : ""}`,
      ),
    );
    const detail = node("details");
    detail.open = state.taskDetails.has(task.task_id);
    detail.ontoggle = () => {
      if (detail.isConnected) {
        if (detail.open) state.taskDetails.add(task.task_id);
        else state.taskDetails.delete(task.task_id);
      }
    };
    detail.append(
      node("summary", "", "任务说明、依赖与执行记录"),
      node("p", "task-description", task.description || "无说明"),
    );
    for (const dependency of task.depends_on || []) {
      const prior = state.tasks.find((t) => t.task_id === dependency);
      detail.append(
        node(
          "p",
          "muted",
          `前置：${prior?.title || dependency} · ${labels[prior?.status] || "未知"}`,
        ),
      );
    }
    for (const blocker of task.blockers || [])
      detail.append(
        node(
          "p",
          "error",
          `等待：${blocker.key} · ${labels[blocker.reason] || blocker.reason}`,
        ),
      );
    const events = state.events
      .filter((e) => e.subject === task.task_id)
      .slice(-8);
    const eventLabels = {
      "task.created": "已派发",
      "task.claimed": "已领取",
      "task.completed": "已完成",
      "task.pause": "已暂停",
      "task.resume": "已恢复排队",
      "task.failed": "执行失败",
      "task.guidance": "收到引导",
      "office.transferred": "已交接",
      "task.cancel": "已取消",
      "task.retry": "重新排队",
    };
    for (const event of events)
      detail.append(
        node(
          "p",
          "muted",
          `${new Date(event.created_at * 1000).toLocaleString()} · ${eventLabels[event.kind] || event.kind}`,
        ),
      );
    const taskMeeting = state.meetings.find(
      (m) => m.id === assignment?.meeting_id,
    );
    if (taskMeeting)
      card.append(
        node(
          "small",
          "muted",
          `${taskMeeting.title} / 第 ${taskMeeting.iteration || 1} 轮 / ${assignment.stage}`,
        ),
      );
    card.append(detail);
    if (task.failure) card.append(node("p", "error", task.failure.message));
    const buttons = node("div", "task-actions");
    if (task.review?.status === "pending") {
      for (const [verb, label] of [
        ["approve", "验收通过"],
        ["reject", "驳回返工"],
      ]) {
        const reviewButton = node(
          "button",
          verb === "approve" ? "primary" : "secondary",
          label,
        );
        reviewButton.onclick = () => openTaskReview(task, verb);
        buttons.append(reviewButton);
      }
    }
    const history = node(
      "button",
      "text-button",
      "执行档案 · " + (task.attempt_count ?? task.fence ?? 0) + " 次尝试",
    );
    history.onclick = () => openTaskInspect(task);
    buttons.append(history);
    const controls = {
      ready: ["pause", "cancel"],
      running: ["pause", "cancel"],
      paused: ["resume", "cancel"],
      failed: ["retry", "cancel"],
      cancelled: ["retry"],
    };
    const controlLabels = {
      pause: "暂停",
      resume: "继续",
      cancel: "取消任务",
      retry: "重试",
    };
    if (task.waiting_user) {
      const resolve = node("button", "primary", "回复待确认事项 →");
      resolve.onclick = () =>
        $("decision-center").scrollIntoView({ behavior: "smooth" });
      buttons.append(resolve);
    }
    for (const operation of task.waiting_user
      ? []
      : controls[task.status] || []) {
      const button = node("button", "text-button", controlLabels[operation]);
      button.onclick = () =>
        action(async () => {
          const result = await controlTask(task, operation);
          await refresh();
          notice(
            result.stopping
              ? "任务已暂停，旧进程仍在退出；请稍后继续。"
              : "任务状态已更新。",
          );
        });
      buttons.append(button);
    }
    if (
      !task.waiting_user &&
      ["ready", "running", "paused", "failed"].includes(task.status)
    ) {
      const transfer = node("button", "text-button", "交接成员");
      transfer.onclick = () => openTransfer(task);
      buttons.append(transfer);
    }
    if (member) {
      const chat = node("button", "text-button", "成员对话 / 引导");
      chat.onclick = () => openMemberChat(member.g.id, member.m.id);
      buttons.append(chat);
    }
    // Deletion is offered wherever the task is not mid-flight; the server refuses a
    // running task and one that other tasks still depend on.
    if (!task.waiting_user && !["running"].includes(task.status)) {
      const remove = node("button", "text-button danger", "删除任务");
      remove.onclick = () => openDeleteTask(task);
      buttons.append(remove);
    }
    if (task.outputs?.length) {
      const resultButton = node("button", "text-button", "查看实际结果");
      resultButton.onclick = () =>
        action(async () => {
          const target = node("pre", "task-description");
          const results = await Promise.all(
            task.outputs.map((o) =>
              api("item_get", { key: o.key, version: o.version }),
            ),
          );
          const text = results
            .map((r) => JSON.stringify(r.value, null, 2))
            .join("\n");
          // Keep the published result with the task revision so a later
          // render (including after a live refresh) does not hide it.
          state.taskResults.set(task.task_id, {
            revision: task.revision,
            text,
          });
          target.textContent = text;
          resultButton.disabled = true;
          card.append(target);
        });
      buttons.append(resultButton);
    }
    card.append(buttons);
    const cachedResult = state.taskResults.get(task.task_id);
    if (cachedResult?.revision === task.revision)
      card.append(node("pre", "task-description", cachedResult.text));
    board.append(card);
  }
  if (!tasks.length)
    board.append(
      node(
        "p",
        "muted",
        "当前项目暂无任务。到会议室填写目标、选择主导者和成员开始。",
      ),
    );
  renderTaskChat();
}
function ensureTaskChatGroup() {
  const valid = state.config.groups.some(
    (group) => group.id === state.taskChat.group,
  );
  if (valid) {
    updateGroupChannelSelection();
    return;
  }
  const activeGroups = new Set(
    state.tasks
      .filter((task) => ["running", "ready", "paused"].includes(task.status))
      .map((task) => assignedMember(task)?.g.id)
      .filter(Boolean),
  );
  const group =
    state.config.groups.find((item) => activeGroups.has(item.id)) ||
    state.config.groups[0];
  const previous = state.taskChat.group;
  state.taskChat.group = group?.id || "";
  if (previous !== state.taskChat.group) resetTaskChat(false);
  updateGroupChannelSelection();
}
function taskChatCanLeaveGroup(groupId) {
  const chat = state.taskChat;
  if (chat.group === groupId) return true;
  const draft = $("group-chat-form").elements.message.value.trim();
  if (!chat.pending && !chat.sending && !draft) return true;
  const reason =
    chat.pending || chat.sending
      ? "上一条引导的发送结果尚未确认；切换小组会丢失重试状态和草稿。"
      : "当前引导尚未发送；切换小组会清空草稿。";
  return window.confirm(reason + "仍要切换？");
}
function updateGroupChannelSelection() {
  for (const channel of $("group-progress").querySelectorAll(
    ".group-channel",
  )) {
    const group = state.config.groups.find(
      (item) => item.id === channel.dataset.groupId,
    );
    if (!group) continue;
    const active = state.taskChat.group === group.id;
    const running = state.tasks.some(
      (task) =>
        task.status === "running" && assignedMember(task)?.g.id === group.id,
    );
    channel.classList.toggle("active", active);
    channel.classList.toggle("live", running);
    channel.setAttribute("aria-pressed", active ? "true" : "false");
    const badge = channel.querySelector(".group-channel-badge");
    if (badge)
      badge.textContent = active ? "当前" : running ? "LIVE" : group.software;
  }
}
function selectTaskChatGroup(groupId) {
  if (!state.config.groups.some((group) => group.id === groupId)) return;
  if (state.taskChat.group === groupId) {
    renderTaskChat();
    if (!state.taskChat.loaded && !state.taskChat.loading) loadGroupChat(true);
    return;
  }
  if (!taskChatCanLeaveGroup(groupId)) return;
  state.taskChat.group = groupId;
  resetTaskChat(false);
  updateGroupChannelSelection();
  renderTaskChat();
  loadGroupChat(true);
}
function chooseTaskChatTarget(taskId) {
  const chat = state.taskChat;
  const select = $("group-chat-member");
  if (!taskId || select.disabled || chat.pending || chat.sending) return;
  // Keep an unsent draft aimed at its original task; the operator can switch
  // targets explicitly after deciding where the text belongs.
  if ($("group-chat-form").elements.message.value.trim()) return;
  if (![...select.options].some((option) => option.value === taskId)) return;
  if (select.value === taskId) return;
  select.value = taskId;
  select.dispatchEvent(new Event("change"));
}
function tasksForGroupMember(group, member) {
  return state.tasks
    .filter((task) => {
      if (!["running", "ready", "paused"].includes(task.status)) return false;
      const person = assignedMember(task);
      return person?.g.id === group.id && person.m.id === member.id;
    })
    .sort(
      (a, b) =>
        (b.status === "running") - (a.status === "running") ||
        b.updated_at - a.updated_at,
    );
}
function taskChatTargetLabel(target) {
  return (
    target.member.name +
    " · " +
    target.task.title +
    " · " +
    (labels[target.task.display_status] ||
      labels[target.task.status] ||
      target.task.status)
  );
}
function taskChatRole(message) {
  if (message.role === "user")
    return message.member_name ? "你 → " + message.member_name : "你的引导";
  if (message.role === "assistant")
    return (
      (message.member_name ? message.member_name + " · " : "") +
      (message.kind === "activity" ? "执行动态" : "交付结果")
    );
  return "执行动态";
}
function renderTaskChat() {
  const chat = state.taskChat;
  const group = state.config.groups.find((item) => item.id === chat.group);
  const memberSelect = $("group-chat-member");
  const form = $("group-chat-form");
  if (!group) {
    $("task-chat-title").textContent = "选择一个小组";
    chat.targetSignature = "";
    memberSelect.replaceChildren();
    memberSelect.disabled = true;
    form.elements.message.disabled = true;
    $("group-chat-form").querySelector('button[type="submit"]').disabled = true;
    updateTaskChatChrome(null, []);
    renderGroupChatMessages();
    return;
  }
  $("task-chat-title").textContent = group.name + " · 小组对话";
  const previous = memberSelect.value || chat.pending?.task_id || "";
  const targets = [];
  for (const member of group.members || []) {
    for (const task of tasksForGroupMember(group, member))
      targets.push({ member, task });
  }
  // Include every active task, not just one per person: a member can own
  // several parallel steps and each one needs its own guidance target.
  const targetSignature = JSON.stringify(
    targets.map(({ member, task }) => [
      member.id,
      member.name,
      task.task_id,
      task.title,
      task.status,
    ]),
  );
  const targetLabels = targets.map((target) => taskChatTargetLabel(target));
  const labelCounts = new Map();
  for (const label of targetLabels)
    labelCounts.set(label, (labelCounts.get(label) || 0) + 1);
  const options = targets.map(({ member, task }, index) => {
    // Parallel steps can share a title and status; include the task id only
    // when the visible label would otherwise be ambiguous.
    const label =
      labelCounts.get(targetLabels[index]) > 1
        ? targetLabels[index] + " · " + task.task_id
        : targetLabels[index];
    const option = new Option(label, task.task_id);
    option.dataset.taskId = task.task_id;
    option.dataset.memberId = member.id;
    return option;
  });
  if (targetSignature !== chat.targetSignature) {
    chat.targetSignature = targetSignature;
    memberSelect.replaceChildren(...options);
    if (options.length) {
      if (targets.some(({ task }) => task.task_id === previous))
        memberSelect.value = previous;
      else if (
        form.elements.message.value.trim() ||
        chat.pending ||
        chat.sending
      )
        // Do not silently retarget an unsent draft (or an unresolved send)
        // when its task ends. Force an explicit choice instead.
        memberSelect.value = "";
      else
        memberSelect.value = (
          targets.find(({ task }) => task.status === "running") || targets[0]
        ).task.task_id;
    }
  }
  if (
    !options.length &&
    !chat.sending &&
    !chat.pending &&
    !chat.loading &&
    !chat.error &&
    !chat.lastError
  ) {
    const empty =
      "该小组当前没有可发送引导的任务。成员任务结束后，这里仍会保留真实对话。";
    // role="status" is a live region; do not re-announce the same sentence on
    // every two-second poll.
    if ($("group-chat-status").textContent !== empty)
      $("group-chat-status").textContent = empty;
  }
  if (
    chat.pending &&
    !chat.sending &&
    !targets.some(
      ({ member, task }) =>
        member.id === chat.pending.member_id &&
        task.task_id === chat.pending.task_id,
    )
  ) {
    // The original task ended or left the office, so the request can no
    // longer be replayed against it. Closing it is safe because the server
    // refuses guidance for finished tasks.
    chat.pending = null;
    chat.lastError = "";
    if (!chat.loading)
      $("group-chat-status").textContent =
        "原引导的目标任务已结束；未确认的发送已关闭。";
  }
  const selected =
    targets.find(({ task }) => task.task_id === memberSelect.value) || null;
  const samePending = Boolean(
    chat.pending &&
      selected &&
      chat.pending.member_id === selected.member.id &&
      chat.pending.task_id === selected.task.task_id,
  );
  const pendingMismatch = Boolean(chat.pending && !samePending);
  memberSelect.disabled = !options.length || chat.sending;
  form.elements.message.disabled = !options.length || chat.sending;
  const submit = form.querySelector('button[type="submit"]');
  submit.disabled =
    !options.length || !selected || chat.sending || pendingMismatch;
  submit.textContent = chat.pending
    ? samePending
      ? "重试发送"
      : "先确认原发送"
    : "发送引导";
  submit.title = !selected
    ? "请选择要发送引导的任务"
    : pendingMismatch
      ? "上一条引导的发送结果尚未确认；请切回原任务重试"
      : "";
  updateTaskChatChrome(group, [...chat.messages.values()]);
  renderGroupChatMessages();
}
// A focused task narrows the conversation to that task's own run, which is how an
// operator follows one execution instead of reading a whole group's history.
function renderTaskChatFocus() {
  const meta = $("task-chat-meta");
  if (!meta) return;
  const existing = $("task-chat-focus");
  const taskId = state.taskChat.focusTask;
  const task = taskId
    ? state.tasks.find((item) => item.task_id === taskId)
    : null;
  if (!task) {
    if (existing) existing.remove();
    state.taskChat.focusTask = "";
    return;
  }
  const signature = JSON.stringify([task.task_id, task.title]);
  if (existing && existing.dataset.signature === signature) return;
  const bar = existing || node("div", "task-chat-focus");
  bar.setAttribute("id", "task-chat-focus");
  bar.dataset.signature = signature;
  const clear = node("button", "text-button", "查看全部对话");
  clear.onclick = () => {
    state.taskChat.focusTask = "";
    renderTaskChat();
    $("group-chat-messages").focus();
  };
  bar.replaceChildren(
    node("span", "task-chat-focus-copy", "只看：" + task.title),
    clear,
  );
  if (!existing) meta.after(bar);
}
function updateTaskChatChrome(group, messages) {
  const chat = state.taskChat;
  const live = $("task-chat-live");
  const groupTasks = group
    ? state.tasks.filter((task) => assignedMember(task)?.g.id === group.id)
    : [];
  const running = groupTasks.filter((task) => task.status === "running").length;
  const done = groupTasks.filter(
    (task) => (task.display_status || task.status) === "done",
  ).length;
  $("task-chat-meta").textContent = group
    ? `${group.software} · ${group.members.length} 位成员 · ${running} 个任务执行中 · ${done}/${groupTasks.length} 步完成`
    : "连接项目后，这里会显示每个小组的真实协作记录。";
  renderTaskChatFocus();
  const runningTasks = state.tasks.filter(
    (task) => task.status === "running",
  ).length;
  $("task-chat-summary").textContent =
    `${state.config.groups.length} 个小组 · ${runningTasks} 个任务执行中 · ${messages.length} 条已加载对话`;
  $("group-chat-older").hidden = !chat.hasMore;
  $("group-chat-latest").hidden = !messages.length;
  const active = groupTasks.filter((task) => task.status === "running");
  const presence = $("group-chat-presence");
  presence.replaceChildren();
  presence.hidden = !active.length;
  if (active.length) {
    const labels = active.slice(0, 2).map((task) => {
      const member = assignedMember(task);
      return `${member?.m.name || "成员"}正在执行「${task.title}」`;
    });
    presence.append(
      node("span", "task-chat-presence-dot"),
      node(
        "span",
        "",
        labels.join(" · ") +
          (active.length > 2 ? ` 等 ${active.length} 个任务` : ""),
      ),
    );
  }
  live.className = "task-chat-live";
  if (chat.loading) {
    live.textContent = "◌ 正在同步…";
  } else if (chat.error) {
    live.textContent = "○ 同步中断";
    live.classList.add("error");
  } else if (running) {
    live.textContent = `● ${running} 个任务执行中 · 同步于 ${new Date().toLocaleTimeString()}`;
    live.classList.add("online");
  } else {
    live.textContent = `● 已同步 · ${new Date().toLocaleTimeString()}`;
    live.classList.add("online");
  }
}
function renderGroupChatMessages() {
  const chat = state.taskChat;
  const box = $("group-chat-messages");
  // Clear a focus whose task disappeared before deriving the visible set.
  renderTaskChatFocus();
  const previousHeight = box.scrollHeight;
  const previousTop = box.scrollTop;
  const nearBottom =
    previousHeight === 0 ||
    previousHeight - previousTop - box.clientHeight < 96;
  const messages = [...chat.messages.values()]
    .filter((message) => !chat.focusTask || message.task_id === chat.focusTask)
    .filter(
      (message) =>
        state.sessionFilter === "all" ||
        (state.sessionFilter === "child"
          ? Boolean(message.parent_agent)
          : !message.parent_agent),
    )
    // Pagination is keyed by the durable log id. Sorting by wall-clock time
    // would let a clock adjustment reorder pages as older history arrives.
    .sort(
      (a, b) =>
        (a.log_id || 0) - (b.log_id || 0) ||
        a.created_at - b.created_at ||
        String(a.id).localeCompare(String(b.id)),
    );
  const signature = JSON.stringify(
    messages.map((message) => [
      message.id,
      message.status || "",
      message.status_label || "",
      message.member_name || "",
      message.task_title || "",
      message.task_status || "",
      message.level || "",
      message.entry_kind || "",
      message.truncated ? "1" : "0",
      String(message.content || "").length,
    ]),
  );
  updateTaskChatChrome(
    state.config.groups.find((group) => group.id === chat.group) || null,
    messages,
  );
  if (signature === chat.signature) {
    // The page may not affect the focused view; do not leave a pending
    // "preserve scroll for prepended rows" flag behind.
    chat.scrollOlder = false;
    return;
  }
  const fragment = document.createDocumentFragment();
  let previousDay = "";
  for (const message of messages) {
    const date = new Date(message.created_at * 1000);
    const day = date.toLocaleDateString();
    if (day !== previousDay) {
      fragment.append(node("div", "chat-day", day));
      previousDay = day;
    }
    if (message.kind === "activity") {
      fragment.append(renderActivityMessage(message, date));
      continue;
    }
    if (message.role === "system") {
      const event = node(
        "div",
        "task-chat-event level-" + (message.level || "info"),
      );
      event.append(
        node("span", "task-chat-event-dot"),
        node(
          "span",
          "task-chat-event-copy",
          (message.member_name ? message.member_name + " · " : "") +
            (message.content || "") +
            (message.task_title || message.task_id
              ? " · " + (message.task_title || message.task_id)
              : ""),
        ),
        node("time", "", date.toLocaleTimeString()),
      );
      fragment.append(event);
      continue;
    }
    const card = node("article", "task-chat-message " + message.role);
    const head = node("header", "chat-message-head");
    head.append(
      node("strong", "", taskChatRole(message)),
      node("time", "", date.toLocaleTimeString()),
    );
    card.append(head);
    const context = message.task_title || message.task_id;
    if (context)
      card.append(node("small", "task-chat-context", "关于：" + context));
    appendChatContent(card, message);
    if (message.status)
      card.append(
        node(
          "span",
          "chat-status-pill " + message.status,
          message.status_label || message.status,
        ),
      );
    if (message.truncated)
      card.append(node("span", "task-chat-truncated", "内容过长，已截断"));
    fragment.append(card);
  }
  if (!messages.length)
    fragment.append(
      node(
        "div",
        "task-chat-empty",
        chat.focusTask
          ? "已加载的对话中没有这个任务的记录。可加载更早对话，或查看全部对话。"
          : "这个小组还没有对话。任务派发后，成员引导、执行进展和交付结果会在这里实时出现。",
      ),
    );
  box.replaceChildren(fragment);
  chat.signature = signature;
  if (chat.scrollOlder && signature !== chat.scrollSignature) {
    box.scrollTop = previousTop + (box.scrollHeight - previousHeight);
  } else if (nearBottom) {
    box.scrollTop = box.scrollHeight;
  } else {
    box.scrollTop = previousTop;
  }
  chat.scrollOlder = false;
  chat.scrollSignature = signature;
}
function applyGuidanceOutcome(chat, message) {
  if (
    message.event_kind !== "chat.applied" &&
    message.event_kind !== "chat.failed"
  )
    return;
  const status = message.event_kind === "chat.applied" ? "applied" : "failed";
  const statusLabel = status === "applied" ? "已应用" : "未能应用";
  let target = null;
  if (message.guidance_id) {
    target = [...chat.messages.values()].find(
      (item) =>
        item.kind === "guidance" && item.guidance_id === message.guidance_id,
    );
  } else {
    // Older outcome logs did not carry the member-message id. The server
    // permits at most one pending guidance per task, so the latest guidance
    // for that task/member is the only safe fallback.
    const candidates = [...chat.messages.values()]
      .filter(
        (item) =>
          item.kind === "guidance" &&
          item.task_id === message.task_id &&
          (!message.member_id || item.member_id === message.member_id) &&
          (item.log_id || 0) < (message.log_id || 0),
      )
      .sort((a, b) => (b.log_id || 0) - (a.log_id || 0));
    if (candidates[0]?.status === "pending") target = candidates[0];
  }
  if (target)
    chat.messages.set(target.id, {
      ...target,
      status,
      status_label: statusLabel,
    });
}
function mergeGroupChatMessages(chat, received) {
  for (const message of received) chat.messages.set(message.id, message);
  // The guidance row is immutable, but its applied/failed outcome arrives as a
  // later log. Fold it into the already-loaded card so the live console does
  // not keep showing "pending" beside an "applied" event.
  for (const message of received) applyGuidanceOutcome(chat, message);
}
async function loadGroupChat(reset = false, older = false) {
  const chat = state.taskChat;
  const project = state.project;
  const group = chat.group;
  if (!project || !group) {
    renderTaskChat();
    return;
  }
  if (chat.loading) return;
  if (older && !chat.hasMore) return;
  chat.loading = true;
  if (older) chat.scrollOlder = true;
  if (reset) {
    chat.scrollOlder = false;
    chat.error = "";
    $("group-chat-status").textContent = "正在读取小组对话…";
    // Keep the readable conversation on screen until the replacement page
    // arrives. A transient failure must not blank the history.
    renderTaskChat();
  }
  try {
    const payload = { project, group_id: group, limit: 100 };
    // Without a cursor the server returns the newest page, not the oldest
    // unseen rows. That page can still have older history, so record its
    // pagination cursor even when this is a routine poll.
    const useCursor = !reset && !older && Boolean(chat.after);
    if (useCursor) payload.after = chat.after;
    if (older && chat.before) payload.before = chat.before;
    const data = await localRequest("group-chat", payload);
    if (
      state.taskChat !== chat ||
      project !== state.project ||
      group !== chat.group
    )
      return;
    const received = data.messages || [];
    const wasLoaded = chat.loaded;
    const previousError = chat.error;
    if (reset) {
      chat.messages = new Map();
      mergeGroupChatMessages(chat, received);
      chat.after = Number(data.next_after) || 0;
      chat.hasMore = Boolean(data.has_more);
      chat.before = data.next_before;
      chat.loaded = true;
      chat.signature = null;
      $("group-chat-status").textContent = received.length
        ? `已刷新对话 · ${received.length} 条`
        : "这个小组还没有对话。";
    } else {
      mergeGroupChatMessages(chat, received);
      if (!older) {
        chat.after = Math.max(chat.after, Number(data.next_after) || 0);
        if (!useCursor || !chat.loaded) {
          chat.hasMore = Boolean(data.has_more);
          chat.before = data.next_before;
        }
        chat.loaded = true;
        // Quiet polls must not rewrite the live status: role="status" would
        // otherwise announce "已是最新" every two seconds.
        if (received.length)
          $("group-chat-status").textContent =
            `收到新对话 · ${received.length} 条`;
        else if (!wasLoaded)
          $("group-chat-status").textContent = "这个小组还没有对话。";
        else if (previousError)
          $("group-chat-status").textContent = "已恢复同步。";
      } else {
        chat.hasMore = Boolean(data.has_more);
        chat.before = data.next_before;
        if (!received.length) chat.scrollOlder = false;
        chat.loaded = true;
        $("group-chat-status").textContent = received.length
          ? `已加载更早对话 · ${received.length} 条`
          : "已是最新";
      }
    }
    chat.error = "";
    renderTaskChat();
  } catch (error) {
    if (state.taskChat !== chat) return;
    chat.error = error.message;
    chat.scrollOlder = false;
    const failure =
      error.message + (group === chat.group ? "" : "（小组已切换）");
    if ($("group-chat-status").textContent !== failure)
      $("group-chat-status").textContent = failure;
    updateTaskChatChrome(
      state.config.groups.find((item) => item.id === chat.group) || null,
      [...chat.messages.values()],
    );
  } finally {
    chat.loading = false;
    if (state.taskChat === chat) {
      updateTaskChatChrome(
        state.config.groups.find((item) => item.id === chat.group) || null,
        [...chat.messages.values()],
      );
    }
  }
}
$("task-chat-refresh").onclick = () =>
  action(async () => {
    await refresh();
    await loadGroupChat(true);
  });
$("group-chat-older").onclick = () => loadGroupChat(false, true);
$("group-chat-latest").onclick = () => {
  const box = $("group-chat-messages");
  box.scrollTop = box.scrollHeight;
};
$("group-chat-member").onchange = () => {
  const chat = state.taskChat;
  if (chat.sending) return;
  const option = $("group-chat-member").selectedOptions[0];
  if (chat.pending) {
    const same =
      option &&
      option.dataset.memberId === chat.pending.member_id &&
      option.value === chat.pending.task_id;
    $("group-chat-status").textContent = same
      ? "上一条引导的发送结果尚未确认；请保持原内容重试。"
      : "上一条引导的发送结果尚未确认；请先切回「" +
        (chat.pending.target_label || "原任务") +
        "」重试。";
  } else if (!$("group-chat-form").elements.message.value.trim())
    $("group-chat-status").textContent = "已切换引导对象。";
  renderTaskChat();
};
$("group-chat-form").onsubmit = async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const chat = state.taskChat;
  const select = $("group-chat-member");
  const option = select.selectedOptions[0];
  const message = form.elements.message.value.trim();
  if (!chat.group || !option || !message || chat.sending) return;
  const memberId = option.dataset.memberId;
  const taskId = option.dataset.taskId;
  const samePending =
    chat.pending &&
    chat.pending.project === state.project &&
    chat.pending.member_id === memberId &&
    chat.pending.task_id === taskId;
  if (chat.pending && !samePending) {
    $("group-chat-status").textContent =
      "上一条引导的发送结果尚未确认；请先切回「" +
      (chat.pending.target_label || "原任务") +
      "」重试，避免重复发送。";
    return;
  }
  if (samePending && chat.pending.message !== message) {
    $("group-chat-status").textContent =
      "上一条引导的发送结果尚未确认；请保持原内容重试。";
    return;
  }
  if (!samePending)
    chat.pending = {
      project: state.project,
      member_id: memberId,
      task_id: taskId,
      target_label: option.textContent,
      message,
      request_id: crypto.randomUUID(),
    };
  chat.sending = true;
  chat.lastError = "";
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  select.disabled = true;
  form.elements.message.disabled = true;
  $("group-chat-status").textContent = "正在发送引导…";
  try {
    await localRequest("member-guide", chat.pending);
    chat.pending = null;
    if (state.taskChat === chat) {
      // Clear only what was sent. Resetting the form would also reset the
      // selected task back to the first option, silently changing the target
      // for the next message.
      form.elements.message.value = "";
      $("group-chat-status").textContent =
        "引导已提交；成员应用后会继续出现在这组对话里。";
      await loadGroupChat(false);
      if (state.taskChat === chat && chat.error)
        $("group-chat-status").textContent =
          "引导已提交；对话暂时同步失败，稍后会自动重试。";
    }
  } catch (error) {
    if (error.status >= 400 && error.status < 500) chat.pending = null;
    chat.lastError = error.message;
    if (state.taskChat === chat)
      $("group-chat-status").textContent =
        error.message +
        (chat.pending ? "。再次点击发送会复用同一请求，不会重复添加。" : "");
  } finally {
    chat.sending = false;
    if (state.taskChat === chat) {
      renderTaskChat();
      if (!form.elements.message.disabled) form.elements.message.focus();
    }
  }
};
async function controlTask(task, actionName) {
  return localRequest("task-control", {
    project: state.project,
    ...mutation({
      task_id: task.task_id,
      expected_revision: task.revision,
      action: actionName,
      reason: "任务指挥台操作",
    }),
  });
}
async function deleteTask(task) {
  return localRequest("task-delete", {
    project: state.project,
    ...mutation({
      task_id: task.task_id,
      expected_revision: task.revision,
    }),
  });
}
function openDeleteTask(task) {
  const warning =
    "永久删除任务“" +
    (task.title || task.task_id) +
    "”？\n\n此操作不可撤销：任务记录、成员消息一并从本机数据库移除。\n\n如果已有产出，产物本身不会被删除。";
  if (!confirm(warning)) return;
  action(async () => {
    await deleteTask(task);
    await refresh();
    notice("任务已永久删除。");
  });
}
const logLevelLabels = {
  debug: "调试",
  info: "信息",
  warning: "警告",
  error: "错误",
};
function logGroupName(groupId) {
  return (
    state.config.groups.find((group) => group.id === groupId)?.name || groupId
  );
}
function renderLogGroups() {
  const select = $("log-group");
  if (!select) return;
  const previous = select.value || state.logs.group;
  const options = [new Option("全部小组", "")];
  for (const group of state.config.groups)
    options.push(new Option(group.name, group.id));
  select.replaceChildren(...options);
  if ([...select.options].some((option) => option.value === previous))
    select.value = previous;
}
function renderLogSummary(summary) {
  const target = $("log-summary");
  target.replaceChildren();
  if (!summary) return;
  const tasks = summary.tasks || {};
  const cards = [
    ["日志总数", summary.total_logs || 0],
    ["错误", summary.by_level?.error || 0],
    ["警告", summary.by_level?.warning || 0],
    ["任务总数", tasks.total || 0],
    [
      "执行中 / 完成",
      `${tasks.status?.running || 0} / ${tasks.status?.done || 0}`,
    ],
    [
      "平均完成耗时",
      tasks.avg_done_seconds == null
        ? "—"
        : Math.round(tasks.avg_done_seconds) + " 秒",
    ],
  ];
  for (const [label, value] of cards) {
    const card = node("div", "log-summary-card");
    card.append(node("strong", "", String(value)), node("small", "", label));
    target.append(card);
  }
  if (summary.by_group?.length) {
    const groupCard = node("div", "log-summary-card log-summary-groups");
    groupCard.append(node("small", "", "按小组"));
    for (const item of summary.by_group.slice(0, 8))
      groupCard.append(
        node(
          "span",
          "",
          logGroupName(item.group_id) + " · " + item.count + " 条",
        ),
      );
    target.append(groupCard);
  }
}
function logDetails(details) {
  const text = JSON.stringify(details || {}, null, 2);
  if (text === "{}") return null;
  const box = node("details", "log-details");
  box.append(node("summary", "", "查看结构化详情"));
  box.append(node("pre", "", text.slice(0, 12000)));
  return box;
}
function renderLogs() {
  const target = $("log-list");
  target.replaceChildren();
  renderLogSummary(state.logs.summary);
  for (const entry of state.logs.entries) {
    const row = node("article", "log-row level-" + entry.level);
    const head = node("div", "log-row-heading");
    head.append(
      node(
        "span",
        "badge level-" + entry.level,
        logLevelLabels[entry.level] || entry.level,
      ),
      node("strong", "", entry.message || entry.kind),
      node("time", "", new Date(entry.created_at * 1000).toLocaleString()),
    );
    const meta = [
      logGroupName(entry.group_id),
      entry.member_id ? "成员 " + entry.member_id : "",
      entry.task_id ? "任务 " + entry.task_id : "",
      entry.kind,
    ].filter(Boolean);
    row.append(head, node("p", "muted", meta.join(" · ")));
    const details = logDetails(entry.details);
    if (details) row.append(details);
    target.append(row);
  }
  if (!state.logs.entries.length)
    target.append(
      node(
        "p",
        "muted",
        state.logs.loaded
          ? "当前筛选条件下没有日志。"
          : "切换到日志分析后读取小组日志。",
      ),
    );
  $("log-older").disabled = !state.logs.hasMore || state.logs.loading;
  $("log-status").textContent = state.logs.error
    ? state.logs.error
    : state.logs.loaded
      ? `${state.logs.entries.length} 条 · 最新在前`
      : "尚未读取";
}
async function loadLogs(reset = false) {
  if (!state.project || state.logs.loading) return;
  if (!reset && !state.logs.hasMore) return;
  state.logs.loading = true;
  if (reset) {
    state.logs.before = 0;
    state.logs.entries = [];
    state.logs.summary = null;
    state.logs.hasMore = false;
    state.logs.loaded = false;
    state.logs.error = "";
  }
  $("log-status").textContent = "正在读取日志…";
  const params = {
    before: reset ? 0 : state.logs.before,
    limit: 50,
  };
  if (state.logs.group) params.group_id = state.logs.group;
  if (state.logs.level) params.level = state.logs.level;
  if (state.logs.query) params.query = state.logs.query;
  try {
    const data = await api("group_logs_list", params);
    state.logs.entries.push(...(data.logs || []));
    state.logs.summary = data.analysis;
    state.logs.hasMore = Boolean(data.has_more);
    state.logs.before = data.next_cursor || 0;
    state.logs.loaded = true;
    state.logs.error = "";
    renderLogs();
  } catch (error) {
    state.logs.error = error.message;
    renderLogs();
  } finally {
    state.logs.loading = false;
    renderLogs();
  }
}
$("log-filter-form").onsubmit = (event) => {
  event.preventDefault();
  state.logs.group = $("log-group").value;
  state.logs.level = $("log-level").value;
  state.logs.query = $("log-query").value.trim();
  action(() => loadLogs(true));
};
$("logs-refresh").onclick = () => action(() => loadLogs(true));
$("log-older").onclick = () => action(() => loadLogs(false));
$("log-latest").onclick = () =>
  action(async () => {
    $("log-query").value = "";
    state.logs.query = "";
    await loadLogs(true);
  });
let transferSelection = null;
function openTransfer(task) {
  transferSelection = { task, project: state.project };
  $("transfer-task").textContent = task.title;
  const form = $("transfer-form");
  form.reset();
  form.elements.member_id.replaceChildren(
    ...people()
      .filter(
        ({ m }) =>
          m.agent_id &&
          m.relationship !== "native_subagent" &&
          m.id !== assignedMember(task)?.m.id,
      )
      .map(({ g, m }) => new Option(g.name + " · " + m.name, m.id)),
  );
  $("transfer-dialog").showModal();
}
$("transfer-form").onsubmit = (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const memberId = form.elements.member_id.value,
    note = form.elements.note.value.trim();
  action(async () => {
    if (!transferSelection || transferSelection.project !== state.project)
      throw Error("项目已切换，请重新选择任务。");
    if (!memberId || !note) throw Error("请选择接收成员并填写交接说明。");
    let task = transferSelection.task;
    if (task.status === "running") {
      const paused = await controlTask(task, "pause");
      task = paused.task;
      transferSelection.task = task;
      if (paused.stopping)
        throw Error("任务已暂停，进程仍在退出；请稍后再次确认交接。");
    }
    await localRequest("task-control", {
      project: state.project,
      ...mutation({
        action: "handoff",
        task_id: task.task_id,
        expected_revision: task.revision,
        member_id: memberId,
        note,
      }),
    });
    $("transfer-dialog").close();
    await refresh();
    notice("工作已交接并保持暂停；确认后点击继续，由新成员领取。");
  });
};
$("task-meeting-filter").onchange = renderTaskDashboard;
$("execution-more").onclick = () => {
  state.taskHistory.expanded = !state.taskHistory.expanded;
  renderTaskDashboard();
};

let renameGroupId = null,
  renameGroupProject = "",
  revisionParent = null;
$("rename-group-form").onsubmit = (event) => {
  event.preventDefault();
  const name = event.currentTarget.elements.name.value.trim();
  action(async () => {
    if (!name) throw Error("请输入小组名称");
    const project = renameGroupProject || state.project;
    const config = await api("office_get", { project });
    const groups = structuredClone(config.groups || []);
    const group = groups.find((g) => g.id === renameGroupId);
    if (!group) throw Error("小组已不存在，请刷新");
    group.name = name;
    const next = await api(
      "office_put",
      mutation({
        project,
        expected_revision: config.revision,
        groups,
      }),
      { project },
    );
    if (project === state.project) {
      state.config = next;
      render();
    }
    state.teams.loaded = false;
    if (!$("teams-view").hidden) await loadTeams(true);
    $("rename-group-dialog").close();
    notice("小组名称已保存。");
  });
};
$("revision-form").onsubmit = (event) => {
  event.preventDefault();
  const changes = event.currentTarget.elements.changes.value.trim();
  action(async () => {
    const result = await api(
      "meeting_revision",
      mutation({
        meeting_id: revisionParent.id,
        briefing_id: id("brief"),
        changes,
      }),
    );
    $("revision-dialog").close();
    await refresh();
    $("task-meeting-filter").value = result.meeting.root_id;
    renderTaskDashboard();
    $("task-iteration-filter").value = result.meeting.id;
    renderTaskDashboard();
    notice("修改轮次已创建，主导者将基于前轮成果拆解本次修改。");
  });
};
$("task-iteration-filter").onchange = renderTaskDashboard;
function renderMemberWall() {
  const wall = $("member-live-wall");
  wall.replaceChildren();
  for (const { g, m } of people()) {
    const live = state.liveMembers.find((v) => v.member_id === m.id),
      task = live?.task;
    const card = node(
      "article",
      "member-live-card " + (task?.status || "idle"),
    );
    const head = node("div", "live-member-heading");
    head.append(
      avatar(m),
      node("h4", "", g.name + " · " + m.name),
      node(
        "span",
        "badge",
        task ? labels[task.display_status] || labels[task.status] : "待命",
      ),
    );
    card.append(head);
    card.append(node("p", "", task?.title || "暂无进行中的任务"));
    if (task?.status === "running")
      card.append(
        node(
          "p",
          "muted",
          "正在执行 · 最后状态更新 " +
            new Date(task.updated_at * 1000).toLocaleTimeString(),
        ),
      );
    for (const message of live?.messages || []) {
      const bubble = node("div", "live-message " + message.role);
      bubble.append(
        node(
          "small",
          "",
          message.role === "user"
            ? "用户引导 · " +
                ({ pending: "处理中", applied: "已加入指令", failed: "未应用" }[
                  message.status
                ] || message.status)
            : message.role === "assistant"
              ? message.kind === "activity"
                ? message.parent_agent
                  ? "子会话输出 · " + message.source_agent
                  : "成员实时输出"
                : "成员发布结果"
              : "执行反馈",
        ),
        node("p", "", message.text),
      );
      card.append(bubble);
    }
    if (task?.blockers?.length)
      card.append(node("p", "muted", "等待前置步骤或输入完成"));
    if (m.relationship === "native_subagent" && !task)
      card.append(
        node(
          "small",
          "muted",
          "原生会话尚未接入，等待对应运行时提供可验证记录。",
        ),
      );
    const chat = node("button", "text-button", "查看记录 / 发送引导");
    chat.onclick = () => openMemberChat(g.id, m.id);
    card.append(chat);
    wall.append(card);
  }
  if (!people().length)
    wall.append(
      node("p", "muted", "配置成员后，这里将显示每位成员的工作进展。"),
    );
  $("live-wall-sync").textContent =
    "● 自动刷新 · " + new Date().toLocaleTimeString();
}

function renderRuntime() {
  const labels = {healthy: "运行正常", starting: "正在启动", stopping: "正在停止",
    stopped: "执行环境已停止", disconnected: "尚未连接执行环境", stale: "调度响应超时",
    degraded: "部分执行环境异常", refresh_pending: "等待应用模型配置"};
  const health = state.runtime?.health;
  const status = state.syncFailed ? "unavailable" : health?.status || "unavailable";
  $("runtime-badge").textContent = state.syncFailed ? "同步中断 · 数据可能过期" :
    labels[status] || (state.project ? "运行状态暂不可用" : "等待连接");
  $("runtime-badge").dataset.status = status;
  $("runtime-reconnect").hidden = !state.workspace || !["stopped", "disconnected"].includes(status);
  const capacity = state.runtime?.capacity;
  $("runtime-summary").textContent = capacity ?
    `本服务执行容量 ${capacity.active_total} / ${capacity.limit} · 当前项目 ${capacity.active.length} 个执行 · 同一项目目录一次执行 1 个任务` :
    "连接项目后显示执行容量、排队原因和异常任务。状态不可用时请检查本地服务。";
  const progress = state.overview?.progress;
  $("progress-explanation").textContent = progress ?
    `本项目全部任务：${progress.completed} / ${progress.eligible} 个有效步骤已完成；已取消 ${progress.cancelled} 个、结果过期 ${progress.stale} 个。待审核不计为完成。` :
    "已取消的步骤不计入完成率；待审核和结果过期的步骤不计为完成。";
  const queue = $("runtime-queue");
  queue.replaceChildren();
  const reasons = {workspace_busy: "等待本项目上一项执行释放目录", capacity_full: "等待本服务执行名额", fair_queue: "已排队，等待调度"};
  for (const item of capacity?.waiting || []) {
    const task = state.tasks.find((t) => t.task_id === item.task_id);
    queue.append(node("p", "runtime-wait", `${task?.title || item.task_id} · ${reasons[item.reason] || "等待调度"}`));
  }
  for (const [identity, worker] of Object.entries(health?.workers || {})) {
    if (worker.ok) continue;
    const agentId = identity.slice(identity.indexOf("/") + 1);
    const label = state.agents.find((agent) => agent.agent_id === agentId)?.label || agentId;
    queue.append(node("p", "runtime-wait", `${label} · 连接异常（${worker.code || "未知原因"}），调度器正在重连。`));
  }
  const attention = state.overview?.attention || [];
  const signature = JSON.stringify([state.project, attention]);
  $("runtime-attention-title").textContent = `需要处理 ${state.overview?.attention_total || 0} 项${(state.overview?.attention_total || 0) > 20 ? " · 显示最近 20 项" : ""}`;
  if (signature === state.runtimeAttentionSignature) return;
  state.runtimeAttentionSignature = signature;
  const list = $("runtime-attention-list");
  list.replaceChildren();
  const advice = {failed: "执行失败：查看错误，修复配置后重试", lease_expired: "执行失联：检查进程和文件后再重试",
    waiting_user: "等待你的回答或确认", awaiting_review: "交付待审核", stale_result: "上游已变化：检查结果并重新执行", paused: "已暂停：确认后继续"};
  for (const item of attention) {
    const row = node("div", "runtime-issue");
    const description = node("div");
    description.append(node("strong", "", item.title), node("p", "muted", advice[item.reason] || item.reason));
    if (item.failure?.message) description.append(node("p", "muted", item.failure.message));
    const button = node("button", "secondary", item.reason === "waiting_user" ? "查看待决策" : "查看任务");
    button.onclick = () => action(async () => {
      if (item.reason === "waiting_user") { $("open-decision-center").click(); return; }
      const task = await api("task_get", {task_id: item.task_id});
      show("tasks");
      await openTaskInspect(task);
    });
    row.append(description, button);
    list.append(row);
  }
  if (!attention.length) list.append(node("p", "muted", "当前没有需要恢复或审核的步骤。"));
}
$("runtime-reconnect").onclick = () => action(async () => {
  if (state.pending) throw Error("请先确认当前项目的待定写入请求。");
  applyLocalStatus(await localRequest("connect", {path: state.workspace}));
  await refresh();
});

function renderDecisions() {
  const pending = state.decisions.filter((d) => d.status === "pending");
  $("decision-count").textContent = pending.length;
  $("hero-decisions").textContent = pending.length;
  const completed = state.tasks.filter(
      (t) => (t.display_status || t.status) === "done" && t.result_freshness?.fresh !== false,
    ).length,
    total = state.tasks.filter((t) => t.status !== "cancelled").length;
  const percent = state.overview?.progress.percent ?? (total ? Math.round((completed / total) * 100) : 0);
  $("hero-progress").textContent = percent + "%";
  $("mission-ring").style.setProperty("--progress", percent + "%");
  const running = state.overview ? state.overview.counts.running || 0 : state.tasks.filter((t) => (t.display_status || t.status) === "running").length;
  $("hero-running").textContent = running + " 个执行中";
  document.body.dataset.running = running ? "true" : "false";
  const signature = JSON.stringify([state.project, pending]);
  if (signature === state.decisionSignature) return;
  state.decisionSignature = signature;
  const list = $("decision-list");
  list.replaceChildren();
  for (const decision of pending) {
    const card = node("article", "decision-card");
    const task = state.tasks.find((t) => t.task_id === decision.task_id),
      member = task ? assignedMember(task) : null;
    const meeting = state.meetings.find((m) => m.id === decision.meeting_id);
    card.append(
      node(
        "span",
        "decision-kicker",
        decision.kind === "execution_gate"
          ? "方案已就绪 / 请决定下一步"
          : "工作已暂停 / 需要你的判断",
      ),
      node("h3", "", decision.question.question),
    );
    card.append(
      node(
        "p",
        "muted",
        [meeting?.title, member?.g.name, member?.m.name]
          .filter(Boolean)
          .join(" / "),
      ),
    );
    if (decision.question.context) {
      const detail = node("details");
      detail.append(
        node("summary", "", "查看问题上下文"),
        node("p", "task-description", decision.question.context),
      );
      card.append(detail);
    }
    const input = node("textarea");
    input.rows = 3;
    input.maxLength = 4000;
    input.placeholder = "补充你的选择、约束或调整要求…";
    input.value = state.decisionDrafts.get(decision.decision_id) || "";
    input.oninput = () =>
      state.decisionDrafts.set(decision.decision_id, input.value);
    const options = node("div", "decision-options");
    for (const option of decision.question.options || []) {
      const button = node("button", "secondary", option);
      button.type = "button";
      button.onclick = () => {
        input.value = option;
        state.decisionDrafts.set(decision.decision_id, option);
        input.focus();
      };
      options.append(button);
    }
    card.append(options, input);
    const actions = node("div", "task-actions");
    const choices =
      decision.kind === "execution_gate"
        ? [
            ["finish_discussion", "仅保存方案"],
            ["continue", "确认并执行"],
            ["revise", "按补充要求重新讨论"],
            ["cancel", "取消任务"],
          ]
        : [
            ["continue", "提交回复并继续"],
            ["cancel", "取消任务"],
          ];
    for (const [operation, label] of choices) {
      const button = node(
        "button",
        operation === "continue" ? "primary" : "secondary",
        label,
      );
      button.type = "button";
      button.onclick = () =>
        action(async () => {
          const answer = input.value.trim();
          if (
            ((decision.kind !== "execution_gate" && operation === "continue") ||
              operation === "revise") &&
            !answer
          )
            throw Error("请先填写明确回复或选择一个建议选项。");
          const key = decision.decision_id + ":" + operation + ":" + answer;
          let payload = state.decisionRequests.get(key);
          if (!payload) {
            payload = {
              project: state.project,
              ...mutation({
                decision_id: decision.decision_id,
                expected_revision: decision.revision,
                action: operation,
                answer,
              }),
            };
            state.decisionRequests.set(key, payload);
          }
          button.disabled = true;
          try {
            await localRequest("decision-respond", payload);
            state.decisionDrafts.delete(decision.decision_id);
            state.decisionRequests.delete(key);
            await refresh();
            notice(
              operation === "finish_discussion"
                ? "方案已保存，讨论结束。"
                : operation === "cancel"
                  ? "任务已取消。"
                  : "回复已保存，团队将根据你的决定继续。",
            );
          } catch (error) {
            if (error.status >= 400 && error.status < 500)
              state.decisionRequests.delete(key);
            throw error;
          } finally {
            button.disabled = false;
          }
        });
      actions.append(button);
    }
    card.append(actions);
    list.append(card);
  }
  if (!pending.length)
    list.append(
      node(
        "p",
        "decision-empty",
        "所有确认事项已处理。需要你判断时，团队会在这里停下来等待。",
      ),
    );
}
$("open-decision-center").onclick = () => {
  show("tasks");
  $("decision-center").scrollIntoView({ behavior: "smooth", block: "start" });
};
$("session-filter").onchange = (event) => {
  state.sessionFilter = event.target.value;
  state.taskChat.signature = null;
  renderGroupChatMessages();
};

function renderDeliveryLanes(tasks) {
  const lanes = $("delivery-lanes");
  lanes.replaceChildren();
  const columns = [
    ["ready", "待领取"],
    ["running", "执行中"],
    ["attention", "需要处理"],
    ["awaiting_review", "待验收"],
    ["done", "已完成"],
  ];
  for (const [key, title] of columns) {
    const entries = tasks.filter((t) => {
      const status = t.display_status || t.status;
      return key === "attention"
        ? [
            "blocked",
            "waiting_user",
            "paused",
            "failed",
            "lease_expired",
          ].includes(status)
        : status === key;
    });
    const lane = node("section", "delivery-lane " + key);
    const head = node("div", "delivery-lane-head");
    head.append(
      node("h4", "", title),
      node("span", "badge", String(entries.length)),
    );
    lane.append(head);
    const list = node("div", "delivery-lane-list");
    for (const task of entries) {
      const member = assignedMember(task);
      const card = node("button", "delivery-ticket");
      card.type = "button";
      card.append(
        node("strong", "", task.title),
        node(
          "small",
          "",
          member
            ? member.g.name + " / " + member.m.name
            : task.owner || "待分配",
        ),
      );
      const reasons = (task.blockers || []).map(
        (b) => (labels[b.reason] || b.reason) + " · " + b.key,
      );
      card.append(
        node(
          "small",
          "ticket-state",
          reasons[0] || labels[task.display_status] || labels[task.status],
        ),
        node(
          "small",
          "",
          (task.attempt_count ?? task.fence ?? 0) + " 次执行尝试",
        ),
      );
      card.onclick = () => openTaskInspect(task);
      list.append(card);
    }
    if (!entries.length) list.append(node("p", "muted", "暂无步骤"));
    lane.append(list);
    lanes.append(lane);
  }
}

let inspectContext = null;
async function openTaskInspect(task, older = false) {
  const project = state.project;
  if (!older) {
    inspectContext = { task, project, before: 0, loading: true };
    $("inspect-title").textContent = task.title;
    $("task-inspect-content").replaceChildren(
      node("p", "muted", "读取执行档案…"),
    );
    $("inspect-older").hidden = true;
    $("task-inspect-dialog").showModal();
  }
  const context = inspectContext;
  try {
    const data = await api("task_inspect", {
      project,
      task_id: task.task_id,
      before: older ? context.before : 0,
    });
    if (inspectContext !== context || project !== state.project) return;
    const content = $("task-inspect-content");
    if (!older) {
      content.replaceChildren();
      const summary = node("section", "inspect-summary");
      summary.append(
        node(
          "span",
          "badge",
          labels[data.task.display_status] || labels[data.task.status],
        ),
        node("p", "", data.task.description),
      );
      for (const b of data.task.blockers || [])
        summary.append(
          node(
            "p",
            "muted",
            "等待：" + b.key + " · " + (labels[b.reason] || b.reason),
          ),
        );
      if (data.task.review?.status === "pending") {
        const buttons = node("div", "actions");
        for (const [verb, label] of [
          ["approve", "验收通过"],
          ["reject", "驳回返工"],
        ]) {
          const button = node(
            "button",
            verb === "approve" ? "primary" : "secondary",
            label,
          );
          button.onclick = () => {
            $("task-inspect-dialog").close();
            openTaskReview(data.task, verb);
          };
          buttons.append(button);
        }
        summary.append(buttons);
      }
      content.append(summary);
      const refs = node("details", "inspect-section");
      refs.append(node("summary", "", "输入与交付物"));
      for (const ref of [
        ...data.task.inputs.map((key) => ({ key })),
        ...data.task.outputs,
      ])
        refs.append(
          node(
            "p",
            "muted",
            ref.key + (ref.version ? " @v" + ref.version : ""),
          ),
        );
      if (data.task.outputs.length) {
        const preview = node("button", "text-button", "读取本次交付内容");
        preview.onclick = () =>
          action(async () => {
            const values = await Promise.all(
              data.task.outputs.map((ref) =>
                api("item_get", {
                  project,
                  key: ref.key,
                  version: ref.version,
                }),
              ),
            );
            const text = node(
              "pre",
              "task-description",
              values.map((v) => JSON.stringify(v.value, null, 2)).join("\n"),
            );
            refs.append(text);
            refs.open = true;
            preview.disabled = true;
          });
        refs.append(preview);
      }
      content.append(refs);
      for (const review of data.reviews) {
        const card = node("article", "inspect-section");
        card.append(
          node(
            "h4",
            "",
            "第 " +
              review.fence +
              " 次交付 · " +
              ({ pending: "待验收", approved: "验收通过", rejected: "已驳回" }[
                review.status
              ] || review.status),
          ),
          node("p", "", review.note || "等待检查本次结果"),
        );
        for (const evidence of review.evidence)
          card.append(node("p", "muted", evidence));
        content.append(card);
      }
      for (const handoff of data.handoffs) {
        const card = node("article", "inspect-section");
        card.append(
          node("h4", "", "工作交接"),
          node("p", "", handoff.details.note || "额度交接"),
          node(
            "p",
            "muted",
            "接收成员：" +
              (handoff.details.to_member || handoff.details.to || "见检查点"),
          ),
        );
        if (handoff.details.checkpoint)
          card.append(
            node("p", "muted", "交接单：" + handoff.details.checkpoint),
          );
        content.append(card);
      }
      content.append(node("h3", "", "执行尝试（最新在前）"));
    }
    for (const attempt of data.attempts) {
      const card = node("article", "inspect-attempt");
      const outcome =
        {
          completed: "执行完成",
          failed: "执行失败",
          pause: "已暂停",
          cancel: "已取消",
          released: "主动释放",
          running: "执行中",
          superseded: "已被接管",
          lease_expired: "租约已过期",
        }[attempt.status] || attempt.status;
      card.append(
        node("h4", "", "#" + attempt.fence + " · " + outcome),
        node(
          "p",
          "muted",
          attempt.owner +
            " · " +
            new Date(attempt.started_at * 1000).toLocaleString(),
        ),
      );
      if (attempt.execution)
        card.append(
          node(
            "p",
            "",
            "模型：" + (attempt.execution.requested?.model || "运行时选择"),
          ),
        );
      if (attempt.ended_at)
        card.append(
          node(
            "p",
            "muted",
            "结束：" + new Date(attempt.ended_at * 1000).toLocaleString(),
          ),
        );
      if (attempt.outcome.message || attempt.outcome.reason)
        card.append(
          node("p", "", attempt.outcome.message || attempt.outcome.reason),
        );
      content.append(card);
    }
    if (!data.attempts.length && !older)
      content.append(node("p", "muted", "尚未领取任务；暂无执行尝试。"));
    context.before = data.next_before;
    $("inspect-older").hidden = !context.before;
  } catch (error) {
    if (inspectContext === context)
      $("task-inspect-content").append(node("p", "error", error.message));
  } finally {
    if (inspectContext === context) context.loading = false;
  }
}
$("inspect-older").onclick = () => {
  if (inspectContext && !inspectContext.loading) {
    inspectContext.loading = true;
    openTaskInspect(inspectContext.task, true);
  }
};

let reviewContext = null;
function openTaskReview(task, verb) {
  reviewContext = { task, verb, project: state.project, request: null };
  $("task-review-form").reset();
  $("review-title").textContent =
    verb === "approve" ? "验收通过" : "驳回并暂停";
  $("review-context").textContent =
    task.title + " · 第 " + task.fence + " 次交付";
  $("task-review-dialog").showModal();
}
$("task-review-form").onsubmit = (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  action(async () => {
    const context = reviewContext;
    if (context.project !== state.project)
      throw Error("项目已切换，请重新打开验收。");
    const payload = {
      project: context.project,
      task_id: context.task.task_id,
      expected_revision: context.task.revision,
      action: context.verb,
      note: form.elements.note.value.trim(),
      evidence: form.elements.evidence.value
        .split("\n")
        .map((v) => v.trim())
        .filter(Boolean),
    };
    context.request ||= mutation(payload);
    try {
      await api("task_review", context.request);
    } catch (error) {
      if (!state.pending) context.request = null;
      throw error;
    }
    $("task-review-dialog").close();
    await refresh();
    notice(
      context.verb === "approve"
        ? "验收通过，依赖步骤可以继续。"
        : "已驳回并暂停，返工要求已保存。恢复后才会重新执行。",
    );
  });
};

function renderProjectSessions() {
  const target = $("project-session-list");
  $("project-session-status").textContent = state.projectSessionsError
    ? "会话状态读取失败，保留上次记录；请检查服务或重启新版服务"
    : "首次执行创建，后续任务继续使用；各软件的历史独立保留";
  target.replaceChildren();
  const runtimes = new Set((state.config.groups || []).map((g) => g.software));
  for (const saved of state.projectSessions) runtimes.add(saved.runtime);
  for (const runtime of runtimes) {
    const binding = state.projectSessions.find((s) => s.runtime === runtime);
    const card = node("article", "project-session-card");
    card.append(
      node("strong", "", runtime),
      node(
        "span",
        "badge",
        binding?.session_id
          ? "固定会话已绑定"
          : binding?.status === "creating"
            ? "正在创建会话"
            : binding
              ? "绑定需检查"
              : state.projectSessionsError
                ? "状态暂不可用"
                : "首次执行时创建",
      ),
    );
    card.append(
      node(
        "p",
        "muted",
        binding?.session_id || "同项目的后续任务不会重复创建聊天",
      ),
    );
    if (binding) {
      card.append(
        node(
          "small",
          "muted",
          "已完成调用 " +
            (binding.turns || 0) +
            " 次 · " +
            ({
              ready: "上次调用已结束",
              running: "上次调用仍在进行或未正常结束",
              creating: "创建尚未确认身份",
              error: "上次调用失败，保留原会话",
              invalid: "绑定文件需要检查",
            }[binding.status] || binding.status),
        ),
      );
      if (binding.status !== "invalid" && !state.projectSessionsError) {
        const button = node("button", "text-button", "重建绑定");
        const project = state.project;
        button.onclick = () => {
          if (
            !confirm(
              "为该项目的 " +
                runtime +
                " 创建新的固定会话？原绑定会归档，软件里的旧聊天不会被删除。",
            )
          )
            return;
          action(async () => {
            if (project !== state.project)
              throw Error("项目已切换，请重新操作。");
            await localRequest("project-session-reset", {
              project,
              runtime,
              session_id: binding.session_id,
              updated_at: binding.updated_at,
            });
            await refresh();
            notice("原绑定已归档，下次执行将建立新的项目会话。");
          });
        };
        card.append(button);
      }
    }
    target.append(card);
  }
  if (!runtimes.size)
    target.append(
      node(
        "p",
        "muted",
        "配置小组后，每种软件会在首次任务执行时建立一个项目专用会话。",
      ),
    );
}

// Workbench is a projection of the existing ledger; it never invents progress.
function workbenchButton(text, run, primary = false) {
  const b = node("button", primary ? "primary" : "text-button", text);
  b.type = "button";
  b.onclick = run;
  return b;
}
function workbenchContext() {
  const meetings = state.meetings.filter(m => m.stage !== "cancelled");
  const pending = state.decisions.find(d => d.status === "pending");
  const meeting = meetings.find(m => m.id === state.focusedMeeting) ||
    meetings.find(m => !["done", "discussion_done"].includes(m.stage)) || meetings[0];
  let tasks = meeting ? state.tasks.filter(t => state.assignments.some(a =>
    a.meeting_id === meeting.id && a.task_id === t.task_id)) : state.tasks;
  tasks = [...tasks].sort((a, b) => (a.created_at || 0) - (b.created_at || 0) ||
    a.task_id.localeCompare(b.task_id, undefined, {numeric: true}));
  return {meeting, tasks, pending};
}
function workbenchStatus(task) {
  return task.result_freshness?.fresh === false ? "结果过期" : labels[task.display_status || task.status] || task.status;
}
function workbenchTaskProgress(tasks) {
  const eligible = tasks.filter(t => t.status !== "cancelled");
  const done = eligible.filter(t => (t.display_status || t.status) === "done" &&
    t.result_freshness?.fresh !== false && t.review?.status !== "pending").length;
  return {eligible: eligible.length, done, percent: eligible.length ? Math.round(done / eligible.length * 100) : 0};
}
function progressElement(progress) {
  const wrapper = node("div", "wb-progress");
  const bar = node("progress");
  bar.max = 100; bar.value = progress.percent;
  bar.setAttribute("aria-label", "有效步骤完成率");
  wrapper.append(bar, node("span", "", progress.percent + "%"));
  return wrapper;
}
function openWorkbenchTask(meeting) {
  state.focusedMeeting = meeting?.id || null;
  renderTaskFocus();
  show("tasks");
  $("task-focus").scrollIntoView({behavior: "smooth", block: "start"});
}
function renderWorkbench() {
  const signature = JSON.stringify([state.project, state.config, state.tasks,
    state.meetings, state.decisions, state.events.slice(-3), state.overview?.progress, state.focusedMeeting]);
  if (state.workbenchSignature === signature) return;
  state.workbenchSignature = signature;
  const {meeting, tasks, pending} = workbenchContext();
  const progress = workbenchTaskProgress(tasks);
  $("workbench-subtitle").textContent = (state.workspace?.split(/[\\/]/).pop() || "选择项目") + " · 让每一步协作清晰可见";
  $("wb-team-count").textContent = state.config.groups.length;
  const overall = state.overview?.progress || progress;
  $("wb-completed").textContent = `${overall.completed ?? overall.done} / ${overall.eligible} 个有效步骤已完成`;
  const current = $("workbench-current"); current.replaceChildren();
  const title = meeting?.title || tasks.find(t => t.status === "running")?.title || tasks[0]?.title;
  if (title) {
    current.append(node("h3", "wb-task-title", title), node("p", "muted", meeting ?
      `第 ${meeting.iteration || 1} 次迭代 · ${meetingStageLabels[meeting.stage] || meeting.stage}` : "独立任务"),
      progressElement(progress));
    const stages = node("div", "wb-stages");
    const keys = meeting?.mode === "discussion" ? ["planning", "reviewing", "ready_for_execution"] : ["planning", "delegating", "executing", "done"];
    for (const key of keys) stages.append(node("span", meeting?.stage === key ? "current" : "", meetingStageLabels[key] || key));
    current.append(stages);
    for (const task of tasks.filter(t => t.status !== "cancelled").sort((a,b) =>
      taskStatusOrder(a.display_status || a.status) - taskStatusOrder(b.display_status || b.status)).slice(0, 2)) {
      const row = node("div", "wb-task-row");
      row.append(node("span", "", task.title), node("span", "badge " + (task.display_status || task.status), workbenchStatus(task)));
      current.append(row);
    }
    current.append(workbenchButton("查看任务流程 →", () => openWorkbenchTask(meeting)));
  } else {
    current.append(node("h3", "wb-task-title", "从一个清晰的目标开始"),
      node("p", "muted", "配置团队，在会议室拆解目标、讨论方案并分派任务。"),
      workbenchButton("创建第一项任务", () => show("meeting"), true));
  }
  const inbox = $("workbench-decision"); inbox.replaceChildren();
  $("wb-decision-count").textContent = state.decisions.filter(d => d.status === "pending").length;
  inbox.append(node("h3", "", pending?.question?.question || "暂时没有待确认事项"),
    node("p", "muted", pending ? "查看完整上下文后，再决定下一步。" : "需要你判断时，团队会在这里提醒。"));
  if (pending) inbox.append(workbenchButton("查看并确认", () => $("open-decision-center").click(), true));
  const events = $("workbench-events"); events.replaceChildren();
  const kinds = {"task.created":"任务已派发", "task.claimed":"成员已领取任务", "task.completed":"步骤已完成", "task.failed":"任务执行失败", "task.pause":"任务已暂停", "task.resume":"任务已继续", "task.guidance":"收到任务引导", "office.saved":"团队配置已保存"};
  for (const event of state.events.slice(-3).reverse()) {
    const row = node("p", "wb-event");
    row.append(node("time", "", new Date(event.created_at * 1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"})), node("span", "", kinds[event.kind] || "项目记录已更新"));
    events.append(row);
  }
  if (!events.childNodes.length) events.append(node("p", "muted", "任务开始后，这里会记录协作动态。"));
  const teams = $("workbench-teams"); teams.replaceChildren();
  for (const group of state.config.groups) {
    const card = node("article", "wb-team");
    const heading = node("div", "card-heading");
    heading.append(node("span", "wb-software", group.software.slice(0, 1).toUpperCase()), node("h3", "", group.name));
    card.append(heading, node("p", "muted", group.software + " · " + group.members.length + " 名成员"));
    const members = node("div", "wb-members");
    for (const member of group.members) {
      const b = workbenchButton(member.name, () => openMemberChat(group.id, member.id));
      b.className = "member-chip";
      b.prepend(avatar(member));
      b.setAttribute("aria-label", "查看 " + member.name + " 的对话");
      members.append(b);
    }
    card.append(members);
    const groupTasks = state.tasks.filter(t => assignedMember(t)?.g.id === group.id && t.status !== "cancelled");
    const task = groupTasks.find(t => (t.display_status || t.status) === "running") || groupTasks.find(t => t.status !== "done") || groupTasks.at(-1);
    card.append(node("p", "wb-team-status", task ? workbenchStatus(task) + " · " + task.title : "待命 · 暂无分配任务"));
    const native = group.members.filter(m => m.relationship === "native_subagent").length;
    if (native) card.append(node("small", "muted", `${native} 名原生子 Agent 待接入`));
    card.append(workbenchButton("管理成员 →", () => {
      setOfficeScene(true); $("office-scene-layout").scrollIntoView({behavior:"smooth", block:"start"});
    }));
    teams.append(card);
  }
  if (!state.config.groups.length) teams.append(node("p", "muted", "还没有小组，可在下方新建或选择已有小组。"));
  renderTaskFocus();
}
function renderTaskFocus() {
  const {meeting, tasks} = workbenchContext();
  const panel = $("task-focus"); panel.hidden = !tasks.length;
  if (!tasks.length) return;
  panel.replaceChildren();
  panel.append(node("p", "muted", meeting ? `第 ${meeting.iteration || 1} 次迭代 · ${meetingStageLabels[meeting.stage] || meeting.stage}` : "独立任务"),
    node("h2", "", meeting?.title || "任务流程"), progressElement(workbenchTaskProgress(tasks)));
  const list = node("ol", "wb-timeline");
  for (const task of tasks) {
    const item = node("li", "wb-step " + (task.display_status || task.status));
    const member = assignedMember(task);
    const heading = node("div", "card-heading");
    heading.append(node("h3", "", task.title), node("span", "badge " + (task.display_status || task.status), workbenchStatus(task)));
    item.append(heading, node("p", "muted", member ? `${member.g.software} · ${member.m.name}` : "未绑定成员"));
    if (task.depends_on?.length) item.append(node("p", "muted", "前置步骤：" + task.depends_on.map(id => state.tasks.find(t => t.task_id === id)?.title || id).join("、")));
    const actions = node("div", "actions");
    if (member) actions.append(workbenchButton("成员对话", () => openMemberChat(member.g.id, member.m.id)));
    actions.append(workbenchButton("执行记录", () => openTaskInspect(task)));
    if (["ready", "running", "paused"].includes(task.status) && !task.waiting_user) {
      const op = task.status === "paused" ? "resume" : "pause";
      actions.append(workbenchButton(op === "pause" ? "暂停" : "继续", () => action(async () => {
        const result = await controlTask(task, op); await refresh();
        notice(result.stopping ? "任务已暂停，执行进程正在退出。" : "任务状态已更新。");
      })));
      actions.append(workbenchButton("成员交接", () => openTransfer(task)));
    }
    item.append(actions); list.append(item);
  }
  panel.append(list);
}
function setOfficeScene(visible) {
  $("office-scene-layout").hidden = !visible;
  $("workbench-teams").hidden = visible;
  $("team-view-button").setAttribute("aria-pressed", String(!visible));
  $("office-view-button").setAttribute("aria-pressed", String(visible));
}
$("team-view-button").onclick = () => setOfficeScene(false);
$("office-view-button").onclick = () => setOfficeScene(true);
$("nav-projects").onclick = () => openProjects();

for (const id of ["task-meeting-filter", "task-iteration-filter"]) {
  $(id).addEventListener("change", () => {
    state.focusedMeeting = $("task-iteration-filter").value || state.meetings.find(m =>
      (m.root_id || m.id) === $("task-meeting-filter").value)?.id || null;
    renderTaskFocus();
  });
}

for (const [id, view] of [["member-conversation-tab", "conversation"], ["member-activity-tab", "activity"]]) {
  $(id).onclick = () => {
    if (!state.chat) return;
    state.chat.view = view;
    state.chat.signature = null;
    $("member-conversation-tab").setAttribute("aria-selected", String(view === "conversation"));
    $("member-activity-tab").setAttribute("aria-selected", String(view === "activity"));
    $("chat-messages").setAttribute("aria-labelledby", id);
    renderChatMessages(state.chat);
  };
  $(id).onkeydown = event => {
    if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const next = (event.key === "Home" || (event.key !== "End" && view === "activity")) ? "member-conversation-tab" : "member-activity-tab";
      $(next).click(); $(next).focus();
    }
  };
}

function meetingGoal(text) {
  const details = node("details", "meeting-goal");
  details.append(node("summary", "", "目标与验收标准"), node("p", "", text));
  return details;
}

for (const id of ["capability-type", "capability-software"]) $(id).onchange = renderSkills;
$("capability-query").oninput = renderSkills;
function mcpEntry(software) {return (state.skills.mcp || []).find(g => g.software === software);}
function capabilityChecks(target, entries, selected, key, change) {
  target.replaceChildren();
  const known = new Set(entries.map(e => e[key]));
  const items = [...entries, ...selected.filter(n => !known.has(n)).map(n => ({[key]:n, name:n, missing:true}))];
  for (const item of items) {
    const row = node("label"), box = node("input");box.type = "checkbox";
    box.checked = selected.includes(item[key]); box.disabled = item.selectable === false;
    box.onchange = () => change(box.checked ? [...selected.filter(n => n !== item[key]),item[key]] : selected.filter(n => n !== item[key]));
    row.append(box,node("span", "", item.name + (item.missing ? "（已缺失，请取消选择）" : item.selectable === false ? "（由软件管理）" : "")));
    target.append(row);
  }
  if (!items.length) target.append(node("p", "muted", "没有发现可选条目。"));
}
function renderMemberMcp(software) {
  const entry = mcpEntry(software), mode = $("member-mcp-mode");
  mode.value = state.memberMcp === null ? "inherit" : "selected";
  mode.options[1].disabled = !entry?.selection_supported;
  mode.disabled = !entry?.selection_supported && state.memberMcp === null;
  $("member-mcp-help").textContent = !state.skills.capabilities_version ? "请重新扫描；如仍不可用，请重启服务以加载 MCP 能力目录。" : entry?.selection_supported ?
    "只控制本地配置的 MCP；不修改配置文件。插件和组织托管工具仍由软件管理；连接状态以实际执行为准。" : "已发现的 MCP 可在能力中心查看。当前适配器只支持沿用该软件配置，不承诺单次开关生效。";
  $("member-mcp-list").hidden = state.memberMcp === null;
  if (state.memberMcp !== null) capabilityChecks($("member-mcp-list"), entry?.servers || [], state.memberMcp, "id", value => {state.memberMcp=value;renderMemberMcp(software);});
}
$("member-mcp-mode").onchange = event => {
  state.memberMcp = event.target.value === "inherit" ? null : [];
  renderMemberMcp(state.config.groups.find(g => g.id === state.edit?.groupId)?.software);
};
function meetingCapabilityMembers() {
  const ids = new Set([...state.order, $("meeting-leader").value].filter(Boolean));
  for (const group of state.config.groups) if (group.members.some(m => ids.has(m.id))) {
    const leader=group.members.find(m => m.relationship === "leader"); if (leader) ids.add(leader.id);
  }
  return people().filter(({m}) => ids.has(m.id));
}
function meetingCapabilityOverrides() {
  const valid = new Set(meetingCapabilityMembers().map(({m}) => m.id));
  return Object.fromEntries(Object.entries(state.meetingCapabilities).filter(([id]) => valid.has(id)));
}
function renderMeetingCapabilities() {
  if (!state.meetingCapabilitiesLoaded) return;
  const roster=meetingCapabilityMembers();
  const signature=JSON.stringify([state.project,roster,state.skills,state.meetingCapabilities]);
  if (state.meetingCapabilitySignature === signature) return;
  state.meetingCapabilitySignature=signature;
  const target=$("meeting-capabilities-list");target.replaceChildren();
  for (const {g,m} of roster) {
    const card=node("div","meeting-capability-member"), active=state.meetingCapabilities[m.id];
    const label=node("label"), enabled=node("input");enabled.type="checkbox"; enabled.checked=!!active;
    enabled.disabled=!state.skills.capabilities_version;
    enabled.onchange=() => {
      if(enabled.checked) state.meetingCapabilities[m.id]={skills:[...(m.skills || [])],mcp_servers:m.mcp_servers == null ? null : [...m.mcp_servers]};
      else delete state.meetingCapabilities[m.id];
      renderMeetingCapabilities();
    };
    label.append(enabled,node("strong","",g.software+" · "+m.name+" · 本次单独配置"));card.append(label);
    if(active) {
      card.append(node("p","muted","Skill：未选表示本次不注入技能。"));
      const skillList=node("div","capability-picker");
      capabilityChecks(skillList,softwareSkills(g.software),active.skills || [],"name",value => {active.skills=value;renderMeetingCapabilities();});card.append(skillList);
      const entry=mcpEntry(g.software), mode=node("select");mode.setAttribute("aria-label",m.name+" 的本次 MCP 使用方式");
      mode.append(new Option("MCP：沿用软件配置","inherit"),new Option("MCP：选择配置中的服务","selected"));
      mode.value=active.mcp_servers === null ? "inherit" : "selected";mode.options[1].disabled=!entry?.selection_supported;
      mode.onchange=() => {active.mcp_servers=mode.value === "inherit" ? null : [];renderMeetingCapabilities();};card.append(mode);
      if(active.mcp_servers !== null) {
        const list=node("div","capability-picker");capabilityChecks(list,entry?.servers || [],active.mcp_servers,"id",value => {active.mcp_servers=value;renderMeetingCapabilities();});card.append(list);
      }
      if (!entry?.selection_supported) card.append(node("p","muted","MCP 开关由该软件管理，本次沿用其配置。"));
    } else card.append(node("p","muted",state.skills.capabilities_version ? "沿用成员默认 Skill / MCP 配置" : "需要重启服务以使用会议能力覆盖"));
    target.append(card);
  }
  if (!roster.length) target.append(node("p","muted","先选择主导者或参会成员。"));
}
$("meeting-capabilities-load").onclick = async () => {state.meetingCapabilitiesLoaded=true;await loadSkills();renderMeetingCapabilities();};
$("meeting-leader").addEventListener("change",renderMeetingCapabilities);
