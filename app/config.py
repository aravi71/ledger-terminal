"""File-backed configuration.

Everything lives under ``<data_dir>/config`` as JSON. Missing files are
written with defaults on first read, so a clean checkout runs with no setup.
Prompts follow a default + override pattern: the tracked defaults live in
``defaults/`` and are copied once into ``<data_dir>/brain`` where the user
can edit them without touching version control.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS_DIR = ROOT / "defaults"


class GuardConfig(BaseModel):
    """Pre-trade rules checked at push time. ``None`` disables a rule."""

    maxOrderValue: float | None = 5_000
    maxPositionValue: float | None = 15_000
    cooldownSeconds: int = 0
    maxOrdersPerDay: int | None = 20
    symbolWhitelist: list[str] = Field(default_factory=list)
    allowShort: bool = False

    @field_validator("symbolWhitelist")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [s.strip().upper() for s in v if s.strip()]


class AccountConfig(BaseModel):
    id: str
    type: Literal["paper", "alpaca", "cfd"]
    label: str = ""
    enabled: bool = True
    guards: GuardConfig = Field(default_factory=GuardConfig)
    brokerConfig: dict[str, Any] = Field(default_factory=dict)


class AccountsFile(BaseModel):
    accounts: list[AccountConfig]


class AIConfig(BaseModel):
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    heartbeatEffort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    maxAgentSteps: int = Field(default=12, ge=1, le=40)


class HeartbeatConfig(BaseModel):
    enabled: bool = False
    intervalMinutes: int = Field(default=60, ge=5, le=24 * 60)


class WatchlistConfig(BaseModel):
    symbols: list[str]


class BotConfig(BaseModel):
    """An automated trader. ``strategy`` bots run a daily rule-based strategy;
    ``intraday`` bots scalp with stop-loss/take-profit on a CFD account;
    ``ai`` bots let the Claude agent stage and commit, then auto-push."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,40}$")
    type: Literal["strategy", "intraday", "ai"] = "strategy"
    timeframe: Literal["5m", "15m", "30m", "60m"] = "15m"
    riskPct: float = Field(default=1.0, gt=0, le=10)
    maxRiskPct: float = Field(default=5.0, gt=0, le=25)
    enabled: bool = False
    account: str = "paper-main"
    strategy: str = "sma_cross"
    params: dict[str, float] = Field(default_factory=dict)  # strategy parameters (numbers only)
    symbols: list[str] = Field(default_factory=list)
    allocationPerSymbol: float = Field(default=5000, gt=0)
    intervalMinutes: int = Field(default=15, ge=1, le=1440)
    maxDailyLossPct: float = Field(default=2.0, gt=0, le=50)
    respectMarketHours: bool = True

    @field_validator("symbols")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(s.strip().upper() for s in v if s.strip()))


class AutomationConfig(BaseModel):
    killSwitch: bool = False
    bots: list[BotConfig] = Field(default_factory=list)


