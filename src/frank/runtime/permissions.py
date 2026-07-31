"""The AgentRuntime permission concern (a mixin composed into AgentRuntime).

Classifying a tool call's permission, the bash "always allow" rule lifecycle (distill, remember,
persist), preflighting a whole batch, resolving per-call decisions, and minting request ids."""
from __future__ import annotations

from frank.runtime.internals import (
    _coerce_structured_arguments,
    _PreflightGate,
    _ResolvedToolDecision,
    _ToolPlan,
)
from frank.runtime.locations import (
    _LOCATION_TOOLS,
    PermissionDecision,
    ResolvedLocation,
    ToolLocationError,
)
from langchain_core.messages import HumanMessage, SystemMessage
from typing import Any, Optional, Sequence
import ast
import sys
import logging
import uuid
from pathlib import Path
from frank.base.serialization import compact

logger = logging.getLogger(__name__)

# The state-changing control_screen primitives. A script that calls any of them is mutating; one that
# only reads (find_one/find_many/read/hover/scroll/tabs/tab/frames) is read-only. This is the
# structural analogue of the bash read-only assessment — a scan of the primitive names the script
# calls, not a regex.
#
# `evaluate` belongs here and was missing, which meant a script whose only act was running arbitrary
# JavaScript inside the user's signed-in page classified read-only: it passed a read-only policy and,
# at `risk: low`, raised no gate at all. `press` is here for the reason the tool's own description
# gives — `press("Enter")` posts a form. `navigate` is here because on a great many sites a URL is a
# command rather than an address (`/logout`, `/unsubscribe?token=…`, `/items/12/delete`), and nothing
# reading primitive names can tell those from a page worth reading.
#
# `caret` and `select` are here because they write. Both call `set_selected_range` on a live
# element — they move somebody's insertion point, or select a range inside a field they are
# editing — and every other description of the harness already said so: the tool's own
# documentation lists them among "the primitives that alter something", and the dispatcher
# brackets them with before-and-after glances exactly as it does a click. This set was the only
# place that disagreed, so a read-only session could reach into a document and change the
# selection, which is both a write and the setup for the user's next keystroke replacing it.
#
# Switching tabs and listing tabs or frames are reads, and stay out: moving attention to a tab the
# user already had open changes nothing about it.
MUTATING_SCREEN_PRIMITIVES = frozenset({
    "click", "type", "choose", "upload", "drag",
    "evaluate", "press", "navigate",
    "new_tab", "close_tab",
    "caret", "select",
})


# There is no import allowlist, and that is a decision rather than an omission.
#
# There was one: twenty modules of pure computation, and anything else made a script `unknown`.
# It was written when this scan was the only thing standing between a screen script and the
# machine, and two things have since made it false. The child a script runs in is confined by the
# operating system to a scratch directory of its own and no network at all, so `import os` reaches
# nothing — `frank.computer.control` narrows the session's own profile before spawning it. And the
# primitives are refused at the surface rather than read out of the source, so no spelling of a
# call gets past a read-only policy.
#
# What the list did instead was make the tool poorer at the thing it is for. A skill's script
# package, the `frank.screen` import in this project's own documented example, and `import csv`
# were all "unknown", which under a read-only policy is a refusal — and the refusal said the
# script *changed state*, which importing `csv` does not. Meanwhile `python3 whatever.py` through
# the bash tool was waved through on the model's own say-so. A restriction that strict, sitting
# beside a door that open, was not buying safety; it was buying friction.
#
# So a script may import what it needs. What it may *do* is answered by the two things that can
# actually answer it: the confinement, and the primitive check at the bridge.


def _screen_primitive(func: ast.expr) -> str:
    """The primitive a call node names, however the script spells it.

    ``screen.click(...)`` and a bare ``click(...)`` are the same act, and this scan only ever saw
    the second. The calling form is a real Python object now, so a walk that matched on
    ``ast.Name`` alone would read a script that clicks as read-only — the whole assessment
    silently inverted by a change of syntax."""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _module_source(module: str, import_roots: Sequence[str]) -> str:
    """The source of an imported module, if it is one of ours to read.

    Only the directories a script's own imports are served from: a person's workflows and the
    script packages their skills carry. Never site-packages — following `httpx` into its own
    imports would parse half the environment to answer a question about one screen script, and
    the answer would be "unknown" long before it finished.
    """
    parts = module.split(".")
    for root in import_roots:
        base = Path(root)
        candidates = [base.joinpath(*parts).with_suffix(".py"), base.joinpath(*parts, "__init__.py")]
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate.read_text()
            except OSError:
                continue
    return ""


