# Trajectory-Guided Harness Optimization
# 方法框架与设计决策

## 一、整体方法框架

### 1.1 研究目标

利用 Agent 的执行轨迹和 Harness 结构依赖关系，分析影响任务完成的关键行为，识别当前 Harness 值得优化的机制，并将整个 Evo Set 中的优化需求整合为统一的 Harness 修改。

核心思路：

1. 对当前 Harness 进行结构和行为建模，建立 Harness Model。
2. 分析单条执行轨迹，结合 Harness Model，推导所需行为并提出针对性的 Harness 修改建议。
3. 汇总整个 Evo Set 的修改提议，由 Agent 整合综合方案并统一实施。

### 1.2 整体流程

整个方法分为三个阶段：

| 阶段 | 名称 | 核心职责 | 输出 |
|---|---|---|---|
| Stage 1 | Harness Modeling | 理解当前 Harness 的结构、行为及依赖关系 | Harness Model |
| Stage 2 | Trajectory Analysis & Modification Proposal | 分析单条轨迹，结合 Harness Model 产生修改提议 | Modification Proposal |
| Stage 3 | Proposal Integration & Execution | 汇总 Evo Set 提议，整合方案并统一实施 | Modified Harness |

---

## 二、Stage 1：Harness Modeling

### 2.1 目标

对当前 Harness 进行结构和行为建模，理解 Harness 由哪些组件组成、各部分实际承担什么职责，以及组件之间存在什么依赖和影响关系。

这一阶段只描述当前 Harness 的真实情况，不负责分析应该增加什么机制，也不提前判断哪些地方需要修改。

### 2.2 信息建模

采用事实层和语义层两类信息。

**事实层（Fact Layer）**

通过 AST、静态分析和代码规则提取确定性信息。

主要包括：

- 文件及目录结构；
- 类、方法等代码结构；
- Import、Call 等调用关系；
- 配置引用；
- 资源读取；
- 文件之间的静态依赖。

事实层负责反映实际代码结构，避免将模型推测当作代码事实。

**语义层（Semantic Layer）**

通过 LLM 阅读和分析源码，理解文件及方法在 Harness 中实际承担的职责。

主要关注：

- 文件或方法负责什么；
- 控制什么执行行为；
- 接收什么输入；
- 产生什么输出；
- 如何影响其他组件或 Agent 的执行行为。

语义层需要基于实际源码进行分析，而不是仅根据文件名称或函数名称推测职责。

两层相互补充：事实层提供结构依据，语义层提供行为理解。

**建模 Agent 的执行方式**

Stage 1 使用一个对 Harness 源码只读的建模 Agent。Agent 首先调用 `parse_source`，工具只提取文件、方法位置和保守的静态调用线索，保存为 `work/source_index.json`；它不推断返回值传播或共享状态语义。Method State 再按字符预算从该索引动态生成批次，批次不作为独立工作文件持久化。

每轮模型只返回一个 JSON action。控制器把 action 作为白名单协议数据，显式分派到 `list_files`、`read`、`find_references`、`edit_file` 等可信工具；模型文本不会作为 Python 方法名动态执行。下一轮只自动带入最近查询结果、集中式 Method 摘要和 `work/harness_model.json` 的结构摘要，完整值通过 `read(scope=work, pointer=...)` 按需取得，避免大草稿反复占据上下文。Agent 可以据此沿入口、主循环、模型与工具调用、状态、错误恢复和退出路径自主回查。

整个过程始终由一个 Harness Modeling Agent 完成。System Prompt 的总体任务、工具协议和输出 schema 保持统一，控制器只根据持久化 `State` 替换 `{{STAGE_GUIDE}}`。五个 State 依次是：`源码解析` 产生 Source Index；`Method` 将索引中的全部方法分批交给 Agent，在单一 `method_analysis.json` 中记录 keep/skip 和行为说明；`Component` 根据 keep 方法划分组件；`harness` 写整体描述；`关系` 写代表性 Edges。`advance_stage` 在当前产出通过检查后前进，`back_stage` 可返回上一分析方向；无论当前 State 为何，Agent 都可以继续读取源码和修正已有产物。

每个索引方法都必须经过一次判断，但分析深度不同：能够解释执行、观测、记录或退出差异的关键方法标记 keep，并写自足说明；纯转发、格式化和未接入实现标记 skip，只写具体用途和省略理由。`record_methods` 一次提交当前整批，只有 `method_analysis.json` 覆盖全部索引方法后才能进入 Component。控制器不恢复逐页笔记、selection/handoff、组件关系证据文件或影响边精简工作流。

