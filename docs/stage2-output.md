# Stage 2 输出讨论记录

本文记录当前整理的 Stage 2 输出结构与字段含义，作为后续讨论 Stage 2 技术方案的依据。

## 一、输出结构

Stage 2 输出 `Modification Proposal`，建议使用 JSON：

```json
{
  "trajectory_id": "trace_001",
  "proposals": [
    {
      "expected_behavior": "...",
      "target": "...",
      "reason": "...",
      "requirement": "..."
    }
  ]
}
```

- `trajectory_id` 用于标识修改提议来自哪一条执行轨迹。
- `proposals` 是该轨迹产生的修改提议列表，允许一条轨迹产生多项修改提议。

如果没有充分依据认为 Harness 存在值得优化的地方，输出：

```json
{
  "trajectory_id": "trace_001",
  "proposals": [],
  "no_proposal_reason": "没有提出 Harness 修改的具体原因。"
}
```

`no_proposal_reason` 只在 `proposals` 为空时出现。模型判断、任务知识、工具或环境
更能解释结果，以及现有证据不足，都可以成为不提出修改的理由。模型服务异常、输入
损坏或轮次耗尽属于运行失败，不应伪装成无提议结论。

## 二、四个字段的内容说明

### 1. `expected_behavior`

**回答：Agent 应该具备什么行为？**

描述为了完成当前任务，Agent 在关键执行环节应该采取的正确行为。

内容要求：

- 从任务要求和实际执行轨迹中推导；
- 描述具体行为，而不是抽象能力；
- 不提前指定应该修改哪个 Harness 组件；
- 不包含代码实现方案。

例如：

> 当测试执行失败时，Agent 应先分析错误信息和失败位置，理解失败原因，再决定后续修改操作。

### 2. `target`

**回答：涉及 Harness 的什么组件或机制？**

描述本次优化涉及的逻辑组件或具体机制。

内容要求：

- 优先使用 Harness Model 中已有的 `Component`；
- 明确涉及组件的具体功能范围；
- 如果需要引入当前 Harness 不具备的新机制，可以直接提出机制名称；
- 不要求指定具体文件、函数或代码位置。

例如：

> Workflow / Test Failure Recovery

表示优化 Workflow 中的测试失败反馈与恢复机制。

### 3. `reason`

**回答：为什么当前 Harness 需要优化？**

解释当前 Harness 与所需行为之间存在什么差距。

内容要求：

- 结合实际执行表现和当前 Harness 的实现情况；
- 说明现有机制为什么不足；
- 不要仅仅重复最终失败结果；
- 不要把没有证据支持的模型错误归因于 Harness。

例如：

> 当前 Harness 缺少有效的测试失败反馈处理机制，Agent 在测试失败后容易直接重复执行相似的代码修改，而没有充分利用错误信息分析问题。

### 4. `requirement`

**回答：具体需要实现什么？**

描述希望 Harness 具备什么能力，或者现有机制需要发生什么变化。

内容要求：

- 尽量具体、可执行；
- 描述需要实现的行为或机制；
- 不指定具体代码实现方式；
- 不重复解释完整的失败原因。

例如：

> 完善测试失败后的反馈处理机制，使 Agent 在再次修改代码之前，先读取并分析测试错误信息，根据失败原因决定后续修复操作。

## 三、Trajectory Analysis Agent Loop

Stage 2 每次使用一个 Agent 实例分析一条轨迹。输入包括：

- Task Specification；
- Execution Trajectory；
- Execution Outcome；
- Stage 1 发布的 Harness Model。

Harness 源码不是 Stage 2 的常规输入。Agent 必须先使用 Harness Model；只有其中缺少
或存在歧义的具体事实会改变 Proposal 判断时，才可以说明已经读取的 Model 信息、
仍缺少的事实及其影响，申请一次只读源码兜底。单纯为了增加信心不能解锁源码。

Agent 的分析目标包括：

1. 理解预期结果、实际结果和关键行为差距，形成候选 `expected_behavior`；
2. 使用 Harness Model 定位相关 Component、Method 和关系；仅在 Model 信息不足时
   申请受控源码兜底；
3. 综合轨迹表现与当前实现，形成 `target`、`reason` 和 `requirement`。

这些目标不是固定的单向流水线。Agent 查看 Component 或源码后，可以回到轨迹重新
判断；新证据也可以推翻原来的所需行为、目标和归因。控制器不维护“当前处于哪个分析
阶段”的状态，只提供白名单只读工具并校验最终 JSON。

当前实现位于 `trajectory_analysis_loop.py`，提供以下查询能力：

- 按行读取或搜索 Task、Trajectory 和 Outcome；
- 按 JSON Pointer 读取或全文搜索 Harness Model；
- 用 `request_source_access` 说明 Harness Model 的具体信息缺口；
- 申请获准后才显示列出源码、读取限定方法和查询静态引用的兜底工具；
- 用 `done` 发布 `modification_proposal.json`；
- 用 `abort` 报告真正的技术阻塞。

Harness Model 是 Stage 2 的主要 Harness 输入，不是等待源码复核的临时索引。它与
当次轨迹共同支持判断，但不能单独证明某个运行分支已经触发。源码只用于补足会改变
Proposal 的具体模型缺口，不能替代 Harness Model 或成为新的主要分析材料。

## 四、运行方式

对当前 Harbor trial 目录运行：

```sh
python3.12 trajectory_analysis_loop.py \
  harness/harbor_terminus2 \
  runs/harbor_terminus2/harness_model.json \
  runs/stage2/feal-linear-cryp \
  --trial-dir datasets/tb21-qwen3.8-27b-terminus2/trajectories/qwen3.8-27b-terminus2/feal-linear-cryp \
  --backend codex
```

也可以用 `--task-file`、`--trajectory-file`、`--outcome-file` 和
`--trajectory-id` 提供与 Harbor 无关的通用输入。运行失败后加 `--resume` 可从保留
的检查点继续；成功后正式目录中的产物是 `modification_proposal.json`。命令中的
Harness 源码路径只供控制器在获准后建立受控兜底，不会在初始 Prompt 或工具列表中
暴露给 Agent。
