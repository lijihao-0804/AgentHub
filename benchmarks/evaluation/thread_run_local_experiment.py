"""Controlled local M4 Thread/Run experiment.

Running the module without flags performs a read-only dry run. A provider call
requires both ``--execute`` and ``--accept-provider-dispatch`` plus the named
isolated database, the pinned local tokenizer, and explicit local Aliyun
configuration. This runner is for fictional Thread history only; it never uses
the business database or sends messages through the product API.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from dotenv import dotenv_values
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from benchmarks.evaluation.deepseek_v41_summary_experiment import (
    DEFAULT_TOKENIZER_PATH,
    DeepseekV41SummaryExperimentGate,
    MemoryTrialBudgetAccountingHook,
    register_local_v2_summary_version,
)
from benchmarks.evaluation.memory_trial_budget import (
    LOCAL_ROUND_ATTEMPT_LIMIT,
    LOCAL_SCOPE_ATTEMPT_LIMIT,
    RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT,
    MemoryTrialBudget,
    upper_bound_cost_cny,
)
from benchmarks.evaluation.memory_trial_client import PRICE_IDENTITY
from packages.agent_runtime.adapters.langgraph import (
    LangGraphCheckpointAdapter,
    configure_windows_asyncio_policy,
)
from packages.agent_runtime.context_budget import (
    ContextBudgetConfig,
    ContextBudgetPolicy,
    ContextCategory,
    ContextMessage,
)
from packages.agent_runtime.frozen import frozen_summary_request_identity, parse_frozen_agent_spec
from packages.agent_runtime.history_planner import PlanPressure
from packages.agent_runtime.history_runtime import plan_history_view
from packages.agent_runtime.history_transcript import build_turn_transcript
from packages.agent_runtime.model_budget_adapter import RunBudgetAttemptAdapter
from packages.agent_runtime.models import Agent, AgentRun, AgentRunEvent, AgentVersion, RunStep
from packages.agent_runtime.publish import AgentPublishService
from packages.agent_runtime.run_budget_ledger import (
    RunBudgetAccount,
    RunBudgetAttempt,
    RunBudgetLedger,
)
from packages.agent_runtime.runtime_service import AgentRunService
from packages.agent_runtime.summary_models import (
    ThreadSummaryArtifact,
    ThreadSummaryJob,
    ThreadSummaryResponse,
)
from packages.agent_runtime.summary_prompt import build_summary_prompt
from packages.agent_runtime.summary_state import SummaryJobStatus
from packages.approvals import ApprovalService
from packages.control_plane.models import (
    Organization,
    OrganizationMembership,
    User,
    Workspace,
    WorkspaceMembership,
)
from packages.control_plane.services import TenantService
from packages.core.canonical.json_hash import canonical_json, canonical_json_hash
from packages.core.config.settings import Settings
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.evaluation.models import PricingSnapshot
from packages.evaluation.reproducibility import pricing_snapshot_content_hash
from packages.model_gateway.contracts import (
    AliyunDeepSeekRequestMode,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEventType,
    ModelUsage,
    ResolvedModelExecutionPlan,
    RetryPolicy,
)
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.model_gateway.run_budget_adapter import AttemptCostUpperBound, ModelAttemptKind
from packages.threads.context import SqlAlchemyThreadContextProvider
from packages.threads.models import AgentThread, ThreadTurn
from packages.threads.service import ThreadService
from packages.tools.runtime import PublishedToolCatalog

WORKTREE_ROOT = Path(__file__).resolve().parents[2]
PARENT_WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_ENV_FILE = PARENT_WORKSPACE_ROOT / ".env"
DEFAULT_LEDGER_PATH = (
    WORKTREE_ROOT / ".scratch" / "context-policy-m4-20261027" / "experiment-budget.json"
)
DEFAULT_OUTPUT_DIRECTORY = WORKTREE_ROOT / ".scratch" / "m4-thread-run-20261028"
EXPERIMENT_DATABASE_NAME = "agenthub_run_budget_m4_20261028_v4"
EXPERIMENT_SCOPE_ID = "M4-local-pilot-20261028"
EXPERIMENT_ID = EXPERIMENT_SCOPE_ID
EXPERIMENT_DATABASE_URL_ENV = "AGENTHUB_M4_EXPERIMENT_DATABASE_URL"
RAG_BASE_URL_ENV = "AGENTHUB_RAG_BASE_URL"
RAG_API_KEY_ENV = "AGENTHUB_RAG_API_KEY"
OUTPUT_TOKEN_LIMIT = 1024
RUN_BUDGET_LIMIT_CNY = Decimal("1.00")
WORKING_INPUT_LIMIT = 6000
PLANNED_PROVIDER_DISPATCHES = 3  # one summary, first answer, then fixed-revision next turn
MAX_CONTEXT_TURNS = 8
_BEIJING_HOST_SUFFIX = ".cn-beijing.maas.aliyuncs.com"
_BEIJING_PATH = "/compatible-mode/v1"
_M4_MODEL = "deepseek-v4.1-flash-1017"
_M4_PROVIDER = "openai-compatible"
_AUTHORIZATION_TEXT = (
    "User authorization: M4 local Thread/Run experiment; maximum 16 scope dispatches, "
    "within the persisted 18-request round and 30 CNY global ledger limit."
)


class ThreadRunExperimentError(RuntimeError):
    """Safe runner error that deliberately excludes credentials and raw requests."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class FixtureTurn:
    user_input: str
    assistant_output: str


@dataclass(frozen=True, slots=True)
class ThreadRunConfig:
    env_file: Path
    ledger_path: Path
    output_directory: Path
    tokenizer_path: Path
    database_url: str | None = field(default=None, repr=False, compare=False)
    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False, compare=False)
    credential_master_key_configured: bool = False


LONG_FIXTURE_APPENDICES = (
    (
        "虚构审计附录：每个备份批次分别登记租户编号、批次编号、创建时刻、状态快照和规则版本。"
        "普通备份进入45天更正后的60天保留流程；合规留置必须单独保存标记来源、到达时刻、复核人和复核结论。"
        "队列扫描只读取这些已登记字段。缺失字段时暂停该条记录并发起人工核对，"
        "不能从租户 ACTIVE 状态推断留置已解除。"
    ),
    (
        "虚构审计附录：两个租户即使共享一个备份批次，也必须分别维护状态和保留期限。"
        "ACTIVE 只决定是否进入常规候选扫描，不会覆盖留置标记；暂停租户可以暂不清理，"
        "但该暂停不改变其他 ACTIVE 租户的60天期限。"
        "审核日志同时保留扫描时的状态、排除原因和规则版本，以便之后重现当时的决定。"
    ),
    (
        "虚构审计附录：晚到的留置通知可能对应已创建并已排期的备份。系统先将对应记录移出待删除队列，"
        "再核对通知来源、租户映射和生效时间；排期时间早于通知到达时间也不能作为继续删除的理由。"
        "来源无法确认时继续保持暂停状态，等复核完成后再更新留置记录。"
    ),
    (
        "虚构审计附录：留置通知的撤销只撤销该通知自身的效力。复核人还要检查同一租户是否存在其他有效留置、"
        "后续到达的补充通知或未完成的复核任务；只要其中任一项仍有效，就继续从常规删除队列排除。"
        "不得将一条撤销记录解释成全部例外依据都已消失。"
    ),
    (
        "虚构审计附录：常规清理的审计条目保留租户、备份批次、原始创建日期、适用期限、ACTIVE 状态、"
        "留置标记、决定时间和排除依据。用户把期限从45天改为60天后，历史日志仍可显示旧规则，"
        "但当前执行必须使用60天；不能因旧记录仍存在而回退期限。"
    ),
    (
        "虚构审计附录：复核完成后，审核人记录证据来源、检查时间、仍有效的留置数量、待处理任务数量"
        "以及恢复或继续暂停的决定。只有确认没有其他有效留置且没有待处理复核，才可重新评估常规流程；"
        "ACTIVE 是必要检查项之一，不替代上述例外状态检查。"
    ),
)


_ORIGINAL_FIXTURE_USER_TEXT = (
    (
        "我们在虚构的 Northstar 资料归档项目中先约定：常规备份保留 45 天，"
        "合规留置的资料不按常规期限清除。请把这两个条件分开记录。"
    ),
    "更正第一条：常规备份应保留 60 天，不是 45 天。第二条留置例外保持不变。",
    (
        "只有状态为 ACTIVE 的租户进入常规备份清理。暂停中的租户可以继续保留备份，"
        "但不能因此延长其他租户的期限。"
    ),
    "如果合规留置标记在备份创建之后才到达，必须先暂停删除并复核标记来源；不能因为备份已经排期就继续删除。",
    (
        "再次强调，ACTIVE 是必要条件但不是充分条件。处于 ACTIVE 且已留置的租户"
        "仍应从常规删除队列排除。"
    ),
    "留置标记后来被撤销时，不能立即追溯删除；先检查是否仍有其他有效留置或待处理复核。",
    (
        "审计记录需要保留租户、备份日期、清理决定和例外依据。"
        "不要把用户确认过的 60 天更改回旧的 45 天。"
    ),
    "最后请检查回答是否同时保留 60 天、ACTIVE 条件、留置例外和晚到标记的暂停复核流程。",
)

