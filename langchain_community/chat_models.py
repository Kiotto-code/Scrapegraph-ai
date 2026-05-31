# Local shim to provide ChatOllama for compatibility with this workspace.
class ChatOllama:
    """Minimal shim class used only for isinstance checks inside the local
    `scrapegraphai` package. This does not implement LLM behavior; real LLM
    usage is handled via the configured `llm` in the graph_config.
    """
    def __init__(self, *args, **kwargs):
        self.format = kwargs.get("format", None)


# Export name expected by imports
__all__ = ["ChatOllama"]
