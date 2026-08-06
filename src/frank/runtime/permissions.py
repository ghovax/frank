"""The AgentRuntime permission concern (a mixin composed into AgentRuntime).

One question, asked the same way for every tool: does this call reach past the box the session
runs in? :mod:`frank.runtime.boundary` answers it; this module carries the answer out — raising
a gate for a person, putting it to the reviewer where nobody is watching, remembering what was
approved, and offering a second run of a command the operating system refused.

A call runs inside its confinement or it asks to leave it, and those two sentences are the whole
policy.
"""
from __future__ import annotations

from frank.runtime.internals import (
    _coerce_structured_arguments,
    _PreflightGate,
    _ResolvedToolDecision,
    _ToolPlan,
)
from frank.base import confinement
from frank.base.confinement import Grant, parse_access_request
from frank.protocol.events import PermissionReason
from frank.runtime.boundary import Escape, RULE_ALLOW, RULE_ASK, RULE_DENY, escape_of, verdict_for
from frank.runtime.locations import (
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
from frank.base.serialization import compact
from frank.base.tuning import Tunable, active_tuning

logger = logging.getLogger(__name__)

# The state-changing control_screen primitives.
MUTATING_SCREEN_PRIMITIVES = frozenset({
    "click", "type", "choose", "upload", "drag",
    "evaluate", "press", "navigate",
    "new_tab", "close_tab",
    "caret", "select",
})


def _screen_primitive(func: ast.expr) -> str:
    """The primitive a call node names, however the script spells it. ``screen.click(...)`` and a
    bare ``click(...)`` are the same act, so both forms are read."""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _screen_mutations(script: str) -> tuple[str, ...]:
    """The state-changing primitives a script calls, in the order they first appear.

    A reading of the script's own call names, and nothing more, because nothing more is needed:
    the child holds only the primitives it was sent, so a mutation hidden inside an imported
    module is refused at the bridge whether or not anything read that module. What this answers
    is whether somebody is asked, never whether the primitive is available.
    """
    try:
        tree = ast.parse(script)
    except SyntaxError:
        # Nothing to decide: the tool will fail to run this, and refusing to name a primitive is not the same as naming one.
        return ()
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _screen_primitive(node.func)
            if name in MUTATING_SCREEN_PRIMITIVES and name not in found:
                found.append(name)
    return tuple(found)


class _DecidesPermissions:
    """Whether a call runs, is asked about, or is refused.

    Composed into :class:`AgentRuntime` beside the dispatcher it answers for."""

    # ---- the reviewer -------------------------------------------------------------------

    def _reviewer_model(self):
        """The model that reviews a request: the session's own, thinking less.

        The same model as the agent, because a judge that knows a different world than the thing
        it judges is a judge arguing from somewhere else — and the same reason it gets the
        session's own prompt fragments. What differs is effort: the agent's setting was chosen
        for the work, and this is one request weighed against a page of rules.

        Built once and kept: a client per call would rebuild the provider's transport for a
        question that takes a second to answer."""
        if self._reviewer_llm is not None:
            return self._reviewer_llm
        # A caller that handed this runtime a model object rather than naming one — the library front door does exactly that — has no identifier to rebuild from, so the judge is that same object at whatever effort it was built with.
        identifier = self.effective_model_identifier
        if not identifier:
            self._reviewer_llm = self._llm
            return self._reviewer_llm
        from frank.runtime.runtime import build_chat_model

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
        """The reviewer's verdict on one gate.

        It takes a gate, so it can only answer a question that was going to be put to somebody:
        where a person would have been asked and there is no person, this is who answers instead.
        It cannot reach a call that raised no gate, and so cannot become a second policy running
        beside the rules.

        Fails closed, after bounded attempts. A judge that cannot be reached is not a verdict,
        and one dropped request should not cost the work; but once the attempts are spent there
        is nobody to hand the question to, so the answer is no.
        """
        context = compact({
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
            # Present only for a second run: the first was refused by the operating system, and a reviewer that did not know that would be judging a command that appears to have simply failed.
            **({"refused_by_the_sandbox": gate.denial_evidence} if gate.denial_evidence else {}),
            "allowed_actions": ["allow", "deny"],
        })
        prompt = self._prompt_loader.load("permission_reviewer", {
            "thinking_language": self._prompt_loader.load("thinking_language", {}).strip(),
            "toolbox": (
                self._prompt_loader.load("reviewer_toolbox", {})
                if getattr(self._tool_context, "toolbox", None) is not None else ""
            ),
        })
        # Instructions and subject as separate messages.
        model = self._reviewer_model().bind_tools([PermissionDecision], tool_choice="auto")
        request = [SystemMessage(content=prompt), HumanMessage(content=context)]
        attempts = active_tuning().amount(Tunable.permission_reviewer_attempts)
        for attempt in range(1, attempts + 1):
            try:
                response = await model.ainvoke(request)
            except Exception:  # noqa: BLE001 — one dropped call is not a verdict
                logger.warning(
                    "the permission reviewer could not be reached (attempt %d of %d)",
                    attempt, attempts, exc_info=True,
                )
                continue
            if not response.tool_calls:
                logger.warning(
                    "the permission reviewer answered without a decision (attempt %d of %d)",
                    attempt, attempts,
                )
                continue
            try:
                decision = PermissionDecision.model_validate(response.tool_calls[0]["args"])
            except Exception:  # noqa: BLE001 — a malformed verdict is not a verdict either
                logger.warning(
                    "the permission reviewer returned a malformed decision (attempt %d of %d)",
                    attempt, attempts, exc_info=True,
                )
                continue
            # A reason is not decoration: it is the whole of what the agent is told, and a verdict without one cannot be acted on.
            if not decision.explanation.strip():
                logger.warning(
                    "the permission reviewer gave no reason for its decision (attempt %d of %d)",
                    attempt, attempts,
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
        """Remember an approved widening for the rest of the session.

        A grant persists, so a build that writes eleven files under one granted directory raises
        one gate and not eleven. That is not a convenience: how often a person is asked is a
        security property, and it points the opposite way to intuition. An approval put in front
        of somebody every few seconds stops being read, and a prompt nobody reads approves
        everything."""
        self._access_grants.append(grant)
        self._record_event("access_granted", {
            "reads": list(grant.reads), "writes": list(grant.writes),
            "network": grant.network, "whole_disk": grant.whole_disk,
            "purpose": grant.purpose, "approved_by": grant.approved_by,
        })

    def _granted_profile(self):
        """The session's confinement with every standing grant folded in.

        What :func:`~frank.runtime.boundary.escape_of` is measured against, so a path approved
        earlier in the session is not an escape any more. One derivation, used by the planner
        and by the tool context alike, because two would disagree about what had been approved.
        """
        profile = self._sandbox
        for grant in self._access_grants:
            profile = profile.with_grant(grant, workspace=self._working_directory or "")
        return profile

    # ---- ids ----------------------------------------------------------------------------

    def _new_permission_request_id(self, tool_call_id: str = "") -> str:
        """The id a person's answer is filed under. Derived from the call, not minted fresh.

        Preflight runs again when a suspended turn resumes — the batch has not executed, so it
        is planned again — and a random id meant the resumed plan asked under a *new* name while
        the answer sat under the old one. Nothing matched, so the turn re-gated and ended without
        running the tool, and the person who had just clicked Allow was told their request was no
        longer active. Deriving it from the tool call makes the second ask the same ask."""
        return f"perm-{self._session_id}-{tool_call_id or uuid.uuid4()}"

    def _new_question_request_id(self, tool_call_id: str = "") -> str:
        """Stable for the same reason, and by the same means, as the permission id above."""
        return f"q-{self._session_id}-{tool_call_id or uuid.uuid4()}"

    def _new_retry_request_id(self, tool_call_id: str) -> str:
        """The id for a second run of a command the operating system refused. Distinct from the
        preflight id for the same call, because both can exist in one turn: the call was
        approved to run, ran, and hit the wall."""
        return f"retry-{self._session_id}-{tool_call_id}"

    # ---- the planner --------------------------------------------------------------------

    async def _preflight_permissions(
        self, tool_calls: list[dict]
    ) -> tuple[dict[str, _ToolPlan], list[_PreflightGate]]:
        """Resolve the verdict for every tool call in a batch BEFORE any tool runs, so a pause
        can be checkpointed durably (concurrent tools cannot be re-run on resume without
        re-doing their side effects). Returns the per-call plans keyed by tool_call_id and the
        flat list of gates that need an answer. When that list is non-empty the turn suspends;
        otherwise the batch executes with every decision already in hand."""
        plans: dict[str, _ToolPlan] = {}
        pending: list[_PreflightGate] = []
        for tool_call_data in tool_calls:
            plan = await self._plan_call(
                tool_call_data["name"], tool_call_data["args"], tool_call_data["id"],
            )
            # Stamp the call's identity onto every gate it raised, here rather than at each construction site.
            for gate in plan.gates:
                gate.tool_name = tool_call_data["name"]
                gate.arguments = dict(tool_call_data["args"] or {})
            plans[tool_call_data["id"]] = plan
            pending.extend(plan.gates)
        return plans, pending

    async def _plan_call(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
    ) -> _ToolPlan:
        """The verdict for one tool call: refused, gated, or cleared to run.

        One path for every tool. What differs between them is two lines — which rule table
        names this kind of call, and what the call is asking to reach — and everything after
        that is shared, so there is no tool whose permission story can drift away from the
        others'.
        """
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
                # A bad location is an execution error, surfaced by _execute_tool; there is no permission decision to make, so the batch runs and errors there.
                return plan
        policy = self._call_policy(resolved_location)
        explanation = str(tool_arguments.get("explanation", "") or "")

        # ask_user is the one call that is a question rather than an act.
        if tool_name == "ask_user":
            # Not offered under `auto` — the tool is left out of the session's set entirely — so this is the second lock rather than the first.
            if not policy.asks:
                plan.refusal = self._refusal(self._prompt_loader.load("nobody_to_ask", {}))
                return plan
            plan.gates.append(_PreflightGate(
                request_id=self._new_question_request_id(tool_call_identifier),
                tool_call_id=tool_call_identifier, kind="question",
                questions=tool_arguments.get("questions", []) or [],
            ))
            return plan

        subject, rule = self._rule_for(tool_name, tool_arguments)
        # A remote location runs on somebody else's machine, where this machine's confinement says nothing.
        profile = None if policy.is_remote else self._granted_profile()
        request, _ = parse_access_request(tool_arguments.get("access_request"))
        escape = escape_of(request, profile, workspace=policy.working_directory)

        # A screen script has a confinement of its own — the primitives its child was handed — so "reaching outside" means asking to change something rather than only look.
        mutations: tuple[str, ...] = ()
        if tool_name == "control_screen":
            mutations = _screen_mutations(str(tool_arguments.get("script", "") or ""))
            if mutations and rule == RULE_ALLOW:
                plan.screen_mutations = True

        verdict = verdict_for(
            escape=escape, rule=rule, profile=profile, workspace=policy.working_directory,
        )
        if verdict.kind == "refuse":
            plan.refusal = self._refusal(verdict.message, reason=verdict.reason, subject=subject)
            return plan
        needs_screen_gate = bool(mutations) and rule != RULE_ALLOW
        if verdict.runs and not needs_screen_gate:
            return plan

        gate = _PreflightGate(
            request_id=self._new_permission_request_id(tool_call_identifier),
            tool_call_id=tool_call_identifier, kind="permission",
            command=self._command_of(tool_name, tool_arguments) or subject,
            explanation=escape.summary(explanation) if escape else explanation,
            reason=verdict.reason or (
                PermissionReason(kind="changes_the_screen", paths=list(mutations))
                if mutations else PermissionReason(kind="asked_for_by_rule")
            ),
            escape=escape,
            grants_screen_mutations=bool(mutations),
            is_bash=(tool_name == "bash"),
            deny_message=self._deny_message(tool_name),
        )
        # Under `auto` the gate is answered here rather than put to somebody.
        if not policy.asks:
            decision = await self._review(gate)
            if decision.action == "allow":
                self._approve(gate, by=confinement.APPROVED_BY_REVIEWER, plan=plan)
                self._record_event("access_allowed", {
                    "tool": tool_name, "reason": decision.explanation, "risk": decision.risk,
                })
                return plan
            plan.refusal = self._refusal(
                self._prompt_loader.load("reviewer_denied", {
                    "reason": decision.explanation or "the safety check would not vouch for this",
                }),
            )
            self._record_event("access_refused", {
                "tool": tool_name, "reason": decision.explanation, "risk": decision.risk,
            })
            return plan
        plan.gates.append(gate)
        return plan

    def _rule_for(self, tool_name: str, tool_arguments: dict) -> tuple[str, str]:
        """What the person's configuration says about this call: ``(subject, decision)``.

        The subject is the thing a rule is written *about*, and it differs by tool because the
        calls do: a shell command is matched against its segments, an MCP call is `server.tool`,
        and a screen script is the primitive it reaches for. Unmatched is ``allow`` everywhere
        the confinement is the real boundary, and ``ask`` where it is not — an MCP server runs
        outside this machine's sandbox entirely, so nothing but the rules stands in front of it.
        """
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
            return subject, tools.screen.decide(subject, unmatched=RULE_ASK if mutations else RULE_ALLOW)
        if tool_name in self._extra_tools:
            # A tool the caller supplied.
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
        self, message: str, *, reason: Optional[PermissionReason] = None, subject: str = "",
    ) -> dict:
        """A hard refusal, in the shape the dispatcher surfaces."""
        return {
            "code": "", "message": message, "denied_injection": bool(subject),
            "raw_command": subject,
            "reason": reason.model_dump() if reason is not None else None,
        }

    def _approve(self, gate: _PreflightGate, *, by: str, plan: Optional[_ToolPlan] = None) -> None:
        """Carry out what approving this gate means.

        One place, because a gate can grant two different things and a call site that handled
        one of them would silently drop the other."""
        if gate.escape or gate.whole_disk:
            self._record_grant(confinement.approved(
                confinement.AccessRequest(
                    mutates=True, reads=gate.escape.reads, writes=gate.escape.writes,
                    network=gate.escape.network,
                ),
                by=by,
                purpose=gate.arguments.get("explanation", "") or gate.explanation,
                whole_disk=gate.whole_disk,
            ))
        if gate.grants_screen_mutations and plan is not None:
            plan.screen_mutations = True

    # ---- resolution ---------------------------------------------------------------------

    def _resolve_tool_decisions(
        self, plans: dict[str, _ToolPlan], answers: dict[str, Any]
    ) -> dict[str, _ResolvedToolDecision]:
        """Collapse the preflight plans plus any answers into one verdict per tool.
        Used on both paths: the fresh path passes empty ``answers`` (plans with no gates), and
        the resumed path passes the answers keyed by ``request_id``. A tool runs only if every
        one of its gates was approved; any refusal turns it into that gate's denial."""
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
                        # A refused retry is not a refused call: the command ran, inside its box, and what the model is owed is what actually happened rather than a sentence about an approval it never had.
                        decision.completed = {"result": gate.refused_result}
                        break
                    decision.approved = False
                    decision.denial = {
                        "code": "", "message": gate.deny_message,
                        "denied_injection": False, "raw_command": gate.command,
                        "reason": None,
                    }
                    break
                # Approved.
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
        self, *, tool_call_id: str, command: str, denial: confinement.Denial, explanation: str,
    ) -> _PreflightGate:
        """The gate a command raises after the operating system refused it.

        This is the whole of what a person is offered, and the offer is narrow on purpose. The
        refusal names no path — neither backend reports one — so there is nothing to widen
        precisely, and inventing a path from the command text would be guessing at exactly the
        thing this harness stopped guessing at. What is offered instead is: let this one command
        reach past the workspace.

        Safe to offer because the first run was confined and could not have been otherwise:
        :func:`~frank.base.confinement.first_attempt` takes no grant. Whatever the command did
        before the wall, it did inside the box.
        """
        return _PreflightGate(
            request_id=self._new_retry_request_id(tool_call_id),
            tool_call_id=tool_call_id, kind="permission", command=command,
            explanation=explanation, is_bash=True, whole_disk=True,
            reason=PermissionReason(kind="refused_by_confinement", paths=[denial.kind]),
            denial_evidence=denial.evidence,
            deny_message="The command was refused by the sandbox and was not re-run.",
        )

    async def reconsider_gate(self, gate) -> str:
        """Decide a gate the session is *already parked on*, under the mode it is under now.

        A gate is a question that has been asked, and changing the policy does not unask it: the
        turn that raised it has ended, its verdict sits in the task record, and nothing re-reads
        that record. So somebody who switches to `auto` precisely because they are tired of being
        asked watches the same card go on asking.

        Answers ``"allow"``, ``"deny"``, or ``""`` for "still a question". The same reviewer the
        preflight would have used, rather than a second policy that could drift from the first.
        """
        if self._call_policy(None).asks:
            # Interactive: a question is exactly what a person is for.
            return ""
        if gate.kind == "question":
            # `ask_user` has nobody to answer it now, and a reviewer cannot answer for the person it was addressed to.
            return "deny"
        decision = await self._review(_PreflightGate.from_dict(
            gate.to_dict() if hasattr(gate, "to_dict") else vars(gate)
        ))
        return "allow" if decision.action == "allow" else "deny"

    async def decide_retry(self, gate: _PreflightGate) -> tuple[str, Optional[Grant]]:
        """What to do with a retry gate: ``("ask", None)``, ``("run", grant)`` or
        ``("refuse", None)``.

        Three answers rather than an optional grant, because "put this to a person" and "the
        reviewer said no" are different outcomes that a ``None`` would have merged — and merging
        them is how an unattended session ends up parked on a question nobody will answer.
        """
        if self._call_policy(None).asks:
            return "ask", None
        decision = await self._review(gate)
        if decision.action != "allow":
            self._record_event("retry_refused", {
                "command": gate.command, "reason": decision.explanation,
            })
            return "refuse", None
        self._record_event("retry_allowed", {
            "command": gate.command, "reason": decision.explanation,
        })
        return "run", confinement.approved(
            by=confinement.APPROVED_BY_REVIEWER, purpose=gate.explanation, whole_disk=True,
        )
