from agentic_harness.core.agent_configuration import AgentConfiguration


class PermissionError(RuntimeError):
    pass


class PermissionEvaluator:
    def __init__(self, agent_config: AgentConfiguration):
        self._config = agent_config

    def check_tool_enabled(self, tool_name: str) -> None:
        if self._config.tools_enabled and tool_name not in self._config.tools_enabled:
            raise PermissionError(
                f"Tool '{tool_name}' is not enabled for agent '{self._config.name}'"
            )

    def check_bash(self, command: str) -> None:
        denied = self._config.tools.bash.deny_commands
        if not denied:
            return
        for token in denied:
            if token in command:
                raise PermissionError(
                    f"Command contains denied token '{token}': {command[:120]}"
                )

    def check_bash_background(self) -> None:
        if not self._config.tools.bash.background_allowed:
            raise PermissionError("Background bash execution is not allowed")

    def check_read(self, file_size: int) -> None:
        maximum = self._config.tools.read.maximum_file_size
        if file_size > maximum:
            raise PermissionError(
                f"File size {file_size} exceeds maximum {maximum}"
            )

    def check_spawn_agent(self, current_recursion_depth: int, current_concurrent: int) -> None:
        if current_recursion_depth >= self._config.recursion_limit:
            raise PermissionError(
                f"Recursion limit ({self._config.recursion_limit}) reached"
            )
        maximum = self._config.tools.spawn_agent.maximum_concurrency
        if current_concurrent >= maximum:
            raise PermissionError(
                f"Maximum concurrent sub-agents ({maximum}) reached"
            )

    def check_tool(self, tool_name: str, **kwargs) -> None:
        self.check_tool_enabled(tool_name)
        if tool_name == "bash":
            self.check_bash(kwargs.get("command", ""))
            if kwargs.get("background"):
                self.check_bash_background()
        elif tool_name == "read":
            file_size = kwargs.get("file_size", 0)
            self.check_read(file_size)
