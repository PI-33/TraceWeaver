# 项目背景说明：Trajectory-Guided Harness Optimization

## 1. 项目背景

本项目关注 Agent Harness Engineering，研究如何利用 Agent 的执行轨迹，分析当前 Harness 中影响任务完成的关键机制，并指导 Harness 的自动优化。

现有 Agent 的能力不仅取决于底层模型，还受到外围 Harness 的影响。Harness 包含 Prompt、Workflow、Tools、Memory、Context Management、Hooks 等机制，这些机制共同决定 Agent 如何理解任务、组织执行流程、调用工具、管理上下文以及完成最终交付。

近期研究已经开始探索 Harness Evolution，即利用 Agent 的执行反馈，自动提出和实施 Harness 修改。

其中，RRSI（Regularized Recursive Self-Improvement of Agent Harnesses）通过对候选修改生成和选择过程进行正则化，减少 Harness Evolution 的过拟合问题，提高优化结果的泛化能力。

但是，本项目关注的是 Harness Evolution 中另一个问题：

**当 Agent 执行任务出现失败或低效行为时，如何更准确地理解失败原因，并找到真正值得优化的 Harness 机制？**

## 2. 研究问题

现有 Harness Evolution 可以根据执行轨迹提出 Harness 修改，但从执行行为到具体 Harness 优化需求之间，仍然存在值得研究的空间。

例如：

- Agent 在某一步发生错误，真正影响这个行为的 Harness 组件是什么？
- 一个组件的执行异常，是否可能由其他上游组件引起？
- 当前 Harness 是否缺少任务所需的某种机制？
- 已有机制是否存在实现缺陷或行为不合理？
- Harness 的组件依赖关系能否帮助缩小优化范围？
- 如何将多条执行轨迹产生的优化需求整合起来，形成一致的 Harness 修改？

本项目希望解决：

**利用 Agent 执行轨迹和 Harness 结构依赖关系，分析影响任务完成的关键行为，识别当前 Harness 值得优化的机制，并指导针对性的 Harness 修改。**

研究重点不是简单地构建 Harness 架构图，也不是让 Agent 根据失败结果直接生成代码，而是建立执行轨迹、Harness 行为和实际实现之间的关联。

## 3. 核心研究思路

整个方法分为三个阶段。
1.2 核心方法思路
整个方法可以概括为：
\[
\text{Harness Modeling}
\rightarrow
\text{Trajectory Analysis}
\rightarrow
\text{Proposal Integration}
\]

其中：
- Harness Modeling：理解当前 Harness 的结构、行为和依赖关系。
- Trajectory Analysis：分析单条执行轨迹，结合 Harness Model 产生针对性的修改提议。
- Proposal Integration：汇总整个 Evo Set 的修改提议，整合并统一实施。

### Stage 1：Harness Modeling

目标：理解当前 Harness 的结构、行为和组件依赖关系，建立 Harness Model。

输入：当前 Harness 源代码。

建模思路：

**（1）事实层**

通过 AST、静态分析和代码规则提取确定性的结构信息，例如：

- 文件及目录结构；
- 关键类和方法；
- Import、Call 等调用关系；
- 配置引用；
- 资源读取；
- 文件之间的静态依赖。

事实层主要保证结构信息来自实际代码，而不是模型推测。

**（2）语义层**

通过 LLM 阅读和分析代码，理解各个文件及方法在 Harness 中承担的职责。

重点关注：

- 文件或方法实际控制什么行为；
- 接收什么输入；
- 产生什么输出；
- 如何影响 Agent 的执行行为；
- 与其他组件之间存在什么行为依赖。

**（3）Graph 关系**

目前初步考虑三类关系：

- `contains`：Harness、Component、File 之间的归属关系。
- `depends_on`：文件或方法之间的静态实现依赖。
- `influences`：一个组件或实现对其他执行行为的影响。

建模粒度初步采用：

Harness → Component → File → Method。