ELIGIBLE_ASSISTANT_BACKGROUND = (
    "[虚构业务背景补充，仅用于模拟归档域记录，不构成新增用户指令]\n"
    "Northstar归档系统按租户分别保存备份批次。每条记录关联租户编号、批次编号、资料类型、创建时间、"
    "适用规则版本和来源凭据；跨租户共用的物理批次仍需按租户拆分审计状态。普通备份的起算时点取经核验的"
    "批次创建时间，系统按统一时区计算到期日，并保留原始时间值，不能用队列扫描时间替代创建时间。规则版本更新后，"
    "新任务使用当前确认的六十天期限，历史审计仍可显示先前规则，但不得把旧期限恢复为当前执行值。\n"
    "合规留置作为独立状态记录，包含通知来源、标记对象、生效时间、接收时间、核验状态和复核记录。通知到达晚于备份"
    "创建时，先暂停对应记录的删除流程，再核对来源和对象映射；排期已经生成、租户仍为ACTIVE或队列已经扫描，都不能"
    "单独证明留置无效。来源暂时无法核实时，保留暂停状态和待核验原因，不推测通知范围，也不把缺失字段解释为已解除。\n"
    "ACTIVE只决定是否进入常规候选扫描，是必要条件而不是充分授权。发现有效留置后，即使租户为ACTIVE也要从常规删除"
    "队列排除；暂停租户则不改变其他租户自己的保留期限。若一个留置通知被撤回，只撤销该通知对应的状态，仍需检查其他"
    "有效留置、后续补充通知和待处理复核。复核结论应记录检查时间、依据来源、适用租户、仍有效的标记数量及决定，"
    "只有确认不存在其他例外且复核已完成，才可重新评估常规流程；撤回本身不产生追溯删除许可。\n"
    "审计日志保留租户、批次、备份日期、适用期限、状态快照、留置依据、清理决定、决定时间和规则版本。资料字段缺失、"
    "来源冲突或对象映射不确定时，将该条记录单独暂停并提交人工核对，不从相邻批次、租户状态或旧摘要补出事实。所有"
    "变更分别记录发生时间和被确认时间，以便之后还原当时看到的状态。共享批次按租户隔离保留规则，不能因其他租户的"
    "ACTIVE状态、留置解除或资料类型相同而合并删除决定。复核界面显示通知原文的来源标识、接收时刻和适用对象；任何"
    "字段尚未核实，都保持待处理并保留原始记录。撤回事件只改变它明确指向的通知，不覆盖稍后到达的其他有效依据。"
    "同一批次出现多个通知时分别登记其对象范围和状态，复核记录逐项注明哪些记录仍暂停、哪些可以重新评估。"
    "批次转存或租户资料迁移时沿用原始创建时间和规则版本，并记录迁移前后的标识映射；无法可靠对应时不自动合并。"
    "扫描任务按已核验的状态快照生成候选清单，执行前再次检查近期通知和未完成复核，避免旧快照覆盖新到达的信息。"
    "系统只按已核验对象建立队列，状态变更附时间和来源；无法匹配批次时暂停单条并复核，不移用其他租户状态。"
    "核验结论引用对应记录并保留操作时间，未完成前维持原队列暂停状态。"
    "这些背景描述是虚构流程样例，不替代本轮用户原话。"
    "\n\n[虚构归档业务解释：记录模型与日常核对流程]\n"
    "归档系统将一次备份任务区分为批次、批次中的资料对象以及对象的租户归属。批次可以由多个存储分片组成，"
    "但清理候选按对象生成，不能因为两个对象位于同一分片就共用一个到期结论。对象索引保留稳定编号、创建时刻、"
    "资料类型、所属租户和批次映射；批次重组时新增映射版本，历史映射仍可查询，以免后续核对把旧对象指向新记录。"
    "备份任务排队、开始、完成、复制和校验分别记录事件时间。期限计算使用经确认的创建时间，不能使用复制完成时间、"
    "最近访问时间或队列处理时间替换。时间以数据库保存的标准时区值为准，界面显示本地时间时仍保留原始时区偏移，"
    "遇到夏令时切换或日期边界时按照已保存的时间戳还原；数据只提供日期而没有时刻时，记录精度限制而不补造时刻。"
    "规则变更写入带生效时间的版本记录。每次清理评估保存读取的规则版本及其有效区间，便于确认当时采用了哪条期限。"
    "更正后的六十天期限用于后续常规备份评估；历史操作日志可保留旧规则以供复核，但重新运行的当前候选必须使用"
    "已经确认的新期限。显示旧值用于解释历史，不代表当前值被撤销或恢复。规则服务暂时不可用时，候选保持未决状态，"
    "不能由最近一次成功扫描推断规则仍然相同。\n"
    "留置资料的状态记录关联具体通知及其适用对象，而不是只挂在租户总状态上。通知核验时分别检查来源标识、对象范围、"
    "生效时刻、接收时刻和当前处理状态。若通知仅指向一个批次，不能自动扩大到同一租户的所有备份；若对象映射存在冲突，"
    "先冻结相关候选并核对映射，不把相似名称当作同一对象。一个对象可同时关联多个仍有效的留置依据，逐条保存其来源和"
    "状态，防止后到通知覆盖先到通知，也防止一次撤回把其他依据一并清除。\n"
    "清理候选计算遵循先排除再筛选的次序。系统先确认对象属于哪个租户、是否有有效留置、是否存在待处理复核，再检查"
    "该租户是否为ACTIVE，最后计算适用期限。ACTIVE允许进入常规候选判断，但不单独证明对象可删除。暂停租户的排除范围"
    "限于该租户；不能因为一个租户暂停而延长或缩短其他租户的期限。批次中部分对象可以进入候选时，应拆分对象记录并分别"
    "保留决定依据，不能用一个批次级状态覆盖不同对象的留置和期限。\n"
    "通知在备份创建之后到达时，接收事件先进入待核验队列，并关联到可能受影响的对象。队列在核验完成前暂停相应删除操作；"
    "先前生成的删除计划只作为历史状态保存，不能被解释为已经执行或作为继续执行的授权。核验人确认来源和对象映射后，"
    "记录确认时间、依据、适用范围和仍未解决的问题。无法确认通知针对哪些对象时，保持较窄的待核验集合并请求人工核对，"
    "不猜测通知覆盖范围，也不把缺少签名、时间或对象字段当作否定通知的证据。\n"
    "撤回或更正通知时，状态变更只作用于该通知明确关联的对象。系统重新列出其余有效通知和待处理复核，并记录撤回事件的"
    "依据、时间及影响范围。只有确认没有其他有效留置、没有待处理复核，并重新检查ACTIVE条件和适用期限后，才重新评估"
    "常规流程。撤回不自动生成删除任务，也不追溯改写之前的暂停记录；操作人员需要基于当前状态作出新的决定。\n"
    "审计条目使用可复核字段，包括租户、对象、批次、创建时间、规则版本、留置通知标识、核验结论、暂停原因、候选状态、"
    "清理决定、决定时间和操作主体。系统区分用户提供的事实、服务读取的状态、人工核验结论和自动计算结果；来源不完整时"
    "保留不确定性，不把系统建议描述成已完成动作。日志中的字段值引用对应记录，便于之后核对，不从相邻对象或旧摘要补齐。\n"
    "批量扫描采用带版本号的状态快照。开始扫描时记录读取时间和规则版本，执行前检查是否有更晚到达的通知、撤回或待处理"
    "复核；若状态发生变化，旧候选转为待重新评估，保留其原始快照和变化原因。扫描中断后从已确认的对象边界恢复，不重复"
    "提交已完成动作；外部状态无法确认时标记为待人工处理，后续核验引用同一个对象和事件身份。\n"
    "资料迁移、批次复制和索引修复都记录旧标识、新标识、映射依据及操作者。迁移不重置对象创建时间，也不自动清除留置状态。"
    "映射无法一对一确认时，将受影响对象隔离在待核验清单中；不能因新旧编号不同就把留置视为失效，也不能仅凭名称相同"
    "合并两条记录。报告汇总按租户和对象边界统计，说明已确认数量、待核验数量和排除原因，避免把未知状态计为已清理。\n"
    "恢复作业从暂停清单读取对象身份、上次核验时间和未解决原因，逐条验证状态仍对应当前租户及批次。状态快照过旧时，"
    "先重新读取通知索引和复核队列，再生成新的判断；不能沿用已过期快照批量清理。若读取过程中出现状态变更，当前对象"
    "返回待评估队列，并保存旧、新快照的差异字段供复核人员查看。\n"
    "归档报告区分已完成、已排除、暂停待核和结果未知。已完成项引用可核验的完成事件；计划项仍标记为计划；结果未知的"
    "项目保持独立状态并进入人工协调，不从没有错误消息、扫描超时或队列消失推定操作成功。补充证据到达后记录其来源和"
    "确认时间，再决定是否更新状态，原先未知的记录仍保留可追溯变更。\n"
    "同一对象被重复导入时先比较稳定编号、租户映射和历史事件链。能够确认是重复记录时建立显式关联并保留两个来源；"
    "无法确认时分别保留并暂停冲突对象。校正重复关系不删除已有审计事件，也不把较晚创建的索引当作对象创建时间。"
    "批次拆分后，各子对象继承可验证的原始时间和适用规则；无法确定继承关系的部分进入复核，不从批次名称推断。\n"
    "复核结论按字段说明证据支持范围：通知来源可以支持来源身份，租户索引可以支持对象归属，已确认创建事件可以支持"
    "起算时间；任何一项都不能单独替代其他字段。来源之间存在冲突时并列记录并暂停受影响的动作，等待进一步核验。"
    "\n\n[虚构业务背景补充：批次变更与复核恢复的审计案例]\n"
    "当存储批次因容量调整而拆分时，系统为新子批次生成新的批次标识，并维护父子映射、拆分时间、迁移任务和校验结果。"
    "子批次中的资料对象仍保留原始创建时间及原有租户归属；拆分不会重新开始保留期限，也不会清除原来关联的留置依据。"
    "若一个对象复制到多个子批次，索引记录每个副本的存储位置和内容校验值，同时指定用于清理判断的逻辑对象身份，"
    "避免同一对象被重复计算或只删除其中一个副本后误记为已完成。迁移失败时保留源位置和失败阶段，恢复前先确认副本是否完整。\n"
    "合并批次时，系统只合并存储作业，不合并租户的保留决定。不同租户的资料即使共享物理容器，也分别维护创建时间、"
    "适用规则和例外依据。迁移清单逐项列出原标识、目标标识、对象数量、校验状态和未匹配项；未匹配项单独暂停，不能"
    "通过批次总数量一致推断每个对象都已正确迁移。补充映射时记录审核人和依据，让后续检查能重放当时的映射决定。\n"
    "到期判断将规则版本、创建时间精度和日历边界一起记录。若只有日期而缺少时刻，系统使用规则明确允许的日期粒度，"
    "并在日志中保留精度标记；若时区来源不明、时间字段冲突或事件顺序无法确认，则暂不生成自动删除候选。闰日、月份长度"
    "及夏令时转换按固定日期运算规则处理，并保存输入时间和计算后的到期时点，不能通过展示格式改变实际计算结果。\n"
    "通知核验过程中可能同时收到原通知、补充材料和更正说明。每份材料分别记录接收顺序、来源身份、所指对象、变更原因和"
    "与先前材料的关系。更正说明只覆盖其明确更正的字段；未涉及的字段继续沿用已核验值。两份材料对同一字段存在冲突时，"
    "并列展示原始值和各自来源，不自动选择较晚的一份；由授权复核人记录采纳依据后，才更新该对象的有效状态。\n"
    "一批次包含多种资料类型时，常规期限仍依对象适用规则计算，不能用批次级的单个期限覆盖内部差异。扫描结果保留"
    "每个对象进入、排除或暂停的原因；租户整体状态变化时，重新评估该租户的候选，但不改动已核验的留置通知和其他租户的"
    "状态。被排除对象继续可追踪，之后重新进入队列时记录状态改变来源和复核时间。\n"
    "恢复中断的核对任务时，读取最近确认的对象边界和已完成的审核步骤。对已完成的只读校验可以复用其结果，但对外部"
    "删除状态若无法查询确认，则标记结果未知并交由人工对账，不自动再发同一个删除动作。对尚未发送的内部检查，可在当前"
    "授权范围内继续；任何存在外部副作用且结果不明的项目都要与普通失败区分，并保留原请求标识和相关记录。\n"
    "周期报表展示规则版本、扫描窗口、候选数、留置排除数、待复核数、对象映射异常数和结果未知数。不同状态使用独立计数，"
    "不能把等待核验的项目并入已完成，也不能因扫描任务成功结束就把每条候选记成已清理。修正统计时保留原报表版本、"
    "重算原因和输入快照，让审阅者能够看到数字变化来自哪条记录。\n"
    "以上背景是Northstar虚构归档项目的流程样例，用于形成可压缩的旧助手历史；它不改变任何用户陈述，不添加当前任务目标，"
    "也不构成现实组织、法律规则或实际删除动作的证据。"
    "这些解释只扩充虚构归档项目的历史背景，用于压力测试摘要输入；其中不增加新的用户要求，不覆盖或改写此前用户原话，"
    "不把计划、队列状态或待核验通知描述为已经完成的清理动作。"
)

