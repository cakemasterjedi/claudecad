"""fsgen command line.

  fsgen generate "a 60x40x5 mm plate with four M4 holes" [--cloud]
  fsgen run part.fs [-p width=80mm] [--png] [--out DIR]
  fsgen watch part.fs                  # rebuild + live 3D viewer on every save
  fsgen push part.fs [-p ...]          # Onshape: upload, regenerate, compare, export STEP/STL
  fsgen eval part.fs                   # Onshape error messages for a failing file
  fsgen compare examples/compat/*.fs   # local engine vs Onshape
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from .fslite.values import UNITS, Q


def parse_param(text: str):
    name, _, raw = text.partition("=")
    raw = raw.strip()
    m = re.fullmatch(r"([-+]?[0-9.eE+-]+)\s*(mm|cm|m|in|deg)?", raw)
    if raw.lower() in ("true", "false"):
        return name, raw.lower() == "true"
    if not m:
        return name, raw
    v, unit = float(m.group(1)), m.group(2)
    if unit is None:
        return name, v
    u = {"mm": UNITS["millimeter"], "cm": UNITS["centimeter"], "m": UNITS["meter"], "in": UNITS["inch"],
         "deg": UNITS["degree"]}[unit]
    return name, Q(v * u.v, u.dims)


def params_of(args) -> dict:
    return dict(parse_param(p) for p in (args.param or []))


def cmd_run(args):
    from .fslite.runner import describe, export, render_png, run_source

    path = Path(args.file)
    res = run_source(path.read_text(), params=params_of(args))
    print(describe(res))
    print(f"({res.seconds * 1000:.0f} ms)")
    if not res.ok:
        return 1
    out = Path(args.out or Path("out") / path.stem)
    files = export(res, out, path.stem)
    if args.png:
        files.append(render_png(res, out / f"{path.stem}.png", path.stem))
    print("wrote", ", ".join(str(f) for f in files))
    return 0


def _ask_answers(questions) -> list[dict]:
    answers = []
    for i, q in enumerate(questions, 1):
        print(f"\n{i}. {q['text']}")
        opts = q.get("options") or []
        for j, o in enumerate(opts, 1):
            print(f"   {j}) {o}")
        raw = input("> ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(opts):
            raw = opts[int(raw) - 1]
        answers.append({"question": q["text"], "answer": raw})
    return answers


def _generate_native_cli(args) -> int:
    from .native.generate import generate_native
    from .native.prompts import append_answers, variant_note

    formats = tuple(f for f in (args.export or "").split(",") if f)
    variants = list(args.variant or [])
    if args.ab and not variants:
        variants = ["", ""]

    def run(request, out, name, ask, note):
        return generate_native(request, out, name=name, paste=args.paste, backend=args.backend, model=args.model,
                               max_fixes=args.max_fixes, review_rounds=0 if args.no_review else args.reviews,
                               web=not args.no_web, llm_command=args.llm_command, document=args.document,
                               export_formats=formats, budget=args.budget, ask=ask, variant_note=note)

    def show(obj):
        print(json.dumps(obj, indent=2, default=str))

    def answer(res, request):
        """The request with answers appended, or None when there is no terminal to ask on."""
        if not sys.stdin.isatty():
            show({k: v for k, v in res.items() if k != "history"})
            return None
        return append_answers(request, _ask_answers(res["questions"]))

    out = Path(args.out) if args.out else None
    if not variants:
        res = run(args.request, out, args.name, args.ask, None)
        if res.get("questions"):
            request = answer(res, args.request)
            if request is None:
                return 2
            res = run(request, out.with_name(out.name + "-answered") if out else None, args.name, False, None)
        show({k: v for k, v in res.items() if k != "history"})
        return 0 if res["ok"] else 1

    request, results, ask = args.request, [], args.ask
    labels = [chr(ord("A") + i) for i in range(len(variants))]
    a_result = None
    for label, text in zip(labels, variants):
        note = variant_note(label, text, a_result.get("assumptions") if a_result else None)
        out_l = out / label if out else None
        name_l = f"{args.name}-{label}" if args.name else None
        if ask and label == "A":
            res = run(request, out / "questions" if out else None, name_l, True, note)
            if res.get("questions"):
                request = answer(res, request)
                if request is None:
                    return 2
                res = run(request, out_l, name_l, False, note)
        else:
            res = run(request, out_l, name_l, False, note)
        if label == "A":
            a_result = res
        results.append({"label": label, **{k: v for k, v in res.items() if k != "history"}})
    show({"variants": results})
    return 0 if all(r["ok"] for r in results) else 1


def cmd_generate(args):
    if not args.custom:
        return _generate_native_cli(args)
    from .generate import generate

    res = generate(args.request, Path(args.out) if args.out else None, backend=args.backend, model=args.model,
                   max_fixes=args.max_fixes, review=not args.no_review, cloud=args.cloud)
    print(json.dumps({"ok": res.ok, "out": str(res.out_dir), "attempts": len(res.attempts),
                      "local": res.local_metrics, "cloud": res.cloud}, indent=2, default=str))
    return 0 if res.ok else 1


def paste_command(path, tr, args) -> int:
    """Write the script as a parametric custom feature to paste into a Feature Studio (0 API calls)."""
    import subprocess
    from .native.paste import PasteError, paste_export
    from .native.script import trace as _trace
    from .outputs import to_dropbox

    title = args.name or path.stem.replace("_", " ").title()
    try:
        code, chk = paste_export(_trace(path.read_text(), "studio"), title)
    except PasteError as e:
        print(f"paste export not possible: {e}")
        return 1
    out = Path("out") / "paste" / f"{path.stem}.fs"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(code)
    print(f"wrote {out}  (parameters: {', '.join(chk.get('parameters', []))})")
    print(f"local check: custom feature {chk.get('paste_volume_mm3')} mm^3 vs native build {chk['native_volume_mm3']} mm^3 -> "
          + ("same" if chk["ok"] else f"DIFFERENT {chk.get('error', '')}"))
    copied = to_dropbox(path.stem, [out])
    if copied:
        print("copied to Dropbox:", copied[0])
    try:
        subprocess.run(["pbcopy"], input=code, text=True, check=True)
        print("copied to the clipboard: in Onshape create a Feature Studio, select all, paste, then insert the "
              f"feature \"{title}\" in a Part Studio (0 API calls)")
    except (OSError, subprocess.CalledProcessError):
        pass
    return 0 if chk["ok"] else 1


def fit_view_command(path, tr, args) -> int:
    """fit: assembled-position interference check from <script>.fit.toml; view: interactive 3D viewer (0 calls)."""
    from .native.fit import FitError, default_spec_path, explode, export_assembly, load_spec, place, run_fit
    from .native.local import build_local
    from .native.viewer import printed_items, scene_parts, write_viewer

    loc = build_local(tr)
    if not loc.ok:
        print(f"local build error: {loc.error}")
        return 1
    if loc.partial:
        print("note: some features were not built locally; the check uses the local geometry")
    solids = [b.shape for b in loc.run.solids]
    spec_path = Path(args.fit) if args.fit else default_spec_path(path)
    out = Path("out") / path.stem
    try:
        spec = load_spec(spec_path) if spec_path.exists() else None
        if args.action == "fit":
            if spec is None:
                print(f"no fit file: write {spec_path} (see `fsgen studio fit --help` and fsgen/native/fit.py)")
                return 1
            res = run_fit(solids, spec, spec_path.parent)
            print(res.report())
            print("wrote", export_assembly(res.placed, out / f"{path.stem}_assembled.step"))
            from .native.section import render_section, section
            cut = section(res.placed, spec)
            if cut:
                print("wrote", render_section(cut, spec, out / f"{path.stem}_section.png", path.stem),
                      "(section: " + ", ".join(f"{c.name} {c.area_mm2:.0f} mm2" for c in cut) + ")")
            else:
                print("section plane misses every part (set [section] origin/normal in the fit file)")
            print("(0 Onshape calls)")
            return 0 if res.ok else 1
        from .native.viewer import PALETTE
        placed = place(solids, spec, spec_path.parent) if spec is not None else []
        names = {p.index: p.name for p in placed if p.index is not None}
        colors = {p.index: p.color for p in placed if p.index is not None and p.color}
        printed = printed_items(solids, names)
        color_of = {label: colors.get(i, PALETTE[i % len(PALETTE)]) for i, (label, _, _) in enumerate(printed)}
        scenes = [{"name": "As printed", "parts": scene_parts([(l, s_, color_of[l]) for l, s_, _ in printed])}]
        if placed:
            scenes.append({"name": "Assembled",
                           "parts": scene_parts([(p.name, p.shape, color_of.get(p.name, p.color)) for p in placed])})
            scenes.append({"name": "Exploded",
                           "parts": scene_parts([(n, s_, color_of.get(n, c)) for n, s_, c in explode(placed, spec)])})
        else:
            print(f"(no fit file at {spec_path}: showing the print layout only)")
        title = args.name or path.stem.replace("_", " ").capitalize()
        f = write_viewer(out / f"{path.stem}_view.html", title, scenes,
                         note=f"{path.name}: {len(solids)} part(s). Generated by fsgen studio view.")
        print("wrote", f, "(open it in a browser; 0 Onshape calls)")
        return 0
    except FitError as e:
        print(f"fit file {spec_path}: {e}")
        return 1


def cmd_studio(args):
    from .native import build as nb
    from .native.script import trace

    path = Path(args.file)
    tr = trace(path.read_text())
    if not tr.ok:
        print(f"script error: {tr.error}")
        return 1
    for f in tr.features:
        print(f"  {f.feature_type:16s} {f.name}")
    from .native import build as nb
    from .native import pushflow
    sname = args.name or path.stem
    est, st0 = pushflow.estimate(tr, sname, args.document, fresh=args.fresh, metrics=args.metrics,
                                 exports=tuple(f for f in (args.export or "").split(",") if f))
    print(f"push to \"{sname}\" ({'new' if st0 is None else 'update'}): " + est.text())
    if args.action in ("check", "build", "push"):
        for w in est.warnings:
            print("warning:", w)
    if args.action == "paste":
        return paste_command(path, tr, args)
    if args.action in ("fit", "view"):
        return fit_view_command(path, tr, args)
    if args.action in ("check", "build"):
        from .native.local import build_local, render
        loc = build_local(tr)
        print(loc.report() if loc.ok else f"local build error: {loc.error}")
        valid = True
        if loc.ok and loc.run.solids:
            from .native.printcheck import overhangs, report as print_report
            solids = [b.shape for b in loc.run.solids]
            print(print_report(overhangs(solids)))
            from .native import geomcheck
            checked = geomcheck.validity(solids)
            valid = all(v.ok for v in checked)
            print(geomcheck.validity_report(checked))
            print(geomcheck.thin_wall_report(geomcheck.thin_walls(solids, args.min_wall), args.min_wall))
            if ov := geomcheck.overlap_report(geomcheck.overlaps(loc.run.solids)):
                print(ov)
        if args.action == "build" and loc.ok and not valid:
            print("not exporting: fix the geometry problems above first")
        if args.action == "build" and loc.ok and valid:
            from .fslite.runner import export as local_export
            out = Path("out") / path.stem
            formats = [f.strip() for f in args.formats.split(",") if f.strip()]
            if bad := [f for f in formats if f not in ("step", "stl", "3mf")]:
                print(f"--formats: unknown {', '.join(bad)} (use step, stl, 3mf)")
                return 1
            files = local_export(loc.run, out, path.stem, tuple(f for f in formats if f != "3mf"))
            files.append(render(loc, out / f"{path.stem}.png", path.stem))
            if loc.run.solids and ("3mf" in formats or len(loc.run.solids) > 1):
                from .native.fit import FitError, default_spec_path, load_spec
                from .native.partexport import export_parts, part_names
                bodies = [b.shape for b in loc.run.solids]
                spec_path = Path(args.fit) if args.fit else default_spec_path(path)
                try:
                    spec = load_spec(spec_path) if spec_path.exists() else None
                except FitError as e:
                    spec = None
                    print(f"fit file {spec_path}: {e}")
                names, note = part_names(bodies, spec, spec_path.parent, [b.name for b in loc.run.solids])
                if note:
                    print(f"note: {note}")
                seen: dict = {}
                for n, b in zip(names, loc.run.solids):
                    if b.color:
                        seen.setdefault(n, set()).add(tuple(round(c, 3) for c in b.color))
                for n, cols in seen.items():
                    if len(cols) > 1:
                        print(f"warning: parts named {n!r} have {len(cols)} different colors; they export as one 3MF "
                              "object with the first color (rename one group to keep them apart)")
                files += export_parts(bodies, names, out, path.stem, tuple(formats), [b.color for b in loc.run.solids])
            print("wrote", ", ".join(map(str, files)))
            from .outputs import open_in_freecad, to_dropbox
            if args.dropbox:
                copied = to_dropbox(path.stem, files + [path])
                print("copied to Dropbox:", copied[0].parent if copied else "(set FSGEN_DROPBOX_DIR)")
            if args.open:
                open_in_freecad(files[0])
        print(f"({loc.seconds * 1000:.0f} ms, 0 Onshape calls)")
        return 0 if loc.ok and valid else 1
    from .onshape import Onshape

    if args.budget is not None and est.total > args.budget:
        print(f"refusing to push: estimate {est.total} exceeds --budget {args.budget}")
        return 1
    o = Onshape(purpose=f"studio push {path.name}")
    r, st = pushflow.push(o, tr, args.name or path.stem, path.read_text(), str(path), args.document,
                          public=args.public or None, fresh=args.fresh,
                          on_learned=lambda learned: print("catalog: learned", ", ".join(learned)))
    print(f"built {r.built}, reused {r.reused}: {r.url}")
    for w in r.warnings:
        print("warning:", w)
    if not r.ok:
        print(r.error)
        print(o.summary())
        return 1
    if args.metrics:
        print(nb.studio_metrics(o, st))
    out = Path("out") / st.name.replace(" ", "_")
    files = []
    if args.export:
        files += nb.export_studio(o, st, out, "part", tuple(args.export.split(",")))
    if args.view:
        files.append(nb.shaded_view(o, st, out / "onshape_view.png"))
    if files:
        print("wrote", ", ".join(map(str, files)))
    print(o.summary())
    return 0


def cmd_recognize(args):
    """STEP -> Part Studio script via featuretree recognition; checked locally, 0 Onshape calls."""
    from .recognize import RecognizeError, recognize_step
    from .native.script import trace
    from .usage import estimate_push

    path = Path(args.step)
    try:
        script, rep = recognize_step(path)
    except RecognizeError as e:
        print(f"could not translate: {e}")
        return 1
    out = Path(args.out or Path("out") / "recognized" / f"{path.stem}.fs")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(script)
    r = rep["report"] if isinstance(rep["report"], dict) else {}
    print(f"wrote {out} ({rep['features']} features, method: {r.get('method')})")
    print(f"volume: STEP {rep['step_volume_mm3']} mm^3, recognized script {rep['script_volume_mm3']} mm^3 -> "
          + ("MATCH" if rep["match"] else "PARTIAL (featuretree could not capture every feature)"))
    for w in r.get("warnings", []):
        print("  featuretree:", w)
    if "rotated onto Z" in " ".join(r.get("warnings", [])):
        print("  note: the part was re-oriented so its extrude axis is Z")
    print("to push: fsgen studio push", out, " (" + estimate_push(trace(script), None).text() + ")")
    return 0 if rep["match"] else 2


def cmd_spec(args):
    """The instructions the LLM gets (dialect spec + rules.md + feature catalog + verified examples)."""
    from .native import prompts
    print(prompts.system_prompt())
    return 0


def cmd_usage(args):
    """Counted Onshape calls from the local ledger, plus the baseline from Onshape's usage report."""
    from .usage import allocation_status, breakdown, set_official

    if args.set_official is not None:
        set_official(args.set_official)
        print(f"recorded official count {args.set_official} (from the MCP get_api_usage tool) as the new baseline")
    a = allocation_status()
    print(f"Onshape API allocation: {a.used} / {a.allocation} used ({a.used / a.allocation:.0%}); {a.left} left")
    if a.logged:
        purposes, calls = breakdown(a.logged, args.top)
        print("logged since baseline by command:", ", ".join(f"{k} {v}" for k, v in purposes))
        print("top endpoints:")
        for k, v in calls:
            print(f"  {v:5d}  {k}")
    return 0