def _control_script_assessment(script: str, import_roots: Sequence[str] = ()) -> tuple[str, str]:
    """Classify a control_screen script as ``read_only``, ``mutating``, or ``unknown``.

    The question is what the script will do *to the screen*, and nothing else. It used to be a
    much broader question — whether the script stayed inside a curated set of modules — because
    this scan was once the only thing between a screen script and the machine. It is not any more.
    The child is confined by the operating system to a scratch directory and no network, and a
    primitive the session may not use is refused at the surface however the script spelled it. So
    this no longer has to be right about arbitrary Python in order for the harness to be safe,
    and it stops trying: a script may import whatever it needs.

    What is left is worth doing well, because it is what decides whether the user is asked. A
    script is ``mutating`` when it calls a primitive that changes something, ``read_only`` when it
    only looks, and ``unknown`` when it reaches for something whose effect cannot be read here.

    Imports are followed rather than surrendered to. A script whose whole body is
    ``from filing import file_all`` says nothing on its face, and answering ``unknown`` meant that
    the more work somebody moved into a reusable module — the thing this tool wants them to do —
    the less could be said about it, and the more often they were asked. Where the module is one
    of ours, it is read and folded in, so a skill's package is classified by what it actually
    calls; where it is not, the verdict is ``unknown`` and the person decides once.

    This is a gate, not a boundary. It reasons about source, and source can be obscured. The
    boundary is the confinement and the primitive check, neither of which reads anything."""
    seen: set[str] = set()

    def assess(source: str, depth: int) -> tuple[str, str]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return "unknown", "the script could not be parsed"
        mutating_detail = ""
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if depth <= 0:
                    continue
                if isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    modules = [alias.name for alias in node.names]
                for module in modules:
                    root = module.split(".")[0]
                    # `frank.screen` is how a script reaches the screen at all, and reading it
                    # would say nothing: the primitives are forwarded by `__getattr__`, so there
                    # is no call in that file to find. What a script does with it is visible in
                    # the script.
                    if not module or root == "frank":
                        continue
                    # The standard library cannot reach the screen — there is no bridge in it —
                    # so what it computes is none of this scan's business.
                    if root in sys.stdlib_module_names:
                        continue
                    if module in seen:
                        continue
                    seen.add(module)
                    source_text = _module_source(module, import_roots)
                    if not source_text:
                        # Not ours to read: a third-party package, or a name that does not
                        # resolve at all. Either way its screen calls are invisible here, and a
                        # module holding a `click` would otherwise make the script that imports
                        # it look read-only. `unknown` escalates rather than refuses, so this
                        # costs one question and not the capability.
                        return "unknown", f"imports {module}, which cannot be read from here"
                    verdict, detail = assess(source_text, depth - 1)
                    if verdict == "unknown":
                        return "unknown", detail or f"imports {module}"
                    if verdict == "mutating" and not mutating_detail:
                        mutating_detail = f"{detail} in {module}" if detail else f"calls into {module}"
            elif isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr.endswith("__"):
                # `().__class__.__subclasses__()` and the rest of the walk back up to the
                # interpreter. Kept not because it is dangerous now — the confinement settles
                # that — but because it is never what a screen script is honestly doing, and
                # asking about it costs one prompt.
                return "unknown", f"reaches for {node.attr}"
            elif isinstance(node, ast.Call) and _screen_primitive(node.func) in MUTATING_SCREEN_PRIMITIVES:
                # Recorded rather than returned: a later node may still prove the script
                # `unknown`, which is the stricter verdict and must win however the walk happens
                # to be ordered.
                mutating_detail = mutating_detail or f"calls {_screen_primitive(node.func)}()"
        return ("mutating", mutating_detail) if mutating_detail else ("read_only", "")

    # Two levels: the script, and the module it imports. Deeper than that is a module importing a
    # module, where naming the offending call is no longer information a person can act on.
    return assess(script, depth=2)


