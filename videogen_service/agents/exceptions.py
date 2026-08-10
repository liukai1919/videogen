class AgentWorkerError(RuntimeError):
    """Base error of the external agent worker layer."""


class UnknownAgent(AgentWorkerError):
    """Asked for an agent name that was never registered."""


class AgentConfigError(AgentWorkerError):
    """config.yaml names an agent this build has no adapter for."""