THREAD_FIXTURE: tuple[FixtureTurn, ...] = (
    FixtureTurn(
        "\n\n".join(_ORIGINAL_FIXTURE_USER_TEXT[:6]),
        "已记录60天期限、ACTIVE必要条件、留置例外、晚到标记的暂停复核和撤销后的检查要求。\n\n"
        + ELIGIBLE_ASSISTANT_BACKGROUND,
    ),
    FixtureTurn(
        _ORIGINAL_FIXTURE_USER_TEXT[6],
        "已记录审计字段，并保留最终期限60天。" + ("\n" + LONG_FIXTURE_APPENDICES[2]) * 7,
    ),
    FixtureTurn(
        _ORIGINAL_FIXTURE_USER_TEXT[7],
        "检查结果包含60天、ACTIVE必要条件、留置排除及暂停复核流程。"
        + ("\n" + LONG_FIXTURE_APPENDICES[3]) * 6,
    ),
)

FIRST_RUN_INPUT = (
    "请根据前面已经确认的 Northstar 归档规则，说明当前常规备份期限、ACTIVE 条件、"
    "合规留置例外，以及晚到留置标记时必须执行的步骤。用户已明确将期限从 45 天更正为 60 天。"
)
NEXT_RUN_INPUT = (
    "继续同一任务。假设租户仍为 ACTIVE，但最近收到一个较晚到达的留置撤销通知，"
    "你应先检查哪些状态？回答中保留 60 天期限和先前确认的审计字段。"
)
SYSTEM_PROMPT = (
    "你是一个受控的归档规则助手。遵守用户更正；区分必要条件、例外和恢复流程。"
    "仅根据对话中已给事实回答，不推断额外法律要求。"
)


def load_config(
    *,
    env_file: Path = DEFAULT_ENV_FILE,
    environ: Mapping[str, str] | None = None,
    ledger_path: Path = DEFAULT_LEDGER_PATH,
    output_directory: Path = DEFAULT_OUTPUT_DIRECTORY,
    tokenizer_path: Path | None = None,
) -> ThreadRunConfig:
    """Read parent workspace configuration without logging secret values."""
    values: dict[str, str] = {}
    if env_file.is_file():
        for key, value in dotenv_values(env_file).items():
            if key and value is not None:
                values[key] = value
    values.update(dict(os.environ if environ is None else environ))
    settings = Settings(
        _env_file=env_file if env_file.is_file() else None, _env_file_encoding="utf-8"
    )
    return ThreadRunConfig(
        env_file=env_file,
        ledger_path=ledger_path,
        output_directory=output_directory,
        tokenizer_path=tokenizer_path
        or settings.summary_deepseek_v41_tokenizer_path
        or DEFAULT_TOKENIZER_PATH,
        database_url=values.get(EXPERIMENT_DATABASE_URL_ENV),
        base_url=values.get(RAG_BASE_URL_ENV),
        api_key=values.get(RAG_API_KEY_ENV),
        credential_master_key_configured=bool(settings.credential_master_key),
    )


def safe_database_identity(database_url: str | None) -> dict[str, Any] | None:
    """Return only non-secret identity fields for the exact isolated DB."""
    if not isinstance(database_url, str) or not database_url:
        return None
    try:
        parsed = make_url(database_url)
    except Exception:
        return None
    host = parsed.host or ""
    if (
        host not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port != 15432
        or parsed.database != EXPERIMENT_DATABASE_NAME
    ):
        return None
    return {
        "host": host,
        "port": parsed.port,
        "database": parsed.database,
    }


def is_allowlisted_beijing_url(base_url: str | None) -> bool:
    if not isinstance(base_url, str) or not base_url:
        return False
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError:
        return False
    hostname = parsed.hostname or ""
    return (
        parsed.scheme == "https"
        and hostname.endswith(_BEIJING_HOST_SUFFIX)
        and len(hostname) > len(_BEIJING_HOST_SUFFIX)
        and port is None
        and parsed.username is None
        and parsed.password is None
        and parsed.path.rstrip("/") == _BEIJING_PATH
        and not parsed.query
        and not parsed.fragment
    )


def build_dry_run_report(
    config: ThreadRunConfig,
    *,
    ledger_state: Mapping[str, Any],
    tokenizer_verified: bool,
) -> dict[str, Any]:
    """Build a no-side-effect readiness report for the default CLI path."""
    attempts = ledger_state.get("attempts")
    policy = ledger_state.get("local_experiment_policy")
    if not isinstance(attempts, list) or not isinstance(policy, Mapping):
        raise ThreadRunExperimentError("LOCAL_EXPERIMENT_LEDGER_UNAVAILABLE")
    if policy.get("mode") != RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT:
        raise ThreadRunExperimentError("LOCAL_EXPERIMENT_POLICY_NOT_ENABLED")
    scope_id = policy.get("active_scope_id")
    if not isinstance(scope_id, str) or not scope_id:
        raise ThreadRunExperimentError("LOCAL_EXPERIMENT_SCOPE_UNAVAILABLE")

    total_cost = sum((Decimal(str(row["charged_or_reserved_cny"])) for row in attempts), Decimal(0))
    total_input = sum(max(int(row["estimated_input_tokens"]), 32768) for row in attempts)
    total_output = sum(int(row["requested_output_tokens"]) for row in attempts)
    total_m4 = sum(row.get("stage") == "M4" for row in attempts)
    experiment_used = sum(row.get("scope_id") == scope_id for row in attempts)
    round_used = int(policy.get("round_attempts_used", 0))
    round_limit = int(policy.get("round_attempt_limit", LOCAL_ROUND_ATTEMPT_LIMIT))
    experiment_limit = int(policy.get("scope_attempt_limit", LOCAL_SCOPE_ATTEMPT_LIMIT))
    planned_input = PLANNED_PROVIDER_DISPATCHES * 32768
    planned_output = PLANNED_PROVIDER_DISPATCHES * OUTPUT_TOKEN_LIMIT
    per_dispatch_cost = upper_bound_cost_cny(
        input_token_bound=32768,
        output_token_bound=OUTPUT_TOKEN_LIMIT,
        input_per_million=Decimal("2"),
        output_per_million=Decimal("8"),
    )
    planned_cost = per_dispatch_cost * PLANNED_PROVIDER_DISPATCHES
    database_identity = safe_database_identity(config.database_url)
    missing: list[str] = []
    if database_identity is None:
        missing.append(EXPERIMENT_DATABASE_URL_ENV)
    if not is_allowlisted_beijing_url(config.base_url):
        missing.append(RAG_BASE_URL_ENV)
    if not config.api_key:
        missing.append(RAG_API_KEY_ENV)
    if not config.credential_master_key_configured:
        missing.append("AGENTHUB_CREDENTIAL_MASTER_KEY")
    if not tokenizer_verified:
        missing.append("PINNED_TOKENIZER_ASSET")
    if scope_id != EXPERIMENT_SCOPE_ID:
        missing.append("ACTIVE_LEDGER_SCOPE_MISMATCH")
    if experiment_used + PLANNED_PROVIDER_DISPATCHES > experiment_limit:
        missing.append("EXPERIMENT_DISPATCH_LIMIT")
    if round_used + PLANNED_PROVIDER_DISPATCHES > round_limit:
        missing.append("ROUND_DISPATCH_LIMIT")
    if len(attempts) + PLANNED_PROVIDER_DISPATCHES > 80:
        missing.append("GLOBAL_DISPATCH_LIMIT")
    if total_m4 + PLANNED_PROVIDER_DISPATCHES > 40:
        missing.append("M4_DISPATCH_LIMIT")
    if total_input + planned_input > 1_500_000:
        missing.append("GLOBAL_INPUT_TOKEN_LIMIT")
    if total_output + planned_output > 60_000:
        missing.append("GLOBAL_OUTPUT_TOKEN_LIMIT")
    remaining_cny = Decimal(str(ledger_state.get("limit_cny", "30"))) - total_cost
    if total_cost + planned_cost > Decimal(str(ledger_state.get("limit_cny", "30"))):
        missing.append("GLOBAL_CNY_LIMIT")

    return {
        "mode": "DRY_RUN",
        "provider_dispatch_enabled": False,
        "execute_ready": not missing,
        "missing_configuration_or_budget": sorted(set(missing)),
        "experiment": {
            "experiment_id": EXPERIMENT_ID,
            "fixture_kind": "FICTIONAL_THREAD_HISTORY",
            "fixture_rounds": len(THREAD_FIXTURE),
            "contains_user_correction": True,
            "max_context_turns": MAX_CONTEXT_TURNS,
            "workspace_input_limit": WORKING_INPUT_LIMIT,
            "task_output_limit": OUTPUT_TOKEN_LIMIT,
            "planned_provider_dispatches_max": PLANNED_PROVIDER_DISPATCHES,
        },
        "database": database_identity,
        "tokenizer_verified": tokenizer_verified,
        "ledger": {
            "scope_id": scope_id,
            "attempt_count": len(attempts),
            "experiment_attempts_used": experiment_used,
            "experiment_attempt_limit": experiment_limit,
            "round_attempts_used": round_used,
            "round_attempt_limit": round_limit,
            "charged_or_reserved_cny": str(total_cost),
            "remaining_cny": str(remaining_cny),
            "dispatches_remaining_global": 80 - len(attempts),
            "input_tokens_remaining_global": 1_500_000 - total_input,
            "output_tokens_remaining_global": 60_000 - total_output,
        },
        "execution_command": (
            "python -m benchmarks.evaluation.thread_run_local_experiment "
            "--execute --accept-provider-dispatch"
        ),
    }


def preflight(config: ThreadRunConfig) -> dict[str, Any]:
    """Load the fixed tokenizer and the existing ledger without changing either."""
    try:
        counter = DeepseekV41SummaryExperimentGate.from_local_asset(
            config.tokenizer_path,
            output_token_limit=OUTPUT_TOKEN_LIMIT,
        ).counter
        tokenizer_verified = counter.verified_source
    except Exception:
        tokenizer_verified = False
    try:
        with MemoryTrialBudget(config.ledger_path, price_identity=PRICE_IDENTITY) as budget:
            state = copy.deepcopy(budget.state)
    except Exception as error:
        raise ThreadRunExperimentError("LOCAL_EXPERIMENT_LEDGER_UNAVAILABLE") from error
    return build_dry_run_report(config, ledger_state=state, tokenizer_verified=tokenizer_verified)


