# Repository FAQ

**Can I use LangChain/LangGraph/CrewAI/AutoGen/etc.?** No. The assignment is specifically about implementing the runtime yourself.

**Can I use dataclasses, typing, jsonschema, pytest, tenacity, rich, sqlite3 or httpx?** Yes. General-purpose support libraries are allowed.

**Can I use a different Groq model?** During development, yes if it supports the required local tool calling. Your final submitted configuration must still work with the instructor-specified default unless an announcement says otherwise.

**Can I call Groq built-in browser search/code execution or MCP?** No. Groq is the model provider only. All operational actions are local simulator tools.

**Can I inspect the simulator source?** You can read it to understand interfaces, but your agent may not access private/oracle state at runtime or encode seed/root-cause mappings.

**Do public tests cover everything?** No. Hidden tests intentionally mutate timing, failures, approvals, seeds and model behavior.