DEFAULTS: dict[str, dict[str, Any]] = {
    "accounts.json": {
        "accounts": [
            {
                "id": "paper-main",
                "type": "paper",
                "label": "Paper · multi-asset",
                "enabled": True,
                "guards": {
                    "maxOrderValue": 5000,
                    "maxPositionValue": 15000,
                    "cooldownSeconds": 60,
                    "maxOrdersPerDay": 20,
                    "symbolWhitelist": [],
                    "allowShort": False,
                },
                "brokerConfig": {"startingCash": 100000, "slippageBps": 2, "commissionPerOrder": 0},
            },
            {
                "id": "paper-gold",
                "type": "cfd",
                "label": "Paper CFD · gold, $1,000, 1:100",
                "enabled": True,
                "guards": {
                    "maxOrderValue": 100000,
                    "maxPositionValue": 200000,
                    "cooldownSeconds": 0,
                    "maxOrdersPerDay": 60,
                    "symbolWhitelist": ["GC=F"],
                    "allowShort": True,
                },
                "brokerConfig": {"startingCash": 1000, "leverage": 100, "spread": 0.30, "commissionPerUnitSide": 0.035, "minQty": 1},
            },
            {
                "id": "alpaca-paper",
                "type": "alpaca",
                "label": "Alpaca paper · US equities",
                "enabled": False,
                "guards": {
                    "maxOrderValue": 1000,
                    "maxPositionValue": 3000,
                    "cooldownSeconds": 300,
                    "maxOrdersPerDay": 10,
                    "symbolWhitelist": ["AAPL", "MSFT", "SPY"],
                    "allowShort": False,
                },
                "brokerConfig": {"keyIdEnv": "ALPACA_API_KEY_ID", "secretKeyEnv": "ALPACA_API_SECRET_KEY"},
            },
        ]
    },
    "ai.json": AIConfig().model_dump(),
    "heartbeat.json": HeartbeatConfig().model_dump(),
    "watchlist.json": {"symbols": ["SPY", "AAPL", "MSFT", "NVDA", "BTC-USD", "GC=F", "EURUSD=X"]},
    "automation.json": {
        "killSwitch": False,
        "bots": [
            {"id": "spy-trend", "type": "strategy", "enabled": False, "account": "paper-main", "strategy": "sma_cross",
             "params": {"fast": 50, "slow": 200}, "symbols": ["SPY"], "allocationPerSymbol": 5000,
             "intervalMinutes": 15, "maxDailyLossPct": 2.0, "respectMarketHours": True},
            {"id": "btc-breakout", "type": "strategy", "enabled": False, "account": "paper-main", "strategy": "breakout",
             "params": {"entry_lookback": 20, "exit_lookback": 10}, "symbols": ["BTC-USD"], "allocationPerSymbol": 3000,
             "intervalMinutes": 15, "maxDailyLossPct": 3.0, "respectMarketHours": True},
            {"id": "ai-autopilot", "type": "ai", "enabled": False, "account": "paper-main", "strategy": "",
             "symbols": ["SPY", "AAPL", "MSFT", "NVDA"], "allocationPerSymbol": 2000,
             "intervalMinutes": 120, "maxDailyLossPct": 2.0, "respectMarketHours": True},
        ],
    },
}

SCHEMAS: dict[str, type[BaseModel]] = {
    "accounts.json": AccountsFile,
    "ai.json": AIConfig,
    "heartbeat.json": HeartbeatConfig,
    "watchlist.json": WatchlistConfig,
    "automation.json": AutomationConfig,
}


class Config:
    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(data_dir or os.getenv("LEDGER_DATA_DIR") or ROOT / "data").resolve()
        self.config_dir = self.data_dir / "config"
        self.brain_dir = self.data_dir / "brain"
        for d in (self.config_dir, self.brain_dir, self.data_dir / "utas", self.data_dir / "brokers"):
            d.mkdir(parents=True, exist_ok=True)
        self._ensure_prompts()

    # --- JSON config -------------------------------------------------------
    def load(self, name: str) -> BaseModel:
        path = self.config_dir / name
        if not path.exists():
            self._write(path, copy.deepcopy(DEFAULTS[name]))
        raw = json.loads(path.read_text(encoding="utf-8"))
        return SCHEMAS[name].model_validate(raw)

    def save(self, name: str, model: BaseModel) -> None:
        SCHEMAS[name].model_validate(model.model_dump())
        self._write(self.config_dir / name, model.model_dump())

    @staticmethod
    def _write(path: Path, data: Any) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)

    @property
    def accounts(self) -> list[AccountConfig]:
        return self.load("accounts.json").accounts  # type: ignore[attr-defined]

    @property
    def ai(self) -> AIConfig:
        return self.load("ai.json")  # type: ignore[return-value]

    @property
    def heartbeat(self) -> HeartbeatConfig:
        return self.load("heartbeat.json")  # type: ignore[return-value]

    @property
    def automation(self) -> AutomationConfig:
        return self.load("automation.json")  # type: ignore[return-value]

    @property
    def watchlist(self) -> list[str]:
        return self.load("watchlist.json").symbols  # type: ignore[attr-defined]

    # --- prompts -----------------------------------------------------------
    def _ensure_prompts(self) -> None:
        for name in ("persona", "heartbeat", "autopilot"):
            target = self.brain_dir / f"{name}.md"
            if not target.exists():
                shutil.copyfile(DEFAULTS_DIR / f"{name}.default.md", target)

    def prompt(self, name: str) -> str:
        return (self.brain_dir / f"{name}.md").read_text(encoding="utf-8")


def market_data_mode() -> str:
    mode = os.getenv("LEDGER_MARKET_DATA", "yahoo").strip().lower()
    return mode if mode in ("yahoo", "demo") else "yahoo"
