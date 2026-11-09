---
description: Design a part as a ScriptForge Part Studio script, check it locally, then paste or push to Onshape
argument-hint: <part description>
---

Make this part: $ARGUMENTS

Follow the "Making a part" workflow in AGENTS.md:

1. Run `.venv/bin/scriptforge spec` and follow it (dialect, the user's rules, feature catalog, examples).
2. Ask before designing when the request leaves a design-changing choice open (which style or version, which
   real product, which of several plausible readings) and no sensible default settles it: ask 1-4 questions
   with AskUserQuestion (2-4 options each; include an option "Make both (A and B)" when two readings are
   equally likely). Don't ask about details you can reasonably assume; list those as assumptions instead.
   If the user wants to choose between two versions, make both: `parts/<short-name>-a.fs` and
   `parts/<short-name>-b.fs`, build each in step 3, and show both previews side by side with a one-line
   difference each before asking which one to deliver.
   If the request names a real product, look up its dimensions online first. List your assumptions (every
   dimension or interpretation the request doesn't state) and show them to the user before designing.
   Write the script to `parts/<short-name>.fs`. Briefly state the plan first: variables, sketch planes, feature order.
3. Run `.venv/bin/scriptforge studio build parts/<short-name>.fs`. Fix errors and warnings, then look at the
   preview `out/<short-name>/<short-name>.png` and check it against the request (size, holes, fillets, one part).
   Features listed as "not built locally" are NOT in the preview: say so, don't claim they are checked.
   For printed parts follow the 3D-printing rules (clearances, print orientation, snap fits) and read the print
   check the build prints (overhangs flatter than 45° off the bed), the geometry check and the wall check
   (`--min-wall`); `--formats step,stl,3mf` writes per-part files and a 3MF.
   Several parts that go together (hinge, snap fit, lid, a part around a real component the user supplied as STEP):
   write `parts/<short-name>.fit.toml` (format: `scriptforge/native/fit.py`, example `examples/native/fit/`) and run
   `.venv/bin/scriptforge studio fit parts/<short-name>.fs`; give each motion an `expect`. Fix overlaps before showing
   the result; look at the section PNG it writes (`[section]` in the fit file). `.venv/bin/scriptforge studio view parts/<short-name>.fs` writes an interactive 3D viewer to show the user.
   Repeat until it is right. This step costs no Onshape calls.
4. Show the user the result (key dimensions, parameters, preview path) and the push estimate the command printed.
   Then ask: paste (free) or push as a native tree (costs the estimated calls)?
5. Paste: `.venv/bin/scriptforge studio paste parts/<short-name>.fs --name "<Part name>"` (copies to clipboard and Dropbox).
   Push: `.venv/bin/scriptforge studio push parts/<short-name>.fs --name "<Part name>" --metrics --budget <estimate + 2>`.
6. Note anything new you learned in docs/FINDINGS.md.
