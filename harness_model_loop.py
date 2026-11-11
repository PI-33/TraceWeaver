"""Stage 1：用一个轻量工具循环把只读 Harness 源码建模为 harness_model.json。

模型每轮返回一个 JSON action。控制器只做五件事：
1. 建立一次性的源码结构索引；
2. 把白名单 action 显式映射到只读查询或草稿编辑方法；
3. 维护源码解析、Method、Component、harness、关系五个 State，并动态注入 Stage Guide；
4. 保存集中式 Method 分析、模型草稿和少量交互历史，支持中断恢复；
5. 在 done 时校验并发布 Harness Model。

源码索引负责文件、方法和调用位置，不猜测返回值传播或跨方法状态语义。Method State
把全部索引方法分批交给模型判断，Component 阶段按全局接线复核和归组。关系阶段
按消费方审阅源码，在同一份草稿上选出正式发布的依赖和影响。正式输出供 Stage 2
定位执行、观测、状态、错误和退出行为。
"""
from __future__ import annotations

import argparse
import ast
import bisect
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tokenize
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass


MAX_TURNS = 50
PAGE = 12_000
HISTORY_CAP = 60_000
RESULT_CAP = 30_000
MAX_QUERY_STREAK = 6
METHOD_BATCH_CHARS = 24_000
METHOD_BATCH_SIZE = 8
RELATION_BATCH_SIZE = 5
STAGES = ("源码解析", "Method", "Component", "harness", "关系")


STAGE_GUIDES = {
    "源码解析": """建立 Harness 的源码结构索引，为后续语义分析准备材料。

使用 parse_source 扫描 Harness 源码，提取：
- 源码文件及相对路径；
- 类、函数和方法定义；
- 方法签名及源码位置；
- Import、继承和可解析的静态调用关系；
- 其他能够从源码中确定的结构线索。

静态分析只记录能够确认的信息。对于动态调用、运行时注册和无法解析的关系，
不强行推断。

本阶段只建立 Source Index，不进行 Component 划分，不生成行为影响关系，
也不判断哪些方法应该进入最终 Harness Model。

完成源码索引后进入 Method State。""",

    "Method": """分析 Harness 源码中的方法，理解其实际职责和行为，并记录与 Harness 执行机制相关的方法。

原则上采用 keep，尽可能保留有助于理解 Harness 功能、执行流程、状态变化、
信息传递和行为控制的方法，不必过度筛选。

只有明显与 Harness 执行机制无关、没有后续定位价值的方法才标记为 skip，
例如纯粹的通用辅助代码。

判断时结合实际源码、调用关系和使用场景，不要仅根据方法名称、注释或 docstring
判断其作用。方法是否被直接调用，也不能单独作为 skip 的依据，应考虑框架回调、
动态注册、配置引用和其他接入方式。

每个保留方法的 Description 应简洁说明：
- 方法的实际职责；
- 接收的输入；
- 产生的输出；
- 关键执行行为及其影响。

对于会改变执行分支、状态、信息传递或后续行为的方法，保留必要的条件和行为说明。

普通方法简洁描述即可，不必展开所有参数、内部实现细节和普通辅助操作。

不需要为了筛选 keep / skip 花费过多精力。默认采用 keep，优先保证有定位价值的方法
不会被过早遗漏。

按 Controller 提供的批次分析方法，使用 record_methods 一次提交当前整批。
覆盖全部索引方法后调用 advance_stage。""",

    "Component": """根据当前 Harness 的实际源码结构、执行流程和方法职责，自主识别并划分逻辑 Component。

Component 不采用预先定义的固定分类框架，由 Agent 根据实际 Harness 内容决定需要划分
哪些组件。

划分时遵循以下原则：

1. 按功能职责划分，将承担相对独立、明确功能的部分归为一个 Component。
2. 关注行为边界，理解各部分控制什么行为、接收什么信息、产生什么结果。
3. 结合实际执行流程和方法之间的协作关系进行归组，不要仅根据文件或类名分类。
4. 一个 Component 可以包含多个文件中的方法。
5. 不要将每个文件或方法都拆分为独立 Component。
6. 职责明显不同的功能应适当区分，避免将不同执行机制强行合并。

最终使用尽可能少且职责清晰的 Component 概述整个 Harness 的主要功能组成。

对之前 Method 阶段的 keep / skip 判断进行全局复核：
- 如果之前被 skip 的方法实际上承担重要的执行职责或信息传递，应重新标记为 keep。
- 如果之前保留的方法明显与 Harness 执行机制无关，可以重新标记为 skip。

所有最终 keep 的方法必须且只能归属一个 Component。

每个 Component 提供一段简洁的 Description，说明其共同承担的功能职责。
每个成员 Method 提供一句简短说明，描述其在该 Component 中承担的具体分工。

使用 record_methods 修正需要调整的方法，使用 record_components 提交完整的组件分组。

完成所有 keep 方法的组件归组后调用 advance_stage。""",

    "harness": """依据已经建立的 Component 和 Method，描述整个 Harness 的主要运行方式。

从实际运行入口出发，结合源码理解：
- Harness 如何初始化；
- 主要工作循环如何推进；
- 各 Component 如何参与执行；
- 模型交互、动作执行和结果反馈如何衔接；
- 主要执行分支及异常处理如何影响流程；
- 哪些信息会进入下一轮执行；
- 任务通过什么条件结束。

重点描述整体执行过程和组件之间的协作关系，而不是逐个重复 Component 和 Method
已经记录的职责。

区分实际运行路径与仅有定义但未接入主流程的能力。
对于可选机制，应说明其实际触发条件，避免将其描述为默认执行流程。

根据 Harness 的实际复杂程度组织描述，保持准确、简洁和连贯。

最终形成一段能够帮助后续 Agent 快速理解 Harness 整体运行方式的 Harness Description。

完成后调用 advance_stage。""",

    "关系": """基于已经确定的 Component 和 Method，分析并建立有助于理解 Harness 执行行为的关系。

本阶段保留三类关系：

1. contains：表达 Harness、Component 和 Method 之间的归属关系，由 Controller 自动生成。
2. depends_on：表达当前方法在实现上依赖哪些方法或资源。
3. influences：表达其他方法产生的数据、状态、结果或行为如何影响当前方法的执行。

关系分析应结合实际源码、调用位置、参数传递、返回值消费和状态变化进行判断。

depends_on：
- 识别当前方法实际使用的内部实现。
- 检查直接调用、有效的动态分派及其他可确认的实现依赖。
- 说明在什么条件下使用该依赖，以及它提供什么功能。
- 方向统一为使用方指向被依赖方。

influences：
- 识别哪些上游方法产生的信息会改变当前方法的行为。
- 关注数据、异常、状态、执行结果和反馈在不同方法之间的传递。
- 说明触发条件、传递内容以及对下游行为产生的实际影响。
- 不要将普通调用、对象初始化或常规参数传递直接视为行为影响。
- 不要仅因为两个方法存在调用关系就建立 influences。
- 对跨方法、跨阶段的信息传递，尽可能找到实际产生处和消费处。

关系分析以能够解释主要执行路径和关键行为变化为目标，不追求覆盖所有理论上的调用关系。

避免重复记录同一行为影响的多个间接祖先。
无法确认的关系不应强行建立。

按照 Controller 提供的批次逐个审阅 Method，使用 record_relations 提交当前批次。

所有方法完成关系审阅后，检查整体关系是否能够解释主要执行路径和关键行为影响，
通过最终关系菜单选择需要保留的关系，并调用 done 发布 Harness Model。""",
}


ACTIONS = {
    "parse_source": {
        "required": (),
        "optional": (),
        "example": {},
        "help": "扫描只读源码并建立文件、方法和调用索引；已有索引时直接复用。",
    },
    "list_files": {
        "required": (),
        "optional": ("scope", "path", "cursor"),
        "example": {},
        "help": "列出源码文件；指定 path 时展开该文件的方法。scope=work 查看工作区。",
    },
    "read": {
        "required": ("path",),
        "optional": ("scope", "cursor", "pointer"),
        "example": {"path": "<文件、文件#限定方法名或文件:行范围>"},
        "help": "读取源码或工作文件；读取 harness_model.json 时可用 pointer 精确取得一个 JSON 子树。",
    },
    "find_references": {
        "required": ("query",),
        "optional": ("direction", "cursor"),
        "example": {"query": "<方法名或文本>", "direction": "any"},
        "help": "查询 callers、callees 或普通文本位置；静态调用只作为核实线索。",
    },
    "edit_file": {
        "required": ("path", "old_text", "new_text"),
        "optional": ("pointer",),
        "example": {
            "path": "harness_model.json",
            "old_text": "<唯一原文>",
            "new_text": "<替换内容>",
        },
        "help": "编辑 method_analysis.json 或 harness_model.json；可用 JSON pointer 精确修改。",
    },
    "record_methods": {
        "required": ("items",),
        "optional": (),
        "example": {
            "items": {
                "<文件#限定方法名>": {
                    "decision": "keep",
                    "description": "<自足行为摘要>",
                }
            }
        },
        "help": "Method 阶段提交当前整批；Component 阶段也可批量修正已审阅方法的决定和说明。",
    },
    "record_components": {
        "required": ("groups",),
        "optional": (),
        "example": {"groups": {"<组件名>": {
            "Description": "<组件共同承担的运行职责>",
            "methods": {"<文件#限定方法名>": "<一句话说明该方法在组件中的分工>"},
        }}},
        "help": "一次按组件分配全部 keep 的方法；每个定位符使用一次，自动带入方法说明。",
    },
    "record_relations": {
        "required": ("items",),
        "optional": (),
        "example": {"items": {"<本批的方法显示名>": {
            "depends_on": {"<用到的方法名>": "<调用条件和用途>"},
            "influenced_by": {"<上游方法名>": "<条件、传递内容和本方法的变化>"},
            "affects": {"<跨步骤消费此结果的方法名>": "<载体、转交路径和下游变化>"},
        }}},
        "help": "一次覆盖本批全部方法；depends_on 指向被用方，influenced_by 来自上游。可选 affects 从本方法指向跨步骤的消费方。",
    },
    "advance_stage": {
        "required": (),
        "optional": (),
        "example": {},
        "help": "提交当前 State 的最低产出检查，通过后进入下一个 State。",
    },
    "back_stage": {
        "required": (),
        "optional": (),
        "example": {},
        "help": "需要重新聚焦前一项工作时回到上一个 State；已有产物不会删除。",
    },
    "done": {
        "required": (),
        "optional": ("selection",),
        "example": {},
        "help": "校验并发布；多批关系审阅完成后按当前菜单提交 selection，选定正式输出的两类关系。",
    },
    "abort": {
        "required": ("reason",),
        "optional": (),
        "example": {"reason": "<无法继续的具体原因>"},
        "help": "确实无法继续时终止并保留工作区。",
    },
}