def _assert_summary_preflight_metrics(metrics: Mapping[str, Any]) -> None:
    if metrics.get("summary_policy_enabled") is not True:
        raise ThreadRunExperimentError("SUMMARY_PREFLIGHT_POLICY_NOT_ENABLED")
    if metrics.get("summary_trigger_expected") is not True:
        raise ThreadRunExperimentError("SUMMARY_PREFLIGHT_TRIGGER_NOT_REACHED")
    if metrics.get("summary_source_policy") != "plain_text_prefix_v1":
        raise ThreadRunExperimentError("SUMMARY_PREFLIGHT_SOURCE_POLICY_INVALID")
    if metrics.get("checkpoint_selector_exact") is not True:
        raise ThreadRunExperimentError("SUMMARY_PREFLIGHT_CHECKPOINT_BINDING_INVALID")
    if metrics.get("approval_service_configured") is not True:
        raise ThreadRunExperimentError("SUMMARY_PREFLIGHT_APPROVAL_SERVICE_MISSING")
    if metrics.get("run_budget_account_ready") is not True:
        raise ThreadRunExperimentError("SUMMARY_PREFLIGHT_RUN_BUDGET_ACCOUNT_INVALID")
    if metrics.get("run_execution_lease_clear") is not True:
        raise ThreadRunExperimentError("SUMMARY_PREFLIGHT_RUN_LEASE_NOT_CLEAR")
    if metrics.get("pre_trim_pressure") not in {"SOFT", "HARD"}:
        raise ThreadRunExperimentError("SUMMARY_PREFLIGHT_TRIGGER_NOT_REACHED")
    if (
        type(metrics.get("prefix_candidate_count")) is not int
        or metrics["prefix_candidate_count"] < 1
    ):
        raise ThreadRunExperimentError("SUMMARY_PREFLIGHT_NO_VERIFIED_PREFIX")
    if metrics.get("recent_turn_count") != 2 or metrics.get("recent_groups_retained") is not True:
        raise ThreadRunExperimentError("SUMMARY_PREFLIGHT_PROTECTED_TAIL_INVALID")
    if (
        metrics.get("summary_request_mode") != "non_thinking"
        or type(metrics.get("summary_request_exact_tokens")) is not int
        or metrics.get("summary_request_within_working_budget") is not True
        or metrics.get("summary_request_context_admission_code") is not None
        or metrics.get("summary_request_exact_tokens", WORKING_INPUT_LIMIT + 1)
        > metrics.get("summary_request_available_input_tokens", 0)
    ):
        raise ThreadRunExperimentError("SUMMARY_PREFLIGHT_EXACT_REQUEST_OVER_BUDGET")
    if (
        type(metrics.get("projected_final_context_tokens")) is not int
        or type(metrics.get("B_work")) is not int
        or metrics["projected_final_context_tokens"] > metrics["B_work"]
    ):
        raise ThreadRunExperimentError("SUMMARY_PREFLIGHT_FINAL_CONTEXT_OVER_BUDGET")


async def _summary_preflight_metrics(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    identity: _ExperimentIdentity,
    run_id: UUID,
    tokenizer_path: Path,
    enforce: bool = True,
) -> dict[str, Any]:
    """Reproduce runtime source planning before any provider boundary."""
    from packages.agent_runtime.runtime_graph import (
        _RUNTIME_POLICY,
        SUMMARY_HISTORICAL_CONTEXT_GOAL,
        _AgentRunGraph,
        _build_summary_input_context,
        _permission_snapshot,
        _summary_candidates,
        _summary_final_view_budget,
        _summary_target_budget,
    )

    async with session_factory() as session:
        run = await session.scalar(
            select(AgentRun).where(
                AgentRun.workspace_id == identity.workspace_id,
                AgentRun.id == run_id,
            )
        )
        version = (
            await session.scalar(
                select(AgentVersion).where(
                    AgentVersion.workspace_id == identity.workspace_id,
                    AgentVersion.id == identity.version_id,
                )
            )
            if run is not None
            else None
        )
        definitions = await PublishedToolCatalog(session).list(
            workspace_id=identity.workspace_id,
            agent_version_id=identity.version_id,
        )
        run_budget_account = (
            await session.get(RunBudgetAccount, (identity.workspace_id, run_id))
            if run is not None
            else None
        )
    if run is None or version is None or run.thread_id != identity.thread_id:
        raise ThreadRunExperimentError("SUMMARY_PREFLIGHT_RUN_BINDING_INVALID")
    spec = parse_frozen_agent_spec(version.resolved_spec, workspace_id=identity.workspace_id)
    conversation = await SqlAlchemyThreadContextProvider(session_factory).history_sources(
        workspace_id=identity.workspace_id,
        thread_id=identity.thread_id,
        before_run_id=run_id,
        max_turns=MAX_CONTEXT_TURNS,
        summary_source_policy="plain_text_prefix_v1",
    )
    manifest = conversation.source_manifest
    if manifest is None or manifest.status != "COMPLETE":
        raise ThreadRunExperimentError("SUMMARY_PREFLIGHT_HISTORY_INCOMPLETE")
    budget = ContextBudgetConfig(
        **spec.runtime["context_budget"],
        max_memory_tokens=spec.runtime.get("memory", {}).get("max_memory_tokens"),
    )
    policy = ContextBudgetPolicy(spec, budget)
    planned = plan_history_view(
        policy=policy,
        conversation=conversation,
        mandatory_messages=(
            ModelMessage("system", _RUNTIME_POLICY),
            ModelMessage("system", spec.system_prompt),
            ModelMessage("user", run.input_text),
        ),
        memory_messages=(),
        tools=_AgentRunGraph._model_tool_definitions({item.identity: item for item in definitions}),
        permission_snapshot=_permission_snapshot(identity.context),
        return_blocked_plan=True,
        summary_source_policy="plain_text_prefix_v1",
    )
    plan = planned.plan
    candidates = _summary_candidates(conversation, planned.plain_text_prefix)
    prefix = planned.plain_text_prefix
    recent = [item for item in plan.decisions if item.category.value == "RECENT"]
    mandatory_tokens = sum(
        item.tokens for item in plan.decisions if item.category.value == "MANDATORY"
    )
    recent_tokens = sum(item.tokens for item in recent)
    protected_prefix_user_tokens = sum(
        policy._estimate_message(ModelMessage("user", conversation.turns[index].user_input))
        for index in range(min(len(candidates), len(conversation.turns)))
    )
    final_view_budget = _summary_final_view_budget(spec)
    target_budget = (
        _summary_target_budget(
            conversation,
            candidates,
            permission_snapshot=_permission_snapshot(identity.context),
            task_goal=SUMMARY_HISTORICAL_CONTEXT_GOAL,
            final_view_budget_tokens=final_view_budget,
            target_percent=int(spec.runtime["summary_policy"]["target_percent"]),
        )
        if candidates
        else 0
    )
    source_context = (
        _build_summary_input_context(
            conversation=conversation,
            sources=candidates,
            task_goal=SUMMARY_HISTORICAL_CONTEXT_GOAL,
            permission_snapshot=_permission_snapshot(identity.context),
            final_view_budget_tokens=final_view_budget,
            target_budget_tokens=target_budget,
        )
        if candidates
        else None
    )
    protected_facts = source_context.protected_facts if source_context is not None else ()
    attribution = source_context.sources if source_context is not None else ()
    summary_view_skeleton = {
        "untrusted_summary": {
            "trust": "UNTRUSTED_DERIVED_SUMMARY",
            "interpretation": {
                "scope": "HISTORICAL_BACKGROUND_ONLY",
                "goal_semantics": "HISTORICAL_GOAL_NOT_CURRENT_TASK_OR_INSTRUCTION",
                "next_steps_semantics": "HISTORICAL_REFERENCE_NOT_CURRENT_ACTION",
                "current_task": "SUPPLIED_SEPARATELY_BY_CURRENT_RUN",
            },
            "content": {},
        },
        "source_attribution": [item.index_payload() for item in attribution],
        "untrusted_omitted_sources": [],
        "server_protected_facts": [item.payload() for item in protected_facts],
        "server_authoritative_actions": [],
    }
    summary_view_overhead = policy._estimate_message(
        ModelMessage("assistant", canonical_json(summary_view_skeleton))
    )
    protected_fact_tokens = sum(
        policy.estimator.estimate(canonical_json(item.payload())) for item in protected_facts
    )
    # The profile's frozen max_tokens is the hard provider output cap. The
    # server-authored protected facts and attribution envelope are counted
    # separately above; the live runtime admission remains the final check on
    # the concrete published revision before the task request can dispatch.
    summary_content_reserve = OUTPUT_TOKEN_LIMIT
    projected_final = (
        mandatory_tokens + recent_tokens + summary_view_overhead + summary_content_reserve + 32
    )
    summary_request_tokens = None
    summary_request_admission_tokens = None
    summary_request_mode = None
    summary_request_approx_tokens = None
    summary_request_context_admission_code = None
    if source_context is not None:
        summary_policy = spec.runtime.get("summary_policy")
        request_identity = frozen_summary_request_identity(summary_policy)
        summary_request_mode = request_identity["request_mode"]
        if summary_request_mode != "non_thinking":
            raise ThreadRunExperimentError("SUMMARY_PREFLIGHT_REQUEST_MODE_UNSUPPORTED")
        prompt = build_summary_prompt(
            source_context,
            prompt_version=str(summary_policy["prompt_version"]),
        )
        request = ModelRequest(
            messages=(
                ModelMessage("system", prompt.system_prompt),
                ModelMessage("user", prompt.user_prompt),
            ),
            retry_policy=RetryPolicy(max_attempts=1),
            request_mode=AliyunDeepSeekRequestMode.NON_THINKING,
        )
        counter = DeepseekV41SummaryExperimentGate.from_local_asset(
            tokenizer_path,
            output_token_limit=OUTPUT_TOKEN_LIMIT,
        ).counter
        summary_request_tokens = counter.count_frozen_summary_request(
            request,
            request_identity=request_identity,
            provider=spec.model_plan.primary.provider,
            model=spec.model_plan.primary.model,
        )
        tagged = (
            ContextMessage(request.messages[0], ContextCategory.SYSTEM_PROMPT),
            ContextMessage(request.messages[1], ContextCategory.CURRENT_USER_TASK),
        )
        summary_request_approx_tokens = sum(
            policy._estimate_message(message) for message in request.messages
        )
        try:
            summary_admission = policy.admit(tagged)
        except Exception as error:
            summary_request_context_admission_code = getattr(
                error, "code", "SUMMARY_CONTEXT_ADMISSION_REJECTED"
            )
        else:
            summary_request_admission_tokens = summary_admission.usage.available_input
            if (
                summary_admission.messages != request.messages
                or summary_admission.usage.truncated
                or summary_admission.usage.dropped_exchange_count
                or summary_admission.tool_definitions
            ):
                summary_request_context_admission_code = "SUMMARY_CONTEXT_ADMISSION_TRUNCATED"
        if (
            type(summary_request_tokens) is not int
            or summary_request_tokens <= 0
            or summary_request_tokens > WORKING_INPUT_LIMIT
            or summary_request_admission_tokens is not None
            and summary_request_tokens > summary_request_admission_tokens
            or summary_request_context_admission_code is not None
        ):
            summary_request_context_admission_code = (
                summary_request_context_admission_code
                or "SUMMARY_PREFLIGHT_EXACT_REQUEST_OVER_BUDGET"
            )
    metrics = {
        "source_manifest_status": manifest.status,
        "source_count": len(conversation.turns),
        "counter_id": plan.counter_id,
        "counter_version": plan.counter_version,
        "B_work": plan.thresholds.b_work_tokens,
        "N_pre_trim": plan.pre_trim_tokens,
        "G_growth": plan.unaccounted_growth_tokens,
        "N_plus_G": plan.planned_tokens,
        "soft_threshold": plan.thresholds.soft_tokens,
        "hard_threshold": plan.thresholds.hard_tokens,
        "target_threshold": plan.thresholds.target_tokens,
        "pre_trim_pressure": plan.pre_trim_pressure.value,
        "post_trim_pressure": plan.post_trim_pressure.value,
        "summary_policy_enabled": spec.runtime.get("summary_policy", {}).get("enabled") is True,
        "summary_source_policy": "plain_text_prefix_v1",
        "checkpoint_selector_exact": _checkpoint_selector_is_exact(
            LangGraphCheckpointAdapter.config_for_run(identity.workspace_id, run_id),
            workspace_id=identity.workspace_id,
            run_id=run_id,
        ),
        "approval_service_configured": callable(ApprovalService(session_factory).create_or_get),
        "run_budget_account_ready": (
            run_budget_account is not None
            and run_budget_account.currency == "CNY"
            and run_budget_account.limit_amount == RUN_BUDGET_LIMIT_CNY
            and run_budget_account.reserved_amount == 0
            and run_budget_account.settled_amount == 0
        ),
        "run_execution_lease_clear": (
            run.status == "RUNNING"
            and run.execution_owner is None
            and run.execution_lease_expires_at is None
        ),
        "summary_request_mode": summary_request_mode,
        "summary_request_exact_tokens": summary_request_tokens,
        "summary_request_approx_tokens": summary_request_approx_tokens,
        "summary_request_context_admission_code": summary_request_context_admission_code,
        "summary_request_available_input_tokens": summary_request_admission_tokens,
        "summary_request_input_limit_tokens": WORKING_INPUT_LIMIT,
        "summary_request_within_working_budget": (
            type(summary_request_tokens) is int
            and summary_request_tokens <= WORKING_INPUT_LIMIT
            and type(summary_request_admission_tokens) is int
            and summary_request_tokens <= summary_request_admission_tokens
        ),
        "summary_trigger_expected": (
            spec.runtime.get("summary_policy", {}).get("enabled") is True
            and (
                not plan.ready_for_dispatch
                or plan.pre_trim_pressure in {PlanPressure.SOFT, PlanPressure.HARD}
            )
        ),
        "prefix_candidate_count": len(candidates),
        "prefix_candidate_tokens": prefix.estimated_tokens if prefix is not None else 0,
        "prefix_protected_user_text_tokens": protected_prefix_user_tokens,
        "protected_fact_count": len(protected_facts),
        "protected_fact_payload_tokens": protected_fact_tokens,
        "recent_turn_count": len(recent),
        "recent_group_tokens": recent_tokens,
        "recent_groups_retained": len(recent) == 2 and all(item.retained for item in recent),
        "mandatory_tokens": mandatory_tokens,
        "summary_source_budget_tokens": (
            source_context.source_budget_tokens if source_context is not None else 0
        ),
        "summary_target_budget_tokens": target_budget,
        "summary_view_envelope_tokens": summary_view_overhead,
        "summary_content_reserved_tokens": summary_content_reserve,
        "projected_final_context_tokens": projected_final,
        "plan_ready_before_summary": plan.ready_for_dispatch,
    }
    if not isinstance(enforce, bool):
        raise TypeError("enforce must be bool")
    if enforce:
        _assert_summary_preflight_metrics(metrics)
    return metrics