其中，文件是主要的结构建模单元，关键方法用于后续细粒度定位。

**最终产物：Harness Model。**

Harness Model 只描述当前真实存在的 Harness 结构、职责和依赖关系，不提前判断应该增加什么机制，也不包含未来的修改计划。

代码证据可以作为建模过程中的中间信息，但不需要作为 Harness Model 的冗余输出字段。

### Stage 2：Trajectory Analysis & Modification Proposal

目标：分析单条执行轨迹，结合 Harness Model，找到值得优化的机制，并生成针对性的修改提议。

输入：

1. Task Specification：原始任务要求、目标和约束。
2. Execution Trajectory：Agent 完整执行轨迹。
3. Execution Outcome：最终结果、Verifier 反馈、错误日志和交付物。
4. Harness Model：Stage 1 生成的结构和行为模型。

采用一个统一的 Trajectory Analysis Agent，通过 Prompt 引导其自主探索和分析。

Harness Model 是 Stage 2 的主要 Harness 输入。只有 Model 缺少或存在歧义的具体
事实会改变 Proposal 判断时，Agent 才能说明信息缺口并申请受控、只读的源码兜底；
源码不在初始上下文和工具列表中，也不能取代 Harness Model 成为主要分析材料。

四个分析方向：

**（1）Outcome Analysis**

理解任务预期结果与实际结果之间的差异，判断任务未达到预期的原因。

不能预设所有失败都来自 Harness，也需要考虑模型、工具和环境因素。

**（2）Behavioral Chain Analysis**

从执行轨迹中还原实际行为：

- Agent 执行了什么 Action；
- 获得了什么 Observation；
- 哪些步骤出现异常；
- 异常如何影响后续执行。

需要区分实际观察到的事实和模型推断的原因，不能仅凭时间先后关系认定因果。

**（3）Required Behavior Extraction**

根据任务要求和实际执行行为，推导 Agent 为了完成任务应该具备什么行为。

例如：

实际行为：测试失败后，Agent 重复修改代码，没有充分分析错误信息。

所需行为：测试失败后，应先分析错误原因，再决定下一步修改。

这一环节只确定所需行为，不提前限定由哪个 Harness 组件实现。

**（4）Harness Analysis & Modification Proposal**

结合 Harness Model，探索哪些组件能够控制相关行为，理解它们之间的依赖关系，并
判断当前实现与所需行为之间的差距。仅当 Model 缺少会改变判断的关键信息时，才申请
源码兜底核实该项信息。

利用 Harness 结构依赖缩小相关范围，最终提出有针对性的 Harness 修改需求。

四个分析方向是 Agent 的思考指导，而不是必须依次执行的固定工作流。Agent 可以根据分析过程中发现的信息自主探索、回溯和调整判断。

Harness Model 是 Stage 2 理解结构、行为和依赖的主要依据；受控源码只补足明确的
Model 信息缺口。

**最终产物：单条轨迹对应的 Modification Proposal。**

Proposal 尽量精简，只保留后续阶段真正需要的信息，例如：

- Target Component；
- Mechanism；
- Modification Reason；
- Modification Requirement；
- Expected Behavior。

轨迹证据、代码证据、依赖路径等可以作为 Agent 的内部分析依据，不需要作为最终 Proposal 的独立输出字段。

如果没有充分依据认为 Harness 存在优化机会，允许 Agent 输出简短结论及原因，不强行产生修改提议。

### Stage 3：Proposal Integration & Execution

目标：整合整个 Evo Set 中的修改提议，形成统一的 Harness 优化方案，并完成实际代码修改。

该阶段分为两个职责部分。

**（1）程序汇总**

每条轨迹独立产生 Modification Proposal。

程序负责汇总整个 Evo Set 的所有提议，进行必要的数据整理。

例如，相同文件或组件可能收到来自不同轨迹的修改需求，需要统一组织起来。

**（2）Agent 整合与实施**

