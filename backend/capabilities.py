"""ONE definition per integration - the only place you edit to add one.

Before this, adding an integration meant editing five places across two
languages: setup.SERVICES (env + copy), setup._validate (the live check),
integrations.SETUP_CAPABILITIES (kind + display name),
integrations.CAPABILITY_WIRING (chat toggle + any-of group), and the frontend's
SECTIONS. Miss one and the capability half-exists. Now it's one entry here.

The probe is DATA, not code: `_validate` was already a table of
(method, url, headers, body, params) with one generic runner, so nothing is
lost by moving it here - and a capability can never carry executable behaviour,
which is what keeps this safe to extend. Anything that needs to *do* things is
an MCP server, not a capability (ARCHITECTURE.md: "MCP at the edge, native at
the core").

`{key}` and `{secret}` in a probe's headers/body/params are filled at call time
from the pasted credential. They are the only substitutions.
"""

CAPABILITIES = {
    "anthropic": {
        "name": "Anthropic (Claude)",
        "kind": "llm",
        "detail": "Claude models - one half of the roster.",
        "unlocked": "Claude can now join your conversations (and write chat titles and summaries).",
        "env": ["ANTHROPIC_API_KEY"],
        "chat_toggle": None,
        "any_of": None,
        "optional": False,
        "probe": {
            "method": "GET",
            "url": "https://api.anthropic.com/v1/models",
            "headers": {"x-api-key": "{key}", "anthropic-version": "2023-06-01"},
        },
    },
    "openai": {
        "name": "OpenAI (GPT)",
        "kind": "llm",
        "detail": "GPT models - the other half of the roster.",
        "unlocked": "GPT can now join your conversations.",
        "env": ["OPENAI_API_KEY"],
        "chat_toggle": None,
        "any_of": None,
        "optional": False,
        "probe": {
            "method": "GET",
            "url": "https://api.openai.com/v1/models",
            "headers": {"Authorization": "Bearer {key}"},
        },
    },
    "elevenlabs": {
        "name": "ElevenLabs Voice",
        "kind": "audio",
        "detail": "Voice conversations (each model gets its own voice).",
        "unlocked": "Voice is on - every model speaks in its own voice, and you can talk over them to interrupt.",
        "env": ["ELEVENLABS_API_KEY"],
        "chat_toggle": "voice_mode",
        "any_of": None,
        "optional": False,
        "probe": {
            "method": "GET",
            "url": "https://api.elevenlabs.io/v1/user",
            "headers": {"xi-api-key": "{key}"},
        },
    },
    "tavily": {
        "name": "Tavily Web Search",
        "kind": "search",
        "detail": "Web search engine #1.",
        "unlocked": "The models can now search the web with Tavily instead of guessing.",
        "env": ["TAVILY_API_KEY"],
        "chat_toggle": "web_enabled",
        "any_of": "web_search",
        "optional": True,
        "probe": {
            "method": "POST",
            "url": "https://api.tavily.com/search",
            "headers": {"Authorization": "Bearer {key}"},
            "body": {"query": "hello", "max_results": 1},
        },
    },
    "brave": {
        "name": "Brave Web Search",
        "kind": "search",
        "detail": "Web search engine #2.",
        "unlocked": "The models can now search the web with Brave instead of guessing.",
        "env": ["BRAVE_API_KEY"],
        "chat_toggle": "web_enabled",
        "any_of": "web_search",
        "optional": True,
        "probe": {
            "method": "GET",
            "url": "https://api.search.brave.com/res/v1/web/search",
            "headers": {"X-Subscription-Token": "{key}", "Accept": "application/json"},
            "params": {"q": "hello", "count": "1"},
        },
    },
    "reddit": {
        "name": "Reddit Research",
        "kind": "search",
        "detail": "Reddit research (read-only).",
        "unlocked": "The models can now read Reddit threads and comments while researching.",
        "env": ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"],
        "chat_toggle": "web_enabled",
        "any_of": None,
        "optional": True,
        # No probe: Reddit's credentials are accepted without a live call.
        "probe": None,
    },
}


# Section metadata per kind - the console renders whatever this says, in this
# order, so a new kind needs no frontend change. Lives here rather than in
# integrationsView.js because that was the last hardcoded site: the page was a
# catalogue precisely because it rendered a hand-written list.
#
# `blurb` is the plain-English "what this is" a non-technical reader sees before
# any row - comprehension is accessibility, so it is required, not decorative.
KINDS = {
    "llm":     ("AI models",
                "The providers that host the models in your room, and the model seats using each one."),
    "code":    ("Coding guest",
                "A coding agent that can be called into a chat to work on your code. It takes a turn like a participant."),
    "audio":   ("Voice",
                "Spoken conversations - each model talks in its own voice, and you can talk back."),
    "search":  ("Web research",
                "Lets the models look things up instead of guessing from memory."),
    "toolset": ("Repository tools",
                "What the models may do with your GitHub repositories - read issues and pull requests, and write back with their name attached."),
    "memory":  ("Shared memory",
                "A companion service that gives every chat a cheat-sheet about you. Optional."),
    "mcp":     ("MCP servers",
                "External tool servers your models can call. Add, test, and remove them below; changes apply after a restart."),
    "channel": ("Incoming events",
                "Ways things reach a chat without anyone asking - your own tools posting in."),
}


def sections():
    """Ordered section descriptors for any UI rendering the registry."""
    return [{"kind": k, "title": t, "blurb": b} for k, (t, b) in KINDS.items()]
