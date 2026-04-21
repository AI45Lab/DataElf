import importlib
import logging
import os
from typing import Callable, Dict, List, Optional, Type

from .base import BaseChecker, CheckerType
from ..result import RiskType

_log = logging.getLogger(__name__)


class CheckerRegistry:
    """Central registry for discovering, registering and retrieving checkers."""

    _checkers: Dict[str, Type[BaseChecker]] = {}
    _loaded: bool = False

    @classmethod
    def register(
        cls,
        risk_type: RiskType,
        tags: Optional[List[str]] = None,
    ) -> Callable:
        """Decorator to register a checker.

        Args:
            risk_type: risk category for this checker.
            tags: grouping tags (e.g. ['default', 'strict']).

        Usage:
            @CheckerRegistry.register(RiskType.PII, tags=["default"])
            class PIIRule(RuleBasedChecker):
                ...
        """
        def decorator(checker_cls: Type[BaseChecker]) -> Type[BaseChecker]:
            checker_cls.name = checker_cls.__name__
            checker_cls.risk_type = risk_type
            checker_cls.tags = tags or []
            cls._checkers[checker_cls.__name__] = checker_cls
            return checker_cls
        return decorator

    @classmethod
    def get(cls, name: str) -> Type[BaseChecker]:
        cls._ensure_loaded()
        if name not in cls._checkers:
            raise KeyError(f"Checker '{name}' not found. Available: {list(cls._checkers)}")
        return cls._checkers[name]

    @classmethod
    def get_by_risk_type(cls, risk_type: RiskType) -> List[Type[BaseChecker]]:
        cls._ensure_loaded()
        return [c for c in cls._checkers.values() if c.risk_type == risk_type]

    @classmethod
    def get_by_checker_type(cls, checker_type: CheckerType) -> List[Type[BaseChecker]]:
        cls._ensure_loaded()
        return [c for c in cls._checkers.values() if c.checker_type == checker_type]

    @classmethod
    def get_by_tag(cls, tag: str) -> List[Type[BaseChecker]]:
        cls._ensure_loaded()
        return [c for c in cls._checkers.values() if tag in getattr(c, "tags", [])]

    @classmethod
    def list_all(cls) -> Dict[str, Type[BaseChecker]]:
        cls._ensure_loaded()
        return dict(cls._checkers)

    @classmethod
    def _ensure_loaded(cls) -> None:
        """Auto-scan and import all checker submodules to trigger @register decorators."""
        if cls._loaded:
            return
        base_dir = os.path.dirname(os.path.abspath(__file__))
        # derive package prefix dynamically from __name__
        package_prefix = __name__.rsplit(".", 1)[0]  # e.g. "tools.security_audit.checker"
        for subdir in ("rule_based", "llm_judge", "heuristic", "model_based"):
            dir_path = os.path.join(base_dir, subdir)
            if not os.path.isdir(dir_path):
                continue
            for fname in os.listdir(dir_path):
                if fname.endswith(".py") and fname != "__init__.py" and not fname.startswith("_"):
                    module = f"{package_prefix}.{subdir}.{fname[:-3]}"
                    try:
                        importlib.import_module(module)
                    except Exception as e:
                        _log.debug(f"Failed to load checker module {module}: {e}")
        cls._loaded = True