async def prepare_thread_run_preflight(config: ThreadRunConfig) -> dict[str, Any]:
    """Persist a fresh isolated Thread/Run fixture and prove summary pressure without dispatch."""
    blockers = _execute_configuration_blockers(config)
    if blockers:
        raise ThreadRunExperimentError("EXECUTION_CONFIGURATION_INCOMPLETE")
    readiness = preflight(config)
    if not readiness["execute_ready"]:
        raise ThreadRunExperimentError("EXECUTION_PREFLIGHT_BLOCKED")
    database_url = config.database_url
    assert database_url is not None
    settings = Settings(_env_file=config.env_file if config.env_file.is_file() else None)
    cipher = ProviderCredentialCipher.from_settings(settings)
    run_directory = _require_scratch_output(config.output_directory)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        await _verify_database_prerequisites(engine)
        identity = await _create_frozen_experiment_identity(
            session_factory,
            config=config,
            settings=settings,
            cipher=cipher,
            base_url=config.base_url or "",
        )
        turn = await ThreadService(session_factory).open_turn(
            identity.context,
            identity.thread_id,
            user_input=FIRST_RUN_INPUT,
            client_token=f"m4-preflight-{uuid4().hex}",
        )
        run = await AgentRunService(session_factory).prepare_run(
            identity.context,
            agent_version_id=identity.version_id,
            input_text=turn.user_input,
            thread_id=identity.thread_id,
            thread_turn_id=turn.id,
        )
        await ThreadService(session_factory).attach_run(
            identity.context,
            turn_id=turn.id,
            run_id=run.id,
        )
        async with session_factory() as session:
            async with session.begin():
                await RunBudgetLedger().create_account(
                    session,
                    workspace_id=identity.workspace_id,
                    run_id=run.id,
                    currency="CNY",
                    limit_amount=RUN_BUDGET_LIMIT_CNY,
                )
        metrics = await _summary_preflight_metrics(
            session_factory,
            identity=identity,
            run_id=run.id,
            tokenizer_path=config.tokenizer_path,
            enforce=False,
        )
        result = {
            "status": "READY_FOR_REVIEW_NO_PROVIDER_DISPATCH",
            "provider_dispatch_enabled": False,
            "fixture_kind": "FICTIONAL_THREAD_HISTORY",
            "fixture_turns": len(THREAD_FIXTURE),
            "workspace_id": str(identity.workspace_id),
            "thread_id": str(identity.thread_id),
            "first_run_id": str(run.id),
            "first_turn_sequence": turn.sequence,
            "summary_preflight": metrics,
            "ledger_preflight": readiness["ledger"],
        }
        try:
            _assert_summary_preflight_metrics(metrics)
        except ThreadRunExperimentError as error:
            result["status"] = f"BLOCKED_{error.code}"
            _write_json_file(run_directory / "thread-run-preflight.json", result)
            raise
        _write_json_file(run_directory / "thread-run-preflight.json", result)
        return result
    finally:
        await engine.dispose()


