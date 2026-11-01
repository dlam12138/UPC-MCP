"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  let current = null;
  let csrf = "";
  let busy = false;
  const checks = new Map();
  const initials = {shiguang: "光", timetable: "课", library: "阅", service_portal: "办"};
  const labels = {
    stored: ["已保存会话", "saved"], unverified: ["待校验", "pending"],
    auth_expired: ["需要重新连接", "pending"], absent: ["尚未连接", "neutral"],
    needs_connection: ["查询时自动连接", "pending"], needs_account: ["请配置账号", "neutral"],
    credential_error: ["账号状态异常", "error"], local_error: ["本地记录异常", "error"],
  };

  function notify(text, error = false) {
    $("message").textContent = text;
    $("message").classList.toggle("error", error);
    $("message").hidden = !text;
  }

  async function api(path, data, extra = {}) {
    const options = {credentials: "same-origin", cache: "no-store", headers: {...extra}};
    if (data !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      if (csrf) options.headers["X-UPC-Settings"] = csrf;
      options.body = JSON.stringify(data);
    }
    let response;
    try { response = await fetch(path, options); }
    catch { throw new Error("本地设置服务已停止，请重新打开设置页面。"); }
    let result;
    try { result = await response.json(); }
    catch { throw new Error("本地设置响应无效，请重新打开设置页面。"); }
    if (!response.ok) throw new Error(result.message || "操作未完成，请重试。");
    return result;
  }

  function timeLabel(value) {
    if (!value) return "尚无校验记录";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "尚无校验记录";
    return "最近校验 " + new Intl.DateTimeFormat("zh-CN", {
      month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
    }).format(date);
  }

  function renderServices() {
    $("service-list").replaceChildren();
    for (const system of current.systems) {
      const row = document.createElement("div"); row.className = "service-row";
      const top = document.createElement("div"); top.className = "service-top";
      const icon = document.createElement("span"); icon.className = "service-initial";
      icon.textContent = initials[system.id]; icon.setAttribute("aria-hidden", "true");
      const name = document.createElement("span"); name.className = "service-name";
      name.textContent = system.name;
      const check = checks.get(system.id);
      const label = check?.state === "ok" ? ["检查通过", "success"]
        : check?.state === "error" ? ["检查未通过", "error"]
        : labels[system.state] || ["状态未知", "neutral"];
      const pill = document.createElement("span"); pill.className = "pill " + label[1];
      pill.textContent = label[0];
      top.append(icon, name, pill);
      const bottom = document.createElement("div"); bottom.className = "service-bottom";
      const time = document.createElement("span"); time.className = "service-time";
      time.textContent = check?.time ? "刚刚完成在线检查" : timeLabel(system.last_verified_at);
      const button = document.createElement("button"); button.type = "button";
      button.className = "check-button";
      button.textContent = check?.state === "pending" ? "检查中…" : "检查连接 ↗";
      button.disabled = busy || !current.credential.configured || check?.state === "pending";
      button.setAttribute("aria-label", "检查" + system.name + "连接");
      button.addEventListener("click", () => checkConnection(system.id));
      bottom.append(time, button); row.append(top, bottom);
      if (check?.message && check.state === "error") {
        const message = document.createElement("p"); message.className = "check-message error";
        message.textContent = check.message; row.append(message);
      }
      $("service-list").append(row);
    }
  }

  function render() {
    const configured = current.credential.configured;
    $("credential-state").textContent = current.credential.error ? "状态异常" : configured ? "已配置" : "未配置";
    $("credential-state").className = "pill " + (current.credential.error ? "error" : configured ? "saved" : "neutral");
    $("account-description").textContent = configured
      ? "账号已保存在本机。如需更换，请重新填写账号和密码。"
      : "保存后，查询时会自动建立各服务的登录会话。";
    $("save-account").textContent = busy ? "正在保存…" : configured ? "更新账号 ↗" : "保存账号 ↗";
    $("save-account").disabled = busy || Boolean(current.credential.error);
    $("clear-account").disabled = busy || !configured;
    for (const id of ["username", "password", "password-confirm"]) $(id).disabled = busy;
    $("credential-form").setAttribute("aria-busy", String(busy));
    $("mcp-config").textContent = JSON.stringify(current.mcp_config, null, 2);
    $("copy-config").disabled = false;
    $("state-directory").textContent = current.state_directory;
    renderServices();
  }

  async function refresh() {
    const next = await api("/api/status");
    if (current && current.credential.generation !== next.credential.generation) checks.clear();
    csrf = next.csrf;
    current = next;
    render();
  }

  async function checkConnection(system) {
    const generation = current.credential.generation;
    checks.set(system, {state: "pending"}); renderServices();
    try {
      await api("/api/check", {system});
      if (current.credential.generation === generation) checks.set(system, {state: "ok", time: true});
    } catch (error) { checks.set(system, {state: "error", message: error.message}); }
    try { await refresh(); }
    catch (error) { notify(error.message, true); }
  }

  $("credential-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || !current) return;
    $("form-error").hidden = true;
    if ($("password").value !== $("password-confirm").value) {
      $("form-error").textContent = "两次密码输入不一致，请检查。";
      $("form-error").hidden = false;
      $("password-confirm").focus(); return;
    }
    busy = true;
    const data = {username: $("username").value.trim(), password: $("password").value,
      password_confirm: $("password-confirm").value, replace: current.credential.configured,
      expected_generation: current.credential.generation};
    render(); notify("");
    try {
      const result = await api("/api/credentials", data);
      $("credential-form").reset(); checks.clear();
      notify(result.message);
      await refresh();
    } catch (error) { notify(error.message, true); }
    finally {
      $("password").value = ""; $("password-confirm").value = "";
      data.password = ""; data.password_confirm = "";
      busy = false; render();
    }
  });

  $("toggle-password").addEventListener("click", () => {
    const show = $("password").type === "password";
    for (const id of ["password", "password-confirm"]) $(id).type = show ? "text" : "password";
    $("toggle-password").textContent = show ? "隐藏" : "显示";
    $("toggle-password").setAttribute("aria-pressed", String(show));
    $("toggle-password").setAttribute("aria-label", show ? "隐藏密码" : "显示密码");
  });

  $("clear-account").addEventListener("click", () => $("clear-dialog").showModal());
  $("cancel-clear").addEventListener("click", () => $("clear-dialog").close());
  $("confirm-clear").addEventListener("click", async () => {
    $("confirm-clear").disabled = true;
    try {
      const result = await api("/api/credentials/clear", {expected_generation: current.credential.generation});
      $("credential-form").reset(); checks.clear(); $("clear-dialog").close();
      notify(result.message); await refresh();
    } catch (error) { $("clear-dialog").close(); notify(error.message, true); }
    finally { $("confirm-clear").disabled = false; }
  });

  $("refresh-status").addEventListener("click", async () => {
    $("refresh-status").disabled = true;
    try { await refresh(); } catch (error) { notify(error.message, true); }
    finally { $("refresh-status").disabled = false; }
  });

  $("copy-config").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(current.mcp_config, null, 2));
      $("copy-config").textContent = "已复制 ✓";
      setTimeout(() => { $("copy-config").textContent = "复制 MCP 配置 ⧉"; }, 2000);
    } catch {
      const selection = window.getSelection(); const range = document.createRange();
      range.selectNodeContents($("mcp-config")); selection.removeAllRanges(); selection.addRange(range);
      notify("配置已选中，可按 Ctrl+C 复制。");
    }
  });

  for (const link of document.querySelectorAll(".nav-link")) link.addEventListener("click", () => {
    document.querySelectorAll(".nav-link").forEach((item) => item.classList.toggle("active", item === link));
  });

  async function closeSettings() {
    try {
      await api("/api/close", {});
      $("credential-form").reset();
      document.querySelectorAll("button,input").forEach((element) => { element.disabled = true; });
      notify("本次设置已结束，可以关闭此窗口。MCP 查询服务不受影响。");
      $("message").scrollIntoView({block: "center"});
    } catch (error) { notify(error.message, true); }
  }
  $("close-settings").addEventListener("click", closeSettings);
  $("close-settings-mobile").addEventListener("click", closeSettings);

  async function start() {
    let access = new URLSearchParams(location.hash.slice(1)).get("access");
    if (access) {
      history.replaceState(null, "", location.pathname);
      try { await api("/api/bootstrap", {}, {"X-UPC-Launch": access}); }
      finally { access = null; }
    }
    await refresh();
    document.documentElement.dataset.ready = "true";
  }
  start().catch((error) => {
    notify(error.message, true);
    $("credential-state").textContent = "未解锁";
    $("credential-state").className = "pill pending";
    $("service-list").textContent = "请在本机重新运行设置入口，获取本次访问授权。";
  });
})();
