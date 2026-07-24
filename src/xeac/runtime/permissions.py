"""The AgentRuntime permission concern (a mixin composed into AgentRuntime).

Classifying a tool call's permission, the bash "always allow" rule lifecycle (distill, remember,
persist), preflighting a whole batch, resolving per-call decisions, and minting request ids."""
from __future__ import annotations

from xeac.runtime.internals import _ResolvedToolDecision
from xeac.runtime.internals import _PreflightGate
from xeac.runtime.internals import _ToolPlan
from xeac.runtime.internals import _coerce_structured_arguments
from xeac.base.background_tasks import spawn_background_task
from xeac.runtime.locations import BashAllowRule
from xeac.runtime.locations import PermissionDecision
from xeac.runtime.locations import ResolvedLocation
from xeac.runtime.locations import ToolLocationError
from xeac.runtime.locations import _LOCATION_TOOLS
from langchain_core.messages import SystemMessage
from typing import Any
from typing import Optional
import ast
import json
import uuid

# The state-changing control_screen primitives. A script that calls any of them is mutating; one that
# only reads (find_one/find_many/read/hover/scroll) is read-only. This is the structural analogue of
# the bash read-only assessment — a scan of the primitive names the script calls, not a regex.
_MUTATING_SCREEN_PRIMITIVES = frozenset({"click", "type", "choose", "upload", "drag"})


def _control_script_assessment(script: str) -> tuple[str, str]:
    """Classify a control_screen script as ``read_only``, ``mutating``, or ``unknown`` by scanning
    its calls for a state-changing primitive, so the permission classifier gets the same static
    signal bash gets. ``unknown`` when the script does not parse (the classifier escalates on doubt)."""
    try:
        tree = ast.parse(script)
    except SyntaxError:
        return "unknown", "the script could not be parsed"
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _MUTATING_SCREEN_PRIMITIVES:
            return "mutating", f"calls {node.func.id}()"
    return "read_only", ""




