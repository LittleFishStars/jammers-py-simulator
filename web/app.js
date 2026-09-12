// 无线电干扰源环境模拟器 - 本地演练版控制台逻辑
"use strict";

const $ = (id) => document.getElementById(id);
let state = null;

async function api(path, body) {
  const opt = body === undefined
    ? { headers: { Accept: "application/json" } }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  return r.json();
}

function fmtSec(s) {
  if (s === null || s === undefined) return "—";
  const v = Math.max(0, Math.round(s));
  const h = Math.floor(v / 3600), m = Math.floor((v % 3600) / 60), sec = v % 60;
  const mm = String(m).padStart(2, "0"), ss = String(sec).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// 官方 action → 中文标签
function actionLabel(a) {
  return { enter: "进入", measure: "测量", clear: "清除", exit: "退出" }[a] || (a ? "未知指令" : "--");
}

// 官方 outcome → 中文标签
function outcomeLabel(e) {
  if (!e.accepted) return e.diagnostic ? `${e.http_status} ${e.diagnostic}` : `${e.http_status} 未接受`;
  switch (e.outcome) {
    case "entered": return "进入成功";
    case "no_signal": return "未检测到信号";
    case "near": return "距离过近";
    case "direction": return e.has_bearing ? `示向度 ${(e.bearing_hundredths / 100).toFixed(2)}°` : "检测到方向";
    case "success": return "清除成功";
    case "no_target_in_range": return "范围内无目标";
    case "user_exit": return "正常退出";
    default: return e.outcome || "已接受";
  }
}

function renderState() {
  if (!state) return;
  $("stState").textContent = state.state_label || state.state;
  $("stProblem").textContent = state.problem_no ? `问题${state.problem_no}` : "—";
  $("stCase").textContent = state.case_code || "—";
  $("stRun").textContent = state.run_no ? `第 ${state.run_no} 次` : "—";
  $("stPrepare").textContent = fmtSec(state.prepare_remaining_s);
  $("stWindow").textContent = fmtSec(state.window_remaining_s);
  $("stProgram").textContent = state.state === "running" ? fmtSec(state.program_remaining_s) : "—";
  $("stReason").textContent = state.end_reason || "—";

  const active = state.state === "preparing" || state.state === "window_open" || state.state === "running";
  $("btnAbort").disabled = !active;
  $("btnClear").disabled = state.state !== "finished";
  $("btnStart3").disabled = active;
  $("btnStart4").disabled = active;

  if (state.engine) {
    const e = state.engine;
    $("stVTime").textContent = `${(+e.virtual_time_s).toFixed(3)} s`;
    $("stMeas").textContent = e.measure_accepted_count;
    $("stCleared").textContent = e.cleared_jammer_count;
    $("stCFail").textContent = e.clear_failure_count;
    $("stSwitch").textContent = e.channel_switch_count;
    $("stEntered").textContent = e.entered ? "是" : "否";
    $("stJammerN").textContent = e.jammer_count;
    renderJammers(e.jammers || []);
  } else {
    $("stJammerN").textContent = "0";
    $("jammerBody").innerHTML = "";
  }
  // 定向源计数
  const jammers = (state.engine && state.engine.jammers) || [];
  $("stOmniN").textContent = jammers.filter((j) => j.kind === "omni").length;
  $("stDirN").textContent = jammers.filter((j) => j.kind === "directional").length;

  if (state.config && state.state === "idle") syncConfigInputs(state.config);
  if (state.log_tail) renderLog(state.log_tail);
}

function syncConfigInputs(cfg) {
  const r = cfg.rules || {};
  $("cfgCntMin").value = r.jammer_count_min ?? 10;
  $("cfgCntMax").value = r.jammer_count_max ?? 16;
  $("cfgRecvMin").value = r.receive_radius_min_m ?? 1000;
  $("cfgRecvMax").value = r.receive_radius_max_m ?? 1500;
  $("cfgClear").value = r.clear_radius_m ?? 20;
  $("cfgNear").value = r.near_distance_m ?? 5;
  $("cfgBeam").value = r.directional_beam_width_deg ?? 180;
  $("cfgBearErr").value = r.bearing_error_max_deg ?? 1;
  $("cfgNoiseGrid").value = r.bearing_noise_grid_m ?? 150;
  $("cfgArena").value = r.arena_radius_m ?? 1800;
  $("cfgDiskMargin").value = r.generation_disk_margin_m ?? 30;
  $("cfgSpeed").value = r.move_speed_m_per_s ?? 5;
  $("cfgMeasDur").value = r.measure_duration_s ?? 5;
  $("cfgClearNT").value = r.clear_no_target_duration_s ?? 3;
  $("cfgClearOK").value = r.clear_success_duration_s ?? 5;
  $("cfgSwitchDur").value = r.channel_switch_duration_s ?? 1;
  $("cfgRobotPort").value = cfg.robot_port;
  $("cfgWindow").value = cfg.window_seconds;
  $("cfgCountdown").value = cfg.countdown_seconds;
  $("cfgTeam").value = cfg.team_no;
}

function renderJammers(list) {
  const body = $("jammerBody");
  body.innerHTML = list.map((j) => `
    <tr>
      <td>${j.channel}</td>
      <td class="${j.kind === "omni" ? "omni" : "dir"}">${j.kind === "omni" ? "全向" : "定向"}</td>
      <td>(${(+j.position.x).toFixed(1)}, ${(+j.position.y).toFixed(1)})</td>
      <td>${(+j.receive).toFixed(0)}</td>
      <td>${j.direction === null || j.direction === undefined ? "—" : j.direction + "°"}</td>
      <td>${j.cleared ? "已清除" : "存在"}</td>
    </tr>`).join("");
}

function renderLog(rows) {
  const box = $("logBox");
  const html = rows.map((r) => {
    const isRobot = r.type === "robot";
    const t = new Date(r.real_timestamp_ms).toLocaleTimeString("zh-CN", { hour12: false });
    if (isRobot) {
      const pos = r.has_position ? `(${r.x.toFixed(1)}, ${r.y.toFixed(1)})` : "--";
      const ch = r.has_channel ? r.channel : "--";
      return `<div class="robot">[${t}] #${r.seq} ${actionLabel(r.action)} pos=${pos} ch=${ch} ⇒ ${esc(outcomeLabel(r))} (虚时 ${(r.virtual_time_us / 1e6).toFixed(1)}s)</div>`;
    }
    return `<div class="lifecycle">[${t}] #${r.seq} ${esc(r.event)} ${esc(r.detail || "")}</div>`;
  }).join("");
  box.innerHTML = html;
  box.scrollTop = box.scrollHeight;
}

function renderHistory(rows) {
  const body = $("histBody");
  if (!rows || !rows.length) { body.innerHTML = `<tr><td colspan="8">暂无记录</td></tr>`; return; }
  body.innerHTML = rows.map((r) => `
    <tr>
      <td>${new Date(r.created_at_ms).toLocaleString("zh-CN", { hour12: false })}</td>
      <td>问题${r.problem_no}</td>
      <td>第 ${r.practice_run_no} 次</td>
      <td>${esc(r.end_reason)}</td>
      <td>${r.measure_accepted_count}</td>
      <td>${r.cleared_jammer_count}</td>
      <td>${(r.virtual_time_us / 1e6).toFixed(1)}s</td>
      <td>${r.channel_switch_count}</td>
    </tr>`).join("");
}

async function refresh() {
  try {
    state = await api("/api/state");
    $("connDot").className = "dot ok";
    $("connText").textContent = "已连接";
    renderState();
  } catch (e) {
    $("connDot").className = "dot bad";
    $("connText").textContent = "控制台不可达";
  }
  try {
    const h = await api("/api/history");
    if (h.rows) renderHistory(h.rows);
  } catch (e) { /* 忽略 */ }
}

async function doStart(problemNo) {
  const ok = await api("/api/start", { problem_no: problemNo });
  if (!ok.ok) alert(ok.error || ok.message || "启动失败");
  refresh();
}

function scenarioInputs() {
  return {
    jammer_count_min: +$("cfgCntMin").value,
    jammer_count_max: +$("cfgCntMax").value,
    receive_radius_min_m: +$("cfgRecvMin").value,
    receive_radius_max_m: +$("cfgRecvMax").value,
    clear_radius_m: +$("cfgClear").value,
    near_distance_m: +$("cfgNear").value,
    directional_beam_width_deg: +$("cfgBeam").value,
    bearing_error_max_deg: +$("cfgBearErr").value,
    bearing_noise_grid_m: +$("cfgNoiseGrid").value,
    arena_radius_m: +$("cfgArena").value,
    generation_disk_margin_m: +$("cfgDiskMargin").value,
    move_speed_m_per_s: +$("cfgSpeed").value,
    measure_duration_s: +$("cfgMeasDur").value,
    clear_no_target_duration_s: +$("cfgClearNT").value,
    clear_success_duration_s: +$("cfgClearOK").value,
    channel_switch_duration_s: +$("cfgSwitchDur").value,
  };
}

async function regen(problemNo) {
  const res = await api("/api/scenario", { ...scenarioInputs(), problem_no: problemNo });
  if (res.ok && res.scenario) {
    const omni = res.scenario.jammers.filter((j) => j.kind === "omni").length;
    const dir = res.scenario.jammer_count - omni;
    alert(`已生成问题${problemNo}场景：${res.scenario.jammer_count} 个干扰源（全向 ${omni} / 定向 ${dir}）`);
  } else {
    alert(res.error || "生成失败");
  }
  refresh();
}

async function saveCfg() {
  const res = await api("/api/config", {
    robot_port: +$("cfgRobotPort").value,
    window_seconds: +$("cfgWindow").value,
    countdown_seconds: +$("cfgCountdown").value,
    team_no: $("cfgTeam").value,
    rules: scenarioInputs(),
  });
  $("cfgHint").textContent = res.ok
    ? (res.note || "已保存") + (res.changed && res.changed.length ? "（变更: " + res.changed.join(", ") + "）" : "")
    : res.error || "保存失败";
}

// 事件绑定
$("btnStart3").addEventListener("click", () => doStart(3));
$("btnStart4").addEventListener("click", () => doStart(4));
$("btnAbort").addEventListener("click", async () => {
  if (!confirm("确定要中止本次测试吗？")) return;
  await api("/api/abort", {});
  refresh();
});
$("btnClear").addEventListener("click", async () => { await api("/api/clear", {}); refresh(); });
$("btnGen").addEventListener("click", () => regen(3));
$("btnGenDir").addEventListener("click", () => regen(4));
$("btnSaveCfg").addEventListener("click", saveCfg);
$("btnHistClear").addEventListener("click", async () => {
  if (!confirm("确定清空全部历史统计？")) return;
  await api("/api/history/clear", {});
  refresh();
});

refresh();
setInterval(refresh, 1000);