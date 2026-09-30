# Chainlit 对话式前端使用说明

本目录新增了一套基于 [Chainlit](https://chainlit.io) 的对话式前端，让非技术同事可以在浏览器里跑 AIDD 任务、看候选结构、看模型决策过程，而不需要使用命令行。

`agent_dashboard.py`（控制台）和 `agent_task.py`（命令行）仍然保留并可独立使用，本前端是**第三条入口**。

---

## 安装

```powershell
pip install -r requirements.txt
```

`requirements.txt` 末尾新增了可选的 `chainlit>=1.0`。如果不需要 Chainlit 前端，正常安装即可，不会失败。

> 注：项目同时依赖 `fastapi` / `uvicorn`，Chainlit 用的是它们。安装后 Chainlit 自带的 `literalai` 遥测默认关闭（见 `.chainlit/config.toml`）。

---

## 启动

```powershell
chainlit run chainlit_app.py --host 127.0.0.1 --port 8000
```

打开浏览器访问 **http://127.0.0.1:8000**。

服务只绑定本机回环地址（`127.0.0.1`），不会暴露到公网。如果同事在另一台机器上访问，需要你做反向代理或 SSH 端口转发（**不要**直接绑定 `0.0.0.0`）。

启动命令说明：
- `--host 127.0.0.1`：仅本机访问
- `--port 8000`：默认 8000；不要与现有 `agent_dashboard.py` 的 `8765` 冲突
- 加 `-w` 监听文件改动（开发 Chainlit 时方便）

---

## 可用命令

在聊天框输入以 `/` 开头的命令，或普通文本消息。

| 命令 | 用途 |
|---|---|
| `/tasks` | 列出 `runs/` 下所有任务及其状态 |
| `/switch <任务ID>` | 切换到指定任务（前 8 位即可） |
| `/start <目标>` | 创建离线演示任务（确定性 mock，不调模型） |
| `/start! <目标>` | 创建真实任务（需要 MiniMax 或 DeepSeek 密钥） |
| `/status` | 查看当前任务：目标、预算、Top 5 候选 |
| `/pause` | 请求在下一个检查点暂停 |
| `/cancel` | 请求取消任务 |
| `/candidates [n]` | 列出 Top N 候选（默认 5），含 RDKit 2D 结构图 |
| `/candidate <id>` | 查看单个候选的属性归因和父子结构对比 |
| `/help` | 命令列表 |
| 其他文本 | 作为新的需求（steer）发送给当前任务 |

---

## 典型使用流程

**1. 第一次进来**

```
用户: /tasks
AIDD: 列出当前所有任务（首次进来一般是空的）
```

**2. 创建一个新任务**

```
用户: /start 设计针对 EGFR 的口服候选，目标是 logP<3、TPSA>75
AIDD: 已创建离线演示任务 `abc12345`。发送任意文本开始执行。

用户: 开始执行
AIDD: [展示 round 1 的步骤卡片，包含动作依据、证据、候选 SMILES 与 2D 结构]
     任务还在运行。要继续吗？[再跑 5 步] [暂停] [取消任务]

用户: [点 再跑 5 步]
AIDD: [继续展示后续步骤，每 3 步询问一次]
```

**3. 切换任务**

```
用户: /tasks
AIDD: - abc12345 已暂停 · 步数 6/20 · 模拟 · 目标：设计针对 EGFR 的口服候选 ...
     - def67890 已完成 · 步数 18/20 · 真实 · 目标：先评估已有候选 ...

用户: /switch def67890
AIDD: 已切换到任务 def67890。
```

**4. 中途改方向**

```
用户: 先不要生成新分子，把现有候选按属性分排序
AIDD: [steer 指令已注入任务，模型下一步决策时能看见]
```

**5. 看候选结构**

```
用户: /candidates 3
AIDD: [每条候选一行 + 一张 RDKit 渲染的 2D 分子图]
```

---

## 安全性说明

- 服务只绑定 `127.0.0.1`，外网不可达
- `.chainlit/config.toml` 关闭了 Chainlit 的 LiteralAI 遥测
- 模型响应、检查点状态不会上传到 Chainlit 自带的数据库（项目使用本地 JSON checkpoint）
- 不要把 `--host` 改成 `0.0.0.0`；同事远程访问请走 SSH 隧道：
  ```powershell
  ssh -L 8000:127.0.0.1:8000 your-server
  ```

---

## 与现有 dashboard / CLI 的关系

| 入口 | 适用人群 | 端口 | 数据 |
|---|---|---|---|
| `agent_dashboard.py` | 需要看完整时间线、控制台交互 | 8765 | JSON checkpoint |
| `agent_task.py`（CLI） | 自动化、批处理、定时任务 | - | JSON checkpoint |
| `chainlit run chainlit_app.py` | 非技术同事、临时探索、对话式控制 | 8000 | JSON checkpoint |

三个入口读写**同一个** `runs/<task_dir>/task.json`，可以混用：在 Chainlit 里 `/pause`，然后到 CLI 用 `python agent_task.py status --task-dir runs/xxx` 查看；再用 `/resume`（Chainlit 内部按 `instruction` 恢复）。

---

## 故障排查

**Q: 启动报 `ModuleNotFoundError: chainlit`**

A: `pip install chainlit>=1.0` 或 `pip install -r requirements.txt`。

**Q: 任务创建后无法切换/恢复**

A: 检查 `runs/` 目录下任务的 `task.json` 是否存在；权限是否可读。

**Q: 真实模型 (`/start!`) 报 401**

A: `.env` 里缺 `MINIMAX_API_KEY` 或 `DEEPSEEK_API_KEY`。注意 `chainlit run` 启动的进程要从项目根目录运行，否则 `.env` 加载不到。

**Q: 浏览器看不到分子图**

A: RDKit SVG 在所有现代浏览器都能直接显示。如果只显示纯文本，检查 `agents/harness/editor.py` 的 `molecule_svg_data_url` 是否报错（通常是无效 SMILES）。

**Q: 服务被同事误关，任务状态会不会丢**

A: 不会。每个决策都写入 `task.json`（原子写入）。服务重启后 `/switch` 选中任务即可继续。