async def _load_prepared_run_identity(
    config: ThreadRunConfig,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: UUID,
) -> tuple[_ExperimentIdentity, dict[str, Any]]:
    """Revalidate the no-dispatch preflight artifact and its exact prepared Run."""
    preflight_path = config.output_directory / "thread-run-preflight.json"
    try:
        preflight_artifact = json.loads(preflight_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ThreadRunExperimentError("PREPARED_RUN_PREFLIGHT_ARTIFACT_INVALID") from error
    if (
        not isinstance(preflight_artifact, Mapping)
        or preflight_artifact.get("status") != "READY_FOR_REVIEW_NO_PROVIDER_DISPATCH"
        or preflight_artifact.get("provider_dispatch_enabled") is not False
        or preflight_artifact.get("first_run_id") != str(run_id)
    ):
        raise ThreadRunExperimentError("PREPARED_RUN_PREFLIGHT_BINDING_MISMATCH")

    async with session_factory() as session:
        run = await session.get(AgentRun, run_id)
        if run is None:
            raise ThreadRunExperimentError("PREPARED_RUN_NOT_FOUND")
        version = await session.get(AgentVersion, run.agent_version_id)
        thread = await session.get(AgentThread, run.thread_id)
        turn = await session.get(ThreadTurn, run.thread_turn_id)
        workspace = await session.get(Workspace, run.workspace_id)
        agent = await session.get(Agent, version.agent_id) if version is not None else None
        run_account = await session.get(RunBudgetAccount, (run.workspace_id, run.id))
        has_steps = await session.scalar(
            select(RunStep.id)
            .where(RunStep.workspace_id == run.workspace_id, RunStep.agent_run_id == run.id)
            .limit(1)
        )
        has_events = await session.scalar(
            select(AgentRunEvent.id)
            .where(
                AgentRunEvent.workspace_id == run.workspace_id,
                AgentRunEvent.agent_run_id == run.id,
            )
            .limit(1)
        )
        has_attempts = await session.scalar(
            select(RunBudgetAttempt.id)
            .where(
                RunBudgetAttempt.workspace_id == run.workspace_id,
                RunBudgetAttempt.run_id == run.id,
            )
            .limit(1)
        )
        has_summary_jobs = await session.scalar(
            select(ThreadSummaryJob.id)
            .where(
                ThreadSummaryJob.workspace_id == run.workspace_id,
                ThreadSummaryJob.thread_id == run.thread_id,
                ThreadSummaryJob.trigger_run_id == run.id,
            )
            .limit(1)
        )
        if (
            run.status != "RUNNING"
            or run.thread_id is None
            or run.thread_turn_id is None
            or run.input_text != FIRST_RUN_INPUT
            or version is None
            or agent is None
            or thread is None
            or turn is None
            or workspace is None
            or thread.agent_id != agent.id
            or thread.id != turn.thread_id
            or turn.agent_run_id != run.id
            or turn.id != run.thread_turn_id
            or turn.user_input != run.input_text
            or version.agent_id != agent.id
            or version.resolved_spec_hash != run.resolved_spec_hash
            or not isinstance(version.resolved_spec, dict)
            or canonical_json_hash(version.resolved_spec) != version.resolved_spec_hash
            or version.spec_schema_version != version.resolved_spec.get("spec_schema_version")
            or has_steps is not None
            or has_events is not None
            or has_attempts is not None
            or has_summary_jobs is not None
            or run_account is None
            or run_account.currency != "CNY"
            or run_account.limit_amount != RUN_BUDGET_LIMIT_CNY
            or run_account.reserved_amount != 0
            or run_account.settled_amount != 0
        ):
            raise ThreadRunExperimentError("PREPARED_RUN_ALREADY_USED_OR_INVALID")
        principal = PrincipalContext(
            request_id=f"m4-prepared-{uuid4().hex}",
            trace_id=f"m4-prepared-{uuid4().hex}",
            user_id=str(run.created_by),
        )
        context = (
            await TenantService().get_workspace_access(
                session,
                principal=principal,
                workspace_id=run.workspace_id,
            )
        ).context
        identity = _ExperimentIdentity(
            user_id=run.created_by,
            organization_id=workspace.organization_id,
            workspace_id=run.workspace_id,
            agent_id=agent.id,
            base_version_id=version.id,
            version_id=version.id,
            resolved_spec_hash=version.resolved_spec_hash,
            thread_id=thread.id,
            context=context,
        )

    metrics = await _summary_preflight_metrics(
        session_factory,
        identity=identity,
        run_id=run.id,
        tokenizer_path=config.tokenizer_path,
    )
    if metrics != preflight_artifact.get("summary_preflight"):
        raise ThreadRunExperimentError("PREPARED_RUN_SOURCE_OR_POLICY_CHANGED")
    return identity, metrics


def require_dispatch_acceptance(*, execute: bool, accept_provider_dispatch: bool) -> None:
    if execute and not accept_provider_dispatch:
        raise ThreadRunExperimentError("PROVIDER_DISPATCH_ACCEPTANCE_REQUIRED")
    if accept_provider_dispatch and not execute:
        raise ThreadRunExperimentError("ACCEPTANCE_FLAG_REQUIRES_EXECUTE")


async def execute_thread_run_experiment(
    config: ThreadRunConfig, *, prepared_run_id: UUID | None = None
) -> dict[str, Any]:
    """Create an isolated fixture and execute its first and fixed-revision next Runs.

    This function is reachable only through the explicit CLI acceptance flags.
    Every external provider attempt passes through the Run budget adapter and
    the same research ledger accounting hook. A run error stops this one-shot
    experiment; it never prepares a replacement Run or retries a dispatch.
    """
    blockers = _execute_configuration_blockers(config)
    if blockers:
        raise ThreadRunExperimentError("EXECUTION_CONFIGURATION_INCOMPLETE")
    report = preflight(config)
    if not report["execute_ready"]:
        raise ThreadRunExperimentError("EXECUTION_PREFLIGHT_BLOCKED")

    database_url = config.database_url
    assert database_url is not None
    settings = Settings(_env_file=config.env_file if config.env_file.is_file() else None)
    cipher = ProviderCredentialCipher.from_settings(settings)
    gate = DeepseekV41SummaryExperimentGate.from_local_asset(
        config.tokenizer_path,
        output_token_limit=OUTPUT_TOKEN_LIMIT,
    )
    run_identity = prepared_run_id.hex[:12] if prepared_run_id is not None else uuid4().hex[:12]
    run_directory = _require_scratch_output(config.output_directory / f"run-{run_identity}")
    manifest_path = run_directory / "thread-run-execution-manifest.json"

    engine = create_async_engine(database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    evidence = {
        "experiment_id": EXPERIMENT_ID,
        "fixture_kind": "FICTIONAL_THREAD_HISTORY",
        "fixture_turns": len(THREAD_FIXTURE),
        "provider_dispatches": [],
        "runs": [],
        "status": "STARTED",
    }
    try:
        await _verify_database_prerequisites(engine)
        with MemoryTrialBudget(config.ledger_path, price_identity=PRICE_IDENTITY) as budget:
            accounting_hook = MemoryTrialBudgetAccountingHook(
                budget,
                scope_id=EXPERIMENT_SCOPE_ID,
            )
            initial_ledger_report = dict(accounting_hook.report())
            prepared_metrics = None
            if prepared_run_id is None:
                identity = await _create_frozen_experiment_identity(
                    session_factory,
                    config=config,
                    settings=settings,
                    cipher=cipher,
                    base_url=config.base_url or "",
                )
            else:
                identity, prepared_metrics = await _load_prepared_run_identity(
                    config,
                    session_factory,
                    run_id=prepared_run_id,
                )
            source_provider = SqlAlchemyThreadContextProvider(session_factory)
            run_ids: list[UUID] = []
            run_budget_ledger = RunBudgetLedger()
            if prepared_run_id is not None:
                run_ids.append(prepared_run_id)
                async with session_factory() as session:
                    turn = await session.get(
                        ThreadTurn,
                        await session.scalar(
                            select(AgentRun.thread_turn_id).where(
                                AgentRun.workspace_id == identity.workspace_id,
                                AgentRun.id == prepared_run_id,
                            )
                        ),
                    )
                if turn is None:
                    raise ThreadRunExperimentError("PREPARED_RUN_TURN_MISSING")
                evidence["runs"].append(
                    {
                        "run_id": str(prepared_run_id),
                        "thread_turn_id": str(turn.id),
                        "status": "PREPARED",
                    }
                )

            async def prepare_task_run(user_input: str) -> UUID:
                turn = await ThreadService(session_factory).open_turn(
                    identity.context,
                    identity.thread_id,
                    user_input=user_input,
                    client_token=f"m4-local-{uuid4().hex}",
                )
                prepared = await AgentRunService(session_factory).prepare_run(
                    identity.context,
                    agent_version_id=identity.version_id,
                    input_text=turn.user_input,
                    thread_id=identity.thread_id,
                    thread_turn_id=turn.id,
                )
                await ThreadService(session_factory).attach_run(
                    identity.context,
                    turn_id=turn.id,
                    run_id=prepared.id,
                )
                async with session_factory() as session:
                    async with session.begin():
                        await run_budget_ledger.create_account(
                            session,
                            workspace_id=identity.workspace_id,
                            run_id=prepared.id,
                            currency="CNY",
                            limit_amount=RUN_BUDGET_LIMIT_CNY,
                        )
                evidence["runs"].append(
                    {
                        "run_id": str(prepared.id),
                        "thread_turn_id": str(turn.id),
                        "status": "PREPARED",
                    }
                )
                return prepared.id

            # Create only the first task turn before the first Run. The next
            # turn is opened after this Run and its summary have completed.
            if not run_ids:
                run_ids.append(await prepare_task_run(FIRST_RUN_INPUT))

            recorder = _DispatchRecorder()
            price_source = _FixedM4AttemptPriceSource()
            checkpoint_adapter = LangGraphCheckpointAdapter(database_url)
            for run_index in range(2):
                run_id = run_ids[run_index]
                permit = gate.bind_runtime_permit(
                    experiment_id=EXPERIMENT_ID,
                    database_url=database_url,
                    workspace_id=identity.workspace_id,
                    thread_id=identity.thread_id,
                    agent_version_id=identity.version_id,
                    resolved_spec_hash=identity.resolved_spec_hash,
                    run_ids=set(run_ids),
                    base_url=config.base_url or "",
                    accounting_hook=accounting_hook,
                )

                def attempt_budget_factory(run: AgentRun, round_number: int):
                    return RunBudgetAttemptAdapter(
                        session_factory,
                        workspace_id=run.workspace_id,
                        run_id=run.id,
                        call_identity=f"round-{round_number}",
                        price_source=price_source,
                        ledger=run_budget_ledger,
                    )

                def gateway_factory(
                    session,
                    *,
                    _run_id: UUID = run_id,
                    _require_summary_view: bool = True,
                ):
                    return _RecordingSqlAlchemyModelGateway(
                        session,
                        recorder=recorder,
                        credential_cipher=cipher,
                        run_id=_run_id,
                        require_summary_view=_require_summary_view,
                    )

                service = AgentRunService(
                    session_factory,
                    model_gateway_factory=gateway_factory,
                    model_attempt_budget_factory=attempt_budget_factory,
                    credential_cipher=cipher,
                    approval_service=ApprovalService(session_factory),
                    checkpoint_adapter=checkpoint_adapter,
                    thread_context_provider=source_provider,
                    summary_request_counter=gate.counter,
                    local_summary_experiment=permit,
                )
                if service.approval_service is None or service.checkpoint_adapter is None:
                    raise ThreadRunExperimentError("SUMMARY_CHECKPOINT_RUNTIME_NOT_CONFIGURED")
                _assert_run_checkpoint_selector(
                    checkpoint_adapter.config_for_run(identity.workspace_id, run_id),
                    workspace_id=identity.workspace_id,
                    run_id=run_id,
                )
                if run_index == 0:
                    try:
                        current_metrics = await _summary_preflight_metrics(
                            session_factory,
                            identity=identity,
                            run_id=run_id,
                            tokenizer_path=config.tokenizer_path,
                        )
                        if prepared_metrics is not None and prepared_metrics != current_metrics:
                            raise ThreadRunExperimentError(
                                "PREPARED_RUN_PREFLIGHT_CHANGED_BEFORE_EXECUTION"
                            )
                        evidence["summary_preflight"] = current_metrics
                    except ThreadRunExperimentError:
                        evidence["status"] = "STOPPED_SUMMARY_PREFLIGHT_BLOCKED"
                        _write_evidence(run_directory, evidence)
                        raise
                    # Setup and frozen configuration are complete; from this
                    # point onward any uncertain provider boundary is one-shot.
                    _create_one_shot_manifest(manifest_path)
                try:
                    result = await service.execute_prepared_run(identity.context, run_id=run_id)
                except Exception:
                    evidence["provider_dispatches"] = recorder.safe_records()
                    evidence["ledger"] = dict(accounting_hook.report())
                    evidence["status"] = "STOPPED_AFTER_DISPATCH_EXCEPTION_NO_RETRY"
                    _write_evidence(run_directory, evidence)
                    raise
                evidence["runs"][run_index].update(
                    {
                        "status": result.status,
                        "failure_code": result.failure_code,
                        "final_output": result.final_output,
                        "model_step_count": result.model_step_count,
                    }
                )
                if result.status != "SUCCEEDED":
                    evidence["provider_dispatches"] = recorder.safe_records()
                    evidence["ledger"] = dict(accounting_hook.report())
                    evidence["status"] = "STOPPED_AFTER_RUN_FAILURE"
                    _write_evidence(run_directory, evidence)
                    raise ThreadRunExperimentError("THREAD_RUN_FAILED_NO_RETRY")
                if run_index == 0:
                    published = await _load_published_summary(
                        session_factory,
                        workspace_id=identity.workspace_id,
                        thread_id=identity.thread_id,
                        trigger_run_id=run_id,
                    )
                    if published is None:
                        evidence["provider_dispatches"] = recorder.safe_records()
                        evidence["ledger"] = dict(accounting_hook.report())
                        evidence["status"] = "STOPPED_NO_PUBLISHED_SUMMARY"
                        _write_evidence(run_directory, evidence)
                        raise ThreadRunExperimentError("EXPECTED_SUMMARY_NOT_PUBLISHED")
                    evidence["summary_revision"] = {
                        "job_id": str(published[0].id),
                        "artifact_id": str(published[1].id),
                        "revision_hash": published[1].revision_hash,
                        "content": dict(published[1].summary_content),
                        "response_usage": dict(published[2].usage_metadata or {}),
                    }
                    run_ids.append(await prepare_task_run(NEXT_RUN_INPUT))

            evidence["provider_dispatches"] = recorder.safe_records()
            evidence["ledger"] = dict(accounting_hook.report())
            try:
                _assert_expected_dispatches(
                    evidence,
                    expected_attempt_count=(
                        int(initial_ledger_report["attempt_count"]) + PLANNED_PROVIDER_DISPATCHES
                    ),
                )
                _assert_fixed_summary_in_next_request(evidence)
                evidence["next_run_validation"] = "PASSED"
            except ThreadRunExperimentError:
                evidence["status"] = "STOPPED_EVIDENCE_VALIDATION_FAILED"
                _write_evidence(run_directory, evidence)
                raise
            evidence["status"] = "SUMMARY_PUBLISHED_AND_FIXED_REVISION_REUSED_NEXT_RUN"
            _write_evidence(run_directory, evidence)
            return evidence
    finally:
        await engine.dispose()


@dataclass(frozen=True, slots=True)
class _ExperimentIdentity:
    user_id: UUID
    organization_id: UUID
    workspace_id: UUID
    agent_id: UUID
    base_version_id: UUID
    version_id: UUID
    resolved_spec_hash: str
    thread_id: UUID
    context: WorkspaceExecutionContext


async def _create_frozen_experiment_identity(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    config: ThreadRunConfig,
    settings: Settings,
    cipher: ProviderCredentialCipher,
    base_url: str,
) -> _ExperimentIdentity:
    api_key = config.api_key
    if not api_key:
        raise ThreadRunExperimentError("ALIYUN_API_KEY_UNAVAILABLE")
    async with session_factory() as session:
        user = User(
            email=f"{uuid4().hex}@m4-local.invalid",
            normalized_email=f"{uuid4().hex}@m4-local.invalid",
            password_hash="local-experiment-no-login",
        )
        session.add(user)
        await session.flush()
        organization = Organization(
            name=f"M4 local experiment {uuid4().hex[:12]}",
            created_by=user.id,
        )
        session.add(organization)
        await session.flush()
        workspace = Workspace(
            organization_id=organization.id,
            name=f"M4 local Thread experiment {uuid4().hex[:12]}",
        )
        session.add(
            organization_membership := OrganizationMembership(
                organization_id=organization.id,
                user_id=user.id,
                role="OWNER",
            )
        )
        session.add(workspace)
        await session.flush()
        session.add(
            WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="DEVELOPER")
        )
        credential = ProviderCredential(
            workspace_id=workspace.id,
            provider=_M4_PROVIDER,
            name=f"m4-local-{EXPERIMENT_ID}-{uuid4().hex[:8]}",
            secret_ciphertext=cipher.encrypt(api_key),
            secret_version=1,
            base_url=base_url,
            enabled=True,
        )
        session.add(credential)
        await session.flush()
        profile = ModelProfile(
            workspace_id=workspace.id,
            provider_credential_id=credential.id,
            model=_M4_MODEL,
            temperature=Decimal("0.1"),
            max_tokens=OUTPUT_TOKEN_LIMIT,
            timeout_seconds=Decimal("90"),
            fallback_profile_id=None,
            capabilities={
                "tool_calling": False,
                "streaming": False,
                "structured_output": False,
                "max_context_tokens": 8192,
            },
            enabled=True,
        )
        session.add(profile)
        await session.commit()
        user_id = user.id
        organization_id = organization.id
        workspace_id = workspace.id
        profile_id = profile.id

    context = WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id=f"m4-local-{uuid4().hex}",
                trace_id=f"m4-local-{uuid4().hex}",
                user_id=str(user_id),
            ),
            organization_id=str(organization_id),
            org_role="OWNER",
        ),
        workspace_id=str(workspace_id),
        workspace_role="DEVELOPER",
        permissions=frozenset(
            {
                "agent_create",
                "agent_edit",
                "agent_run",
                "tool_run",
                "workspace_read",
                "knowledge_run",
            }
        ),
    )
    principal = context.organization.principal
    async with session_factory() as session:
        context = (
            await TenantService().get_workspace_access(
                session,
                principal=principal,
                workspace_id=workspace_id,
            )
        ).context
    runtime_config = {
        "context_budget": {"reserved_output_tokens": OUTPUT_TOKEN_LIMIT},
        "context_budget_sources": {"reserved_output_tokens": "CUSTOM"},
        "context_policy": {
            "policy_version": "adaptive_context_v1",
            "preset": "GENERAL_64K",
            "workspace_input_limit": WORKING_INPUT_LIMIT,
            "workspace_input_limit_source": "CUSTOM",
        },
        "history_policy": "deterministic_history_v1",
    }
    publisher = AgentPublishService(settings=settings)
    async with session_factory() as session:
        agent = await publisher.create_draft(
            session,
            context,
            name=f"M4 local Thread experiment {uuid4().hex[:8]}",
            system_prompt=SYSTEM_PROMPT,
            model_profile_id=profile_id,
            description="Fictional M4 Thread/Run fixture; local-only experiment.",
            runtime_config=runtime_config,
        )
        agent_id = agent.id
    async with session_factory() as session:
        base = await publisher.publish(session, context, agent_id)
        base_version_id = base.id
    async with session_factory() as session:
        async with session.begin():
            version = await register_local_v2_summary_version(
                session,
                base_version_id=base_version_id,
                workspace_id=workspace_id,
                created_by=user_id,
            )
            await session.flush()
            version_id = version.id
            resolved_hash = version.resolved_spec_hash

    pricing = PricingSnapshot(
        workspace_id=workspace_id,
        name=f"m4-local-{EXPERIMENT_ID}-{uuid4().hex[:8]}",
        provider=_M4_PROVIDER,
        model=_M4_MODEL,
        currency="CNY",
        input_price_per_1m=Decimal("2.00000000"),
        output_price_per_1m=Decimal("8.00000000"),
        effective_at=datetime.now(UTC) - timedelta(minutes=1),
        source_note=f"Fixed isolated M4 rate snapshot; {PRICE_IDENTITY}.",
        content_hash="0" * 64,
        created_by=user_id,
    )
    pricing.content_hash = pricing_snapshot_content_hash(pricing)
    async with session_factory() as session:
        session.add(pricing)
        await session.commit()

    thread = await ThreadService(session_factory).create_thread(
        context,
        agent_id=agent_id,
        title="M4 fictional archive policy thread",
    )
    await _seed_fixture_history(
        session_factory,
        context=context,
        agent_version_id=version_id,
        resolved_spec_hash=resolved_hash,
        thread_id=thread.id,
        turns=THREAD_FIXTURE,
    )
    del organization_membership
    return _ExperimentIdentity(
        user_id=user_id,
        organization_id=organization_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
        base_version_id=base_version_id,
        version_id=version_id,
        resolved_spec_hash=resolved_hash,
        thread_id=thread.id,
        context=context,
    )


