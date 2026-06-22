from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel


class ApiConfiguration(BaseModel):
    endpoint: str
    model: str
    api_key: str


class GlobalConfiguration(BaseModel):
    api: ApiConfiguration
    default_agent: str = "main"
    agents_directory: str = "agents"

    @classmethod
    def from_yaml(cls, path: str | Path) -> "GlobalConfiguration":
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)
