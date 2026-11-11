# Stage 1 输出讨论记录

Stage 1 产出 `harness_model.json`，描述当前 Harness 如何组织一次任务执行。Stage 2 的 Agent 阅读它，是为了从轨迹中的一个行为找到相关组件和方法，再结合当次运行的证据判断原因。

讨论中尝试过按六类职责归组，也尝试过围绕每个 LLM 调用组织内容。最终采用的主线是**哪些实现共同控制了任务的推进**。模型请求与响应是其中一部分。动作是否真正执行、信息怎样进入后续回合、任务何时结束，以及轨迹记录能说明什么，也可能是 Stage 2 需要理解的行为。组件如何划分取决于这份 Harness 的实际实现。

## 敲定的 MVP 框架

```text
harness_model.json
├── schema_version
├── harness
├── nodes
│   └── Component
│       └── Method
│           ├── Description
│           └── file_path
└── edges
    ├── contains
    ├── depends_on
    └── influences
```

结构约定到这棵树为止。**key 用来组织内容，详细说明写在对应的 value 里，供 Agent 按语义阅读。** 下面用 Terminus-2 的同一条执行路径举例，从任务整体讲到“一批命令执行后，模型怎样看到终端输出”。示例中的组件名称是对这份源码的一种归纳。

## 1. harness

`harness` 让 Agent 快速理解整个 Harness。在一段概览中交代源码根目录，沿主要工作循环说明一次任务怎样启动、推进和结束，并点出主要组件在这个过程中怎样协作。

**Terminus-2 示例**

