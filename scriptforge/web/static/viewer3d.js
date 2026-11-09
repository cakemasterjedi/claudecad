/* Small reusable three.js viewer: window.scriptforge3d.create(container) -> {show(parts), showStl(buffer, color), dispose()}.
   parts: [{label, color, stl}] with `stl` = base64 binary STL (see scriptforge/native/viewer.py scene_parts).
   showStl takes a saved STL as ArrayBuffer. show() and showStl() resolve to {size: [x, y, z] (model units, mm),
   volume (mm^3)} or null when nothing was shown.
   three.js is loaded on first use from the same CDNs as the standalone viewer; when that fails (offline), the
   container shows a message and show() resolves quietly so the text results keep working. */
(function () {
  "use strict";
  const SCRIPTS = [
    "https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js",
    "https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js",
    "https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/loaders/STLLoader.js",
  ];
  let loading = null;

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = src; s.onload = resolve; s.onerror = () => reject(new Error("could not load " + src));
      document.head.append(s);
    });
  }
  function loadThree() {
    if (window.THREE && THREE.OrbitControls && THREE.STLLoader) return Promise.resolve();
    if (!loading) {
      loading = (async () => { for (const s of SCRIPTS) await loadScript(s); })();
      loading.catch(() => { loading = null; });  // allow a retry later
    }
    return loading;
  }

  function create(container) {
    let ready = false, failed = false, disposed = false, parts = null;
    let renderer, scene, camera, controls, group, raf = 0, ro = null;
    const loader = {};
    const note = document.createElement("div");
    note.className = "v3d-note";
    note.textContent = "Loading 3D viewer...";
    container.append(note);

    function init() {
      renderer = new THREE.WebGLRenderer({antialias: true});
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      container.prepend(renderer.domElement);
      renderer.domElement.className = "v3d-canvas";
      scene = new THREE.Scene();
      camera = new THREE.PerspectiveCamera(35, 1.6, 0.5, 20000);
      camera.up.set(0, 0, 1);
      controls = new THREE.OrbitControls(camera, renderer.domElement);
      controls.enableDamping = true;
      scene.add(new THREE.HemisphereLight(0xffffff, 0x445566, 0.75));
      const key = new THREE.DirectionalLight(0xffffff, 0.75); key.position.set(60, -90, 120); scene.add(key);
      const fill = new THREE.DirectionalLight(0xffffff, 0.3); fill.position.set(-80, 70, 40); scene.add(fill);
      loader.stl = new THREE.STLLoader();
      ro = new ResizeObserver(resize); ro.observe(container);
      resize();
      renderer.setClearColor(new THREE.Color("#0b0e12"));
      (function loop() { raf = requestAnimationFrame(loop); controls.update(); renderer.render(scene, camera); })();
    }
    function resize() {
      if (!renderer) return;
      const w = Math.max(1, container.clientWidth), h = Math.max(1, container.clientHeight);
      renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix();
    }
    function decode(b64) {
      const bin = atob(b64), buf = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
      return decodeBuffer(buf.buffer);
    }
    function decodeBuffer(ab) {
      const g = loader.stl.parse(ab); g.computeVertexNormals();
      return g;
    }
    function volumeOf(g) {  // signed tetrahedra against the origin; assumes a closed, consistently wound mesh
      const p = g.getAttribute("position"), idx = g.index;
      const n = idx ? idx.count : p.count;
      let v = 0;
      for (let i = 0; i < n; i += 3) {
        const a = idx ? idx.getX(i) : i, b = idx ? idx.getX(i + 1) : i + 1, c = idx ? idx.getX(i + 2) : i + 2;
        const ax = p.getX(a), ay = p.getY(a), az = p.getZ(a), bx = p.getX(b), by = p.getY(b), bz = p.getZ(b),
              cx = p.getX(c), cy = p.getY(c), cz = p.getZ(c);
        v += ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx);
      }
      return Math.abs(v / 6);
    }
    function render() {
      if (group) {
        scene.remove(group);
        group.traverse(o => { if (o.geometry) o.geometry.dispose(); if (o.material) o.material.dispose(); });
      }
      group = new THREE.Group();
      let volume = 0;
      for (const p of parts || []) {
        const g = p.geometry || decode(p.stl);
        volume += volumeOf(g);
        group.add(new THREE.Mesh(g, new THREE.MeshStandardMaterial(
          {color: new THREE.Color(p.color), roughness: 0.6, metalness: 0.05, side: THREE.DoubleSide})));
      }
      scene.add(group);
      if (!group.children.length) { note.textContent = "Nothing to show."; note.hidden = false; return null; }
      note.hidden = true;
      const box = new THREE.Box3().setFromObject(group);
      const c = box.getCenter(new THREE.Vector3()), r = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 1e-3);
      const dist = r / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)) * 0.95;
      camera.position.copy(c).add(new THREE.Vector3(0.7, -1.2, 0.9).normalize().multiplyScalar(dist));
      camera.near = Math.max(dist / 1000, 0.01); camera.far = dist * 20; camera.updateProjectionMatrix();
      controls.target.copy(c); controls.update();
      const sz = box.getSize(new THREE.Vector3());
      return {size: [sz.x, sz.y, sz.z], volume};
    }

    const start = loadThree().then(() => {
      if (disposed) return;
      try { init(); ready = true; if (parts) render(); }
      catch (e) { failed = true; note.hidden = false; note.textContent = "3D view unavailable here: " + e.message; }
    }).catch(() => {
      failed = true; note.hidden = false;
      note.textContent = "3D preview needs three.js from a CDN and could not load it (offline?). The results above still apply.";
    });

    async function present(p) {
      parts = p;
      await start;
      if (ready && !disposed) {
        try { return render(); } catch (e) { note.hidden = false; note.textContent = "Could not display the model: " + e.message; }
      }
      return null;
    }
    return {
      show: present,
      async showStl(buffer, color) {
        await start;  // the loader exists only after three.js is ready
        if (!ready || disposed) return null;
        try { return await present([{label: "model", color: color || "#8fb4d9", geometry: decodeBuffer(buffer)}]); }
        catch (e) { note.hidden = false; note.textContent = "Could not read the STL: " + e.message; return null; }
      },
      get failed() { return failed; },
      dispose() {
        disposed = true; cancelAnimationFrame(raf);
        if (ro) ro.disconnect();
        if (renderer) { renderer.dispose(); renderer.domElement.remove(); }
        note.remove();
      },
    };
  }

  window.scriptforge3d = {create};
})();
