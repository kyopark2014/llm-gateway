# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Local JSON state for idempotent installs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class State:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def update(self, mapping: dict[str, Any]) -> None:
        self.data.update(mapping)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def endpoints(self) -> dict[str, str]:
        return {
            "gateway_alb": self.get("gateway_alb_dns", ""),
            "admin_ui_alb": self.get("admin_ui_alb_dns", ""),
            "admin_api_alb": self.get("admin_api_alb_dns", ""),
            "admin_api_gateway": self.get("api_gateway_endpoint", ""),
            "cluster": self.get("cluster_name", ""),
        }
