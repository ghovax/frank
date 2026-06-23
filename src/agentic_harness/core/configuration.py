import os
import re
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel


class ApiConfiguration(BaseModel):
    endpoint: str
    model: str
    api_key: str


class ExaConfiguration(BaseModel):
    api_key: str = ""

    @property
    def effective_api_key(self) -> str:
        return os.environ.get("EXA_API_KEY") or self.api_key


class GlobalConfiguration(BaseModel):
    api: ApiConfiguration
    exa: ExaConfiguration = ExaConfiguration()
    default_agent: str = "main"
    agents_directory: str = "agents"
    maximum_history_age_days: int = 30

    @classmethod
    def from_yaml(cls, path: str | Path) -> "GlobalConfiguration":
        with open(path) as file_handle:
            data = yaml.safe_load(file_handle)
        return cls(**data)


class BashToolConfiguration(BaseModel):
    enabled: bool = True
    background_allowed: bool = True
    permissions: dict[str, str] = {}

    def evaluate_permission(self, command: str) -> str:
        best_match_length = 0
        best_decision = "allow"
        for pattern, decision in self.permissions.items():
            if self._pattern_matches(command, pattern):
                if not best_match_length or len(pattern) > best_match_length:
                    best_match_length = len(pattern)
                    best_decision = decision.lower()
        return best_decision

    @staticmethod
    def _pattern_matches(command: str, pattern: str) -> bool:
        if pattern.endswith("*"):
            return command.startswith(pattern[:-1].rstrip())
        return command == pattern


class SpawnAgentToolConfiguration(BaseModel):
    enabled: bool = True
    maximum_concurrency: int = 5


class ToolsConfiguration(BaseModel):
    bash: BashToolConfiguration = BashToolConfiguration()
    spawn_agent: SpawnAgentToolConfiguration = SpawnAgentToolConfiguration()


class AgentConfiguration(BaseModel):
    name: str
    label: str = ""
    color: str = ""
    description: str = ""
    model: Optional[str] = None
    reasoning_effort: str = "high"
    maximum_iterations: int = 25
    recursion_limit: int = 3
    tools: ToolsConfiguration = ToolsConfiguration()
    tools_enabled: list[str] = []
    system_prompt: str = ""
    stream_agent_progress: bool = False

    @classmethod
    def from_markdown(cls, path: str | Path) -> "AgentConfiguration":
        with open(path) as file_handle:
            content = file_handle.read()

        frontmatter_match = re.match(
            r"^---\s*\n(.*?)\n---\s*\n(.*)", content, re.DOTALL
        )
        if not frontmatter_match:
            raise ValueError(f"No YAML frontmatter found in {path}")

        frontmatter = yaml.safe_load(frontmatter_match.group(1))
        markdown_body = frontmatter_match.group(2).strip()

        tools_data = frontmatter.pop("tools", {})
        tools_configuration = (
            ToolsConfiguration(**{name: value for name, value in tools_data.items()})
            if tools_data
            else ToolsConfiguration()
        )

        return cls(
            **frontmatter,
            tools=tools_configuration,
            system_prompt=markdown_body,
        )


class PermissionEvaluator:
    def __init__(self, agent_configuration: AgentConfiguration):
        self._configuration = agent_configuration

    def check_tool_enabled(self, tool_name: str) -> None:
        if tool_name not in ("spawn_agent", "orchestrate"):
            return
        if (
            self._configuration.tools_enabled
            and tool_name not in self._configuration.tools_enabled
        ):
            raise PermissionError(
                f"Tool '{tool_name}' is not enabled for agent '{self._configuration.name}'"
            )

    def evaluate_bash_permission(self, command: str) -> str:
        return self._configuration.tools.bash.evaluate_permission(command)

    def check_bash_background(self) -> None:
        if not self._configuration.tools.bash.background_allowed:
            raise PermissionError("Background bash execution is not allowed")

    def check_spawn_agent(self, current_depth: int, current_concurrent: int) -> None:
        if current_depth >= self._configuration.recursion_limit:
            raise PermissionError(
                f"Recursion limit ({self._configuration.recursion_limit}) reached"
            )
        maximum = self._configuration.tools.spawn_agent.maximum_concurrency
        if current_concurrent >= maximum:
            raise PermissionError(
                f"Maximum concurrent sub-agents ({maximum}) reached"
            )

    def check_tool(self, tool_name: str, **arguments) -> None:
        self.check_tool_enabled(tool_name)


class PermissionError(RuntimeError):
    pass


class PromptLoader:
    def __init__(self, prompts_directory: str | Path, extension: str = "md"):
        self._directory = Path(prompts_directory)
        self._extension = extension

    def load(self, template_name: str, variables: dict[str, str]) -> str:
        path = self._directory / f"{template_name}.{self._extension}"
        if not path.exists():
            return ""
        content = path.read_text()
        return self._replace_variables(content, variables)

    @staticmethod
    def _replace_variables(template: str, variables: dict[str, str]) -> str:
        def replacer(match: re.Match) -> str:
            variable_name = match.group(1)
            return variables.get(variable_name, match.group(0))

        return re.sub(r"\{\{\s*(\w+)\s*\}\}", replacer, template)


def load_agent_configuration(
    name: str, agents_directory: str | Path
) -> AgentConfiguration:
    path = Path(agents_directory) / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Agent configuration not found: {path}")
    return AgentConfiguration.from_markdown(path)


def list_available_agents(agents_directory: str | Path) -> list[str]:
    return sorted(p.stem for p in Path(agents_directory).glob("*.md"))