例如，Agent 发现解析器会返回完成标记后，可以追到主循环，核实第一次标记与待确认状态的两个分支，再把解析职责、主循环职责和有行为意义的影响写入草稿。`done` 只是发布申请：控制器检查组件和方法确实存在、描述非占位、关系端点有效，并自动重建 `contains`；它不替 Agent 判断语义关系是否正确。校验通过后发布 `harness_model.json` 并清理临时索引、草稿和动作记录，失败原因则作为下一轮结果返回。

### 2.3 节点粒度

初步采用以下层级：

```text
Harness
  └── Component
        └── File
              └── Method
```

各层级的定位：

- Harness：整体执行系统。
- Component：逻辑功能组件。
- File：源码文件，是主要的结构建模单元。
- Method：具体行为的实现位置，用于后续细粒度定位。

不要求将所有函数都独立建成 Graph 节点，重点关注与 Harness 执行行为相关的关键方法。

### 2.4 关系设计

目前考虑三类关系：

| 关系 | 含义 | 主要信息来源 |
|---|---|---|
| `contains` | Harness、Component、File 之间的归属关系 | 目录、配置和代码结构 |
| `depends_on` | 文件或方法之间的静态实现依赖 | Import、Call、配置引用、资源读取 |
| `influences` | 一个组件或实现对其他执行行为的影响 | 代码语义、调用关系和信息传递分析 |

其中，`influences` 是连接 Stage 1 和 Stage 2 的重要关系。

例如：

```text
TmuxSession
     |
     v
获取终端输出
     |
     v
Terminus2 Observation
     |
     v
下一轮 LLM 输入
```

需要注意：

- `depends_on` 描述代码层面的实现依赖。
- `influences` 描述行为层面的影响关系。

两者不能简单等同。

### 2.5 最终产物：Harness Model

当前阶段先确认 Stage 1 的输出内容。

Harness Model 主要保留：

- 当前组件及文件结构；
- 关键方法及其职责；
- 组件之间的依赖关系；
- 行为之间的影响关系；
- 文件和方法的行为描述。

Harness Model 只反映当前实际存在的 Harness，不负责记录未来需要增加的机制或修改规划。

代码证据、行号及关系分析过程可以在建模过程中使用，但不需要作为正式输出中的独立字段。

### 2.6 已确定的设计决策

1. 采用事实层与语义层相结合的建模方式。
2. 文件作为主要建模单元，关键方法用于细粒度定位。
3. 保留 `contains`、`depends_on`、`influences` 三类关系。
4. Harness Model 以当前实际 Harness 为描述对象，不提前判断能力缺口。
5. 建模应服务于后续轨迹定位，而不是追求完整描述整个代码库。
6. 全量方法先做静态清点，再由 LLM 分批完成一次 keep/skip 判断；关键方法深描，辅助方法简述。
7. 控制器只验证结构事实和安全边界，不用启发式静态分析替代模型判断行为影响。

---

## 三、Stage 2：Trajectory Analysis & Modification Proposal

### 3.1 目标

从单条 Agent 执行轨迹出发，结合当前 Harness Model，理解任务执行过程中的关键行为，推导完成任务所需的行为，并提出针对性的 Harness 修改建议。

这是整个方法的核心研究阶段。

### 3.2 输入

每次处理一条轨迹。

| 输入 | 内容 |
|---|---|
| Task Specification | 原始任务要求、目标和约束 |
| Execution Trajectory | Agent 的完整实际执行过程 |
| Execution Outcome | 最终结果、Verifier 反馈、错误信息及交付物 |
| Harness Model | Stage 1 生成的当前 Harness 结构和行为模型 |

其中：

- Task Specification 用于理解任务预期。
- Execution Trajectory 用于还原实际执行行为。
- Execution Outcome 用于确认最终任务结果。
- Harness Model 通过只读查询提供。Stage 2 开始分析时先取得对应模型的简短概览，再按需展开组件和关系。它是 Stage 2 的主要 Harness 输入，不是等待源码复核的临时索引。
- Harness 源码不在初始上下文和工具列表中。只有 Agent 已查询 Model，仍有会改变 Proposal 的具体信息缺口时，才可说明已读信息、缺失事实及其影响，申请受控只读源码兜底；为了增加信心不能解锁源码。

预期的 Model 查询入口包括组件 ID、文件、限定符号名和轨迹反馈文本。例如，按 `TmuxSession.get_incremental_output` 查询时，模型应能给出该方法的源码位置、职责、上下游影响及生效条件。Stage 2 再结合当次轨迹判断相应分支是否触发；仅在这些信息不足以支持判断时申请源码兜底。

### 3.3 Agent 工作方式

采用一个统一的 Trajectory Analysis Agent，通过 Prompt 引导其自主探索和分析。

不将分析过程拆分为多个 Agent，也不将分析环节设置成必须严格依次执行的固定工作流。

