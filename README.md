# 无线电干扰源环境模拟器，本地演练版

基于官方《模拟器使用说明》和《模拟器通信接口说明及编程指南》，也就是附件1、附件2，再叠上对官方
Windows 客户端 `jammers-simulator.exe` 的逆向分析，用 Python 标准库重写的本地演练模拟器。不装
WebView2，不引第三方依赖，也不连竞赛服务器。机器狗那套协议已经跟官方文档逐项对过。

> 仅供本地演练与技术学习。正式测试的认证、加密和行为日志校验都靠官方服务器端验签，本程序不参与正式赛程。

## 与官方一致的关键行为

机器狗接口默认听 `http://127.0.0.1:2026`，空闲时端口可以改。

一共 4 条指令：`POST /enter`、`/measure`、`/clear`、`/exit`。移动和切换频道不用单独下指令，由参数自动推断出来。
请求字段是 `arena_id="default"`、`robot_id=<参赛队号>`、`request_id`，`/measure` 和 `/clear` 另带
`position:{x,y}` 与 `channel`，坐标单位米。

响应固定含 `accepted`/`real_timestamp_ms`/`virtual_time_s` 三个字段。`/enter` 再追加
`max_virtual_duration_s`/`max_real_duration_s`/`remaining_real_duration_s`；`/measure` 追加
`measure_result`，取值 no_signal/near/direction，命中方向时还有 `svd_deg`；`/clear` 追加 `clear_result`，
取值 success/no_target_in_range；`/exit` 追加 `exit_reason`。

HTTP 状态码的语义：200 也可能带 accepted=false 的拒绝，另有 400、404、405、409 表示幂等冲突或并发、413、415、429。
接口没开放或测试已结束时，服务器直接关连接，不回 JSON。

物理规则对齐官方：目标区域半径 1800m；干扰源 10 到 16 个，频道 1 到 20 且互不重复；有效接收半径
1000 到 1500m；移动 5 m/s；检测 5s；清除未发现 3s、成功 5s；切换频道 1s；近距阈值 5m；清除半径 20m。
定向覆盖角全角 180°、半角 90°，边界也算在内。虚拟限时 360000s，现实限时 1200s，窗口 25 分钟，倒计时 5s。

虚拟时钟就是 int64 微秒整数，跟官方同款。移动耗时按 `int64(1e12×距离m/速度µm·s⁻¹)` 算，向零截断到微秒。

响应体是手工拼装的 JSON 文本，官方 renderResult 也是这么干的。`virtual_time_s` 是整数时输出 `105`，不写成
`105.0`；`svd_deg` 恒定两位小数，`270.00` 这种；`max_*_duration_s` 都是整数。

示向度误差是 ±1° 的确定性空间噪声，不是逐次随机。误差等于 150m 网格上的平滑值噪声，只由场景噪声种子、
本次测量的频道、机器狗的测量位置这三样决定。同一个位置同一频道重复测，误差一模一样，取平均压不下去。
落在同一个 150m 网格里的点误差同向、强相关，相距越远越独立。

演练场景还带两条硬约束：问题3 纯全向，一个定向干扰源都不许有；问题4 至少有 1 个定向。官方对应的报错是
`problem 3 cannot contain directional jammers` 和 `problem 4 requires at least one directional jammer`。

请求体不超过 65536 字节，UTF-8 且不许带 BOM，不许有重复键，嵌套最多 16 层。
`request_id` 是幂等的：相同内容重放返回首次那份响应，同 ID 换内容返回 409。

## 快速开始

```bash
cd jammers-py
python3 run.py                 # 一键启动，浏览器打开 http://127.0.0.1:8080
python3 run.py --demo          # 固定演示场景：问题4，10 个干扰源，含 1 个定向
python3 run.py --window 60 --countdown 3   # 缩短窗口便于测试
python3 -m simulator           # 等价入口
```

启动后在 Web 控制台点“开始演练 · 问题3/4”，倒计时走完，默认 5s，机器狗接口才开放。

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
├── test_api.py             # 端到端自测，59 项协议对齐验证
├── web/                    # 控制台前端，纯 HTML/CSS/JS，中文
├── simulator/
│   ├── _entry.py           # CLI 入口
│   ├── config.py           # 配置 + 物理规则，取官方默认值
│   ├── scenario.py         # 场景模型 + 生成器，10–16 个干扰源
│   ├── engine.py           # 模拟引擎：enter/measure/clear 判定 + 虚拟时钟
│   ├── bearingnoise.py     # 示向度空间噪声，官方 bearingnoise 包的忠实移植
│   ├── render.py           # 响应体渲染，官方 renderResult 同款手工拼装 JSON
│   ├── session.py          # 会话状态机 + 请求校验/幂等
│   ├── robot_api.py        # 机器狗 HTTP 接口 2026，负责 HTTP 层校验
│   ├── webui.py            # 控制台 HTTP 服务 + REST API
│   └── store.py            # SQLite 统计 + 行为日志落盘
└── data/                   # 运行态数据，首次启动自动创建，不入库
```

## 自测

```bash
cd jammers-py
python3 test_api.py
```

预期输出 59 项 PASS，覆盖：接口开放时机、enter 五字段、measure 三结果、clear 两结果、exit、幂等、未知字段、
robot_id、坐标与频道校验、415、405、404、重复键、统计落库、中止、场景生成、问题3 与问题4 的定向约束、
示向度噪声的确定性、值域与去相关、位置面积均匀分布。

## 已从二进制还原的关键数值

附件2 里有两处数值在 docx 提取时丢了，原文档那一块是公式或图片，所以只能从官方二进制里挖回来。

一是定向覆盖角和示向度误差：`directional_beam_width_udeg = 180°`，半角 90°；`bearing_error_max_udeg = ±1°`；
`bearing_noise_grid_um = 150m`。本地默认值跟它们一致，配置文件和 Web 控制台都能改。二是登录与联网：官方
每次运行都要联网登录、校验服务器时间，本地版没有服务器，改用 `robot_id` 是否等于配置里的 `team_no`
作为校验基准。
