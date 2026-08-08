# Acknowledgements

This project is MIT-licensed and built on permissively-licensed open source
(installed via pip/npm, not vendored; all licences checked for compatibility:
MIT, BSD, Apache-2.0, PSF, MPL-2.0, ISC):

## Backend
- [FastAPI](https://fastapi.tiangolo.com/) (MIT) · [Uvicorn](https://www.uvicorn.org/) (BSD-3-Clause) · [Pydantic](https://docs.pydantic.dev/) (MIT)
- [python-dotenv](https://github.com/theskumar/python-dotenv) (BSD-3-Clause) · [python-multipart](https://github.com/Kludex/python-multipart) (Apache-2.0): `.env` loading, file uploads
- [Anthropic](https://github.com/anthropics/anthropic-sdk-python) (MIT) / [OpenAI](https://github.com/openai/openai-python) (Apache-2.0) Python SDKs
- [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) (MIT), which drives the summoned Claude Code guest (which also needs the Claude Code CLI on the machine at runtime)
- [httpx](https://www.python-httpx.org/) (BSD-3-Clause) · [websockets](https://websockets.readthedocs.io/) (BSD-3-Clause)
- [youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api) (MIT)
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) (Apache-2.0) · [NumPy](https://numpy.org/) (BSD-3-Clause): the offline local speaker matcher for room mode (#28), which names a known voice on-device so the common single-speaker turn needs no second transcription
- [SQLite](https://sqlite.org/) (public domain) · [pytest](https://pytest.org/) (MIT)

## Frontend
- [React](https://react.dev/) (MIT) · [Vite](https://vitejs.dev/) (MIT) · [Tailwind CSS](https://tailwindcss.com/) (MIT)
- [react-markdown](https://github.com/remarkjs/react-markdown) (MIT) · [remark-gfm](https://github.com/remarkjs/remark-gfm) (MIT): message rendering
- [Lucide](https://lucide.dev/) (ISC): icons

## Models (fetched at runtime, not vendored)
- **NeMo TitaNet-Small** speaker-verification model ([CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/)), by [NVIDIA NeMo](https://github.com/NVIDIA/NeMo), distributed as ONNX via the [sherpa-onnx speaker-recognition models](https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models). Crossband fetches this ~38MB file once to the data directory (SHA-256-verified, never committed) and runs it fully offline to identify known voices in room mode (#28).

## Services (paid APIs, bring your own keys)
- **Anthropic** and **OpenAI**: the minds in the room
- **ElevenLabs**: the voices, Flash v2.5 streaming TTS and Scribe v2 streaming STT
- **Tavily** and **Brave Search**: dual-engine web research
- **Reddit**: thread fetching (OAuth)

## Design & research
Companion memory service and its research lineage:
[Membro](https://github.com/shawn-durrani/membro).
See its REFERENCES.md for the cited agent-memory literature.
