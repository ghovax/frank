"""Carrying out the boundary's verdict: raising gates, reviewing them, recording grants, offering retries."""

from __future__ import annotations

from langmesh.runtime.internals import (
    _coerce_structured_arguments,
    _PreflightGate,
    _ResolvedToolDecision,
    _ToolPlan,
)
from langmesh.base import confinement
from langmesh.base.confinement import Grant, parse_access_request
from langmesh.protocol.events import PermissionReason
from langmesh.runtime.boundary import RULE_ALLOW, RULE_ASK, escape_of, verdict_for
from langmesh.runtime.locations import (
    _LOCATION_TOOLS,
    PermissionDecision,
    ResolvedLocation,
    ToolLocationError,
)
from langchain_core.messages import HumanMessage, SystemMessage
from typing import Any, Optional
import ast
import logging
import uuid
from langmesh.base.serialization import compact
from langmesh.base.tuning import Tunable, active_tuning

logger = logging.getLogger(__name__)

# The screen primitives that change something. The set the gate is about, and the one the bridge sends.
MUTATING_SCREEN_PRIMITIVES = frozenset(
    {
        "click",
        "type",
        "choose",
        "upload",
        "drag",
        "evaluate",
        "press",
        "navigate",
        "new_tab",
        "close_tab",
        "caret",
        "select",
    }
)


def _screen_primitive(func: ast.expr) -> str:
    """The primitive a call node names, bare or through ``screen``."""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _screen_mutations(script: str) -> tuple[str, ...]:
    """The state-changing primitives a script calls. Decides who is asked, never what is available."""
    try:
        tree = ast.parse(script)
    except SyntaxError:
        # Unparseable: the tool will fail to run it, so there is nothing to name.
        return ()
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _screen_primitive(node.func)
            if name in MUTATING_SCREEN_PRIMITIVES and name not in found:
                found.append(name)
    return tuple(found)