以下四个环节是 Agent 的分析指导，Agent 可以根据分析过程中发现的信息自主探索、回溯和调整判断。

**① Outcome Analysis：结果分析**

理解任务预期结果与实际结果之间的差异。

不预设所有任务失败都来自 Harness。

**② Behavioral Chain Analysis：行为链分析**

需要区分实际观察到的事实和模型推断的原因，不能仅凭时间先后关系认定因果。

**③ Required Behavior Extraction：所需行为推导**

根据任务要求和实际执行行为之间的差距，推导 Agent 为了完成任务应该具备什么行为。

**④ Harness Analysis & Modification Proposal：Harness 分析与修改提议**

结合 Harness Model，探索当前 Harness 中与所需行为相关的组件和实现。


Harness Model 用于理解结构、行为和依赖。受控源码兜底只补足会改变 Proposal 的
明确 Model 信息缺口，不能替代 Harness Model 成为主要分析材料。

不要仅根据失败发生的位置判断应该修改哪个组件，也不要因为组件存在调用关系就认定它是故障原因。

Agent 应根据实际分析结果自主决定是否提出修改，以及提出什么样的修改。

### 3.4 最终产物：Modification Proposal

每条轨迹独立产生一份修改提议。

建议保留以下信息：

| 字段 | 含义 |
|---|---|
| Target Component | 涉及的 Harness 组件 |
| Mechanism | 涉及的机制 |
| Modification Reason | 为什么需要优化 |
| Modification Requirement | 具体需要实现什么 |
| Expected Behavior | 预期产生什么行为变化 |

不要求输出完整的行为因果链、轨迹证据、代码证据、依赖路径和中间推理过程。

这些信息可以作为 Agent 内部分析和判断的依据，但不作为最终 Proposal 的独立字段。

如果没有充分依据认为 Harness 存在值得优化的地方，允许 Agent 输出简短结论及原因，不强行产生修改提议。

### 3.5 已确定的设计决策

1. 每次处理单条轨迹，由一个 Agent 实例完成分析和提议。
2. 四个分析环节是思考指导，不是固定执行流水线。
3. Stage 2 默认只提供 Harness Model，不直接提供完整 Harness 源码。
4. Agent 可以自主探索 Harness Model；具体信息不足时可说明理由申请只读源码兜底。
5. 证据只用于中间分析，不要求作为最终输出。
6. 最终只保留后续阶段真正需要的 Modification Proposal。
7. 不预先限定具体修改方式，由 Agent 根据分析结果自主判断。
8. 不要求每条失败轨迹都产生 Harness 修改。

---

## 四、Stage 3：Proposal Integration & Execution

### 4.1 目标

汇总整个 Evo Set 中所有单条轨迹产生的 Modification Proposals，由 Agent 整合为统一的 Harness 优化方案，并完成实际代码修改。

该阶段采用程序汇总、Agent 综合判断与实施的方式。

### 4.2 程序汇总

程序负责收集整个 Evo Set 中产生的所有 Modification Proposals，并进行统一整理。

例如：

| 来源 | Target Component | Modification Requirement |
|---|---|---|
| Trace 1 | Workflow | 增加测试失败后的错误分析 |
| Trace 2 | Workflow | 增加工具执行失败后的恢复 |
| Trace 3 | Memory | 增加跨阶段信息复用 |
| Trace 4 | Workflow | 减少重复执行检查 |

程序只负责汇总和整理，不负责决定提议是否应该合并。

### 4.3 Agent 整合与实施

启动一个统一的 Modification Agent。

**输入：**

1. 当前 Harness 源代码。
2. 整个 Evo Set 汇总后的 Modification Proposals。

不再提供完整 Harness Model。

原因：

- Stage 2 已经使用 Harness Model 完成了结构理解和组件级分析。
- Stage 3 的主要任务是探索实际代码并确定实施方式。
- 实际源码是代码修改的最终依据。
- 避免重复输入抽象模型，增加不必要的上下文。

**Agent 的工作：**

1. 理解全部修改提议。
2. 识别重复、互补和冲突的需求。
3. 综合判断哪些修改应该保留和整合。
4. 探索实际 Harness 源码。
5. 确定具体修改文件和实现方式。
6. 形成综合修改方案。
7. 按照综合方案统一执行代码修改。

例如：

Stage 2 提出增加 Persistent Memory 机制。

Stage 3 探索实际源码后发现，`mechanisms.py` 已经定义了 Memory 原语，只是没有接入主执行流程。

那么 Agent 可以选择复用现有实现，将其接入主流程，而不是重新开发一套 Memory。

需要注意：Stage 2 负责提出需要什么机制，Stage 3 负责探索如何在当前代码中实现。

**Stage 3 是唯一真正修改 Harness 源代码的 Agent。**

