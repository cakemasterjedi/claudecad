# Changelog

## Unreleased

### Added

- **`fsgen web`**: LAN web UI (stdlib server, password login, CSRF header, security headers; plain HTTP, for trusted
  networks only). Tabs: **Parts** (edit, save, check in a worker process, 3D preview), **Generate** (prompt to
  part with a live log, paste mode, 0 Onshape calls, one job at a time), **Usage** (read-only API usage).
- `generate_native(..., on_log=)`: per-run progress sink (several runs in threads keep separate logs).
- **Geometry check** in `studio check` / `build`: per part, valid B-rep, closed shell, positive volume, one solid,
  watertight STL mesh.
- **Wall check** (`--min-wall`, default 1.2 mm): regions thinner than the limit, by ray casting; skips grazing hits
  near fillets and edges.
- **`studio build --formats step,stl,3mf`**: one file per part (named from the fit file, else `part<N>`) next to the
  combined files, and a 3MF with one object per part.
- **Section PNG** from `studio fit` (`out/<name>/<name>_section.png`): the assembled parts cut by the `[section]`
  plane of the fit file.
- **Exploded scene** in `studio view` (`[view]` table in the fit file).

### Changed

- `studio build` exits 1 and exports nothing when a part fails the geometry check.
- `fsgen generate --paste` no longer needs Onshape keys (paste mode makes no calls).

## 0.2.0 (2026-10-10)

### Added

- **Sketches on part faces.** Pick the face by feature (`qCapEntity`, `qNonCapEntity` + `qParallelPlanes`).
  Coordinates start at the world origin projected onto the face, with the axes of the parallel default plane.
  The push measures Onshape's axes on that face (1 extra call per face sketch) and dimensions every point to the
  origin, so variables move the sketch. Paste export and local builder use the same frame.
  Example: `examples/native/face_sketches.fs`.
- **`--document <Onshape URL>`** (or `FSGEN_DOCUMENT`) on `studio push` and `generate`: build in an existing
  document instead of `fsgen-workspace`.
- **Fit check** (`fsgen studio fit`): moves printed parts into their assembled position, adds real components
  (STEP files, pins, wires) and reports overlaps, hinge and slide motions and snap fits. 0 calls.
- **3D viewer** (`fsgen studio view`): one HTML file with the print layout and the assembly; hide parts, cut
  along X/Y/Z.
- **Print check** in `studio check` / `build`: downward faces flatter than 45° that are not on the bed.
- Local engine: `qCapEntity`, `qNonCapEntity`, `qParallelPlanes`.

### Changed

- Variables now drive every line, polyline and arc point (before: only rectangles, circles and explicit
  dimensions), on default planes, offset planes and part faces.
- Feature patterns use "Reapply features" (`fullFeaturePattern`) by default; set it to `false` to copy geometry.
- A variable change no longer re-sends dimensioned face sketches.
- The spec tells the AI to pick faces by feature: point-based queries are stored as fixed numbers in Onshape and
  lose their face when a variable moves it.
- README: custom features vs. native trees, comparison with the Onshape MCP servers, public example document with
  the prompts and call costs.

### Fixed

- Mirrored or patterned ADD extrudes failed in Onshape with `PATTERN_SWITCH_TO_PER_INSTANCE`.
- Local builder: a query for edges could return the sketch edge instead of the part's edge, so chamfers found no
  edges; fillets and chamfers that OpenCascade refused at meter scale are retried at millimeter scale.

## 0.1.0 (2026-10-01)

First public version: native Onshape feature trees from a description or a script, variables in a Variable
Studio, local test build, cost estimate before every push, paste export (0 calls), threads, STEP → editable tree.