async def _seed_fixture_history(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    context: WorkspaceExecutionContext,
    agent_version_id: UUID,
    resolved_spec_hash: str,
    thread_id: UUID,
    turns: tuple[FixtureTurn, ...],
) -> None:
    thread_service = ThreadService(session_factory)
    for index, fixture in enumerate(turns, start=1):
        turn = await thread_service.open_turn(
            context,
            thread_id,
            user_input=fixture.user_input,
            client_token=f"m4-fixture-{index}-{uuid4().hex[:8]}",
        )
        run_id = uuid4()
        run = AgentRun(
            id=run_id,
            workspace_id=UUID(context.workspace_id),
            agent_version_id=agent_version_id,
            thread_id=thread_id,
            thread_turn_id=turn.id,
            input_text=fixture.user_input,
            created_by=UUID(context.user_id),
            resolved_spec_hash=resolved_spec_hash,
            status="SUCCEEDED",
            final_output=fixture.assistant_output,
            thread_history_transcript=build_turn_transcript(
                run_id=run_id,
                turn_id=UUID(str(turn.id)),
                messages=(
                    ModelMessage("user", fixture.user_input),
                    ModelMessage("assistant", fixture.assistant_output),
                ),
            ).to_payload(),
            thread_history_evidence_registry={},
            effective_knowledge_snapshots=[],
            effective_wiki_publications=[],
            effective_memory_snapshot={},
        )
        async with session_factory() as session:
            session.add(run)
            await session.commit()
        await thread_service.attach_run(context, turn_id=turn.id, run_id=run.id)


class _FixedM4AttemptPriceSource:
    async def resolve(
        self,
        session,
        *,
        profile,
        request: ModelRequest,
        provider: str,
        attempt_kind: ModelAttemptKind,
    ) -> AttemptCostUpperBound | None:
        del session, request, attempt_kind
        if provider != _M4_PROVIDER or profile.model != _M4_MODEL:
            return None
        amount = upper_bound_cost_cny(
            input_token_bound=32768,
            output_token_bound=profile.max_tokens,
            input_per_million=Decimal("2"),
            output_per_million=Decimal("8"),
        )
        return AttemptCostUpperBound(amount, "CNY", PRICE_IDENTITY)


class _DispatchRecorder:
    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []

    def record_request(self, *, run_id: UUID | None, plan, request: ModelRequest) -> int:
        index = len(self._records)
        self._records.append(
            {
                "run_id": str(run_id) if run_id is not None else None,
                "provider": plan.primary.provider,
                "model": plan.primary.model,
                "request_mode": request.request_mode.value,
                "messages": [
                    {"role": message.role, "content": message.content}
                    for message in request.messages
                ],
                "response": None,
                "usage": None,
                "response_finish_reason": None,
            }
        )
        return index

    def record_response(self, index: int, response: ModelResponse) -> None:
        record = self._records[index]
        record["response"] = response.content
        record["usage"] = _usage_payload(response.usage)
        record["estimated_cost_cny"] = _estimated_response_cost(response.usage)
        record["response_finish_reason"] = response.finish_reason

    def safe_records(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._records)


class _RecordingPreparedGateway:
    def __init__(
        self,
        gateway,
        recorder: _DispatchRecorder,
        run_id: UUID | None,
        *,
        require_summary_view: bool = False,
    ):
        self._gateway = gateway
        self._recorder = recorder
        self._run_id = run_id
        self._require_summary_view = require_summary_view

    @property
    def _credentials(self):
        return getattr(self._gateway, "_credentials", None)

    async def generate_resolved(
        self,
        context: WorkspaceExecutionContext,
        plan: ResolvedModelExecutionPlan,
        request: ModelRequest,
    ) -> ModelResponse:
        if self._requires_summary_view(request) and not _request_has_summary_view(request):
            raise ThreadRunExperimentError("SUMMARY_REVISION_MISSING_BEFORE_MAIN_MODEL")
        index = self._recorder.record_request(run_id=self._run_id, plan=plan, request=request)
        response = await self._gateway.generate_resolved(context, plan, request)
        self._recorder.record_response(index, response)
        return response

    async def stream_resolved(self, *args, **kwargs):
        context, plan, request = args[:3]
        if self._requires_summary_view(request) and not _request_has_summary_view(request):
            raise ThreadRunExperimentError("SUMMARY_REVISION_MISSING_BEFORE_MAIN_MODEL")
        index = self._recorder.record_request(run_id=self._run_id, plan=plan, request=request)
        response: ModelResponse | None = None
        async for event in self._gateway.stream_resolved(context, plan, request):
            if event.event_type is ModelStreamEventType.COMPLETED and event.response is not None:
                response = event.response
            yield event
        if response is not None:
            self._recorder.record_response(index, response)

    def _requires_summary_view(self, request: ModelRequest) -> bool:
        return (
            self._require_summary_view
            and request.request_mode is AliyunDeepSeekRequestMode.PROVIDER_DEFAULT
        )


def _assert_run_checkpoint_selector(
    config: Mapping[str, Any], *, workspace_id: UUID, run_id: UUID
) -> None:
    configurable = config.get("configurable")
    if (
        not isinstance(configurable, Mapping)
        or configurable.get("thread_id") != f"agenthub:{workspace_id}:{run_id}"
        or configurable.get("checkpoint_id") is not None
        or configurable.get("checkpoint_ns") not in {None, ""}
    ):
        raise ThreadRunExperimentError("SUMMARY_CHECKPOINT_SELECTOR_BINDING_INVALID")


def _checkpoint_selector_is_exact(
    config: Mapping[str, Any], *, workspace_id: UUID, run_id: UUID
) -> bool:
    try:
        _assert_run_checkpoint_selector(config, workspace_id=workspace_id, run_id=run_id)
    except ThreadRunExperimentError:
        return False
    return True


class _RecordingSqlAlchemyModelGateway(SqlAlchemyModelGateway):
    def __init__(
        self,
        session,
        *,
        recorder: _DispatchRecorder,
        credential_cipher,
        run_id: UUID,
        require_summary_view: bool = False,
    ):
        super().__init__(session, credential_cipher=credential_cipher)
        self._recorder = recorder
        self._experiment_run_id = run_id
        self._require_summary_view = require_summary_view

    async def prepare_resolved(self, context, plan, request, *, operation):
        prepared = await super().prepare_resolved(context, plan, request, operation=operation)
        return _RecordingPreparedGateway(
            prepared,
            self._recorder,
            self._experiment_run_id,
            require_summary_view=self._require_summary_view,
        )


def _request_has_summary_view(request: ModelRequest) -> bool:
    for message in request.messages:
        if message.role != "assistant" or not isinstance(message.content, str):
            continue
        try:
            payload = json.loads(message.content)
        except ValueError:
            continue
        summary = payload.get("untrusted_summary") if isinstance(payload, Mapping) else None
        if (
            isinstance(summary, Mapping)
            and summary.get("trust") == "UNTRUSTED_DERIVED_SUMMARY"
            and isinstance(summary.get("content"), Mapping)
        ):
            return True
    return False