> 源码根目录是 `harness/harbor_terminus2/`。入口 `AgentHarness` 沿用 `Terminus2` 的执行流程。任务与回合控制组件建立终端会话，把任务说明与初始屏幕放入首轮提示。每到新回合，主循环先检查会话是否存活。回复协议组件把模型的 JSON 或 XML 回复解释为命令、完成标志和格式反馈。终端执行与观测组件将有效命令发往同一窗格，把批次输出交还主循环。普通未完成回合用这段反馈继续任务。首次完成声明触发确认，待确认时再次有效声明完成，循环结束。[任务入口](../harness/harbor_terminus2/__init__.py#L31) · [首轮提示](../harness/harbor_terminus2/terminus_2.py#L1573) · [完成确认](../harness/harbor_terminus2/terminus_2.py#L1545)

## 2. Component

由建模 Agent 阅读当前 Harness 的源码，自主确定组件名称和数量。Prompt、Tools、Memory、Workflow 等名称只有在源码中对应明确职责时才适用。划分的目标是用尽可能少、职责清楚的组件，概述主要功能及其协作方式，让 Stage 2 能从轨迹中的行为找到负责的实现。

1. **按职责归组。** 用一句话说清一组实现共同完成什么功能，据此决定组件名称。
2. **辨认行为边界。** 找出它接收什么信息、控制哪个动作或选择、产生什么结果，以及谁使用这个结果。
3. **沿执行流程核实。** 从主循环追到实际调用与返回，核对组件在运行中的作用。文件名和目录名是阅读线索。
4. **控制粒度。** 一个组件可以跨多个文件、包含多个方法。一起完成同一项功能的实现可以放在一组。
5. **区分不同职责。** 如果一段实现负责解释回复，另一段负责把按键送进终端，两者的输入、输出和行为就有明确的分界。

**组件示例 · 终端执行与观测**

> 这个组件接收已经解析好的命令批次，负责把按键依次送入持久的 tmux 窗格，并把读取到的终端状态交还主循环。批次调度位于 `terminus_2.py`，按键发送和取屏位于 `tmux_session.py`。把这几处实现放在同一组件下，是因为它们共同完成了“从命令批次到终端反馈”的过程。收到反馈后继续还是结束任务，由主循环决定。[批次调度](../harness/harbor_terminus2/terminus_2.py#L1213) · [按键发送](../harness/harbor_terminus2/tmux_session.py#L793) · [取屏](../harness/harbor_terminus2/tmux_session.py#L855)

## 3. Method

`Method` 选取组件中有代表性的实现，回答“哪个具体方法实现了这个行为”。`Description` 用一段话说明方法负责什么、接收什么参数或信息（Inputs）、执行什么操作、向调用方返回什么结果（Outputs）。如果某个分支会改变结果，就在这段描述中说明。`file_path` 给出相对于 Harness 源码根目录的文件路径，方便按方法名核实实现。

**方法示例 · `Terminus2._execute_commands`**

| 已约定的 key | 示例 value |
|---|---|
| `Description` | 接收一批 `Command` 和当前 `TmuxSession`，按顺序把每条命令的按键发往同一窗格，并使用该命令的时长作为最短等待时间。正常发完整批后，读取终端输出并限制其长度，返回 `(False, 终端文本)`。如果某次发送抛出 `TimeoutError`，方法在该处结束批次，返回 `(True, 超时反馈文本)`，其中包含命令、等待时长和当时的终端状态。 |
| `file_path` | [terminus_2.py](../harness/harbor_terminus2/terminus_2.py#L1213) |

## 4. Edges

`edges` 汇集模型中能够成立的关系。每条关系都要让 Agent 看出关系类型、起点和终点。下面用箭头表示方向，关系的具体含义直接写在相应的 value 中。

### 4.1 contains

说明归属，方向从上级对象指向成员。Harness 包含 Component，Component 包含实现其职责的 Method。

**归属示例**

> `Terminus-2 → 终端执行与观测` 表示这个组件属于 Terminus-2。
>
> `终端执行与观测 → Terminus2._execute_commands` 表示该方法承担组件中的批次调度职责。

### 4.2 depends_on

说明一处实现依赖谁才能完成自己的功能，方向从使用方指向被使用方。可以依据 import、方法调用、配置引用、资源读取，或其他可从源码确认的静态实现依赖。

**依赖示例**

> `Terminus2._execute_commands → TmuxSession.get_incremental_output`。执行器在命令批次发完后调用会话的取屏方法，取得它要交还主循环的终端文本。[调用位置](../harness/harbor_terminus2/terminus_2.py#L1243)

### 4.3 influences

说明上游组件或方法的结果如何改变下游行为，方向从影响来源指向受影响方。在 value 中写清什么条件下传递了什么信息，以及下游如何使用它。

**影响示例**

> `TmuxSession.get_incremental_output → Terminus2._execute_commands`。能提取非空增量时，取屏方法返回新增文本。首次读取或无法提取非空增量时，它返回当前可见屏幕。这个选择决定了执行器拿到哪段终端文本。[取屏分支](../harness/harbor_terminus2/tmux_session.py#L868)
>
> `Terminus2._execute_commands → Terminus2._run_agent_loop`。在普通未完成回合，主循环用执行器返回的批次文本构造 observation，必要时附上解析警告。循环把这条 observation 赋给下一轮的 prompt。沿这两条关系，Stage 2 就能从“模型看到了什么”追到“当时取回了哪段终端输出”。[构造 observation](../harness/harbor_terminus2/terminus_2.py#L1413) · [接续下一轮](../harness/harbor_terminus2/terminus_2.py#L1554)

## 运行 MVP

每次请求的 `system` 都包含统一的建模目标、输出 schema 和动作协议，并按当前持久化 State 替换 `{{STAGE_GUIDE}}`。五个 State 是 `源码解析 → Method → Component → harness → 关系`，始终由同一个 Agent 执行。首轮 context 提示 Agent 调用 `{"action":"parse_source"}`；loop 将这个白名单 action 显式分派到 `parse_source(harness_root, work_dir)`，解析成功后自动进入 Method。`advance_stage` 检查当前最低产出后前进，`back_stage` 返回上一分析方向；State 不限制源码读取和旧产物修正。[解析函数](../harness_model_loop.py)

每轮只执行一个动作。Method State 分批提供完整方法源码，以及包内导入、继承、导出和调用位置。Agent 用 `record_methods` 一次提交整批的 keep/skip 和 Description。每个索引方法均有判断后才能进入 Component。Component State 能看到跨批汇总的保留理由和省略理由，可以批量修正判断，再用 `record_components` 为所有保留方法分组。控制器按源码定位符填入已经审过的方法说明和相对路径。提交成员分工时，该短句也用于生成对应的 `contains` 描述。

关系 State 分批提供源码、调用位置、所在赋值和同一字段的读取位置。Agent 用 `record_relations` 为每个本批方法提交两张表。`depends_on` 的键是它使用的实现。`influenced_by` 的键是改变它行为的上游。控制器将后者按“上游 → 消费方”写入正式模型的 `influences`。两张表均可为空。若从结果产生处发现跨步骤的消费方，可用 `affects` 补充转交路径和下游变化。这部分随检查点保存，最后与消费方发现的影响合并去重，供发布时选择。索引也会标出从工厂返回的本地类推导出的候选分派，实际调用条件和行为影响由 Agent 阅读源码核实。需要更多材料时，仍可读取任一源码文件或用 JSON pointer 查看草稿。连续六轮只查询后，须先提交分析或切换 State。

可以在项目根目录直接打印某个 Harness 实际使用的完整系统提示词。

```sh
python3.12 -c 'from pathlib import Path; from harness_model_loop import system_prompt; print(system_prompt(Path("harness/harbor_terminus2").resolve()))'
```

从项目根目录运行，第二个位置参数指定本次使用的空运行目录。使用能解析目标源码的 Python 版本，Terminus-2 可用 Python 3.12。命令从环境变量读取兼容 Chat Completions 的模型服务配置 `OPENAI_BASE_URL`、`OPENAI_MODEL`，需要鉴权时再设置 `OPENAI_API_KEY`。也可在 Python 中给 `run_modeling` 注入自己的 `generate` 函数。

```sh
python3.12 harness_model_loop.py harness/harbor_terminus2 runs/terminus_new
```

如果本地开发副本中有 `tests/` 目录，可以运行单元测试。

```sh
python3.12 -m unittest discover -s tests -v
```

也可以使用已经登录的 Codex CLI。每次模型调用都在新的空目录里进行，模型只返回一个动作，由外层 loop 读取源码和写入工作区。程序优先使用 `CODEX_APP_RUNTIME_CLI_PATH` 指向的可执行文件，其次使用 `PATH` 中的 `codex`。不传 `--model` 时使用该 Codex 账户支持的默认模型。如果 CLI 报告已从 WebSocket 回退到 HTTPS 并成功返回动作，本次运行的后续调用沿用 HTTPS。传输选择只保存在当前进程。

```sh
python3.12 harness_model_loop.py harness/harbor_terminus2 runs/terminus_new --backend codex
```

运行期间有 `work/source_index.json`、`work/method_analysis.json` 和 `work/harness_model.json` 三项核心产物，当前 State 和交互历史保存在检查点。`edit_file` 只能修改后两个分析产物，源码和索引均只读。Component 必须且只能归组 Method State 标记 keep 的方法，所有 keep 方法都要被归组。`done` 只允许在关系 State 调用，并核对方法路径与关系端点有效，再根据组件成员自动重建 `contains`；通过后发布正式 JSON 并清理临时状态。

文件开头的 `MAX_TURNS = 50` 控制一次建模的累计轮次上限，解析工具的调用也计一轮。用满 50 轮仍未完成时，程序保存进度并返回 `max_turns`。

关系审阅进度随检查点保存。校正方法说明时，文本替换和 JSON pointer 两种编辑都只重新审阅受改方法和已有关系的相邻端点。改变组件归属会重新核对完整方法集。旧的 `edit_file` 仍可精确编辑关系。完成全图审阅后，Agent 看到两类边的编号和说明，按能否定位实际行为选择正式发布的关系，随 `done.selection` 提交。菜单版本绑定到当前草稿，发布失败可用相同选择重试。能在一个批次内审完的小模型，若每个方法已出现在手写的方法级关系中，也可直接提交 `done`。发布时校验方法归属、端点和审阅覆盖。语义质量在生成后与参考模型及源码核对。

验收时再加载参考答案。带有 `tests/compare_models.py` 的本地开发副本可用以下命令比较可定位的方法、共同归组的方法对以及有向边。语义质量还需要结合源码检查。

```sh
python3.12 tests/compare_models.py runs/terminus_new/harness_model.json docs/examples/harbor_terminus2.harness_model.reference.json
```
