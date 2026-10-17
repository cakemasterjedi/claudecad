# fsgen — instructions for AI coding assistants

Read by any assistant that supports `AGENTS.md` (Codex, Cursor, Copilot, Gemini CLI, …); Claude Code reads
`CLAUDE.md`, which points here. fsgen turns part descriptions into Onshape parts. **You are the designer:** you
write the Part Studio script, fsgen checks it, builds it locally and delivers it to Onshape. Read `README.md`,
`docs/CAPABILITIES.md` and `docs/FINDINGS.md` before changing the code.

## Making a part

1. Run `.venv/bin/fsgen spec`: the script dialect, the user's `rules.md`, the feature catalog and verified
   examples. Follow it.
2. If the request names a real product, look up its dimensions first (if you can browse). List your
   assumptions (every dimension or interpretation the request doesn't state) and show them to the user.
3. Write `parts/<name>.fs` (a Part Studio script).
4. Run `.venv/bin/fsgen studio build parts/<name>.fs`: trace + local build (errors, geometry report, STEP/STL,
   a 4-view preview `out/<name>/<name>.png`) and the push estimate. 0 Onshape calls. Look at the preview if you
   can read images; iterate with the user. Features reported as "not built locally" are not in the preview:
   say so, don't claim they are checked. For printed parts follow the 3D-printing rules in `rules.md`; the
   build prints a print check (overhangs flatter than 45° off the bed), a geometry check (valid closed solid,
   watertight mesh) and a wall check (`--min-wall`, default 1.2 mm); `--formats step,stl,3mf` also writes one file
   per part and a 3MF.
   Parts that go together (hinges, snap fits, lids, a part around a real component): write
   `parts/<name>.fit.toml` and run `fsgen studio fit parts/<name>.fs` (assembled-position overlaps and motions,
   format in `fsgen/native/fit.py`). `fsgen studio fit` also writes a section PNG of the assembly (`[section]` in the fit file);
   `fsgen studio view parts/<name>.fs` writes an interactive 3D viewer (with an exploded scene).
5. Deliver: `fsgen studio paste parts/<name>.fs --name "…"` (custom feature to paste, 0 calls) or
   `fsgen studio push parts/<name>.fs --name "…" --metrics --budget N` (native editable tree; ask first and show
   the estimate). Edits to a pushed part: change the script and push again (1–2 calls).

`fsgen generate "…"` does the same unattended with a background LLM (`--backend`, or any CLI LLM via
`FSGEN_LLM_COMMAND`); use it only when asked.

## Onshape API budget (EDU plans: 2500 calls/year — the scarcest resource)

- Develop and test offline: local builder, `tests/` (fake Onshape in `fsgen/costmodel.py`). Never use the real
  API to test code changes.
- Before anything that costs calls, show the estimate (`fsgen studio check …` prints it) and ask if it's more
  than a few calls. Prefer the free paste workflow unless the user wants an editable native tree.
- `fsgen usage` shows calls used/left from the local ledger; `fsgen usage --set-official N` records Onshape's
  official count.

## Documentation

- Append findings to `docs/FINDINGS.md` as they happen (dated, with how they were established: tested /
  measured / docs). Keep `docs/CAPABILITIES.md` current; regenerate its feature table with
  `fsgen features --markdown`.
- New feature types: verified example in `examples/native/`, local support in `fsgen/native/local.py`, and an
  Onshape-measured volume in `tests/test_native_local.py`.
- Commits: no AI attribution lines (no "Co-Authored-By" for assistants).

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
