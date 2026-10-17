# fsgen — notes for Claude Code

Follow **`AGENTS.md`** (the shared instructions for all AI assistants: making a part, API budget,
documentation). Claude-specific additions:

- `/make-part <description>` (`.claude/commands/make-part.md`) runs the part workflow from `AGENTS.md`.
- `fsgen generate` uses Claude by default: the logged-in `claude` CLI, or the Anthropic API if
  `ANTHROPIC_API_KEY` is set.

## FeatureScript MCP server (`onshape-featurescript`)

Every MCP tool call uses the same Onshape allocation (OAuth). Use it only where it adds something fsgen can't:

- At the start of a session that will use the API: call `get_api_usage` once and record the result with
  `fsgen usage --set-official <used>` so the ledger baseline includes MCP and other usage.
- Before writing or changing generated FeatureScript (paste export `fsgen/native/paste.py`, prompts in
  `fsgen/native/prompts.py`), check unfamiliar std signatures with `search_featurescript_documentation`
  instead of guessing. Note in `docs/FINDINGS.md` whether a doc search changed the official count.
- Never call tools that write to the user's Onshape (`put_featurescript`, `create_feature_studio`,
  `test_feature`, `create_geometry`, `set_api_allocation`) without asking first.

<!-- handoff:learnings:begin -->
## Session Learnings (auto-updated by handoff)

### Implicit Rules
- Hardware: AMD RX 6700 XT 12GB VRAM (ROCm, no nvidia-smi; use rocm-smi), 35GB RAM
- Ollama models installed: qwen3.5:9b, gemma4:12b-it-qat
- Ollama loop diagnosis: journalctl -u ollama grep 'truncated' (truncated=1 → window filled)
- fsgen generate CLI without --cloud = 0 Onshape calls; use for live local-model tests
- Web UI model choice: Force local model checkbox + dropdown (list from Ollama at page load)
- Run tests: .venv/bin/python -m pytest -q (~110s, 202 tests); no ruff in venv
- Live server: .venv/bin/fsgen web on port 8770, log out/web-server.log, password from .env
- Screenshot helper: /tmp/claude-1000/-home-smacklin-claudecad/7ab81a8c-cdb7-4c3c-bbd1-597ec236792d/scratchpad/shot.py with PYTHONPATH=<that dir>/pylib; e2e harness /tmp/claude-1000/fsgen-e2e/srv.py on 8791 pw testpw
- Ollama systemd service active+enabled at localhost:11434; env FSGEN_OLLAMA_MODEL, FSGEN_OLLAMA_THINK, FSGEN_OLLAMA_NUM_CTX, FSGEN_OLLAMA_NUM_PREDICT, FSGEN_OLLAMA_TEMPERATURE
- No Co-Authored-By trailer in claudecad commits; exclude .handoff/ from commits

### Key Decisions
- Decision: Ollama options = num_ctx 32768 (FSGEN_OLLAMA_NUM_CTX) + num_predict 8192 (FSGEN_OLLAMA_NUM_PREDICT); temperature only if FSGEN_OLLAMA_TEMPERATURE set — Reason: model Modelfile sampling avoids qwen repetition loop; cap makes loop fail in ~3 min not 8+
- Decision: retry once when done_reason=length, then LLMError 'cut off twice' — Reason: looping is stochastic at temp 1; second run often answers fine
- Decision: recommended local models gemma4:26b (MoE ~4B active, vision) > qwen3-coder:30b (MoE, text only) > qwen2.5-coder:14b (fits VRAM, fastest) — Reason: 12GB VRAM; MoE tolerates CPU spill, dense 24-27B too slow
- Decision: web password persisted as FSGEN_WEB_PASSWORD in root .env (mode 600), single-quoted, min 8 chars, no ' or newline — Reason: survives restarts; cmd_web now load_dotenv(root/.env)
- Decision: password change requires current pw, wrong attempts hit LoginLimiter, other sessions dropped via SessionStore.keep_only — Reason: security
- Decision: save_password lives in fsgen/web/password_api.py, reused by CLI set_web_password — Reason: one validator
- Decision: Vec.of units error names both units + area hint when length^2 — Reason: small local models looped on same mistake with vague error
- Decision: shared push in fsgen/native/pushflow.py (estimate 0 calls, push incl. catalog recording) used by CLI studio push and web — Reason: no duplicated push logic
- Decision: server.py Download return type → Content-Disposition attachment — Reason: router treated non-dict returns as SSE
- Decision: zip excludes llm_*.md; failed jobs still get zip — Reason: logs large; last attempt useful
- Decision: preview reuses /api/check + viewer3d.js lazily — Reason: no new mesh endpoint, 0 Onshape calls
- Decision: push estimate + window.confirm first; 409 over ONSHAPE_RUN_BUDGET (60) or allocation left; one push at a time; target existing .fsgen_workspace.json doc — Reason: API budget rules in AGENTS.md

<!-- handoff:learnings:end -->