class _PermissionsMixin:

    def _evaluate_bash_permission(self, command: str) -> str:
        unmatched = "ask" if self._interactive_manual_mode else "allow"
        return self._permissions.evaluate_bash_permission(command, unmatched=unmatched)

    async def _classify_permission(
        self,
        *,
        tool_kind: str,
        command: str,
        raw_command: str,
        default_decision: str,
        read_only: bool,
        risk: str,
        justification: str,
        static_classification: str = "",
        static_detail: str = "",
        outside_reads: Optional[list[str]] = None,
    ) -> PermissionDecision:
        context = json.dumps(
            {
                "tool_kind": tool_kind,
                "working_directory": self._working_directory,
                "command": command,
                "raw_command": raw_command,
                "default_permission_decision": default_decision,
                "model_declared_read_only": read_only,
                "model_declared_risk": risk,
                "model_justification": justification,
                "static_read_only_classification": static_classification,
                "static_detail": static_detail,
                "outside_working_directory_reads": outside_reads or [],
                "allowed_actions": ["auto_approve", "escalate"],
            },
            ensure_ascii=False,
        )
        prompt = self._prompt_loader.load("permission_classifier", {"context": context})
        try:
            model = self._llm.bind_tools([PermissionDecision], tool_choice="auto")
            response = await model.ainvoke([
                SystemMessage(content=prompt),
            ])
            if not response.tool_calls:
                return PermissionDecision(action="escalate", justification="Classifier returned no structured decision.", risk="medium")
            decision = PermissionDecision.model_validate(response.tool_calls[0]["args"])
            if default_decision == "deny" and decision.action == "auto_approve":
                return PermissionDecision(action="escalate", justification="User-configured permissions deny this action.", risk="high")
            if not decision.justification.strip():
                return PermissionDecision(action="escalate", justification="Classifier did not provide a justification.", risk="medium")
            return decision
        except Exception as exception:
            return PermissionDecision(action="escalate", justification=f"{exception}", risk="medium")

    def _evaluate_bash_permission(self, command: str) -> str:
        unmatched = "ask" if self._interactive_manual_mode else "allow"
        return self._permissions.evaluate_bash_permission(command, unmatched=unmatched)

    async def _classify_permission(
        self,
        *,
        tool_kind: str,
        command: str,
        raw_command: str,
        default_decision: str,
        read_only: bool,
        risk: str,
        justification: str,
        static_classification: str = "",
        static_detail: str = "",
        outside_reads: Optional[list[str]] = None,
    ) -> PermissionDecision:
        context = json.dumps(
            {
                "tool_kind": tool_kind,
                "working_directory": self._working_directory,
                "command": command,
                "raw_command": raw_command,
                "default_permission_decision": default_decision,
                "model_declared_read_only": read_only,
                "model_declared_risk": risk,
                "model_justification": justification,
                "static_read_only_classification": static_classification,
                "static_detail": static_detail,
                "outside_working_directory_reads": outside_reads or [],
                "allowed_actions": ["auto_approve", "escalate"],
            },
            ensure_ascii=False,
        )
        prompt = self._prompt_loader.load("permission_classifier", {"context": context})
        try:
            model = self._llm.bind_tools([PermissionDecision], tool_choice="auto")
            response = await model.ainvoke([
                SystemMessage(content=prompt),
            ])
            if not response.tool_calls:
                return PermissionDecision(action="escalate", justification="Classifier returned no structured decision.", risk="medium")
            decision = PermissionDecision.model_validate(response.tool_calls[0]["args"])
            if default_decision == "deny" and decision.action == "auto_approve":
                return PermissionDecision(action="escalate", justification="User-configured permissions deny this action.", risk="high")
            if not decision.justification.strip():
                return PermissionDecision(action="escalate", justification="Classifier did not provide a justification.", risk="medium")
            return decision
        except Exception as exception:
            return PermissionDecision(action="escalate", justification=f"{exception}", risk="medium")

    def _evaluate_bash_permission(self, command: str) -> str:
        unmatched = "ask" if self._interactive_manual_mode else "allow"
        return self._permissions.evaluate_bash_permission(command, unmatched=unmatched)

    async def _classify_permission(
        self,
        *,
        tool_kind: str,
        command: str,
        raw_command: str,
        default_decision: str,
        read_only: bool,
        risk: str,
        justification: str,
        static_classification: str = "",
        static_detail: str = "",
        outside_reads: Optional[list[str]] = None,
    ) -> PermissionDecision:
        context = json.dumps(
            {
                "tool_kind": tool_kind,
                "working_directory": self._working_directory,
                "command": command,
                "raw_command": raw_command,
                "default_permission_decision": default_decision,
                "model_declared_read_only": read_only,
                "model_declared_risk": risk,
                "model_justification": justification,
                "static_read_only_classification": static_classification,
                "static_detail": static_detail,
                "outside_working_directory_reads": outside_reads or [],
                "allowed_actions": ["auto_approve", "escalate"],
            },
            ensure_ascii=False,
        )
        prompt = self._prompt_loader.load("permission_classifier", {"context": context})
        try:
            model = self._llm.bind_tools([PermissionDecision], tool_choice="auto")
            response = await model.ainvoke([
                SystemMessage(content=prompt),
            ])
            if not response.tool_calls:
                return PermissionDecision(action="escalate", justification="Classifier returned no structured decision.", risk="medium")
            decision = PermissionDecision.model_validate(response.tool_calls[0]["args"])
            if default_decision == "deny" and decision.action == "auto_approve":
                return PermissionDecision(action="escalate", justification="User-configured permissions deny this action.", risk="high")
            if not decision.justification.strip():
                return PermissionDecision(action="escalate", justification="Classifier did not provide a justification.", risk="medium")
            return decision
        except Exception as exception:
            return PermissionDecision(action="escalate", justification=f"{exception}", risk="medium")

    def _evaluate_bash_permission(self, command: str) -> str:
        unmatched = "ask" if self._interactive_manual_mode else "allow"
        return self._permissions.evaluate_bash_permission(command, unmatched=unmatched)

    async def _classify_permission(
        self,
        *,
        tool_kind: str,
        command: str,
        raw_command: str,
        default_decision: str,
        read_only: bool,
        risk: str,
        justification: str,
        static_classification: str = "",
        static_detail: str = "",
        outside_reads: Optional[list[str]] = None,
    ) -> PermissionDecision:
        context = json.dumps(
            {
                "tool_kind": tool_kind,
                "working_directory": self._working_directory,
                "command": command,
                "raw_command": raw_command,
                "default_permission_decision": default_decision,
                "model_declared_read_only": read_only,
                "model_declared_risk": risk,
                "model_justification": justification,
                "static_read_only_classification": static_classification,
                "static_detail": static_detail,
                "outside_working_directory_reads": outside_reads or [],
                "allowed_actions": ["auto_approve", "escalate"],
            },
            ensure_ascii=False,
        )
        prompt = self._prompt_loader.load("permission_classifier", {"context": context})
        try:
            model = self._llm.bind_tools([PermissionDecision], tool_choice="auto")
            response = await model.ainvoke([
                SystemMessage(content=prompt),
            ])
            if not response.tool_calls:
                return PermissionDecision(action="escalate", justification="Classifier returned no structured decision.", risk="medium")
            decision = PermissionDecision.model_validate(response.tool_calls[0]["args"])
            if default_decision == "deny" and decision.action == "auto_approve":
                return PermissionDecision(action="escalate", justification="User-configured permissions deny this action.", risk="high")
            if not decision.justification.strip():
                return PermissionDecision(action="escalate", justification="Classifier did not provide a justification.", risk="medium")
            return decision
        except Exception as exception:
            return PermissionDecision(action="escalate", justification=f"{exception}", risk="medium")

    def _new_permission_request_id(self) -> str:
        return f"perm-{self._session_id}-{uuid.uuid4()}"

    def _new_question_request_id(self) -> str:
        return f"q-{self._session_id}-{uuid.uuid4()}"

    async def _preflight_permissions(
        self, tool_calls: list[dict]
    ) -> tuple[dict[str, _ToolPlan], list[_PreflightGate]]:
        """Resolve the human-in-the-loop verdict for every tool call in a batch BEFORE
        any tool runs, so a pause can be checkpointed durably (concurrent tools cannot
        be re-run on resume without re-doing their side effects). Returns the per-call
        plans keyed by tool_call_id and the flat list of gates that need a human. When
        that list is non-empty the turn suspends; otherwise the batch executes with
        every decision already in hand."""
        plans: dict[str, _ToolPlan] = {}
        pending: list[_PreflightGate] = []
        for tool_call_data in tool_calls:
            plan = await self._classify_tool_permission(
                tool_call_data["name"], tool_call_data["args"], tool_call_data["id"],
            )
            plans[tool_call_data["id"]] = plan
            pending.extend(plan.gates)
        return plans, pending

    def _resolve_tool_decisions(
        self, plans: dict[str, _ToolPlan], answers: dict[str, Any]
    ) -> dict[str, _ResolvedToolDecision]:
        """Collapse the preflight plans plus any human answers into one verdict per tool.
        Used on both paths: the fresh path passes empty ``answers`` (plans with no gates),
        and the resumed path passes the answers keyed by ``request_id``. A tool runs only
        if every one of its gates was approved; any deny turns it into that gate's denial."""
        decisions: dict[str, _ResolvedToolDecision] = {}
        for tool_call_id, plan in plans.items():
            decision = _ResolvedToolDecision(tool_call_id=tool_call_id)
            if plan.denial is not None:
                decision.approved = False
                decision.denial = plan.denial
                decisions[tool_call_id] = decision
                continue
            for gate in plan.gates:
                answer = answers.get(gate.request_id)
                if gate.kind == "question":
                    # ask_user: the answers list, or the decline sentinel from the resolver.
                    decision.answers = answer
                    continue
                decision_value = str(answer) if answer is not None else "deny"
                if decision_value == "deny":
                    decision.approved = False
                    decision.denial = {"code": "", "message": gate.deny_message, "denied_injection": False, "raw_command": gate.command}
                    break
            decisions[tool_call_id] = decision
        return decisions


    async def _classify_tool_permission(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
    ) -> _ToolPlan:
        """The preflight verdict for one tool call: a hard denial, one or more pending
        gates, or auto-approved. This is the single place permission is decided — it
        reuses the same functions the inline gates used, so nothing that used to prompt
        or block silently becomes auto-approved. Setup errors (bad location, failed
        validation) are left to ``_execute_tool`` to surface; here a tool that cannot be
        set up simply yields no gate and is 'approved' to run, where the error is raised."""
        plan = _ToolPlan(tool_call_id=tool_call_identifier)
        schema = self._tool_schemas.get(tool_name)
        if schema is not None:
            tool_arguments = _coerce_structured_arguments(schema, tool_arguments)

        resolved_location: ResolvedLocation | None = None
        if tool_name in _LOCATION_TOOLS:
            tool_arguments = dict(tool_arguments)
            location_value = tool_arguments.pop("location", None) or None
            try:
                resolved_location = self._resolve_location(location_value)
            except ToolLocationError:
                # A bad location is an execution error, surfaced by _execute_tool; there
                # is no permission decision to make, so the batch runs and errors there.
                return plan
        policy = self._call_policy(resolved_location)

        if tool_name == "bash":
            raw_command = tool_arguments.get("command", "")
            justification = tool_arguments.get("justification", "")
            risk = tool_arguments.get("risk", "")
            read_only = tool_arguments.get("read_only", False)
            if isinstance(read_only, str):
                read_only = read_only.lower() == "true"
            static_classification, static_detail = self._agent_configuration.tools.bash.read_only_assessment(raw_command)
            outside_reads = (
                []
                if policy.is_remote
                else self._outside_working_directory_reads(raw_command, policy.working_directory)
            )
            # Sandbox read approval (reads outside the working directory).
            if outside_reads:
                paths = ", ".join(outside_reads)
                sandbox_message = (
                    f"Sandbox approval required: this command reads outside the working directory ({paths})."
                )
                # A delegated agent escalates a sandbox read to the user like any other gate
                # (it parks in place and resumes on the answer), rather than hard-denying:
                # every human-in-the-loop interrupt a delegated agent raises reaches the user.
                permission_decision = self._evaluate_bash_permission(raw_command)
                if policy.auto_permissions and permission_decision != "deny":
                    decision = await self._classify_permission(
                        tool_kind="bash", command=raw_command, raw_command=raw_command,
                        default_decision=permission_decision, read_only=read_only,
                        risk=risk or "medium", justification=justification or sandbox_message,
                        static_classification=static_classification, static_detail=static_detail,
                        outside_reads=outside_reads,
                    )
                    if decision.action == "auto_approve":
                        self._record_event("bash_auto_approved", {"command": raw_command, "reason": decision.justification, "risk": decision.risk})
                    else:
                        plan.gates.append(_PreflightGate(
                            request_id=self._new_permission_request_id(), tool_call_id=tool_call_identifier,
                            kind="permission", command=raw_command,
                            justification=decision.justification or sandbox_message, risk=decision.risk, is_bash=True,
                            deny_message="Sandbox read was not approved by the user.",
                        ))
                else:
                    if permission_decision == "deny":
                        plan.denial = {"code": "", "message": "Sandbox read denied by the default permission policy.", "denied_injection": False, "raw_command": raw_command}
                        return plan
                    plan.gates.append(_PreflightGate(
                        request_id=self._new_permission_request_id(), tool_call_id=tool_call_identifier,
                        kind="permission", command=raw_command, justification=sandbox_message, risk="medium", is_bash=True,
                        deny_message="Sandbox read was not approved by the user.",
                    ))
            # Read-only enforcement is a hard block (no human in the loop).
            if policy.read_only:
                violation = None
                if static_classification == "mutating":
                    violation = static_detail
                elif static_classification == "unknown" and not read_only:
                    violation = "a command not recognized as read-only that you marked as modifying state"
                if violation:
                    deny_message = self._prompt_loader.load("read_only_denied", {"violation": violation})
                    plan.denial = {"code": "", "message": deny_message, "denied_injection": True, "raw_command": raw_command}
                    return plan
            # Main command approval.
            permission_decision = self._evaluate_bash_permission(raw_command)
            if permission_decision == "deny":
                plan.denial = {"code": "", "message": f"Command '{raw_command}' is not permitted.", "denied_injection": True, "raw_command": raw_command}
                return plan
            elif permission_decision == "ask" or risk in ("medium", "high"):
                if policy.auto_permissions:
                    decision = await self._classify_permission(
                        tool_kind="bash", command=raw_command, raw_command=raw_command,
                        default_decision=permission_decision, read_only=read_only,
                        risk=risk or "medium", justification=justification,
                        static_classification=static_classification, static_detail=static_detail,
                        outside_reads=outside_reads,
                    )
                    if decision.action == "auto_approve":
                        self._record_event("bash_auto_approved", {"command": raw_command, "reason": decision.justification, "risk": decision.risk})
                    else:
                        plan.gates.append(_PreflightGate(
                            request_id=self._new_permission_request_id(), tool_call_id=tool_call_identifier,
                            kind="permission", command=raw_command,
                            justification=decision.justification or justification, risk=decision.risk, is_bash=True,
                            deny_message="Command was not approved by the user.",
                        ))
                else:
                    plan.gates.append(_PreflightGate(
                        request_id=self._new_permission_request_id(), tool_call_id=tool_call_identifier,
                        kind="permission", command=raw_command, justification=justification, risk=risk, is_bash=True,
                        deny_message="Command was not approved by the user.",
                    ))
            return plan

        if tool_name == "call_mcp_tool":
            read_only = tool_arguments.get("read_only", False)
            risk = tool_arguments.get("risk", "low")
            if policy.read_only and not read_only:
                deny_message = self._prompt_loader.load("read_only_denied", {"violation": "a mutating MCP tool call"})
                plan.denial = {"code": "", "message": deny_message, "denied_injection": False, "raw_command": ""}
                return plan
            if not read_only and risk in ("medium", "high"):
                action = f"MCP {tool_arguments.get('server', '')}.{tool_arguments.get('tool_name', '')}"
                justification = tool_arguments.get("justification", "")
                if policy.auto_permissions:
                    decision = await self._classify_permission(
                        tool_kind="mcp", command=action,
                        raw_command=json.dumps(tool_arguments.get("arguments") or {}, ensure_ascii=False),
                        default_decision="ask", read_only=False, risk=risk, justification=justification,
                    )
                    if decision.action == "auto_approve":
                        self._record_event("mcp_auto_approved", {
                            "server": tool_arguments.get("server", ""), "tool": tool_arguments.get("tool_name", ""),
                            "reason": decision.justification, "risk": decision.risk,
                        })
                    else:
                        plan.gates.append(_PreflightGate(
                            request_id=self._new_permission_request_id(), tool_call_id=tool_call_identifier,
                            kind="permission", command=action,
                            justification=decision.justification or justification, risk=decision.risk,
                            deny_message="MCP tool call not approved by user",
                        ))
                else:
                    plan.gates.append(_PreflightGate(
                        request_id=self._new_permission_request_id(), tool_call_id=tool_call_identifier,
                        kind="permission", command=action, justification=justification, risk=risk,
                        deny_message="MCP tool call not approved by user",
                    ))
            return plan

        if tool_name == "ask_user":
            plan.gates.append(_PreflightGate(
                request_id=self._new_question_request_id(), tool_call_id=tool_call_identifier,
                kind="question", questions=tool_arguments.get("questions", []) or [],
            ))
            return plan

        if tool_name == "control_screen":
            script = tool_arguments.get("script", "") or ""
            justification = tool_arguments.get("justification", "")
            risk = tool_arguments.get("risk", "") or ""
            static_classification, static_detail = _control_script_assessment(script)
            # Read-only enforcement is a hard block (no human in the loop): a mutating script cannot
            # run under a read-only policy; an unparseable one is treated as modifying, not waved through.
            if policy.read_only and static_classification in ("mutating", "unknown"):
                violation = f"a screen action that changes state ({static_detail})" if static_detail else "a screen action that changes state"
                deny_message = self._prompt_loader.load("read_only_denied", {"violation": violation})
                plan.denial = {"code": "", "message": deny_message, "denied_injection": False, "raw_command": ""}
                return plan
            if (static_classification != "read_only" or risk in ("medium", "high")):
                if policy.auto_permissions:
                    decision = await self._classify_permission(
                        tool_kind="screen", command="control_screen", raw_command=script,
                        default_decision="ask", read_only=(static_classification == "read_only"),
                        risk=risk or "medium", justification=justification,
                        static_classification=static_classification, static_detail=static_detail,
                    )
                    if decision.action == "auto_approve":
                        self._record_event("screen_auto_approved", {"reason": decision.justification, "risk": decision.risk})
                    else:
                        plan.gates.append(_PreflightGate(
                            request_id=self._new_permission_request_id(), tool_call_id=tool_call_identifier,
                            kind="permission", command="control_screen",
                            justification=decision.justification or justification, risk=decision.risk,
                            deny_message="Screen action not approved by user",
                        ))
                else:
                    plan.gates.append(_PreflightGate(
                        request_id=self._new_permission_request_id(), tool_call_id=tool_call_identifier,
                        kind="permission", command="control_screen", justification=justification, risk=risk or "medium",
                        deny_message="Screen action not approved by user",
                    ))
            return plan

        return plan