def lan_addresses() -> list[str]:
    """This machine's non-loopback IPv4 addresses (no traffic is sent)."""
    import socket

    found = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # UDP connect only picks the outgoing interface
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        found += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    return sorted({a for a in found if not a.startswith("127.") and a != "0.0.0.0"})


def set_web_password(env_path: Path, password: str | None = None) -> int:
    """Store FSGEN_WEB_PASSWORD in .env (prompting without echo when none is given)."""
    import getpass

    if password is None:
        password = getpass.getpass("New web password: ")
        if password != getpass.getpass("Repeat: "):
            print("passwords differ; nothing changed")
            return 1
    from .web.password_api import save_password

    try:
        save_password(env_path, password)
    except ValueError as e:
        print(f"{e}; nothing changed")
        return 1
    print(f"password saved to {env_path}; restart `fsgen web` to use it")
    return 0


def cmd_web(args):
    """Serve the LAN web UI."""
    import os
    import secrets

    from .web.server import make_server

    sys.stdout.reconfigure(line_buffering=True)  # show the URLs and password at once when piped to a file
    if args.set_password is not None:
        return set_web_password(Path(args.root) / ".env", args.set_password or None)
    from dotenv import load_dotenv
    load_dotenv(Path(args.root) / ".env")
    password = args.password or os.environ.get("FSGEN_WEB_PASSWORD")
    generated = not password
    if generated:
        password = secrets.token_urlsafe(9)
    httpd, app = make_server(Path(args.root), args.host, args.port, password)
    port = httpd.server_address[1]
    print(f"fsgen web on http://127.0.0.1:{port}/  (root: {app.root})")
    if args.host in ("0.0.0.0", "", "::"):
        for a in lan_addresses():
            print(f"  LAN: http://{a}:{port}/")
    if generated:
        print(f"password (shown once): {password}")
    print("WARNING: plain HTTP. Anyone on the network can see the traffic, including the password. "
          "Use only on a trusted network.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()
    return 0


def cmd_features(args):
    from .native import catalog
    from .native.script import SPECS

    if args.refresh:
        from .native.specs_tool import refresh
        from .cloud import workspace_ids
        from .onshape import Onshape
        o = Onshape(purpose="features --refresh")
        ws = workspace_ids(o)
        print("refreshed", refresh(o, ws["did"], ws["wid"], ws["part"]), "feature types")
    cat = catalog.load()
    if args.markdown:
        print(catalog.markdown_table())
        return 0
    if args.feature:
        ft = args.feature
        if ft not in SPECS:
            print(f"unknown feature type {ft}")
            return 1
        e = cat.get(ft, {})
        print(f"{ft} ({SPECS[ft]['name']}): {e.get('status', 'available')}")
        for n in e.get("notes", []):
            print("  note:", n)
        if e.get("example"):
            print("  example:", e["example"])
        for n in e.get("failures", []):
            print("  failure:", n)
        print("  settable parameters:")
        for k in catalog.settable_params(ft):
            p = SPECS[ft]["params"][k]
            extra = f" {p['enum']}{{{', '.join(p['options'])}}}" if p["type"] == "Enum" else \
                f" {p.get('quantity', '')}" if p["type"] == "Quantity" else ""
            print(f"    {k:32s} {p['type']}{extra}")
        return 0
    groups = {"verified": [], "learned": [], "available": []}
    for ft in sorted(SPECS):
        groups.setdefault(cat.get(ft, {}).get("status", "available"), []).append(ft)
    for g in ("verified", "learned", "available"):
        names = groups.get(g, [])
        if g == "available" and not args.all:
            names = [n for n in names if not n.startswith("sheetMetal")] + ["(sheetMetal*: 17 more, use --all)"]
        print(f"{g} ({len(groups.get(g, []))}): {', '.join(names)}\n")
    return 0


def cmd_push(args):
    from . import cloud
    from .compare import compare_metrics
    from .fslite.runner import run_source
    from .onshape import Onshape

    path = Path(args.file)
    src = path.read_text()
    o = Onshape()
    ws = cloud.ensure_workspace(o)
    p = cloud.push(o, ws, src, feature_name=path.stem, params=params_of(args))
    print(json.dumps(p, indent=2))
    if not p["ok"]:
        ev = cloud.evaluate(o, ws, src, params=params_of(args))
        print("\n".join(ev.errors))
        return 1
    m = cloud.part_metrics(o, ws)
    local = run_source(src, params=params_of(args))
    print("onshape:", m)
    if local.ok:
        print("local:  ", local.metrics())
        print("diff:   ", compare_metrics(local.metrics(), m) or "none")
    files = cloud.export(o, ws, Path(args.out or Path("out") / path.stem), f"{path.stem}_onshape",
                         formats=tuple(args.formats.split(",")))
    print("wrote", ", ".join(map(str, files)))
    return 0


def cmd_eval(args):
    from . import cloud
    from .onshape import Onshape

    o = Onshape()
    ws = cloud.ensure_workspace(o)
    r = cloud.evaluate(o, ws, Path(args.file).read_text(), params=params_of(args))
    print(json.dumps(r.__dict__, indent=2, default=str))
    return 0 if r.ok else 1


def cmd_compare(args):
    from .compare import main

    rows = main([Path(f) for f in args.files], Path("out/compare.json"))
    return 0 if all(r.get("match") for r in rows) else 1


def _uv(text: str, what: str):
    try:
        u, v = (float(x) for x in text.split(","))
    except ValueError:
        raise SystemExit(f"{what}: expected U,V in mm, e.g. 0,0")
    return (u, v)


def cmd_emboss(args):
    """Add flat-color SVG art to a native Part Studio script as raised multicolor bodies (0 Onshape calls)."""
    from .native import colorlayers as cl
    from .native import embossfile as ef

    part = Path(args.part)
    try:
        o = ef.Options(
            plane=args.plane, offset=args.offset, flip=args.flip, width=args.width, center=_uv(args.center, "--center"),
            mirror=args.mirror, height=args.height, heights=ef.parse_pairs(args.heights, "--heights", True),
            names={ef.norm_hex(k): v for k, v in ef.parse_pairs(args.names, "--names").items()},
            min_width=args.min_width, skip=args.skip_color or [], cylinder=args.cylinder, axis_plane=args.axis_plane,
            axis_center=_uv(args.axis_center, "--axis-center"), standoff=args.standoff, depth_in=args.depth_in,
            span=args.span, span_side=args.span_side, base_name=args.base_name, base_color=args.base_color,
            prefix=args.name)
        if args.cylinder is not None and args.flip:
            raise ef.EmbossError("--flip is for flat placement; with --cylinder the side comes from the sign of --offset")
        if args.cylinder is not None and args.cylinder <= 0:
            raise ef.EmbossError("--cylinder RADIUS must be positive")
        if args.base_color and not args.base_name:
            raise ef.EmbossError("--base-color needs --base-name")
        out, rep = ef.apply(part.read_text(), args.art, o)
    except (ef.EmbossError, ValueError, OSError) as e:
        print(f"emboss: {e}")
        return 1
    dest = Path(args.output) if args.output else part.with_name(f"{part.stem}_emboss.fs")
    dest.write_text(out)
    print(f"color groups in {args.art}:")
    for name, hx, area in rep["found"]:
        skipped = " (skipped)" if hx.lower() in {ef.norm_hex(h) for h in o.skip} else ""
        print(f"  {name:10s} {hx}  {area:8.2f} mm2{skipped}")
    if o.min_width > 0:
        print(f"widening to >= {o.min_width:g} mm: thin bands {len(rep['before'])} -> {len(rep['after'])}, "
              f"absorbed {len(rep['absorbed'])}, dropped pieces {len(rep['dropped'])}")
        for f, t, lbl, a in rep["absorbed"]:
            print(f"  absorbed {lbl}: {f} band {a:.3f} mm2 -> {t}")
        for t in rep["after"]:
            print(f"  STILL THIN {t.label} {t.name}: {t.width:.2f} mm, area {t.area:.2f} mm2, bbox {t.bbox}")
    print("embossed layers:")
    for name, hx, area, h in rep["groups"]:
        print(f"  {name:10s} {hx}  {area:8.2f} mm2  height {h:g} mm")
    b = rep["bounds"]
    where = (f"cylinder R={args.cylinder:g} (apex at {args.offset:g} mm from the {args.plane} plane)" if args.cylinder is not None
             else f"{args.plane} plane" + (f" + {args.offset:g} mm" if args.offset else ""))
    print(f"placement on {where}: u {b[0]:.2f}..{b[2]:.2f}, v {b[1]:.2f}..{b[3]:.2f} mm "
          f"({b[2] - b[0]:.2f} x {b[3] - b[1]:.2f})")
    print(f"wrote {dest}")
    print(f"next: fsgen studio build {dest} --formats step,stl,3mf")
    return 0


def cmd_watch(args):
    from .watch import watch

    watch(Path(args.file), port=args.port, params=params_of(args))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fsgen", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="prompt -> FeatureScript -> geometry")
    g.add_argument("request")
    g.add_argument("--cloud", action="store_true", help="also push to Onshape, compare and export")
    g.add_argument("--backend", default="auto", choices=["auto", "anthropic", "claude-cli", "command", "ollama"],
                   help="auto: FSGEN_LLM_COMMAND if set, else the Anthropic API if a key is set, else the claude CLI; "
                        "ollama: local model via OLLAMA_HOST / FSGEN_OLLAMA_MODEL (never chosen by auto)")
    g.add_argument("--llm-command", help="any LLM CLI that reads stdin and prints the answer, e.g. "
                                         "'ollama run qwen2.5-coder:32b' or 'gemini -p' (also FSGEN_LLM_COMMAND)")
    g.add_argument("--model")
    g.add_argument("--max-fixes", type=int, default=4)
    g.add_argument("--no-review", action="store_true")
    g.add_argument("--out")
    g.add_argument("--name", help="Part Studio name (reuses and updates it if it exists)")
    g.add_argument("--document", help="build in this existing document/branch URL (also FSGEN_DOCUMENT)")
    g.add_argument("--export", default="", help="export via API: comma list of step,stl (costs calls; the Onshape UI "
                                                "exports for free)")
    g.add_argument("--reviews", type=int, default=2, help="max review rounds (a follow-up checks the fixes)")
    g.add_argument("--no-web", action="store_true", help="don't let the background LLM search the web")
    g.add_argument("--paste", action="store_true", help="no Onshape calls: write a custom feature to paste "
                                                         "into a Feature Studio instead of pushing a native tree")
    g.add_argument("--budget", type=int, help="max Onshape API calls for this run (default ONSHAPE_RUN_BUDGET)")
    g.add_argument("--ask", action="store_true",
                   help="let the model ask clarifying questions first (asked on the terminal; exit 2 with the "
                        "questions as JSON when stdin is not a terminal)")
    g.add_argument("--variant", action="append", metavar="TEXT",
                   help="make a version of the part following this note; repeat to make several (A, B, ...) and compare")
    g.add_argument("--ab", action="store_true", help="make two versions (A and B) without notes and compare them")
    g.add_argument("--custom", action="store_true",
                   help="old mode: one custom feature (FS-Lite) instead of a native feature tree")
    g.set_defaults(fn=cmd_generate)

    s = sub.add_parser("studio", help="native Part Studio scripts: check or push to Onshape")
    s.add_argument("action", choices=["check", "build", "fit", "view", "paste", "push"],
                   help="check: trace + local build + print check (0 calls); build: also export STEP/STL/PNG "
                        "locally; fit: assembled-position interference check from <file>.fit.toml (0 calls); "
                        "view: interactive 3D viewer HTML (0 calls); paste: custom feature to paste into Onshape "
                        "(0 calls); push: native tree via the API")
    s.add_argument("file")
    s.add_argument("--name", help="Part Studio name (default: file name)")
    s.add_argument("--fresh", action="store_true", help="create a new Part Studio instead of updating")
    s.add_argument("--document", help="build in this existing document/branch (URL .../documents/<id>/w/<id>/...) "
                                      "instead of fsgen's own private document; also FSGEN_DOCUMENT")
    s.add_argument("--export", default="", help="also export via API: step,stl (STL ~1 call, STEP ~4)")
    s.add_argument("--metrics", action="store_true", help="fetch parts/volume/area (1 call)")
    s.add_argument("--view", action="store_true", help="save Onshape's isometric render (1 call)")
    s.add_argument("--budget", type=int, help="refuse to push if the estimate exceeds this many calls")
    s.add_argument("--dropbox", action="store_true", help="build: also copy outputs to FSGEN_DROPBOX_DIR")
    s.add_argument("--open", action="store_true", help="build: open the STEP in FreeCAD")
    s.add_argument("--min-wall", type=float, default=1.2, metavar="MM",
                   help="check/build: report walls thinner than this (default 1.2 mm)")
    s.add_argument("--formats", default="step,stl", help="build: comma list of step,stl,3mf (several parts: one file "
                   "per part too, plus a combined 3MF with one object per part)")
    s.add_argument("--fit", help="fit/view: fit file (default: the script's name with .fit.toml)")
    s.add_argument("--public", action="store_true", help="push: create fsgen's document as public (needed on free "
                   "Onshape plans; anyone on Onshape can see it). Also FSGEN_PUBLIC=1")
    s.set_defaults(fn=cmd_studio)

    for name, fn, h in (("run", cmd_run, "build locally and export STEP/STL"),
                        ("push", cmd_push, "upload to Onshape, regenerate, export"),
                        ("eval", cmd_eval, "Onshape diagnostics"),
                        ("watch", cmd_watch, "rebuild on save with a live viewer")):
        p = sub.add_parser(name, help=h)
        p.add_argument("file")
        p.add_argument("-p", "--param", action="append", help="override a parameter, e.g. width=80mm")
        if name in ("run", "push"):
            p.add_argument("--out")
        if name == "run":
            p.add_argument("--png", action="store_true")
        if name == "push":
            p.add_argument("--formats", default="step,stl", help="comma list of step,stl,x_t")
        if name == "watch":
            p.add_argument("--port", type=int, default=8765)
        p.set_defaults(fn=fn)

    em = sub.add_parser("emboss", help="put flat-color SVG art on a native Part Studio script as raised multicolor "
                                       "bodies (0 Onshape calls)")
    em.add_argument("art", metavar="ART.svg", help="flat-color SVG (filled shapes; text must be converted to paths)")
    em.add_argument("part", metavar="PART.fs", help="native Part Studio script with a build function; not overwritten "
                                                    "unless -o PART.fs")
    em.add_argument("-o", "--output", metavar="OUT.fs", help="default: <PART>_emboss.fs next to PART.fs")
    em.add_argument("--plane", choices=["top", "front", "right"], default="top",
                    help="placement plane; art u,v = the plane's sketch x,y, seen from its normal side "
                         "(Top: u=X v=Y; Front: u=X v=Z; Right: u=Y v=Z). Default top")
    em.add_argument("--offset", type=float, default=0.0, metavar="MM",
                    help="plane offset along its normal (flat: where the art stands; with --cylinder: signed distance "
                         "of the cylinder's apex line from the plane)")
    em.add_argument("--flip", action="store_true", help="extrude against the plane normal (faces that look the other way)")
    em.add_argument("--width", type=float, metavar="MM", help="scale the art to this width (default: SVG size)")
    em.add_argument("--center", default="0,0", metavar="U,V", help="art center in plane sketch coordinates, mm")
    em.add_argument("--mirror", action="store_true", help="mirror in u (planes seen from behind)")
    em.add_argument("--height", type=float, default=0.6, metavar="MM", help="height of every color (default 0.6)")
    em.add_argument("--heights", metavar="NAME=MM,...", help="per color name height overrides, e.g. Black=0.4,Gold=0.8")
    em.add_argument("--names", metavar="HEX=NAME,...", help="color names, e.g. '#f5d90a=Gold' (else the nearest common name)")
    em.add_argument("--min-width", type=float, default=0.45, metavar="MM",
                    help="widen color bands thinner than this (default 0.45; 0 disables)")
    em.add_argument("--skip-color", action="append", metavar="HEX", help="drop this color (repeatable), e.g. a white background")
    em.add_argument("--cylinder", type=float, metavar="RADIUS", help="art on a convex barrel face of this radius (mm)")
    em.add_argument("--axis-plane", choices=["top", "front", "right"], default="top",
                    help="with --cylinder: default plane perpendicular to the cylinder axis (default top = vertical axis)")
    em.add_argument("--axis-center", default="0,0", metavar="U,V", help="cylinder axis position in --axis-plane coordinates, mm")
    em.add_argument("--standoff", type=float, default=1.5, metavar="MM", help="cylinder: sketch plane distance outside the apex")
    em.add_argument("--depth-in", type=float, default=3.0, metavar="MM", help="cylinder: how far the prisms reach past the apex into the base")
    em.add_argument("--span", type=float, default=200.0, metavar="MM", help="cylinder: trim extent along the axis (default 200)")
    em.add_argument("--span-side", choices=["both", "normal", "opposite"], default="both",
                    help="cylinder: trim extrudes from --axis-plane both ways (default), along its normal, or against it")
    em.add_argument("--base-name", metavar="NAME", help="name the existing bodies (3MF object / filament)")
    em.add_argument("--base-color", metavar="HEX", help="color of the existing bodies (with --base-name)")
    em.add_argument("--name", default="Emboss", metavar="PREFIX", help="feature id prefix (default Emboss; made unique)")
    em.set_defaults(fn=cmd_emboss)

    rc = sub.add_parser("recognize", help="STEP -> editable Part Studio script (featuretree), 0 calls")
    rc.add_argument("step")
    rc.add_argument("--out")
    rc.set_defaults(fn=cmd_recognize)

    sp = sub.add_parser("spec", help="print the Part Studio script spec, rules, catalog and examples")
    sp.set_defaults(fn=cmd_spec)

    us = sub.add_parser("usage", help="Onshape API calls used vs. the annual allocation")
    us.add_argument("--top", type=int, default=12)
    us.add_argument("--set-official", type=int, metavar="N",
                    help="record Onshape's official used count (e.g. from the MCP get_api_usage tool) as the baseline")
    us.set_defaults(fn=cmd_usage)

    w = sub.add_parser("web", help="LAN web UI (usage, scripts, generation); plain HTTP, password protected")
    w.add_argument("--host", default="0.0.0.0")
    w.add_argument("--port", type=int, default=8770)
    w.add_argument("--password", help="default: $FSGEN_WEB_PASSWORD, else a random one printed at start")
    w.add_argument("--set-password", nargs="?", const="", metavar="PASSWORD",
                   help="save the web password to .env and exit (prompts when PASSWORD is omitted)")
    w.add_argument("--root", default=".", help="project folder to serve (default: current directory)")
    w.set_defaults(fn=cmd_web)

    fe = sub.add_parser("features", help="feature catalog: verified / learned / available")
    fe.add_argument("feature", nargs="?", help="show one feature type with its settable parameters")
    fe.add_argument("--all", action="store_true")
    fe.add_argument("--refresh", action="store_true", help="re-download Onshape feature specs (1 call)")
    fe.add_argument("--markdown", action="store_true", help="print the feature/local-builder table")
    fe.set_defaults(fn=cmd_features)

    c = sub.add_parser("compare", help="local engine vs Onshape on files")
    c.add_argument("files", nargs="+")
    c.set_defaults(fn=cmd_compare)

    args = ap.parse_args(argv)
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