class _DecidesPermissions:
    """Whether a call runs, is asked about, or is refused."""

    # ---- the reviewer -------------------------------------------------------------------

    def _reviewer_model(self):
        """The model that reviews a request: the session's own, at a lower effort, built once."""
        if self._reviewer_llm is not None:
            return self._reviewer_llm
        # A caller that passed a model object has no identifier to rebuild from, so the judge is that object.
        identifier = self.effective_model_identifier
        if not identifier:
            self._reviewer_llm = self._llm
            return self._reviewer_llm
        from langmesh.runtime.runtime import build_chat_model

        effort = self._global_configuration.permission_reviewer.reasoning_effort
        self._reviewer_llm = build_chat_model(
            identifier,
            self._global_configuration,
            self._agent_configuration.model_copy(update={"reasoning_effort": effort}),
            self._working_directory,
            session_id=self._session_id,
        )
        return self._reviewer_llm

    async def _review(self, gate: _PreflightGate) -> PermissionDecision:
        """The reviewer's verdict on one gate. Takes a gate, so it cannot reach a call that raised none."""
        context = compact(
            {
                "tool": gate.tool_name,
                "working_directory": self._working_directory,
                "command": gate.command,
                "arguments": gate.arguments,
                "requested_access": {
                    "reads": list(gate.escape.reads),
                    "writes": list(gate.escape.writes),
                    "network": gate.escape.network,
                    "whole_disk": gate.whole_disk,
                },
                "model_explanation": gate.arguments.get("explanation", "") or gate.explanation,
                "confinement": self._sandbox.describe(workspace=self._working_directory),
                # Only on a second run, so the reviewer knows the command hit a wall rather than merely failed.
                **(
                    {"refused_by_the_sandbox": gate.denial_evidence} if gate.denial_evidence else {}
                ),
                "allowed_actions": ["allow", "deny"],
            }
        )
        prompt = self._prompt_loader.load(
            "permission_reviewer",
            {
                "thinking_language": self._prompt_loader.load("thinking_language", {}).strip(),
                "toolbox": (
                    self._prompt_loader.load("reviewer_toolbox", {})
                    if getattr(self._tool_context, "toolbox", None) is not None
                    else ""
                ),
            },
        )
        # Instructions and subject as separate messages: some providers reject a request with no input.
        model = self._reviewer_model().bind_tools([PermissionDecision], tool_choice="auto")
        request = [SystemMessage(content=prompt), HumanMessage(content=context)]
        attempts = active_tuning().amount(Tunable.permission_reviewer_attempts)
        for attempt in range(1, attempts + 1):
            try:
                response = await model.ainvoke(request)
            except Exception:  # noqa: BLE001 — one dropped call is not a verdict
                logger.warning(
                    "the permission reviewer could not be reached (attempt %d of %d)",
                    attempt,
                    attempts,
                    exc_info=True,
                )
                continue
            if not response.tool_calls:
                logger.warning(
                    "the permission reviewer answered without a decision (attempt %d of %d)",
                    attempt,
                    attempts,
                )
                continue
            try:
                decision = PermissionDecision.model_validate(response.tool_calls[0]["args"])
            except Exception:  # noqa: BLE001 — a malformed verdict is not a verdict either
                logger.warning(
                    "the permission reviewer returned a malformed decision (attempt %d of %d)",
                    attempt,
                    attempts,
                    exc_info=True,
                )
                continue
            # A verdict with no reason cannot be acted on. A failed attempt, not a refusal — the next may supply it.
            if not decision.explanation.strip():
                logger.warning(
                    "the permission reviewer gave no reason for its decision (attempt %d of %d)",
                    attempt,
                    attempts,
                )
                continue
            return decision
        logger.warning("the permission reviewer did not decide in %d attempts; denying", attempts)
        return PermissionDecision(
            action="deny",
            explanation="The safety check could not run, so this request was refused.",
            risk="medium",
        )

    # ---- grants -------------------------------------------------------------------------

    def _record_grant(self, grant: Grant) -> None:
        """Remember an approved widening for the session, so one grant is not re-asked on every command."""
        self._access_grants.append(grant)
        self._record_event(
            "access_granted",
            {
                "reads": list(grant.reads),
                "writes": list(grant.writes),
                "network": grant.network,
                "whole_disk": grant.whole_disk,
                "purpose": grant.purpose,
                "approved_by": grant.approved_by,
            },
        )

    def _granted_profile(self):
        """The session's confinement with every standing grant folded in. What an escape is measured against."""
        profile = self._sandbox
        for grant in self._access_grants:
            profile = profile.with_grant(grant, workspace=self._working_directory or "")
        return profile

    # ---- ids ----------------------------------------------------------------------------

    def _new_permission_request_id(self, tool_call_id: str = "") -> str:
        """The id an answer is filed under, derived from the call so a replanned gate is the same gate."""
        return f"perm-{self._session_id}-{tool_call_id or uuid.uuid4()}"

    def _new_question_request_id(self, tool_call_id: str = "") -> str:
        """Stable for the same reason, and by the same means, as the permission id above."""
        return f"q-{self._session_id}-{tool_call_id or uuid.uuid4()}"

    def _new_retry_request_id(self, tool_call_id: str) -> str:
        """The id for a second run, distinct from the preflight id since both can exist in one turn."""
        return f"retry-{self._session_id}-{tool_call_id}"

    # ---- the planner --------------------------------------------------------------------

    async def _preflight_permissions(
        self, tool_calls: list[dict]
    ) -> tuple[dict[str, _ToolPlan], list[_PreflightGate]]:
        """Every call's verdict, resolved before any tool runs, so a pause can be checkpointed durably."""
        plans: dict[str, _ToolPlan] = {}
        pending: list[_PreflightGate] = []
        for tool_call_data in tool_calls:
            plan = await self._plan_call(
                tool_call_data["name"],
                tool_call_data["args"],
                tool_call_data["id"],
            )
            # A gate is raised before its call is announced, so it carries what is being asked for.
            for gate in plan.gates:
                gate.tool_name = tool_call_data["name"]
                gate.arguments = dict(tool_call_data["args"] or {})
            plans[tool_call_data["id"]] = plan
            pending.extend(plan.gates)
        return plans, pending

    async def _plan_call(
        self,
        tool_name: str,
        tool_arguments: dict,
        tool_call_identifier: str,
    ) -> _ToolPlan:
        """The verdict for one call. One path for every tool; only the rule table and the escape differ."""
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
                # A bad location is an execution error, raised by _execute_tool rather than decided here.
                return plan
        policy = self._call_policy(resolved_location)
        explanation = str(tool_arguments.get("explanation", "") or "")

        # ask_user is the one call that is a question rather than an act.
        if tool_name == "ask_user":
            # The second lock: the tool is already withheld under `automatic`, but a stale plan could still name it.
            if not policy.asks:
                plan.refusal = self._refusal(self._prompt_loader.load("nobody_to_ask", {}))
                return plan
            plan.gates.append(
                _PreflightGate(
                    request_id=self._new_question_request_id(tool_call_identifier),
                    tool_call_id=tool_call_identifier,
                    kind="question",
                    questions=tool_arguments.get("questions", []) or [],
                )
            )
            return plan

        subject, rule = self._rule_for(tool_name, tool_arguments)
        # A remote call has no box here to escape, so the rules are the whole policy.
        profile = None if policy.is_remote else self._granted_profile()
        request, _ = parse_access_request(tool_arguments.get("access_request"))
        escape = escape_of(request, profile, workspace=policy.working_directory)

        # A screen script's box is the primitives it was handed, so escaping it means changing something.
        mutations: tuple[str, ...] = ()
        if tool_name == "control_screen":
            mutations = _screen_mutations(str(tool_arguments.get("script", "") or ""))
            if mutations and rule == RULE_ALLOW:
                plan.screen_mutations = True

        verdict = verdict_for(
            escape=escape,
            rule=rule,
            profile=profile,
            workspace=policy.working_directory,
        )
        if verdict.kind == "refuse":
            plan.refusal = self._refusal(verdict.message, reason=verdict.reason, subject=subject)
            return plan
        needs_screen_gate = bool(mutations) and rule != RULE_ALLOW
        if verdict.runs and not needs_screen_gate:
            return plan

        gate = _PreflightGate(
            request_id=self._new_permission_request_id(tool_call_identifier),
            tool_call_id=tool_call_identifier,
            kind="permission",
            command=self._command_of(tool_name, tool_arguments) or subject,
            explanation=escape.summary(explanation) if escape else explanation,
            reason=verdict.reason
            or (
                PermissionReason(kind="changes_the_screen", paths=list(mutations))
                if mutations
                else PermissionReason(kind="asked_for_by_rule")
            ),
            escape=escape,
            grants_screen_mutations=bool(mutations),
            is_bash=(tool_name == "bash"),
            deny_message=self._deny_message(tool_name),
        )
        # Under `automatic` every gate goes to the reviewer; one that skipped it would park the session.
        if not policy.asks:
            decision = await self._review(gate)
            if decision.action == "allow":
                self._approve(gate, by=confinement.APPROVED_BY_REVIEWER, plan=plan)
                self._record_event(
                    "access_allowed",
                    {
                        "tool": tool_name,
                        "reason": decision.explanation,
                        "risk": decision.risk,
                    },
                )
                return plan
            plan.refusal = self._refusal(
                self._prompt_loader.load(
                    "reviewer_denied",
                    {
                        "reason": decision.explanation
                        or "the safety check would not vouch for this",
                    },
                ),
            )
            self._record_event(
                "access_refused",
                {
                    "tool": tool_name,
                    "reason": decision.explanation,
                    "risk": decision.risk,
                },
            )
            return plan
        plan.gates.append(gate)
        return plan

    def _rule_for(self, tool_name: str, tool_arguments: dict) -> tuple[str, str]:
        """What the configuration says about this call. The subject differs by tool, because the calls do."""
        tools = self._agent_configuration.tools
        if tool_name == "bash":
            command = str(tool_arguments.get("command", "") or "")
            return command, tools.bash.evaluate_permission(command, unmatched=RULE_ALLOW)
        if tool_name == "call_mcp_tool":
            subject = f"{tool_arguments.get('server', '')}.{tool_arguments.get('tool_name', '')}"
            return subject, tools.mcp.decide(subject, unmatched=RULE_ASK)
        if tool_name == "control_screen":
            mutations = _screen_mutations(str(tool_arguments.get("script", "") or ""))
            subject = mutations[0] if mutations else "read"
            return subject, tools.screen.decide(
                subject, unmatched=RULE_ASK if mutations else RULE_ALLOW
            )
        if tool_name in self._extra_tools:
            # A supplied tool is unknown to the engine, so it is asked about unless the caller said otherwise.
            return tool_name, RULE_ALLOW if self._supplied_tool_gate == "none" else RULE_ASK
        return tool_name, RULE_ALLOW

    def _command_of(self, tool_name: str, tool_arguments: dict) -> str:
        """What the person deciding is shown as *the thing being done*."""
        if tool_name == "bash":
            return str(tool_arguments.get("command", "") or "")
        if tool_name == "call_mcp_tool":
            return f"MCP {tool_arguments.get('server', '')}.{tool_arguments.get('tool_name', '')}"
        return tool_name

    def _deny_message(self, tool_name: str) -> str:
        """The model-facing sentence when a gate is answered no."""
        if tool_name == "bash":
            return "Command was not approved."
        if tool_name == "call_mcp_tool":
            return "The MCP call was not approved."
        if tool_name == "control_screen":
            return "The screen action was not approved."
        return f"{tool_name} was not approved."

    def _refusal(
        self,
        message: str,
        *,
        reason: Optional[PermissionReason] = None,
        subject: str = "",
    ) -> dict:
        """A hard refusal, in the shape the dispatcher surfaces."""
        return {
            "code": "",
            "message": message,
            "denied_injection": bool(subject),
            "raw_command": subject,
            "reason": reason.model_dump() if reason is not None else None,
        }

    def _approve(self, gate: _PreflightGate, *, by: str, plan: Optional[_ToolPlan] = None) -> None:
        """Carry out what approving a gate means, in one place, since it can grant two different things."""
        if gate.escape or gate.whole_disk:
            self._record_grant(
                confinement.approved(
                    confinement.AccessRequest(
                        mutates=True,
                        reads=gate.escape.reads,
                        writes=gate.escape.writes,
                        network=gate.escape.network,
                    ),
                    by=by,
                    purpose=gate.arguments.get("explanation", "") or gate.explanation,
                    whole_disk=gate.whole_disk,
                )
            )
        if gate.grants_screen_mutations and plan is not None:
            plan.screen_mutations = True

    # ---- resolution ---------------------------------------------------------------------

    def _resolve_tool_decisions(
        self, plans: dict[str, _ToolPlan], answers: dict[str, Any]
    ) -> dict[str, _ResolvedToolDecision]:
        """Plans plus answers into one verdict per tool. A tool runs only if every gate it raised was approved."""
        decisions: dict[str, _ResolvedToolDecision] = {}
        for tool_call_id, plan in plans.items():
            decision = _ResolvedToolDecision(tool_call_id=tool_call_id)
            decision.screen_mutations = plan.screen_mutations
            decision.retry_grant = plan.retry_grant
            if plan.refusal is not None:
                decision.approved = False
                decision.denial = plan.refusal
                decisions[tool_call_id] = decision
                continue
            for gate in plan.gates:
                answer = answers.get(gate.request_id)
                if gate.kind == "question":
                    # ask_user: the answers list, or the decline sentinel from the resolver.
                    decision.answers = answer
                    continue
                approved = answer is not None and str(answer) != "deny"
                if not approved:
                    if gate.refused_result is not None:
                        # A refused retry still owes the model what the confined run actually did.
                        decision.completed = {"result": gate.refused_result}
                        break
                    decision.approved = False
                    decision.denial = {
                        "code": "",
                        "message": gate.deny_message,
                        "denied_injection": False,
                        "raw_command": gate.command,
                        "reason": None,
                    }
                    break
                # Recorded here, the only point that knows somebody said yes; preflight alone grants nothing.
                self._approve(gate, by=confinement.APPROVED_BY_PERSON)
                if gate.grants_screen_mutations:
                    decision.screen_mutations = True
                if gate.whole_disk:
                    decision.retry_grant = confinement.approved(
                        by=confinement.APPROVED_BY_PERSON,
                        purpose=gate.arguments.get("explanation", "") or gate.explanation,
                        whole_disk=True,
                    )
            decisions[tool_call_id] = decision
        return decisions

    # ---- the second run -----------------------------------------------------------------

    def retry_gate(
        self,
        *,
        tool_call_id: str,
        command: str,
        denial: confinement.Denial,
        explanation: str,
    ) -> _PreflightGate:
        """The gate a refused command raises. The offer is whole-disk because no backend reports the path."""
        return _PreflightGate(
            request_id=self._new_retry_request_id(tool_call_id),
            tool_call_id=tool_call_id,
            kind="permission",
            command=command,
            explanation=explanation,
            is_bash=True,
            whole_disk=True,
            reason=PermissionReason(kind="refused_by_confinement", paths=[denial.kind]),
            denial_evidence=denial.evidence,
            deny_message="The command was refused by the sandbox and was not re-run.",
        )

    async def reconsider_gate(self, gate) -> str:
        """Re-decide a gate the session is parked on, under the mode now in force."""
        if self._call_policy(None).asks:
            # Interactive: a question is exactly what a person is for.
            return ""
        if gate.kind == "question":
            # A reviewer cannot answer for the person an `ask_user` was addressed to.
            return "deny"
        decision = await self._review(
            _PreflightGate.from_dict(gate.to_dict() if hasattr(gate, "to_dict") else vars(gate))
        )
        return "allow" if decision.action == "allow" else "deny"

    async def decide_retry(self, gate: _PreflightGate) -> tuple[str, Optional[Grant]]:
        """What to do with a retry gate: ask, run with a grant, or refuse. Three answers, not an optional grant."""
        if self._call_policy(None).asks:
            return "ask", None
        decision = await self._review(gate)
        if decision.action != "allow":
            self._record_event(
                "retry_refused",
                {
                    "command": gate.command,
                    "reason": decision.explanation,
                },
            )
            return "refuse", None
        self._record_event(
            "retry_allowed",
            {
                "command": gate.command,
                "reason": decision.explanation,
            },
        )
        return "run", confinement.approved(
            by=confinement.APPROVED_BY_REVIEWER,
            purpose=gate.explanation,
            whole_disk=True,
        )
