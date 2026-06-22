import re
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel


class BashToolConfiguration(BaseModel):
    enabled: bool = True
    background_allowed: bool = True
    deny_commands: list[str] = []


class ReadToolConfiguration(BaseModel):
    enabled: bool = True
    maximum_file_size: int = 1_048_576


class EditToolConfiguration(BaseModel):
    enabled: bool = True


class SpawnAgentToolConfiguration(BaseModel):
    enabled: bool = True
    maximum_concurrency: int = 5


class ToolsConfiguration(BaseModel):
    bash: BashToolConfiguration = BashToolConfiguration()
    read: ReadToolConfiguration = ReadToolConfiguration()
    edit: EditToolConfiguration = EditToolConfiguration()
    spawn_agent: SpawnAgentToolConfiguration = SpawnAgentToolConfiguration()


class AgentConfiguration(BaseModel):
    name: str
    description: str = ""
    model: Optional[str] = None
    reasoning_effort: str = "high"
    maximum_iterations: int = 25
    recursion_limit: int = 3
    tools: ToolsConfiguration = ToolsConfiguration()
    tools_enabled: list[str] = []
    system_prompt: str = ""

    @classmethod
    def from_markdown(cls, path: str | Path) -> "AgentConfiguration":
        with open(path) as f:
            content = f.read()

        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", content, re.DOTALL)
        if not match:
            raise ValueError(f"No YAML frontmatter found in {path}")

        frontmatter = yaml.safe_load(match.group(1))
        body = match.group(2).strip()

        tools_data = frontmatter.pop("tools", {})
        tools_config = ToolsConfiguration(**{
            k: v for k, v in tools_data.items()
        }) if tools_data else ToolsConfiguration()

        return cls(
            **frontmatter,
            tools=tools_config,
            system_prompt=body,
        )


def load_agent_configuration(name: str, agents_directory: str | Path) -> AgentConfiguration:
    path = Path(agents_directory) / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Agent configuration not found: {path}")
    return AgentConfiguration.from_markdown(path)


def list_available_agents(agents_directory: str | Path) -> list[str]:
    return sorted(
        p.stem for p in Path(agents_directory).glob("*.md")
    )