SYSTEM_PROMPT_TEMPLATE = """你是 Harness 建模 Agent。

## Overall Mission

你的任务是分析 {{ROOT}} 下的 Harness 源码，理解其真实结构与运行机制，并生成
harness_model.json。

最终需要建立一份能够描述当前 Harness 主要结构、组件职责、具体实现及其关系的模型，
供后续 Agent 根据执行轨迹定位相关 Harness 组件和实现。

建模需要理解：
1. Harness 整体如何运行，主要执行流程是什么？
2. Harness 可以划分为哪些逻辑 Component，各自承担什么职责？
3. 哪些 Method 实现了这些行为，它们接收什么输入、产生什么输出？
4. Component 和 Method 之间存在哪些实现依赖与行为影响关系？

Harness Model 的目标不是完整描述整个代码库，而是保留能够解释 Harness 主要执行行为、
组件协作和关键行为影响关系的信息，为后续轨迹分析和 Harness 定位提供支持。

必须以实际源码、调用位置、配置和接入路径为依据。名称、注释和 docstring 可以作为线索，
但不得单独作为行为结论。

区分已经接入实际运行路径的机制与仅有定义的能力，不要将代码中存在某个实现直接等同于
该能力已经在 Harness 中生效。

本阶段只理解和描述当前 Harness，不分析具体任务轨迹，不判断某次任务失败的原因，
也不提出 Harness 优化或修改建议。

## Execution Protocol

你通过注册的 Action 与 Controller 交互，完成源码读取、信息查询和建模产物保存。

- 源码目录只读，所有写入只能发生在本次工作区。
- 每轮只返回一个 JSON Action，不要返回 Markdown、额外解释或多个动作。
- 严格遵循 Action 名称和参数协议，不得调用协议外工具。
- read 返回 next_cursor 时，使用相同 path，并将 cursor 作为 JSON 字符串传入。
- 工具调用失败时，根据错误信息修正，不要原样重复请求。
- 根据当前 State Guide 工作，只能通过 advance_stage 或 back_stage 请求切换 State。
- 不得通过修改工作文件伪造状态。
- 后续 State 仍可读取源码并修正已有分析，不必为了状态顺序牺牲准确性。
- 应及时保存已经确认的结论，避免连续查询而没有形成有效产出。
- 根据当前任务需要自主决定查询和分析方式，不要进行不必要的重复查询。

## Current State Guide

{{STAGE_GUIDE}}

## Available Actions

{{ACTIONS}}

## Output Schema

{{SCHEMA}}

## Workspace and Model Requirements

所有分析结果保存在工作区中的结构化文件内：

- source_index.json：源码解析生成的源码结构索引。
- method_analysis.json：Method 阶段的中间分析结果。
- harness_model.json：最终 Harness Model 的草稿及各阶段持续完善的结果。

源码目录始终只读，所有分析产物只能写入工作区。

所有产物必须符合约定的数据结构，并保持内容有效、引用一致。

需要读取或修改已有产物时，先使用 read 获取对应内容，再进行操作。

Harness Model 的整体结构为：

- schema_version
- harness
- nodes
  - Component
    - Method
      - Description
      - file_path
- edges
  - contains
  - depends_on
  - influences

其中：

- Component 描述逻辑功能组件。
- Method 描述具体实现方法。
- Description 是节点的语义描述，不是独立节点。
- file_path 指向 Method 实际所在的源码文件。
- contains 表达归属关系。
- depends_on 表达实现依赖。
- influences 表达行为影响。

contains 关系由 Controller 根据 Component 和 Method 的归属自动生成，不需要手工维护。

最终 Harness Model 只保留后续轨迹分析和定位真正需要的结构、描述和关系，
不保存冗余代码证据、行号清单或分析推理过程。

具体的分析目标、判断标准、处理方法和阶段产出要求，以当前 State Guide 为准。
全局约束与当前 State Guide 不一致时，优先遵守全局运行和数据安全约束。

## Completion

只有在各阶段的必要产物完成，并且最终 Harness Model 通过 Controller 校验后，
才能调用 done 发布结果。

不要为了追求完整描述而无限扩大建模范围。
优先保证最终 Harness Model 的准确性、可理解性和对后续轨迹定位的实际价值。
"""