### 4.4 最终产物

Modified Harness，即完成统一优化后的 Harness 源代码。

### 4.5 已确定的设计决策

1. 每条轨迹独立产生修改提议。
2. 程序负责汇总整个 Evo Set 的修改提议。
3. Agent 负责综合分析、整合和实施。
4. Stage 3 不需要完整 Harness Model。
5. Stage 3 以实际 Harness 源码为实施依据。
6. 允许复用当前 Harness 中已经存在的实现。
7. 避免简单拼接修改需求，必须考虑重复、互补和冲突。
8. 只有 Stage 3 可以实际修改 Harness 源代码。

---

## 五、三个阶段的整体输入输出

| 阶段 | 核心目标 | 输入 | 输出 |
|---|---|---|---|
| Stage 1：Harness Modeling | 理解当前 Harness 结构与行为 | Harness 源码 | Harness Model |
| Stage 2：Trajectory Analysis & Modification Proposal | 分析单条轨迹并提出优化需求 | Task、Trajectory、Outcome、Harness Model | Modification Proposal |
| Stage 3：Proposal Integration & Execution | 整合 Evo Set 提议并统一实施 | 当前 Harness 源码、汇总后的 Modification Proposals | Modified Harness |

## 六、全局设计原则与开发约束

以下内容是前期讨论中已经明确的设计决策，后续技术设计和代码实现应遵循这些原则。

### 6.1 职责边界

- Stage 1 只描述当前 Harness，不负责能力缺口分析。
- Stage 2 负责单条轨迹分析和修改提议，不修改代码。
- Stage 3 负责整个 Evo Set 的提议整合和代码实施。

### 6.2 信息与证据管理

- Harness Model 只保留后续定位需要的结构和语义信息。
- 轨迹证据和代码证据用于中间分析，不需要作为最终输出的独立字段。
- Modification Proposal 保持精简，只保留后续整合需要的信息。
- 不重复传递不必要的中间分析过程。

### 6.3 Agent 自主性

- Stage 2 允许 Agent 根据发现的信息自主探索、回溯和调整判断。
- 四个分析环节不是固定执行节点。
- 不预先限定具体 Harness 修改方式。
- 不强制每条轨迹都产生修改提议。

### 6.4 实现与研究方法分离

- Stage 1、Stage 2 负责理解、分析和提出优化需求。
- Stage 3 负责探索实际源码并确定实现方式。
- 不要求 Stage 2 提前确定具体代码修改位置。
- Stage 3 不受 Stage 2 对具体实现方式的预设限制。

### 6.5 保持设计简洁

- 不增加不必要的 Agent。
- 不增加不必要的中间节点、状态和数据字段。
- 优先复用已有 Harness 能力。
- 不为了构建 Graph 而构建 Graph，结构和依赖关系必须服务于轨迹分析与 Harness 优化。
- 不将尚未验证的算法和假设写成已经确定的设计。

## 七、尚待具体技术设计的内容

目前确定的是方法框架和职责边界，以下内容仍需要在后续实施方案中进一步明确：

1. Stage 1 中事实层的具体提取方式，以及语义层如何生成和组织行为描述。
2. `influences` 关系如何从真实 Harness 源码和执行链路中识别。
3. Harness Model 的具体数据结构及存储方式。
4. Stage 2 中依赖关系如何辅助 Agent 缩小定位范围。
5. Stage 2 的具体 Prompt、Model 查询与受控源码兜底方式及最终输出格式。
6. Stage 3 中提议汇总、冲突处理和统一修改的具体实现方式。
7. 三个阶段之间的实际代码接口和运行流程。

这些问题不代表可以自由扩展研究范围，而是后续技术设计需要解决的具体事项。

在没有明确实施方案之前，不应自行将某种算法、数据结构或架构选择视为既定要求。

---

## 八、给开发 Agent 的执行说明

本文件用于说明已经讨论确定的方法框架、各阶段职责及设计决策。

它不是完整的技术实施方案，也不是 Coding Specification。

后续将分别提供 Stage 1、Stage 2、Stage 3 的具体实施方案。开发时应以这些实施方案为直接依据，将研究方法转化为具体代码实现。

当前需要遵循以下要求：

- 充分理解三个阶段的职责及其关系。
- 保留已经确定的设计决策和约束。
- 不自行增加新的研究模块或 Agent。
- 不提前设计尚未确定的复杂算法和数据结构。
- 不根据自己的理解擅自改变方法框架。
- 对尚未明确的技术细节，等待具体实施方案，而不是自行发散。

**最终目标是将既定研究方法实现为一个简洁、可运行、可验证的 Harness Optimization 框架，而不是构建一个过度复杂的通用 Agent 平台。**
