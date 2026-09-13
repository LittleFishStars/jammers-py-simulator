# 无线电干扰源环境模拟器（本地演练版 · Python）

基于官方《模拟器使用说明》与《模拟器通信接口说明及编程指南》（附件1/附件2）以及官方 Windows 客户端 `jammers-simulator.exe` 的逆向分析，用 **Python 标准库**重写的本地演练模拟器。无需 WebView2、无第三方依赖、无需连接竞赛服务器。机器狗协议已与官方文档逐项对齐。

> 仅供本地演练与技术学习。正式测试的认证、加密行为日志校验依赖官方服务器端验签，本程序不参与正式赛程。

## 与官方一致的关键行为

- 机器狗接口默认 `http://127.0.0.1:2026`（可在空闲时修改端口）
- 4 条指令：`POST /enter`、`/measure`、`/clear`、`/exit`；移动与切换频道由参数自动推断，无独立指令
- 请求字段：`arena_id="default"`、`robot_id=<参赛队号>`、`request_id`；`/measure`、`/clear` 另带 `position:{x,y}`（米）与 `channel`
- 响应：`accepted`/`real_timestamp_ms`/`virtual_time_s` 三字段；`/enter` 追加 `max_virtual_duration_s`/`max_real_duration_s`/`remaining_real_duration_s`；`/measure` 追加 `measure_result`（no_signal/near/direction）与 `svd_deg`；`/clear` 追加 `clear_result`（success/no_target_in_range）；`/exit` 追加 `exit_reason`
- HTTP 状态码语义：200（含 accepted=false 拒绝）、400、404、405、409（幂等冲突/并发）、413、415、429
- 接口未开放/已结束：直接关闭连接，无 JSON 响应
- 物理规则：目标区域半径 1800m；干扰源 10–16 个、频道 1–20 唯一；接收半径 1000–1500m；移动 5 m/s；检测 5s；清除未发现 3s/成功 5s；切换频道 1s；近距阈值 5m；清除半径 20m；定向覆盖角 **全角 180°（半角 90°，含边界）**；虚拟限时 360000s；现实限时 1200s；窗口 25 分钟；倒计时 5s
- 虚拟时钟为 **int64 微秒整数**（官方同款），移动耗时按 `int64(1e12×距离m/速度µm·s⁻¹)` 向零截断到微秒
- 响应体为**手工拼装的 JSON 文本**（官方 renderResult 同款）：`virtual_time_s` 整数时输出 `105`（非 `105.0`）、`svd_deg` 恒两位小数（`270.00`）、`max_*_duration_s` 为整数
- 示向度误差为 **±1° 的确定性空间噪声**（非逐次随机）：误差 = 150m 网格上的平滑值噪声，只由「场景噪声种子 + 本次测量频道 + 机器狗测量位置」决定。**同一位置同一频道重复测量误差完全相同，取平均无法减小误差**；同一 150m 网格内误差同向、强相关，相距越远越独立
- **问题3 演练纯全向（0 个定向干扰源）；问题4 演练至少 1 个定向干扰源**（对齐官方 `problem 3 cannot contain directional jammers` / `problem 4 requires at least one directional jammer`）
- 请求体 ≤65536 字节、无 BOM UTF-8、无重复键、嵌套 ≤16 层
- `request_id` 幂等：相同内容重放返回首次响应，相同 ID 不同内容返回 409

## 快速开始

```bash
cd jammers-py
python3 run.py                 # 一键启动，浏览器打开 http://127.0.0.1:8080
python3 run.py --demo          # 固定演示场景（问题4：10 干扰源，含 1 个定向）
python3 run.py --window 60 --countdown 3   # 缩短窗口便于测试
python3 -m simulator           # 等价入口
```

启动后，在 Web 控制台点击“开始演练 · 问题3/4”，倒计时结束（默认 5s）后机器狗接口开放。

## 机器狗程序接入示例

```python
import json
from urllib.request import Request, urlopen

BASE_URL = "http://127.0.0.1:2026"
ROBOT_ID = "<参赛队号>"

def post(path, payload):
    request = Request(BASE_URL + path, data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=5) as http_response:
        return json.loads(http_response.read().decode("utf-8"))

def base(request_id):
    return {"arena_id": "default", "robot_id": ROBOT_ID, "request_id": request_id}

enter = post("/enter", base("enter-1"))
print("本局可用现实时间：", enter["remaining_real_duration_s"], "秒")

resp = post("/measure", {**base("m1"), "position": {"x": 300, "y": 400}, "channel": 1})
if resp["measure_result"] == "direction":
    print("示向度：", resp["svd_deg"], "度")
elif resp["measure_result"] == "near":
    print("距离过近，没有示向度")
else:
    print("未测得信号")

resp = post("/clear", {**base("c1"), "position": {"x": 300, "y": 0}, "channel": 3})
print("清除结果：", resp["clear_result"])

resp = post("/exit", base("exit-1"))
print("退出原因：", resp["exit_reason"])
```

## 目录结构

```
jammers-py/
├── run.py                  # 一键启动
├── test_api.py             # 端到端自测（59 项协议对齐验证）
├── web/                    # 控制台前端（纯 HTML/CSS/JS，中文）
├── simulator/
│   ├── _entry.py           # CLI 入口
│   ├── config.py           # 配置 + 物理规则（官方默认值）
│   ├── scenario.py         # 场景模型 + 生成器（10–16 干扰源）
│   ├── engine.py           # 模拟引擎（enter/measure/clear 判定 + 虚拟时钟）
│   ├── bearingnoise.py     # 示向度空间噪声（官方 bearingnoise 包忠实移植）
│   ├── render.py           # 响应体渲染（官方 renderResult 同款手工拼装 JSON）
│   ├── session.py          # 会话状态机 + 请求校验/幂等
│   ├── robot_api.py        # 机器狗 HTTP 接口 2026（HTTP 层校验）
│   ├── webui.py            # 控制台 HTTP 服务 + REST API
│   └── store.py            # SQLite 统计 + 行为日志落盘
└── data/                   # 运行态数据（首次启动自动创建，不入库）
```

## 自测

```bash
cd jammers-py
python3 test_api.py
```

预期输出：59 项 PASS（接口开放时机/enter 五字段/measure 三结果/clear 两结果/exit/幂等/未知字段/robot_id/坐标频道校验/415/405/404/重复键/统计落库/中止/场景生成/问题3、4 定向约束/示向度噪声确定性、值域、去相关/位置面积均匀分布）。

## 已从二进制还原的关键数值（文档公式丢失处）

1. **定向覆盖角与示向度误差**：附件2中这两处数值在 docx 提取时丢失（原文档为公式/图片），现已从官方二进制完整还原——`directional_beam_width_udeg = 180°`（半角 90°）、`bearing_error_max_udeg = ±1°`、`bearing_noise_grid_um = 150m`，本地默认值与之一致，并可在配置或 Web 控制台调整。
2. **登录/联网**：官方每次运行需联网登录、校验服务器时间；本地版无服务器，以 `robot_id` 等于配置中的 `team_no` 作为校验基准。