def encoded(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def save(path, text):
    """原子替换文本文件，避免检查点、索引或草稿只写入一半。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        try:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def strict_json(text):
    """解析 JSON，同时拒绝会被普通 json.loads 静默覆盖的重复键。"""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=unique)


def skeleton():
    return {
        "schema_version": "0.1",
        "harness": "",
        "nodes": {"Component": {}},
        "edges": {"contains": {}, "depends_on": {}, "influences": {}},
    }


def concrete(value):
    if not isinstance(value, str) or not value.strip():
        return False
    text = value.strip()
    return not (
        re.fullmatch(r"<[^<>]+>|\.\.\.|…", text)
        or text in {
            "职责",
            "具体职责",
            "方法职责",
            "具体职责、输入、输出和条件",
            "生效条件、结果及下游变化",
        }
    )


def _decode_source(file, raw):
    try:
        encoding = (
            tokenize.detect_encoding(io.BytesIO(raw).readline)[0]
            if file.endswith(".py")
            else "utf-8"
        )
        if b"\0" in raw:
            raise ValueError("binary resource")
        return raw.decode(encoding), encoding, None
    except (UnicodeError, SyntaxError, LookupError, ValueError) as exc:
        if file.endswith(".py"):
            raise ValueError(f"{file}: cannot decode Python source: {exc}") from exc
        placeholder = f"[binary resource: {len(raw)} bytes]"
        return placeholder, None, placeholder


def _line_starts(lines):
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return offsets


def _line_at(starts, offset):
    return min(max(1, len(starts) - 1), bisect.bisect_right(starts, offset))


def _method_definitions(file, tree, starts):
    """列出函数、方法和嵌套函数，并保留供调用匹配使用的本层 AST。"""
    seen = set()
    definitions = (ast.FunctionDef, ast.AsyncFunctionDef)

    def visit(parent, scope, owner):
        for node in ast.iter_child_nodes(parent):
            if isinstance(node, ast.ClassDef):
                cls = (*scope, node.name)
                yield from visit(node, cls, ".".join(cls))
            elif isinstance(node, definitions):
                path = (*scope, node.name)
                name = ".".join(path)
                if name in seen:
                    name += f"@{node.lineno}"
                seen.add(name)
                first = min([node.lineno] + [item.lineno for item in node.decorator_list])
                body = []
                pending = list(reversed(node.body))
                while pending:
                    child = pending.pop()
                    if not isinstance(child, (ast.ClassDef, *definitions)):
                        body.append(child)
                        pending.extend(reversed(list(ast.iter_child_nodes(child))))
                prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                signature = f"{prefix} {node.name}({ast.unparse(node.args)})"
                if node.returns is not None:
                    signature += f" -> {ast.unparse(node.returns)}"
                doc = ast.get_docstring(node)
                method = {
                    "file": file,
                    "name": name,
                    "owner": owner,
                    "line": first,
                    "last": node.end_lineno,
                    "start": starts[first - 1],
                    "end": starts[node.end_lineno],
                    "signature": signature,
                    "decorators": [ast.unparse(item) for item in node.decorator_list],
                    "doc": doc.splitlines()[0] if doc else "",
                }
                yield method, node, body
                yield from visit(node, path, owner)
            else:
                yield from visit(node, scope, owner)

    return visit(tree, (), "")


def _call_edges(trees, definitions, source_lines):
    """建立保守调用索引；resolved 只表示满足这里的静态名称匹配规则。"""
    def module_file(file, module, level):
        parent = Path(file).parent if level else Path()
        for _ in range(max(0, level - 1)):
            parent = parent.parent
        stem = parent.joinpath(*module.split("."))
        candidates = (str(stem) + ".py", (stem / "__init__.py").as_posix())
        return next((candidate for candidate in candidates if candidate in trees), None)

    ancestry = {}
    imports = {}
    links = []
    local_classes = {
        (file, node.name) for file, tree in trees.items()
        for node in tree.body if isinstance(node, ast.ClassDef)
    }
    for file, tree in trees.items():
        for cls in ast.walk(tree):
            if isinstance(cls, ast.ClassDef):
                ancestry.setdefault(cls.name, set()).update(
                    ast.unparse(base).split(".")[-1] for base in cls.bases
                )
    for _ in range(len(ancestry)):
        for parents in ancestry.values():
            parents.update(set().union(
                *(ancestry.get(parent, set()) for parent in list(parents))))

    for file, tree in trees.items():
        bindings = imports[file] = {}
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    bindings[local] = module_file(file, alias.name, 0), ""
                    target, _ = bindings[local]
                    if target:
                        links.append(
                            f"{file}:{node.lineno} {ast.unparse(node)} → {target}"
                        )
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    local = alias.asname or alias.name
                    if node.module:
                        target = module_file(file, node.module, node.level)
                        bindings[local] = target, alias.name
                    else:
                        target = module_file(file, alias.name, node.level)
                        bindings[local] = target, ""
                    if target:
                        links.append(
                            f"{file}:{node.lineno} {ast.unparse(node)} → {target}"
                        )
            elif isinstance(node, ast.ClassDef) and node.bases:
                links.append(
                    f"{file}:{node.lineno} class {node.name}"
                    f"({', '.join(ast.unparse(base) for base in node.bases)})"
                )
            elif isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
                links.append(f"{file}:{node.lineno} {ast.unparse(node)}")

    by_short = {}
    attributes = {}
    # For self.field = self.factory(), retain the local classes actually constructed
    # by return statements. Their methods are dispatch candidates, not certain calls.
    def returned_classes(file, expr):
        if isinstance(expr, ast.IfExp):
            yield from returned_classes(file, expr.body)
            yield from returned_classes(file, expr.orelse)
        if isinstance(expr, ast.Call):
            func = expr.func
            if isinstance(func, ast.Name):
                location = imports[file].get(func.id, (file, func.id))
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                module, binding = imports[file].get(func.value.id, (None, None))
                location = (module, func.attr) if binding == "" else None
            else:
                return
            if location in local_classes:
                yield location

    factory_returns = {}
    for key, (_, _, body) in definitions.items():
        constructed = {cls for node in body if isinstance(node, ast.Return)
                       for cls in returned_classes(key[0], node.value)}
        if constructed:
            factory_returns[key] = constructed
    dispatch_types = {}
    for (file, _), (method, _, body) in definitions.items():
        by_short.setdefault(method["name"].split(".")[-1].split("@")[0], []).append(method)
        for node in body:
            if isinstance(node, ast.AnnAssign):
                attributes.setdefault(
                    (file, method["owner"], ast.unparse(node.target)), set()
                ).update(re.findall(r"\b\w+\b", ast.unparse(node.annotation)))
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(node.value, ast.Call):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute):
                    key = file, method["owner"], ast.unparse(target)
                    func = node.value.func
                    attributes.setdefault(key, set()).add(ast.unparse(func).split(".")[-1])
                    if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                            and func.value.id == "self"):
                        factory = file, f"{method['owner']}.{func.attr}"
                        if factory in factory_returns:
                            dispatch_types.setdefault(key, set()).update(factory_returns[factory])

    calls = []
    for (file, name), (method, function, body) in definitions.items():
        owner = method["owner"]
        parents = {child: parent for parent in body for child in ast.iter_child_nodes(parent)}
        annotations = {}
        local_names = {
            node.id for node in body
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        }
        arguments = function.args
        for argument in [
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs]:
            local_names.add(argument.arg)
            if argument.annotation:
                annotations[argument.arg] = set(
                    re.findall(r"\b\w+\b", ast.unparse(argument.annotation)))
        for node in body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                cls = ast.unparse(node.value.func).split(".")[-1]
                if cls in ancestry:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            annotations.setdefault(target.id, set()).add(cls)

        for call in body:
            if not isinstance(call, ast.Call):
                continue
            if isinstance(call.func, ast.Name):
                short, receiver = call.func.id, ""
            elif isinstance(call.func, ast.Attribute):
                short, receiver = call.func.attr, ast.unparse(call.func.value)
            else:
                continue
            use = parents.get(call)
            if isinstance(use, ast.Await):
                use = parents.get(use)
            expression = call
            statement = parents.get(expression)
            while statement is not None and not isinstance(statement, ast.stmt):
                expression = statement
                statement = parents.get(expression)
            assignment = ""
            if isinstance(statement, ast.Assign) and expression is statement.value:
                assignment = " = ".join(ast.unparse(t) for t in statement.targets)
            elif isinstance(statement, ast.AnnAssign) and expression is statement.value:
                assignment = ast.unparse(statement.target)
            imported_file, imported_name = imports[file].get(
                receiver or short, (None, ""))
            target_name = imported_name if not receiver and imported_name else short
            types = (
                annotations.get(receiver, set())
                | attributes.get((file, owner, receiver), set())
            ).intersection(ancestry)
            for destination in by_short.get(target_name, ()):
                target_owner = destination["owner"]
                local = (
                    receiver in ("self", "cls")
                    and owner
                    and owner == target_owner
                    and file == destination["file"]
                )
                typed = (
                    len(types) == 1
                    and target_owner
                    and target_owner.split(".")[-1] in types
                )
                direct = (
                    not receiver
                    and short not in local_names
                    and not target_owner
                    and destination["file"] == file
                    and destination["name"] == short
                )
                imported = destination["file"] == imported_file and (
                    (
                        not receiver
                        and short not in local_names
                        and destination["name"] == imported_name
                    )
                    or (
                        receiver
                        and receiver not in local_names
                        and target_owner == imported_name
                    )
                )
                override = (
                    receiver in ("self", "cls")
                    and owner in ancestry.get(target_owner, set())
                )
                resolved = bool(local or typed or direct or imported)
                candidate = (destination["file"], target_owner) in dispatch_types.get(
                    (file, owner, receiver), ())
                if not (resolved or override or candidate):
                    continue
                calls.append({
                    "from": name,
                    "from_file": file,
                    "to": destination["name"],
                    "to_file": destination["file"],
                    "line": call.lineno,
                    "resolved": resolved,
                    "override": override,
                    "result_used": not isinstance(use, ast.Expr),
                    "assignment": assignment,
                    "code": " ".join(line.strip() for line in source_lines[file][
                        call.lineno - 1:min(call.end_lineno, call.lineno + 4)])[:240],
                })
    return (
        sorted(
            calls,
            key=lambda item: (
                item["from_file"],
                item["line"],
                item["to_file"],
                item["to"],
            ),
        ),
        links,
        {file: sorted({target for target, _ in bindings.values() if target})
         for file, bindings in imports.items()},
    )


def parse_source(harness_root: str | Path, work_dir: str | Path) -> str:
    """扫描源码一次并持久化事实索引；不会执行被分析代码。"""
    root = Path(harness_root).resolve(strict=True)
    work = Path(work_dir).resolve()
    output = work / "source_index.json"
    if not root.is_dir() or work.is_relative_to(root):
        raise ValueError("choose a source directory and a separate work directory")
    if output.is_file():
        index = strict_json(output.read_text(encoding="utf-8"))
        return (
            f"OK work/source_index.json，{len(index['files'])} 个文件、"
            f"{len(index['methods'])} 个方法、{len(index['calls'])} 条调用线索。"
        )

    files = []
    methods = []
    trees = {}
    definitions = {}
    source_lines = {}
    syntax_errors = []
    digest = hashlib.sha256()

    def scan_error(error):
        raise error

    for directory, dirs, names in os.walk(root, onerror=scan_error):
        dirs[:] = sorted(
            name for name in dirs if name not in {".git", "__pycache__"})
        for name in [*dirs, *sorted(names)]:
            target = (Path(directory) / name).resolve()
            if not target.is_relative_to(root):
                raise ValueError(f"source symlink leaves root: {target}")
        for name in sorted(names):
            path = Path(directory) / name
            file = path.relative_to(root).as_posix()
            raw = path.read_bytes()
            file_hash = hashlib.sha256(raw)
            digest.update(file.encode() + b"\0" + file_hash.digest())
            text, encoding, warning = _decode_source(file, raw)
            lines = text.splitlines(keepends=True)
            starts = _line_starts(lines)
            files.append({
                "path": file,
                "lines": len(lines),
                "chars": len(text),
                "encoding": encoding,
                "warning": warning,
                "sha256": file_hash.hexdigest(),
            })
            if not file.endswith(".py"):
                continue
            try:
                tree = ast.parse(text, filename=file)
            except SyntaxError as exc:
                syntax_errors.append(
                    f"{file}: Python parse error at line {exc.lineno}: {exc.msg}")
                continue
            trees[file] = tree
            source_lines[file] = lines
            for method, node, body in _method_definitions(file, tree, starts):
                methods.append(method)
                definitions[file, method["name"]] = (method, node, body)

    if syntax_errors:
        raise ValueError(", ".join(syntax_errors))
    if not files:
        raise ValueError("source tree is empty")
    calls, module_links, imports = _call_edges(trees, definitions, source_lines)
    assigned_fields = {call["assignment"] for call in calls if call["assignment"].startswith("self.")}
    for method, _, body in definitions.values():
        reads = {}
        for node in body:
            if (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                    and isinstance(node.value, ast.Name) and node.value.id == "self"):
                field = "self." + node.attr
                if field in assigned_fields:
                    reads.setdefault(field, node.lineno)
        if reads:
            method["field_uses"] = reads
    for item in files:
        if item["path"] in imports:
            item["imports"] = imports[item["path"]]
    base = Path.cwd().resolve()
    display_root = (
        root.relative_to(base).as_posix()
        if root != base and root.is_relative_to(base)
        else str(root)
    )
    index = {
        "root": display_root,
        "snapshot": digest.hexdigest(),
        "files": sorted(files, key=lambda item: item["path"]),
        "methods": sorted(
            methods, key=lambda item: (
                item["file"], item["line"], item["name"])),
        "calls": calls,
        "module_links": module_links,
    }
    save(output, encoded(index) + "\n")
    return (
        f"OK work/source_index.json，{len(files)} 个文件、"
        f"{len(methods)} 个方法、{len(calls)} 条调用线索。"
    )


def system_prompt(root, stage="源码解析"):
    if stage not in STAGE_GUIDES:
        raise ValueError(f"unknown modeling stage {stage}")
    actions = []
    for name, spec in ACTIONS.items():
        example = json.dumps(
            {"action": name, **spec["example"]}, ensure_ascii=False)
        required = ", ".join(spec["required"]) or "无"
        optional = ", ".join(spec["optional"]) or "无"
        actions.append(
            f"{example}\n  {spec['help']} 必填：{required}。可选：{optional}。")
        if name == "edit_file":
            actions.extend([
                json.dumps({
                    "action": "edit_file",
                    "path": "method_analysis.json",
                    "pointer": "/<相对文件#限定方法名，文件中的斜杠写成~1>",
                    "old_text": None,
                    "new_text": {
                        "decision": "keep",
                        "description": "<关键行为摘要>",
                    },
                }, ensure_ascii=False),
                json.dumps({
                    "action": "edit_file",
                    "path": "harness_model.json",
                    "pointer": "/nodes/Component/<组件名>",
                    "old_text": None,
                    "new_text": {
                        "Description": "<组件职责>",
                        "Method": {
                            "<限定方法名>": {
                                "Description": "<关键行为>",
                                "file_path": "<相对源码路径>",
                            }
                        },
                    },
                }, ensure_ascii=False),
            ])
    return (
        SYSTEM_PROMPT_TEMPLATE
        .replace("{{ROOT}}", str(root))
        .replace("{{ACTIONS}}", "\n".join(actions))
        .replace("{{SCHEMA}}", encoded(skeleton()))
        .replace("{{STAGE_GUIDE}}", STAGE_GUIDES[stage])
    )


def parse_action(raw):
    """把模型文本收敛为一个参数合法的白名单 action。"""
    text = raw.strip()
    fence = chr(96) * 3
    if text.startswith(fence) and text.endswith(fence):
        text = text.split("\n", 1)[-1][:-3].strip()
    try:
        action = strict_json(text)
    except json.JSONDecodeError as exc:
        if exc.msg != "Extra data" or not re.fullmatch(r"}+", text[exc.pos:].strip()):
            raise
        action = strict_json(text[:exc.pos])
    if (
        not isinstance(action, dict)
        or not isinstance(action.get("action"), str)
        or action["action"] not in ACTIONS
    ):
        raise ValueError("return one registered JSON action object")
    spec = ACTIONS[action["action"]]
    keys = set(action) - {"action"}
    if not set(spec["required"]) <= keys or not keys <= set(
            spec["required"] + spec["optional"]):
        raise ValueError(
            f"{action['action']} requires {spec['required']}, "
            f"optional {spec['optional']}")
    typed = set()
    if action["action"] == "edit_file" and "pointer" in action:
        typed = {"old_text", "new_text"}
    elif action["action"] in {"record_methods", "record_relations"}:
        typed = {"items"}
    elif action["action"] == "record_components":
        typed = {"groups"}
    elif action["action"] == "done":
        typed = {"selection"}
    if any(
        not isinstance(value, str)
        for key, value in action.items()
        if key not in typed
    ):
        raise ValueError("expected string parameters apart from structured actions and pointer values")
    if action.get("scope", "source") not in {"source", "work"}:
        raise ValueError("scope must be source or work")
    if action.get("direction", "any") not in {
            "any", "callers", "callees", "text"}:
        raise ValueError("direction must be any, callers, callees or text")
    return action


def paginate(text, cursor=None, start=0, end=None):
    limit = len(text) if end is None else end
    offset = int(cursor) if cursor is not None else start
    if offset < start or offset > limit:
        raise ValueError(f"cursor outside requested range [{start}, {limit}]")
    right = min(limit, offset + PAGE)
    info = f"chars=[{offset},{right})"
    if right < limit:
        info += f" next_cursor={right}"
    return f"{info}\n{text[offset:right]}", offset, right


@dataclass
class LoopResult:
    status: str
    n_turns: int
    path: Path | None = None
    reason: str = ""


class AgentRun:
    """持久化的单 Agent 状态机；源码只读，分析只写两个集中式产物。"""

    def __init__(self, root, directory, resume=False):
        self.root = Path(root).resolve()
        self.dir = Path(directory).resolve()
        self.work = self.dir / "work"
        self.draft = self.work / "harness_model.json"
        self.method_analysis = self.work / "method_analysis.json"
        self.index_path = self.work / "source_index.json"
        self.checkpoint = self.dir / "run_state.json"
        self.log_path = self.dir / "actions.jsonl"
        if resume:
            self.state = strict_json(
                self.checkpoint.read_text(encoding="utf-8"))
            if self.state["root"] != str(self.root):
                raise ValueError("resume requires the same source root")
            self.state.setdefault(
                "stage",
                "Method" if self.index_path.is_file() else "源码解析",
            )
            self.state.setdefault("query_streak", 0)
        else:
            if any(self.dir.iterdir()):
                raise ValueError(
                    "run directory already contains files; choose a new one or use resume")
            self.work.mkdir()
            self.state = {
                "root": str(self.root),
                "turns": 0,
                "history": [],
                "status": "running",
                "last": "",
                "stage": "源码解析",
                "query_streak": 0,
            }
            save(self.draft, encoded(skeleton()) + "\n")
            save(self.method_analysis, "{}\n")
            self.check()

    def check(self):
        save(self.checkpoint, encoded(self.state) + "\n")

    def work_path(self, relative):
        if relative.startswith("work/"):
            relative = relative[len("work/"):]
        path = (self.work / relative).resolve()
        if (
            Path(relative).is_absolute()
            or not path.is_relative_to(self.work)
            or path == self.work
        ):
            raise ValueError("path must be inside the work directory")
        return path

    def text(self, relative):
        path = self.work_path(relative)
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def remember(self, action, result):
        recorded = dict(action)
        if recorded.get("action") == "edit_file":
            for key in ("old_text", "new_text"):
                if key not in recorded:
                    continue
                value = encoded(recorded[key])
                recorded[key] = (
                    value if len(value) <= 240
                    else f"<{type(action[key]).__name__}, {len(value)} chars>"
                )
        elif recorded.get("action") in {"record_methods", "record_components", "record_relations"}:
            field = "groups" if "groups" in recorded else "items"
            items = recorded.get(field)
            recorded[field] = f"<{len(items)} entries>" if isinstance(items, dict) else "<invalid>"
        request = json.dumps(recorded, ensure_ascii=False)
        entry = f"[action] {request[:4000]}\n[result] {str(result)[:RESULT_CAP]}"
        history = [*self.state.get("history", []), entry]
        while len("\n\n".join(history)) > HISTORY_CAP and len(history) > 1:
            history.pop(0)
        self.state["history"] = history
        self.state["last"] = str(result)

    def read_index(self):
        if not self.index_path.is_file():
            raise ValueError("no work/source_index.json; call parse_source first")
        index = strict_json(self.index_path.read_text(encoding="utf-8"))
        index["files_by_path"] = {
            item["path"]: item for item in index["files"]}
        index["methods_by_location"] = {
            (item["file"], item["name"]): item for item in index["methods"]}
        return index

    def source_document(self, index, file):
        opened = index.setdefault("_opened_files", {})
        if file not in opened:
            target = (self.root / file).resolve()
            if not target.is_relative_to(self.root):
                raise ValueError("source path leaves root")
            info = index["files_by_path"][file]
            raw = target.read_bytes()
            text = (
                raw.decode(info["encoding"])
                if info["encoding"]
                else info["warning"]
            )
            lines = text.splitlines(keepends=True)
            opened[file] = {
                "text": text,
                "lines": lines,
                "starts": _line_starts(lines),
            }
        return opened[file]

    def source_line(self, index, file, line):
        lines = self.source_document(index, file)["lines"]
        return lines[line - 1].strip() if lines else ""

    def method_for(self, index, display, file):
        name = display[len(file) + 1:] if display.startswith(file + "#") else display
        method = index["methods_by_location"].get((file, name))
        if not method:
            raise ValueError(f"no indexed method {display} in {file}")
        return method

    def validate_method_analysis(self, index, analysis, *, complete):
        if not isinstance(analysis, dict):
            raise ValueError("method_analysis.json must be a JSON object")
        kept = 0
        for locator, item in analysis.items():
            file, separator, method_name = locator.partition("#")
            if (
                not separator
                or file not in index["files_by_path"]
                or not isinstance(item, dict)
                or set(item) != {"decision", "description"}
                or item["decision"] not in {"keep", "skip"}
                or not concrete(item["description"])
            ):
                raise ValueError(
                    f"{locator}: use file#qualified_method with decision keep/skip "
                    "and a concrete description")
            self.method_for(index, method_name, file)
            kept += item["decision"] == "keep"
        if complete:
            expected = {
                f"{method['file']}#{method['name']}"
                for method in index["methods"]
            }
            missing = sorted(expected - set(analysis))
            if missing:
                preview = ", ".join(missing[:8])
                suffix = f" … and {len(missing) - 8} more" if len(missing) > 8 else ""
                raise ValueError(
                    f"Method State still has {len(missing)} unreviewed methods: "
                    f"{preview}{suffix}")
            if kept == 0:
                raise ValueError(
                    "Method State must retain at least one key method")
        return analysis

    def load_method_analysis(self, index, *, complete=False):
        analysis = strict_json(
            self.method_analysis.read_text(encoding="utf-8"))
        self.validate_method_analysis(
            index, analysis, complete=complete)
        return analysis

    def method_batch(self, index, analysis):
        """按源码顺序选择下一批尚未判断的方法，单个超长方法独占一批。"""
        pending = [
            method for method in index["methods"]
            if f"{method['file']}#{method['name']}" not in analysis
        ]
        batch = []
        characters = 0
        for method in pending:
            document = self.source_document(index, method["file"])
            source = document["text"][method["start"]:method["end"]]
            cost = len(source) + len(method["name"]) + 300
            if batch and (
                len(batch) >= METHOD_BATCH_SIZE
                or characters + cost > METHOD_BATCH_CHARS
            ):
                break
            batch.append((method, source))
            characters += cost
            if len(batch) >= METHOD_BATCH_SIZE:
                break
        return batch

    def module_context(self, index):
        rows = list(index["module_links"][:80])
        # This is a package-import path, not a test of runtime reachability.
        # Older saved indexes have the textual links but no structured imports.
        imports = {f["path"]: f["imports"] for f in index["files"] if "imports" in f}
        entry = [f for f in ("__init__.py", "__main__.py") if imports.get(f)]
        if entry:
            seen = set(entry)
            pending = entry.copy()
            while pending:
                for target in imports.get(pending.pop(), []):
                    if target not in seen:
                        seen.add(target)
                        pending.append(target)
            rows.append("从 " + ", ".join(entry) + " 沿索引中的顶层 import 可到达：" + ", ".join(sorted(seen)))
            outside = sorted(imports.keys() - seen)
            if outside:
                rows.append("未在上述静态导入路径中：" + ", ".join(outside)
                            + "。若无注册、回调或动态加载的源码依据，将独立能力记为 skip，在概览交代边界。")
        return "\n".join(rows) or "索引没有包内导入关系，请按公开入口及源码核实。"

    def component_context(self, index, analysis):
        kept = {key for key, item in analysis.items() if item["decision"] == "keep"}
        rows = []
        seen = set()
        shared_results = {}
        for call in index["calls"]:
            caller = f"{call['from_file']}#{call['from']}"
            callee = f"{call['to_file']}#{call['to']}"
            if caller in kept and callee not in kept and call.get("result_used"):
                shared_results.setdefault(callee, set()).add(caller)
            pair = caller, callee
            if pair in seen or (caller not in kept and callee not in kept):
                continue
            seen.add(pair)
            dispatch = "" if call["resolved"] else " [候选分派]"
            rows.append(f"{call['from']} → {call['to']}{dispatch} | {call['from_file']}:"
                        f"{call['line']} {call['code'][:140]}")
        sites = "\n".join(rows)
        if len(sites) > METHOD_BATCH_CHARS:
            sites = sites[:METHOD_BATCH_CHARS].rsplit("\n", 1)[0] + "\n…"
        review = [f"{callee} 的结果用于 " + "、".join(sorted(callers))
                  for callee, callers in shared_results.items() if len(callers) > 1]
        return (self.module_context(index) + "\n暂列 skip、但多个保留方法消费其结果的定位符：\n"
                + ("\n".join(review) or "（无）") + "\n归组复核所用的实际接线（每对端点显示一处，"
                "更多位置可用 find_references）：\n" + sites)

    def method_batch_context(self, index, analysis):
        batch = self.method_batch(index, analysis)
        reviewed = len(analysis)
        total = len(index["methods"])
        if not batch:
            return (
                f"Method 覆盖：{reviewed}/{total}。全部方法均已判断；"
                "检查 keep/skip 结果后调用 advance_stage。"
            )
        rows = [
            f"Method 覆盖：{reviewed}/{total}。本轮需要分析以下 {len(batch)} 个方法。",
            "原则上采用 keep，尽可能保留与 Harness 执行机制相关的方法。",
            "只有明显无关、没有后续定位价值的方法才使用 skip。",
            "每个方法都需要结合源码理解实际职责和行为，并给出简洁、具体的 Description。",
            "不要为了筛选而反复查询；当前材料不足时，优先结合已有调用线索进行判断。",
            self.module_context(index),
        ]
        locators = []
        for method, source in batch:
            locator = f"{method['file']}#{method['name']}"
            locators.append(locator)
            location = method["file"], method["name"]
            outgoing = []
            incoming = []
            for call in index["calls"]:
                certainty = "" if call["resolved"] else " [候选分派]"
                site = f"L{call['line']} {call['code'][:140]}{certainty}"
                if (call["from_file"], call["from"]) == location:
                    outgoing.append(f"{call['to']} @ {site}")
                if (call["to_file"], call["to"]) == location:
                    incoming.append(f"{call['from_file']}#{call['from']} @ {site}")
            rows.extend([
                "",
                f"===== {locator} lines={method['line']}-{method['last']} =====",
                f"signature: {method['signature']}",
                "callers: " + (", ".join(dict.fromkeys(incoming)) or "（索引未解析到）"),
                "callees: " + (", ".join(dict.fromkeys(outgoing)) or "（索引未解析到）"),
                "只读方法源码：",
                source,
                "===== 方法源码结束 =====",
            ])
        example = {
            "action": "record_methods",
            "items": {
                locator: {
                    "decision": "keep 或 skip",
                    "description": "<职责、输入、输出、关键条件和行为影响；skip 可简写>",
                }
                for locator in locators
            },
        }
        rows.extend([
            "",
            "只返回一个 record_methods action，并且 items 的 key 必须与本批完全一致：",
            json.dumps(example, ensure_ascii=False),
        ])
        return "\n".join(rows)

    def record_methods(self, index, items):
        stage = self.state["stage"]
        if stage not in {"Method", "Component"}:
            raise ValueError("record_methods requires Method or Component State")
        if not isinstance(items, dict) or not items:
            raise ValueError("record_methods.items must be a nonempty object")
        analysis = self.load_method_analysis(index)
        if stage == "Component":
            if items.keys() - analysis.keys():
                raise ValueError("revise only previously reviewed method locators")
        else:
            expected = {
                f"{method['file']}#{method['name']}"
                for method, _ in self.method_batch(index, analysis)
            }
            actual = set(items)
            if actual != expected:
                missing = sorted(expected - actual)
                extra = sorted(actual - expected)
                raise ValueError(
                    "record_methods must cover the current batch exactly; "
                    f"missing={missing}, extra={extra}")
        merged = {**analysis, **items}
        self.validate_method_analysis(
            index, merged, complete=False)
        save(self.method_analysis, encoded(merged) + "\n")
        if stage == "Component":
            return f"OK revised {len(items)} methods; group the updated keep set."
        remaining = len(index["methods"]) - len(merged)
        return (
            f"OK recorded {len(items)} methods; {remaining} remain."
            + (
                " Call advance_stage after reviewing the accumulated decisions."
                if remaining == 0
                else ""
            )
        )

    def record_components(self, index, groups):
        if self.state["stage"] != "Component" or not isinstance(groups, dict):
            raise ValueError("record_components requires a groups object in Component State")
        analysis = self.load_method_analysis(index, complete=True)
        kept = {key for key, item in analysis.items() if item["decision"] == "keep"}
        names = Counter(key.partition("#")[2] for key in kept)
        components = {}
        assigned = set()
        contains = {}
        for label, group in groups.items():
            if (not isinstance(group, dict) or set(group) != {"Description", "methods"}
                    or not isinstance(group["methods"], (dict, list)) or not group["methods"]):
                raise ValueError(f"{label}: provide Description and a nonempty methods map or list")
            methods = {}
            for key in group["methods"]:
                if not isinstance(key, str) or key not in kept or key in assigned:
                    raise ValueError(f"{key}: assign an existing keep locator exactly once")
                file, _, name = key.partition("#")
                display = name if names[name] == 1 else key
                methods[display] = {
                    "Description": analysis[key]["description"], "file_path": file,
                }
                if isinstance(group["methods"], dict):
                    purpose = group["methods"][key]
                    if not concrete(purpose):
                        raise ValueError(f"{key}: describe the member's responsibility")
                    contains[f"{label} → {display}"] = purpose
                assigned.add(key)
            components[label] = {"Description": group["Description"], "Method": methods}
        model = self.load_model(index)
        model["nodes"]["Component"] = components
        model["edges"]["contains"] = contains
        self.validate_model(index, model, complete=False)
        self.validate_component_members(index, model, analysis)
        save(self.draft, encoded(model) + "\n")
        return f"OK grouped {len(assigned)} methods in {len(components)} components."

    def validate_component_members(self, index, model, analysis):
        kept = {
            locator for locator, item in analysis.items()
            if item["decision"] == "keep"
        }
        selected = set()
        for component in model["nodes"]["Component"].values():
            for display, details in component["Method"].items():
                method = self.method_for(
                    index, display, details["file_path"])
                locator = f"{method['file']}#{method['name']}"
                if locator not in kept:
                    raise ValueError(
                        f"{display}: Component members must first be marked "
                        f"keep in method_analysis.json ({locator})")
                selected.add(locator)
        if not selected:
            raise ValueError(
                "Component State must assign at least one kept method")
        missing = sorted(kept - selected)
        if missing:
            raise ValueError(
                "Component State must assign every kept method exactly once; "
                "unassigned=" + ", ".join(missing[:12]))

    def validate_model(self, index, model, *, complete):
        """校验已写内容并重建 contains；不要求覆盖全部源码方法。"""
        expected = skeleton()
        if (
            not isinstance(model, dict)
            or set(model) != set(expected)
            or model.get("schema_version") != expected["schema_version"]
        ):
            raise ValueError(
                "use the existing schema_version, harness, nodes and edges skeleton")
        if (
            not isinstance(model["harness"], str)
            or not isinstance(model["nodes"], dict)
            or set(model["nodes"]) != {"Component"}
            or not isinstance(model["nodes"]["Component"], dict)
        ):
            raise ValueError("harness is text and nodes contains a Component map")
        if complete and not concrete(model["harness"]):
            raise ValueError("write a concrete harness execution overview")

        components = model["nodes"]["Component"]
        edges = model["edges"]
        if (
            not isinstance(edges, dict)
            or set(edges) != {"contains", "depends_on", "influences"}
            or any(not isinstance(edges[kind], dict) for kind in edges)
        ):
            raise ValueError(
                "edges must contain contains, depends_on and influences maps")

        contains = {}
        locations = set()
        method_names = set()
        for label, component in components.items():
            if (
                not concrete(label)
                or not isinstance(component, dict)
                or set(component) != {"Description", "Method"}
                or not concrete(component["Description"])
                or not isinstance(component["Method"], dict)
            ):
                raise ValueError(
                    f"{label!r} requires a concrete Description and Method map")
            if complete and not component["Method"]:
                raise ValueError(f"{label}: select at least one key method")
            contains[f"{self.root.name} → {label}"] = component["Description"]
            for display, details in component["Method"].items():
                if (
                    not concrete(display)
                    or not isinstance(details, dict)
                    or set(details) != {"Description", "file_path"}
                    or not concrete(details["Description"])
                    or not isinstance(details["file_path"], str)
                ):
                    raise ValueError(
                        f"{display}: provide Description and file_path")
                method = self.method_for(
                    index, display, details["file_path"])
                location = method["file"], method["name"]
                if location in locations or display in method_names:
                    raise ValueError(
                        f"{display}: each source method and display name may appear once")
                locations.add(location)
                method_names.add(display)
                edge = f"{label} → {display}"
                purpose = edges["contains"].get(edge)
                contains[edge] = purpose if concrete(purpose) else details["Description"]

        if complete and (not components or not method_names):
            raise ValueError(
                "model at least one component with a key source method")

        endpoints = (
            set(components)
            | method_names
            | set(index["files_by_path"])
            | {self.root.name}
        )
        for kind in ("depends_on", "influences"):
            for edge, description in edges[kind].items():
                left, separator, right = edge.partition(" → ")
                if (
                    not separator
                    or left not in endpoints
                    or right not in endpoints
                    or left == right
                ):
                    raise ValueError(
                        f"{kind} edge {edge!r} must connect two modeled endpoints")
                if not concrete(description):
                    raise ValueError(
                        f"{kind} edge {edge!r} needs a concrete behavior description")
        edges["contains"] = contains
        return model

    def load_model(self, index, *, complete=False):
        model = strict_json(self.draft.read_text(encoding="utf-8"))
        original = encoded(model) + "\n"
        self.validate_model(index, model, complete=complete)
        normalized = encoded(model) + "\n"
        if normalized != original or normalized != self.draft.read_text(encoding="utf-8"):
            save(self.draft, normalized)
        return model

    def advance_stage(self):
        stage = self.state["stage"]
        if stage == "关系":
            raise ValueError("关系 is the final State; call done when ready")
        if stage == "源码解析":
            if not self.index_path.is_file():
                raise ValueError("call parse_source before leaving 源码解析")
        else:
            index = self.read_index()
            analysis = self.load_method_analysis(
                index, complete=stage in {"Method", "Component", "harness"})
            model = self.load_model(index)
            if stage == "Method":
                pass
            elif stage == "Component":
                self.validate_component_members(
                    index, model, analysis)
            elif stage == "harness":
                self.validate_component_members(
                    index, model, analysis)
                if not concrete(model["harness"]):
                    raise ValueError(
                        "harness State requires a concrete Harness Description")
        next_stage = STAGES[STAGES.index(stage) + 1]
        self.state["stage"] = next_stage
        self.check()
        return f"OK advanced State: {stage} → {next_stage}"

    def back_stage(self):
        stage = self.state["stage"]
        position = STAGES.index(stage)
        if position <= STAGES.index("Method"):
            raise ValueError(
                "Method can still read all source; there is no earlier analysis State")
        previous = STAGES[position - 1]
        self.state["stage"] = previous
        self.check()
        return f"OK returned State: {stage} → {previous}"

    def relation_work(self, index, model):
        """Batch by the consuming method so both edge directions have one owner."""
        methods = {
            display: self.method_for(index, display, details["file_path"])
            for component in model["nodes"]["Component"].values()
            for display, details in component["Method"].items()
        }
        # An edited node/contract invalidates previous reviews, including on resume.
        basis = self.node_version(model)
        reviewed = (self.state.get("relation_reviewed", [])
                    if self.state.get("relation_basis") == basis else [])
        batch = []
        chars = 0
        for display, method in methods.items():
            if display in reviewed:
                continue
            size = method["end"] - method["start"]
            if batch and (len(batch) >= RELATION_BATCH_SIZE or chars + size > METHOD_BATCH_CHARS):
                break
            batch.append(display)
            chars += size
        return methods, basis, reviewed, batch

    @staticmethod
    def node_version(model):
        return hashlib.sha256(encoded(model["nodes"]).encode()).hexdigest()

    def refresh_method_review(self, before, after):
        """Reopen corrected methods and their neighbors for either kind of JSON edit."""
        if self.state.get("relation_basis") != self.node_version(before):
            return
        previous = before["nodes"]["Component"]
        current = after["nodes"]["Component"]
        if previous.keys() != current.keys():
            return
        changed = set()
        for label, component in current.items():
            old = previous[label]
            if (old["Description"] != component["Description"]
                    or old["Method"].keys() != component["Method"].keys()):
                return
            for name, item in component["Method"].items():
                if old["Method"][name]["file_path"] != item["file_path"]:
                    return
                if old["Method"][name]["Description"] != item["Description"]:
                    changed.add(name)
        if not changed:
            return
        self.include_downstream_effects(before)
        affected = changed.copy()
        for kind in ("depends_on", "influences"):
            for edge in before["edges"][kind]:
                endpoints = edge.split(" → ")
                if changed.intersection(endpoints):
                    affected.update(endpoints)
        self.state["relation_basis"] = self.node_version(after)
        self.state["relation_reviewed"] = [
            n for n in self.state["relation_reviewed"] if n not in affected]
        self.state["downstream_effects"] = {
            n: v for n, v in self.state.get("downstream_effects", {}).items() if n not in affected}
        self.check()

    def relation_context(self, index, model):
        methods, _, reviewed, batch = self.relation_work(index, model)
        rows = [f"关系审阅：{len(reviewed)}/{len(methods)}。"]
        if not batch:
            self.include_downstream_effects(model)
            return rows[0] + "\n" + self.relation_menu(index, model, methods)
        rows.append("本批消费方：" + ", ".join(batch))
        rows.append("核实这些方法用到谁、受谁影响。作用来自相邻调用之间转交的值或先前写入的状态时，也要定位真实上游。")
        labels = {(m["file"], m["name"]): name for name, m in methods.items()}
        rows.append("已归组方法的调用位置（可选实现需核实触发条件）：")
        siblings = {}
        field_sites = {}
        callers = {labels[(call["from_file"], call["from"])] for call in index["calls"]
                   if (call["from_file"], call["from"]) in labels
                   and labels.get((call["to_file"], call["to"])) in batch}
        for call in index["calls"]:
            caller = labels.get((call["from_file"], call["from"]))
            callee = labels.get((call["to_file"], call["to"]))
            if not caller or not callee or caller == callee:
                continue
            assigned = call.get("assignment", "")
            callee_site = f"{callee}（赋值给 {assigned}）" if assigned else callee
            siblings.setdefault(caller, {}).setdefault(callee_site, None)
            if re.fullmatch(r"self\.\w+", assigned):
                field_sites.setdefault((call["from_file"], methods[caller]["owner"], assigned),
                                       []).append((callee, call))
            if caller in batch or callee in batch or caller in callers or callee in callers:
                kind = "调用" if call["resolved"] else "候选分派"
                rows.append(f"- {caller} → {callee} [{kind} {call['from_file']}:{call['line']}] "
                            f"{call['code'][:180]}")
        rows.append("包内接续线索（按源码位置，同场出现的调用可能位于不同分支）：")
        rows.extend(f"- {caller} 使用 " + "、".join(callees)
                    for caller, callees in siblings.items() if caller not in batch)
        for name in batch:
            m = methods[name]
            source = self.source_document(index, m["file"])["text"][m["start"]:m["end"]]
            for field, line in m.get("field_uses", {}).items():
                for callee, call in field_sites.get((m["file"], m["owner"], field), ()):
                    if callee != name and call["from"] != m["name"]:
                        rows.append(f"同一字段 {field}：{m['file']}:{call['line']} 的赋值用到 {callee}；"
                                    f"{name} 在 L{line} 使用该字段。核实分支与实际内容。")
            rows.append(f"===== {name} | {m['file']}:{m['line']}-{m['last']} =====\n{source}")
        rows.append("本批各方法提交 depends_on、influenced_by。若沿源码找到跨边界消费点，"
                    "用可选 affects 写出从本方法到它的影响。端点使用实际方法显示名。")
        return "\n".join(rows)

    @staticmethod
    def edge_version(edges):
        inventory = {kind: edges[kind] for kind in ("depends_on", "influences")}
        return hashlib.sha256(encoded(inventory).encode()).hexdigest()[:12]

    def relation_menu(self, index, model, methods):
        edges = model["edges"]
        version = self.edge_version(edges)
        rows = [
            "已逐方法审阅。沿概览走一次实际运行，选择能让读者从轨迹现象回到源码的关系。",
            "depends_on 保留各行为端点实际使用的实现，含有效的可选分派。"
            "已由具体方法定位的资源、重复的跨层调用可省略。",
            "influences 按证据分组。先从实际消费返回值的边中保留有区分度的行为："
            "同一调用在不同条件下给消费方不同的输入、动作、错误反馈、观察内容、退出决定或记录。"
            "这样的反向影响应与正向依赖同时保留。经过中介时保留每个真正改变值或作决定的消费点，"
            "别跳过这些点把所有上游直连调度器。",
            "同一来源有多个真实消费者时，首轮输入、普通回合反馈、异常恢复、持久化产物是不同的"
            "可见结果；分别保留相应的直接交接。按配置互换的实现也各有真实的协议结果。"
            "区分只传常规参数与调用者构造了决定下一阶段内容的输入，区分丢弃返回值与就地改变输出文件。",
            "再补直接调用解释不了的变化：同一调度者把某一方的具体产物交给另一方、先前写入的模式"
            "改变实际选择、跨轮反馈、更深层的返回类型或产物归属。对跨层项核实来源、载体、消费点。"
            "仅把配置保存到字段、调用者传入常规参数、调用成功或通用异常能传播、初始化对象存在、"
            "计量汇总等写在节点或依赖中即可。同一条数据的多条传递路径不要展开为传递闭包。"
            "可继续看源码或补改边再提交。",
            "将 influences 精简为解释一次任务推进所需的最小行为图。先找真正消费结果的决策点，"
            "再为每个独立分支保留最能定位它的来源。协议转换、条件选择、内容裁剪等中介若改变了数据，"
            "要保留中介两侧不同的交接。一般准备或内部辅助实现可以没有任何影响入边。"
            "行为图通常比实现依赖图更稀疏，若仍很密，检查是否保留了能由同一效果解释的多层祖先。",
            '用 {"action":"done","selection":{"version":"' + version
            + '","depends_on":"all","influences":[...]}} 提交。两类均用本栏编号数组，depends_on 也可用 "all"；'
            "逐条按语义选择，无固定数量。两栏编号各自从 1 开始。",
        ]
        rows.append("--- depends_on ---")
        for number, (edge, detail) in enumerate(edges["depends_on"].items(), 1):
            rows.append(f"{number}. {edge} | {detail[:160]}")
        labels = {(m["file"], m["name"]): name for name, m in methods.items()}
        sites = {}
        for call in index["calls"]:
            pair = labels.get((call["from_file"], call["from"])), labels.get((call["to_file"], call["to"]))
            if all(pair) and (pair not in sites or call.get("result_used")):
                sites[pair] = call
        headings = ("消费方读取来源的返回值，检查分支与结果",
                    "消费方调用来源，检查实际可见的副作用或异常",
                    "来源调用消费方，仅在传入内容确实改变其行为时保留",
                    "无直接调用线索，核实跨方法的具体载体与消费点")
        groups = {heading: [] for heading in headings}
        for number, (edge, detail) in enumerate(edges["influences"].items(), 1):
            source, target = edge.split(" → ")
            consumer = sites.get((target, source))
            caller = sites.get((source, target))
            site = consumer or caller
            if consumer and consumer.get("result_used"):
                group = headings[0]
            elif consumer:
                group = headings[1]
            elif caller:
                group = headings[2]
            else:
                group = headings[3]
            location = f" [{site['from_file']}:{site['line']} {site['code'][:85]}]" if site else ""
            groups[group].append(f"{number}. {edge}{location} | {detail[:160]}")
        for group, items in groups.items():
            if items:
                rows.extend([f"--- influences · {group} ---", *items])
        return "\n".join(rows)

    def select_edges(self, model, selection):
        edges = model["edges"]
        if (not isinstance(selection, dict)
                or set(selection) != {"version", "depends_on", "influences"}
                or selection["version"] != self.edge_version(edges)):
            raise ValueError("use the version and both lists from the current relation menu")
        for kind in ("depends_on", "influences"):
            numbers = selection[kind]
            if kind == "depends_on" and numbers == "all":
                continue
            if (not isinstance(numbers, list) or any(
                    type(n) is not int or not 1 <= n <= len(edges[kind]) for n in numbers)
                    or len(set(numbers)) != len(numbers)):
                raise ValueError(f"{kind}: select unique numbers from the current menu")
            selected = set(numbers)
            edges[kind] = {edge: detail for n, (edge, detail) in enumerate(edges[kind].items(), 1)
                           if n in selected}

    def include_downstream_effects(self, model):
        # Keep producer-side observations separate until all consumer batches
        # are complete. Otherwise the later of the two reviews can erase a
        # cross-step effect found by the earlier one.
        influences = model["edges"]["influences"]
        for source, effects in self.state.get("downstream_effects", {}).items():
            for target, detail in effects.items():
                influences.setdefault(f"{source} → {target}", detail)

    def record_relations(self, index, items):
        if self.state["stage"] != "关系" or not isinstance(items, dict):
            raise ValueError("record_relations requires an items object in 关系 State")
        model = self.load_model(index)
        methods, basis, reviewed, batch = self.relation_work(index, model)
        if not batch or set(items) != set(batch):
            raise ValueError(f"review the current relation batch exactly: {batch}")
        downstream = dict(self.state.get("downstream_effects", {})) if reviewed else {}
        # The batch owns outgoing dependencies and incoming effects of its consumers.
        # Re-reviewing after a contract change must also remove withdrawn claims.
        for kind in ("depends_on", "influences"):
            current = model["edges"][kind]
            owner = 0 if kind == "depends_on" else 1
            model["edges"][kind] = {
                edge: detail for edge, detail in current.items()
                if edge.split(" → ")[owner] not in items
            }
        for name, item in items.items():
            required = {"depends_on", "influenced_by"}
            if not isinstance(item, dict) or not required <= item.keys() <= required | {"affects"}:
                raise ValueError(f"{name}: provide depends_on, influenced_by and optional affects objects")
            downstream.pop(name, None)
            for field, relations in item.items():
                if not isinstance(relations, dict):
                    raise ValueError(f"{name}.{field} must map other endpoints to descriptions")
                if field == "affects":
                    for target, detail in relations.items():
                        if target not in methods or target == name or not concrete(detail):
                            raise ValueError(f"{name}.affects: name another modeled method and its concrete change")
                    if relations:
                        downstream[name] = relations
                    continue
                outgoing = field == "depends_on"
                kind = "depends_on" if outgoing else "influences"
                for other, description in relations.items():
                    if other not in methods and other not in index["files_by_path"]:
                        raise ValueError(f"{other}: choose a modeled method or indexed resource")
                    edge = f"{name} → {other}" if outgoing else f"{other} → {name}"
                    model["edges"][kind][edge] = description
        self.validate_model(index, model, complete=False)
        save(self.draft, encoded(model) + "\n")
        self.state["relation_basis"] = basis
        self.state["relation_reviewed"] = [*reviewed, *batch]
        self.state["downstream_effects"] = downstream
        self.check()
        return f"OK reviewed relations for {len(reviewed) + len(batch)}/{len(methods)} methods."

    def method_summary(self, analysis, *, include_skips=False):
        kept = [
            (locator, item["description"])
            for locator, item in analysis.items()
            if item["decision"] == "keep"
        ]
        skipped = sum(
            item["decision"] == "skip" for item in analysis.values())
        rows = [
            f"Method 分析：keep {len(kept)}，skip {skipped}。"
        ]
        for locator, description in kept:
            if len(description) > 450:
                description = description[:260] + "…" + description[-180:]
            rows.append(f"- {locator}: {description}")
        if not kept:
            rows.append("- 尚无 keep 方法。")
        if include_skips:
            rows.append("各批暂列 skip 的实现，可在全局复核时重新判断：")
            rows.extend(f"- {locator}: {item['description'][:160]}"
                        for locator, item in analysis.items() if item["decision"] == "skip")
        rows.append(
            "精确读取：pointer=/<相对文件#限定方法名>，文件路径中的 / 转义为 ~1。")
        return "\n".join(rows)

    def model_summary(self, model, *, show_edge_keys=True):
        """给每轮 context 的结构导航；完整值通过 read + JSON pointer 按需取得。"""
        harness = model["harness"].strip()
        rows = [
            "harness: " + (
                harness[:1200] + ("…" if len(harness) > 1200 else "")
                if harness
                else "（未填写）"
            )
        ]
        components = model["nodes"]["Component"]
        rows.append(f"Component ({len(components)}):")
        for label, component in components.items():
            escaped = label.replace("~", "~0").replace("/", "~1")
            description = component["Description"]
            if len(description) > 240:
                description = description[:240] + "…"
            rows.append(
                f"- {label}: {description} "
                f"[pointer=/nodes/Component/{escaped}]")
            methods = component["Method"]
            if methods:
                rows.append(
                    "  Method: "
                    + ", ".join(
                        f"{name} ({details['file_path']})"
                        for name, details in methods.items()
                    )
                )
        for kind in ("depends_on", "influences"):
            edges = model["edges"][kind]
            names = list(edges)
            rows.append(f"{kind} ({len(names)}):")
            if not show_edge_keys:
                continue
            rows.extend(f"- {edge}" for edge in names[:100])
            if len(names) > 100:
                rows.append(
                    f"- …其余 {len(names) - 100} 条用 "
                    f"pointer=/edges/{kind} 查看")
        rows.append(
            "精确读取示例："
            '{"action":"read","scope":"work","path":"harness_model.json",'
            '"pointer":"/nodes/Component/<组件名>"}')
        return "\n".join(rows)

    def context(self):
        history = "\n\n".join(self.state.get("history", [])) or "（尚无动作结果）"
        history_prompt = (
            history if len(history) <= 20_000
            else "…较早工具结果已由结构化产物替代…\n" + history[-20_000:]
        )
        if not self.index_path.is_file():
            self.state["stage"] = "源码解析"
            return (
                f"只读源码根目录：{self.root}\n"
                "当前 State：源码解析\n"
                '当前产出：调用 {"action":"parse_source"} 建立 Source Index。\n'
                f"交互记录：\n{history_prompt}"
            )
        index = self.read_index()
        model = self.load_model(index)
        analysis = self.load_method_analysis(index)
        files = "\n".join(
            f"- {item['path']} ({item['lines']} lines"
            + (f", {item['warning']}" if item["warning"] else "")
            + ")"
            for item in index["files"][:400]
        )
        if len(index["files"]) > 400:
            files += f"\n- … 其余 {len(index['files']) - 400} 个文件用 list_files 查看"
        stage = self.state["stage"]
        stage_output = {
            "源码解析": "Source Index；解析完成后会自动进入 Method。",
            "Method": "逐批覆盖索引中的全部方法；全部判断完成后调用 advance_stage。",
            "Component": "补充 Component 及成员 Method；充分时调用 advance_stage。",
            "harness": "补充 Harness Description；充分时调用 advance_stage。",
            "关系": "按本批具体方法 record_relations；全图审阅完毕后调用 done。",
        }[stage]
        query_streak = self.state.get("query_streak", 0)
        progress = ""
        if query_streak >= MAX_QUERY_STREAK:
            progress = (
                f"\n推进约束：已经连续 {query_streak} 轮只查询。下一动作必须是 "
                "当前阶段的提交、edit_file、advance_stage、back_stage 或 done；继续查询会被拒绝。\n"
            )
        elif stage == "Method" and any(
                item["decision"] == "keep" for item in analysis.values()):
            progress = (
                "\n推进提示：已有 keep 方法；继续处理 Controller 给出的下一批。"
                "只有全部索引方法均有 keep/skip 判断后才能 advance_stage。\n"
            )
        if stage == "Method":
            return (
                f"只读源码根目录：{self.root}\n\n"
                "当前 State：Method\n"
                f"当前产出：{stage_output}\n"
                f"{progress}\n"
                f"{self.method_batch_context(index, analysis)}\n\n"
                f"最近工具结果：\n{history_prompt}\n"
            )
        if stage == "关系":
            evidence = self.relation_context(index, model)
        elif stage == "Component":
            evidence = self.component_context(index, analysis)
        else:
            evidence = "包内接线线索：\n" + self.module_context(index)
        return (
            f"只读源码根目录：{self.root}\n\n"
            f"当前 State：{stage}\n"
            f"当前产出：{stage_output}\n"
            "State 只表示主要工作方向，可继续读取源码并修正已有产物。"
            f"{progress}\n"
            f"源码文件：\n{files}\n\n"
            f"Method 分析摘要：\n{self.method_summary(analysis, include_skips=stage in {'Component', 'harness'})}\n\n"
            f"当前模型摘要：\n{self.model_summary(model, show_edge_keys=stage != '关系')}\n\n"
            f"最近工具结果：\n{history_prompt}\n\n{evidence}\n\n"
            "请选择一个最能推进当前模型的 JSON action。"
        )

    def find_references(self, index, query, direction):
        file_query, separator, symbol = query.partition("#")
        needle = symbol if separator else file_query

        def matches(file, name):
            if separator and file != file_query:
                return False
            return needle in {name, name.split(".")[-1].split("@")[0]}

        rows = []
        if direction != "text":
            for call in index["calls"]:
                caller = call["from_file"], call["from"]
                callee = call["to_file"], call["to"]
                include = (
                    direction in {"any", "callers"} and matches(*callee)
                ) or (
                    direction in {"any", "callees"} and matches(*caller)
                )
                if include:
                    kind = "resolved" if call["resolved"] else "candidate"
                    rows.append(
                        f"{kind} {call['from_file']}:{call['line']} "
                        f"{call['from']} → {call['to']} | {call['code']}"
                    )
        if not rows or direction in {"any", "text"}:
            text_needle = Path(needle).name if needle in index["files_by_path"] else needle
            for file in index["files_by_path"]:
                target = self.root / file
                if not target.is_file():
                    continue
                for line_number, line in enumerate(
                        self.source_document(index, file)["lines"], 1):
                    if needle in line or text_needle in line:
                        rows.append(
                            f"text {file}:{line_number} | {line.strip()}")
        return "\n".join(dict.fromkeys(rows)) or "No matching source reference."

    def edit_artifact(self, index, path, old_text, new_text, pointer=None):
        target = self.work_path(path)
        if target not in {self.draft, self.method_analysis}:
            raise ValueError(
                "edit_file may only modify method_analysis.json or harness_model.json")
        original = target.read_text(encoding="utf-8")
        if pointer is None:
            if not isinstance(old_text, str) or not isinstance(new_text, str):
                raise ValueError(
                    "text replacement requires string old_text and new_text")
            matches = original.count(old_text)
            if matches != 1:
                raise ValueError(
                    f"old_text matched {matches} times in {target.name}")
            changed = original.replace(old_text, new_text, 1)
            where = f"char {original.find(old_text)}"
        else:
            if not isinstance(pointer, str) or not pointer.startswith("/"):
                raise ValueError("pointer must be an absolute JSON pointer")
            keys = [
                key.replace("~1", "/").replace("~0", "~")
                for key in pointer[1:].split("/")
            ]
            document = strict_json(original)
            parent = document
            for key in keys[:-1]:
                if not isinstance(parent, dict) or key not in parent:
                    raise ValueError(f"missing parent of {pointer}")
                parent = parent[key]
            key = keys[-1]
            if not isinstance(parent, dict):
                raise ValueError(f"parent of {pointer} is not an object")
            exists = key in parent
            expected = old_text
            if exists and parent[key] != expected and isinstance(expected, str):
                try:
                    decoded = strict_json(expected)
                except (json.JSONDecodeError, ValueError):
                    decoded = expected
                if parent[key] == decoded:
                    expected = decoded
            if (exists and parent[key] != expected) or (
                    not exists and old_text is not None):
                actual = parent.get(key, "<missing>")
                raise ValueError(
                    f"old_text does not match at {pointer}; current value is "
                    f"{encoded(actual)[:500]}")
            parent[key] = new_text
            changed = encoded(document) + "\n"
            where = pointer
        if changed == original:
            raise ValueError("replacement makes no change")
        document = strict_json(changed)
        if target == self.draft:
            self.validate_model(index, document, complete=False)
        else:
            self.validate_method_analysis(
                index, document, complete=False)
        save(target, encoded(document) + "\n")
        if target == self.draft and self.state.get("relation_reviewed"):
            before = strict_json(original)
            if before["nodes"] != document["nodes"]:
                self.refresh_method_review(before, document)
        return f"OK edited {target.name} at {where}"

    def read_work(self, path, cursor=None, pointer=None):
        target = self.work_path(path)
        if not target.is_file():
            raise ValueError(f"no work file {path}")
        text = target.read_text(encoding="utf-8")
        if pointer is not None:
            if not isinstance(pointer, str) or not pointer.startswith("/"):
                raise ValueError("pointer must be an absolute JSON pointer")
            value = strict_json(text)
            for raw_key in pointer[1:].split("/"):
                key = raw_key.replace("~1", "/").replace("~0", "~")
                if isinstance(value, dict) and key in value:
                    value = value[key]
                elif isinstance(value, list) and key.isdigit() and int(key) < len(value):
                    value = value[int(key)]
                else:
                    raise ValueError(f"no JSON value at {pointer}")
            text = encoded(value)
        return paginate(text, cursor)[0]

    def dispatch(self, action):
        """显式 action 白名单；模型内容永远不会作为 Python 名称执行。"""
        name = action["action"]
        path = action.get("path", "")
        cursor = action.get("cursor")
        if (
            name in {"list_files", "read", "find_references"}
            and self.state.get("query_streak", 0) >= MAX_QUERY_STREAK
        ):
            raise ValueError(
                "query budget reached; write the current analysis or advance/back "
                "the State before requesting more source")
        if name == "parse_source":
            result = parse_source(self.root, self.work)
            if self.state["stage"] == "源码解析":
                self.state["stage"] = "Method"
                self.check()
                result += "\nOK advanced State: 源码解析 → Method"
            return result
        if name == "advance_stage":
            return self.advance_stage()
        if name == "back_stage":
            return self.back_stage()
        if name == "list_files" and action.get("scope") == "work":
            directory = self.work if path in {"", "."} else self.work_path(path)
            if not directory.is_dir():
                raise ValueError("choose a work directory")
            rows = sorted(
                item.relative_to(self.work).as_posix()
                for item in directory.rglob("*")
                if item.is_file()
            )
            return paginate("\n".join(rows), cursor)[0]
        if name == "read" and action.get("scope") == "work":
            return self.read_work(path, cursor, action.get("pointer"))

        index = self.read_index()
        files = index["files_by_path"]
        if name == "record_methods":
            return self.record_methods(index, action["items"])
        if name == "record_components":
            return self.record_components(index, action["groups"])
        if name == "record_relations":
            return self.record_relations(index, action["items"])
        if name == "edit_file":
            return self.edit_artifact(
                index,
                path,
                action["old_text"],
                action["new_text"],
                action.get("pointer"),
            )
        if name == "list_files":
            if path in files:
                rows = [
                    f"{method['file']}#{method['name']} "
                    f"lines={method['line']}-{method['last']} "
                    f"{method['signature']}"
                    for method in index["methods"]
                    if method["file"] == path
                ]
                rows.insert(
                    0,
                    f"{path} ({files[path]['chars']} chars) "
                    f"{files[path]['warning'] or ''}",
                )
            else:
                prefix = "" if path in {"", "."} else path.rstrip("/") + "/"
                rows = [
                    file for file in files if file.startswith(prefix)]
                if not rows:
                    raise ValueError(f"no indexed files for {path}")
            return paginate("\n".join(rows), cursor)[0]
        if name == "find_references":
            query = action["query"].strip()
            if not query:
                raise ValueError("provide a symbol or text to search for")
            return paginate(
                self.find_references(
                    index, query, action.get("direction", "any")),
                cursor,
            )[0]
        if name == "read":
            scope = action.get("scope", "source")
            if (
                "scope" not in action
                and path not in files
                and path.partition("#")[0] not in files
                and self.work_path(path).is_file()
            ):
                scope = "work"
            if scope == "work":
                return self.read_work(
                    path, cursor, action.get("pointer"))
            if action.get("pointer") is not None:
                raise ValueError(
                    "JSON pointer is only valid when reading a work JSON file")
            file, method_separator, method_name = path.partition("#")
            bounds = None
            if not method_separator and file not in files:
                source, line_separator, anchor = file.rpartition(":")
                span = re.fullmatch(r"L?(\d+)(?:-L?(\d+))?", anchor)
                if line_separator and source in files and span:
                    first = int(span[1])
                    last = int(span[2] or span[1])
                    if not 1 <= first <= last <= files[source]["lines"]:
                        raise ValueError("source line range outside file")
                    document = self.source_document(index, source)
                    file = source
                    bounds = (
                        document["starts"][first - 1],
                        document["starts"][last],
                    )
            if file not in files:
                raise ValueError(f"no indexed source file {file}")
            low, high = bounds or (0, files[file]["chars"])
            if method_separator:
                method = self.method_for(index, method_name, file)
                low, high = method["start"], method["end"]
            document = self.source_document(index, file)
            content, start, end = paginate(
                document["text"], cursor, low, high)
            return (
                f"{file}:{_line_at(document['starts'], start)} {content}"
                + (
                    f"\nsource_range=[{file},{start},{end})"
                    if end < high
                    else ""
                )
            )
        raise ValueError(f"unknown or terminal action {name}")

    def publish(self, selection=None):
        if self.state["stage"] != "关系":
            raise ValueError(
                f"done is only available in 关系 State; current State is "
                f"{self.state['stage']}")
        index = self.read_index()
        analysis = self.load_method_analysis(index, complete=True)
        model = self.load_model(index, complete=True)
        self.validate_component_members(index, model, analysis)
        methods, _, reviewed, batch = self.relation_work(index, model)
        if batch:
            # A small manually edited model can submit its single visible batch
            # with done if every method participates in a method-level relation.
            manual = set()
            for kind in ("depends_on", "influences"):
                for edge in model["edges"][kind]:
                    left, right = edge.split(" → ")
                    if left in methods and right in methods:
                        manual.update((left, right))
            if reviewed or set(batch) != set(methods) or manual != set(methods):
                raise ValueError(f"relation review incomplete; use record_relations for {batch}")
        else:
            self.include_downstream_effects(model)
        if selection is not None:
            self.select_edges(model, selection)
        elif len(methods) > RELATION_BATCH_SIZE and reviewed:
            raise ValueError("submit done with selection from the final relation menu")
        root = index["root"]
        if root not in model["harness"] and str(self.root) not in model["harness"]:
            model["harness"] = f"源码根目录为 {root}/。" + model["harness"]
        self.validate_model(index, model, complete=True)
        contents = encoded(model) + "\n"
        # Keep the complete, numbered draft available until publication succeeds.
        save(self.dir / "harness_model.json", contents)

    def run(self, generate):
        published = self.dir / "harness_model.json"
        self.state["status"] = "running"
        self.state.pop("reason", None)
        self.check()
        while self.state["turns"] < MAX_TURNS:
            try:
                stage = self.state["stage"]
                system = system_prompt(self.root, stage)
                prompt = self.context()
            except (ValueError, KeyError, OSError) as exc:
                return self.stop(
                    "abort", f"cannot build modeling context: {exc}")

            for attempt in range(2):
                try:
                    raw = generate(
                        prompt, system=system, cache_prefix=system)
                    if not isinstance(raw, str):
                        raise TypeError("generate must return a string")
                    break
                except Exception as exc:
                    if attempt:
                        return self.stop("abort", f"model backend: {exc}")

            self.state["turns"] += 1
            action = {"action": "invalid"}
            terminal = None
            try:
                action = parse_action(raw)
                name = action["action"]
                if name == "done":
                    self.publish(action.get("selection"))
                    result = "OK published harness_model.json"
                    terminal = "done"
                elif name == "abort":
                    if not action["reason"].strip():
                        raise ValueError(
                            "give the blocker and what you tried")
                    result = action["reason"]
                    terminal = "abort"
                else:
                    result = self.dispatch(action)
            except (
                    json.JSONDecodeError,
                    ValueError,
                    KeyError,
                    TypeError,
                    OSError) as exc:
                result = f"ERROR {exc}"
                action = action if action["action"] != "invalid" else {
                    "action": "invalid",
                    "reply_excerpt": raw[:500],
                }

            if action["action"] in {"list_files", "read", "find_references"}:
                self.state["query_streak"] = (
                    self.state.get("query_streak", 0) + 1)
            elif action["action"] != "invalid":
                self.state["query_streak"] = 0
            self.remember(action, result)
            record = {
                "turn": self.state["turns"],
                "stage": stage,
                "action": action["action"],
                "target": action.get("path", action.get("query", "")),
                "result": str(result)[:650],
            }
            if getattr(generate, "last_model", None):
                record["model"] = generate.last_model
            with self.log_path.open("a", encoding="utf-8") as log:
                log.write(json.dumps(record, ensure_ascii=False) + "\n")

            if terminal:
                return self.stop(
                    terminal,
                    str(result),
                    published if terminal == "done" else None,
                )
            self.check()
        return self.stop(
            "max_turns", f"total turn limit {MAX_TURNS} reached")

    def stop(self, status, reason, path=None):
        self.state["status"] = status
        self.state["reason"] = reason
        if status == "done":
            shutil.rmtree(self.work)
            self.checkpoint.unlink(missing_ok=True)
            self.log_path.unlink(missing_ok=True)
        else:
            self.check()
        return LoopResult(status, self.state["turns"], path, reason)


def run_modeling(harness_root, run_dir, generate, *, resume=False):
    """运行或恢复一次 Stage 1 建模。"""
    root = Path(harness_root).resolve(strict=True)
    directory = Path(run_dir).resolve()
    if (
        not root.is_dir()
        or root.is_relative_to(directory)
        or directory.is_relative_to(root)
    ):
        raise ValueError(
            "choose a source directory and a separate run directory")
    directory.mkdir(parents=True, exist_ok=True)
    published = directory / "harness_model.json"
    if resume and published.is_file():
        return LoopResult("done", 0, published)
    return AgentRun(root, directory, resume).run(generate)


def chat_backend(
        base_url,
        model,
        api_key=None,
        *,
        json_mode=False,
        max_tokens=4096,
        timeout=None):
    """适配 OpenAI-compatible chat/completions。"""
    host = urllib.parse.urlsplit(base_url).hostname
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    opener = (
        urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if loopback
        else urllib.request.build_opener()
    )
    if timeout is None:
        timeout = 900 if loopback else 180

    def generate(prompt, *, system, cache_prefix):
        del cache_prefix
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        request = urllib.request.Request(
            base_url.rstrip("/") + "/chat/completions",
            body,
            headers,
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read(600).decode(errors="replace")
            raise RuntimeError(
                f"model HTTP {exc.code}: {detail}") from exc
        generate.last_model = result.get("model", model)
        choice = result["choices"][0]
        message = choice["message"].get("content")
        if not isinstance(message, str) or not message.strip():
            raise ValueError(
                f"{generate.last_model} returned no action text "
                f"(finish_reason={choice.get('finish_reason')})")
        return message

    generate.last_model = model
    return generate


def codex_backend(model=None, *, timeout=450):
    """用已登录的 Codex CLI 建立无原生工具的单轮动作后端。"""
    executable = (
        os.environ.get("CODEX_APP_RUNTIME_CLI_PATH")
        or shutil.which("codex")
    )
    if not executable:
        raise ValueError(
            "codex CLI not found; install it or use --backend chat")

    prefer_https = False

    def generate(prompt, *, system, cache_prefix):
        nonlocal prefer_https
        del cache_prefix
        # Each action starts a fresh CLI. Remember a successful transport
        # fallback for this run instead of negotiating the failing socket again.
        use_https, prefer_https = prefer_https, False
        with tempfile.TemporaryDirectory(
                prefix="traceweaver-model-") as directory:
            temporary = Path(directory)
            instructions = temporary / "system.txt"
            output = temporary / "reply.txt"
            instructions.write_text(
                system
                + "\n只使用本轮输入材料，直接输出单个 JSON action。"
                + "工具由外层 loop 执行。",
                encoding="utf-8",
            )
            command = [
                executable,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "-s",
                "read-only",
                "--json",
                "-o",
                str(output),
            ]
            if model:
                command.extend(["-m", model])
            for feature in (
                    "apps",
                    "code_mode_host",
                    "multi_agent",
                    "shell_tool",
                    "unified_exec",
                    "plugins",
                    "memories"):
                command.extend(["--disable", feature])
            settings = [
                'model_reasoning_effort="medium"',
                'web_search="disabled"',
                "project_doc_max_bytes=0",
                "model_instructions_file=" + json.dumps(str(instructions)),
            ]
            if use_https:
                settings.extend([
                    'model_provider="traceweaver_https"',
                    'model_providers.traceweaver_https.name="OpenAI"',
                    "model_providers.traceweaver_https.requires_openai_auth=true",
                    "model_providers.traceweaver_https.supports_websockets=false",
                ])
            for setting in settings:
                command.extend(["-c", setting])
            result = subprocess.run(
                [*command, "-"],
                input=prompt,
                cwd=temporary,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode or not output.is_file():
                raise RuntimeError(
                    "model CLI failed: " + result.stderr[-650:])
            for line in result.stdout.splitlines():
                event = json.loads(line)
                if event.get("type") in {"item.started", "item.completed"}:
                    item = event.get("item", {})
                    kind = item.get("type")
                    if kind not in {"agent_message", "reasoning", "error"}:
                        raise RuntimeError(
                            "model backend invoked an out-of-protocol "
                            f"native tool: {kind}")
                    if kind == "error" and "Falling back from WebSockets to HTTPS" in item.get("message", ""):
                        use_https = True
            reply = output.read_text(encoding="utf-8")
            prefer_https = use_https
            return reply

    generate.last_model = model or "codex-default"
    return generate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("harness_root")
    parser.add_argument("run_dir")
    parser.add_argument(
        "--backend", choices=("chat", "codex"), default="chat")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL"))
    parser.add_argument(
        "--base-url", default=os.environ.get("OPENAI_BASE_URL"))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--json-mode", action="store_true")
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--timeout", type=int)
    arguments = parser.parse_args()
    if arguments.backend == "chat" and (
            not arguments.model or not arguments.base_url):
        parser.error(
            "chat backend requires --model and --base-url "
            "(or OPENAI_MODEL and OPENAI_BASE_URL)")
    generate = (
        codex_backend(
            arguments.model, timeout=arguments.timeout or 450)
        if arguments.backend == "codex"
        else chat_backend(
            arguments.base_url,
            arguments.model,
            os.environ.get("OPENAI_API_KEY"),
            json_mode=arguments.json_mode,
            max_tokens=arguments.max_output_tokens,
            timeout=arguments.timeout,
        )
    )
    result = run_modeling(
        arguments.harness_root,
        arguments.run_dir,
        generate,
        resume=arguments.resume,
    )
    print(json.dumps({
        "status": result.status,
        "turns": result.n_turns,
        "path": str(result.path) if result.path else None,
        "reason": result.reason,
    }, ensure_ascii=False))
    return 0 if result.status == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
