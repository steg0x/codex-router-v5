"""Router package with lazy exports so lightweight telemetry has no routing startup cost."""

_CORE_EXPORTS = ("StrEnum", "RuleStatus", "Scope", "Level", "Verification", "Mutation", "Volume",
                 "Dependencies", "Phase", "HistorySupport", "Profile", "Role", "Family", "Effort",
                 "Failure", "DecisionKind", "Route", "AUTO_EFFORTS", "AUTO_ROUTES", "Lease",
                 "TaskCapsule", "MODEL_VERSION_PATTERN", "is_supported_model_version", "CAPSULE_AXES",
                 "canonicalize_task_capsule", "build_task_capsule", "Decision", "route_v4_1", "route_v5")
__all__ = list(_CORE_EXPORTS) + ["HistoricalRoute", "OutcomeRegistry", "OutcomeRecord", "Arena", "evaluate_promotion"]


def __getattr__(name):
    from importlib import import_module
    if name in _CORE_EXPORTS:
        module = import_module(".core", __name__)
    elif name in ("HistoricalRoute", "OutcomeRegistry", "OutcomeRecord"):
        module = import_module(".registry", __name__)
    elif name in ("Arena", "evaluate_promotion"):
        module = import_module(".arena", __name__)
    else:
        raise AttributeError(name)
    value = getattr(module, name)
    globals()[name] = value
    return value