class _DecidesPermissions:
    """Whether a call is allowed, asked about, or refused.

    Composed into :class:`AgentRuntime` beside the dispatcher it answers for."""

    def _evaluate_bash_permission(self, command: str) -> str:
        unmatched = "ask" if self._interactive_manual_mode else "allow"
        return self._permissions.evaluate_bash_permission(command, unmatched=unmatched)

    def _script_import_roots(self) -> list[str]:
        """Where a screen script's own imports are served from.

        The same list the script will actually be run with, so that what the classifier can read
        and what the script can import are the same set by construction. Two lists would drift,
        and the drift would show up as a script that ran fine being asked about every time."""
        from frank.computer import workflows as workflow_registry

        try:
            return workflow_registry.import_roots(self._project_directory or "")
        except Exception:  # noqa: BLE001 — a classifier must not fail on a missing directory
            return []

    async def _classify_permission(
        self,
        *,
        tool_kind: str,
        command: str,
        raw_command: str,
        default_decision: str,
        read_only: bool,
        risk: str,
        explanation: str,
        static_classification: str = "",
        static_detail: str = "",
        outside_reads: Optional[list[str]] = None,
    ) -> PermissionDecision:
        context = compact(
            {
                "tool_kind": tool_kind,
                "working_directory": self._working_directory,
                "command": command,
                "raw_command": raw_command,
                "default_permission_decision": default_decision,
                "model_declared_read_only": read_only,
                "model_declared_risk": risk,
                "model_explanation": explanation,
                "static_read_only_classification": static_classification,
                "static_detail": static_detail,
                "outside_working_directory_reads": outside_reads or [],
                "allowed_actions": ["auto_approve", "escalate"],
            },
        )
        prompt = self._prompt_loader.load("permission_classifier", {})
        try:
            model = self._llm.bind_tools([PermissionDecision], tool_choice="auto")
            # Instructions and subject as separate messages. Folding the call being judged into
            # the system prompt left the request with no input at all, which some providers
            # reject outright — the ChatGPT Codex endpoint answers `400: One of "input" … must be
            # provided`. On those the classifier never ran: every ambiguous call fell to the
            # exception path below and escalated, so the permission system quietly degraded to
            # "ask about everything" and reported an API error as its reason for asking.
            response = await model.ainvoke([
                SystemMessage(content=prompt),
                HumanMessage(content=context),
            ])
            if not response.tool_calls:
                return PermissionDecision(action="escalate", explanation="Classifier returned no structured decision.", risk="medium")
            decision = PermissionDecision.model_validate(response.tool_calls[0]["args"])
            if default_decision == "deny" and decision.action == "auto_approve":
                return PermissionDecision(action="escalate", explanation="User-configured permissions deny this action.", risk="high")
            if not decision.explanation.strip():
                return PermissionDecision(action="escalate", explanation="Classifier did not provide a explanation.", risk="medium")
            return decision
        except Exception:
            # Fail closed — a classifier that cannot answer must not approve anything. But its
            # failure is not a reason, and this used to hand the raw exception to the person as
            # the explanation for the gate, so they were shown a provider's 400 where they
            # expected to read why their approval was needed. The detail goes to the log, where
            # somebody can act on it.
            logger.warning("The permission classifier could not decide; escalating", exc_info=True)
            return PermissionDecision(
                action="escalate",
                explanation="The safety check could not run, so this needs your decision.",
                risk="medium",
            )

    def _needs_a_second_opinion(self, rule: str, model_risk: str) -> bool:
        """The barrier. Does this call need the classifier, or is the answer already known?

        Two cheap facts decide it, and neither costs a model call:

        - **The rule** the user configured for this command: allow, ask, or deny.
        - **The risk the model itself declared** when it made the call.

        A call the rules allow, which the model judged low-risk, runs. A call the rules deny
        never gets here — a denial is not a question. Everything between those is the
        ambiguous middle, and only that middle is worth a model call.

        This is the whole shape of the permission system: a static barrier in front, and a
        classifier behind it that sees only what the barrier could not settle.
        """
        return rule == "ask" or model_risk in ("medium", "high")

    def _new_permission_request_id(self, tool_call_id: str = "") -> str:
        """The id a person's answer is filed under. Derived from the call, not minted fresh.

        Preflight runs again when a suspended turn resumes — the batch has not executed, so it
        is planned again — and a random id meant the resumed plan asked under a *new* name while
        the answer sat under the old one. Nothing matched, so the turn re-gated and ended without
        running the tool, and the person who had just clicked Allow was told their request was no
        longer active. Deriving it from the tool call makes the second ask the same ask.
        """
        return f"perm-{self._session_id}-{tool_call_id or uuid.uuid4()}"

    def _new_question_request_id(self, tool_call_id: str = "") -> str:
        """Stable for the same reason, and by the same means, as the permission id above."""
        return f"q-{self._session_id}-{tool_call_id or uuid.uuid4()}"

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
            # Stamp the call's identity onto every gate it raised, here rather than at each
            # of the ten construction sites. A gate is shown to a person before the tool call
            # it belongs to has been announced, so the gate is the only thing that can tell
            # them what is being asked for: which tool, and the arguments the model chose —
            # including its own `explanation` of why it wants the call.
            for gate in plan.gates:
                gate.tool_name = tool_call_data["name"]
                gate.arguments = dict(tool_call_data["args"] or {})
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
            explanation = tool_arguments.get("explanation", "")
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
                # A sandbox read escalates to the user like any other gate — the turn parks
                # in place and resumes on the answer — rather than hard-denying. A session
                # created by another parks the same way: its request reaches a person through
                # `frank approve` or the app, because there is nobody else to answer it.
                permission_decision = self._evaluate_bash_permission(raw_command)
                # Behind the same barrier as every other call. A read outside the working
                # directory used to reach the classifier on every occurrence, whatever the
                # rules said and whatever risk the model had declared — the one path that
                # skipped the barrier, and the one that fires most often.
                if (
                    policy.auto_permissions
                    and permission_decision != "deny"
                    and self._needs_a_second_opinion(permission_decision, risk or "medium")
                ):
                    decision = await self._classify_permission(
                        tool_kind="bash", command=raw_command, raw_command=raw_command,
                        default_decision=permission_decision, read_only=read_only,
                        risk=risk or "medium", explanation=explanation or sandbox_message,
                        static_classification=static_classification, static_detail=static_detail,
                        outside_reads=outside_reads,
                    )
                    if decision.action == "auto_approve":
                        self._record_event("bash_auto_approved", {"command": raw_command, "reason": decision.explanation, "risk": decision.risk})
                    else:
                        plan.gates.append(_PreflightGate(
                            request_id=self._new_permission_request_id(tool_call_identifier), tool_call_id=tool_call_identifier,
                            kind="permission", command=raw_command,
                            explanation=decision.explanation or sandbox_message, risk=decision.risk, is_bash=True,
                            deny_message="Sandbox read was not approved by the user.",
                        ))
                else:
                    if permission_decision == "deny":
                        plan.denial = {"code": "", "message": "Sandbox read denied by the default permission policy.", "denied_injection": False, "raw_command": raw_command}
                        return plan
                    plan.gates.append(_PreflightGate(
                        request_id=self._new_permission_request_id(tool_call_identifier), tool_call_id=tool_call_identifier,
                        kind="permission", command=raw_command, explanation=sandbox_message, risk="medium", is_bash=True,
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
            elif self._needs_a_second_opinion(permission_decision, risk):
                if policy.auto_permissions:
                    decision = await self._classify_permission(
                        tool_kind="bash", command=raw_command, raw_command=raw_command,
                        default_decision=permission_decision, read_only=read_only,
                        risk=risk or "medium", explanation=explanation,
                        static_classification=static_classification, static_detail=static_detail,
                        outside_reads=outside_reads,
                    )
                    if decision.action == "auto_approve":
                        self._record_event("bash_auto_approved", {"command": raw_command, "reason": decision.explanation, "risk": decision.risk})
                    else:
                        plan.gates.append(_PreflightGate(
                            request_id=self._new_permission_request_id(tool_call_identifier), tool_call_id=tool_call_identifier,
                            kind="permission", command=raw_command,
                            explanation=decision.explanation or explanation, risk=decision.risk, is_bash=True,
                            deny_message="Command was not approved by the user.",
                        ))
                else:
                    plan.gates.append(_PreflightGate(
                        request_id=self._new_permission_request_id(tool_call_identifier), tool_call_id=tool_call_identifier,
                        kind="permission", command=raw_command, explanation=explanation, risk=risk, is_bash=True,
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
                explanation = tool_arguments.get("explanation", "")
                if policy.auto_permissions:
                    decision = await self._classify_permission(
                        tool_kind="mcp", command=action,
                        raw_command=compact(tool_arguments.get("arguments") or {}),
                        default_decision="ask", read_only=False, risk=risk, explanation=explanation,
                    )
                    if decision.action == "auto_approve":
                        self._record_event("mcp_auto_approved", {
                            "server": tool_arguments.get("server", ""), "tool": tool_arguments.get("tool_name", ""),
                            "reason": decision.explanation, "risk": decision.risk,
                        })
                    else:
                        plan.gates.append(_PreflightGate(
                            request_id=self._new_permission_request_id(tool_call_identifier), tool_call_id=tool_call_identifier,
                            kind="permission", command=action,
                            explanation=decision.explanation or explanation, risk=decision.risk,
                            deny_message="MCP tool call not approved by user",
                        ))
                else:
                    plan.gates.append(_PreflightGate(
                        request_id=self._new_permission_request_id(tool_call_identifier), tool_call_id=tool_call_identifier,
                        kind="permission", command=action, explanation=explanation, risk=risk,
                        deny_message="MCP tool call not approved by user",
                    ))
            return plan

        if tool_name == "ask_user":
            plan.gates.append(_PreflightGate(
                request_id=self._new_question_request_id(tool_call_identifier), tool_call_id=tool_call_identifier,
                kind="question", questions=tool_arguments.get("questions", []) or [],
            ))
            return plan

        if tool_name == "control_screen":
            script = tool_arguments.get("script", "") or ""
            explanation = tool_arguments.get("explanation", "")
            risk = tool_arguments.get("risk", "") or ""
            static_classification, static_detail = _control_script_assessment(
                script, import_roots=self._script_import_roots(),
            )
            # Read-only enforcement is a hard block (no human in the loop): a mutating script cannot
            # run under a read-only policy; an unparseable one is treated as modifying, not waved through.
            if policy.read_only and static_classification in ("mutating", "unknown"):
                # Two verdicts, two files, because they are different facts and are acted on
                # differently. `unknown` used to borrow the mutating sentence, so a script that
                # imported `csv` was told it *changed state* — which it did not — and advised not
                # to write files, which it had not tried to do. A model reading that concludes its
                # screen work was the problem and rewrites the half that was fine.
                violation = self._prompt_loader.load(
                    "read_only_screen_mutating" if static_classification == "mutating"
                    else "read_only_screen_unknown",
                    {"detail": f" ({static_detail})" if static_detail else ""},
                )
                deny_message = self._prompt_loader.load("read_only_denied", {"violation": violation})
                plan.denial = {"code": "", "message": deny_message, "denied_injection": False, "raw_command": ""}
                return plan
            if (static_classification != "read_only" or risk in ("medium", "high")):
                if policy.auto_permissions:
                    decision = await self._classify_permission(
                        tool_kind="screen", command="control_screen", raw_command=script,
                        default_decision="ask", read_only=(static_classification == "read_only"),
                        risk=risk or "medium", explanation=explanation,
                        static_classification=static_classification, static_detail=static_detail,
                    )
                    if decision.action == "auto_approve":
                        self._record_event("screen_auto_approved", {"reason": decision.explanation, "risk": decision.risk})
                    else:
                        plan.gates.append(_PreflightGate(
                            request_id=self._new_permission_request_id(tool_call_identifier), tool_call_id=tool_call_identifier,
                            kind="permission", command="control_screen",
                            explanation=decision.explanation or explanation, risk=decision.risk,
                            deny_message="Screen action not approved by user",
                        ))
                else:
                    plan.gates.append(_PreflightGate(
                        request_id=self._new_permission_request_id(tool_call_identifier), tool_call_id=tool_call_identifier,
                        kind="permission", command="control_screen", explanation=explanation, risk=risk or "medium",
                        deny_message="Screen action not approved by user",
                    ))
            return plan

        if tool_name in self._extra_tools:
            # A tool the caller supplied. The engine classifies by tool *name* and has never
            # heard of this one, so there is no honest way to infer what it does — and the safe
            # direction is the one where adding a tool cannot silently widen what a session may
            # do. It is gated at the risk the caller stated (`tool_risk`, "medium" unless said
            # otherwise), and `tool_risk="none"` is how a caller says a tool needs no gate at
            # all, which is a deliberate sentence rather than a default.
            if self._tool_risk == "none" or not self._permission_mode.is_interactive:
                return plan
            plan.gates.append(_PreflightGate(
                request_id=self._new_permission_request_id(tool_call_identifier),
                tool_call_id=tool_call_identifier,
                kind="permission",
                command=tool_name,
                explanation=f"{tool_name} was supplied by the program embedding this session.",
                risk=self._tool_risk,
                deny_message=f"{tool_name} was not approved by the user",
            ))
            return plan

        return plan