启动一个统一的 Modification Agent，输入：

- 当前 Harness 源代码；
- 整个 Evo Set 汇总后的 Modification Proposals。

Agent 负责：

- 理解全部修改提议；
- 合并重复或互补的修改需求；
- 分析不同提议之间的冲突；
- 探索实际 Harness 源码；
- 确定具体实现方式；
- 形成综合修改方案；
- 按照综合方案统一执行代码修改。

Stage 3 不需要再次输入完整 Harness Model，因为它的核心任务是基于真实源码确定具体实现方式，而不是重复进行结构建模。


**Stage 3 是唯一真正修改 Harness 源代码的阶段。**

## 4. 当前研究中的重要设计原则

以下原则是前期讨论中已经明确的设计约束，希望在后续技术方案和代码实现中得到保留。

### 4.1 Harness Model 只反映当前现状

Harness Model 负责描述当前实际存在的 Harness，不负责预测未来需要什么机制。

某个机制是否需要增加、修改或删除，应由 Stage 2 根据任务要求、执行轨迹和当前 Harness Model 自主分析得出。

不要在 Harness Model 中提前维护能力缺口或未来机制规划。

### 4.2 证据用于分析，不必作为最终输出

Stage 1 和 Stage 2 可以利用代码证据、轨迹证据和依赖路径进行深入分析。

但最终 Harness Model 和 Modification Proposal 应尽量精简，只保留后续阶段真正需要的信息。

### 4.3 结构关系必须服务于定位

Harness Graph 不是为了完整描述整个代码库。

它的价值在于帮助 Stage 2 理解组件之间的结构依赖和行为影响，从执行轨迹中的异常行为找到相关 Harness 组件。

不能仅仅因为两个组件存在调用关系，就认定它们都存在问题。

### 4.4 不预设 Harness 优化方式

Agent 应根据任务和轨迹分析结果，自主判断应该如何优化 Harness。

不要在 Prompt 或流程中预先限定具体的修改策略。

### 4.5 分析与实施分离

Stage 1、Stage 2 不修改代码。

Stage 2 每条轨迹独立产生修改提议。

Stage 3 统一整合整个 Evo Set 的修改需求，并由唯一的实施 Agent 修改代码。

### 4.6 保持方法和工程实现简洁

避免过度结构化、过多中间状态和不必要的 Agent 拆分。

优先复用现有能力和实现。

Graph 应该解决真实的定位问题，而不是为了建图而建图。

## 5. 当前研究基础与参考材料

目前已经讨论并提供了以下真实研究材料：

**（1）RRSI 论文**

Regularized Recursive Self-Improvement of Agent Harnesses。

用于理解已有 Harness Evolution 方法、Proposal 和 Selection 机制，以及 Harness 自动优化过程中的泛化问题。

**（2）Terminal-Bench 2.1 轨迹数据集**


包含 Agent 在 Terminal-Bench 2.1 上的执行轨迹及相关结果信息，可用于分析真实 Agent 行为。

**（3）Terminus-2 源码**



## 6. 当前项目所处阶段

目前已经形成三个阶段的方法雏形，但尚未完成具体技术设计和工程实现。

下一步将由项目负责人提供 Stage 1、Stage 2、Stage 3 的具体实施方案。

后续开发工作应以实施方案为准，将方法设计转化为实际代码实现。

在收到具体实施方案之前：

- 不要自行扩展研究范围；
- 不要提前设计复杂的 Graph Schema；
- 不要自行增加额外的 Agent 或工作流阶段；
- 不要把尚未确定的算法和数据结构当成既定要求；
- 不要直接开始修改 Terminus-2 或开发完整框架。

首先需要充分理解上述研究背景、目标、方法边界和设计原则。

**本说明用于建立项目背景和统一开发认知，不是具体的 Coding Specification。具体开发任务、模块设计、接口、数据结构、算法及验收要求，将在后续实施方案中明确。**