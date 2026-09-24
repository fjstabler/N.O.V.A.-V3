"""Plugin architecture: skills expose tools, the registry discovers and runs them."""

from .base import Param, Skill, SkillInfo, ToolSpec, tool
from .registry import SkillRegistry

__all__ = ["Param", "Skill", "SkillInfo", "SkillRegistry", "ToolSpec", "tool"]
