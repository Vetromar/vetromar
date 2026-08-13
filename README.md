# Vetromar

**Your company converted into context.** Vetromar fuses what's said in the
room with every digital source — meetings, Slack, Notion, email, tickets —
into one living, evidence-backed knowledge graph that your AI agents can
query over MCP.

Free and open source (AGPL-3.0). Local-first. Bring your own AI provider or
run fully offline.

- **Capture** meetings (import audio or record live) — transcription +
  speaker diarization + decision extraction, with every extracted unit
  **evidence-gated**: it must carry verbatim quotes validated against the
  raw transcript, or it is rejected.
- **Connect** your stack as a generic MCP client: Notion, Slack, GitHub,
  Google Workspace, Linear, PostHog, and 20+ more official servers — no
  per-source integration code, ever.
- **Query** it from your own agent: `vetromar serve` exposes the graph over
  MCP (hybrid semantic + full-text search, time travel, provenance, edges).
- **Share** knowledge in shared graphs hosted by whoever creates them — a
  friend's always-on machine or a small server, never central
  infrastructure. Your identity is a local keypair; no accounts, no
  passwords.

## Install (macOS, Apple Silicon)

Download the signed, notarized app:
**[Vetromar.dmg](https://github.com/Vetromar/releases/releases/latest/download/Vetromar.dmg)** —
drag to Applications. It keeps itself up to date.

## Bring your own AI

Every AI call uses a provider you configure in Settings — nothing routes
through any Vetromar service:

| Provider setting | Works with |
| --- | --- |
| **Anthropic** (API key) | claude models via the native SDK |
| **OpenAI-compatible** (base URL + key + model) | OpenAI, OpenRouter, Groq, Gemini's compat endpoint, vLLM, a LiteLLM proxy, … |
| **OpenAI-compatible, local** (`http://localhost:11434/v1`) | Ollama, LM Studio — nothing leaves your machine |
| **Fully local mode** | bundled Ollama runtime + local Whisper: extraction, transcription, and search all on-device |

Optional: a [Deepgram](https://deepgram.com) key for fast cloud
transcription (otherwise transcription runs locally).

## Host shared graphs (headless)

Shared graphs sync through a host: the app on an always-on machine, or a
headless server anywhere — see **[docs/self-hosting.md](docs/self-hosting.md)**:

```sh
docker compose up -d
docker compose exec server python -m cloud set-owner <your-public-key>
```

## Development

Python 3.11–3.13 (`whisperx` caps at <3.14):

```sh
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # engine + tests
pip install -e ".[capture]"      # + audio pipeline (torch, whisperx)
pip install -e ".[ui]"           # + the desktop app's local API
pip install -e ".[cloud]"        # + the workspace server
python -m pytest tests/ -q
```

The desktop app is a Tauri shell over a local FastAPI sidecar — see
`desktop/README.md`. The `vetromar` CLI covers capture, connect/sync, and
the MCP server. See [CONTRIBUTING.md](CONTRIBUTING.md) for the project's
architectural invariants before making changes.

## License

[AGPL-3.0](LICENSE). You can use, modify, self-host, and redistribute
freely; if you offer a modified Vetromar as a network service, you must
offer its source too.
