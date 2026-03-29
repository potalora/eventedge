from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class OrderResult:
    order_id: str
    status: str  # "filled", "rejected", "cancelled", "pending"
    filled_qty: float = 0
    filled_price: float = 0.0
    message: str = ""


@dataclass
class AccountInfo:
    cash: float
    portfolio_value: float
    buying_power: float


class BaseBroker(ABC):
    @abstractmethod
    def submit_stock_order(self, symbol: str, side: str, qty: int,
                           order_type: str = "market", **kwargs) -> OrderResult:
        pass

    @abstractmethod
    def submit_options_order(self, symbol: str, expiry: str, strike: float,
                             right: str, side: str, qty: int,
                             **kwargs) -> OrderResult:
        pass

    @abstractmethod
    def get_positions(self) -> List[Dict[str, Any]]:
        pass

    @abstractmethod
    def get_account(self) -> AccountInfo:
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        pass