async def _load_published_summary(session_factory, *, workspace_id, thread_id, trigger_run_id):
    async with session_factory() as session:
        job = await session.scalar(
            select(ThreadSummaryJob).where(
                ThreadSummaryJob.workspace_id == workspace_id,
                ThreadSummaryJob.thread_id == thread_id,
                ThreadSummaryJob.trigger_run_id == trigger_run_id,
                ThreadSummaryJob.status == SummaryJobStatus.PUBLISHED.value,
            )
        )
        if job is None or job.artifact_id is None or job.response_id is None:
            return None
        artifact = await session.get(ThreadSummaryArtifact, job.artifact_id)
        response = await session.get(ThreadSummaryResponse, job.response_id)
        if artifact is None or response is None or artifact.publication_status != "PUBLISHED":
            return None
        return job, artifact, response


def _assert_fixed_summary_in_next_request(evidence: Mapping[str, Any]) -> None:
    revision = evidence.get("summary_revision")
    runs = evidence.get("runs")
    dispatches = evidence.get("provider_dispatches")
    if not isinstance(revision, Mapping) or not isinstance(runs, list) or len(runs) != 2:
        raise ThreadRunExperimentError("PUBLISHED_SUMMARY_EVIDENCE_MISSING")
    content = revision.get("content")
    if not isinstance(content, Mapping):
        raise ThreadRunExperimentError("PUBLISHED_SUMMARY_CONTENT_MISSING")
    marker = canonical_json_hash(dict(content))
    matching = []
    for record in dispatches if isinstance(dispatches, list) else []:
        if record.get("run_id") not in {runs[0].get("run_id"), runs[1].get("run_id")}:
            continue
        messages = record.get("messages")
        if isinstance(messages, list) and any(
            isinstance(message, Mapping)
            and isinstance(message.get("content"), str)
            and "UNTRUSTED_DERIVED_SUMMARY" in message["content"]
            and _contains_summary_content(message["content"], content)
            for message in messages
        ):
            matching.append(record)
    matched_run_ids = {record.get("run_id") for record in matching}
    if matched_run_ids != {runs[0].get("run_id"), runs[1].get("run_id")}:
        raise ThreadRunExperimentError("FIXED_REVISION_NOT_FOUND_IN_NEXT_MODEL_REQUEST")
    evidence["revision_reuse"] = {
        "revision_hash": revision.get("revision_hash"),
        "first_run_id": runs[0].get("run_id"),
        "next_run_id": runs[1].get("run_id"),
        "observed_model_requests": len(matching),
        "summary_content_identity": marker,
        "next_run_validation": "PASSED",
    }


def _assert_expected_dispatches(
    evidence: Mapping[str, Any], *, expected_attempt_count: int
) -> None:
    dispatches = evidence.get("provider_dispatches")
    runs = evidence.get("runs")
    ledger = evidence.get("ledger")
    if not isinstance(dispatches, list) or len(dispatches) != PLANNED_PROVIDER_DISPATCHES:
        raise ThreadRunExperimentError("UNEXPECTED_PROVIDER_DISPATCH_COUNT")
    if not isinstance(runs, list) or len(runs) != 2 or not isinstance(ledger, Mapping):
        raise ThreadRunExperimentError("RUN_OR_LEDGER_EVIDENCE_MISSING")
    if ledger.get("attempt_count") != expected_attempt_count:
        raise ThreadRunExperimentError("LEDGER_DISPATCH_COUNT_MISMATCH")
    run_ids = [run.get("run_id") for run in runs]
    modes = [dispatch.get("request_mode") for dispatch in dispatches]
    if (
        modes.count("non_thinking") != 1
        or modes.count("provider_default") != 2
        or sorted(dispatch.get("run_id") for dispatch in dispatches)
        != sorted([run_ids[0], run_ids[0], run_ids[1]])
    ):
        raise ThreadRunExperimentError("FROZEN_REQUEST_MODE_OR_RUN_BINDING_MISMATCH")
    for dispatch in dispatches:
        if (
            dispatch.get("provider") != _M4_PROVIDER
            or dispatch.get("model") != _M4_MODEL
            or dispatch.get("response") is None
            or dispatch.get("usage") is None
            or dispatch.get("estimated_cost_cny") is None
        ):
            raise ThreadRunExperimentError("PROVIDER_RESPONSE_USAGE_OR_COST_MISSING")


def _estimated_response_cost(usage: ModelUsage | None) -> str | None:
    if usage is None:
        return None
    amount = (
        Decimal(usage.input_tokens) * Decimal("2") + Decimal(usage.output_tokens) * Decimal("8")
    ) / Decimal(1_000_000)
    return str(amount.quantize(Decimal("0.00000001")))


def _contains_summary_content(text_value: str, content: Mapping[str, Any]) -> bool:
    try:
        serialized = canonical_json(dict(content))
    except (TypeError, ValueError):
        return False
    return serialized.replace(" ", "") in text_value.replace(" ", "")


def _execute_configuration_blockers(config: ThreadRunConfig) -> list[str]:
    missing = []
    if safe_database_identity(config.database_url) is None:
        missing.append(EXPERIMENT_DATABASE_URL_ENV)
    if not is_allowlisted_beijing_url(config.base_url):
        missing.append(RAG_BASE_URL_ENV)
    if not config.api_key:
        missing.append(RAG_API_KEY_ENV)
    if not config.credential_master_key_configured:
        missing.append("AGENTHUB_CREDENTIAL_MASTER_KEY")
    if not config.tokenizer_path.is_file():
        missing.append("PINNED_TOKENIZER_ASSET")
    return missing


def _require_scratch_output(path: Path) -> Path:
    root = (WORKTREE_ROOT / ".scratch").resolve()
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise ThreadRunExperimentError("OUTPUT_DIRECTORY_OUTSIDE_LOCAL_SCRATCH")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _create_one_shot_manifest(path: Path) -> None:
    manifest = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "PROVIDER_DISPATCH_PENDING",
        "fixture_kind": "FICTIONAL_THREAD_HISTORY",
        "planned_dispatches_max": PLANNED_PROVIDER_DISPATCHES,
        "retry_policy": "NO_AUTOMATIC_RETRY_AFTER_UNKNOWN",
    }
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as error:
        raise ThreadRunExperimentError("EXPERIMENT_MANIFEST_ALREADY_EXISTS_NO_RETRY") from error


def _write_evidence(directory: Path, evidence: Mapping[str, Any]) -> None:
    target = directory / "thread-run-evidence.json"
    _write_json_file(target, evidence)


def _write_json_file(target: Path, evidence: Mapping[str, Any]) -> None:
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(evidence, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)


async def _verify_database_prerequisites(engine) -> None:
    async with engine.connect() as connection:
        database = (await connection.execute(text("select current_database()"))).scalar_one()
        revisions = list(
            (await connection.execute(text("select version_num from alembic_version"))).scalars()
        )
        if database != EXPERIMENT_DATABASE_NAME or "0048_summary_run_step" not in revisions:
            raise ThreadRunExperimentError("ISOLATED_DATABASE_MIGRATION_MISMATCH")
        checkpoint_tables = set(
            (
                await connection.execute(
                    text(
                        "select table_name from information_schema.tables "
                        "where table_schema='langgraph_checkpoint'"
                    )
                )
            ).scalars()
        )
        if not {"checkpoints", "checkpoint_blobs", "checkpoint_writes"}.issubset(checkpoint_tables):
            raise ThreadRunExperimentError("LANGGRAPH_CHECKPOINT_SCHEMA_NOT_BOOTSTRAPPED")


def _usage_payload(usage: ModelUsage | None) -> dict[str, Any] | None:
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "cached_tokens": usage.cached_tokens,
    }


def _safe_exception_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,96}", code):
        return code
    return "THREAD_RUN_EXPERIMENT_FAILED"


def _context_for_identity(user_id: UUID, organization_id: UUID, workspace_id: UUID):
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id=f"m4-local-{uuid4().hex}",
                trace_id=f"m4-local-{uuid4().hex}",
                user_id=str(user_id),
            ),
            organization_id=str(organization_id),
            org_role="OWNER",
        ),
        workspace_id=str(workspace_id),
        workspace_role="DEVELOPER",
        permissions=frozenset(
            {
                "agent_create",
                "agent_edit",
                "agent_run",
                "tool_run",
                "workspace_read",
                "knowledge_run",
            }
        ),
    )


def _async_database_url(database_url: str) -> str:
    return database_url.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1).replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER_PATH)
    parser.add_argument("--tokenizer", type=Path)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help=(
            "Create an isolated Thread/Run fixture and assert summary pressure "
            "without provider calls."
        ),
    )
    parser.add_argument(
        "--execute", action="store_true", help="Run provider calls in the named isolated DB."
    )
    parser.add_argument(
        "--prepared-run-id",
        type=UUID,
        help="Execute only the exact Run recorded by --prepare-only preflight.",
    )
    parser.add_argument(
        "--accept-provider-dispatch",
        action="store_true",
        help="Explicitly accept the already-prepared local provider requests.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = load_config(
        env_file=args.env_file,
        ledger_path=args.ledger,
        output_directory=args.output_directory,
        tokenizer_path=args.tokenizer,
    )
    configure_windows_asyncio_policy()
    try:
        if args.prepare_only and (
            args.execute or args.accept_provider_dispatch or args.prepared_run_id is not None
        ):
            raise ThreadRunExperimentError("PREPARE_ONLY_CANNOT_ENABLE_PROVIDER_DISPATCH")
        if args.prepared_run_id is not None and not args.execute:
            raise ThreadRunExperimentError("PREPARED_RUN_ID_REQUIRES_EXECUTE")
        require_dispatch_acceptance(
            execute=args.execute,
            accept_provider_dispatch=args.accept_provider_dispatch,
        )
        if args.prepare_only:
            result = asyncio.run(prepare_thread_run_preflight(config))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if not args.execute:
            report = preflight(config)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        result = asyncio.run(
            execute_thread_run_experiment(config, prepared_run_id=args.prepared_run_id)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ThreadRunExperimentError as error:
        print(json.dumps({"status": "BLOCKED", "code": error.code}, ensure_ascii=False))
        return 2
    except Exception as error:
        print(
            json.dumps(
                {"status": "FAILED", "code": _safe_exception_code(error)},
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "RAG_API_KEY_ENV",
    "RAG_BASE_URL_ENV",
    "DEFAULT_ENV_FILE",
    "DEFAULT_LEDGER_PATH",
    "DEFAULT_OUTPUT_DIRECTORY",
    "EXPERIMENT_DATABASE_NAME",
    "EXPERIMENT_DATABASE_URL_ENV",
    "EXPERIMENT_ID",
    "FIRST_RUN_INPUT",
    "FixtureTurn",
    "NEXT_RUN_INPUT",
    "PLANNED_PROVIDER_DISPATCHES",
    "RUN_BUDGET_LIMIT_CNY",
    "THREAD_FIXTURE",
    "ThreadRunConfig",
    "ThreadRunExperimentError",
    "build_dry_run_report",
    "is_allowlisted_beijing_url",
    "load_config",
    "main",
    "preflight",
    "prepare_thread_run_preflight",
    "require_dispatch_acceptance",
    "safe_database_identity",
]
